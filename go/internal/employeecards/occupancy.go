package employeecards

import (
	"slices"
	"time"

	"douyinboard/internal/accountwrite"
)

func (w employeeMutation) saveApplication() (dataRow, error) {
	data, employee := w.plan.Data, w.plan.Employee
	if err := validateFields(data, "id", "operator_id", "requested_on", "applied_on", "quantity", "result", "failure_reason",
		"retry_on", "retry_source", "failure_resolved", "source"); err != nil {
		return nil, err
	}
	id := w.recordID()
	var old dataRow
	var err error
	if field(data, "id") != "" {
		old, err = getOne(w.ctx, w.tx, `SELECT to_json(a)::text FROM board_candidate.employee_card_applications a WHERE id=$1 AND employee_id=$2`, id, employee)
		if err != nil {
			return nil, err
		}
		if old == nil {
			return nil, accountwrite.Invalid("申请记录不存在")
		}
	}
	op, err := employeeText(valueOr(data, "operator_id", field(old, "operator_id")), "发卡运营商", 80, true)
	if err != nil {
		return nil, err
	}
	if _, err := readOperator(w.ctx, w.tx, op); err != nil {
		return nil, err
	}
	day, err := employeeDate(valueOr(data, "requested_on", valueOr(data, "applied_on", field(old, "applied_on"))), "申请日期", false, w.today)
	if err != nil {
		return nil, err
	}
	p := employeeInput{row: data}
	quantity := p.number("quantity", "申请数量", old["quantity"], 1, 100, false)
	result := p.choice("result", "申请结果", valueOr(old, "result", "pending"), "pending", "succeeded", "partial", "failed", "cancelled")
	failure := p.text("failure_reason", "失败原因", "", 1000, false)
	retry := p.date("retry_on", "再次申请日期", "", true, "")
	retrySource := p.text("retry_source", "再次申请日期依据", "", 500, false)
	source := p.text("source", "申请来源", "", 500, false)
	if p.err != nil {
		return nil, p.err
	}
	if retry != "" && retrySource == "" {
		return nil, accountwrite.Invalid("请填写运营商给出再次申请日期的依据")
	}
	if old != nil && (numberValue(quantity) != numberValue(old["quantity"]) || op != old["operator_id"] || day != old["applied_on"]) {
		return nil, accountwrite.Invalid("申请数量、运营商和原日期需保留；请新增一次申请记录")
	}
	closed := result == "failed" || result == "cancelled"
	if (old["result"] == "failed" || old["result"] == "cancelled") && !closed {
		return nil, accountwrite.Invalid("已结束的申请不能重新占用旧名额，请登记新申请")
	}
	units := w.plan.UnitIDs
	if old != nil {
		units = []string{}
		rows, err := w.tx.QueryContext(w.ctx, `SELECT id FROM board_candidate.employee_card_units WHERE application_id=$1 ORDER BY id`, id)
		if err != nil {
			return nil, err
		}
		defer rows.Close()
		for rows.Next() {
			var unit string
			if err := rows.Scan(&unit); err != nil {
				return nil, err
			}
			units = append(units, unit)
		}
		if err := rows.Err(); err != nil {
			return nil, err
		}
	} else if int64(len(units)) != numberValue(quantity) {
		return nil, accountwrite.Invalid("申请占额单元与申请数量不一致，请重新预览")
	}
	raw, err := encodeRow(dataRow{"unit_ids": units, "failure_reason": failure, "retry_on": retry, "retry_source": retrySource,
		"failure_resolved": truth(data, "failure_resolved"), "source": source})
	if err != nil {
		return nil, err
	}
	if old != nil {
		if _, err := w.tx.ExecContext(w.ctx, `UPDATE board_candidate.employee_card_applications
			SET result=$2,payload=$3,revision=revision+1 WHERE id=$1`, id, result, raw); err != nil {
			return nil, err
		}
		if closed {
			if _, err := w.tx.ExecContext(w.ctx, `UPDATE board_candidate.employee_card_units
				SET phase=$2,revision=revision+1 WHERE application_id=$1 AND phase='pending'`, id, result); err != nil {
				return nil, err
			}
		}
	} else {
		if _, err := w.tx.ExecContext(w.ctx, `INSERT INTO board_candidate.employee_card_applications
			(id,employee_id,operator_id,applied_on,result,quantity,payload) VALUES($1,$2,$3,$4,$5,$6,$7)`,
			id, employee, op, day, result, quantity, raw); err != nil {
			return nil, err
		}
		phase := "pending"
		if closed {
			phase = result
		}
		if _, err := w.tx.ExecContext(w.ctx, `INSERT INTO board_candidate.employee_card_units
			(id,employee_id,operator_id,application_id,phase,requested_on,source)
			SELECT u,$2,$3,$4,$5,$6,$7 FROM unnest($1::text[]) u`, units, employee, op, id, phase, day, source); err != nil {
			return nil, err
		}
	}
	err = w.event(employee, "application", dataRow{"application_id": id, "quantity": quantity, "result": result,
		"failure_reason": failure, "retry_on": retry, "retry_source": retrySource})
	return w.success(id, employee, "记录申请和稳定在途单元；成功后关联已有卡，不重复新增名额"), err
}

