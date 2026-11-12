package employeecards

import (
	"encoding/json"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"time"
)

type quotaRow = map[string]any

var quotaLabels = map[string]string{"eligible": "符合已知申请条件", "ineligible": "暂不符合", "needs_verification": "待核验"}

func quotaString(value any) string {
	text, _ := value.(string)
	return text
}

func quotaText(value any) bool { return strings.TrimSpace(quotaString(value)) != "" }

func quotaNumber(value any) (int64, bool) {
	switch number := value.(type) {
	case int:
		return int64(number), number >= 0
	case int64:
		return number, number >= 0
	case json.Number:
		n, err := number.Int64()
		return n, err == nil && n >= 0
	default:
		return 0, false
	}
}

func quotaDate(value any) string {
	text := quotaString(value)
	day, err := time.Parse(time.DateOnly, text)
	if err != nil || day.Year() < 1 || day.Format(time.DateOnly) != text {
		return ""
	}
	return text
}

func quotaDatePlus(day string, count int64) string {
	if day == "" || count < 0 || count > 3652059 {
		return ""
	}
	value, _ := time.Parse(time.DateOnly, day)
	return quotaDate(value.AddDate(0, 0, int(count)).Format(time.DateOnly))
}

func quotaWindow(period, today string) (string, string) {
	switch period {
	case "day":
		return today, today
	case "month":
		start, _ := time.Parse(time.DateOnly, today[:7]+"-01")
		return start.Format(time.DateOnly), start.AddDate(0, 1, -1).Format(time.DateOnly)
	default:
		return "", ""
	}
}

func quotaAge(birth, today string) any {
	if birth == "" || birth > today {
		return nil
	}
	start, _ := strconv.Atoi(birth[:4])
	current, _ := strconv.Atoi(today[:4])
	if today[5:] < birth[5:] {
		current--
	}
	return current - start
}

func quotaThreshold(birth string, minimum int64) string {
	if birth == "" || minimum > 150 {
		return ""
	}
	day, _ := time.Parse(time.DateOnly, birth)
	return quotaDate(day.AddDate(int(minimum), 0, 0).Format(time.DateOnly))
}

func quotaStringOrNil(text string) any {
	if text == "" {
		return nil
	}
	return text
}

func quotaRows(rows []quotaRow, label string) ([]quotaRow, []string) {
	result, issues := []quotaRow{}, []string{}
	seen := map[string]int{}
	for _, row := range rows {
		id := quotaString(row["id"])
		if id == "" {
			issues = quotaAddIssue(issues, label+"_invalid_id")
			continue
		}
		if index, exists := seen[id]; exists {
			if !reflect.DeepEqual(result[index], row) {
				issues = quotaAddIssue(issues, label+"_duplicate_conflict")
			}
			continue
		}
		seen[id] = len(result)
		result = append(result, row)
	}
	return result, issues
}

func quotaAddIssue(issues []string, issue string) []string {
	if !slices.Contains(issues, issue) {
		issues = append(issues, issue)
	}
	return issues
}

func quotaKnownNetwork(value any) bool {
	return slices.Contains([]string{"unicom", "telecom", "mobile", "broadcast"}, quotaString(value))
}

func quotaTruth(value any) bool {
	switch typed := value.(type) {
	case nil:
		return false
	case bool:
		return typed
	case string:
		return typed != ""
	case []any:
		return len(typed) > 0
	case map[string]any:
		return len(typed) > 0
	default:
		number, ok := quotaNumber(value)
		return !ok || number != 0
	}
}

func quotaFrozenNetwork(row quotaRow) (string, string, bool) {
	policy, ok := row["network_policy"].(map[string]any)
	if !ok {
		return "", "", false
	}
	network := quotaString(policy["network"])
	channel := ""
	if raw, exists := policy["channel_id"]; exists {
		channel, ok = raw.(string)
	}
	if (network != "unknown" && !quotaKnownNetwork(network)) || !ok || !quotaText(policy["source"]) ||
		quotaString(policy["operator_id"]) != quotaString(row["operator_id"]) ||
		quotaTruth(policy["recorded_on"]) && quotaDate(policy["recorded_on"]) == "" {
		return "", "", false
	}
	if network == "unknown" && channel == "" && !quotaTruth(policy["effective_from"]) {
		return network, channel, true
	}
	event := quotaDate(row["activated_on"])
	if event == "" {
		event = quotaDate(row["requested_on"])
	}
	effective := quotaDate(policy["effective_from"])
	return network, channel, effective != "" && event != "" && effective <= event
}

