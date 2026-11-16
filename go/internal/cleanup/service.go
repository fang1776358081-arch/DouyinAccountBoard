// Package cleanup implements the owner-confirmed workspace business reset.
package cleanup

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"maps"
	"net/http"
	"regexp"
	"slices"
	"strings"
	"time"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

const (
	PreviewPath  = "/api/workspace/business-cleanup-preview"
	ApplyPath    = "/api/workspace/business-cleanup-apply"
	Confirmation = "永久清理全站业务资料"

	schema       = "board_candidate."
	previewKind  = "workspace-business-cleanup"
	gridCountKey = "publishing_slots_cleared"
	previewTTL   = 10 * time.Minute
)

var previewIDPattern = regexp.MustCompile("^[0-9a-f]{32}$")

type Service struct {
	Write accountwrite.Service
}

// AuthorizeOwner applies to both mutations and to their reusable receipts.
func AuthorizeOwner(_ context.Context, _ *sql.Tx, actor session.User, _ map[string]any) error {
	if !actor.Active || actor.Role != "owner" || actor.ScopeMode != "all" {
		return accountwrite.Forbidden("只有拥有全站范围的总管理员可以清理业务资料")
	}
	return nil
}

func (s Service) Preview() http.HandlerFunc {
	return s.Write.Direct(1024, false, AuthorizeOwner, s.preview)
}

func (s Service) Apply() http.HandlerFunc {
	return s.Write.HandleLimit(ApplyPath, "确认清理必须带请求标识", 2048, AuthorizeOwner, s.apply)
}

type cleanupPlan struct {
	Generation    string           `json:"generation"`
	Authorization int64            `json:"authorization"`
	Guard         string           `json:"guard"`
	Counts        map[string]int64 `json:"counts"`
}

func (s Service) preview(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	if len(body) != 0 {
		return nil, accountwrite.Invalid("清理预览无需提交资料或筛选条件")
	}
	_, current, err := readCurrent(ctx, tx)
	if err != nil {
		return nil, err
	}
	id, err := accountwrite.NewGroup()
	if err != nil {
		return nil, err
	}
	plan := cleanupPlan{s.Write.RestoreGeneration, actor.AuthorizationVersion, current.guard, current.counts}
	payload, err := json.Marshal(plan)
	if err != nil {
		return nil, err
	}
	expires := time.Now().Add(previewTTL).Unix()
	_, err = tx.ExecContext(ctx, `
INSERT INTO board_candidate.operations_previews(id,actor_id,expires_at,kind,payload)
VALUES($1,$2,$3,$4,$5)`, id, actor.ID, expires, previewKind, string(payload))
	if err != nil {
		return nil, err
	}
	return map[string]any{"preview_id": id, "expires_at": expires, "counts": current.counts}, nil
}

func (s Service) apply(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	if len(body) != 3 {
		return nil, accountwrite.Invalid("清理确认仅接受预览编号、确认文字和请求编号")
	}
	id, _ := body["preview_id"].(string)
	if !previewIDPattern.MatchString(id) || body["confirmation"] != Confirmation {
		return nil, accountwrite.Invalid("请确认本次预览并输入“" + Confirmation + "”")
	}
	const stale = "清理预览已失效或业务资料发生变化，请重新预览"
	var payload string
	var expires int64
	err := tx.QueryRowContext(ctx, `
SELECT payload,expires_at FROM board_candidate.operations_previews
WHERE id=$1 AND actor_id=$2 AND kind=$3 AND expires_at>$4`, id, actor.ID, previewKind, time.Now().Unix()).Scan(&payload, &expires)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, accountwrite.Conflict(stale)
	}
	if err != nil {
		return nil, err
	}
	var plan cleanupPlan
	if json.Unmarshal([]byte(payload), &plan) != nil || plan.Generation != s.Write.RestoreGeneration ||
		plan.Authorization != actor.AuthorizationVersion {
		return nil, accountwrite.Conflict(stale)
	}
	tables, current, err := readCurrent(ctx, tx)
	if err != nil {
		return nil, err
	}
	if current.guard != plan.Guard || !maps.Equal(current.counts, plan.Counts) || expires <= time.Now().Unix() {
		return nil, accountwrite.Conflict(stale)
	}
	if err := removeBusinessRows(ctx, tx, tables, current.layoutAfter); err != nil {
		return nil, err
	}

	// Any old data-import or rollback preview must be rebuilt after this reset.
	// Audit and durable general receipts retain their historical meaning.
	if _, err := tx.ExecContext(ctx, "DELETE FROM board_candidate.operations_previews"); err != nil {
		return nil, err
	}
	group, err := accountwrite.NewGroup()
	if err != nil {
		return nil, err
	}
	if err := accountwrite.InsertHistory(ctx, tx, actor, "reload", group, "sync:reload", nil, nil, nil); err != nil {
		return nil, err
	}
	target, err := recordwire.Encode(map[string]int64{
		"accounts": current.counts["accounts"], "phone_cards": current.counts["phone_cards"],
		"phone_devices": current.counts["phone_devices"], "employee_card_employees": current.counts["employee_card_employees"],
		"publishing_slots_cleared": current.counts[gridCountKey],
	})
	if err != nil {
		return nil, err
	}
	if err := accountwrite.Audit(ctx, tx, actor, ApplyPath, string(target)); err != nil {
		return nil, err
	}
	return map[string]any{"ok": true, "counts": current.counts}, nil
}

