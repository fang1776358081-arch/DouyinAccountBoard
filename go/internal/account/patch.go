package account

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"net/url"
	"reflect"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/inventory"
	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

const patchPath = "/api/account/patch"

type WriteHandler struct{ accountwrite.Service }

func (h WriteHandler) Patch() http.HandlerFunc {
	return h.Handle(patchPath, "修改账号资料必须带请求标识", authorizePatch, applyPatch)
}

var accountStates = []string{"正常", "养号7天", "短期封禁", "封禁", "发不了", "停用", "待确认"}
var stabilityStates = []string{"待确认", "未稳定", "已稳定"}
var passwordStates = []string{"待确认", "未修改", "已修改"}
var patchPermissions = map[string]string{
	"name": "account_manage", "note": "account_manage", "computer_id": "account_manage", "slot": "account_manage",
	"douyin_id": "account_manage", "homepage": "account_manage", "account_type": "account_manage", "parent_account_id": "account_manage",
	"registered_on": "account_manage", "lifecycle_note": "account_manage", "registration_phone": "account_manage", "office_id": "account_manage",
	"status": "account_status", "auxiliary_state": "account_progress", "card_stability": "account_progress", "password_state": "account_progress",
	"phone_device_id": "device_manage",
}

func identifier(value any) string {
	switch value := value.(type) {
	case string:
		return value
	case json.Number:
		if !strings.ContainsAny(string(value), ".eE") {
			return string(value)
		}
	case int:
		return strconv.Itoa(value)
	case int64:
		return strconv.FormatInt(value, 10)
	}
	return ""
}

func integer(value any) (int64, bool) {
	switch value := value.(type) {
	case json.Number:
		number, err := value.Int64()
		return number, err == nil
	case int:
		return int64(value), true
	case int64:
		return value, true
	}
	return 0, false
}

func authorizePatch(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) error {
	changes, ok := body["changes"].(map[string]any)
	if !ok {
		return accountwrite.Invalid("请提交需要修改的字段")
	}
	old, err := requireAccount(ctx, tx, actor, identifier(body["id"]))
	if err != nil {
		return err
	}
	if err := authorizeReferences(ctx, tx, actor, old, changes); err != nil {
		return err
	}
	if len(changes) == 0 {
		return accountwrite.Invalid("请提交需要修改的字段")
	}
	for field := range changes {
		if _, allowed := patchPermissions[field]; !allowed {
			return accountwrite.Invalid("包含不允许修改的字段")
		}
	}
	for field, value := range changes {
		if !actor.Has(patchPermissions[field]) {
			return accountwrite.Forbidden(permissionDenied)
		}
		if field == "slot" {
			if _, ok := integer(value); !ok {
				return accountwrite.Invalid("账号位置必须为整数")
			}
			continue
		}
		if value == nil && slices.Contains([]string{"parent_account_id", "phone_device_id", "office_id"}, field) {
			continue
		}
		if text, ok := value.(string); !ok || strings.ContainsRune(text, 0) {
			return accountwrite.Invalid("字段格式不正确：" + field)
		}
	}
	if value, present := body["return_account"]; present {
		isInline, ok := value.(bool)
		if !ok {
			return accountwrite.Invalid("账号回执选项必须为布尔值")
		}
		if isInline {
			for field := range changes {
				if field != "name" && field != "status" && field != "note" {
					return accountwrite.Invalid("行内编辑仅支持昵称、状态和备注")
				}
			}
		}
	}
	return nil
}

func applyPatch(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	id := identifier(body["id"])
	old, err := requireAccount(ctx, tx, actor, id)
	if err != nil {
		return nil, err
	}
	changes := body["changes"].(map[string]any)
	set, err := captureChanges(ctx, tx, patchPath, id, old, changes)
	if err != nil {
		return nil, err
	}
	if err := accountwrite.CheckVersions(body, set.checked); err != nil {
		return nil, err
	}
	day := boardToday()
	if err := savePatch(ctx, tx, actor, id, old, changes, day); err != nil {
		return nil, err
	}
	result, next, err := set.finish(ctx, tx, actor, day)
	if err != nil {
		return nil, err
	}
	if body["return_account"] == true {
		inline, err := inlineRecord(next)
		if err == nil {
			err = tx.QueryRowContext(ctx, "SELECT COALESCE(MAX(id),0) FROM board_candidate.operations_history").Scan(&inline.Cursor)
		}
		if err != nil {
			return nil, err
		}
		result["account"], result["record_version"], result["cursor"] = inline.Account, inline.RecordVersion, inline.Cursor
	}
	return result, nil
}