type quotaUnit struct {
	id, operator, network, channel, activated, released string
	issued, current, pending                            bool
	issues                                              []string
}

func quotaUnitInfo(row, operator quotaRow, today string) quotaUnit {
	unit := quotaUnit{id: quotaString(row["id"]), operator: quotaString(row["operator_id"]), activated: quotaDate(row["activated_on"])}
	phase, released := quotaString(row["phase"]), quotaDate(row["release_confirmed_on"])
	if !slices.Contains([]string{"pending", "active", "released", "failed", "cancelled"}, phase) {
		unit.issues = append(unit.issues, "unit_phase_unknown")
	}
	if quotaTruth(row["activated_on"]) && unit.activated == "" {
		unit.issues = append(unit.issues, "activation_date_invalid")
	}
	if unit.activated > today {
		unit.issues = append(unit.issues, "activation_in_future")
	}
	network, configured := quotaString(row["network"]), quotaString(operator["network"])
	if row["network"] != nil && row["network"] != "" && row["network"] != "unknown" && !quotaKnownNetwork(network) {
		unit.issues = append(unit.issues, "unit_network_invalid")
	}
	unit.channel = quotaString(row["channel_id"])
	if _, frozen := row["network_policy"]; frozen {
		var valid bool
		unit.network, unit.channel, valid = quotaFrozenNetwork(row)
		if !valid {
			unit.network, unit.channel = "invalid", ""
			unit.issues = append(unit.issues, "network_policy_invalid")
		}
	} else {
		switch {
		case quotaKnownNetwork(network) && quotaKnownNetwork(configured) && network != configured:
			unit.network = "conflict"
			unit.issues = append(unit.issues, "unit_network_conflict")
		case quotaKnownNetwork(network):
			unit.network = network
		case quotaKnownNetwork(configured):
			unit.network = configured
		default:
			unit.network = "unknown"
		}
	}
	unit.issued = phase == "active" || phase == "released" || unit.activated != "" && unit.activated <= today
	unit.pending = phase == "pending" && unit.activated == ""
	if phase == "released" || quotaTruth(row["release_confirmed_on"]) {
		policy := operator
		frozen, ok := row["release_policy"].(map[string]any)
		if ok {
			policy = frozen
		}
		effective := quotaDate(policy["effective_from"])
		valid := quotaText(policy["source"]) && effective != "" && released != "" && effective <= released
		if row["release_policy"] != nil && (!ok || frozen["operator_id"] != nil && quotaString(frozen["operator_id"]) != unit.operator) {
			valid = false
		}
		switch {
		case released == "" || released > today || !quotaText(row["release_source"]):
			unit.issues = append(unit.issues, "release_unconfirmed")
		case !valid:
			unit.issues = append(unit.issues, "release_policy_unconfirmed")
		case quotaString(policy["release_mode"]) == "confirmed":
			unit.released = released
		case quotaString(policy["release_mode"]) == "after_days":
			delay, ok := quotaNumber(policy["release_delay_days"])
			allowed := quotaDatePlus(quotaDate(row["cancelled_on"]), delay)
			switch {
			case !ok || allowed == "":
				unit.issues = append(unit.issues, "release_policy_incomplete")
			case released < allowed:
				unit.issues = append(unit.issues, "release_before_allowed_date")
			default:
				unit.released = released
			}
		default:
			unit.issues = append(unit.issues, "release_policy_unknown")
		}
		if unit.activated != "" && released != "" && released < unit.activated {
			unit.released = ""
			unit.issues = append(unit.issues, "release_before_activation")
		}
	}
	if unit.activated != "" && slices.Contains([]string{"failed", "cancelled", "pending"}, phase) {
		unit.issues = append(unit.issues, "unit_phase_conflict")
	}
	unit.current = unit.issued && unit.released == ""
	return unit
}

