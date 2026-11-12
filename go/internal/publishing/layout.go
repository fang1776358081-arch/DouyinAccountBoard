// Package publishing manages links and moves in the existing release matrix.
package publishing

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"slices"
	"strconv"
	"strings"
	"time"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

const LinkPath = "/api/publishing/link"
const AssignmentPath = "/api/publishing/assignment"

type coordinate struct {
	batch, computer string
	slot            int64
}

func (k coordinate) key() string {
	return "layout:" + k.batch + ":" + k.computer + ":" + strconv.FormatInt(k.slot, 10)
}

func text(value any) string {
	s, _ := value.(string)
	return s
}

func number(value any) (int64, bool) {
	n, ok := value.(json.Number)
	if !ok {
		return 0, false
	}
	v, err := n.Int64()
	return v, err == nil
}

func scope(ctx context.Context, tx *sql.Tx, table string, id string) (string, error) {
	var query string
	switch table {
	case "computer":
		query = "SELECT office_id FROM computer_offices WHERE computer_id=$1"
	case "account":
		query = "SELECT office_id FROM account_offices WHERE account_id=$1"
	}
	var office string
	err := tx.QueryRowContext(ctx, query, id).Scan(&office)
	if errors.Is(err, sql.ErrNoRows) {
		return "", nil
	}
	return office, err
}

func requireSameOffice(ctx context.Context, tx *sql.Tx, accountID, computerID string) error {
	accountOffice, err := scope(ctx, tx, "account", accountID)
	if err != nil {
		return err
	}
	computerOffice, err := scope(ctx, tx, "computer", computerID)
	if err != nil {
		return err
	}
	if accountOffice == "" || accountOffice != computerOffice {
		return accountwrite.Invalid("账号与发布电脑必须明确属于同一个办公室")
	}
	return nil
}

func Authorize(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) error {
	if !actor.Has("publishing_manage") {
		return accountwrite.Forbidden("当前账号没有此操作权限")
	}
	if actor.Global() {
		return nil
	}
	require := func(kind, id string) error {
		if id == "" {
			return nil
		}
		office, err := scope(ctx, tx, kind, id)
		if err == nil && (office == "" || !slices.Contains(actor.OfficeIDs, office)) {
			return accountwrite.Forbidden("无权访问此资料")
		}
		return err
	}
	for _, key := range []string{"computer_id", "target_computer"} {
		if err := require("computer", text(body[key])); err != nil {
			return err
		}
	}
	if err := require("account", text(body["account_id"])); err != nil {
		return err
	}
	slot, ok := number(body["slot"])
	if !ok {
		return nil
	}
	var account sql.NullString
	err := tx.QueryRowContext(ctx, `SELECT account_id FROM publishing_layout
		WHERE batch=$1 AND computer_id=$2 AND slot=$3`, text(body["batch"]), text(body["computer_id"]), slot).Scan(&account)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return err
	}
	return require("account", account.String)
}

func PermitReceipt(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) error {
	if !actor.Has("publishing_manage") {
		return accountwrite.Forbidden("当前账号没有此操作权限")
	}
	if actor.Global() {
		return nil
	}
	for _, key := range []string{"account_id", "previous_account_id"} {
		id := text(body[key])
		if id == "" {
			continue
		}
		office, err := scope(ctx, tx, "account", id)
		if err != nil {
			return err
		}
		if office == "" || !slices.Contains(actor.OfficeIDs, office) {
			return accountwrite.Forbidden("无权访问此资料")
		}
	}
	return nil
}

func position(ctx context.Context, tx *sql.Tx, body map[string]any) (coordinate, error) {
	var batch string
	err := tx.QueryRowContext(ctx, "SELECT batch FROM publishing_layout_state WHERE id=1").Scan(&batch)
	if errors.Is(err, sql.ErrNoRows) || err == nil && text(body["batch"]) != batch {
		return coordinate{}, accountwrite.Conflict("发布安排已更新，请刷新后重新选择")
	}
	if err != nil {
		return coordinate{}, err
	}
	slot, ok := number(body["slot"])
	if !ok {
		return coordinate{}, accountwrite.Invalid("发布位置不正确")
	}
	return coordinate{batch, text(body["computer_id"]), slot}, nil
}