func savePatch(ctx context.Context, tx *sql.Tx, actor session.User, id string, old, changes map[string]any, day time.Time) error {
	values, err := buildAccountChanges(ctx, tx, actor, id, old, changes, day, false)
	if err != nil {
		return err
	}
	if err := updateAccount(ctx, tx, id, values); err != nil {
		return err
	}
	return updateRelations(ctx, tx, actor, id, old, changes, false)
}

// BuildTablePatch validates planned account fields without writing. The table
// synchronization service owns authorization, allowed fields and version checks.
func BuildTablePatch(ctx context.Context, tx *sql.Tx, actor session.User, id string, old, changes map[string]any, day time.Time) (map[string]any, error) {
	return buildAccountChanges(ctx, tx, actor, id, old, changes, day, false)
}

func buildAccountChanges(ctx context.Context, tx *sql.Tx, actor session.User, id string, old, changes map[string]any, day time.Time, full bool) (map[string]any, error) {
	values := map[string]any{}
	if raw, ok := changes["name"]; ok {
		name := strip(identifier(raw))
		if name == "" || utf8.RuneCountInString(name) > 100 {
			return nil, accountwrite.Invalid("账号名称不能为空或超过100字")
		}
		values["name"] = name
	}
	if raw, ok := changes["note"]; ok {
		note := identifier(raw)
		if !full && utf8.RuneCountInString(note) > 2000 {
			return nil, accountwrite.Invalid("备注不能超过2000字")
		}
		values["note"] = accountwrite.Truncate(note, 2000)
	}
	if raw, ok := changes["status"]; ok {
		status := identifier(raw)
		if !slices.Contains(accountStates, status) {
			return nil, accountwrite.Invalid("账号状态不正确")
		}
		values["status"] = status
		if status == "养号7天" && old["status"] != status {
			values["nurture_started"] = ""
		}
	}
	computer, computerSet := changes["computer_id"]
	slot, slotSet := changes["slot"]
	if computerSet || slotSet {
		if !computerSet {
			computer = old["computer_id"]
		}
		if !slotSet {
			slot = old["slot"]
			if n, _ := integer(slot); n == 0 {
				slot = 1
			}
		}
		computerID := identifier(computer)
		number, ok := integer(slot)
		if !ok || number < 1 || number > 100 {
			return nil, accountwrite.Invalid("电脑或账号位置不正确")
		}
		exists, err := rowExists(ctx, tx, `SELECT EXISTS(SELECT 1 FROM board_candidate.computers WHERE id=$1)`, computerID)
		if err != nil {
			return nil, err
		}
		if !exists {
			return nil, accountwrite.Invalid("电脑或账号位置不正确")
		}
		values["computer_id"], values["slot"] = computerID, number
		if computerID == "unassigned" {
			values["slot"] = nil
		}
	}
	if raw, ok := changes["homepage"]; ok {
		homepage, err := NormalizeHomepage(identifier(raw))
		if err != nil {
			return nil, err
		}
		values["homepage"] = homepage
	}
	if raw, ok := changes["douyin_id"]; ok {
		identity := strip(identifier(raw))
		if utf8.RuneCountInString(identity) > 100 || strings.IndexFunc(identity, pythonSpace) >= 0 {
			return nil, accountwrite.Invalid("真实抖音号不能包含空格或超过100字")
		}
		if identity != "" && identity != old["douyin_id"] {
			exists, err := rowExists(ctx, tx, `SELECT EXISTS(SELECT 1 FROM board_candidate.accounts WHERE douyin_id=$1 AND id<>$2)`, identity, id)
			if err != nil {
				return nil, err
			}
			if exists {
				return nil, accountwrite.Invalid("这个抖音号已经在工作台中，请编辑已有账号")
			}
		}
		values["douyin_id"] = identity
		if _, ok := values["homepage"]; !ok {
			values["homepage"] = old["homepage"]
		}
		if identity != old["douyin_id"] && values["homepage"] == old["homepage"] {
			values["homepage"] = ""
		}
	}
	if full || slices.ContainsFunc(lifecycleFields, func(field string) bool { _, found := changes[field]; return found }) {
		lifecycle, err := validateLifecycle(ctx, tx, actor, id, changes, old, day)
		if err != nil {
			return nil, err
		}
		for field, value := range lifecycle {
			if full || value != old[field] {
				values[field] = value
			}
		}
	}
	for _, field := range []string{"douyin_id", "homepage"} {
		if value, ok := values[field]; ok && value != old[field] {
			values["work_count"], values["last_collected"], values["last_attempted"] = nil, "", ""
			values["collection_state"], values["collection_error"] = "", ""
		}
	}
	return values, nil
}

