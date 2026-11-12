package board

import (
	"context"
	"database/sql"
	"encoding/json"
	"strconv"
	"strings"
)

func loadWorkflowRefs(ctx context.Context, tx *sql.Tx, scope *scopeMap) error {
	var available bool
	if err := tx.QueryRowContext(ctx, `SELECT to_regclass('board_candidate.phone_units') IS NOT NULL
		AND to_regclass('board_candidate.phone_card_placements') IS NOT NULL
		AND to_regclass('board_candidate.phone_account_access') IS NOT NULL
		AND to_regclass('board_candidate.device_cases') IS NOT NULL
		AND to_regclass('board_candidate.device_workflow_events') IS NOT NULL`).Scan(&available); err != nil || !available {
		return err
	}
	units, err := selectRows(ctx, tx, `SELECT jsonb_build_object('id',id,'device_id',device_id,'original_device_id',original_device_id) FROM board_candidate.phone_units`)
	if err != nil {
		return err
	}
	cases, err := selectRows(ctx, tx, `SELECT jsonb_build_object('id',id,'unit_id',unit_id,'account_ids',account_ids) FROM board_candidate.device_cases`)
	if err != nil {
		return err
	}
	scope.units, scope.cases = byID(units), byID(cases)
	return nil
}

func workflowOverview(ctx context.Context, tx *sql.Tx, scope *scopeMap, accounts, devices, cards []row) (row, error) {
	if err := loadWorkflowRefs(ctx, tx, scope); err != nil {
		return nil, err
	}
	if scope.units == nil {
		return row{"enabled": false, "reason": "设备流程尚未完成受控迁移", "units": []row{}, "placements": []row{}, "access": []row{}, "cases": []row{}, "events": []row{}, "todos": []row{}}, nil
	}
	byDevice := byID(devices)
	byAccount := byID(accounts)
	byCard := byID(cards)
	units, err := selectRows(ctx, tx, `SELECT to_jsonb(t) FROM board_candidate.phone_units t ORDER BY t.device_id,t.generation`)
	if err != nil {
		return nil, err
	}
	byUnit := make(map[string]row)
	visibleUnits := make([]row, 0)
	for _, unit := range units {
		device, original := str(unit["device_id"]), str(unit["original_device_id"])
		if byDevice[device] == nil || byDevice[original] == nil {
			continue
		}
		name := str(byDevice[device]["name"])
		label := name + " · 第" + str(unit["generation"]) + "台"
		if number(unit["active"]) == 0 {
			label += "（旧机）"
		}
		if original != device {
			label += "（原" + deviceNumber(original) + "号）"
		}
		unit["device_name"], unit["label"] = name, label
		byUnit[str(unit["id"])] = unit
		visibleUnits = append(visibleUnits, unit)
	}
	placements, err := selectRows(ctx, tx, `SELECT to_jsonb(t) FROM board_candidate.phone_card_placements t ORDER BY t.card_id`)
	if err != nil {
		return nil, err
	}
	visiblePlacements := make([]row, 0)
	for _, item := range placements {
		card, unit := str(item["card_id"]), str(item["unit_id"])
		if byCard[card] == nil || (unit != "" && byUnit[unit] == nil) {
			continue
		}
		item["unit_label"] = str(byUnit[unit]["label"])
		visiblePlacements = append(visiblePlacements, item)
	}
	access, err := selectRows(ctx, tx, `SELECT to_jsonb(t) FROM board_candidate.phone_account_access t ORDER BY t.account_id`)
	if err != nil {
		return nil, err
	}
	visibleAccess := make([]row, 0)
	for _, item := range access {
		if byAccount[str(item["account_id"])] == nil {
			continue
		}
		redacted := false
		for _, field := range []string{"last_unit_id", "target_unit_id"} {
			if unit := str(item[field]); unit != "" && byUnit[unit] == nil {
				item[field] = nil
				redacted = true
			}
		}
		if redacted {
			redactRecovery(item)
		}
		item["last_unit_label"] = str(byUnit[str(item["last_unit_id"])]["label"])
		item["target_unit_label"] = str(byUnit[str(item["target_unit_id"])]["label"])
		visibleAccess = append(visibleAccess, item)
	}
	cases, err := selectRows(ctx, tx, `SELECT to_jsonb(t) FROM board_candidate.device_cases t ORDER BY t.updated_at DESC,t.id`)
	if err != nil {
		return nil, err
	}
	visibleCases := make([]row, 0)
	for _, item := range cases {
		unit := byUnit[str(item["unit_id"])]
		linked, valid := caseAccounts(item["account_ids"])
		if !valid || unit == nil || !containsAll(byAccount, linked) {
			continue
		}
		item["unit_label"], item["account_ids"] = unit["label"], linked
		visibleCases = append(visibleCases, item)
	}
	byCase := byID(visibleCases)
	events, err := workflowEvents(ctx, tx, byAccount, byDevice, byUnit, byCard, byCase)
	if err != nil {
		return nil, err
	}
	for _, item := range visibleAccess {
		if id := str(item["case_id"]); id != "" && byCase[id] == nil {
			redactRecovery(item)
		}
	}
	attachWorkflow(accounts, devices, cards, visibleUnits, visiblePlacements, visibleAccess)
	return row{"enabled": true, "units": visibleUnits, "placements": visiblePlacements, "access": visibleAccess, "cases": visibleCases, "events": events,
		"todos": workflowTodos(visibleCases, visibleAccess), "options": row{
			"unit_statuses":   []string{"待确认", "在用", "待清理", "可用闲置", "故障", "送修", "返修待验", "回收"},
			"case_statuses":   []string{"待处理", "处理中", "暂挂", "已结案"},
			"recovery_states": []string{"待处理", "等待协助", "恢复成功", "暂不恢复", "无法恢复"},
			"login_states":    []string{"待确认", "登录受阻", "可操作"}, "slots": []string{"1", "2"},
		}}, nil
}

