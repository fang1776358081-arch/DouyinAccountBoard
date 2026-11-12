package employeecards

import (
	"context"
	"database/sql"
	"maps"
	"slices"
	"strings"

	"douyinboard/internal/session"
)

const ledgerCardFields = "c.id,c.phone,c.carrier,c.status,c.keeper,c.location,c.code_state,c.return_state"

var ledgerCounts = []string{"registered", "unclosed", "closed", "candidates", "pending_cancel", "needs_review"}
var unclosedStates = []string{"正常", "欠费", "停机", "停用", "待注销"}

const cardVisible = `EXISTS(SELECT 1 FROM board_candidate.phone_card_offices m
	JOIN board_candidate.user_office_grants g ON g.office_id=m.office_id
	JOIN board_candidate.offices o ON o.id=m.office_id
	WHERE m.card_id=c.id AND g.user_id=$2 AND o.active=1)`

func allowedCardIDs(ctx context.Context, tx *sql.Tx, actor session.User) (map[int64]bool, error) {
	if actor.Global() {
		return nil, nil
	}
	rows, err := tx.QueryContext(ctx, `SELECT DISTINCT m.card_id FROM board_candidate.phone_card_offices m
		JOIN board_candidate.user_office_grants g ON g.office_id=m.office_id AND g.user_id=$1
		JOIN board_candidate.offices o ON o.id=m.office_id AND o.active=1`, actor.ID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := map[int64]bool{}
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		result[id] = true
	}
	return result, rows.Err()
}

func loadBundle(ctx context.Context, tx *sql.Tx, actor session.User) (employeeBundle, error) {
	employees, err := getRows(ctx, tx, `SELECT to_json(e)::text FROM board_candidate.employee_card_employees e
		WHERE ($1 AND ($2 OR e.office_id=ANY($3))) OR (NOT $1 AND e.auth_user_id=$4)
		ORDER BY e.employee_no,e.id`, allowedManager(actor), actor.Global(), actor.OfficeIDs, actor.ID)
	if err != nil {
		return nil, err
	}
	bundle := employeeBundle{"employees": employees}
	for _, input := range []struct {
		key, table string
	}{{"operators", "employee_card_operators"}, {"rules", "employee_card_rules"}} {
		rows, err := getRows(ctx, tx, `SELECT to_json(r)::text FROM board_candidate.`+input.table+` r ORDER BY id`)
		if err != nil {
			return nil, err
		}
		bundle[input.key], err = entityPayloads(rows, "revision")
		if err != nil {
			return nil, err
		}
	}
	ids := make([]string, 0, len(employees))
	for _, row := range employees {
		ids = append(ids, field(row, "id"))
	}
	for _, input := range []struct {
		key, table, order string
		payloadColumns    []string
	}{{"units", "employee_card_units", "id", nil},
		{"applications", "employee_card_applications", "id", []string{"id", "employee_id", "operator_id", "applied_on", "result", "quantity", "revision"}},
		{"verifications", "employee_card_verifications", "created_at,id", []string{"id", "employee_id", "revision", "created_at"}}} {
		rows, err := getRows(ctx, tx, `SELECT to_json(r)::text FROM board_candidate.`+input.table+
			` r WHERE employee_id=ANY($1) ORDER BY `+input.order, ids)
		if err != nil {
			return nil, err
		}
		if input.payloadColumns != nil {
			rows, err = entityPayloads(rows, input.payloadColumns...)
			if err != nil {
				return nil, err
			}
		}
		if input.key == "units" {
			for _, row := range rows {
				for _, key := range []string{"release_policy", "network_policy"} {
					if raw := field(row, key); raw != "" {
						var value dataRow
						if err := decodeJSON(raw, &value); err != nil {
							return nil, err
						}
						row[key] = value
					} else {
						delete(row, key)
					}
				}
			}
		}
		bundle[input.key] = rows
	}
	bundle["links"], err = getRows(ctx, tx, `SELECT (to_jsonb(l)||jsonb_build_object('employee_id',u.employee_id))::text
		FROM board_candidate.employee_card_links l JOIN board_candidate.employee_card_units u ON u.id=l.unit_id
		WHERE u.employee_id=ANY($1) ORDER BY l.id`, ids)
	if err != nil {
		return nil, err
	}
	bundle["name_cards"], bundle["name_match_metadata"], err = nameAssociations(ctx, tx, actor, employees)
	return bundle, err
}