var homepagePattern = regexp.MustCompile(`^/user/[A-Za-z0-9_-]{5,200}/?$`)

// NormalizeHomepage accepts an official Douyin profile and discards sharing parameters.
func NormalizeHomepage(value string) (string, error) {
	value = strip(value)
	if value == "" {
		return "", nil
	}
	parsed, err := url.Parse(value)
	if err != nil || parsed.Scheme != "https" || parsed.User != nil || parsed.Host != "www.douyin.com" || !homepagePattern.MatchString(parsed.EscapedPath()) {
		return "", accountwrite.Invalid("请从抖音网页版复制完整个人主页链接：https://www.douyin.com/user/...")
	}
	return "https://www.douyin.com" + strings.TrimSuffix(parsed.Path, "/"), nil
}

func updateAccount(ctx context.Context, tx *sql.Tx, id string, values map[string]any) error {
	if len(values) == 0 {
		return nil
	}
	var assignments []string
	args := []any{id}
	for field, value := range values {
		assignments = append(assignments, field+"=$"+strconv.Itoa(len(args)+1))
		args = append(args, value)
	}
	_, err := tx.ExecContext(ctx, `UPDATE board_candidate.accounts SET `+strings.Join(assignments, ",")+` WHERE id=$1`, args...)
	var sqlState interface{ SQLState() string }
	if errors.As(err, &sqlState) && sqlState.SQLState() == "23505" {
		return accountwrite.Invalid("这个电脑位置已经有账号，请选择空位")
	}
	return err
}

func updateRelations(ctx context.Context, tx *sql.Tx, actor session.User, id string, old, changes map[string]any, create bool) error {
	stable, stableSet := changes["card_stability"]
	password, passwordSet := changes["password_state"]
	if stableSet || passwordSet || create {
		if !stableSet {
			stable = old["card_stability"]
		}
		if !passwordSet {
			password = old["password_state"]
		}
		if !slices.Contains(stabilityStates, identifier(stable)) || !slices.Contains(passwordStates, identifier(password)) {
			return accountwrite.Invalid("账号稳定或改密状态不正确")
		}
		if err := updateAccount(ctx, tx, id, map[string]any{"card_stability": stable, "password_state": password}); err != nil {
			return err
		}
	}
	if _, exists := changes["office_id"]; exists || create {
		office, err := requestedOffice(ctx, tx, actor, changes)
		if err != nil {
			return err
		}
		if err := setOffice(ctx, tx, id, office); err != nil {
			return err
		}
	}
	if device, present := changes["phone_device_id"]; present && identifier(device) != textField(old, "phone_device_id") {
		return inventory.AssignAccountDevice(ctx, tx, id, identifier(device))
	}
	return nil
}

type accountChangeSet struct {
	path, id         string
	before, nurture  map[string]any
	checked          map[string]any
	assignment       map[string]map[string]any
	assignmentTarget *string
}

func captureChanges(ctx context.Context, tx *sql.Tx, path, id string, before, changes map[string]any) (accountChangeSet, error) {
	set := accountChangeSet{path: path, id: id, before: before, checked: map[string]any{}}
	offices, _ := before["office_ids"].([]string)
	var err error
	if before != nil {
		set.nurture, err = readNurture(ctx, tx, id, offices)
		if err != nil {
			return set, err
		}
		version, err := recordwire.Version(before)
		if err != nil {
			return set, err
		}
		set.checked["account:"+id] = version
	}
	if path == patchPath {
		for _, entity := range relatedEntities[:2] {
			old, current := textField(before, entity.field), textField(changes, entity.field)
			if _, set := changes[entity.field]; !set {
				if _, hasSlot := changes["slot"]; entity.field != "computer_id" || !hasSlot {
					continue
				}
				current = old
			}
			for _, target := range []string{old, current} {
				if !hasReference(entity, target) {
					continue
				}
				key := entity.prefix + ":" + target
				if _, exists := set.checked[key]; exists {
					continue
				}
				version, err := referenceVersion(ctx, tx, entity, target)
				if err != nil {
					return set, err
				}
				set.checked[key] = version
			}
		}
	}
	if raw, exists := changes["phone_device_id"]; exists {
		target := identifier(raw)
		if err := set.captureAssignment(ctx, tx, target, textField(before, "phone_device_id")); err != nil {
			return set, err
		}
	}
	return set, nil
}

