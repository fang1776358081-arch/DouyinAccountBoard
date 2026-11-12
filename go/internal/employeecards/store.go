// Package employeecards serves the employee real-name SIM ledger.
package employeecards

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"regexp"
	"slices"
	"strings"
	"time"
	"unicode/utf8"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

type dataRow = map[string]any

const (
	Path                = "/api/employee-cards"
	PreviewPath         = Path + "/preview"
	ApplyPath           = Path + "/apply"
	employeePreviewKind = "employee-cards"
	// The original 0005 Git blob. Reads and writes require the installed migration.
	employeeMigrationHash = "1f207d7a130b3f586189cb1097930ede2f9f1dac45df5d422184918aada435e1"
)

var (
	employeeUUID    = regexp.MustCompile(`^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$`)
	employeeActions = []string{"employee", "operator", "rules", "verification", "application", "card_link", "card_link_batch", "replacement", "cancel_request", "release"}
	employeeTables  = []string{"employee_card_employees", "employee_card_operators", "employee_card_rules",
		"employee_card_applications", "employee_card_units", "employee_card_links", "employee_card_verifications",
		"employee_card_events", "employee_card_requests"}
)

type Service struct {
	Write accountwrite.Service
}

func employeeToday() string {
	return time.Now().In(time.FixedZone("Asia/Shanghai", 8*3600)).Format(time.DateOnly)
}

func available(ctx context.Context, tx *sql.Tx) (bool, error) {
	var enabled bool
	err := tx.QueryRowContext(ctx, `SELECT
		EXISTS(SELECT 1 FROM board_candidate.schema_migrations WHERE version=5 AND sha256=$1)
		AND NOT EXISTS(SELECT 1 FROM unnest($2::text[]) t(name) WHERE to_regclass('board_candidate.'||t.name) IS NULL)`,
		employeeMigrationHash, employeeTables).Scan(&enabled)
	return enabled, err
}

func allowedManager(actor session.User) bool {
	return actor.Role == "owner" || actor.Has("card_manage")
}

func visibleEmployee(actor session.User, row dataRow) bool {
	if row == nil {
		return false
	}
	if allowedManager(actor) {
		return actor.Global() || slices.Contains(actor.OfficeIDs, field(row, "office_id"))
	}
	value, _ := numeric(row["auth_user_id"])
	return value == actor.ID
}

func authorizeManager(_ context.Context, _ *sql.Tx, actor session.User, _ map[string]any) error {
	if !allowedManager(actor) {
		return accountwrite.Forbidden("当前账号没有此操作权限")
	}
	return nil
}

func authorizeAction(ctx context.Context, tx *sql.Tx, actor session.User, action, employee string) error {
	if err := authorizeManager(ctx, tx, actor, nil); err != nil {
		return err
	}
	if !slices.Contains(employeeActions, action) {
		return accountwrite.Invalid("员工办卡操作不正确")
	}
	if action == "operator" || action == "rules" {
		if actor.Role != "owner" {
			return accountwrite.Forbidden("当前账号没有此操作权限")
		}
		return nil
	}
	if employee != "" {
		_, err := requireEmployee(ctx, tx, actor, employee)
		return err
	}
	return nil
}

func requireEmployee(ctx context.Context, tx *sql.Tx, actor session.User, id string) (dataRow, error) {
	row, err := getOne(ctx, tx, `SELECT to_json(e)::text FROM board_candidate.employee_card_employees e WHERE id=$1`, id)
	if err != nil {
		return nil, err
	}
	if !visibleEmployee(actor, row) {
		return nil, accountwrite.Forbidden("无权访问此员工资料")
	}
	return row, nil
}

func decodeJSON(raw string, out any) error {
	decoder := json.NewDecoder(strings.NewReader(raw))
	decoder.UseNumber()
	return decoder.Decode(out)
}

func getOne(ctx context.Context, tx *sql.Tx, query string, args ...any) (dataRow, error) {
	var raw string
	err := tx.QueryRowContext(ctx, query, args...).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var result dataRow
	err = decodeJSON(raw, &result)
	return result, err
}

func getRows(ctx context.Context, tx *sql.Tx, query string, args ...any) ([]dataRow, error) {
	rows, err := tx.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []dataRow{}
	for rows.Next() {
		var raw string
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		var item dataRow
		if err := decodeJSON(raw, &item); err != nil {
			return nil, err
		}
		result = append(result, item)
	}
	return result, rows.Err()
}