func quotaScope(unit quotaUnit, scope, id string) (match, known bool) {
	switch scope {
	case "all":
		return true, true
	case "operator":
		return unit.operator == id, true
	case "network":
		return unit.network == id, quotaKnownNetwork(unit.network)
	case "channel":
		return unit.channel == id, quotaText(unit.channel)
	default:
		return false, false
	}
}

func quotaIDs(value any) map[string]bool {
	ids := map[string]bool{}
	switch list := value.(type) {
	case []any:
		for _, item := range list {
			id := quotaString(item)
			if id == "" {
				return nil
			}
			ids[id] = true
		}
	case []string:
		for _, id := range list {
			if id == "" {
				return nil
			}
			ids[id] = true
		}
	default:
		return nil
	}
	return ids
}

func quotaLatestCheck(checks []quotaRow, scope, scopeID, period, today string, operator quotaRow) (quotaRow, string, []string) {
	var latest quotaRow
	var latestDate string
	var recorded int64
	conflict := false
	for _, row := range checks {
		id, hasID := row["scope_id"]
		if !hasID {
			id = ""
		}
		if row["scope"] != scope || id != scopeID || row["period"] != period {
			continue
		}
		date := quotaDate(row["verified_on"])
		if date == "" {
			return nil, "", []string{"verification_date_invalid"}
		}
		stamp, valid := quotaNumber(row["recorded_at"])
		if !valid {
			stamp = 0
		}
		if latest == nil || date > latestDate || date == latestDate && stamp > recorded {
			latest, latestDate, recorded, conflict = row, date, stamp, false
			continue
		}
		if date == latestDate && stamp == recorded {
			for _, key := range []string{"count", "coverage", "unit_ids", "pending_unit_ids", "valid_until"} {
				if !reflect.DeepEqual(row[key], latest[key]) {
					conflict = true
				}
			}
		}
	}
	if latest == nil {
		return nil, "", []string{"verification_missing"}
	}
	if conflict {
		return nil, "", []string{"verification_conflict"}
	}
	issues := []string{}
	if latestDate > today {
		issues = append(issues, "verification_in_future")
	}
	if _, valid := quotaNumber(latest["count"]); !valid || !quotaText(latest["source"]) {
		issues = append(issues, "verification_claim_invalid")
	}
	if latest["coverage"] != "total" && latest["coverage"] != "external" {
		issues = append(issues, "verification_coverage_unknown")
	}
	until := quotaDate(latest["valid_until"])
	if quotaTruth(latest["valid_until"]) && until == "" {
		issues = append(issues, "verification_validity_invalid")
	}
	if days, valid := quotaNumber(operator["verification_valid_days"]); valid {
		if policy := quotaDatePlus(latestDate, days); policy != "" && (until == "" || policy < until) {
			until = policy
		}
	}
	if until == "" {
		issues = append(issues, "verification_validity_unknown")
	} else if until < latestDate || until < today {
		issues = append(issues, "verification_expired")
	}
	start, end := quotaWindow(period, today)
	if period != "stock" && (quotaDate(latest["period_start"]) != start || quotaDate(latest["period_end"]) != end) {
		issues = append(issues, "verification_window_mismatch")
	}
	if period != "stock" && (latestDate < start || latestDate > end) {
		issues = append(issues, "verification_outside_window")
	}
	if quotaIDs(latest["unit_ids"]) == nil || quotaIDs(latest["pending_unit_ids"]) == nil {
		issues = append(issues, "verification_snapshot_invalid")
	}
	return latest, until, issues
}

type quotaLimit struct {
	ruleID, rawScope, rawScopeID, rawPeriod, rawLimit, verificationID, coverage any
	scope, period, verificationOn, followup                                     string
	issues                                                                      []string
	used, reserved, available, other, verified                                  *int64
	complete                                                                    bool
}

func quotaValue(value *int64) any {
	if value == nil {
		return nil
	}
	return *value
}

func (limit quotaLimit) public() quotaRow {
	labels := make([]string, len(limit.issues))
	for i, code := range limit.issues {
		labels[i] = quotaIssueLabel(code)
	}
	return quotaRow{"rule_id": limit.ruleID, "scope": limit.rawScope, "scope_id": limit.rawScopeID,
		"period": limit.rawPeriod, "limit": limit.rawLimit, "used": quotaValue(limit.used), "reserved": quotaValue(limit.reserved),
		"available": quotaValue(limit.available), "verification_id": limit.verificationID, "coverage": limit.coverage,
		"issues": limit.issues, "issue_labels": labels}
}

