package employeecards

import (
	"errors"
	"slices"
	"strconv"
)

// evaluateEmployee evaluates only confirmed rules and evidence. Unknown counts
// stay nullable, even when recorded company counts are available.
func evaluateEmployee(employee quotaRow, operators, units, verifications, applications, rules []quotaRow, today string) (quotaRow, error) {
	if quotaDate(today) == "" {
		return nil, errors.New("评估日期不正确")
	}
	if employee == nil {
		return nil, errors.New("员工资料格式不正确")
	}
	operators, issues := quotaRows(operators, "operator")
	units, unitIssues := quotaRows(units, "unit")
	checks, checkIssues := quotaRows(verifications, "verification")
	apps, appIssues := quotaRows(applications, "application")
	rules, ruleIssues := quotaRows(rules, "rule")
	for _, group := range [][]string{unitIssues, checkIssues, appIssues, ruleIssues} {
		for _, code := range group {
			issues = quotaAddIssue(issues, code)
		}
	}
	if !quotaText(employee["id"]) {
		issues = quotaAddIssue(issues, "employee_id_invalid")
	}
	operatorByID := make(map[string]quotaRow, len(operators))
	for _, operator := range operators {
		operatorByID[quotaString(operator["id"])] = operator
	}
	prepared := make([]quotaUnit, 0, len(units))
	for _, unit := range units {
		operator := operatorByID[quotaString(unit["operator_id"])]
		if operator == nil {
			issues = quotaAddIssue(issues, "unit_operator_unknown")
		}
		prepared = append(prepared, quotaUnitInfo(unit, operator, today))
	}
	var age any
	if employee["consent_confirmed"] == true {
		age = quotaAge(quotaDate(employee["birth_date"]), today)
	}
	output := make([]quotaRow, 0, len(operators))
	globalLimits := make([]quotaLimit, 0)
	for _, operator := range operators {
		result, global := quotaEvaluateOperator(employee, operator, age, prepared, checks, apps, rules, issues, today)
		output = append(output, result)
		globalLimits = append(globalLimits, global...)
	}
	var globalStock, globalEvidence *quotaLimit
	for i := range globalLimits {
		limit := &globalLimits[i]
		if globalStock == nil && limit.verified != nil {
			globalStock = limit
		}
		if globalEvidence == nil && limit.other != nil {
			globalEvidence = limit
		}
	}
	if globalStock != nil {
		globalEvidence = globalStock
	}
	var totalAvailable, other, verified any
	if globalEvidence != nil {
		other = quotaValue(globalEvidence.other)
	}
	if globalStock != nil {
		verified = quotaValue(globalStock.verified)
		if globalStock.available != nil && len(output) > 0 {
			allKnown, sum := true, int64(0)
			for _, result := range output {
				if count, ok := quotaNumber(result["estimated_available"]); ok {
					// Saturation at the global ceiling avoids overflow with many carriers.
					sum += min(count, *globalStock.available-sum)
				} else {
					allKnown = false
				}
			}
			if allKnown {
				totalAvailable = sum
			}
		}
	}
	lifetime, current, pending := quotaUnitCounts(prepared, "", false)
	counts := quotaRow{"company_lifetime": lifetime, "company_current": current, "other_current": other,
		"verified_total": verified, "pending": pending, "estimated_available": totalAvailable}
	nextDate, action := quotaEmployeeFollowup(output)
	basis := quotaCountBasis(globalEvidence)
	return quotaRow{"employee_id": employee["id"], "age_years": age, "company_lifetime": lifetime, "company_current": current,
		"other_current": other, "verified_total": verified, "pending_count": pending, "estimated_available": totalAvailable,
		"company_inventory_complete": basis["company_inventory_complete"], "count_basis": basis, "next_followup_on": quotaStringOrNil(nextDate),
		"followup_action": quotaStringOrNil(action), "counts": counts, "operators": output, "issues": issues}, nil
}

