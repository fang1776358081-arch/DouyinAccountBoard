// Package exchange implements the previewed CSV/XLSX business imports.
package exchange

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"douyinboard/internal/account"
	"douyinboard/internal/accountwrite"
	"douyinboard/internal/inventory"
	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
	"github.com/jackc/pgx/v5/pgconn"
)

const PreviewPath = "/api/workspace/import-preview"
const ApplyPath = "/api/workspace/import-apply"
const ExportPath = "/api/workspace/export"

type field struct{ label, key string }
type entity struct {
	table, mapping, ref, prefix string
	fields                      []field
}

var entities = map[string]entity{
	"accounts": {"accounts", "account_offices", "account_id", "account", []field{
		{"内部编号", "id"}, {"昵称", "name"}, {"抖音号", "douyin_id"}, {"注册手机号", "registration_phone"},
		{"账号状态", "status"}, {"账号类型", "account_type"}, {"关联大号编号", "parent_account_id"}, {"注册日期", "registered_on"},
		{"小号注册状态", "auxiliary_state"}, {"稳定状态", "card_stability"}, {"改密状态", "password_state"},
		{"养号开始日期", "nurture_started"}, {"备注", "note"},
	}},
	"cards": {"phone_cards", "phone_card_offices", "card_id", "card", []field{
		{"内部编号", "id"}, {"手机卡号码", "phone"}, {"卡名称", "label"}, {"卡主", "owner"}, {"运营商", "carrier"},
		{"卡状态", "status"}, {"卡位置", "location"}, {"卡归属", "ownership"}, {"负责人", "keeper"},
		{"接码联系人", "code_contact"}, {"接码状态", "code_state"}, {"接码事项", "code_note"}, {"归还状态", "return_state"},
		{"预计归还日期", "return_due"}, {"实际归还日期", "returned_on"}, {"到期日期", "expires_on"}, {"备注", "note"},
	}},
	"devices": {"phone_devices", "phone_device_offices", "device_id", "device", []field{
		{"内部编号", "id"}, {"手机序号", "number"}, {"手机名称", "name"}, {"安排状态", "state"},
		{"负责人", "owner"}, {"设备位置", "location"}, {"备注", "note"},
	}},
}

type difference struct {
	Field  string `json:"field"`
	Before any    `json:"before"`
	After  string `json:"after"`
}

type publicRow struct {
	Row      int          `json:"row"`
	Action   string       `json:"action"`
	Error    string       `json:"error"`
	Changes  []difference `json:"changes"`
	Selected bool         `json:"selected"`
	Identity string       `json:"identity,omitempty"`
	Entity   string       `json:"entity,omitempty"`
	Create   bool         `json:"create,omitempty"`
}

type plannedRow struct {
	publicRow
	Values   map[string]string `json:"values,omitempty"`
	Expected string            `json:"expected,omitempty"`
}

type preview struct {
	Kind  string         `json:"kind"`
	Scope map[string]any `json:"scope"`
	Plans []plannedRow   `json:"plans"`
}

var previewID = regexp.MustCompile(`^[0-9a-f]{32}$`)
var phoneNumber = regexp.MustCompile(`^1[0-9]{10}$`)
var deviceNumber = regexp.MustCompile(`^[0-9]{1,6}$`)
var storedDeviceID = regexp.MustCompile(`^(?:(?:office3:)|(?:phone:[a-f0-9]{32}:))?([0-9]{1,6})$`)

func String(value any) string {
	switch v := value.(type) {
	case string:
		return v
	case json.Number:
		return v.String()
	case int64:
		return strconv.FormatInt(v, 10)
	}
	return ""
}

func normalNumber(value string) string {
	number, err := strconv.Atoi(value)
	if err != nil || number < 1 || number > 999999 {
		return ""
	}
	return strconv.Itoa(number)
}

func serialFor(id string) string {
	found := storedDeviceID.FindStringSubmatch(id)
	if found == nil {
		return ""
	}
	return normalNumber(found[1])
}