func unknownNetwork(operatorID, today string) dataRow {
	return dataRow{"network": "unknown", "channel_id": "", "operator_id": operatorID,
		"source": "发卡时实际网络尚未核验；不根据当前品牌配置倒推", "effective_from": "", "recorded_on": today}
}

func networkPolicy(operator dataRow, activated, today string) dataRow {
	id := field(operator, "id")
	effective := field(operator, "effective_from")
	if field(operator, "source") == "" || effective == "" || effective > activated {
		return unknownNetwork(id, today)
	}
	result := dataRow{"operator_id": id, "recorded_on": today}
	for _, key := range []string{"network", "channel_id", "source", "effective_from"} {
		result[key] = valueOr(operator, key, "")
	}
	return result
}

func frozenNetwork(unit dataRow, operators map[string]dataRow) (string, string, error) {
	policy := resultRow(unit["network_policy"])
	if raw := field(unit, "network_policy"); raw != "" {
		if err := decodeJSON(raw, &policy); err != nil {
			return "", "", err
		}
	}
	if policy == nil && unit["phase"] == "pending" {
		policy = operators[field(unit, "operator_id")]
	}
	if policy == nil {
		return "unknown", "", nil
	}
	return field(policy, "network"), field(policy, "channel_id"), nil
}

func (w employeeMutation) recordRelease() (dataRow, error) {
	data, employee := w.plan.Data, w.plan.Employee
	if err := validateFields(data, "unit_id", "requested_on", "released_on", "source"); err != nil {
		return nil, err
	}
	unit, err := readUnit(w.ctx, w.tx, employee, field(data, "unit_id"))
	if err != nil {
		return nil, err
	}
	id := field(unit, "id")
	source, err := employeeText(valueOr(data, "source", ""), "注销或释放依据", 500, true)
	if err != nil {
		return nil, err
	}
	if w.plan.Action == "cancel_request" {
		requested, err := employeeDate(valueOr(data, "requested_on", ""), "注销申请日期", false, w.today)
		if err != nil {
			return nil, err
		}
		if unit["phase"] != "active" || requested < field(unit, "activated_on") {
			return nil, accountwrite.Invalid("只能为已办理且尚未释放的实名卡登记注销申请")
		}
		cancel := field(unit, "cancelled_on")
		if cancel != "" && (cancel != requested || unit["cancel_source"] != source) {
			return nil, accountwrite.Invalid("已记录注销申请，请核对历史后登记确认释放")
		}
		if cancel == "" {
			if _, err := w.tx.ExecContext(w.ctx, `UPDATE board_candidate.employee_card_units
				SET cancelled_on=$2,cancel_source=$3,revision=revision+1 WHERE id=$1`, id, requested, source); err != nil {
				return nil, err
			}
		}
	} else {
		released, err := employeeDate(valueOr(data, "released_on", ""), "确认释放日期", false, w.today)
		if err != nil {
			return nil, err
		}
		if unit["phase"] == "released" {
			if unit["release_confirmed_on"] != released || unit["release_source"] != source {
				return nil, accountwrite.Invalid("该名额已释放，不能再次释放或覆盖原依据")
			}
			return w.success(id, employee, "此前已确认释放，本次不重复释放"), nil
		}
		op, err := readOperator(w.ctx, w.tx, field(unit, "operator_id"))
		if err != nil {
			return nil, err
		}
		cancelled := field(unit, "cancelled_on")
		if unit["phase"] != "active" || cancelled == "" || released < cancelled {
			return nil, accountwrite.Invalid("请先记录注销申请，再依据运营商确认名额释放")
		}
		mode := field(op, "release_mode")
		if field(op, "effective_from") > released || (mode != "confirmed" && mode != "after_days") {
			return nil, accountwrite.Invalid("该运营商名额释放规则未确认，注销处理中仍占名额")
		}
		if mode == "after_days" {
			from, err := time.Parse(time.DateOnly, cancelled)
			delay, known := numeric(op["release_delay_days"])
			if err != nil || !known || delay < 0 || delay > 36500 {
				return nil, accountwrite.Invalid("该运营商名额释放规则未确认，注销处理中仍占名额")
			}
			if released < from.AddDate(0, 0, int(delay)).Format(time.DateOnly) {
				return nil, accountwrite.Invalid("尚未达到已确认的注销释放日期")
			}
		}
		policy := dataRow{"operator_id": unit["operator_id"]}
		for _, key := range []string{"release_mode", "release_delay_days", "source", "effective_from"} {
			policy[key] = op[key]
		}
		raw, err := encodeRow(policy)
		if err != nil {
			return nil, err
		}
		if _, err := w.tx.ExecContext(w.ctx, `UPDATE board_candidate.employee_card_units
			SET phase='released',release_confirmed_on=$2,release_source=$3,release_policy=$4,revision=revision+1 WHERE id=$1`,
			id, released, source, raw); err != nil {
			return nil, err
		}
	}
	err = w.event(employee, w.plan.Action, dataRow{"unit_id": id, "source": source, "date": valueOr(data, "released_on", data["requested_on"])})
	return w.success(id, employee, "注销申请仍占名额；只有明确确认释放才减少当前占用"), err
}

