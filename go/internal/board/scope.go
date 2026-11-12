package board

import (
	"context"
	"database/sql"
	"encoding/json"
	"slices"
	"strings"

	"douyinboard/internal/session"
)

type scopeMap struct {
	actor        session.User
	ids          map[string]map[string][]string
	units, cases map[string]row
}

func loadScope(ctx context.Context, tx *sql.Tx, actor session.User) (*scopeMap, error) {
	scope := &scopeMap{actor: actor, ids: map[string]map[string][]string{
		"accounts": {}, "computers": {}, "phone_devices": {}, "phone_cards": {},
	}}
	rows, err := tx.QueryContext(ctx, `
		SELECT 'accounts' AS kind, account_id AS id, office_id FROM board_candidate.account_offices
		UNION ALL SELECT 'computers', computer_id, office_id FROM board_candidate.computer_offices
		UNION ALL SELECT 'phone_devices', device_id, office_id FROM board_candidate.phone_device_offices
		UNION ALL SELECT 'phone_cards', card_id::text, office_id FROM board_candidate.phone_card_offices
		ORDER BY 1,2,3`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var kind, id, office string
		if err := rows.Scan(&kind, &id, &office); err != nil {
			return nil, err
		}
		scope.ids[kind][id] = append(scope.ids[kind][id], office)
	}
	return scope, rows.Err()
}

type reference struct{ table, id string }

func (s *scopeMap) offices(table, id string) []string {
	if offices := s.ids[table][id]; len(offices) > 0 {
		return offices
	}
	return []string{}
}

func (s *scopeMap) canSee(table, id string) bool {
	if id == "" {
		return false
	}
	if s.actor.Global() {
		return true
	}
	for _, office := range s.offices(table, id) {
		if slices.Contains(s.actor.OfficeIDs, office) {
			return true
		}
	}
	return false
}

func (s *scopeMap) recordVisible(table string, item row) bool {
	if s.actor.Global() {
		return true
	}
	refs := s.references(table, item)
	for _, ref := range refs {
		if !s.canSee(ref.table, ref.id) {
			return false
		}
	}
	return len(refs) != 0
}

func (s *scopeMap) recordOffices(table string, item row) []string {
	offices := make([]string, 0)
	for _, ref := range s.references(table, item) {
		values := s.offices(ref.table, ref.id)
		if len(values) == 0 {
			return []string{}
		}
		offices = append(offices, values...)
	}
	slices.Sort(offices)
	return slices.Compact(offices)
}

func (s *scopeMap) references(table string, item row) []reference {
	refs := make([]reference, 0, 2)
	add := func(table string, value any) { refs = append(refs, reference{table, str(value)}) }
	addUnit := func(value any) {
		if id := str(value); id != "" {
			unit := s.units[id]
			add("phone_devices", unit["device_id"])
			add("phone_devices", unit["original_device_id"])
		}
	}
	addCase := func(value any) {
		if id := str(value); id != "" {
			item := s.cases[id]
			if item == nil {
				add("phone_devices", "")
				return
			}
			addUnit(item["unit_id"])
			accounts, valid := caseAccounts(item["account_ids"])
			if !valid {
				add("accounts", "")
				return
			}
			for _, account := range accounts {
				add("accounts", account)
			}
		}
	}
	switch table {
	case "accounts", "computers", "phone_cards", "phone_devices":
		add(table, item["id"])
	case "daily", "nurture_records":
		add("accounts", item["account_id"])
	case "publishing_layout":
		add("computers", item["computer_id"])
		if item["account_id"] != nil {
			add("accounts", item["account_id"])
		}
	case "phone_units":
		add("phone_devices", item["device_id"])
		add("phone_devices", item["original_device_id"])
	case "phone_card_placements":
		add("phone_cards", item["card_id"])
		addUnit(item["unit_id"])
	case "phone_account_access":
		add("accounts", item["account_id"])
		addUnit(item["last_unit_id"])
		addUnit(item["target_unit_id"])
		addCase(item["case_id"])
	case "device_cases":
		addUnit(item["unit_id"])
		addCase(item["id"])
	case "device_workflow_events":
		addUnit(item["unit_id"])
		addCase(item["case_id"])
		if str(item["card_id"]) != "" && number(item["card_id"]) != 0 {
			add("phone_cards", item["card_id"])
		}
		if str(item["account_id"]) != "" {
			add("accounts", item["account_id"])
		}
		var detail row
		if err := json.Unmarshal([]byte(str(item["detail"])), &detail); err != nil || detail == nil {
			return []reference{{"phone_devices", ""}}
		}
		accounts, valid := eventAccounts(detail)
		if !valid {
			return []reference{{"accounts", ""}}
		}
		for _, account := range accounts {
			add("accounts", account)
		}
		for _, key := range []string{"old_unit_id", "new_unit_id", "replaces_unit_id", "from_unit_id", "target_unit_id"} {
			addUnit(detail[key])
		}
		for _, key := range []string{"old_device_id", "device_id", "original_device_id"} {
			if str(detail[key]) != "" {
				add("phone_devices", detail[key])
			}
		}
		for _, key := range []string{"before", "after"} {
			if fields, ok := detail[key].(row); ok {
				addUnit(fields["unit_id"])
			}
		}
	}
	return refs
}