func Permit(permission string) accountwrite.Authorize {
	return func(_ context.Context, _ *sql.Tx, actor session.User, _ map[string]any) error {
		if !actor.Has(permission) {
			return accountwrite.Forbidden("当前账号没有此操作权限")
		}
		return nil
	}
}

func stringList(value any) ([]string, bool) {
	switch list := value.(type) {
	case []string:
		return list, true
	case []any:
		items := make([]string, len(list))
		for i, item := range list {
			var ok bool
			items[i], ok = item.(string)
			if !ok {
				return nil, false
			}
		}
		return items, true
	default:
		return nil, value == nil
	}
}

func requestedScope(ctx context.Context, tx *sql.Tx, actor session.User, kind string, scope map[string]any) ([]string, error) {
	name := "office_ids"
	if kind == "accounts" {
		name = "office_id"
	}
	value, found := scope[name]
	if !found && !actor.Global() {
		return nil, accountwrite.Invalid("新增资料必须明确选择办公室")
	}
	var offices []string
	if name == "office_id" {
		if value != nil {
			id, ok := value.(string)
			if !ok {
				return nil, accountwrite.Invalid("办公室编号格式不正确")
			}
			if id != "" {
				offices = []string{id}
			}
		}
	} else {
		var ok bool
		offices, ok = stringList(value)
		if !ok || len(offices) > 100 {
			return nil, accountwrite.Invalid("办公室范围格式不正确")
		}
	}
	if !actor.Global() && len(offices) == 0 {
		return nil, accountwrite.Forbidden("目标办公室不在授权范围内")
	}
	result := []string{}
	for _, id := range offices {
		if id == "" || utf8.RuneCountInString(id) > 80 || slices.Contains(result, id) {
			return nil, accountwrite.Invalid("办公室范围格式不正确")
		}
		var active bool
		err := tx.QueryRowContext(ctx, "SELECT EXISTS(SELECT 1 FROM offices WHERE id=$1 AND active=1)", id).Scan(&active)
		if err != nil {
			return nil, err
		}
		if !active {
			return nil, accountwrite.Invalid("办公室不存在或已停用")
		}
		if !actor.Global() && !slices.Contains(actor.OfficeIDs, id) {
			return nil, accountwrite.Forbidden("目标办公室不在授权范围内")
		}
		result = append(result, id)
	}
	slices.Sort(result)
	return result, nil
}

func requireWrite(actor session.User, record map[string]any) error {
	offices, _ := stringList(record["office_ids"])
	if !actor.Global() && (len(offices) == 0 || slices.ContainsFunc(offices, func(id string) bool {
		return !slices.Contains(actor.OfficeIDs, id)
	})) {
		return accountwrite.Forbidden("无权导入此资料或关联对象")
	}
	return nil
}

func decodeRecord(raw string) (map[string]any, error) {
	decoder := json.NewDecoder(strings.NewReader(raw))
	decoder.UseNumber()
	var result map[string]any
	err := decoder.Decode(&result)
	return result, err
}

