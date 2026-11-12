// Package tablesync reads the office-three dual-sheet pilot's durable data.
package tablesync

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"regexp"
	"slices"
	"time"

	"douyinboard/internal/board"
	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

type Handler struct{ DB *sql.DB }
type item = map[string]any

const pilotOffice = "three"

// This is the SHA-256 of the canonical 0007 Git blob, without checkout line-ending conversion.
const migrationHash = "35d87536b4ad28a82b39aafc867765377c3103f8ee693a501ac20586fdc2f3bb"

var uuid = regexp.MustCompile(`^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$`)

type readError struct {
	status  int
	message string
	code    string
}

func (e readError) Error() string { return e.message }

func invalid(message string) error { return readError{http.StatusBadRequest, message, ""} }

func (h Handler) Read(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	tx, err := h.DB.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
	if err != nil {
		failure(w, err)
		return
	}
	defer tx.Rollback()
	actor, err := session.AuthenticateWith(ctx, tx, r)
	if err != nil {
		failure(w, err)
		return
	}
	value, err := read(ctx, tx, actor, r)
	if err != nil {
		failure(w, err)
		return
	}
	if err := tx.Commit(); err != nil {
		failure(w, err)
		return
	}
	board.WriteNegotiatedJSON(w, r, value)
}

func read(ctx context.Context, tx *sql.Tx, actor session.User, r *http.Request) (any, error) {
	office := query(r, "office_id", pilotOffice)
	if office != pilotOffice {
		return nil, invalid("当前仅开放办公室三试点，其他办公室和待分配区不参与写入")
	}
	var name string
	err := tx.QueryRowContext(ctx, `SELECT name FROM board_candidate.offices WHERE id=$1 AND active=1`, office).Scan(&name)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, invalid("办公室不存在或已停用")
	}
	if err != nil {
		return nil, err
	}
	if !actor.Global() && !slices.Contains(actor.OfficeIDs, office) {
		return nil, readError{http.StatusForbidden, "无权访问此办公室的表格同步", ""}
	}
	present, err := available(ctx, tx)
	if err != nil {
		return nil, err
	}
	switch r.URL.Path {
	case "/api/table-sync":
		return status(ctx, tx, actor, office, name, present)
	case "/api/table-sync/account-sources":
		return sources(ctx, tx, office, present)
	}
	if actor.Role != "owner" {
		return nil, readError{http.StatusForbidden, "只有总管理员可以管理人员和分配权限", ""}
	}
	if !present {
		return nil, invalid("表格同步扩展尚未启用，不能读取批次或执行写入")
	}
	switch r.URL.Path {
	case "/api/table-sync/batch":
		return receipt(ctx, tx, office, query(r, "id", ""))
	case "/api/table-sync/request":
		return requestResult(ctx, tx, actor, query(r, "request_id", ""))
	default:
		return nil, invalid("表格同步接口不存在")
	}
}

func available(ctx context.Context, tx *sql.Tx) (bool, error) {
	var present bool
	err := tx.QueryRowContext(ctx, `SELECT
		EXISTS(SELECT 1 FROM board_candidate.schema_migrations WHERE version=7 AND sha256=$1)
		AND to_regclass('board_candidate.table_sync_pilots') IS NOT NULL
		AND to_regclass('board_candidate.table_sync_account_sources') IS NOT NULL
		AND to_regclass('board_candidate.table_sync_batches') IS NOT NULL
		AND to_regclass('board_candidate.table_sync_review_items') IS NOT NULL
		AND to_regclass('board_candidate.table_sync_events') IS NOT NULL
		AND to_regclass('board_candidate.table_sync_requests') IS NOT NULL`, migrationHash).Scan(&present)
	return present, err
}

func status(ctx context.Context, tx *sql.Tx, actor session.User, office, name string, present bool) (item, error) {
	manage := actor.Role == "owner"
	pilot := item{"state": "disabled", "revision": 0}
	recent := make([]item, 0)
	note := "表格同步扩展尚未启用；需按项目流程备份并升级0007后才能试点，其他功能可继续使用。"
	if present {
		note = ""
		pilot["updated_at"] = nil
		var state string
		var revision, updatedAt int64
		err := tx.QueryRowContext(ctx, `SELECT state,revision,updated_at FROM board_candidate.table_sync_pilots WHERE office_id=$1`, office).
			Scan(&state, &revision, &updatedAt)
		if err == nil {
			pilot = item{"state": state, "revision": revision, "updated_at": updatedAt}
		} else if !errors.Is(err, sql.ErrNoRows) {
			return nil, err
		}
		if manage {
			recent, err = jsonRows(ctx, tx, `SELECT jsonb_build_object('id',id,'created_at',created_at,'kind',kind,'summary',receipt_json::jsonb->'summary')
				FROM board_candidate.table_sync_batches WHERE office_id=$1 ORDER BY created_at DESC,id DESC LIMIT 20`, office)
			if err != nil {
				return nil, err
			}
		}
	}
	user := item{"id": actor.ID, "username": actor.Username, "name": actor.Name, "role": actor.Role,
		"active": actor.Active, "permissions": actor.Permissions, "authorization_version": actor.AuthorizationVersion, "csrf": actor.CSRF}
	return item{"available": present, "user": user, "office_id": office, "office_name": name, "pilot": pilot,
		"can_manage": manage && present, "can_export": manage && actor.Has("data_export"), "recent_batches": recent, "note": note}, nil
}

