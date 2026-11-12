package tablesync

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net/http"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

type Writer struct {
	DB                *sql.DB
	WriterEpoch       int64
	RestoreGeneration string
	PublicOrigin      string
}

const previewKind = "table-sync-pilot"
const confirmation = "同步办公室三资料"
const reverseConfirmation = "回退办公室三批次"

var china = time.FixedZone("CST", 8*60*60)

func (h Writer) Write(w http.ResponseWriter, r *http.Request) {
	origin := strings.TrimSuffix(h.PublicOrigin, "/")
	if origin == "" {
		scheme := "http"
		if r.TLS != nil {
			scheme = "https"
		}
		origin = scheme + "://" + r.Host
	}
	if supplied := r.Header.Get("Origin"); supplied != "" && supplied != origin {
		failure(w, readError{http.StatusForbidden, "请求来源不正确", ""})
		return
	}
	media, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || media != "application/json" {
		failure(w, invalid("需要 JSON 请求"))
		return
	}
	limit := int64(100000)
	if r.URL.Path == "/api/table-sync/preview" {
		limit = 12 << 20
	}
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, limit))
	decoder.UseNumber()
	var body item
	if err := decoder.Decode(&body); err != nil || body == nil || decoder.Decode(new(any)) != io.EOF {
		failure(w, invalid("表格同步请求格式不正确或过长"))
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 90*time.Second)
	defer cancel()
	tx, err := session.BeginWrite(ctx, h.DB, h.WriterEpoch, h.RestoreGeneration)
	if err != nil {
		var guard session.AdmissionError
		if errors.As(err, &guard) {
			status := http.StatusServiceUnavailable
			if guard.Code == "WRITER_VERSION_RETIRED" || guard.Code == "RESTORE_GENERATION_EXPIRED" {
				status = http.StatusConflict
			}
			err = readError{status, "当前运行状态不允许写入，请刷新后重试", guard.Code}
		}
		failure(w, err)
		return
	}
	defer tx.Rollback()
	actor, err := session.AuthenticateWith(ctx, tx, r)
	if err != nil {
		failure(w, err)
		return
	}
	csrf := r.Header.Get("X-CSRF-Token")
	if csrf == "" || subtle.ConstantTimeCompare([]byte(csrf), []byte(actor.CSRF)) != 1 {
		failure(w, readError{http.StatusForbidden, "页面已过期，请刷新后重试", ""})
		return
	}
	result, err := executeWrite(ctx, tx, actor, h.RestoreGeneration, r.URL.Path, body, time.Now().In(china).Format(time.DateOnly))
	if err != nil {
		failure(w, err)
		return
	}
	if err := tx.Commit(); err != nil {
		failure(w, err)
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	_ = json.NewEncoder(w).Encode(result)
}

