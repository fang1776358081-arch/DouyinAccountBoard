package employeecards

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"maps"
	"net/http"
	"net/url"
	"slices"
	"strconv"
	"strings"
	"time"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/session"
)

func (s Service) Get(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 20*time.Second)
	defer cancel()
	tx, err := s.Write.DB.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
	if err != nil {
		reply(w, nil, err)
		return
	}
	defer tx.Rollback()
	actor, err := session.AuthenticateWith(ctx, tx, r)
	if err != nil {
		reply(w, nil, err)
		return
	}
	result, err := s.get(ctx, tx, actor, r.URL.Query(), employeeToday())
	reply(w, result, err)
}

func reply(w http.ResponseWriter, result dataRow, err error) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	status := http.StatusOK
	if err != nil {
		var api accountwrite.APIError
		switch {
		case errors.Is(err, session.ErrUnauthorized):
			status, result = http.StatusUnauthorized, dataRow{"error": session.ErrUnauthorized.Error()}
		case errors.As(err, &api):
			status, result = api.Status, dataRow{"error": api.Message}
			if api.Code != "" {
				result["code"] = api.Code
			}
		default:
			status, result = http.StatusServiceUnavailable, dataRow{"error": "员工办卡资料暂时无法读取"}
		}
	}
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(result)
}

func readQuery(values url.Values, key, fallback string) (string, error) {
	found := []string{}
	for _, value := range values[key] {
		if value != "" {
			found = append(found, value)
		}
	}
	switch len(found) {
	case 0:
		return fallback, nil
	case 1:
		return found[0], nil
	default:
		return "", accountwrite.Invalid("查询条件不正确")
	}
}

func (s Service) get(ctx context.Context, tx *sql.Tx, actor session.User, query url.Values, today string) (dataRow, error) {
	enabled, err := available(ctx, tx)
	if err != nil {
		return nil, err
	}
	result := dataRow{"available": enabled, "today": today, "user": dataRow{"id": actor.ID, "name": actor.Name, "role": actor.Role, "permissions": actor.Permissions, "csrf": actor.CSRF},
		"can_manage": allowedManager(actor), "can_configure_rules": actor.Role == "owner", "can_view_birth_date": allowedManager(actor),
		"operators": []dataRow{}, "employees": []dataRow{}, "summaries": []dataRow{}, "total": 0, "page": 1, "page_size": 50,
		"offices": []dataRow{}, "staff_candidates": []dataRow{}, "card_ledger_filter_incomplete": false,
		"card_ledger_totals": unknownLedger("员工卡台账尚未读取", false, false, []string{})}
	if !enabled {
		result["message"] = "员工办卡结构升级尚未完成，原手机卡功能可继续使用"
		return result, nil
	}
	requestID, err := readQuery(query, "request_id", "")
	if err != nil {
		return nil, err
	}
	if requestID != "" {
		return s.requestResult(ctx, tx, actor, requestID)
	}
	bundle, err := loadBundle(ctx, tx, actor)
	if err != nil {
		return nil, err
	}
	allowed, err := allowedCardIDs(ctx, tx, actor)
	if err != nil {
		return nil, err
	}
	result["operators"], result["rules"] = bundle["operators"], bundle["rules"]
	result["version"], err = bundle.version(actor, "", s.Write.RestoreGeneration)
	if err != nil {
		return nil, err
	}
	result["offices"], err = getRows(ctx, tx, `SELECT to_json(o)::text FROM
		(SELECT id,name,active FROM board_candidate.offices WHERE $1 OR id=ANY($2) ORDER BY name,id) o`, actor.Global(), actor.OfficeIDs)
	if err != nil {
		return nil, err
	}
	if actor.Role == "owner" {
		result["staff_candidates"], err = getRows(ctx, tx, `SELECT to_json(u)::text FROM
			(SELECT id,name FROM board_candidate.auth_users WHERE active=1 ORDER BY id) u`)
		if err != nil {
			return nil, err
		}
	}
	values := map[string]string{}
	for _, key := range []string{"employee_id", "operator_id", "card_state"} {
		values[key], err = readQuery(query, key, "")
		if err != nil {
			return nil, err
		}
	}
	if !slices.Contains([]string{"", "unclosed", "closed", "candidate", "pending_cancel", "review"}, values["card_state"]) {
		return nil, accountwrite.Invalid("手机卡台账状态筛选不正确")
	}
	if ident := values["employee_id"]; ident != "" {
		cardQuery, err := readQuery(query, "card_query", "")
		if err != nil {
			return nil, err
		}
		return s.detail(ctx, tx, actor, bundle, allowed, ident, values["operator_id"], cardQuery, today, result)
	}
	for _, key := range []string{"query", "status", "office_id", "employment", "followup", "view", "page", "page_size"} {
		fallback := map[string]string{"page": "1", "page_size": "50"}[key]
		values[key], err = readQuery(query, key, fallback)
		if err != nil {
			return nil, err
		}
	}
	return s.list(ctx, tx, actor, bundle, allowed, values, today, result)
}

