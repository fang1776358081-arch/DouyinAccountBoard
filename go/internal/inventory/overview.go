// Package inventory implements the workbench's phone and SIM inventory.
package inventory

import (
	"context"
	"database/sql"
	"encoding/json"
	"regexp"
	"slices"
	"strconv"
	"strings"

	"douyinboard/internal/session"
)

var (
	cardStatuses = []string{"待确认", "正常", "欠费", "停机", "停用", "待注销", "已注销"}
	carriers     = []string{"待确认", "中国移动", "中国联通", "中国电信", "中国广电", "民生电信", "长城移动", "其他"}
	ownerships   = []string{"待确认", "自有", "借用", "代管"}
	codeStates   = []string{"无待办", "待接码", "已完成", "暂无法接码"}
	returnStates = []string{"待确认", "不需归还", "待归还", "已归还"}
	deviceStates = []string{"待确认", "在用", "待清理", "可用闲置", "故障"}

	numberPattern     = regexp.MustCompile(`^[0-9]{1,6}$`)
	namespacedPattern = regexp.MustCompile(`^(phone:[a-f0-9]{32}:)([0-9]{1,6})$`)
)

func validPhone(value string) bool {
	if len(value) != 11 || value[0] != '1' {
		return false
	}
	for _, character := range value[1:] {
		if character < '0' || character > '9' {
			return false
		}
	}
	return true
}

func serialNumber(value string) string {
	value = strings.TrimSpace(value)
	if !numberPattern.MatchString(value) {
		return ""
	}
	number, _ := strconv.Atoi(value)
	if number == 0 {
		return ""
	}
	return strconv.Itoa(number)
}

func deviceIdentity(value string) (id, number string) {
	value = strings.TrimSpace(value)
	prefix := ""
	if parts := namespacedPattern.FindStringSubmatch(value); parts != nil {
		prefix, value = parts[1], parts[2]
	} else if strings.HasPrefix(value, "office3:") {
		prefix, value = "office3:", strings.TrimPrefix(value, "office3:")
	}
	if number = serialNumber(value); number != "" {
		return prefix + number, number
	}
	return "", ""
}