func executeWrite(ctx context.Context, tx *sql.Tx, actor session.User, generation, path string, body item, day string) (item, error) {
	if actor.Role != "owner" {
		return nil, readError{http.StatusForbidden, "只有总管理员可以管理人员和分配权限", ""}
	}
	office := text(body["office_id"])
	if office != pilotOffice {
		return nil, invalid("当前仅开放办公室三试点，其他办公室和待分配区不参与写入")
	}
	var active bool
	if err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM board_candidate.offices WHERE id=$1 AND active=1)`, office).Scan(&active); err != nil {
		return nil, err
	}
	if !active {
		return nil, invalid("办公室不存在或已停用")
	}
	if present, err := available(ctx, tx); err != nil || !present {
		if err == nil {
			err = invalid("表格同步扩展尚未启用；未修改任何资料")
		}
		return nil, err
	}
	id := text(body["request_id"])
	if !uuid.MatchString(id) {
		return nil, invalid("请求标识不正确")
	}
	requestBody := make(item, len(body)-1)
	for key, value := range body {
		if key != "request_id" {
			requestBody[key] = value
		}
	}
	fingerprint, err := recordwire.Version(item{"action": path, "body": requestBody})
	if err != nil {
		return nil, err
	}
	var previousGeneration, previousHash string
	var stored []byte
	err = tx.QueryRowContext(ctx, `SELECT generation,fingerprint,result_json FROM board_candidate.table_sync_requests WHERE actor_id=$1 AND request_id=$2`, actor.ID, id).
		Scan(&previousGeneration, &previousHash, &stored)
	if err == nil {
		if previousGeneration != generation {
			return nil, readError{http.StatusConflict, "请求属于已关闭的恢复代次，不能再次执行", "RESTORE_GENERATION_EXPIRED"}
		}
		if previousHash != fingerprint {
			return nil, readError{http.StatusConflict, "同一请求编号不能用于不同内容", "REQUEST_ID_REUSED"}
		}
		return decodeObject(stored)
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	}
	var value item
	switch path {
	case "/api/table-sync/pilot":
		value, err = setPilot(ctx, tx, actor, office, body)
	case "/api/table-sync/preview":
		value, err = makePreview(ctx, tx, actor, office, generation, day, body)
	case "/api/table-sync/apply", "/api/table-sync/rollback-apply":
		value, err = applyPreview(ctx, tx, actor, office, generation, day, body, path == "/api/table-sync/rollback-apply")
	case "/api/table-sync/review":
		value, err = reviewIssue(ctx, tx, actor, office, body)
	case "/api/table-sync/rollback-preview":
		value, err = reversePreview(ctx, tx, actor, office, generation, day, body)
	default:
		return nil, invalid("表格同步接口不存在")
	}
	if err != nil {
		return nil, err
	}
	result, err := recordwire.Encode(value)
	if err != nil {
		return nil, err
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO board_candidate.table_sync_requests(actor_id,request_id,generation,fingerprint,result_json) VALUES($1,$2,$3,$4,$5)`,
		actor.ID, id, generation, fingerprint, string(result))
	return value, err
}

func onlyFields(body item, fields ...string) error {
	allowed := map[string]bool{"office_id": true, "request_id": true}
	for _, field := range fields {
		allowed[field] = true
	}
	for key := range body {
		if !allowed[key] {
			return invalid("请求包含未开放字段，不能提交客户端修改计划")
		}
	}
	return nil
}

func conflict(message string) error { return readError{http.StatusConflict, message, ""} }

func text(value any) string {
	text, _ := value.(string)
	return text
}

func intValue(value any) (int64, bool) {
	switch value := value.(type) {
	case json.Number:
		parsed, err := strconv.ParseInt(value.String(), 10, 64)
		return parsed, err == nil
	case int:
		return int64(value), true
	case int64:
		return value, true
	default:
		return 0, false
	}
}

func nextID(dashed bool) string {
	var bytes [16]byte
	_, _ = rand.Read(bytes[:])
	bytes[6] = bytes[6]&0x0f | 0x40
	bytes[8] = bytes[8]&0x3f | 0x80
	value := hex.EncodeToString(bytes[:])
	if !dashed {
		return value
	}
	return value[:8] + "-" + value[8:12] + "-" + value[12:16] + "-" + value[16:20] + "-" + value[20:]
}

func currentPilot(ctx context.Context, tx *sql.Tx, office string) (item, int64, error) {
	var state string
	var revision, updatedAt int64
	err := tx.QueryRowContext(ctx, `SELECT state,revision,updated_at FROM board_candidate.table_sync_pilots WHERE office_id=$1`, office).Scan(&state, &revision, &updatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return item{"state": "disabled", "revision": int64(0), "updated_at": nil}, 0, nil
	}
	return item{"state": state, "revision": revision, "updated_at": updatedAt}, revision, err
}

func enabled(ctx context.Context, tx *sql.Tx, office string) (int64, error) {
	pilot, revision, err := currentPilot(ctx, tx, office)
	if err == nil && pilot["state"] != "enabled" {
		err = invalid("办公室三表格同步试点未开启或已暂停；未修改任何资料")
	}
	return revision, err
}