func quotaEvaluateLimit(rule, operator quotaRow, units []quotaUnit, checks []quotaRow, today string) quotaLimit {
	scopeID, found := rule["scope_id"]
	if !found {
		scopeID = ""
	}
	result := quotaLimit{ruleID: rule["id"], rawScope: rule["scope"], rawScopeID: scopeID, rawPeriod: rule["period"], rawLimit: rule["limit"],
		scope: quotaString(rule["scope"]), period: quotaString(rule["period"]), issues: []string{}}
	add := func(code string) { result.issues = quotaAddIssue(result.issues, code) }
	effective := quotaDate(rule["effective_from"])
	if rule["confirmed"] != true || !quotaText(rule["source"]) || effective == "" {
		add("rule_unconfirmed")
	} else if effective > today {
		add("rule_not_effective")
	}
	if !slices.Contains([]string{"all", "operator", "network", "channel"}, result.scope) ||
		!slices.Contains([]string{"stock", "day", "month"}, result.period) {
		add("rule_scope_unknown")
		return result
	}
	ident, validID := scopeID.(string)
	if !validID || (result.scope == "all") != (ident == "") {
		add("rule_scope_id_invalid")
		return result
	}
	maximum, limitKnown := quotaNumber(rule["limit"])
	if !limitKnown {
		add("rule_limit_unknown")
	}
	check, until, problems := quotaLatestCheck(checks, result.scope, ident, result.period, today, operator)
	result.issues = append(result.issues, problems...)
	if check == nil {
		return result
	}
	result.verificationID = check["id"]
	result.coverage = check["coverage"]
	result.verificationOn = quotaString(check["verified_on"])
	result.followup = until
	result.complete = check["company_inventory_complete"] == true
	if value, found := check["company_inventory_complete"]; found {
		if _, valid := value.(bool); !valid {
			add("company_inventory_confirmation_invalid")
		}
	}
	covered := quotaIDs(check["unit_ids"])
	coveredPending := quotaIDs(check["pending_unit_ids"])
	if covered == nil || coveredPending == nil {
		return result
	}
	byID := map[string]quotaUnit{}
	for _, unit := range units {
		byID[unit.id] = unit
	}
	for id := range covered {
		if coveredPending[id] {
			add("snapshot_overlap")
		}
	}
	for _, set := range []map[string]bool{covered, coveredPending} {
		for id := range set {
			if _, found := byID[id]; !found {
				add("snapshot_unit_missing")
			}
		}
	}
	if result.coverage == "external" && (len(covered) != 0 || len(coveredPending) != 0) {
		add("external_snapshot_contains_company")
	}

	stock := result.period == "stock"
	start, end := quotaWindow(result.period, today)
	selected, pending := []quotaUnit{}, []quotaUnit{}
	for _, unit := range units {
		match, known := quotaScope(unit, result.scope, ident)
		inWindow := unit.activated != "" && unit.activated >= start && unit.activated <= end
		if !known {
			if (stock && unit.current) || (!stock && unit.issued && (unit.activated == "" || inWindow)) || unit.pending {
				add("unit_scope_unknown")
			}
			continue
		}
		if !match {
			continue
		}
		if unit.issued {
			if stock || inWindow {
				selected = append(selected, unit)
			}
			if !stock && unit.activated == "" {
				add("activation_date_unknown")
			}
			if stock && (unit.current || covered[unit.id] || coveredPending[unit.id]) {
				for _, problem := range unit.issues {
					add(problem)
				}
			}
		}
		if unit.pending {
			pending = append(pending, unit)
		}
	}
	for _, unit := range units {
		if covered[unit.id] || coveredPending[unit.id] {
			if match, known := quotaScope(unit, result.scope, ident); !match || !known {
				add("snapshot_unit_scope_mismatch")
			}
			if covered[unit.id] && (!unit.issued || unit.activated > result.verificationOn) {
				add("snapshot_active_date_conflict")
			}
		}
	}
	sourceCount, valid := quotaNumber(check["count"])
	if !valid {
		return result
	}
	used := sourceCount
	switch result.coverage {
	case "total":
		count := int64(len(covered) + len(coveredPending))
		if count > sourceCount {
			add("company_exceeds_verified_total")
		}
		for _, unit := range selected {
			if covered[unit.id] || coveredPending[unit.id] {
				if stock && unit.released != "" {
					if unit.released > result.verificationOn {
						used--
					} else {
						add("snapshot_contains_already_released")
					}
				}
				continue
			}
			if stock && !unit.current {
				continue
			}
			if unit.activated > result.verificationOn {
				used++
			} else {
				add("uncovered_existing_company_unit")
			}
		}
		for _, unit := range units {
			if coveredPending[unit.id] && !unit.pending && !unit.issued {
				add("covered_pending_outcome_unverified")
			}
		}
		if result.complete {
			other := sourceCount - count
			result.other = &other
		}
		result.verified = &sourceCount
	case "external":
		for _, unit := range selected {
			if !stock || unit.current {
				used++
			}
		}
		result.other = &sourceCount
		if result.complete {
			result.verified = &used
		} else {
			add("company_inventory_incomplete")
		}
	default:
		return result
	}
	if used < 0 {
		add("negative_derived_total")
	}
	if _, confirmed := rule["pending_counts"].(bool); len(pending) != 0 && !confirmed {
		add("pending_policy_unknown")
	}
	if len(coveredPending) > 0 && rule["pending_counts"] == false {
		add("pending_coverage_policy_conflict")
	}
	reserved := int64(0)
	for _, unit := range pending {
		if !covered[unit.id] && !coveredPending[unit.id] {
			reserved++
		}
	}
	evidence := []string{}
	for _, problem := range result.issues {
		if !slices.Contains([]string{"rule_unconfirmed", "rule_not_effective", "rule_limit_unknown", "pending_policy_unknown"}, problem) {
			evidence = append(evidence, problem)
		}
	}
	if len(evidence) == 0 {
		result.used, result.reserved = &used, &reserved
	}
	if len(result.issues) == 0 {
		available := max(0, maximum-used-reserved)
		result.available = &available
	} else if len(evidence) != 0 && !(result.coverage == "external" && len(evidence) == 1 && evidence[0] == "company_inventory_incomplete") {
		result.other, result.verified = nil, nil
	}
	return result
}