func matching(ctx context.Context, tx *sql.Tx, entity entity, where string, values ...any) ([]map[string]any, error) {
	// Table, mapping, column and predicates are fixed by callers, never cells.
	statement := `SELECT (to_jsonb(r)||jsonb_build_object('office_ids',
		COALESCE((SELECT jsonb_agg(o.office_id ORDER BY o.office_id) FROM ` + entity.mapping + ` o WHERE o.` + entity.ref + `=r.id),'[]'::jsonb)))::text
		FROM ` + entity.table + ` r WHERE ` + where
	rows, err := tx.QueryContext(ctx, statement, values...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []map[string]any{}
	for rows.Next() {
		var raw string
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		record, err := decodeRecord(raw)
		if err != nil {
			return nil, err
		}
		result = append(result, record)
	}
	return result, rows.Err()
}

func deviceMatches(ctx context.Context, tx *sql.Tx, serial, office string) ([]map[string]any, error) {
	rows, err := matching(ctx, tx, entities["devices"],
		"EXISTS(SELECT 1 FROM phone_device_offices s WHERE s.device_id=r.id AND s.office_id=$1) AND ltrim(regexp_replace(r.id,'^.*:',''),'0')=$2 ORDER BY r.id", office, serial)
	if err != nil {
		return nil, err
	}
	return slices.DeleteFunc(rows, func(row map[string]any) bool { return serialFor(String(row["id"])) != serial }), nil
}

type target struct {
	identity string
	old      map[string]any
	offices  []string
	serial   string
}

func findTarget(ctx context.Context, tx *sql.Tx, actor session.User, kind string, values map[string]string, scope map[string]any) (target, error) {
	var out target
	entity := entities[kind]
	id := values["id"]
	var rows []map[string]any
	var err error
	switch kind {
	case "accounts":
		if id != "" {
			rows, err = matching(ctx, tx, entity, "r.id=$1 LIMIT 1", id)
		} else if identity := values["douyin_id"]; identity != "" {
			rows, err = matching(ctx, tx, entity, "r.douyin_id=$1 LIMIT 2", identity)
			out.identity = "douyin:" + identity
		} else {
			return out, accountwrite.Invalid("需填写内部编号或真实抖音号；不按昵称猜测身份")
		}
	case "cards":
		phone := values["phone"]
		if !phoneNumber.MatchString(phone) {
			return out, accountwrite.Invalid("需填写完整11位手机卡号码")
		}
		rows, err = matching(ctx, tx, entity, "r.phone=$1 LIMIT 1", phone)
		out.identity = "phone:" + phone
	case "devices":
		raw := values["number"]
		if raw != "" {
			out.serial = normalNumber(raw)
			if !deviceNumber.MatchString(raw) || out.serial == "" {
				return out, accountwrite.Invalid("手机序号请填写1至6位正整数；内部编号请放在单独一列")
			}
		}
		if id != "" {
			rows, err = matching(ctx, tx, entity, "r.id=$1 LIMIT 1", id)
		} else if out.serial == "" {
			return out, accountwrite.Invalid("需填写内部编号或手机序号")
		} else {
			out.offices, err = requestedScope(ctx, tx, actor, kind, scope)
			if err == nil && len(out.offices) != 1 {
				return out, accountwrite.Invalid("按手机序号导入必须明确选择一个办公室")
			}
			if err == nil {
				rows, err = deviceMatches(ctx, tx, out.serial, out.offices[0])
			}
		}
	}
	if err != nil {
		return out, err
	}
	if len(rows) > 1 {
		if kind == "devices" {
			return out, accountwrite.Invalid("该办公室的手机序号存在多条资料，请先核对")
		}
		return out, accountwrite.Invalid("该抖音号对应多条记录，需明确内部编号")
	}
	if len(rows) == 1 {
		out.old = rows[0]
		if err := requireWrite(actor, out.old); err != nil {
			return out, err
		}
	}
	if id != "" && (out.old == nil || String(out.old["id"]) != id) {
		if kind == "cards" {
			return out, accountwrite.Invalid("内部编号与手机卡号码不一致")
		}
		return out, accountwrite.Invalid("内部编号不存在；新增资料请留空内部编号")
	}
	if out.old == nil && out.offices == nil {
		out.offices, err = requestedScope(ctx, tx, actor, kind, scope)
		if err != nil {
			return out, err
		}
	}
	switch kind {
	case "accounts":
		if out.old != nil {
			out.identity = "id:" + String(out.old["id"])
		}
	case "devices":
		if out.old != nil {
			actual := serialFor(String(out.old["id"]))
			if actual == "" || out.serial != "" && actual != out.serial {
				return out, accountwrite.Invalid("内部编号与手机序号不一致")
			}
			out.serial = actual
			out.offices, _ = stringList(out.old["office_ids"])
			if len(out.offices) != 1 {
				return out, accountwrite.Invalid("已有手机未明确且仅归属一个办公室，请先核对归属")
			}
			reserved, err := deviceMatches(ctx, tx, actual, out.offices[0])
			if err != nil {
				return out, err
			}
			if len(reserved) != 1 || String(reserved[0]["id"]) != String(out.old["id"]) {
				return out, accountwrite.Invalid("该办公室的手机序号存在多条资料，请先核对")
			}
		}
		out.identity = "device-office:" + out.offices[0] + ":" + out.serial
	}
	return out, nil
}

func execute(ctx context.Context, tx *sql.Tx, actor session.User, kind string, values map[string]string, candidate target) error {
	payload := map[string]any{}
	for key, value := range candidate.old {
		payload[key] = value
	}
	delete(payload, "office_ids")
	if candidate.old == nil && kind == "accounts" {
		payload = map[string]any{"name": "", "computer_id": "unassigned", "slot": json.Number("1"),
			"status": "待确认", "account_type": "待确认", "note": ""}
	}
	for key, value := range values {
		if key != "id" {
			payload[key] = value
		}
	}
	create := candidate.old == nil
	if create {
		if kind == "accounts" {
			payload["office_id"] = nil
			if len(candidate.offices) != 0 {
				payload["office_id"] = candidate.offices[0]
			}
		} else {
			payload["office_ids"] = candidate.offices
		}
	} else {
		versions, err := targetVersions(ctx, tx, kind, candidate.old)
		if err != nil {
			return err
		}
		payload["_expected_versions"] = versions
	}
	switch kind {
	case "accounts":
		delete(payload, "phone_device_id")
		if values["nurture_started"] == "" {
			delete(payload, "nurture_started")
		}
		if payload["slot"] == nil {
			payload["slot"] = json.Number("1")
		}
		_, err := account.SaveAccount(ctx, tx, actor, payload, create)
		return err
	case "cards":
		_, err := inventory.SaveCard(ctx, tx, actor, payload)
		return err
	case "devices":
		payload["number"] = candidate.serial
		if candidate.old != nil && candidate.old["state_source"] == "auto" && (values["state"] == "" || values["state"] == candidate.old["state"]) {
			payload["state"] = "auto"
		}
		_, err := inventory.SaveDevice(ctx, tx, actor, payload, create)
		return err
	}
	return accountwrite.Invalid("导入类型不正确")
}

func targetVersions(ctx context.Context, tx *sql.Tx, kind string, old map[string]any) (map[string]any, error) {
	id := String(old["id"])
	if kind == "devices" {
		return inventory.DeviceVersions(ctx, tx, id)
	}
	token, err := recordwire.Version(old)
	return map[string]any{entities[kind].prefix + ":" + id: token}, err
}

func targetVersion(ctx context.Context, tx *sql.Tx, kind string, old map[string]any) (string, error) {
	if old == nil {
		return "", nil
	}
	versions, err := targetVersions(ctx, tx, kind, old)
	if err != nil {
		return "", err
	}
	return recordwire.Version(versions)
}

func rowError(item *plannedRow, err error) error {
	var domain accountwrite.APIError
	if errors.As(err, &domain) {
		if domain.Status == 403 {
			item.publicRow = publicRow{Row: item.Row, Changes: []difference{}}
			item.Values, item.Expected = nil, ""
			item.Error = "无权导入此资料或关联对象"
		} else {
			item.Error = domain.Message
		}
	} else {
		var pg *pgconn.PgError
		if !errors.As(err, &pg) || !strings.HasPrefix(pg.Code, "23") {
			return err
		}
		item.Error = "数据约束不满足，请核对必填项、唯一值及关联资料"
	}
	item.Action, item.Selected = "error", false
	return nil
}

func validateRow(ctx context.Context, tx *sql.Tx, actor session.User, kind string, item *plannedRow, candidate target) error {
	if _, err := tx.ExecContext(ctx, "SAVEPOINT exchange_validation"); err != nil {
		return err
	}
	saveErr := execute(ctx, tx, actor, kind, item.Values, candidate)
	if _, err := tx.ExecContext(ctx, "ROLLBACK TO SAVEPOINT exchange_validation"); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, "RELEASE SAVEPOINT exchange_validation"); err != nil {
		return err
	}
	if saveErr != nil {
		return rowError(item, saveErr)
	}
	return nil
}

