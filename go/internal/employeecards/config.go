package employeecards

import (
	"maps"
	"slices"
	"time"

	"douyinboard/internal/accountwrite"
)

type employeeInput struct {
	row dataRow
	err error
}

func (p *employeeInput) text(key, label string, fallback any, limit int, required bool) string {
	if p.err != nil {
		return ""
	}
	value, err := employeeText(valueOr(p.row, key, fallback), label, limit, required)
	p.err = err
	return value
}

func (p *employeeInput) date(key, label, fallback string, optional bool, latest string) string {
	if p.err != nil {
		return ""
	}
	value, err := employeeDate(valueOr(p.row, key, fallback), label, optional, latest)
	p.err = err
	return value
}

func (p *employeeInput) choice(key, label string, fallback any, options ...string) string {
	if p.err != nil {
		return ""
	}
	value, err := employeeChoice(valueOr(p.row, key, fallback), label, options...)
	p.err = err
	return value
}

func (p *employeeInput) number(key, label string, fallback any, min, max int64, nullable bool) any {
	if p.err != nil {
		return nil
	}
	value, err := employeeNumber(valueOr(p.row, key, fallback), label, min, max, nullable)
	p.err = err
	return value
}

func (p *employeeInput) boolean(key string, fallback any, nullable bool, message string) any {
	if p.err != nil {
		return nil
	}
	value := valueOr(p.row, key, fallback)
	if nullable && value == nil {
		return nil
	}
	if _, ok := value.(bool); !ok {
		p.err = accountwrite.Invalid(message)
		return nil
	}
	return value
}

func (w employeeMutation) saveEmployee() (dataRow, error) {
	data, employee := w.plan.Data, w.plan.Employee
	if err := validateFields(data, "name", "employee_no", "birth_date", "employment_status", "office_id",
		"auth_user_id", "consent_source", "consent_on"); err != nil {
		return nil, err
	}
	var old dataRow
	var err error
	if employee != "" {
		old, err = requireEmployee(w.ctx, w.tx, w.actor, employee)
		if err != nil {
			return nil, err
		}
	}
	p := employeeInput{row: data}
	name := p.text("name", "员工姓名", field(old, "name"), 100, true)
	number := p.text("employee_no", "员工编号", field(old, "employee_no"), 80, true)
	birth := p.date("birth_date", "出生日期", field(old, "birth_date"), true, w.today)
	source := p.text("consent_source", "员工授权来源", field(old, "consent_source"), 500, false)
	consent := p.date("consent_on", "员工授权日期", field(old, "consent_on"), true, w.today)
	employment := p.choice("employment_status", "在职状态", valueOr(old, "employment_status", "unknown"), "active", "left", "unknown")
	if p.err != nil {
		return nil, p.err
	}
	if birth != "" && (source == "" || consent == "") {
		return nil, accountwrite.Invalid("保存出生日期前，请登记员工授权来源和日期；不要填写身份证号")
	}
	office, err := validateOffice(w.ctx, w.tx, w.actor, valueOr(data, "office_id", field(old, "office_id")))
	if err != nil {
		return nil, err
	}
	user := valueOr(data, "auth_user_id", old["auth_user_id"])
	if user == "" {
		user = nil
	}
	if user != nil {
		user, err = employeeNumber(user, "已有员工登录身份", 1, 9223372036854775807, false)
		if err != nil {
			return nil, err
		}
	}
	if numberValue(user) != numberValue(old["auth_user_id"]) && w.actor.Role != "owner" {
		return nil, accountwrite.Forbidden("当前账号没有此操作权限")
	}
	if user != nil {
		var present bool
		if err := w.tx.QueryRowContext(w.ctx, `SELECT EXISTS(SELECT 1 FROM board_candidate.auth_users WHERE id=$1)`, user).Scan(&present); err != nil {
			return nil, err
		}
		if !present {
			return nil, accountwrite.Invalid("所绑定的登录身份不存在")
		}
	}
	var duplicate bool
	err = w.tx.QueryRowContext(w.ctx, `SELECT EXISTS(SELECT 1 FROM board_candidate.employee_card_employees
		WHERE (employee_no=$1 OR auth_user_id=$2) AND id<>$3)`, number, user, employee).Scan(&duplicate)
	if err != nil {
		return nil, err
	}
	if duplicate {
		return nil, accountwrite.Invalid("员工编号或登录身份已有关联；请核对原员工记录，不按姓名合并")
	}
	if employee == "" {
		employee = w.plan.NewID
		_, err = w.tx.ExecContext(w.ctx, `INSERT INTO board_candidate.employee_card_employees
			(id,employee_no,name,office_id,auth_user_id,birth_date,employment_status,consent_source,consent_on,created_at)
			VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`, employee, number, name, office, user, birth, employment, source, consent, time.Now().Unix())
	} else {
		_, err = w.tx.ExecContext(w.ctx, `UPDATE board_candidate.employee_card_employees
			SET employee_no=$2,name=$3,office_id=$4,auth_user_id=$5,birth_date=$6,employment_status=$7,
			    consent_source=$8,consent_on=$9,revision=revision+1 WHERE id=$1`,
			employee, number, name, office, user, birth, employment, source, consent)
	}
	if err != nil {
		return nil, err
	}
	err = w.event(employee, "employee", dataRow{"fields": slices.Sorted(maps.Keys(data)), "birth_authorized": birth != "" && source != "" && consent != ""})
	return w.success(employee, employee, "保存员工资料；实名关联不会从卡主或负责人文字推断"), err
}