func nameAssociations(ctx context.Context, tx *sql.Tx, actor session.User, employees []dataRow) ([]dataRow, []dataRow, error) {
	employeesByName := map[string]string{}
	for _, row := range employees {
		if name := field(row, "name"); name != "" {
			employeesByName[name] = field(row, "id")
		}
	}
	names := slices.Sorted(maps.Keys(employeesByName))
	cards, metadata := []dataRow{}, []dataRow{}
	unique, hidden, conflicts := map[string]bool{}, map[string]bool{}, map[string]bool{}
	for chunk := range slices.Chunk(names, 200) {
		matches, err := getRows(ctx, tx, `SELECT json_build_object('name',e.name,'n',COUNT(*))::text
			FROM board_candidate.employee_card_employees e WHERE e.name=ANY($1) GROUP BY e.name`, chunk)
		if err != nil {
			return nil, nil, err
		}
		selected := []string{}
		for _, row := range matches {
			name := field(row, "name")
			unique[name] = numberValue(row["n"]) == 1
			if unique[name] {
				selected = append(selected, name)
			}
		}
		if len(selected) == 0 {
			continue
		}
		rows, err := getRows(ctx, tx, `SELECT DISTINCT jsonb_build_object('name',c.owner)::text
			FROM board_candidate.phone_cards c JOIN board_candidate.employee_card_employees e ON e.name=c.owner
			WHERE c.owner=ANY($1) AND (
				EXISTS(SELECT 1 FROM board_candidate.employee_card_links l
					LEFT JOIN board_candidate.employee_card_units u ON u.id=l.unit_id
					WHERE (l.card_id=c.id OR l.phone_snapshot=c.phone) AND (u.employee_id IS NULL OR u.employee_id<>e.id))
				OR EXISTS(SELECT 1 FROM board_candidate.employee_card_units u WHERE u.current_card_id=c.id AND u.employee_id<>e.id)
			)`, selected)
		if err != nil {
			return nil, nil, err
		}
		for _, row := range rows {
			conflicts[field(row, "name")] = true
		}
		const unclaimed = `c.owner=ANY($1) AND c.owner<>'' AND NOT EXISTS(SELECT 1 FROM board_candidate.employee_card_links l
			WHERE l.card_id=c.id OR l.phone_snapshot=c.phone) AND NOT EXISTS(SELECT 1 FROM board_candidate.employee_card_units u WHERE u.current_card_id=c.id)`
		visible, args := "", []any{selected}
		if !actor.Global() {
			args = append(args, actor.ID)
			visible = " AND " + cardVisible
			rows, err := getRows(ctx, tx, `SELECT DISTINCT jsonb_build_object('name',c.owner)::text
				FROM board_candidate.phone_cards c WHERE `+unclaimed+` AND NOT `+cardVisible, args...)
			if err != nil {
				return nil, nil, err
			}
			for _, row := range rows {
				hidden[field(row, "name")] = true
			}
		}
		rows, err = getRows(ctx, tx, `SELECT (to_jsonb(visible)||jsonb_build_object('_office_ids',
			COALESCE((SELECT jsonb_agg(o.office_id ORDER BY o.office_id) FROM board_candidate.phone_card_offices o
			WHERE o.card_id=visible.id),'[]'::jsonb)))::text
			FROM (SELECT `+ledgerCardFields+`,c.owner FROM board_candidate.phone_cards c WHERE `+unclaimed+visible+
			`) AS visible ORDER BY visible.id`, args...)
		if err != nil {
			return nil, nil, err
		}
		for _, row := range rows {
			row["employee_id"] = employeesByName[field(row, "owner")]
		}
		cards = append(cards, rows...)
	}
	for _, row := range employees {
		name := field(row, "name")
		issues := []string{}
		if !unique[name] {
			issues = append(issues, "姓名无法唯一确认，未自动关联手机卡")
		} else if conflicts[name] {
			issues = append(issues, "卡主文字与既有实名关系不一致，待核对")
		}
		metadata = append(metadata, dataRow{"employee_id": row["id"], "name": name, "unique": unique[name],
			"association_complete": unique[name] && !conflicts[name], "scope_incomplete": hidden[name], "issues": issues})
	}
	return cards, metadata, nil
}

func scopeIncomplete(employee string, bundle employeeBundle, allowed map[int64]bool) bool {
	if allowed == nil {
		return false
	}
	for _, row := range related(bundle["name_match_metadata"], employee) {
		if truth(row, "scope_incomplete") {
			return true
		}
	}
	for _, row := range related(bundle["units"], employee) {
		if id, found := numeric(row["current_card_id"]); found && !allowed[id] {
			return true
		}
	}
	for _, row := range related(bundle["links"], employee) {
		// A historical card's phone snapshot has the same scope as its
		// physical card; do not expose it if the card or its scope is gone.
		if id, found := numeric(row["card_id"]); !found || !allowed[id] {
			return true
		}
	}
	return false
}