func quotaEvaluateOperator(employee, operator quotaRow, age any, units []quotaUnit, checks, apps, rules []quotaRow, issues []string, today string) (quotaRow, []quotaLimit) {
	id := quotaString(operator["id"])
	reasons := make([]quotaRow, 0)
	applyDates, verifyDates := []string{}, []string{}
	for _, issue := range issues {
		reasons = append(reasons, quotaReason(issue, "资料存在缺失或冲突，需核验后计算额度", "unknown"))
	}
	if complete, exists := employee["company_scope_complete"]; exists && complete != true {
		reasons = append(reasons, quotaReason("company_scope_incomplete", "公司卡资料范围不完整，不能据局部资料推算名下总额", "unknown"))
	}
	switch employee["employment_status"] {
	case "left":
		reasons = append(reasons, quotaReason("employee_left", "员工已离职，公司办卡需人工确认", "unknown"))
	case "active":
	default:
		reasons = append(reasons, quotaReason("employment_unknown", "在职状态待确认", "unknown"))
	}
	if employee["consent_confirmed"] != true {
		reasons = append(reasons, quotaReason("consent_unknown", "尚未记录员工授权提供资料的依据", "unknown"))
	}
	gate := ""
	ageMinimum, validAge := quotaNumber(operator["min_age"])
	ageEffective := quotaDate(operator["effective_from"])
	switch {
	case operator["age_confirmed"] != true || !validAge || !quotaText(operator["source"]) || ageEffective == "" || ageEffective > today:
		reasons = append(reasons, quotaReason("age_rule_unknown", "运营商年龄门槛尚未确认", "unknown"))
	case age == nil:
		reasons = append(reasons, quotaReason("birth_date_unknown", "出生日期缺失、无效或尚未经授权确认", "unknown"))
	default:
		gate = quotaThreshold(quotaDate(employee["birth_date"]), ageMinimum)
		if gate == "" {
			reasons = append(reasons, quotaReason("age_rule_invalid", "年龄门槛或出生日期无效", "unknown"))
		} else if today < gate {
			reasons = append(reasons, quotaReason("below_minimum_age", "尚未达到满"+strconv.FormatInt(ageMinimum, 10)+"周岁的年龄门槛", "blocking"))
			applyDates = append(applyDates, gate)
		}
	}
	active := make([]quotaRow, 0, len(rules))
	hasGlobal, hasDaily, hasNetwork, hasChannel := false, false, false, false
	for _, rule := range rules {
		hasNetwork = hasNetwork || rule["scope"] == "network"
		hasChannel = hasChannel || rule["scope"] == "channel"
		if quotaApplicable(rule, operator) {
			active = append(active, rule)
			hasGlobal = hasGlobal || rule["scope"] == "all" && rule["period"] == "stock"
			hasDaily = hasDaily || rule["scope"] == "operator" && rule["period"] == "day"
		}
	}
	if !hasGlobal {
		reasons = append(reasons, quotaReason("global_stock_rule_missing", "缺少覆盖所有运营商及虚商的总占用规则", "unknown"))
	}
	if !hasDaily {
		reasons = append(reasons, quotaReason("operator_day_rule_missing", "单运营商每日实名数量规则待确认", "unknown"))
	}
	if hasNetwork && !quotaKnownNetwork(operator["network"]) {
		reasons = append(reasons, quotaReason("operator_network_unknown", "实际发卡网络待确认，不能排除网络合并限额", "unknown"))
	}
	if hasChannel && !quotaText(operator["channel_id"]) {
		reasons = append(reasons, quotaReason("operator_channel_unknown", "办理渠道待确认，不能排除渠道限额", "unknown"))
	}
	limits := make([]quotaLimit, 0, len(active))
	global := make([]quotaLimit, 0)
	public := make([]quotaRow, 0, len(active))
	var stock, globalLimit *quotaLimit
	var latestVerification string
	for _, rule := range active {
		limit := quotaEvaluateLimit(rule, operator, units, checks, today)
		limits = append(limits, limit)
		public = append(public, limit.public())
		if limit.scope == "all" && limit.period == "stock" {
			global = append(global, limit)
			if globalLimit == nil {
				globalLimit = &limits[len(limits)-1]
			}
		}
		if stock == nil && limit.scope == "operator" && limit.period == "stock" {
			stock = &limits[len(limits)-1]
		}
		for _, code := range limit.issues {
			reasons = append(reasons, quotaReason(code, quotaIssueLabel(code), "unknown"))
		}
		if limit.followup != "" {
			verifyDates = append(verifyDates, limit.followup)
		}
		if limit.available != nil && *limit.available == 0 {
			reasons = append(reasons, quotaReason("capacity_exhausted", "当前规则范围内已无未分配名额", "blocking"))
			if _, end := quotaWindow(limit.period, today); end != "" {
				if next := quotaDatePlus(end, 1); next != "" {
					applyDates = append(applyDates, next)
				}
			}
		}
		latestVerification = max(latestVerification, limit.verificationOn)
	}
	reasons, applyDates = quotaApplicationWait(operator, units, apps, reasons, applyDates, today)
	evidence := quotaOperatorEvidence(operator, units, checks, today)
	if reconciled, ok := quotaNumber(evidence["reconciled_used"]); ok && globalLimit != nil && globalLimit.used != nil && reconciled > *globalLimit.used {
		const code = "cross_scope_total_conflict"
		label := quotaIssueLabel(code)
		reasons = append(reasons, quotaReason(code, label, "unknown"))
		evidence["issues"] = append(evidence["issues"].([]string), code)
		evidence["issue_labels"] = append(evidence["issue_labels"].([]string), label)
	}
	unknown, blocked, unresolved := false, false, false
	for _, reason := range reasons {
		unknown = unknown || reason["kind"] == "unknown"
		blocked = blocked || reason["kind"] == "blocking"
		unresolved = unresolved || reason["code"] == "failure_needs_resolution"
	}
	status := "eligible"
	if blocked {
		status = "ineligible"
	} else if unknown {
		status = "needs_verification"
	}
	capacity := quotaAvailable(limits, unknown, blocked)
	lifetime, current, pending := quotaUnitCounts(units, id, true)
	other, verified, basis := evidence["other_current"], evidence["verified_total"], evidence["count_basis"].(quotaRow)
	if stock != nil {
		other, verified, basis = quotaValue(stock.other), quotaValue(stock.verified), quotaCountBasis(stock)
	}
	counts := quotaRow{"company_lifetime": lifetime, "company_current": current, "other_current": other,
		"verified_total": verified, "pending": pending, "estimated_available": capacity}
	followup, action := quotaOperatorFollowup(limits, applyDates, verifyDates, unresolved, unknown, blocked)
	return quotaRow{"operator_id": id, "status": status, "status_label": quotaLabels[status], "reasons": reasons,
		"estimated_available": capacity, "age_threshold_on": quotaStringOrNil(gate), "next_followup_on": quotaStringOrNil(followup),
		"followup_action": quotaStringOrNil(action), "verification_on": quotaStringOrNil(latestVerification), "counts": counts,
		"company_inventory_complete": basis["company_inventory_complete"], "count_basis": basis, "count_evidence": evidence,
		"limits": public}, global
}