// Cards returns the legacy board's inventory shape. The accounts slice already
// contains only rows visible to actor in tx. A nil slice reads that same scope.
func Cards(ctx context.Context, tx *sql.Tx, actor session.User, accounts []map[string]any) (map[string]any, error) {
	rows, err := tx.QueryContext(ctx, `
SELECT p.id, to_json(p)::text,
       COALESCE((SELECT json_agg(m.office_id ORDER BY m.office_id)::text
                 FROM board_candidate.phone_card_offices m WHERE m.card_id=p.id), '[]')
FROM board_candidate.phone_cards p
WHERE $1 OR EXISTS (
    SELECT 1 FROM board_candidate.phone_card_offices m
    JOIN board_candidate.user_office_grants g ON g.office_id=m.office_id AND g.user_id=$2
    JOIN board_candidate.offices o ON o.id=m.office_id AND o.active=1
    WHERE m.card_id=p.id
)
ORDER BY p.id DESC`, actor.Global(), actor.ID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	cards := []map[string]any{}
	known := map[string]struct{}{}
	custom := map[string]struct{}{}
	for rows.Next() {
		var id int64
		var raw, officeJSON string
		if err := rows.Scan(&id, &raw, &officeJSON); err != nil {
			return nil, err
		}
		card, err := inventoryRow(raw, officeJSON)
		if err != nil {
			return nil, err
		}
		card["id"] = id
		known[stringField(card, "phone")] = struct{}{}
		if value := stringField(card, "carrier"); !slices.Contains(carriers, value) {
			custom[value] = struct{}{}
		}
		cards = append(cards, card)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	phones := map[string]struct{}{}
	if accounts == nil {
		phones, err = accountPhones(ctx, tx, actor)
		if err != nil {
			return nil, err
		}
	} else {
		for _, account := range accounts {
			phones[stringField(account, "registration_phone")] = struct{}{}
		}
	}
	importable := 0
	for value := range phones {
		if _, found := known[value]; !found && validPhone(value) {
			importable++
		}
	}
	additional := make([]string, 0, len(custom))
	for value := range custom {
		additional = append(additional, value)
	}
	slices.Sort(additional)
	list := append(slices.Clone(carriers[:len(carriers)-1]), additional...)
	return map[string]any{
		"cards": cards, "statuses": slices.Clone(cardStatuses), "carriers": append(list, "其他"),
		"ownerships": slices.Clone(ownerships), "code_states": slices.Clone(codeStates),
		"return_states": slices.Clone(returnStates), "importable": importable,
	}, nil
}

func accountPhones(ctx context.Context, tx *sql.Tx, actor session.User) (map[string]struct{}, error) {
	rows, err := tx.QueryContext(ctx, `
SELECT DISTINCT a.registration_phone
FROM board_candidate.accounts a
WHERE a.registration_phone<>'' AND ($1 OR EXISTS (
    SELECT 1 FROM board_candidate.account_offices m
    JOIN board_candidate.user_office_grants g ON g.office_id=m.office_id AND g.user_id=$2
    JOIN board_candidate.offices o ON o.id=m.office_id AND o.active=1
    WHERE m.account_id=a.id
))`, actor.Global(), actor.ID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	phones := map[string]struct{}{}
	for rows.Next() {
		var value string
		if err := rows.Scan(&value); err != nil {
			return nil, err
		}
		phones[value] = struct{}{}
	}
	return phones, rows.Err()
}

func stringField(row map[string]any, key string) string {
	value, _ := row[key].(string)
	return value
}

func inventoryRow(raw, officeJSON string) (map[string]any, error) {
	var row map[string]any
	decoder := json.NewDecoder(strings.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&row); err != nil {
		return nil, err
	}
	var offices []string
	if err := json.Unmarshal([]byte(officeJSON), &offices); err != nil {
		return nil, err
	}
	row["office_ids"] = offices
	return row, nil
}

// Devices returns the legacy device overview and enriches visible account
// rows with the name of their visible device.
func Devices(ctx context.Context, tx *sql.Tx, actor session.User, accounts []map[string]any) (map[string]any, error) {
	rows, err := tx.QueryContext(ctx, `
SELECT to_json(p)::text,
       COALESCE((SELECT json_agg(m.office_id ORDER BY m.office_id)::text
                 FROM board_candidate.phone_device_offices m WHERE m.device_id=p.id), '[]')
FROM board_candidate.phone_devices p
WHERE $1 OR EXISTS (
    SELECT 1 FROM board_candidate.phone_device_offices m
    JOIN board_candidate.user_office_grants g ON g.office_id=m.office_id AND g.user_id=$2
    JOIN board_candidate.offices o ON o.id=m.office_id AND o.active=1
    WHERE m.device_id=p.id
)`, actor.Global(), actor.ID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	devices := []map[string]any{}
	byID := map[string]map[string]any{}
	for rows.Next() {
		var raw, officeJSON string
		if err := rows.Scan(&raw, &officeJSON); err != nil {
			return nil, err
		}
		device, err := inventoryRow(raw, officeJSON)
		if err != nil {
			return nil, err
		}
		id := stringField(device, "id")
		_, serial := deviceIdentity(id)
		device["office_id"], device["number"] = nil, nil
		if serial != "" {
			device["number"] = serial
		}
		offices := device["office_ids"].([]string)
		device["identity_state"] = "归属或序号待核对"
		if len(offices) == 1 {
			device["office_id"] = offices[0]
			if serial != "" {
				device["identity_state"] = "已确认单办公室"
			}
		}
		device["account_ids"], device["inactive_count"] = []string{}, 0
		devices = append(devices, device)
		byID[id] = device
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	unassigned, unresolved := 0, 0
	for _, account := range accounts {
		account["phone_device_name"] = ""
		id := stringField(account, "phone_device_id")
		if id == "" {
			unassigned++
		}
		if strings.Contains(stringField(account, "phone_device_note"), "待核对") && stringField(account, "phone_device_source") != "manual" {
			unresolved++
		}
		if device, found := byID[id]; found {
			account["phone_device_name"] = device["name"]
			device["account_ids"] = append(device["account_ids"].([]string), stringField(account, "id"))
			if status := stringField(account, "status"); status == "封禁" || status == "停用" {
				device["inactive_count"] = device["inactive_count"].(int) + 1
			}
		}
	}
	for _, device := range devices {
		enrichDevice(device)
	}
	slices.SortFunc(devices, func(a, b map[string]any) int {
		if diff := strings.Compare(stringField(a, "office_id"), stringField(b, "office_id")); diff != 0 {
			return diff
		}
		if diff := displayOrder(a) - displayOrder(b); diff != 0 {
			return diff
		}
		return strings.Compare(stringField(a, "id"), stringField(b, "id"))
	})
	return map[string]any{"devices": devices, "statuses": slices.Clone(deviceStates), "unassigned": unassigned, "unresolved": unresolved}, nil
}

func displayOrder(device map[string]any) int {
	if value, err := strconv.Atoi(stringField(device, "number")); err == nil {
		return value
	}
	return 1000000
}

func enrichDevice(device map[string]any) {
	count := len(device["account_ids"].([]string))
	inactive := device["inactive_count"].(int)
	device["account_count"], device["attention"] = count, ""
	switch {
	case count == 0:
		device["occupancy"], device["suggested_state"] = "无当前账号", "待确认"
	case count == inactive:
		device["occupancy"], device["suggested_state"] = "仅封禁或停用", "待清理"
		device["attention"] = "关联账号均封禁或停用，请核对清理"
	default:
		device["occupancy"], device["suggested_state"] = "有在用或待确认账号", "在用"
	}
	if device["state_source"] == "auto" {
		device["state"] = device["suggested_state"]
	}
	if count == 0 && device["state"] != "可用闲置" {
		device["attention"] = "没有当前登录记录，需确认是否已清理"
	}
}