func setPilot(ctx context.Context, tx *sql.Tx, actor session.User, office string, body item) (item, error) {
	if err := onlyFields(body, "state", "expected_revision", "confirmation"); err != nil {
		return nil, err
	}
	state := text(body["state"])
	phrases := map[string]string{"enabled": "开启办公室三试点", "paused": "暂停办公室三试点", "ended": "结束办公室三试点"}
	if phrases[state] == "" || body["confirmation"] != phrases[state] {
		return nil, invalid("请明确确认本次试点状态变更")
	}
	before, current, err := currentPilot(ctx, tx, office)
	if err != nil {
		return nil, err
	}
	if expected, ok := intValue(body["expected_revision"]); !ok || expected != current {
		return nil, conflict("试点状态已变化，请重新读取")
	}
	now := time.Now().Unix()
	after := item{"state": state, "revision": current + 1, "updated_at": now}
	_, err = tx.ExecContext(ctx, `INSERT INTO board_candidate.table_sync_pilots(office_id,state,revision,updated_at) VALUES($1,$2,$3,$4)
		ON CONFLICT(office_id) DO UPDATE SET state=excluded.state,revision=excluded.revision,updated_at=excluded.updated_at`, office, state, current+1, now)
	if err == nil {
		err = writeEvent(ctx, tx, actor, office, "pilot", office, before, after)
	}
	return item{"ok": true, "office_id": office, "pilot": after}, err
}

func reviewIssue(ctx context.Context, tx *sql.Tx, actor session.User, office string, body item) (item, error) {
	if err := onlyFields(body, "batch_id", "issue_id", "state", "note"); err != nil {
		return nil, err
	}
	state := text(body["state"])
	rawNote, supplied := body["note"]
	value, validNote := rawNote.(string)
	if supplied && !validNote {
		return nil, invalid("核对依据格式不正确或过长")
	}
	note := strings.TrimSpace(value)
	if state != "open" && state != "confirmed_no_action" && state != "corrected" {
		return nil, invalid("待核对状态不正确")
	}
	if utf8.RuneCountInString(note) > 1000 || (state != "open" && note == "") || strings.IndexFunc(note, func(r rune) bool { return r < 32 && r != '\n' && r != '\t' }) >= 0 {
		return nil, invalid("核对依据格式不正确或过长")
	}
	id, batch := text(body["issue_id"]), text(body["batch_id"])
	var oldState, oldNote string
	err := tx.QueryRowContext(ctx, `SELECT state,note FROM board_candidate.table_sync_review_items WHERE id=$1 AND batch_id=$2 AND office_id=$3`, id, batch, office).
		Scan(&oldState, &oldNote)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, invalid("未找到本批次待核对项")
	}
	if err != nil {
		return nil, err
	}
	_, err = tx.ExecContext(ctx, `UPDATE board_candidate.table_sync_review_items SET state=$1,note=$2,updated_at=$3 WHERE id=$4`, state, note, time.Now().Unix(), id)
	if err == nil {
		err = writeEvent(ctx, tx, actor, office, "review", id, item{"state": oldState, "note": oldNote}, item{"state": state, "note": note})
	}
	return item{"ok": true, "batch_id": batch, "issue_id": id, "state": state, "note": note}, err
}

func writeEvent(ctx context.Context, tx *sql.Tx, actor session.User, office, kind, target string, before, after item) error {
	var old any
	if before != nil {
		encoded, err := recordwire.Encode(before)
		if err != nil {
			return err
		}
		old = string(encoded)
	}
	encoded, err := recordwire.Encode(after)
	if err != nil {
		return err
	}
	now := time.Now().Unix()
	_, err = tx.ExecContext(ctx, `INSERT INTO board_candidate.table_sync_events(id,office_id,actor_id,at,kind,target,before_json,after_json) VALUES($1,$2,$3,$4,$5,$6,$7,$8)`,
		nextID(true), office, actor.ID, now, kind, target, old, string(encoded))
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO board_candidate.auth_audit(at,actor_id,actor_name,action,target) VALUES($1,$2,$3,$4,$5)`,
		now, actor.ID, actor.Name, "表格同步:"+kind, target)
	return err
}