func lockAndValidate(ctx context.Context, tx *sql.Tx, tables []string) error {
	touched := append(slices.Clone(tables), "publishing_layout", "operations_previews", "operations_history", "auth_audit")
	lockNames := append(slices.Clone(tables), "publishing_layout", "operations_previews", "publishing_layout_state", "collection_settings")
	slices.Sort(lockNames)
	for i, name := range lockNames {
		lockNames[i] = schema + name
	}
	if _, err := tx.ExecContext(ctx, "LOCK TABLE "+strings.Join(lockNames, ", ")+" IN SHARE ROW EXCLUSIVE MODE"); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, "LOCK TABLE board_candidate.auth_audit, board_candidate.operations_history IN ACCESS SHARE MODE"); err != nil {
		return err
	}
	// Only the reviewed relations may have a foreign key to any removed table.
	// Reject custom triggers/rules before writing, including on the audit tables.
	var unexpected bool
	err := tx.QueryRowContext(ctx, `
WITH removed AS (
	SELECT to_regclass('board_candidate.'||name) AS oid FROM unnest($1::text[]) AS names(name)
	UNION SELECT 'board_candidate.operations_previews'::regclass
), affected AS (
	SELECT to_regclass('board_candidate.'||name) AS oid FROM unnest($2::text[]) AS names(name)
)
SELECT EXISTS (
	SELECT 1 FROM pg_catalog.pg_constraint
	WHERE contype='f' AND confrelid IN (SELECT oid FROM removed)
	  AND conrelid NOT IN (SELECT oid FROM removed UNION SELECT 'board_candidate.publishing_layout'::regclass)
) OR EXISTS (
	SELECT 1 FROM pg_catalog.pg_constraint
	WHERE contype='f' AND confrelid='board_candidate.publishing_layout'::regclass
	  AND conrelid<>'board_candidate.publishing_layout_links'::regclass
) OR EXISTS (
	SELECT 1 FROM pg_catalog.pg_trigger
	WHERE tgrelid IN (SELECT oid FROM affected) AND NOT tgisinternal AND tgenabled<>'D'
) OR EXISTS (
	SELECT 1 FROM pg_catalog.pg_rewrite
	WHERE ev_class IN (SELECT oid FROM affected) AND ev_enabled<>'D'
) OR EXISTS (
	SELECT 1 FROM pg_catalog.pg_class c JOIN affected a ON a.oid=c.oid
	WHERE c.relkind<>'r' OR c.relispartition OR c.relhassubclass OR c.relrowsecurity
)`, tables, touched).Scan(&unexpected)
	if err != nil {
		return err
	}
	if unexpected {
		return accountwrite.Conflict("数据库存在未纳入清理范围的关系或自动写入规则，未更改业务资料")
	}
	var safeCollection bool
	err = tx.QueryRowContext(ctx, `
SELECT COALESCE((
	SELECT auto_enabled=0
	AND NOT EXISTS(SELECT 1 FROM board_candidate.collection_jobs WHERE state='running')
	AND (paused<>0 OR NOT EXISTS(SELECT 1 FROM board_candidate.collection_jobs WHERE state='queued'))
	FROM board_candidate.collection_settings WHERE id=1
),false)`).Scan(&safeCollection)
	if err != nil {
		return err
	}
	if !safeCollection {
		return accountwrite.Conflict("请先停用自动采集，并确认没有正在采集或即将启动的任务")
	}
	return nil
}