func field(row dataRow, key string) string {
	value, _ := row[key].(string)
	return value
}

func numeric(value any) (int64, bool) {
	switch v := value.(type) {
	case json.Number:
		number, err := v.Int64()
		return number, err == nil
	case int64:
		return v, true
	case int:
		return int64(v), true
	default:
		return 0, false
	}
}

func numberValue(value any) int64 {
	number, _ := numeric(value)
	return number
}

func truth(row dataRow, key string) bool {
	value, _ := row[key].(bool)
	return value
}

func valueOr(row dataRow, key string, fallback any) any {
	if value, found := row[key]; found {
		return value
	}
	return fallback
}

func validateFields(row dataRow, keys ...string) error {
	if row == nil {
		return accountwrite.Invalid("存在未开放的员工办卡字段")
	}
	for key := range row {
		if !slices.Contains(keys, key) {
			return accountwrite.Invalid("存在未开放的员工办卡字段")
		}
	}
	return nil
}

func employeeText(raw any, label string, limit int, required bool) (string, error) {
	value, ok := raw.(string)
	if !ok {
		return "", accountwrite.Invalid(label + "格式不正确")
	}
	value = strings.TrimSpace(value)
	if utf8.RuneCountInString(value) > limit || strings.ContainsFunc(value, func(c rune) bool { return c < 32 && c != '\n' && c != '\t' }) {
		return "", accountwrite.Invalid(label + "过长或包含无效字符")
	}
	if required && value == "" {
		return "", accountwrite.Invalid("请填写" + label)
	}
	return value, nil
}

func employeeDate(raw any, label string, optional bool, latest string) (string, error) {
	value, err := employeeText(raw, label, 10, !optional)
	if err != nil || value == "" {
		return value, err
	}
	parsed, err := time.Parse(time.DateOnly, value)
	if err != nil || parsed.Year() < 1 || parsed.Format(time.DateOnly) != value || (latest != "" && value > latest) {
		return "", accountwrite.Invalid(label + "不正确或晚于允许日期")
	}
	return value, nil
}

func employeeNumber(raw any, label string, min, max int64, nullable bool) (any, error) {
	if raw == nil && nullable {
		return nil, nil
	}
	value, ok := numeric(raw)
	if !ok || value < min || value > max {
		return nil, accountwrite.Invalid(label + "需为范围内的整数")
	}
	return value, nil
}

func employeeChoice(raw any, label string, choices ...string) (string, error) {
	value, ok := raw.(string)
	if !ok || !slices.Contains(choices, value) {
		return "", accountwrite.Invalid(label + "不正确")
	}
	return value, nil
}

func employeeStrings(raw any) ([]string, bool) {
	switch values := raw.(type) {
	case []string:
		return slices.Clone(values), true
	case []any:
		result := make([]string, 0, len(values))
		for _, value := range values {
			item, ok := value.(string)
			if !ok {
				return nil, false
			}
			result = append(result, item)
		}
		return result, true
	default:
		return nil, false
	}
}

func employeeKey() (string, error) {
	raw, err := accountwrite.NewGroup()
	if err != nil {
		return "", err
	}
	return fmt.Sprintf("%s-%s-%s-%s-%s", raw[:8], raw[8:12], raw[12:16], raw[16:20], raw[20:]), nil
}

func encodeRow(value any) (string, error) {
	raw, err := recordwire.Encode(value)
	return string(raw), err
}

