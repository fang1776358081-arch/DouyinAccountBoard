package account

import (
	"context"
	"database/sql"
	"errors"
	"net/http"
	"reflect"
	"slices"
	"strings"
	"time"
	"unicode/utf8"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

const batchPath = "/api/batch-edit"

var batchFields = map[string][]string{
	"accounts":     {"status", "card_stability", "password_state"},
	"cards":        {"owner", "ownership", "keeper", "code_contact"},
	"devices":      {"state", "owner", "location"},
	"daily":        {"published", "date"},
	"verification": {"verification_state", "verification_owner", "verification_note"},
}

func (h WriteHandler) Batch() http.HandlerFunc {
	return h.Handle(batchPath, "批量修改业务资料必须带请求标识", AuthorizeBatch, ApplyBatch)
}

func batchPayload(body map[string]any) (string, []string, map[string]any, error) {
	kind := identifier(body["kind"])
	fields, supported := batchFields[kind]
	if !supported {
		return "", nil, nil, accountwrite.Invalid("批量修改类型不正确")
	}
	rawIDs, ok := body["ids"].([]any)
	if !ok || len(rawIDs) == 0 || len(rawIDs) > 2000 {
		return "", nil, nil, accountwrite.Invalid("请选择1至2000条不重复的记录")
	}
	ids := make([]string, 0, len(rawIDs))
	seen := map[string]bool{}
	for _, rawID := range rawIDs {
		id, ok := rawID.(string)
		if !ok || id == "" || strings.ContainsRune(id, 0) || seen[id] {
			return "", nil, nil, accountwrite.Invalid("请选择1至2000条不重复的记录")
		}
		ids = append(ids, id)
		seen[id] = true
	}
	patch, ok := body["patch"].(map[string]any)
	if !ok || len(patch) == 0 {
		return "", nil, nil, accountwrite.Invalid("只能批量修改页面列出的字段")
	}
	for key, value := range patch {
		if !slices.Contains(fields, key) {
			return "", nil, nil, accountwrite.Invalid("只能批量修改页面列出的字段")
		}
		if key != "published" {
			if text, ok := value.(string); !ok || strings.ContainsRune(text, 0) {
				return "", nil, nil, accountwrite.Invalid("字段应为文本")
			}
		}
	}
	if kind == "daily" {
		if _, ok := patch["published"].(bool); !ok || len(patch) != 2 {
			return "", nil, nil, accountwrite.Invalid("请选择发布日期及已发/未发")
		}
	}
	return kind, ids, patch, nil
}

func AuthorizeBatch(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) error {
	kind, ids, patch, err := batchPayload(body)
	if err != nil {
		return err
	}
	if kind == "cards" || kind == "devices" {
		return authorizeInventoryBatch(ctx, tx, actor, kind, ids, patch)
	}
	for _, id := range ids {
		if _, err := requireAccount(ctx, tx, actor, id); err != nil {
			return err
		}
	}
	for field := range patch {
		permission := "account_progress"
		switch {
		case kind == "daily":
			permission = "publish"
		case kind == "verification":
			permission = "nurture_manage"
		case field == "status":
			permission = "account_status"
		}
		if !actor.Has(permission) {
			return accountwrite.Forbidden(permissionDenied)
		}
	}
	return nil
}

func ApplyBatch(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	kind, ids, patch, err := batchPayload(body)
	if err != nil {
		return nil, err
	}
	if kind == "daily" {
		return applyDailyBatch(ctx, tx, actor, ids, patch, body)
	}
	if kind == "cards" || kind == "devices" {
		return applyInventoryBatch(ctx, tx, actor, kind, ids, patch, body)
	}
	return applyAccountBatch(ctx, tx, actor, batchPath, ids, patch, body, kind == "verification")
}

func applyAccountBatch(ctx context.Context, tx *sql.Tx, actor session.User, path string, ids []string, patch, body map[string]any, verification bool) (map[string]any, error) {
	sets := make([]accountChangeSet, 0, len(ids))
	for _, id := range ids {
		old, err := requireAccount(ctx, tx, actor, id)
		if err != nil {
			return nil, err
		}
		set, err := captureChanges(ctx, tx, path, id, old, nil)
		if err != nil {
			return nil, err
		}
		if verification {
			if err := addVersion(set.checked, "nurture:"+id, set.nurture); err != nil {
				return nil, err
			}
		}
		if err := accountwrite.CheckVersions(body, set.checked); err != nil {
			return nil, err
		}
		sets = append(sets, set)
	}
	group, err := accountwrite.NewGroup()
	if err != nil {
		return nil, err
	}
	result := map[string]any{}
	today := boardToday()
	for _, set := range sets {
		if verification {
			if err := applyVerification(ctx, tx, set.id, set.nurture, patch, today); err != nil {
				return nil, err
			}
		} else {
			if status, ok := patch["status"]; ok {
				if err := changeStatus(ctx, tx, set.id, identifier(status)); err != nil {
					return nil, err
				}
			}
			if err := updateRelations(ctx, tx, actor, set.id, set.before, patch, false); err != nil {
				return nil, err
			}
		}
		next, err := requireAccount(ctx, tx, actor, set.id)
		if err != nil {
			return nil, err
		}
		if err := set.recordChanges(ctx, tx, actor, next, today, group); err != nil {
			return nil, err
		}
		for key, value := range set.checked {
			result[key] = value
		}
	}
	if err := accountwrite.Audit(ctx, tx, actor, path, ""); err != nil {
		return nil, err
	}
	body = map[string]any{"ok": true, "_record_versions": result}
	if path == batchPath {
		body["count"] = len(ids)
	}
	return body, nil
}

func addVersion(versions map[string]any, key string, record map[string]any) error {
	if record == nil {
		versions[key] = nil
		return nil
	}
	version, err := recordwire.Version(record)
	if err == nil {
		versions[key] = version
	}
	return err
}

func applyVerification(ctx context.Context, tx *sql.Tx, id string, prior, patch map[string]any, today time.Time) error {
	if prior == nil {
		if _, err := tx.ExecContext(ctx, `INSERT INTO board_candidate.nurture_records(account_id) VALUES($1)`, id); err != nil {
			return err
		}
		prior = map[string]any{"verification_state": "待检查"}
	}
	value := func(field string) string {
		if next, set := patch[field]; set {
			return identifier(next)
		}
		return textField(prior, field)
	}
	state := value("verification_state")
	if !slices.Contains([]string{"待检查", "无需核对，可使用", "核对处理中", "核对已通过"}, state) {
		return accountwrite.Invalid("核对情况不正确")
	}
	owner, note := strip(value("verification_owner")), strip(value("verification_note"))
	if utf8.RuneCountInString(owner) > 100 || utf8.RuneCountInString(note) > 2000 {
		return accountwrite.Invalid("负责人最多100字，处理备注最多2000字")
	}
	if state != "无需核对，可使用" && state != "核对已通过" && textField(prior, "completed_on") == "" && textField(prior, "workflow") == "运行" {
		if err := pauseNurture(ctx, tx, id, prior, today); err != nil {
			return err
		}
	}
	_, err := tx.ExecContext(ctx, `UPDATE board_candidate.nurture_records
SET verification_state=$1,verification_owner=$2,verification_note=$3,verification_updated_on=$4,revision=revision+1 WHERE account_id=$5`,
		state, owner, note, today.Format(time.DateOnly), id)
	return err
}

func applyDailyBatch(ctx context.Context, tx *sql.Tx, actor session.User, ids []string, patch, body map[string]any) (map[string]any, error) {
	day := textField(patch, "date")
	if _, err := isoDate(day); err != nil {
		return nil, accountwrite.Invalid("日期格式不正确")
	}
	type dailyChange struct{ before, after map[string]any }
	changes := map[string]dailyChange{}
	versions := map[string]any{}
	for _, id := range ids {
		account, err := requireAccount(ctx, tx, actor, id)
		if err != nil {
			return nil, err
		}
		if err := addVersion(versions, "account:"+id, account); err != nil {
			return nil, err
		}
		var before map[string]any
		var published int64
		err = tx.QueryRowContext(ctx, `SELECT published FROM board_candidate.daily WHERE account_id=$1 AND day=$2`, id, day).Scan(&published)
		if err == nil {
			before = map[string]any{"account_id": id, "day": day, "published": published, "office_ids": account["office_ids"]}
		} else if !errors.Is(err, sql.ErrNoRows) {
			return nil, err
		}
		after := map[string]any{"account_id": id, "day": day, "published": int64(0), "office_ids": account["office_ids"]}
		if patch["published"] == true {
			after["published"] = int64(1)
		}
		key := "daily:" + id + ":" + day
		if err := addVersion(versions, key, before); err != nil {
			return nil, err
		}
		changes[key] = dailyChange{before, after}
	}
	if err := accountwrite.CheckVersions(body, versions); err != nil {
		return nil, err
	}
	group, err := accountwrite.NewGroup()
	if err != nil {
		return nil, err
	}
	for key, change := range changes {
		if reflect.DeepEqual(change.before, change.after) {
			continue
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO board_candidate.daily(account_id,day,published) VALUES($1,$2,$3)
ON CONFLICT(account_id,day) DO UPDATE SET published=EXCLUDED.published`, change.after["account_id"], day, change.after["published"]); err != nil {
			return nil, err
		}
		offices, _ := change.after["office_ids"].([]string)
		if err := accountwrite.InsertHistory(ctx, tx, actor, batchPath, group, key, change.before, change.after, offices); err != nil {
			return nil, err
		}
		if err := addVersion(versions, key, change.after); err != nil {
			return nil, err
		}
	}
	if err := accountwrite.Audit(ctx, tx, actor, batchPath, ""); err != nil {
		return nil, err
	}
	return map[string]any{"ok": true, "count": len(ids), "_record_versions": versions}, nil
}