func Preview(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	kind := String(body["kind"])
	entity, ok := entities[kind]
	if !ok {
		return nil, accountwrite.Invalid("导入类型不正确")
	}
	rows, err := readSheet(String(body["filename"]), String(body["content"]))
	if err != nil {
		return nil, accountwrite.Invalid(err.Error())
	}
	keys, ignored := make([]string, len(rows[0])), []string{}
	seen := map[string]bool{}
	mapped := 0
	for i, label := range rows[0] {
		label = strings.TrimSpace(label)
		if seen[label] {
			return nil, accountwrite.Invalid("表头存在重复列")
		}
		seen[label] = true
		for _, f := range entity.fields {
			if label == f.label {
				keys[i] = f.key
				mapped++
				break
			}
		}
		if keys[i] == "" {
			ignored = append(ignored, label)
		}
	}
	if mapped == 0 {
		return nil, accountwrite.Invalid("没有识别到列，请下载本系统模板")
	}
	plan := preview{Kind: kind, Scope: map[string]any{}, Plans: []plannedRow{}}
	for _, key := range []string{"office_id", "office_ids"} {
		if value, ok := body[key]; ok {
			plan.Scope[key] = value
		}
	}
	identities := map[string]int{}
	for i, row := range rows[1:] {
		if !slices.ContainsFunc(row, func(value string) bool { return strings.TrimSpace(value) != "" }) {
			continue
		}
		item := plannedRow{publicRow: publicRow{Row: i + 2, Action: "error", Changes: []difference{}}, Values: map[string]string{}}
		for j, key := range keys {
			if key != "" && j < len(row) {
				if value := strings.TrimSpace(row[j]); value != "" {
					item.Values[key] = value
				}
			}
		}
		candidate, err := findTarget(ctx, tx, actor, kind, item.Values, plan.Scope)
		if err != nil {
			if err := rowError(&item, err); err != nil {
				return nil, err
			}
			plan.Plans = append(plan.Plans, item)
			continue
		}
		if first, found := identities[candidate.identity]; found {
			for _, duplicate := range []*plannedRow{&plan.Plans[first], &item} {
				duplicate.Action, duplicate.Error, duplicate.Selected = "error", "同一文件重复记录，请合并后重试", false
			}
			plan.Plans = append(plan.Plans, item)
			continue
		}
		identities[candidate.identity] = len(plan.Plans)
		item.Create = candidate.old == nil
		item.Identity = candidate.identity
		if item.Create {
			item.Action, item.Selected = "create", true
		} else {
			item.Action = "update"
			item.Entity = entity.prefix + ":" + String(candidate.old["id"])
			item.Expected, err = targetVersion(ctx, tx, kind, candidate.old)
			if err != nil {
				return nil, err
			}
		}
		for _, key := range keys {
			value, present := item.Values[key]
			if !present || key == "id" {
				continue
			}
			before := candidate.old[key]
			if key == "number" {
				before = serialFor(String(candidate.old["id"]))
				value = candidate.serial
			}
			if before == nil {
				before = ""
			}
			if item.Create || String(before) != value {
				item.Changes = append(item.Changes, difference{key, before, value})
			}
		}
		if !item.Create && len(item.Changes) == 0 {
			item.Action = "same"
		}
		if err := validateRow(ctx, tx, actor, kind, &item, candidate); err != nil {
			return nil, err
		}
		plan.Plans = append(plan.Plans, item)
	}
	ident, err := storePreview(ctx, tx, actor.ID, "import", plan)
	if err != nil {
		return nil, err
	}
	public := make([]publicRow, len(plan.Plans))
	for i, row := range plan.Plans {
		public[i] = row.publicRow
	}
	return map[string]any{"preview_id": ident, "rows": public, "ignored_columns": ignored,
		"note": "空白保留原值；不导入发布安排、不删除资料，已有记录默认不覆盖，请逐行选择确认。"}, nil
}