func employeePublic(row dataRow, birth bool) dataRow {
	result := maps.Clone(row)
	if !birth {
		for _, key := range []string{"birth_date", "consent_source", "consent_on"} {
			delete(result, key)
		}
	}
	return result
}

func (s Service) detail(ctx context.Context, tx *sql.Tx, actor session.User, bundle employeeBundle, allowed map[int64]bool,
	id, operator, cardQuery, today string, result dataRow) (dataRow, error) {
	matches := related(bundle["employees"], id)
	if len(matches) != 1 {
		return nil, accountwrite.Forbidden("无权访问此员工资料")
	}
	if scopeIncomplete(id, bundle, allowed) {
		return nil, accountwrite.Forbidden("该员工有关联卡超出当前办公室范围，请全局管理人员核对；未返回卡明细或核验来源")
	}
	employee := matches[0]
	context, err := readLedgerContext(ctx, tx, actor, bundle, allowed, map[string]bool{id: true})
	if err != nil {
		return nil, err
	}
	ledger, linked := deriveLedger(id, bundle, context, operator, true)
	version, err := bundle.version(actor, id, s.Write.RestoreGeneration)
	if err != nil {
		return nil, err
	}
	summary, err := employeeSummary(employee, bundle, today, false)
	if err != nil {
		return nil, err
	}
	units := []dataRow{}
	for _, row := range related(bundle["units"], id) {
		unit := maps.Clone(row)
		unit["card_phone"] = ""
		if card := context.cards[numberValue(unit["current_card_id"])]; card != nil {
			unit["card_phone"], unit["card_status"] = card["phone"], card["status"]
		}
		units = append(units, unit)
	}
	cardQuery, err = employeeText(cardQuery, "卡查询", 40, false)
	if err != nil {
		return nil, err
	}
	candidates := []dataRow{}
	if allowedManager(actor) {
		candidates, err = getRows(ctx, tx, `SELECT to_json(candidates)::text FROM
			(SELECT c.id,c.phone,c.carrier,c.status,c.owner,c.keeper FROM board_candidate.phone_cards c
			 WHERE ($1 OR `+cardVisible+`) AND ($3='' OR c.phone LIKE '%'||$3||'%' OR c.id::text=$3)
			 ORDER BY id DESC LIMIT 100) candidates`, actor.Global(), actor.ID, cardQuery)
		if err != nil {
			return nil, err
		}
	}
	history, err := getRows(ctx, tx, `SELECT json_build_object('id',e.id,'at',e.at,'action',e.action,'detail',e.detail::json)::text
		FROM board_candidate.employee_card_events e WHERE employee_id=$1 ORDER BY at DESC,id DESC LIMIT 100`, id)
	if err != nil {
		return nil, err
	}
	public := employeePublic(employee, true)
	public["version"], public["card_ledger"] = version, ledger
	maps.Copy(result, dataRow{"employee": public, "summary": summary, "units": units, "verifications": related(bundle["verifications"], id),
		"applications": related(bundle["applications"], id), "history": history, "card_candidates": candidates,
		"card_links": related(bundle["links"], id), "linked_cards": linked, "card_ledger": ledger,
		"card_ledger_totals": ledger, "version": version, "can_view_birth_date": true})
	return result, nil
}