func defaultText(value any, fallback string) any {
	if value == nil || value == "" {
		return fallback
	}
	return value
}

func (w employeeMutation) saveOperator() (dataRow, error) {
	data := w.plan.Data
	allowed := []string{"id", "name", "network", "channel_id", "age_confirmed", "min_age", "source", "effective_from",
		"verification_valid_days", "release_mode", "release_delay_days", "waiting_confirmed", "waiting_days", "waiting_basis", "occupied_phases"}
	if err := validateFields(data, allowed...); err != nil {
		return nil, err
	}
	id := w.recordID()
	var old dataRow
	var err error
	if field(data, "id") != "" {
		old, err = readOperator(w.ctx, w.tx, id)
		if err != nil {
			return nil, err
		}
	}
	value := dataRow{"id": id}
	for _, field := range allowed[1:] {
		value[field] = valueOr(data, field, old[field])
	}
	for key, fallback := range map[string]string{"network": "unknown", "channel_id": "", "source": "", "effective_from": "",
		"release_mode": "unknown", "waiting_basis": ""} {
		value[key] = defaultText(value[key], fallback)
	}
	p := employeeInput{row: value}
	value["name"] = p.text("name", "运营商名称", "", 40, true)
	value["network"] = p.choice("network", "底层网络", "unknown", "unknown", "unicom", "telecom", "mobile", "broadcast")
	value["channel_id"] = p.text("channel_id", "渠道", "", 80, false)
	value["source"] = p.text("source", "规则依据", "", 500, true)
	value["effective_from"] = p.date("effective_from", "生效日期", "", false, "")
	for _, field := range []string{"age_confirmed", "waiting_confirmed"} {
		value[field] = p.boolean(field, nil, false, "规则确认项必须明确选择")
	}
	for _, field := range []string{"min_age", "verification_valid_days", "release_delay_days", "waiting_days"} {
		maximum := int64(36500)
		if field == "min_age" {
			maximum = 120
		}
		value[field] = p.number(field, field, nil, 0, maximum, true)
	}
	value["release_mode"] = p.choice("release_mode", "释放规则", "unknown", "unknown", "confirmed", "after_days")
	value["waiting_basis"] = p.choice("waiting_basis", "等待期起算依据", "", "", "application", "activation", "release")
	if p.err != nil {
		return nil, p.err
	}
	if truth(value, "age_confirmed") && value["min_age"] == nil {
		return nil, accountwrite.Invalid("确认年龄规则时请明确门槛；无额外门槛应有依据后填0")
	}
	phases, ok := employeeStrings(valueOr(data, "occupied_phases", valueOr(old, "occupied_phases", []string{"active"})))
	if !ok || !slices.Equal(phases, []string{"active"}) {
		return nil, accountwrite.Invalid("已实名、停用、注销处理中或移出列表的卡均继续占额；只有确认释放才结束active占额")
	}
	value["occupied_phases"] = phases
	if value["release_mode"] == "after_days" && value["release_delay_days"] == nil {
		return nil, accountwrite.Invalid("释放延迟天数尚未明确")
	}
	if truth(value, "waiting_confirmed") && value["waiting_days"] == nil {
		return nil, accountwrite.Invalid("请填写有依据的等待天数")
	}
	var duplicate bool
	err = w.tx.QueryRowContext(w.ctx, `SELECT EXISTS(SELECT 1 FROM board_candidate.employee_card_operators
		WHERE name=$1 AND id<>$2)`, value["name"], id).Scan(&duplicate)
	if err != nil {
		return nil, err
	}
	if duplicate {
		return nil, accountwrite.Invalid("这个运营商名称已存在，请编辑原配置")
	}
	raw, err := encodeRow(value)
	if err != nil {
		return nil, err
	}
	if _, err := w.tx.ExecContext(w.ctx, `INSERT INTO board_candidate.employee_card_operators(id,name,payload) VALUES($1,$2,$3)
		ON CONFLICT(id) DO UPDATE SET name=excluded.name,payload=excluded.payload,revision=employee_card_operators.revision+1`,
		id, value["name"], raw); err != nil {
		return nil, err
	}
	err = w.event("", "operator", dataRow{"operator_id": id, "fields": slices.Sorted(maps.Keys(data))})
	return w.success(id, "", "保存运营商规则；未知项不会作为0或无限制"), err
}