func updateEvent(ctx context.Context, tx *sql.Tx, actor session.User, employee, action string, detail dataRow) error {
	id, err := employeeKey()
	if err != nil {
		return err
	}
	raw, err := encodeRow(detail)
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO board_candidate.employee_card_events(id,employee_id,at,actor_id,action,detail)
		VALUES($1,$2,$3,$4,$5,$6)`, id, employee, time.Now().Unix(), actor.ID, action, raw)
	return err
}

func payloadRow(row dataRow, keys ...string) (dataRow, error) {
	var payload dataRow
	if err := decodeJSON(field(row, "payload"), &payload); err != nil {
		return nil, err
	}
	for _, key := range keys {
		payload[key] = row[key]
	}
	return payload, nil
}

func entityPayloads(rows []dataRow, keys ...string) ([]dataRow, error) {
	result := make([]dataRow, 0, len(rows))
	for _, row := range rows {
		payload, err := payloadRow(row, keys...)
		if err != nil {
			return nil, err
		}
		result = append(result, payload)
	}
	return result, nil
}

func related(rows []dataRow, employee string) []dataRow {
	result := []dataRow{}
	for _, row := range rows {
		if valueOr(row, "employee_id", row["id"]) == employee {
			result = append(result, row)
		}
	}
	return result
}

type employeeBundle map[string][]dataRow

func (bundle employeeBundle) version(actor session.User, employee, generation string) (string, error) {
	if generation == "" || utf8.RuneCountInString(generation) > 200 {
		return "", accountwrite.Invalid("恢复代次未配置，员工办卡写入与请求回执不可用")
	}
	records := maps.Clone(bundle)
	if employee != "" {
		for _, key := range []string{"employees", "units", "applications", "verifications", "links", "name_cards", "name_match_metadata"} {
			records[key] = related(records[key], employee)
		}
	}
	permissions := append([]string{}, actor.Permissions...)
	offices := append([]string{}, actor.OfficeIDs...)
	slices.Sort(permissions)
	slices.Sort(offices)
	return recordwire.Version(dataRow{"scope": dataRow{"actor_id": actor.ID, "role": actor.Role,
		"permissions": permissions, "offices": offices, "restore_generation": generation}, "records": records})
}

func readOperator(ctx context.Context, tx *sql.Tx, id string) (dataRow, error) {
	row, err := getOne(ctx, tx, `SELECT to_json(o)::text FROM board_candidate.employee_card_operators o WHERE id=$1`, id)
	if err != nil {
		return nil, err
	}
	if row == nil {
		return nil, accountwrite.Invalid("请选择已配置的运营商")
	}
	return payloadRow(row, "revision")
}

func readUnit(ctx context.Context, tx *sql.Tx, employee, id string) (dataRow, error) {
	row, err := getOne(ctx, tx, `SELECT to_json(u)::text FROM board_candidate.employee_card_units u WHERE id=$1 AND employee_id=$2`, id, employee)
	if err != nil {
		return nil, err
	}
	if row == nil {
		return nil, accountwrite.Invalid("该员工的实名占额记录不存在")
	}
	return row, nil
}

func validateOffice(ctx context.Context, tx *sql.Tx, actor session.User, raw any) (string, error) {
	office, err := employeeText(raw, "办公室", 80, false)
	if err != nil {
		return "", err
	}
	if office == "" {
		if !actor.Global() {
			return "", accountwrite.Forbidden("只有全局管理人员可登记待分配员工")
		}
		return "", nil
	}
	var active bool
	if err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM board_candidate.offices WHERE id=$1 AND active=1)`, office).Scan(&active); err != nil {
		return "", err
	}
	if !active {
		return "", accountwrite.Invalid("办公室不存在或已停用")
	}
	if !actor.Global() && !slices.Contains(actor.OfficeIDs, office) {
		return "", accountwrite.Forbidden("无权在该办公室登记员工")
	}
	return office, nil
}

func lookupCard(ctx context.Context, tx *sql.Tx, actor session.User, raw any, includeScope bool) (dataRow, error) {
	value, err := employeeNumber(raw, "手机卡编号", 1, 9223372036854775807, false)
	if err != nil {
		return nil, err
	}
	card, err := getOne(ctx, tx, `SELECT (to_jsonb(c)||jsonb_build_object('_office_ids',COALESCE(
		(SELECT jsonb_agg(m.office_id ORDER BY m.office_id) FROM board_candidate.phone_card_offices m WHERE m.card_id=c.id),'[]'::jsonb)))::text
		FROM board_candidate.phone_cards c WHERE id=$1`, value)
	if err != nil {
		return nil, err
	}
	offices, _ := employeeStrings(card["_office_ids"])
	if card == nil || (!actor.Global() && len(offices) == 0) {
		return nil, accountwrite.Forbidden("无权访问此资料")
	}
	if !actor.Global() {
		for _, office := range offices {
			if !slices.Contains(actor.OfficeIDs, office) {
				return nil, accountwrite.Forbidden("无权访问此资料")
			}
		}
	}
	if !includeScope {
		delete(card, "_office_ids")
	}
	return card, nil
}