func quotaCountBasis(limit *quotaLimit) quotaRow {
	if limit == nil {
		limit = &quotaLimit{}
	}
	verified, other := "unknown", "unknown"
	if limit.verified != nil {
		verified = "external_plus_complete_company"
		if limit.coverage == "total" {
			verified = "total_including_company"
		}
	}
	if limit.other != nil {
		other = "derived_from_complete_company"
		if limit.coverage == "external" {
			other = "explicit_external_verification"
		}
	}
	notes := []string{}
	if !limit.complete {
		notes = append(notes, "公司实名台账尚未确认完整，公司数量仅代表已登记记录，不推断未登记公司卡为零")
	}
	return quotaRow{"company": "registered_units", "company_inventory_complete": limit.complete, "verified_total": verified,
		"other_current": other, "verification_id": limit.verificationID, "notes": notes}
}

func quotaOperatorEvidence(operator quotaRow, units []quotaUnit, checks []quotaRow, today string) quotaRow {
	id := operator["id"]
	limit := quotaEvaluateLimit(quotaRow{"id": "", "scope": "operator", "scope_id": id, "period": "stock", "limit": nil,
		"pending_counts": true, "confirmed": true, "source": "count-evidence-only", "effective_from": today}, operator, units, checks, today)
	codes, labels := []string{}, []string{}
	for _, code := range limit.issues {
		if code != "rule_limit_unknown" {
			codes = append(codes, code)
			labels = append(labels, quotaIssueLabel(code))
		}
	}
	return quotaRow{"scope": "operator", "scope_id": id, "period": "stock", "verification_id": limit.verificationID,
		"coverage": limit.coverage, "verified_on": quotaStringOrNil(limit.verificationOn), "verified_total": quotaValue(limit.verified),
		"other_current": quotaValue(limit.other), "reconciled_used": quotaValue(limit.used), "company_inventory_complete": limit.complete,
		"count_basis": quotaCountBasis(&limit), "issues": codes, "issue_labels": labels}
}