func workflowEvents(ctx context.Context, tx *sql.Tx, accounts, devices, units, cards, cases map[string]row) ([]row, error) {
	rows, err := tx.QueryContext(ctx, `SELECT to_jsonb(t) FROM board_candidate.device_workflow_events t ORDER BY t.at DESC,t.id DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := make([]row, 0)
	for rows.Next() && len(result) < 200 {
		var raw []byte
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		var item row
		decoder := json.NewDecoder(strings.NewReader(string(raw)))
		decoder.UseNumber()
		if err := decoder.Decode(&item); err != nil {
			return nil, err
		}
		if !eventRefVisible(item["unit_id"], units) || !eventRefVisible(item["card_id"], cards) ||
			!eventRefVisible(item["account_id"], accounts) || !eventRefVisible(item["case_id"], cases) {
			continue
		}
		var details row
		decoder = json.NewDecoder(strings.NewReader(str(item["detail"])))
		decoder.UseNumber()
		if err := decoder.Decode(&details); err != nil || details == nil {
			continue
		}
		linked, valid := eventAccounts(details)
		if !valid || !containsAll(accounts, linked) || !eventDetailsVisible(details, devices, units) {
			continue
		}
		item["details"] = details
		result = append(result, item)
	}
	return result, rows.Err()
}

func eventRefVisible(value any, values map[string]row) bool {
	id := str(value)
	return id == "" || values[id] != nil
}

func eventDetailsVisible(details row, devices, units map[string]row) bool {
	for _, field := range []string{"old_unit_id", "new_unit_id", "replaces_unit_id", "from_unit_id", "target_unit_id"} {
		if !eventRefVisible(details[field], units) {
			return false
		}
	}
	for _, field := range []string{"before", "after"} {
		if values, ok := details[field].(row); ok && !eventRefVisible(values["unit_id"], units) {
			return false
		}
	}
	for _, field := range []string{"old_device_id", "device_id", "original_device_id"} {
		if !eventRefVisible(details[field], devices) {
			return false
		}
	}
	return true
}

func containsAll(lookup map[string]row, ids []string) bool {
	for _, id := range ids {
		if lookup[id] == nil {
			return false
		}
	}
	return true
}

func redactRecovery(item row) {
	item["case_id"], item["reason"], item["owner"], item["followup_on"] = nil, "", "", ""
}

func attachWorkflow(accounts, devices, cards, units, placements, access []row) {
	byAccount := make(map[string]row)
	for _, item := range access {
		byAccount[str(item["account_id"])] = item
	}
	byCard := make(map[string]row)
	for _, item := range placements {
		byCard[str(item["card_id"])] = item
	}
	accountsByUnit := make(map[string][]row)
	for _, account := range accounts {
		item := byAccount[str(account["id"])]
		account["device_access"] = item
		if item != nil {
			account["phone_device_name"] = item["last_unit_label"]
			if id := str(item["last_unit_id"]); id != "" {
				accountsByUnit[id] = append(accountsByUnit[id], account)
			}
		}
	}
	for _, card := range cards {
		card["placement"] = byCard[str(card["id"])]
	}
	current := make(map[string]row)
	history := make(map[string][]row)
	for _, unit := range units {
		device, original := str(unit["device_id"]), str(unit["original_device_id"])
		if number(unit["active"]) != 0 && current[device] == nil {
			current[device] = unit
		}
		history[device] = append(history[device], unit)
		if original != device {
			history[original] = append(history[original], unit)
		}
	}
	for _, device := range devices {
		id := str(device["id"])
		device["current_unit"], device["unit_history"] = current[id], history[id]
		if history[id] == nil {
			device["unit_history"] = []row{}
		}
		own := accountsByUnit[str(current[id]["id"])]
		ids, inactive := make([]string, 0, len(own)), 0
		for _, account := range own {
			ids = append(ids, str(account["id"]))
			if account["status"] == "封禁" || account["status"] == "停用" {
				inactive++
			}
		}
		occupancy := "有在用或待确认账号"
		if len(own) == 0 {
			occupancy = "无当前账号"
		} else if inactive == len(own) {
			occupancy = "仅封禁或停用"
		}
		device["account_ids"], device["account_count"], device["inactive_count"], device["occupancy"] = ids, len(own), inactive, occupancy
	}
}

func workflowTodos(cases, access []row) []row {
	result := make([]row, 0)
	for _, item := range cases {
		if item["status"] == "已结案" {
			continue
		}
		todo := picked(item, "id", "unit_id", "owner", "followup_on", "reason")
		todo["kind"], todo["title"] = "device_case", str(item["unit_label"])+" · "+str(item["status"])
		result = append(result, todo)
	}
	for _, item := range access {
		if item["login_state"] != "登录受阻" || item["recovery_state"] == "恢复成功" || item["recovery_state"] == "暂不恢复" || item["recovery_state"] == "无法恢复" {
			continue
		}
		todo := picked(item, "account_id", "case_id", "owner", "followup_on", "reason")
		todo["kind"], todo["title"], todo["id"] = "account_recovery", "账号登录待恢复", item["account_id"]
		result = append(result, todo)
	}
	return result
}

func deviceNumber(id string) string {
	if cut := strings.LastIndexByte(id, ':'); cut >= 0 {
		id = id[cut+1:]
	}
	if n, err := strconv.Atoi(id); err == nil && n > 0 && len(id) <= 6 {
		return strconv.Itoa(n)
	}
	return "待核对"
}
