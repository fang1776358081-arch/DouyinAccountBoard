package account

import (
	"context"
	"database/sql"
	"net/http"
	"slices"
	"strings"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/session"
)

const editPath, createPath = "/api/account", "/api/account/create"

func (h WriteHandler) Save(create bool) http.HandlerFunc {
	path, required := editPath, "保存账号必须带请求标识"
	if create {
		path, required = createPath, "新增账号必须带请求标识"
	}
	return h.Handle(path, required, func(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) error {
		_, err := authorizeSave(ctx, tx, actor, body, create)
		return err
	}, func(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
		return saveFull(ctx, tx, actor, body, create, true)
	})
}

// SaveAccount is the admitted transaction entry point for a validated import.
// The import service owns batch conflict checks and transaction completion.
func SaveAccount(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any, create bool) (string, error) {
	result, err := saveFull(ctx, tx, actor, body, create, false)
	if err != nil {
		return "", err
	}
	return textField(result, "id"), nil
}

func authorizeSave(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any, create bool) (map[string]any, error) {
	var old map[string]any
	permission := "account_manage"
	if create {
		permission = "account_create"
		if _, err := requestedOffice(ctx, tx, actor, body); err != nil {
			return nil, err
		}
	} else {
		var err error
		old, err = requireAccount(ctx, tx, actor, identifier(body["id"]))
		if err != nil {
			return nil, err
		}
	}
	if err := authorizeReferences(ctx, tx, actor, old, body); err != nil {
		return nil, err
	}
	if !actor.Has(permission) {
		return nil, accountwrite.Forbidden(permissionDenied)
	}
	return old, nil
}

func saveFull(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any, create, checkVersions bool) (map[string]any, error) {
	old, err := authorizeSave(ctx, tx, actor, body, create)
	if err != nil {
		return nil, err
	}
	id, path := identifier(body["id"]), editPath
	if create {
		path = createPath
		group, err := accountwrite.NewGroup()
		if err != nil {
			return nil, err
		}
		id = "account-" + group
	}
	set, err := captureChanges(ctx, tx, path, id, old, body)
	if err != nil {
		return nil, err
	}
	if checkVersions {
		if err := accountwrite.CheckVersions(body, set.checked); err != nil {
			return nil, err
		}
	}
	if create {
		officeGroup := ""
		if raw, found := body["office_group"]; found {
			var ok bool
			officeGroup, ok = raw.(string)
			if !ok || !slices.Contains([]string{"", "three"}, officeGroup) {
				return nil, accountwrite.Invalid("办公室不正确")
			}
		}
		_, err := tx.ExecContext(ctx, `INSERT INTO board_candidate.computers VALUES('unassigned','未分配电脑','','','','') ON CONFLICT(id) DO NOTHING`)
		if err != nil {
			return nil, err
		}
		var raw []byte
		err = tx.QueryRowContext(ctx, `INSERT INTO board_candidate.accounts(id,computer_id,slot,name,status,note,douyin_id,homepage,last_collected,office_group)
VALUES($1,'unassigned',NULL,'新账号','待确认','','','','',$2) RETURNING to_jsonb(accounts)::text`, id, officeGroup).Scan(&raw)
		if err != nil {
			return nil, err
		}
		old, err = decodeRow(raw)
		if err != nil {
			return nil, err
		}
	}
	for _, field := range []string{"name", "status", "computer_id", "slot"} {
		if _, found := body[field]; !found {
			return nil, accountwrite.Invalid("缺少字段：" + field)
		}
	}
	patch := map[string]any{"note": "", "homepage": "", "douyin_id": ""}
	for field := range patchPermissions {
		if value, ok := body[field]; ok {
			if field != "slot" && strings.ContainsRune(identifier(value), 0) {
				return nil, accountwrite.Invalid("字段格式不正确：" + field)
			}
			patch[field] = value
		}
	}
	for field, permission := range map[string]string{
		"status": "account_status", "auxiliary_state": "account_progress", "card_stability": "account_progress",
		"password_state": "account_progress", "phone_device_id": "device_manage",
	} {
		if value, exists := patch[field]; exists && !actor.Has(permission) && identifier(value) != identifier(old[field]) {
			return nil, accountwrite.Forbidden(permissionDenied)
		}
	}
	today := boardToday()
	values, err := buildAccountChanges(ctx, tx, actor, id, old, patch, today, true)
	if err != nil {
		return nil, err
	}
	// Imports may explicitly set the existing nurture date. Ordinary full
	// edits use the dedicated date action and never alter it directly.
	if started := identifier(body["nurture_started"]); !checkVersions && started != "" {
		day, err := isoDate(started)
		if err != nil {
			return nil, accountwrite.Invalid("养号开始日期格式不正确")
		}
		if day.After(today) {
			return nil, accountwrite.Invalid("养号开始日期不能在未来")
		}
		if (create || started != textField(old, "nurture_started")) && !actor.Has("account_status") {
			return nil, accountwrite.Forbidden(permissionDenied)
		}
		values["nurture_started"] = started
	}
	if err := updateAccount(ctx, tx, id, values); err != nil {
		return nil, err
	}
	if err := updateRelations(ctx, tx, actor, id, old, patch, create); err != nil {
		return nil, err
	}
	result, _, err := set.finish(ctx, tx, actor, today)
	return result, err
}
