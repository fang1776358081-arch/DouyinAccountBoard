package account

import (
	"context"
	"database/sql"
	"maps"
	"strconv"
	"strings"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/inventory"
	"douyinboard/internal/session"
)

func batchCardID(id string) (int64, error) {
	number, err := strconv.ParseInt(id, 10, 64)
	if err != nil || number < 0 || strconv.FormatInt(number, 10) != id {
		return 0, accountwrite.Invalid("手机卡记录编号不正确")
	}
	return number, nil
}

func authorizeInventoryBatch(ctx context.Context, tx *sql.Tx, actor session.User, kind string, ids []string, patch map[string]any) error {
	permission := "device_manage"
	if kind == "cards" {
		permission = "card_manage"
	}
	if !actor.Has(permission) {
		return accountwrite.Forbidden(permissionDenied)
	}
	for _, id := range ids {
		if kind == "cards" {
			number, err := batchCardID(id)
			if err != nil {
				return err
			}
			candidate := maps.Clone(patch)
			candidate["id"] = number
			if err := inventory.AuthorizeCard(ctx, tx, actor, candidate); err != nil {
				return err
			}
		} else if err := inventory.AuthorizeDevice(ctx, tx, actor, map[string]any{"id": id}, false); err != nil {
			return err
		}
	}
	return nil
}

func applyInventoryBatch(ctx context.Context, tx *sql.Tx, actor session.User, kind string, ids []string, patch, body map[string]any) (map[string]any, error) {
	type plan struct {
		payload  map[string]any
		versions map[string]any
	}
	plans := make([]plan, 0, len(ids))
	_, changingState := patch["state"]
	for _, id := range ids {
		versions := map[string]any{}
		var payload map[string]any
		if kind == "cards" {
			number, err := batchCardID(id)
			if err != nil {
				return nil, err
			}
			payload, err = inventory.CardRecord(ctx, tx, number)
			if err != nil {
				return nil, err
			}
			if payload == nil {
				return nil, accountwrite.Forbidden(noAccess)
			}
			if err := addVersion(versions, "card:"+id, payload); err != nil {
				return nil, err
			}
			payload["id"] = number
		} else {
			var err error
			payload, err = inventory.DeviceRecord(ctx, tx, id)
			if err != nil {
				return nil, err
			}
			if payload == nil {
				return nil, accountwrite.Forbidden(noAccess)
			}
			versions, err = inventory.DeviceVersions(ctx, tx, id)
			if err != nil {
				return nil, err
			}
			if payload["state_source"] == "auto" {
				payload["state"] = "auto"
			}
		}
		required := versions
		if kind == "devices" && !changingState {
			key := "device:" + id
			required = map[string]any{key: versions[key]}
		}
		if err := accountwrite.CheckVersions(body, required); err != nil {
			return nil, err
		}
		delete(payload, "office_ids")
		maps.Copy(payload, patch)
		payload["_expected_versions"] = versions
		plans = append(plans, plan{payload, versions})
	}
	group, err := accountwrite.NewGroup()
	if err != nil {
		return nil, err
	}
	writeCtx := accountwrite.WithHistoryGroup(ctx, batchPath, group)
	versions := map[string]any{}
	for _, plan := range plans {
		var result map[string]any
		var err error
		if kind == "cards" {
			result, err = inventory.SaveCard(writeCtx, tx, actor, plan.payload)
		} else {
			result, err = inventory.SaveDevice(writeCtx, tx, actor, plan.payload, false)
		}
		if err != nil {
			return nil, err
		}
		delta, _ := result["_record_versions"].(map[string]any)
		for key, value := range delta {
			if kind != "devices" || changingState || !strings.HasPrefix(key, "unit:") || value != plan.versions[key] {
				versions[key] = value
			}
		}
	}
	if err := accountwrite.Audit(ctx, tx, actor, batchPath, ""); err != nil {
		return nil, err
	}
	return map[string]any{"ok": true, "count": len(ids), "_record_versions": versions}, nil
}
