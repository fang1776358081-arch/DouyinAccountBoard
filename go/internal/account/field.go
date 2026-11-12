package account

import (
	"context"
	"database/sql"
	"net/http"
	"slices"
	"strings"
	"time"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/inventory"
	"douyinboard/internal/session"
)

const statusPath = "/api/account/status"
const bulkPath = "/api/bulk-status"
const readinessPath = "/api/account/card-readiness"
const phonePath = "/api/account/phone-device"
const lifecyclePath = "/api/lifecycle-status"
const nurtureDatePath = "/api/account/nurture"

type fieldRoute struct{ permission, required string }

var fieldRoutes = map[string]fieldRoute{
	statusPath:      {"account_status", "修改账号状态必须带请求标识"},
	bulkPath:        {"account_status", "批量修改账号状态必须带请求标识"},
	readinessPath:   {"account_progress", "修改稳定或改密记录必须带请求标识"},
	phonePath:       {"device_manage", "调整登录手机必须带请求标识"},
	lifecyclePath:   {"account_progress", "修改小号注册进度必须带请求标识"},
	nurtureDatePath: {"account_status", "修改养号开始日期必须带请求标识"},
}

func (h WriteHandler) Field(path string) http.HandlerFunc {
	route := fieldRoutes[path]
	return h.Handle(path, route.required, func(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) error {
		old, err := requireAccount(ctx, tx, actor, identifier(body["id"]))
		if err != nil {
			return err
		}
		if path == phonePath {
			if _, exists := body["phone_device_id"]; !exists {
				return accountwrite.Invalid("请选择手机设备或明确移除关联")
			}
			if err := authorizeReferences(ctx, tx, actor, old, map[string]any{"phone_device_id": body["phone_device_id"]}); err != nil {
				return err
			}
		}
		if route.permission == "" || !actor.Has(route.permission) {
			return accountwrite.Forbidden(permissionDenied)
		}
		return nil
	}, func(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
		id := identifier(body["id"])
		old, err := requireAccount(ctx, tx, actor, id)
		if err != nil {
			return nil, err
		}
		var relations map[string]any
		if path == phonePath {
			relations = map[string]any{"phone_device_id": body["phone_device_id"]}
		}
		set, err := captureChanges(ctx, tx, path, id, old, relations)
		if err != nil {
			return nil, err
		}
		if err := accountwrite.CheckVersions(body, set.checked); err != nil {
			return nil, err
		}
		day := boardToday()
		if err := applyField(ctx, tx, actor, path, id, old, body, day); err != nil {
			return nil, err
		}
		result, _, err := set.finish(ctx, tx, actor, day)
		if path != statusPath {
			delete(result, "id")
		}
		return result, err
	})
}

func applyField(ctx context.Context, tx *sql.Tx, actor session.User, path, id string, old, body map[string]any, today time.Time) error {
	switch path {
	case statusPath:
		return changeStatus(ctx, tx, id, identifier(body["status"]))
	case readinessPath:
		changes := map[string]any{}
		for _, key := range []string{"card_stability", "password_state"} {
			changes[key] = old[key]
			if value, found := body[key]; found {
				changes[key] = value
			}
		}
		return updateRelations(ctx, tx, actor, id, old, changes, false)
	case phonePath:
		raw := body["phone_device_id"]
		deviceID, ok := raw.(string)
		if raw != nil && (!ok || strings.ContainsRune(deviceID, 0)) {
			return accountwrite.Invalid("请选择已有手机设备，或先新增手机")
		}
		return inventory.AssignAccountDevice(ctx, tx, id, deviceID)
	case lifecyclePath:
		if old["account_type"] != "大号" {
			return accountwrite.Invalid("请先在详情中确认账号类型为大号")
		}
		values, err := validateLifecycle(ctx, tx, actor, id, map[string]any{"auxiliary_state": body["auxiliary_state"]}, old, today)
		if err != nil {
			return err
		}
		return updateAccount(ctx, tx, id, map[string]any{"auxiliary_state": values["auxiliary_state"]})
	case nurtureDatePath:
		started := identifier(body["started"])
		if started != "" {
			day, err := isoDate(started)
			if err != nil {
				return accountwrite.Invalid("养号开始日期格式不正确")
			}
			if day.After(today) {
				return accountwrite.Invalid("养号开始日期不能在未来")
			}
		}
		return updateAccount(ctx, tx, id, map[string]any{"nurture_started": started})
	default:
		return accountwrite.Invalid("接口未开放")
	}
}

func changeStatus(ctx context.Context, tx *sql.Tx, id, status string) error {
	if !slices.Contains(accountStates, status) {
		return accountwrite.Invalid("账号状态不正确")
	}
	_, err := tx.ExecContext(ctx, `UPDATE board_candidate.accounts
SET nurture_started=CASE WHEN $2='养号7天' AND status<>'养号7天' THEN '' ELSE nurture_started END,status=$2 WHERE id=$1`, id, status)
	return err
}

func bulkIDs(body map[string]any) ([]string, error) {
	values, ok := body["ids"].([]any)
	if !ok || len(values) == 0 {
		return nil, accountwrite.Invalid("请选择账号及状态")
	}
	result := make([]string, 0, len(values))
	seen := map[string]bool{}
	for _, value := range values {
		id := identifier(value)
		if !seen[id] {
			result = append(result, id)
			seen[id] = true
		}
	}
	return result, nil
}

func (h WriteHandler) BulkStatus() http.HandlerFunc {
	return h.Handle(bulkPath, fieldRoutes[bulkPath].required, func(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) error {
		ids, err := bulkIDs(body)
		if err != nil {
			return err
		}
		for _, id := range ids {
			if _, err := requireAccount(ctx, tx, actor, id); err != nil {
				return err
			}
		}
		if !actor.Has("account_status") {
			return accountwrite.Forbidden(permissionDenied)
		}
		return nil
	}, func(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
		ids, err := bulkIDs(body)
		if err != nil {
			return nil, err
		}
		status := identifier(body["status"])
		if !slices.Contains(accountStates, status) {
			return nil, accountwrite.Invalid("请选择账号及状态")
		}
		return applyAccountBatch(ctx, tx, actor, bulkPath, ids, map[string]any{"status": status}, body, false)
	})
}

// PermitReceipt applies the current permission and account scope to the fields
// stored in a response from an account mutation.
func PermitReceipt(ctx context.Context, tx *sql.Tx, actor session.User, action string, body map[string]any) error {
	permitted := false
	switch action {
	case patchPath:
		permitted = actor.Has("account_manage") || actor.Has("account_status") || actor.Has("account_progress") || actor.Has("device_manage")
	case createPath:
		permitted = actor.Has("account_create")
	case editPath:
		permitted = actor.Has("account_manage")
	case batchPath:
		for _, permission := range []string{"card_manage", "card_tasks", "account_status", "account_progress", "device_manage", "nurture_manage", "publish"} {
			permitted = permitted || actor.Has(permission)
		}
	default:
		route, found := fieldRoutes[action]
		permitted = found && actor.Has(route.permission)
		if name, nurture := strings.CutPrefix(action, NurturePathPrefix); nurture {
			_, found := nurtureRequests[name]
			permitted = found && actor.Has("nurture_manage")
		}
	}
	if !permitted {
		return accountwrite.Forbidden(permissionDenied)
	}
	if account, found := body["account"].(map[string]any); found {
		_, err := requireAccount(ctx, tx, actor, identifier(account["id"]))
		return err
	}
	return nil
}