type ledgerContext struct {
	cards    map[int64]dataRow
	accounts map[string][]dataRow
	hidden   map[string]bool
}

func readLedgerContext(ctx context.Context, tx *sql.Tx, actor session.User, bundle employeeBundle, allowed map[int64]bool, employees map[string]bool) (ledgerContext, error) {
	result := ledgerContext{map[int64]dataRow{}, map[string][]dataRow{}, map[string]bool{}}
	ids := map[int64]bool{}
	for _, item := range []struct {
		rows []dataRow
		key  string
	}{{bundle["links"], "card_id"}, {bundle["units"], "current_card_id"}, {bundle["name_cards"], "id"}} {
		for _, row := range item.rows {
			id, ok := numeric(row[item.key])
			if ok && employees[field(row, "employee_id")] && (allowed == nil || allowed[id]) {
				ids[id] = true
			}
		}
	}
	if len(ids) == 0 {
		return result, nil
	}
	rows, err := getRows(ctx, tx, `SELECT to_json(visible)::text FROM (SELECT `+ledgerCardFields+
		` FROM board_candidate.phone_cards c WHERE c.id=ANY($1)) visible`, slices.Sorted(maps.Keys(ids)))
	if err != nil {
		return result, err
	}
	phones := map[string]bool{}
	for _, row := range rows {
		result.cards[numberValue(row["id"])] = row
		if phone := field(row, "phone"); phone != "" {
			phones[phone] = true
		}
	}
	if len(phones) == 0 {
		return result, nil
	}
	for chunk := range slices.Chunk(slices.Sorted(maps.Keys(phones)), 200) {
		args := []any{chunk}
		predicate := `a.registration_phone=ANY($1)`
		if !actor.Global() {
			const visibleAccount = `EXISTS(SELECT 1 FROM board_candidate.account_offices m
				JOIN board_candidate.user_office_grants g ON g.office_id=m.office_id AND g.user_id=$2
				JOIN board_candidate.offices o ON o.id=m.office_id AND o.active=1 WHERE m.account_id=a.id)`
			args = append(args, actor.ID)
			rows, err := getRows(ctx, tx, `SELECT DISTINCT jsonb_build_object('phone',a.registration_phone)::text
				FROM board_candidate.accounts a WHERE `+predicate+` AND NOT `+visibleAccount, args...)
			if err != nil {
				return result, err
			}
			for _, row := range rows {
				result.hidden[field(row, "phone")] = true
			}
			predicate += " AND " + visibleAccount
		}
		rows, err := getRows(ctx, tx, `SELECT to_json(visible)::text FROM (SELECT id,registration_phone,status,account_type,card_stability,password_state
			FROM board_candidate.accounts a WHERE `+predicate+`) visible`, args...)
		if err != nil {
			return result, err
		}
		for _, row := range rows {
			phone := field(row, "registration_phone")
			result.accounts[phone] = append(result.accounts[phone], row)
		}
	}
	return result, nil
}

func cancellationCandidate(card dataRow, accounts []dataRow, complete bool) (bool, []string) {
	if !complete {
		return false, []string{"关联账号的授权范围不完整，需有完整权限的人员核对"}
	}
	reasons := []string{}
	if len(accounts) == 0 {
		reasons = append(reasons, "未关联同注册手机号的账号，无法核对条件")
	} else {
		active, main, small := 0, 0, 0
		unknownStatus, unknownType, unstable, passwordMissing := false, false, false, false
		for _, row := range accounts {
			kind, status := field(row, "account_type"), field(row, "status")
			if kind == "大号" {
				main++
			}
			if kind == "小号" {
				small++
			}
			unknownStatus = unknownStatus || status == ""
			if status != "封禁" {
				active++
				unknownType = unknownType || (kind != "大号" && kind != "小号")
				unstable = unstable || field(row, "card_stability") != "已稳定"
				passwordMissing = passwordMissing || field(row, "password_state") != "已修改"
			}
		}
		for _, check := range []struct {
			invalid bool
			text    string
		}{
			{active == 0, "没有未封禁的关联账号"}, {small == 0, "未登记关联小号"}, {main > 1, "关联多个大号，需核对"},
			{unknownStatus, "关联账号状态资料未核对"}, {unknownType, "未封禁账号的类型尚未明确"},
			{unstable, "未封禁账号尚未全部登记为已稳定"}, {passwordMissing, "未封禁账号尚未全部登记为已改密"},
		} {
			if check.invalid {
				reasons = append(reasons, check.text)
			}
		}
	}
	switch field(card, "code_state") {
	case "待接码":
		reasons = append(reasons, "仍有待接码事项，先处理并人工核对")
	case "暂无法接码":
		reasons = append(reasons, "当前暂无法接码，处理事项尚未完成")
	case "无待办", "已完成":
	default:
		reasons = append(reasons, "接码事项状态未核对，不能当作已完成")
	}
	switch field(card, "return_state") {
	case "待归还":
		reasons = append(reasons, "仍有待归还事项，先处理并人工核对")
	case "不需归还", "已归还":
	default:
		reasons = append(reasons, "归还事项状态未核对，不能当作已完成")
	}
	if len(reasons) > 0 {
		return false, reasons
	}
	return true, []string{"符合现有稳定、改密等业务核对条件；仍需人工确认注销"}
}

