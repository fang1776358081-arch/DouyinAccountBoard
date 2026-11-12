package tablesync

import (
	"context"
	"database/sql"
	"errors"
	"maps"
	"regexp"
	"strconv"
	"strings"
	"time"

	"douyinboard/internal/account"
	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

var previewPattern = regexp.MustCompile(`^[a-f0-9]{32}$`)

func validatePlan(ctx context.Context, tx *sql.Tx, actor session.User, day string, plan *changePlan) error {
	if plan.Action == "delete" {
		return nil
	}
	if plan.Action == "create" || !same(plan.BeforeAccount["status"], plan.AfterAccount["status"]) || !same(plan.BeforeAccount["registered_on"], plan.AfterAccount["registered_on"]) {
		if changed, err := changesNurture(ctx, tx, plan.AfterAccount, day); err != nil || changed {
			if err != nil {
				return err
			}
			return invalid("本行会影响养号或其他受保护资料，需单独人工核对")
		}
	}
	changes := item{}
	for _, key := range accountFields {
		if !same(plan.BeforeAccount[key], plan.AfterAccount[key]) {
			changes[key] = plan.AfterAccount[key]
		}
	}
	if len(changes) == 0 {
		return nil
	}
	when, err := time.Parse(time.DateOnly, day)
	if err != nil {
		return err
	}
	validate := func(old item) error {
		proposed, err := account.BuildTablePatch(ctx, tx, actor, plan.AccountID, old, changes, when)
		if err != nil {
			return err
		}
		for key, value := range proposed {
			if !same(plan.AfterAccount[key], value) {
				return invalid("现有账号校验会隐式改变大小号关系或其他非同步字段，整名账号待核对")
			}
		}
		return nil
	}
	if plan.Action != "create" {
		return validate(plan.BeforeAccount)
	}
	// Use database defaults, but accept only reviewed defaults in the plan.
	return savepoint(ctx, tx, func() error {
		if err := writePlan(ctx, tx, *plan); err != nil {
			return err
		}
		rows, err := jsonRows(ctx, tx, `SELECT to_jsonb(a) FROM board_candidate.accounts a WHERE a.id=$1`, plan.AccountID)
		if err != nil {
			return err
		}
		if len(rows) != 1 {
			return invalid("新增账号约束异常，保留待核对")
		}
		defaults := item{"work_count": nil, "last_collected": "", "collection_state": "", "collection_error": "", "last_attempted": "",
			"account_type": "待确认", "parent_account_id": nil, "auxiliary_state": "待确认", "lifecycle_note": "", "card_stability": "待确认",
			"password_state": "待确认", "phone_device_id": nil, "phone_device_source": "", "phone_device_note": "", "office_group": "", "nurture_started": ""}
		maps.Copy(defaults, plan.AfterAccount)
		if !same(defaults, rows[0]) {
			return invalid("新增账号出现计划外身份、状态或关系，保留待核对")
		}
		plan.AfterAccount = rows[0]
		return validate(rows[0])
	})
}

func changesNurture(ctx context.Context, tx *sql.Tx, next item, day string) (bool, error) {
	var completed, workflow, started string
	err := tx.QueryRowContext(ctx, `SELECT completed_on,workflow,started_on FROM board_candidate.nurture_records WHERE account_id=$1`, next["id"]).Scan(&completed, &workflow, &started)
	if errors.Is(err, sql.ErrNoRows) {
		start := text(next["nurture_started"])
		if start == "" {
			start = text(next["registered_on"])
		}
		recent := false
		if when, err := time.Parse(time.DateOnly, start); err == nil {
			current, _ := time.Parse(time.DateOnly, day)
			days := (current.Unix() - when.Unix()) / 86400
			recent = days >= 0 && days < 7
		}
		return next["status"] == "养号7天" || recent, nil
	}
	if err != nil {
		return false, err
	}
	status := text(next["status"])
	return completed == "" && ((workflow == "待开始" && started != "") ||
		(workflow == "运行" && status != "正常" && status != "待确认" && status != "养号7天")), nil
}

func writePlan(ctx context.Context, tx *sql.Tx, plan changePlan) error {
	id, after := plan.AccountID, plan.AfterAccount
	switch plan.Action {
	case "create":
		_, err := tx.ExecContext(ctx, `INSERT INTO board_candidate.accounts
			(id,computer_id,slot,name,status,note,douyin_id,homepage,last_collected,registered_on,registration_phone)
			VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)`, id, after["computer_id"], slotArgument(after["slot"]), after["name"], after["status"], after["note"], after["douyin_id"], after["homepage"], after["last_collected"], after["registered_on"], after["registration_phone"])
		if err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO board_candidate.account_offices(account_id,office_id) VALUES($1,$2)`, id, plan.Office); err != nil {
			return err
		}
	case "delete":
		for _, statement := range []string{
			`DELETE FROM board_candidate.table_sync_account_sources WHERE account_id=$1`,
			`DELETE FROM board_candidate.account_offices WHERE account_id=$1`,
			`DELETE FROM board_candidate.accounts WHERE id=$1`,
		} {
			if _, err := tx.ExecContext(ctx, statement, id); err != nil {
				return err
			}
		}
		return nil
	case "update":
		columns, args := make([]string, 0), []any{id}
		for _, column := range accountFields {
			if !same(plan.BeforeAccount[column], after[column]) {
				value := after[column]
				if column == "slot" {
					value = slotArgument(value)
				}
				args = append(args, value)
				columns = append(columns, column+"=$"+strconv.Itoa(len(args)))
			}
		}
		if len(columns) != 0 {
			if _, err := tx.ExecContext(ctx, `UPDATE board_candidate.accounts SET `+strings.Join(columns, ",")+` WHERE id=$1`, args...); err != nil {
				return err
			}
		}
	default:
		return invalid("未知的表格同步计划操作")
	}
	if same(plan.BeforeSource, plan.AfterSource) {
		return nil
	}
	if plan.AfterSource == nil {
		_, err := tx.ExecContext(ctx, `DELETE FROM board_candidate.table_sync_account_sources WHERE account_id=$1`, id)
		return err
	}
	source := plan.AfterSource
	revision, ok := intValue(source["revision"])
	if !ok || revision < 1 {
		return invalid("来源记录版本不正确")
	}
	_, err := tx.ExecContext(ctx, `INSERT INTO board_candidate.table_sync_account_sources(account_id,office_id,responsible,usage,violation,business_type,storefront,source_data,revision)
		VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT(account_id) DO UPDATE SET office_id=excluded.office_id,responsible=excluded.responsible,
		usage=excluded.usage,violation=excluded.violation,business_type=excluded.business_type,storefront=excluded.storefront,source_data=excluded.source_data,revision=excluded.revision`,
		id, plan.Office, source["responsible"], source["usage"], source["violation"], source["business_type"], source["storefront"], source["source_data"], revision)
	return err
}

func slotArgument(raw any) any {
	if slot, ok := intValue(raw); ok {
		return slot
	}
	return nil
}

func storePreview(ctx context.Context, tx *sql.Tx, actor session.User, before snapshot, payload preview) (item, error) {
	if len(payload.Plans) > 0 {
		err := savepoint(ctx, tx, func() error {
			for _, plan := range payload.Plans {
				if err := writePlan(ctx, tx, plan); err != nil {
					return err
				}
			}
			return verifyWrite(ctx, tx, before, payload.Plans, 0, 0)
		})
		if err != nil {
			if _, known := safeIssue(err); !known {
				return nil, err
			}
			const message = "联合验证存在资料约束或计划外业务变化，整批安全行保留待核对；未修改任何资料"
			for _, row := range payload.Rows {
				if row["selected"] == true {
					row["action"], row["selected"], row["blocked"], row["reason"] = "conflict", false, true, message
					row["issues"] = []item{issue("batch_validation_conflict", message)}
				}
			}
			payload.Plans = []changePlan{}
		}
	}
	if err := verifyWrite(ctx, tx, before, nil, 0, 0); err != nil {
		return nil, err
	}
	guard, err := recordwire.Version(before)
	if err != nil {
		return nil, err
	}
	payload.Guard, payload.Expires = guard, time.Now().Unix()+1800
	payload.Summary = summary(payload)
	encoded, err := recordwire.Encode(payload)
	if err != nil {
		return nil, err
	}
	if _, err := tx.ExecContext(ctx, `DELETE FROM board_candidate.operations_previews WHERE expires_at<$1`, time.Now().Unix()); err != nil {
		return nil, err
	}
	ident := nextID(false)
	_, err = tx.ExecContext(ctx, `INSERT INTO board_candidate.operations_previews(id,actor_id,expires_at,kind,payload) VALUES($1,$2,$3,$4,$5)`, ident, actor.ID, payload.Expires, previewKind, string(encoded))
	if err != nil {
		return nil, err
	}
	phrase := confirmation
	if payload.Mode == "rollback" {
		phrase = reverseConfirmation
	}
	return item{"preview_id": ident, "office_id": payload.Office, "mode": payload.Mode, "source": payload.Source, "rows": payload.Rows,
		"summary": payload.Summary, "expires_at": payload.Expires, "confirmation": phrase,
		"warnings": []string{"只使用真实抖音号精确匹配；空白不更新；电脑只指资料登记位置，实际发布安排不变。",
			"预览有效30分钟；相关资料变化须重新预览；未知结果只能核对原请求编号。"}}, nil
}

func summary(payload preview) item {
	handles, conflicts := map[string]bool{}, map[string]bool{}
	changed, unchanged, issues, skipped, updated, created := 0, int64(0), 0, 0, 0, 0
	for _, row := range payload.Rows {
		handle := text(row["douyin_id"])
		if handle != "" {
			handles[handle] = true
			if row["action"] == "conflict" {
				conflicts[handle] = true
			}
			if row["action"] == "skip" {
				skipped++
			}
		}
		if row["selected"] == true {
			changed += len(asRows(row["changes"]))
		}
		count, _ := intValue(row["unchanged_fields"])
		unchanged += count
		issues += len(asRows(row["issues"]))
	}
	for _, plan := range payload.Plans {
		if plan.Action == "create" {
			created++
		}
		if plan.Action == "update" {
			updated++
		}
	}
	sourceRows := int64(len(payload.Rows))
	if payload.Mode != "rollback" {
		sourceRows = 0
		for _, key := range []string{"accounts", "progress"} {
			part, _ := payload.Source[key].(item)
			count, _ := intValue(part["rows"])
			sourceRows += count
		}
	}
	return item{"source_rows": sourceRows, "preview_rows": len(payload.Rows), "unique_accounts": len(handles), "safe_accounts": len(payload.Plans),
		"updated_accounts": updated, "new_accounts": created, "changed_fields": changed, "unchanged_fields": unchanged,
		"conflict_accounts": len(conflicts), "conflict_items": issues, "skipped_accounts": skipped}
}

func asRows(value any) []item {
	if rows, ok := value.([]item); ok {
		return rows
	}
	values, _ := value.([]any)
	result := make([]item, 0, len(values))
	for _, value := range values {
		if row, ok := value.(item); ok {
			result = append(result, row)
		}
	}
	return result
}

func applyPreview(ctx context.Context, tx *sql.Tx, actor session.User, office, generation, day string, body item, reverse bool) (item, error) {
	if err := onlyFields(body, "preview_id", "selected_rows", "confirmation"); err != nil {
		return nil, err
	}
	revision, err := enabled(ctx, tx, office)
	if err != nil {
		return nil, err
	}
	id := text(body["preview_id"])
	if !previewPattern.MatchString(id) {
		return nil, invalid("预览编号不正确")
	}
	var raw []byte
	err = tx.QueryRowContext(ctx, `SELECT payload FROM board_candidate.operations_previews WHERE id=$1 AND actor_id=$2 AND expires_at>$3 AND kind=$4`, id, actor.ID, time.Now().Unix(), previewKind).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, invalid("预览已过期或不属于当前登录人员，请重新预览")
	}
	if err != nil {
		return nil, err
	}
	var payload preview
	if err := decodeStructured(raw, &payload); err != nil {
		return nil, err
	}
	if (payload.Mode == "rollback") != reverse {
		return nil, invalid("确认接口与预览类型不一致，未保存任何修改")
	}
	phrase := confirmation
	if reverse {
		phrase = reverseConfirmation
	}
	if body["confirmation"] != phrase {
		return nil, invalid("请明确输入“" + phrase + "”后确认")
	}
	selected, err := selectedRows(body["selected_rows"])
	if err != nil {
		return nil, err
	}
	plans := make([]changePlan, 0, len(selected))
	for _, plan := range payload.Plans {
		if selected[plan.RowKey] {
			plans = append(plans, plan)
		}
	}
	if len(plans) != len(selected) {
		return nil, invalid("不能选择冲突、跳过、无变化或不属于本预览的行")
	}
	before, err := readSnapshot(ctx, tx)
	if err != nil {
		return nil, err
	}
	guard, err := recordwire.Version(before)
	if err != nil {
		return nil, err
	}
	if payload.Office != office || payload.Day != day || payload.Generation != generation || payload.PilotRevision != revision || payload.Guard != guard {
		return nil, conflict("预览后资料、权限、日期或试点状态已变化，请重新预览；未保存任何修改")
	}
	if reverse && payload.Reverses != nil {
		if already, err := hasReversal(ctx, tx, *payload.Reverses); err != nil || already {
			if err == nil {
				err = conflict("此批次已有回退回执，请先核对原回退结果")
			}
			return nil, err
		}
	}
	for _, plan := range plans {
		if plan.Office != office || (plan.BeforeAccount != nil && before.Offices[plan.AccountID] != office) {
			return nil, conflict("账号归属已变化，整批未执行")
		}
		if err := writePlan(ctx, tx, plan); err != nil {
			if reason, known := safeIssue(err); known {
				return nil, conflict(reason)
			}
			return nil, err
		}
	}
	if err := verifyWrite(ctx, tx, before, plans, 0, 0); err != nil {
		return nil, err
	}
	return recordBatch(ctx, tx, actor, before, payload, id, selected, plans)
}

func selectedRows(raw any) (map[string]bool, error) {
	values, ok := raw.([]any)
	if !ok || len(values) > 5000 {
		return nil, invalid("请选择不重复的安全账号行")
	}
	selected := make(map[string]bool, len(values))
	for _, value := range values {
		key, ok := value.(string)
		if !ok || selected[key] {
			return nil, invalid("请选择不重复的安全账号行")
		}
		selected[key] = true
	}
	return selected, nil
}

func recordBatch(ctx context.Context, tx *sql.Tx, actor session.User, before snapshot, payload preview, previewID string, selected map[string]bool, plans []changePlan) (item, error) {
	batch, now := nextID(true), time.Now().Unix()
	changes := make([]item, 0)
	for _, plan := range plans {
		for _, diff := range planChanges(plan) {
			diff["account_id"], diff["douyin_id"], diff["source_rows"] = plan.AccountID, plan.Handle, plan.SourceRows
			changes = append(changes, diff)
		}
	}
	issues := make([]item, 0)
	for _, row := range payload.Rows {
		applied := selected[text(row["row_key"])]
		row["applied"], row["selected"] = applied, applied
		for _, problem := range asRows(row["issues"]) {
			item := maps.Clone(problem)
			item["id"], item["row_key"], item["douyin_id"], item["source_rows"], item["state"], item["note"] = nextID(true), row["row_key"], row["douyin_id"], row["source_rows"], "open", ""
			issues = append(issues, item)
		}
	}
	counts := maps.Clone(payload.Summary)
	counts["applied_accounts"], counts["changed_fields"], counts["excluded_safe_accounts"] = len(plans), len(changes), len(payload.Plans)-len(plans)
	verification := item{"only_planned_fields_changed": true, "other_offices_unchanged": true, "identity_preserved": true,
		"devices_preserved": true, "publishing_preserved": true, "nurture_preserved": true}
	receipt := item{"batch_id": batch, "office_id": payload.Office, "created_at": now, "kind": payload.Mode, "reverts_batch_id": payload.Reverses,
		"actor": item{"id": actor.ID, "name": actor.Name}, "source": payload.Source, "summary": counts, "rows": payload.Rows,
		"changes": changes, "issues": issues, "verification": verification}
	encoded, err := recordwire.Encode(receipt)
	if err != nil {
		return nil, err
	}
	plansJSON, err := recordwire.Encode(plans)
	if err != nil {
		return nil, err
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO board_candidate.table_sync_batches(id,office_id,actor_id,created_at,kind,reverts_batch_id,receipt_json,plan_json)
		VALUES($1,$2,$3,$4,$5,$6,$7,$8)`, batch, payload.Office, actor.ID, now, payload.Mode, payload.Reverses, string(encoded), string(plansJSON))
	if err != nil {
		return nil, err
	}
	for _, issue := range issues {
		raw, err := recordwire.Encode(issue)
		if err != nil {
			return nil, err
		}
		_, err = tx.ExecContext(ctx, `INSERT INTO board_candidate.table_sync_review_items(id,batch_id,office_id,state,payload_json,note,updated_at)
			VALUES($1,$2,$3,'open',$4,'',$5)`, issue["id"], batch, payload.Office, string(raw), now)
		if err != nil {
			return nil, err
		}
	}
	var historyDelta int64
	for _, plan := range plans {
		written, err := writePlanHistory(ctx, tx, actor, batch, plan, now)
		if err != nil {
			return nil, err
		}
		historyDelta += written
	}
	if err := writeEvent(ctx, tx, actor, payload.Office, "batch", batch, nil, item{"kind": payload.Mode, "summary": counts}); err != nil {
		return nil, err
	}
	if _, err := tx.ExecContext(ctx, `DELETE FROM board_candidate.operations_previews WHERE id=$1`, previewID); err != nil {
		return nil, err
	}
	if err := verifyWrite(ctx, tx, before, plans, historyDelta, 1); err != nil {
		return nil, err
	}
	return item{"ok": true, "batch_id": batch, "office_id": payload.Office, "summary": counts, "receipt": receipt, "verification": verification}, nil
}

func writePlanHistory(ctx context.Context, tx *sql.Tx, actor session.User, batch string, plan changePlan, now int64) (int64, error) {
	offices, err := recordwire.Encode([]string{plan.Office})
	if err != nil {
		return 0, err
	}
	insert := func(entity string, before, after item) error {
		values := make([]any, 0, 2)
		for _, value := range []item{before, after} {
			var raw any
			if value != nil {
				body, err := recordwire.Encode(value)
				if err != nil {
					return err
				}
				raw = string(body)
			}
			values = append(values, raw)
		}
		_, err := tx.ExecContext(ctx, `INSERT INTO board_candidate.operations_history(group_id,at,actor_id,actor_name,action,entity,before_json,after_json,scope_offices)
			VALUES($1,$2,$3,$4,'/api/table-sync/apply',$5,$6,$7,$8)`, batch, now, actor.ID, actor.Name, entity, values[0], values[1], string(offices))
		return err
	}
	var count int64
	if !same(plan.BeforeAccount, plan.AfterAccount) {
		before, after := maps.Clone(plan.BeforeAccount), maps.Clone(plan.AfterAccount)
		if before != nil {
			before["office_ids"] = []string{plan.Office}
		}
		if after != nil {
			after["office_ids"] = []string{plan.Office}
		}
		if err := insert("account:"+plan.AccountID, before, after); err != nil {
			return 0, err
		}
		count++
	}
	if !same(plan.BeforeSource, plan.AfterSource) {
		if err := insert("account-source:"+plan.AccountID, nil, item{"account_id": plan.AccountID, "office_ids": []string{plan.Office}, "revision": plan.AfterSource["revision"]}); err != nil {
			return 0, err
		}
		count++
	}
	return count, nil
}

func hasReversal(ctx context.Context, tx *sql.Tx, batch string) (bool, error) {
	var exists bool
	err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM board_candidate.table_sync_batches WHERE reverts_batch_id=$1)`, batch).Scan(&exists)
	return exists, err
}