func cell(ctx context.Context, tx *sql.Tx, k coordinate) (map[string]any, error) {
	var raw string
	err := tx.QueryRowContext(ctx, `SELECT to_jsonb(p)::text FROM publishing_layout p
		WHERE batch=$1 AND computer_id=$2 AND slot=$3`, k.batch, k.computer, k.slot).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	decoder := json.NewDecoder(strings.NewReader(raw))
	decoder.UseNumber()
	var result map[string]any
	if err := decoder.Decode(&result); err != nil {
		return nil, err
	}
	computer, err := scope(ctx, tx, "computer", k.computer)
	if err != nil {
		return nil, err
	}
	offices := []string{}
	if computer != "" {
		offices = append(offices, computer)
		if id := text(result["account_id"]); id != "" {
			office, err := scope(ctx, tx, "account", id)
			if err != nil {
				return nil, err
			}
			if office == "" {
				offices = []string{}
			} else if office != computer {
				offices = append(offices, office)
				slices.Sort(offices)
			}
		}
	}
	result["office_ids"] = offices
	return result, nil
}

func Link(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	k, err := position(ctx, tx, body)
	if err != nil {
		return nil, err
	}
	before, err := cell(ctx, tx, k)
	if err != nil {
		return nil, err
	}
	if before == nil || strings.TrimSpace(text(before["label"])) == "" {
		return nil, accountwrite.Invalid("只能处理已有文字的待匹配格子")
	}
	expected, present := body["expected_account_id"]
	_, stringExpected := expected.(string)
	if !present || expected != nil && !stringExpected || expected != before["account_id"] {
		return nil, accountwrite.Conflict("这个格子已被其他人处理，请刷新后重试")
	}
	var previousReason string
	err = tx.QueryRowContext(ctx, `SELECT previous_reason FROM publishing_layout_links
		WHERE batch=$1 AND computer_id=$2 AND slot=$3`, k.batch, k.computer, k.slot).Scan(&previousReason)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	}
	manual := err == nil
	if before["account_id"] != nil && !manual {
		return nil, accountwrite.Invalid("此格子已由来源表匹配，本入口仅处理待匹配和手动关联的格子")
	}
	result := map[string]any{"ok": true, "source_cell": before["source_cell"], "previous_account_id": before["account_id"], "account_id": nil}
	action, exists := body["action"]
	if !exists {
		action = "link"
	}
	switch action {
	case "clear":
		if !manual {
			return nil, accountwrite.Invalid("该格子没有可取消的手动关联")
		}
		if _, err := tx.ExecContext(ctx, `UPDATE publishing_layout SET account_id=NULL,reason=$1
			WHERE batch=$2 AND computer_id=$3 AND slot=$4`, previousReason, k.batch, k.computer, k.slot); err != nil {
			return nil, err
		}
		if err := deleteLink(ctx, tx, k); err != nil {
			return nil, err
		}
	case "link":
		id := text(body["account_id"])
		var exists bool
		if err := tx.QueryRowContext(ctx, "SELECT EXISTS(SELECT 1 FROM accounts WHERE id=$1)", id).Scan(&exists); err != nil {
			return nil, err
		}
		if !exists {
			return nil, accountwrite.Invalid("请选择工作台中已存在的账号")
		}
		if err := requireSameOffice(ctx, tx, id, k.computer); err != nil {
			return nil, err
		}
		var other string
		err = tx.QueryRowContext(ctx, `SELECT source_cell FROM publishing_layout WHERE batch=$1 AND account_id=$2
			AND NOT(computer_id=$3 AND slot=$4) LIMIT 1`, k.batch, id, k.computer, k.slot).Scan(&other)
		if err == nil {
			return nil, accountwrite.Conflict("该账号已有发布位置 " + other + "，请先核对，不能重复关联")
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return nil, err
		}
		if err := saveLink(ctx, tx, k, text(before["reason"]), actor.ID); err != nil {
			return nil, err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE publishing_layout SET account_id=$1,reason=''
			WHERE batch=$2 AND computer_id=$3 AND slot=$4`, id, k.batch, k.computer, k.slot); err != nil {
			return nil, err
		}
		result["account_id"] = id
	default:
		return nil, accountwrite.Invalid("关联操作不正确")
	}
	detail, err := recordwire.Encode(map[string]any{
		"computer": k.computer, "slot": k.slot, "source_cell": result["source_cell"],
		"account_id": result["account_id"], "previous_account_id": result["previous_account_id"],
	})
	if err != nil {
		return nil, err
	}
	if err := accountwrite.Audit(ctx, tx, actor, "调整发布格子关联", string(detail)); err != nil {
		return nil, err
	}
	return finish(ctx, tx, actor, LinkPath, result, []coordinate{k}, []map[string]any{before})
}

func Assign(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	source, err := position(ctx, tx, body)
	if err != nil {
		return nil, err
	}
	old, err := cell(ctx, tx, source)
	if err != nil {
		return nil, err
	}
	keys, before := []coordinate{source}, []map[string]any{old}
	switch text(body["action"]) {
	case "move":
		slot, ok := number(body["target_slot"])
		if !ok {
			return nil, accountwrite.Invalid("目标位置不正确")
		}
		target := coordinate{source.batch, text(body["target_computer"]), slot}
		targetOld, err := cell(ctx, tx, target)
		if err != nil {
			return nil, err
		}
		keys, before = append(keys, target), append(before, targetOld)
	case "remove":
	default:
		return nil, accountwrite.Invalid("位置操作不正确")
	}
	versions := map[string]any{}
	for i, key := range keys {
		var version any
		if before[i] != nil {
			version, err = recordwire.Version(before[i])
			if err != nil {
				return nil, err
			}
		}
		versions[key.key()] = version
	}
	if err := accountwrite.CheckVersions(body, versions); err != nil {
		return nil, err
	}
	if old == nil || old["account_id"] == nil {
		return nil, accountwrite.Invalid("请选择已关联账号的发布位置")
	}
	if len(keys) == 2 {
		target, targetOld := keys[1], before[1]
		if targetOld == nil || targetOld["account_id"] != nil || strings.TrimSpace(text(targetOld["label"])) != "" {
			return nil, accountwrite.Conflict("目标不是空白发布格，请选择空白位置")
		}
		if strings.HasPrefix(source.computer, "office3:") != strings.HasPrefix(target.computer, "office3:") {
			return nil, accountwrite.Invalid("不同设备批次不能混用发布电脑")
		}
		if err := requireSameOffice(ctx, tx, text(old["account_id"]), target.computer); err != nil {
			return nil, err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE publishing_layout SET account_id=$1,label=$2,office=$3,reason=''
			WHERE batch=$4 AND computer_id=$5 AND slot=$6`, old["account_id"], old["label"], old["office"], target.batch, target.computer, target.slot); err != nil {
			return nil, err
		}
		if err := saveLink(ctx, tx, target, text(targetOld["reason"]), actor.ID); err != nil {
			return nil, err
		}
	}
	if _, err := tx.ExecContext(ctx, `UPDATE publishing_layout SET account_id=NULL,label='',reason=''
		WHERE batch=$1 AND computer_id=$2 AND slot=$3`, source.batch, source.computer, source.slot); err != nil {
		return nil, err
	}
	if err := deleteLink(ctx, tx, source); err != nil {
		return nil, err
	}
	return finish(ctx, tx, actor, AssignmentPath, map[string]any{"ok": true}, keys, before)
}