func unknownLedger(label string, scope, association bool, issues []string) dataRow {
	result := dataRow{"scope_complete": scope, "association_complete": association, "scope_label": label,
		"name_matched_count": nil, "association_issues": issues}
	for _, key := range ledgerCounts {
		result[key] = nil
	}
	return result
}

func deriveLedger(employee string, bundle employeeBundle, ctx ledgerContext, operatorID string, complete bool) (dataRow, []dataRow) {
	if !complete {
		return unknownLedger("当前办公室权限范围不完整，卡数量及明细已隐藏", false, false, []string{"部分手机卡超出当前授权范围"}), []dataRow{}
	}
	operators, units := map[string]dataRow{}, map[string]dataRow{}
	for _, row := range bundle["operators"] {
		operators[field(row, "id")] = row
	}
	for _, row := range related(bundle["units"], employee) {
		units[field(row, "id")] = row
	}
	operatorName := "全部登记运营商"
	if operatorID != "" {
		operatorName = field(operators[operatorID], "name")
		if operatorName == "" {
			operatorName = "所选登记运营商"
		}
	}
	label := operatorName + " · 已明确关联的公司卡（含历史补换卡），不等于名下核验总数；待核对可与未销重叠"
	groups, order := map[any][]dataRow{}, []any{}
	for _, link := range related(bundle["links"], employee) {
		unit := units[field(link, "unit_id")]
		if operatorID != "" && field(unit, "operator_id") != operatorID {
			continue
		}
		key := link["card_id"]
		if key == nil {
			key = field(link, "id")
		}
		if _, present := groups[key]; !present {
			order = append(order, key)
		}
		groups[key] = append(groups[key], link)
	}
	result := []dataRow{}
	seenCards, seenPhones := map[int64]bool{}, map[string]bool{}
	for _, key := range order {
		links := groups[key]
		slices.SortFunc(links, func(a, b dataRow) int {
			if compare := strings.Compare(field(a, "linked_on"), field(b, "linked_on")); compare != 0 {
				return compare
			}
			return strings.Compare(field(a, "id"), field(b, "id"))
		})
		link := links[0]
		unit := units[field(link, "unit_id")]
		op := operators[field(unit, "operator_id")]
		card := ctx.cards[numberValue(link["card_id"])]
		reasons := []string{}
		for _, other := range links[1:] {
			if other["unit_id"] != link["unit_id"] || other["phone_snapshot"] != link["phone_snapshot"] {
				reasons = append(reasons, "同一卡存在不一致的历史实名关联，需人工核对")
				break
			}
		}
		if unit == nil || op == nil {
			reasons = append(reasons, "实名关联的登记运营商资料待核对")
		}
		result = append(result, physicalLedgerRow(ctx, card, link["card_id"], field(link, "phone_snapshot"),
			unit, op, field(link, "linked_on"), false, reasons))
		if id, found := numeric(link["card_id"]); found {
			seenCards[id] = true
		}
		seenPhones[field(link, "phone_snapshot")] = true
	}
	if operatorID == "" {
		for _, auto := range related(bundle["name_cards"], employee) {
			id, phone := numberValue(auto["id"]), field(auto, "phone")
			if seenCards[id] || (phone != "" && seenPhones[phone]) {
				continue
			}
			result = append(result, physicalLedgerRow(ctx, ctx.cards[id], auto["id"], phone, nil, nil, "", true, []string{}))
			seenCards[id], seenPhones[phone] = true, true
		}
	}
	associated, issues := true, []string{}
	for _, row := range related(bundle["name_match_metadata"], employee) {
		associated = associated && truth(row, "association_complete")
		values, _ := employeeStrings(row["issues"])
		issues = append(issues, values...)
	}
	if !associated {
		return unknownLedger("卡主姓名对应实名关系尚未完整核对，完整卡数量待核对；已确认人工关联仍可查看", true, false, issues), result
	}
	counts := dataRow{"scope_complete": true, "scope_label": label, "association_complete": true,
		"association_issues": issues, "name_matched_count": 0, "registered": len(result), "closed": 0, "unclosed": 0, "candidates": 0,
		"pending_cancel": 0, "needs_review": 0}
	for _, row := range result {
		if state := field(row, "ledger_state"); state != "review" {
			counts[state] = counts[state].(int) + 1
		}
		for property, key := range map[string]string{"candidate": "candidates", "pending_cancel": "pending_cancel", "needs_review": "needs_review"} {
			if truth(row, property) {
				counts[key] = counts[key].(int) + 1
			}
		}
		if row["association_source"] == "name" {
			counts["name_matched_count"] = counts["name_matched_count"].(int) + 1
		}
	}
	return counts, result
}