func quotaApplicationWait(operator quotaRow, units []quotaUnit, apps, reasons []quotaRow, applyDates []string, today string) ([]quotaRow, []string) {
	id := quotaString(operator["id"])
	related := make([]quotaRow, 0)
	for _, app := range apps {
		if app["operator_id"] != id {
			continue
		}
		related = append(related, app)
		retry, sourced := quotaDate(app["retry_on"]), quotaTruth(app["retry_source"])
		if quotaTruth(app["retry_on"]) && (retry == "" || !sourced) {
			reasons = append(reasons, quotaReason("retry_date_unconfirmed", "再次申请日期缺少运营商明确依据", "unknown"))
		} else if retry != "" && sourced && retry > today {
			reasons = append(reasons, quotaReason("explicit_retry_wait", "尚未到运营商明确给出的再次申请日期", "blocking"))
			applyDates = append(applyDates, retry)
		}
		if app["result"] == "failed" && app["failure_resolved"] != true && (retry == "" || !sourced) {
			reasons = append(reasons, quotaReason("failure_needs_resolution", "失败原因或再次办理条件待核实，不自动安排每天重试", "unknown"))
		}
	}
	wait, confirmed := quotaNumber(operator["waiting_days"])
	if operator["waiting_confirmed"] != true || !confirmed {
		return append(reasons, quotaReason("waiting_rule_unknown", "是否存在再次申请等待期尚未确认", "unknown")), applyDates
	}
	if wait == 0 {
		return reasons, applyDates
	}
	var latest string
	switch operator["waiting_basis"] {
	case "application":
		missing := false
		for _, app := range related {
			day := quotaDate(app["applied_on"])
			latest = max(latest, day)
			missing = missing || day == ""
		}
		if missing {
			reasons = append(reasons, quotaReason("waiting_history_date_unknown", "等待期所需的申请日期不完整", "unknown"))
		}
	case "activation", "release":
		for _, unit := range units {
			if unit.operator != id {
				continue
			}
			date := unit.activated
			if operator["waiting_basis"] == "release" {
				date = unit.released
			}
			latest = max(latest, date)
		}
	default:
		reasons = append(reasons, quotaReason("waiting_basis_unknown", "等待期起算依据尚未确认", "unknown"))
	}
	if retry := quotaDatePlus(latest, wait); retry > today {
		reasons = append(reasons, quotaReason("configured_wait", "尚未达到已确认等待期", "blocking"))
		applyDates = append(applyDates, retry)
	}
	return reasons, applyDates
}