func referenceVersion(ctx context.Context, tx *sql.Tx, entity relatedEntity, id string) (any, error) {
	var raw []byte
	err := tx.QueryRowContext(ctx, `SELECT to_jsonb(r)::text FROM board_candidate.`+entity.table+` r WHERE id=$1`, id).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	record, err := decodeRow(raw)
	if err != nil {
		return nil, err
	}
	offices, err := referenceOffices(ctx, tx, entity, id)
	if err != nil {
		return nil, err
	}
	record["office_ids"] = offices
	return recordwire.Version(record)
}

func (set accountChangeSet) finish(ctx context.Context, tx *sql.Tx, actor session.User, day time.Time) (map[string]any, map[string]any, error) {
	next, err := requireAccount(ctx, tx, actor, set.id)
	if err != nil {
		return nil, nil, err
	}
	batchAction, group := accountwrite.HistoryGroup(ctx)
	if group == "" {
		group, err = accountwrite.NewGroup()
		if err != nil {
			return nil, nil, err
		}
	} else {
		set.path = batchAction
	}
	if err := set.recordChanges(ctx, tx, actor, next, day, group); err != nil {
		return nil, nil, err
	}
	target := ""
	if set.path == patchPath || set.path == editPath || set.path == createPath || set.path == statusPath {
		target = set.id
	}
	if batchAction == "" {
		if err := accountwrite.Audit(ctx, tx, actor, set.path, target); err != nil {
			return nil, nil, err
		}
	}
	return map[string]any{"ok": true, "id": set.id, "_record_versions": set.checked}, next, nil
}

func (set accountChangeSet) recordChanges(ctx context.Context, tx *sql.Tx, actor session.User, next map[string]any, day time.Time, group string) error {
	offices, _ := next["office_ids"].([]string)
	nurture, err := readNurture(ctx, tx, set.id, offices)
	if err != nil {
		return err
	}
	if !reflect.DeepEqual(set.before, next) {
		if err := syncNurture(ctx, tx, set.id, next, nurture, day); err != nil {
			return err
		}
		nurture, err = readNurture(ctx, tx, set.id, offices)
		if err != nil {
			return err
		}
		scope, err := accountHistoryScope(ctx, tx, set.before, next)
		if err != nil {
			return err
		}
		if len(scope) != 1 {
			scope = nil
			if err := enqueueScopedReload(ctx, tx, group); err != nil {
				return err
			}
		}
		if err := set.record(ctx, tx, actor, group, "account:"+set.id, set.before, next, scope); err != nil {
			return err
		}
	}
	if !reflect.DeepEqual(set.nurture, nurture) {
		scope := append([]string{}, offices...)
		if set.nurture != nil {
			previous, _ := set.nurture["office_ids"].([]string)
			if len(previous) == 0 || nurture != nil && len(offices) == 0 {
				scope = nil
			} else {
				scope = append(scope, previous...)
				slices.Sort(scope)
				scope = slices.Compact(scope)
			}
		}
		if len(scope) != 1 {
			scope = nil
		}
		if err := set.record(ctx, tx, actor, group, "nurture:"+set.id, set.nurture, nurture, scope); err != nil {
			return err
		}
	}
	return set.recordAssignment(ctx, tx, actor, group)
}

func enqueueScopedReload(ctx context.Context, tx *sql.Tx, group string) error {
	_, err := tx.ExecContext(ctx, `INSERT INTO board_candidate.operations_history
(group_id,at,actor_name,action,entity) VALUES($1,$2,'同步','reload','sync:reload')`, group, time.Now().Unix())
	return err
}

func (set accountChangeSet) record(ctx context.Context, tx *sql.Tx, actor session.User, group, key string, before, after map[string]any, scope []string) error {
	if err := accountwrite.InsertHistory(ctx, tx, actor, set.path, group, key, before, after, scope); err != nil {
		return err
	}
	if after == nil {
		set.checked[key] = nil
		return nil
	}
	version, err := recordwire.Version(after)
	if err == nil {
		set.checked[key] = version
	}
	return err
}