func strictRows(value any) ([]dataRow, bool) {
	if rows, ok := value.([]dataRow); ok {
		return rows, true
	}
	raw, ok := value.([]any)
	if !ok {
		return nil, false
	}
	rows := make([]dataRow, 0, len(raw))
	for _, value := range raw {
		row, ok := value.(dataRow)
		if !ok {
			return nil, false
		}
		rows = append(rows, row)
	}
	return rows, true
}

func (w employeeMutation) saveRules() (dataRow, error) {
	if err := validateFields(w.plan.Data, "rules"); err != nil {
		return nil, err
	}
	rules, ok := strictRows(w.plan.Data["rules"])
	if !ok || len(rules) == 0 || len(rules) > 100 {
		return nil, accountwrite.Invalid("请提交1至100条规则")
	}
	seen := map[string]bool{}
	for _, rule := range rules {
		if err := validateFields(rule, "id", "scope", "scope_id", "period", "limit", "pending_counts", "source", "effective_from", "confirmed", "revision"); err != nil {
			return nil, err
		}
		p := employeeInput{row: rule}
		id := p.text("id", "规则编号", "", 80, true)
		scope := p.choice("scope", "规则范围", "", "all", "operator", "network", "channel")
		scopeID := p.text("scope_id", "规则范围编号", "", 80, false)
		period := p.choice("period", "统计窗口", "", "stock", "day", "month")
		limit := p.number("limit", "数量上限", nil, 0, 100000, true)
		source := p.text("source", "规则依据", "", 500, true)
		effective := p.date("effective_from", "规则生效日期", "", false, "")
		const message = "规则确认或在途占额选项不正确"
		confirmed := p.boolean("confirmed", nil, false, message)
		pending := p.boolean("pending_counts", nil, true, message)
		if p.err != nil {
			return nil, p.err
		}
		if seen[id] {
			return nil, accountwrite.Invalid("规则编号重复")
		}
		seen[id] = true
		if (scope == "all") != (scopeID == "") {
			return nil, accountwrite.Invalid("请明确规则适用范围")
		}
		if scope == "operator" {
			if _, err := readOperator(w.ctx, w.tx, scopeID); err != nil {
				return nil, err
			}
		}
		raw, err := encodeRow(dataRow{"id": id, "scope": scope, "scope_id": scopeID, "period": period, "limit": limit,
			"source": source, "effective_from": effective, "confirmed": confirmed, "pending_counts": pending})
		if err != nil {
			return nil, err
		}
		if _, err := w.tx.ExecContext(w.ctx, `INSERT INTO board_candidate.employee_card_rules(id,payload) VALUES($1,$2)
			ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,revision=employee_card_rules.revision+1`, id, raw); err != nil {
			return nil, err
		}
	}
	err := w.event("", "rules", dataRow{"rule_ids": slices.Sorted(maps.Keys(seen))})
	return w.success("rules", "", "保存明确提交的规则；未提交的既有规则保留"), err
}
