package tablesync

import (
	"context"
	"database/sql"
	"errors"
	"maps"
	"strconv"
	"strings"

	"douyinboard/internal/session"
)

func reversePreview(ctx context.Context, tx *sql.Tx, actor session.User, office, generation, day string, body item) (item, error) {
	if err := onlyFields(body, "batch_id"); err != nil {
		return nil, err
	}
	revision, err := enabled(ctx, tx, office)
	if err != nil {
		return nil, err
	}
	id := text(body["batch_id"])
	var kind string
	var raw []byte
	err = tx.QueryRowContext(ctx, `SELECT kind,plan_json FROM board_candidate.table_sync_batches WHERE id=$1 AND office_id=$2`, id, office).Scan(&kind, &raw)
	if errors.Is(err, sql.ErrNoRows) || kind == "rollback" {
		return nil, invalid("请选择可回退的原始同步批次")
	}
	if err != nil {
		return nil, err
	}
	if already, err := hasReversal(ctx, tx, id); err != nil || already {
		if err == nil {
			err = conflict("此批次已有回退回执，不可重复回退")
		}
		return nil, err
	}
	var original []changePlan
	if err := decodeStructured(raw, &original); err != nil {
		return nil, err
	}
	if len(original) == 0 {
		return nil, invalid("该批次仅记录核对结果，没有业务变更需要回退")
	}
	before, err := readSnapshot(ctx, tx)
	if err != nil {
		return nil, err
	}
	plans, rows := make([]changePlan, 0), make([]item, 0)
	for index, old := range original {
		key := "rollback:" + strconv.Itoa(index)
		current := before.Accounts[old.AccountID]
		entry := pendingRow(key, old.Handle, text(old.BeforeAccount["name"]), old.SourceRows)
		entry["account_id"], entry["current_name"] = old.AccountID, text(current["name"])
		plan, err := proposeReversal(ctx, tx, actor, office, day, key, old, current, before)
		if err != nil {
			reason, known := safeIssue(err)
			if !known {
				return nil, err
			}
			entry["reason"], entry["issues"] = reason, []item{issue("rollback_conflict", reason)}
		} else {
			entry["action"], entry["selected"], entry["blocked"], entry["changes"] = "update", true, false, planChanges(plan)
			entry["reason"] = "仅恢复仍等于本批写入值的字段；保留后续无关修改"
			if plan.Action == "delete" {
				entry["reason"] = "删除尚无后续修改或依赖的本批新增账号"
			}
			plans = append(plans, plan)
		}
		rows = append(rows, entry)
	}
	return storePreview(ctx, tx, actor, before, preview{Office: office, Mode: "rollback", Day: day, Generation: generation, PilotRevision: revision,
		Source: item{"reverts_batch_id": id}, Rows: rows, Plans: plans, Reverses: &id})
}

func proposeReversal(ctx context.Context, tx *sql.Tx, actor session.User, office, day, key string, old changePlan, current item, before snapshot) (changePlan, error) {
	if current == nil || before.Offices[old.AccountID] != office || current["douyin_id"] != old.Handle {
		return changePlan{}, invalid("账号已不存在、身份或办公室变化，不能回退")
	}
	if !same(before.Sources[old.AccountID], old.AfterSource) {
		return changePlan{}, invalid("来源字段已被后续修改，不能覆盖")
	}
	plan := changePlan{key, old.AccountID, office, old.Handle, old.SourceRows, "update", current, maps.Clone(current), before.Sources[old.AccountID], old.BeforeSource}
	if old.Action == "create" {
		dependent, err := hasDependencies(ctx, tx, old.AccountID)
		if err != nil {
			return changePlan{}, err
		}
		if !same(current, old.AfterAccount) || dependent {
			return changePlan{}, invalid("新增账号已有后续修改或依赖，保留账号，不执行删除")
		}
		plan.Action, plan.AfterAccount = "delete", nil
	} else if old.Action == "update" {
		for _, field := range accountFields {
			if same(old.AfterAccount[field], old.BeforeAccount[field]) {
				continue
			}
			if !same(current[field], old.AfterAccount[field]) {
				return changePlan{}, invalid("拟回退字段已被后续人工修改，不能覆盖")
			}
			plan.AfterAccount[field] = old.BeforeAccount[field]
		}
	} else {
		return changePlan{}, invalid("批次包含无法回退的原始操作")
	}
	return plan, validatePlan(ctx, tx, actor, day, &plan)
}

func hasDependencies(ctx context.Context, tx *sql.Tx, id string) (bool, error) {
	var found bool
	err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM board_candidate.accounts WHERE parent_account_id=$1)`, id).Scan(&found)
	if err != nil || found {
		return found, err
	}
	tables, err := jsonRows(ctx, tx, `SELECT jsonb_build_object('name',table_name) FROM information_schema.columns
		WHERE table_schema='board_candidate' AND column_name='account_id'`)
	if err != nil {
		return false, err
	}
	for _, row := range tables {
		table := text(row["name"])
		if !sqlName.MatchString(table) || strings.HasPrefix(table, "table_sync_") || table == "account_offices" {
			continue
		}
		query := `SELECT EXISTS(SELECT 1 FROM board_candidate.` + table + ` WHERE account_id=$1)`
		if err := tx.QueryRowContext(ctx, query, id).Scan(&found); err != nil || found {
			return found, err
		}
	}
	return false, nil
}