func quotaOperatorFollowup(limits []quotaLimit, applyDates, verifyDates []string, unresolved, unknown, blocked bool) (string, string) {
	switch {
	case unresolved:
		return "", "resolve"
	case unknown:
		dates := append(verifyDates, applyDates...)
		if len(dates) == 0 {
			return "", "verify"
		}
		return slices.Min(dates), "verify"
	case blocked:
		indefinite := slices.ContainsFunc(limits, func(limit quotaLimit) bool {
			return limit.period == "stock" && limit.available != nil && *limit.available == 0
		})
		if len(applyDates) > 0 && !indefinite {
			return slices.Max(applyDates), "apply"
		}
		return "", "verify"
	case len(verifyDates) > 0:
		return slices.Min(verifyDates), "verify"
	default:
		return "", ""
	}
}

func quotaEmployeeFollowup(output []quotaRow) (string, string) {
	date, action := "", ""
	resolve := false
	for _, result := range output {
		next, kind := quotaString(result["next_followup_on"]), quotaString(result["followup_action"])
		if next != "" && (date == "" || next < date || next == date && kind < action) {
			date, action = next, kind
		}
		resolve = resolve || kind == "resolve"
	}
	if date == "" && len(output) > 0 {
		action = "verify"
		if resolve {
			action = "resolve"
		}
	}
	return date, action
}

func quotaUnitCounts(units []quotaUnit, operator string, filter bool) (issued, occupied, pending int64) {
	for _, unit := range units {
		if filter && unit.operator != operator {
			continue
		}
		if unit.issued {
			issued++
		}
		if unit.current {
			occupied++
		}
		if unit.pending {
			pending++
		}
	}
	return issued, occupied, pending
}

func quotaAvailable(limits []quotaLimit, unknown, blocked bool) any {
	if unknown || len(limits) == 0 {
		return nil
	}
	if blocked {
		return int64(0)
	}
	if limits[0].available == nil {
		return nil
	}
	value := *limits[0].available
	for _, limit := range limits[1:] {
		if limit.available == nil {
			return nil
		}
		value = min(value, *limit.available)
	}
	return value
}

func quotaApplicable(rule, operator quotaRow) bool {
	id := quotaString(rule["scope_id"])
	switch rule["scope"] {
	case "operator":
		return id == quotaString(operator["id"])
	case "network":
		return quotaKnownNetwork(operator["network"]) && id == quotaString(operator["network"])
	case "channel":
		return quotaText(operator["channel_id"]) && id == quotaString(operator["channel_id"])
	default:
		return true
	}
}

func quotaReason(code, text, kind string) quotaRow {
	return quotaRow{"code": code, "text": text, "kind": kind}
}
