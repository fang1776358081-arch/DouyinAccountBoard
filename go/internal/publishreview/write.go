package publishreview

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net/http"
	"regexp"
	"slices"
	"strings"
	"time"
	"unicode/utf8"

	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

const taskPath = "/api/publish-review/task"
const samplePath = "/api/publish-review/sample"

var uuid = regexp.MustCompile(`^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$`)
var deadlineFormat = regexp.MustCompile(`^(?:[01][0-9]|2[0-3]):[0-5][0-9]$`)

func (h Handler) Write(w http.ResponseWriter, r *http.Request) {
	path := r.URL.Path
	permission := "publishing_manage"
	if path == samplePath {
		permission = "collection_manage"
	} else if path != taskPath {
		failure(w, apiError{http.StatusNotFound, "发布核对接口不存在", ""})
		return
	}
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		failure(w, apiError{http.StatusMethodNotAllowed, "请求方法不正确", ""})
		return
	}
	origin := strings.TrimRight(h.PublicOrigin, "/")
	if origin == "" {
		scheme := "http"
		if r.TLS != nil {
			scheme = "https"
		}
		origin = scheme + "://" + r.Host
	}
	if originValue := r.Header.Get("Origin"); originValue != "" && originValue != origin {
		failure(w, forbidden("请求来源不正确"))
		return
	}
	contentType, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || contentType != "application/json" {
		failure(w, invalid("需要 JSON 请求"))
		return
	}
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 100000))
	decoder.UseNumber()
	var body row
	if err := decoder.Decode(&body); err != nil || body == nil || decoder.Decode(new(any)) != io.EOF {
		failure(w, invalid("请求内容应为对象且不能过大"))
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()
	tx, err := session.BeginWrite(ctx, h.DB, h.WriterEpoch, h.RestoreGeneration)
	if err != nil {
		var guard session.AdmissionError
		if errors.As(err, &guard) {
			status := http.StatusServiceUnavailable
			if guard.Code == "WRITER_VERSION_RETIRED" || guard.Code == "RESTORE_GENERATION_EXPIRED" {
				status = http.StatusConflict
			}
			err = apiError{status, "当前运行状态不允许写入，请刷新后重试", guard.Code}
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
		failure(w, forbidden("页面已过期，请刷新后重试"))
		return
	}
	if !actor.Has(permission) {
		failure(w, forbidden("当前账号没有此操作权限"))
		return
	}
	if present, err := available(ctx, tx); err != nil || !present {
		if err == nil {
			err = apiError{http.StatusServiceUnavailable, "发布核对的数据结构尚未升级", schemaRequired}
		}
		failure(w, err)
		return
	}
	result, err := apply(ctx, tx, actor, h.RestoreGeneration, path, body, h.clock())
	if err != nil {
		failure(w, err)
		return
	}
	if err := tx.Commit(); err != nil {
		failure(w, err)
		return
	}
	writeJSON(w, http.StatusOK, result)
}

type account struct {
	ID, Name, Handle, Status, Computer, Office string
}

func writeAccount(ctx context.Context, tx *sql.Tx, actor session.User, value any) (account, error) {
	id, ok := value.(string)
	if !ok || id == "" || utf8.RuneCountInString(id) > 200 || strings.ContainsRune(id, 0) {
		return account{}, invalid("请指定稳定的系统账号ID")
	}
	var result account
	err := tx.QueryRowContext(ctx, `SELECT a.id,COALESCE(a.name,''),COALESCE(a.douyin_id,''),COALESCE(a.status,''),
		COALESCE(a.computer_id,''),COALESCE(o.office_id,'') FROM board_candidate.accounts a
		LEFT JOIN board_candidate.account_offices o ON o.account_id=a.id WHERE a.id=$1`, id).
		Scan(&result.ID, &result.Name, &result.Handle, &result.Status, &result.Computer, &result.Office)
	if errors.Is(err, sql.ErrNoRows) || (err == nil && !canWriteOffice(actor, result.Office)) {
		return account{}, forbidden("无权访问此资料")
	}
	return result, err
}

func canWriteOffice(actor session.User, office string) bool {
	return actor.Global() || (office != "" && slices.Contains(actor.OfficeIDs, office))
}

func apply(ctx context.Context, tx *sql.Tx, actor session.User, generation, path string, body row, current time.Time) (row, error) {
	account, err := writeAccount(ctx, tx, actor, body["account_id"])
	if err != nil {
		return nil, err
	}
	requestID := str(body["request_id"])
	if !uuid.MatchString(requestID) {
		return nil, invalid("请求标识不正确，请刷新后重试")
	}
	// Preserve the dedicated Python digest, including its whitespace and the UUID.
	raw, err := legacyJSON(row{"path": path, "body": body})
	if err != nil {
		return nil, err
	}
	sum := sha256.Sum256(raw)
	fingerprint := hex.EncodeToString(sum[:])
	var oldGeneration, oldHash string
	var stored []byte
	err = tx.QueryRowContext(ctx, `SELECT generation,fingerprint,result_json FROM board_candidate.publish_review_requests WHERE actor_id=$1 AND request_id=$2`, actor.ID, requestID).
		Scan(&oldGeneration, &oldHash, &stored)
	if err == nil {
		if oldGeneration != generation || oldHash != fingerprint {
			return nil, conflict("请求标识已用于其他内容或恢复代次，请先核对结果")
		}
		return decodeRow(stored)
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	}
	var result row
	if path == taskPath {
		result, err = saveTask(ctx, tx, actor, account, body, current)
	} else {
		result, err = ingestSample(ctx, tx, account, requestID, body, current)
	}
	if err != nil {
		return nil, err
	}
	encoded, err := recordwire.Encode(result)
	if err != nil {
		return nil, err
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO board_candidate.publish_review_requests(actor_id,request_id,generation,fingerprint,result_json)
		VALUES($1,$2,$3,$4,$5)`, actor.ID, requestID, generation, fingerprint, string(encoded))
	return result, err
}

func saveTask(ctx context.Context, tx *sql.Tx, actor session.User, account account, body row, current time.Time) (row, error) {
	day := str(body["day"])
	if !validDay(day) {
		return nil, invalid("业务日期格式不正确")
	}
	if day < current.Format(time.DateOnly) {
		return nil, invalid("历史日期的目标和任务归属已冻结，不能直接改写")
	}
	state := str(body["task_state"])
	var target any
	if body["target"] != nil {
		value, ok := number(body["target"])
		if !ok || value < 0 || value > 100000 {
			return nil, invalid("任务状态或目标不正确；未设置目标请留空")
		}
		target = value
	}
	if state != "active" && state != "paused" && state != "none" {
		return nil, invalid("任务状态或目标不正确；未设置目标请留空")
	}
	reason := strings.TrimSpace(str(body["reason"]))
	if length := utf8.RuneCountInString(reason); length < 1 || length > 500 || strings.ContainsRune(reason, 0) {
		return nil, invalid("请填写设置或修改目标的依据")
	}
	var deadline string
	if raw := body["deadline"]; raw != nil && raw != "" {
		value := str(raw)
		if !deadlineFormat.MatchString(value) {
			return nil, invalid("截止时间须为北京时间HH:MM，未知可留空")
		}
		deadline = day + "T" + value + ":00.000000+08:00"
	}
	computer, computerName, operator := account.Computer, "任务电脑待核对", ""
	if raw, ok := body["computer_id"]; ok {
		computer, ok = raw.(string)
		if !ok || strings.ContainsRune(computer, 0) {
			return nil, invalid("任务电脑不正确")
		}
	}
	if computer != "" {
		var office string
		err := tx.QueryRowContext(ctx, `SELECT COALESCE(c.name,''),COALESCE(c.operator,''),COALESCE(o.office_id,'')
			FROM board_candidate.computers c LEFT JOIN board_candidate.computer_offices o ON o.computer_id=c.id WHERE c.id=$1`, computer).
			Scan(&computerName, &operator, &office)
		if errors.Is(err, sql.ErrNoRows) {
			return nil, invalid("请选择当日任务的实际电脑")
		}
		if err != nil {
			return nil, err
		}
		if !canWriteOffice(actor, office) {
			return nil, forbidden("无权访问此资料")
		}
		if office != account.Office {
			return nil, invalid("账号与任务电脑的明确办公室须一致，请先核对归属")
		}
	}
	expected, ok := number(body["expected_revision"])
	if !ok || expected < 0 {
		return nil, invalid("缺少当前任务版本，请刷新后重试")
	}
	var revision int64
	var priorTarget sql.NullInt64
	var oldOffice string
	err := tx.QueryRowContext(ctx, `SELECT revision,target,office_id FROM board_candidate.publish_review_tasks WHERE account_id=$1 AND day=$2`, account.ID, day).
		Scan(&revision, &priorTarget, &oldOffice)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	}
	if err == nil && !canWriteOffice(actor, oldOffice) {
		return nil, forbidden("无权修改任务原办公室的计划")
	}
	if expected != revision {
		return nil, conflict("目标已被其他操作修改，请刷新后核对；未覆盖")
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO board_candidate.publish_review_tasks(account_id,day,target,task_state,deadline,computer_id,computer_name,
		operator,office_id,account_name,douyin_id,account_status,revision,reason,updated_at)
		VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
		ON CONFLICT(account_id,day) DO UPDATE SET target=excluded.target,task_state=excluded.task_state,deadline=excluded.deadline,
		computer_id=excluded.computer_id,computer_name=excluded.computer_name,operator=excluded.operator,office_id=excluded.office_id,
		account_name=excluded.account_name,douyin_id=excluded.douyin_id,account_status=excluded.account_status,revision=excluded.revision,
		reason=excluded.reason,updated_at=excluded.updated_at`, account.ID, day, target, state, deadline, computer, computerName, operator,
		account.Office, account.Name, account.Handle, account.Status, revision+1, reason, current.Format(timestamp))
	if err != nil {
		return nil, err
	}
	var before any
	if priorTarget.Valid {
		before = priorTarget.Int64
	}
	audit, err := legacyJSON(struct {
		Account      string `json:"account_id"`
		Day          string `json:"day"`
		BeforeTarget any    `json:"before_target"`
		Target       any    `json:"target"`
		State        string `json:"task_state"`
		Reason       string `json:"reason"`
	}{account.ID, day, before, target, state, reason})
	if err != nil {
		return nil, err
	}
	targetText := []rune(string(audit))
	_, err = tx.ExecContext(ctx, `INSERT INTO board_candidate.auth_audit(at,actor_id,actor_name,action,target) VALUES($1,$2,$3,'设置发布核对任务',$4)`,
		current.Unix(), actor.ID, actor.Name, string(targetText[:min(len(targetText), 200)]))
	return row{"ok": true, "revision": revision + 1}, err
}

func legacyJSON(value any) ([]byte, error) {
	compact, err := recordwire.Encode(value)
	if err != nil {
		return nil, err
	}
	var out bytes.Buffer
	inString, escaped := false, false
	for _, value := range compact {
		out.WriteByte(value)
		if inString {
			if escaped {
				escaped = false
			} else if value == '\\' {
				escaped = true
			} else if value == '"' {
				inString = false
			}
			continue
		}
		if value == '"' {
			inString = true
		} else if value == ',' || value == ':' {
			out.WriteByte(' ')
		}
	}
	return out.Bytes(), nil
}