func storePreview(ctx context.Context, tx *sql.Tx, actor int64, kind string, payload any) (string, error) {
	raw, err := recordwire.Encode(payload)
	if err != nil {
		return "", err
	}
	ident := newHexID()
	now := time.Now().Unix()
	if _, err := tx.ExecContext(ctx, "DELETE FROM operations_previews WHERE expires_at<$1", now); err != nil {
		return "", err
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO operations_previews(id,actor_id,expires_at,kind,payload)
		VALUES($1,$2,$3,$4,$5)`, ident, actor, now+1800, kind, string(raw))
	return ident, err
}

func newHexID() string {
	id := make([]byte, 16)
	rand.Read(id)
	return hex.EncodeToString(id)
}

func readPreview(ctx context.Context, tx *sql.Tx, actor int64, kind, ident string, payload any) error {
	if !previewID.MatchString(ident) {
		return accountwrite.Invalid("预览已过期或不属于当前登录人员，请重新预览")
	}
	var raw string
	err := tx.QueryRowContext(ctx, `SELECT payload FROM operations_previews WHERE id=$1
		AND actor_id=$2 AND kind=$3 AND expires_at>$4`, ident, actor, kind, time.Now().Unix()).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return accountwrite.Invalid("预览已过期或不属于当前登录人员，请重新预览")
	}
	if err != nil {
		return err
	}
	decoder := json.NewDecoder(strings.NewReader(raw))
	decoder.UseNumber()
	return decoder.Decode(payload)
}

func Apply(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	id := String(body["preview_id"])
	var plan preview
	if err := readPreview(ctx, tx, actor.ID, "import", id, &plan); err != nil {
		return nil, err
	}
	selected, err := selectedRows(body["rows"])
	if err != nil {
		return nil, err
	}
	group, err := accountwrite.NewGroup()
	if err != nil {
		return nil, err
	}
	ctx = accountwrite.WithHistoryGroup(ctx, ApplyPath, group)
	count := 0
	for _, item := range plan.Plans {
		if !selected[item.Row] {
			continue
		}
		if item.Action != "create" && item.Action != "update" {
			return nil, accountwrite.Invalid("不能导入错误行或无变化行")
		}
		candidate, err := findTarget(ctx, tx, actor, plan.Kind, item.Values, plan.Scope)
		if err != nil {
			return nil, err
		}
		version, err := targetVersion(ctx, tx, plan.Kind, candidate.old)
		if err != nil {
			return nil, err
		}
		if candidate.identity != item.Identity || version != item.Expected {
			return nil, accountwrite.Conflict(fmt.Sprintf("第%d行已被修改，请重新预览", item.Row))
		}
		if err := execute(ctx, tx, actor, plan.Kind, item.Values, candidate); err != nil {
			return nil, err
		}
		count++
	}
	if count != len(selected) {
		return nil, accountwrite.Invalid("选择的行不正确")
	}
	if _, err := tx.ExecContext(ctx, "DELETE FROM operations_previews WHERE id=$1", id); err != nil {
		return nil, err
	}
	if err := accountwrite.Audit(ctx, tx, actor, ApplyPath, ""); err != nil {
		return nil, err
	}
	return map[string]any{"imported": count, "_record_versions": map[string]any{}}, nil
}

func selectedRows(value any) (map[int]bool, error) {
	list, ok := value.([]any)
	if !ok || len(list) == 0 || len(list) > maxRows {
		return nil, accountwrite.Invalid("请勾选要导入的行")
	}
	result := map[int]bool{}
	for _, value := range list {
		n, ok := value.(json.Number)
		row, err := strconv.Atoi(string(n))
		if !ok || err != nil || row < 2 || row > maxRows {
			return nil, accountwrite.Invalid("选择的行不正确")
		}
		result[row] = true
	}
	return result, nil
}