func resultRows(value any) []dataRow {
	if result, ok := value.([]dataRow); ok {
		return result
	}
	items, _ := value.([]any)
	result := []dataRow{}
	for _, item := range items {
		if row, ok := item.(dataRow); ok {
			result = append(result, row)
		}
	}
	return result
}

func resultRow(value any) dataRow {
	row, _ := value.(dataRow)
	return row
}

func (s Service) list(ctx context.Context, tx *sql.Tx, actor session.User, bundle employeeBundle, allowed map[int64]bool,
	filters map[string]string, today string, result dataRow) (dataRow, error) {
	complete := map[string]bool{}
	for _, employee := range bundle["employees"] {
		id := field(employee, "id")
		complete[id] = !scopeIncomplete(id, bundle, allowed)
	}
	ledgerCtx, err := readLedgerContext(ctx, tx, actor, bundle, allowed, complete)
	if err != nil {
		return nil, err
	}
	rows, ledgers := []dataRow{}, []dataRow{}
	scopeComplete, associationComplete := true, true
	for _, employee := range bundle["employees"] {
		id := field(employee, "id")
		if (filters["office_id"] != "" && employee["office_id"] != filters["office_id"]) ||
			(filters["employment"] != "" && employee["employment_status"] != filters["employment"]) {
			continue
		}
		summary, err := employeeSummary(employee, bundle, today, !complete[id])
		if err != nil {
			return nil, err
		}
		ledger, links := deriveLedger(id, bundle, ledgerCtx, filters["operator_id"], complete[id])
		if !employeeFilters(employee, summary, links, filters, today) {
			continue
		}
		scopeComplete = scopeComplete && truth(ledger, "scope_complete")
		associationComplete = associationComplete && truth(ledger, "association_complete")
		if (filters["view"] == "cards" && filters["status"] == "" && filters["operator_id"] != "" && numberValue(ledger["registered"]) == 0) ||
			!ledgerMatches(ledger, filters["card_state"]) {
			continue
		}
		version, err := bundle.version(actor, id, s.Write.RestoreGeneration)
		if err != nil {
			return nil, err
		}
		row := employeePublic(employee, false)
		row["summary"], row["card_ledger"], row["version"] = summary, ledger, version
		rows, ledgers = append(rows, row), append(ledgers, ledger)
	}
	page, err1 := strconv.Atoi(strings.TrimSpace(filters["page"]))
	size, err2 := strconv.Atoi(strings.TrimSpace(filters["page_size"]))
	if err1 != nil || err2 != nil {
		return nil, accountwrite.Invalid("页码不正确")
	}
	if page < 1 || !slices.Contains([]int{20, 50, 100}, size) {
		return nil, accountwrite.Invalid("页码或每页条数不正确")
	}
	page = min(page, max(1, (len(rows)+size-1)/size))
	summaries := employeeSummaries(rows, bundle["operators"])
	label := "当前登记运营商范围内的已关联卡（含历史补换卡）"
	if len(ledgers) != 0 {
		label = field(ledgers[0], "scope_label")
	}
	incomplete := !scopeComplete || !associationComplete
	total := aggregateLedger(ledgers, "当前员工筛选结果 · "+label)
	if incomplete {
		total = unknownLedger("部分关联卡范围或姓名归属不完整，当前卡筛选匹配及合计待核对", scopeComplete, associationComplete,
			[]string{"卡片授权范围或姓名归属尚未完整核对"})
	}
	maps.Copy(result, dataRow{"employees": rows[(page-1)*size : min(page*size, len(rows))], "summaries": summaries,
		"total": len(rows), "page": page, "page_size": size, "card_ledger_totals": total, "card_ledger_filter_incomplete": incomplete})
	return result, nil
}