func physicalLedgerRow(ctx ledgerContext, card dataRow, cardID any, phone string, unit, op dataRow, linkedOn string, auto bool, reasons []string) dataRow {
	status, state := field(card, "status"), "review"
	actual := field(card, "phone")
	candidate := false
	switch {
	case auto && (actual == "" || actual != phone):
		reasons = append(reasons, "当前手机卡号码缺失或已变化，需核对")
	case card == nil:
		reasons = append(reasons, "原手机卡记录已缺失，不能推断已销或未销")
	case actual == "" || phone == "" || actual != phone:
		reasons = append(reasons, "当前号码与实名关联原记录不一致或未登记，需核对")
	case status != "已注销" && !slices.Contains(unclosedStates, status):
		reasons = append(reasons, "手机卡状态未核对或为历史文字，不能推断已销或未销")
	case len(reasons) == 0:
		if status == "已注销" {
			state = "closed"
			reasons = append(reasons, "手机卡已明确登记为已注销，不再列注销候选")
		} else {
			state = "unclosed"
			candidate, reasons = cancellationCandidate(card, ctx.accounts[actual], !ctx.hidden[actual])
		}
	}
	operatorName, source, sourceLabel := field(op, "name"), "explicit", "人工实名关联"
	if auto {
		operatorName, source, sourceLabel = "待确认", "name", "卡主姓名自动关联"
	} else if operatorName == "" {
		operatorName = "待核对"
	}
	return dataRow{"card_id": cardID, "phone_snapshot": phone, "phone": actual, "carrier": field(card, "carrier"), "operator_id": field(unit, "operator_id"),
		"operator_name": operatorName, "card_status": status, "ledger_state": state, "candidate": candidate, "candidate_reasons": reasons,
		"pending_cancel": state == "unclosed" && status == "待注销", "needs_review": state == "review" || (state == "unclosed" && !candidate),
		"linked_on": linkedOn, "activated_on": field(unit, "activated_on"), "keeper": field(card, "keeper"), "location": field(card, "location"),
		"association_source": source, "association_label": sourceLabel}
}

func ledgerMatches(ledger dataRow, state string) bool {
	key := map[string]string{"unclosed": "unclosed", "closed": "closed", "candidate": "candidates", "pending_cancel": "pending_cancel", "review": "needs_review"}[state]
	if key == "" || (state == "review" && (!truth(ledger, "scope_complete") || !truth(ledger, "association_complete"))) {
		return true
	}
	return numberValue(ledger[key]) > 0
}

func aggregateLedger(ledgers []dataRow, label string) dataRow {
	scoped, associated := true, true
	issues, seen := []string{}, map[string]bool{}
	for _, ledger := range ledgers {
		scoped = scoped && truth(ledger, "scope_complete")
		associated = associated && truth(ledger, "association_complete")
		values, _ := employeeStrings(ledger["association_issues"])
		for _, issue := range values {
			if !seen[issue] {
				issues = append(issues, issue)
				seen[issue] = true
			}
		}
	}
	result := unknownLedger(label, scoped, associated, issues)
	if scoped && associated {
		for _, key := range append(slices.Clone(ledgerCounts), "name_matched_count") {
			var sum int64
			for _, ledger := range ledgers {
				sum += numberValue(ledger[key])
			}
			result[key] = sum
		}
	}
	return result
}