func caseAccounts(value any) ([]string, bool) {
	var parsed []string
	if err := json.Unmarshal([]byte(str(value)), &parsed); err != nil || parsed == nil || slices.Contains(parsed, "") {
		return nil, false
	}
	return parsed, true
}

func eventAccounts(detail row) ([]string, bool) {
	value, exists := detail["affected_account_ids"]
	if !exists {
		return nil, true
	}
	items, ok := value.([]any)
	if !ok {
		return nil, false
	}
	result := make([]string, 0, len(items))
	for _, item := range items {
		id, ok := item.(string)
		if !ok || id == "" {
			return nil, false
		}
		result = append(result, id)
	}
	return result, true
}

func integerOrder(expression string) string {
	return `CASE WHEN (` + expression + `) IS NULL THEN NULL ELSE GREATEST(-9223372036854775808::numeric,
		LEAST(9223372036854775807::numeric,COALESCE(substring((` + expression + `) from '^[[:space:]]*[+-]?[0-9]+')::numeric,0))) END NULLS FIRST`
}

func boardEntities(ctx context.Context, tx *sql.Tx, scope *scopeMap, day string) ([]row, []row, error) {
	computerOrder := integerOrder(`CASE WHEN c.id LIKE 'office3:%' THEN substr(c.id,9) ELSE c.id END`)
	computers, err := selectRows(ctx, tx, `SELECT to_jsonb(c) FROM board_candidate.computers c
		WHERE $1 OR EXISTS (SELECT 1 FROM board_candidate.computer_offices o WHERE o.computer_id=c.id AND o.office_id=ANY($2::text[]))
		ORDER BY CASE WHEN c.id LIKE 'office3:%' THEN 1 ELSE 0 END,`+computerOrder+`,c.id`, scope.actor.Global(), scope.actor.OfficeIDs)
	if err != nil {
		return nil, nil, err
	}
	accounts, err := selectRows(ctx, tx, `SELECT to_jsonb(a) || jsonb_build_object('published',COALESCE(d.published,0))
		FROM board_candidate.accounts a LEFT JOIN board_candidate.daily d ON d.account_id=a.id AND d.day=$3
		WHERE $1 OR EXISTS (SELECT 1 FROM board_candidate.account_offices o WHERE o.account_id=a.id AND o.office_id=ANY($2::text[]))
		ORDER BY `+integerOrder("a.computer_id")+`,a.slot`, scope.actor.Global(), scope.actor.OfficeIDs, day)
	if err != nil {
		return nil, nil, err
	}
	unassigned := false
	for _, computer := range computers {
		id := str(computer["id"])
		computer["office_ids"] = scope.offices("computers", id)
		unassigned = unassigned || id == "unassigned"
	}
	virtual := false
	for _, account := range accounts {
		account["office_ids"] = scope.offices("accounts", str(account["id"]))
		if scope.actor.Global() {
			continue
		}
		if !scope.canSee("accounts", str(account["parent_account_id"])) {
			account["parent_account_id"] = nil
		}
		if !scope.canSee("phone_devices", str(account["phone_device_id"])) {
			account["phone_device_id"] = nil
		}
		if !scope.canSee("computers", str(account["computer_id"])) {
			account["computer_id"] = "unassigned"
			virtual = true
		}
	}
	if virtual && !unassigned {
		computers = append(computers, row{"id": "unassigned", "name": "未分配电脑", "shadow_account": "", "ip": "", "operator": "", "category": "", "office_ids": []string{}})
	}
	return computers, accounts, nil
}

func byID(items []row) map[string]row {
	result := make(map[string]row, len(items))
	for _, item := range items {
		result[str(item["id"])] = item
	}
	return result
}

func joinedKey(prefix string, fields []string, item row) string {
	var key strings.Builder
	key.WriteString(prefix)
	for _, field := range fields {
		key.WriteByte(':')
		key.WriteString(str(item[field]))
	}
	return key.String()
}