func employeeFilters(employee, summary dataRow, links []dataRow, filters map[string]string, today string) bool {
	search := strings.ToLower(strings.TrimSpace(filters["query"]))
	if search != "" && !strings.Contains(strings.ToLower(field(employee, "name")+" "+field(employee, "employee_no")), search) &&
		!slices.ContainsFunc(links, func(card dataRow) bool {
			return strings.Contains(strings.ToLower(field(card, "phone_snapshot")+" "+field(card, "phone")), search)
		}) {
		return false
	}
	ops := resultRows(summary["operators"])
	if !slices.ContainsFunc(ops, func(op dataRow) bool {
		return (filters["operator_id"] == "" || op["operator_id"] == filters["operator_id"]) &&
			(filters["status"] == "" || op["status"] == filters["status"])
	}) {
		return false
	}
	switch filters["followup"] {
	case "due":
		return slices.ContainsFunc(ops, func(op dataRow) bool {
			date := field(op, "next_followup_on")
			return date != "" && date <= today
		})
	case "scheduled":
		return field(summary, "next_followup_on") != ""
	case "unscheduled":
		return field(summary, "next_followup_on") == ""
	}
	return true
}

func employeeSummaries(employees, operators []dataRow) []dataRow {
	result := []dataRow{}
	for _, operator := range operators {
		item := dataRow{"operator_id": operator["id"], "name": operator["name"], "employees": 0, "eligible": 0,
			"ineligible": 0, "needs_verification": 0, "company_current": int64(0), "company_lifetime": int64(0), "pending_count": int64(0)}
		for _, employee := range employees {
			for _, op := range resultRows(resultRow(employee["summary"])["operators"]) {
				if op["operator_id"] != operator["id"] {
					continue
				}
				item["employees"] = item["employees"].(int) + 1
				status := field(op, "status")
				if item[status] != nil {
					item[status] = item[status].(int) + 1
				}
				for key, target := range map[string]string{"company_current": "company_current", "company_lifetime": "company_lifetime", "pending": "pending_count"} {
					value := resultRow(op["counts"])[key]
					if value == nil {
						item[target] = nil
					} else if item[target] != nil {
						item[target] = numberValue(item[target]) + numberValue(value)
					}
				}
			}
		}
		result = append(result, item)
	}
	return result
}