func saveLink(ctx context.Context, tx *sql.Tx, k coordinate, reason string, actor int64) error {
	_, err := tx.ExecContext(ctx, `INSERT INTO publishing_layout_links
		(batch,computer_id,slot,previous_reason,actor_id,updated_at) VALUES($1,$2,$3,$4,$5,$6)
		ON CONFLICT(batch,computer_id,slot) DO UPDATE SET actor_id=excluded.actor_id,updated_at=excluded.updated_at`,
		k.batch, k.computer, k.slot, reason, actor, time.Now().Unix())
	return err
}

func deleteLink(ctx context.Context, tx *sql.Tx, k coordinate) error {
	_, err := tx.ExecContext(ctx, "DELETE FROM publishing_layout_links WHERE batch=$1 AND computer_id=$2 AND slot=$3", k.batch, k.computer, k.slot)
	return err
}

func finish(ctx context.Context, tx *sql.Tx, actor session.User, action string, result map[string]any, keys []coordinate, before []map[string]any) (map[string]any, error) {
	group, err := accountwrite.NewGroup()
	if err != nil {
		return nil, err
	}
	versions := map[string]any{}
	for i, k := range keys {
		next, err := cell(ctx, tx, k)
		if err != nil {
			return nil, err
		}
		prevWire, err := recordwire.Encode(before[i])
		if err != nil {
			return nil, err
		}
		nextWire, err := recordwire.Encode(next)
		if err != nil {
			return nil, err
		}
		if string(prevWire) != string(nextWire) || action == AssignmentPath {
			versions[k.key()], err = recordwire.Version(next)
			if err != nil {
				return nil, err
			}
		}
		if string(prevWire) == string(nextWire) {
			continue
		}
		oldOffices, _ := before[i]["office_ids"].([]string)
		newOffices, _ := next["office_ids"].([]string)
		var scope []string
		if len(oldOffices) != 0 && len(newOffices) != 0 {
			offices := append(slices.Clone(oldOffices), newOffices...)
			slices.Sort(offices)
			scope = slices.Compact(offices)
		}
		if err := accountwrite.InsertHistory(ctx, tx, actor, action, group, k.key(), before[i], next, scope); err != nil {
			return nil, err
		}
	}
	result["_record_versions"] = versions
	return result, accountwrite.Audit(ctx, tx, actor, action, "")
}