func (w employeeMutation) saveVerification() (dataRow, error) {
	data, employee, id := w.plan.Data, w.plan.Employee, w.plan.NewID
	if err := validateFields(data, "scope", "scope_id", "period", "period_start", "period_end", "count", "coverage",
		"verified_on", "valid_until", "source", "pending_unit_ids", "company_inventory_complete"); err != nil {
		return nil, err
	}
	p := employeeInput{row: data}
	scope := p.choice("scope", "核验范围", "", "all", "operator", "network", "channel")
	scopeID := p.text("scope_id", "核验范围编号", "", 80, false)
	period := p.choice("period", "核验统计窗口", "", "stock", "day", "month")
	start := p.date("period_start", "统计开始日期", "", false, "")
	end := p.date("period_end", "统计结束日期", "", false, "")
	count := p.number("count", "核验数量", nil, 0, 100000, false)
	coverage := p.choice("coverage", "公司卡是否已包括", "", "total", "external", "unknown")
	verified := p.date("verified_on", "核验日期", "", false, w.today)
	validUntil := p.date("valid_until", "核验有效期", "", true, "")
	source := p.text("source", "核验来源", "", 500, true)
	complete := p.boolean("company_inventory_complete", false, false, "公司实名卡台账完整性必须明确选择，不能以空值或数字替代")
	if p.err != nil {
		return nil, p.err
	}
	if start > end || (validUntil != "" && validUntil < verified) {
		return nil, accountwrite.Invalid("核验统计范围或有效日期不正确")
	}
	if (scope == "all") != (scopeID == "") {
		return nil, accountwrite.Invalid("请明确核验是否涵盖全部基础运营商与虚商")
	}
	if scope == "operator" {
		if _, err := readOperator(w.ctx, w.tx, scopeID); err != nil {
			return nil, err
		}
	}
	units, err := getRows(w.ctx, w.tx, `SELECT to_json(u)::text FROM board_candidate.employee_card_units u WHERE employee_id=$1 ORDER BY id`, employee)
	if err != nil {
		return nil, err
	}
	rows, err := getRows(w.ctx, w.tx, `SELECT to_json(o)::text FROM board_candidate.employee_card_operators o`)
	if err != nil {
		return nil, err
	}
	operators := map[string]dataRow{}
	for _, row := range rows {
		op, err := payloadRow(row)
		if err != nil {
			return nil, err
		}
		operators[field(row, "id")] = op
	}
	company, validPending := []string{}, map[string]bool{}
	for _, unit := range units {
		if scope == "operator" && unit["operator_id"] != scopeID {
			continue
		}
		if scope == "network" || scope == "channel" {
			network, channel, err := frozenNetwork(unit, operators)
			if err != nil {
				return nil, err
			}
			if (scope == "network" && network != scopeID) || (scope == "channel" && channel != scopeID) {
				continue
			}
		}
		unitID := field(unit, "id")
		if unit["phase"] == "pending" {
			validPending[unitID] = true
		}
		activated, released := field(unit, "activated_on"), field(unit, "release_confirmed_on")
		if activated != "" && activated <= verified && (period == "stock" || (start <= activated && activated <= end)) &&
			(period != "stock" || released == "" || released > verified) {
			company = append(company, unitID)
		}
	}
	pending, ok := employeeStrings(valueOr(data, "pending_unit_ids", []string{}))
	if !ok {
		return nil, accountwrite.Invalid("已计入核验的在途单元需逐条选择")
	}
	slices.Sort(pending)
	for i, key := range pending {
		if i > 0 && pending[i-1] == key {
			return nil, accountwrite.Invalid("已计入核验的在途单元需逐条选择")
		}
		if !validPending[key] || coverage != "total" {
			return nil, accountwrite.Invalid("在途核验关联不正确；仅完整总数可包含公司在途")
		}
	}
	included := []string{}
	if coverage == "total" {
		included = company
		if numberValue(count) < int64(len(included)+len(pending)) {
			return nil, accountwrite.Invalid("核验总数小于已明确包括的公司数量，请核对来源与范围")
		}
	}
	value := dataRow{"scope": scope, "scope_id": scopeID, "period": period, "period_start": start, "period_end": end,
		"count": count, "coverage": coverage, "verified_on": verified, "valid_until": validUntil, "source": source,
		"company_inventory_complete": complete, "company_snapshot_ids": company, "unit_ids": included,
		"pending_unit_ids": pending, "recorded_at": time.Now().UnixNano()}
	raw, err := encodeRow(value)
	if err != nil {
		return nil, err
	}
	if _, err := w.tx.ExecContext(w.ctx, `INSERT INTO board_candidate.employee_card_verifications
		(id,employee_id,payload,created_at) VALUES($1,$2,$3,$4)`, id, employee, raw, time.Now().Unix()); err != nil {
		return nil, err
	}
	err = w.event(employee, "verification", dataRow{"verification_id": id, "scope": scope, "scope_id": scopeID,
		"period": period, "count": count, "source": source, "company_inventory_complete": complete})
	return w.success(id, employee, "记录来源、范围与公司卡快照；不会把总数再加一次公司卡"), err
}