func employeeSummary(employee dataRow, bundle employeeBundle, today string, incomplete bool) (dataRow, error) {
	id := field(employee, "id")
	operators := map[string]dataRow{}
	for _, op := range bundle["operators"] {
		operators[field(op, "id")] = op
	}
	units := []dataRow{}
	for _, row := range related(bundle["units"], id) {
		unit := maps.Clone(row)
		op := operators[field(unit, "operator_id")]
		policy := resultRow(unit["network_policy"])
		if policy == nil && unit["phase"] != "pending" {
			policy = unknownNetwork(field(unit, "operator_id"), today)
			unit["network_policy"] = policy
		}
		if policy == nil {
			policy = op
		}
		unit["network"], unit["channel_id"] = valueOr(policy, "network", "unknown"), valueOr(policy, "channel_id", "")
		units = append(units, unit)
	}
	profile := maps.Clone(employee)
	profile["consent_confirmed"] = field(employee, "consent_source") != "" && field(employee, "consent_on") != ""
	result, err := evaluateEmployee(profile, bundle["operators"], units, related(bundle["verifications"], id), related(bundle["applications"], id),
		bundle["rules"], today)
	if err != nil {
		return nil, err
	}
	ops := resultRows(result["operators"])
	if incomplete {
		basis := dataRow{"company": "scope_incomplete", "company_inventory_complete": nil, "verified_total": "unknown",
			"other_current": "unknown", "verification_id": nil, "notes": []string{"当前范围不完整，已隐藏关联卡统计及其核验依据"}}
		maps.Copy(result, dataRow{"scope_incomplete": true, "issues": []string{"部分关联卡超出当前办公室范围，名额判断待核验"},
			"next_followup_on": nil, "followup_action": "verify", "company_inventory_complete": nil, "count_basis": maps.Clone(basis)})
		clearSummaryCounts(result, "company_lifetime", "company_current", "other_current", "verified_total", "pending_count", "estimated_available")
		for key := range resultRow(result["counts"]) {
			resultRow(result["counts"])[key] = nil
		}
		for _, op := range ops {
			maps.Copy(op, dataRow{"status": "needs_verification", "status_label": "待核验", "estimated_available": nil, "limits": []dataRow{},
				"verification_on": nil, "next_followup_on": nil, "followup_action": "verify", "company_inventory_complete": nil, "count_basis": maps.Clone(basis),
				"reasons": []dataRow{{"code": "scope_incomplete", "text": "关联卡范围不完整，不能确定可申请数量", "kind": "unknown"}}})
			for key := range resultRow(op["counts"]) {
				resultRow(op["counts"])[key] = nil
			}
			delete(op, "count_evidence")
		}
		return result, nil
	}
	ambiguous := slices.ContainsFunc(related(bundle["name_match_metadata"], id), func(row dataRow) bool { return !truth(row, "association_complete") })
	if !ambiguous && len(related(bundle["name_cards"], id)) == 0 {
		return result, nil
	}
	code, text := "name_association_quota_unverified", "存在按卡主姓名自动关联的卡片，实际发卡商、办理日期及名额对应尚未核验；原核验记录保留，不能据当前名额台账确定余额"
	if ambiguous {
		code, text = "name_association_ambiguous", "卡主姓名无法唯一对应实名关系或与既有关系不一致，完整卡归属及名额对应待核对；原核验记录保留，不能确定余额"
	}
	reason := dataRow{"code": code, "kind": "unknown", "text": text}
	appendIssue(result, "issues", code)
	clearSummaryCounts(result, "company_lifetime", "company_current", "other_current", "pending_count", "estimated_available")
	countFields := []string{"company_lifetime", "company_current", "other_current", "pending", "estimated_available"}
	clearSummaryCounts(resultRow(result["counts"]), countFields...)
	maps.Copy(result, dataRow{"name_association_quota_unverified": true, "company_inventory_complete": false, "next_followup_on": nil, "followup_action": "verify"})
	unverifiedBasis(result, text)
	for _, op := range ops {
		maps.Copy(op, dataRow{"estimated_available": nil, "company_inventory_complete": false, "next_followup_on": nil, "followup_action": "verify"})
		if op["status"] != "ineligible" {
			op["status"], op["status_label"] = "needs_verification", "待核验"
		}
		op["reasons"] = append(resultRows(op["reasons"]), reason)
		clearSummaryCounts(resultRow(op["counts"]), countFields...)
		unverifiedBasis(op, text)
		for _, limit := range resultRows(op["limits"]) {
			clearSummaryCounts(limit, "used", "reserved", "available")
			appendIssue(limit, "issues", code)
			appendIssue(limit, "issue_labels", text)
		}
		if evidence := resultRow(op["count_evidence"]); evidence != nil {
			clearSummaryCounts(evidence, "other_current", "reconciled_used")
			evidence["company_inventory_complete"], evidence["count_basis"] = false, maps.Clone(resultRow(op["count_basis"]))
			appendIssue(evidence, "issues", code)
			appendIssue(evidence, "issue_labels", text)
		}
	}
	return result, nil
}

func appendIssue(row dataRow, key, value string) {
	items, _ := employeeStrings(row[key])
	row[key] = append(items, value)
}

func clearSummaryCounts(row dataRow, keys ...string) {
	if row != nil {
		for _, key := range keys {
			row[key] = nil
		}
	}
}

func unverifiedBasis(row dataRow, reason string) {
	basis := maps.Clone(resultRow(row["count_basis"]))
	if basis == nil {
		basis = dataRow{}
	}
	maps.Copy(basis, dataRow{"company": "name_association_unverified", "company_inventory_complete": false, "other_current": "unknown"})
	appendIssue(basis, "notes", reason)
	row["count_basis"] = basis
}