func sources(ctx context.Context, tx *sql.Tx, office string, present bool) (item, error) {
	result := make([]item, 0)
	if present {
		var err error
		result, err = jsonRows(ctx, tx, `SELECT (to_jsonb(s)-'source_data')||jsonb_build_object('source_data',s.source_data::jsonb)
			FROM board_candidate.table_sync_account_sources s JOIN board_candidate.account_offices o ON o.account_id=s.account_id
			WHERE s.office_id=$1 AND o.office_id=$1 ORDER BY s.account_id`, office)
		if err != nil {
			return nil, err
		}
	}
	version, err := recordwire.Version(result)
	if err != nil {
		return nil, err
	}
	return item{"available": present, "office_id": office, "sources": result, "version": version}, nil
}

func receipt(ctx context.Context, tx *sql.Tx, office, id string) (item, error) {
	var raw []byte
	err := tx.QueryRowContext(ctx, `SELECT receipt_json FROM board_candidate.table_sync_batches WHERE id=$1 AND office_id=$2`, id, office).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, invalid("未找到本办公室批次")
	}
	if err != nil {
		return nil, err
	}
	result, err := decodeObject(raw)
	if err != nil {
		return nil, err
	}
	issues, err := jsonRows(ctx, tx, `SELECT payload_json::jsonb||jsonb_build_object('state',state,'note',note,'updated_at',updated_at)
		FROM board_candidate.table_sync_review_items WHERE batch_id=$1 AND office_id=$2 ORDER BY id`, id, office)
	if err != nil {
		return nil, err
	}
	result["issues"] = issues
	return result, nil
}

func requestResult(ctx context.Context, tx *sql.Tx, actor session.User, id string) (any, error) {
	if !uuid.MatchString(id) {
		return nil, invalid("请求标识不正确")
	}
	var generation, current string
	var raw []byte
	err := tx.QueryRowContext(ctx, `SELECT r.generation,r.result_json,c.restore_generation
		FROM board_candidate.table_sync_requests r CROSS JOIN board_candidate.runtime_control c
		WHERE r.actor_id=$1 AND r.request_id=$2 AND c.id=1`, actor.ID, id).Scan(&generation, &raw, &current)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, readError{http.StatusNotFound, "未找到原请求结果；只能核对或重试同一请求编号，不能另建重复请求", "REQUEST_NOT_FOUND"}
	}
	if err != nil {
		return nil, err
	}
	if current == "" {
		return nil, invalid("恢复代次未配置，表格同步暂不可写")
	}
	if generation != current {
		return nil, readError{http.StatusConflict, "请求属于已关闭的恢复代次，不能再次执行", "RESTORE_GENERATION_EXPIRED"}
	}
	return decodeObject(raw)
}

func query(r *http.Request, key, fallback string) string {
	for _, value := range r.URL.Query()[key] {
		if value != "" {
			return value
		}
	}
	return fallback
}

func jsonRows(ctx context.Context, tx *sql.Tx, query string, args ...any) ([]item, error) {
	rows, err := tx.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make([]item, 0)
	for rows.Next() {
		var raw []byte
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		value, err := decodeObject(raw)
		if err != nil {
			return nil, err
		}
		out = append(out, value)
	}
	return out, rows.Err()
}

func decodeObject(raw []byte) (item, error) {
	var result item
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&result); err != nil {
		return nil, err
	}
	if result == nil {
		return nil, errors.New("table-sync record must be an object")
	}
	return result, nil
}

func failure(w http.ResponseWriter, err error) {
	var request readError
	if errors.Is(err, session.ErrUnauthorized) {
		request = readError{http.StatusUnauthorized, "请先登录工作台", ""}
	} else if !errors.As(err, &request) {
		request = readError{http.StatusServiceUnavailable, "数据服务暂时不可用，请稍后刷新核对操作结果", "DB_UNAVAILABLE"}
	}
	response := item{"error": request.message}
	if request.code != "" {
		response["code"] = request.code
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(request.status)
	_ = json.NewEncoder(w).Encode(response)
}
