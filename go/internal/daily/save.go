package daily

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
	"regexp"
	"strconv"
	"strings"
	"time"

	"douyinboard/internal/account"
	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

const dailyPath = "/api/daily"
const resultRetention = 30 * 24 * time.Hour
const conflictMessage = "这条资料已被其他人修改，请刷新核对后再保存；未覆盖对方修改"

var uuidPattern = regexp.MustCompile("^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")

type dailyRequest struct {
	ID, Date, RequestID string
	Published           bool
	Expected            map[string]any
	Fingerprint         string
}

type requestError struct {
	status        int
	message, code string
}

func (e requestError) Error() string { return e.message }

type saveResponse struct {
	status  int
	body    any
	reserve bool
}

func parseRequest(r io.Reader) (dailyRequest, error) {
	var body map[string]any
	decoder := json.NewDecoder(r)
	decoder.UseNumber()
	if err := decoder.Decode(&body); err != nil || body == nil {
		return dailyRequest{}, requestError{http.StatusBadRequest, "请求内容应为 JSON 对象", ""}
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return dailyRequest{}, requestError{http.StatusBadRequest, "请求内容应为 JSON 对象", ""}
	}
	var input dailyRequest
	for key := range body {
		switch key {
		case "id", "date", "published", "_expected_versions", "request_id":
		default:
			return input, requestError{http.StatusBadRequest, "发布请求含有不支持的字段", ""}
		}
	}
	input.RequestID, _ = body["request_id"].(string)
	if input.RequestID == "" {
		return input, requestError{http.StatusBadRequest, "记录当天发布必须带请求标识", "REQUEST_ID_REQUIRED"}
	}
	if !uuidPattern.MatchString(input.RequestID) {
		return input, requestError{http.StatusBadRequest, "请求标识不正确", ""}
	}
	switch id := body["id"].(type) {
	case string:
		input.ID = id
	case json.Number:
		number, err := strconv.ParseInt(string(id), 10, 64)
		if err == nil {
			input.ID = strconv.FormatInt(number, 10)
			body["id"] = number
		}
	}
	if input.ID == "" || strings.IndexByte(input.ID, 0) >= 0 {
		return input, requestError{http.StatusBadRequest, "账号编号不正确", ""}
	}
	input.Date, _ = body["date"].(string)
	if !validDay(input.Date) {
		return input, requestError{http.StatusBadRequest, "日期格式不正确", ""}
	}
	published, ok := body["published"].(bool)
	if !ok {
		return input, requestError{http.StatusBadRequest, "发布标记应为布尔值", ""}
	}
	input.Published = published
	input.Expected, ok = body["_expected_versions"].(map[string]any)
	if !ok {
		return input, requestError{http.StatusConflict, "页面版本过旧，请刷新后重新操作；未保存任何修改", ""}
	}
	for _, version := range input.Expected {
		if _, isString := version.(string); version != nil && !isString {
			return input, requestError{http.StatusConflict, conflictMessage, ""}
		}
	}
	delete(body, "request_id")
	fingerprint, err := recordwire.Version(map[string]any{"action": dailyPath, "body": body})
	input.Fingerprint = fingerprint
	return input, err
}

func (s Service) Save(w http.ResponseWriter, r *http.Request) {
	if s.WriterEpoch < 1 || s.RestoreGeneration == "" {
		writeError(w, http.StatusServiceUnavailable, "当前运行状态不允许写入，请刷新后重试", "CONTROL_STATE_MISSING")
		return
	}
	origin := strings.TrimSuffix(s.PublicOrigin, "/")
	if origin == "" {
		scheme := "http"
		if r.TLS != nil {
			scheme = "https"
		}
		origin = scheme + "://" + r.Host
	}
	if sent := r.Header.Get("Origin"); sent != "" && sent != origin {
		writeError(w, http.StatusForbidden, "请求来源不正确", "")
		return
	}
	contentType, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || contentType != "application/json" {
		writeError(w, http.StatusBadRequest, "需要 JSON 请求", "")
		return
	}
	input, err := parseRequest(http.MaxBytesReader(w, r.Body, 100000))
	if err != nil {
		writeSaveError(w, err, "")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()
	result, err := s.saveOnce(ctx, r, input)
	if err == nil && result.reserve {
		// Match the existing two-transaction protocol. The durable pending
		// record survives a stale version, lost connection or a process crash.
		result, err = s.saveOnce(ctx, r, input)
	}
	if err != nil {
		writeSaveError(w, err, input.RequestID)
		return
	}
	if result.reserve {
		databaseError(w, nil, input.RequestID)
		return
	}
	writeJSON(w, result.status, result.body)
}

func writeSaveError(w http.ResponseWriter, err error, requestID string) {
	var apiErr requestError
	var admission session.AdmissionError
	switch {
	case errors.As(err, &apiErr):
		writeError(w, apiErr.status, apiErr.message, apiErr.code)
	case errors.As(err, &admission):
		status := http.StatusServiceUnavailable
		if admission.Code == "WRITER_VERSION_RETIRED" || admission.Code == "RESTORE_GENERATION_EXPIRED" {
			status = http.StatusConflict
		}
		writeError(w, status, "当前运行状态不允许写入，请刷新后重试", admission.Code)
	default:
		writeAuthError(w, err, requestID)
	}
}

func (s Service) saveOnce(ctx context.Context, r *http.Request, input dailyRequest) (saveResponse, error) {
	tx, err := session.BeginWrite(ctx, s.DB, s.WriterEpoch, s.RestoreGeneration)
	if err != nil {
		return saveResponse{}, err
	}
	defer tx.Rollback()
	actor, err := session.AuthenticateWith(ctx, tx, r)
	if err != nil {
		return saveResponse{}, err
	}
	csrf := r.Header.Get("X-CSRF-Token")
	if csrf == "" || subtle.ConstantTimeCompare([]byte(csrf), []byte(actor.CSRF)) != 1 {
		return saveResponse{}, requestError{http.StatusForbidden, "页面已过期，请刷新后重试", ""}
	}
	if !actor.Has("publish") {
		return saveResponse{}, requestError{http.StatusForbidden, "当前账号没有此操作权限", ""}
	}
	accountVersion, office, err := readAccount(ctx, tx, actor, input.ID)
	if err != nil {
		return saveResponse{}, err
	}
	out, proceed, err := s.claim(ctx, tx, actor.ID, input)
	if err != nil || !proceed {
		return out, err
	}
	record, err := readDaily(ctx, tx, input, office)
	if err != nil {
		return saveResponse{}, err
	}
	versions := map[string]any{"account:" + input.ID: accountVersion}
	key := dailyKey(input.ID, input.Date)
	versions[key] = nil
	if record != nil {
		version, err := recordwire.Version(record)
		if err != nil {
			return saveResponse{}, err
		}
		versions[key] = version
	}
	for name, actual := range versions {
		if input.Expected[name] != actual {
			return saveResponse{}, requestError{http.StatusConflict, conflictMessage, ""}
		}
	}
	next := dailyRecord{input.ID, input.Date, officeIDs(office), 0}
	if input.Published {
		next.Published = 1
	}
	if err := writeDaily(ctx, tx, actor, record, next); err != nil {
		return saveResponse{}, err
	}
	versions[key], err = recordwire.Version(next)
	if err != nil {
		return saveResponse{}, err
	}
	body := map[string]any{
		"ok":               true,
		"daily":            map[string]any{"id": input.ID, "date": input.Date, "published": input.Published},
		"_record_versions": versions,
	}
	responseJSON, err := recordwire.Encode(body)
	if err != nil {
		return saveResponse{}, err
	}
	receipt, err := tx.ExecContext(ctx, `
		UPDATE board_candidate.idempotency_records
		SET state='succeeded',result_status=200,result_json=$1,completed_at=$2
		WHERE restore_generation=$3 AND user_id=$4 AND request_id=$5 AND state='pending'`,
		string(responseJSON), time.Now().Unix(), s.RestoreGeneration, actor.ID, input.RequestID)
	if err != nil {
		return saveResponse{}, err
	}
	if count, err := receipt.RowsAffected(); err != nil || count != 1 {
		return saveResponse{}, errors.New("daily receipt was not completed")
	}
	if err := tx.Commit(); err != nil {
		return saveResponse{}, err
	}
	return saveResponse{status: http.StatusOK, body: body}, nil
}

func (s Service) claim(ctx context.Context, tx *sql.Tx, actorID int64, input dailyRequest) (saveResponse, bool, error) {
	var generation, digest, state string
	var completed, status sql.NullInt64
	var result sql.NullString
	err := tx.QueryRowContext(ctx, `
		SELECT restore_generation,request_sha256,state,result_status,result_json,completed_at
		FROM board_candidate.idempotency_records
		WHERE user_id=$1 AND request_id=$2
		ORDER BY (restore_generation=$3) DESC LIMIT 1 FOR UPDATE`,
		actorID, input.RequestID, s.RestoreGeneration).Scan(&generation, &digest, &state, &status, &result, &completed)
	if errors.Is(err, sql.ErrNoRows) {
		_, err = tx.ExecContext(ctx, `
			INSERT INTO board_candidate.idempotency_records
				(restore_generation,user_id,request_id,action,request_sha256,state,created_at)
			VALUES($1,$2,$3,$4,$5,'pending',$6)`,
			s.RestoreGeneration, actorID, input.RequestID, dailyPath, input.Fingerprint, time.Now().Unix())
		if err == nil {
			err = tx.Commit()
		}
		return saveResponse{reserve: err == nil}, false, err
	}
	if err != nil {
		return saveResponse{}, false, err
	}
	if generation != s.RestoreGeneration {
		return saveResponse{}, false, requestError{http.StatusConflict, "请求属于已关闭的恢复代次，不能再次执行", "RESTORE_GENERATION_EXPIRED"}
	}
	if digest != input.Fingerprint {
		return saveResponse{}, false, requestError{http.StatusConflict, "同一请求标识不能用于不同内容", "REQUEST_ID_REUSED"}
	}
	if state == "pending" {
		return saveResponse{}, true, nil
	}
	if state != "succeeded" && state != "failed" {
		return saveResponse{}, false, requestError{http.StatusConflict, "请求标识状态不正确", "REQUEST_ID_REUSED"}
	}
	if !result.Valid || (completed.Valid && time.Since(time.Unix(completed.Int64, 0)) > resultRetention) {
		if result.Valid {
			_, err = tx.ExecContext(ctx, `
				UPDATE board_candidate.idempotency_records SET result_status=NULL,result_json=NULL
				WHERE restore_generation=$1 AND user_id=$2 AND request_id=$3`, generation, actorID, input.RequestID)
			if err != nil {
				return saveResponse{}, false, err
			}
			if err := tx.Commit(); err != nil {
				return saveResponse{}, false, err
			}
		}
		return saveResponse{}, false, requestError{http.StatusConflict, "该请求的结果已超过保留期，不能再次执行", "RESULT_EXPIRED"}
	}
	if !status.Valid || !json.Valid([]byte(result.String)) {
		return saveResponse{}, false, errors.New("stored daily receipt invalid")
	}
	return saveResponse{status: int(status.Int64), body: json.RawMessage(result.String)}, false, nil
}

func readAccount(ctx context.Context, tx *sql.Tx, actor session.User, id string) (string, sql.NullString, error) {
	record, office, err := account.ReadScoped(ctx, tx, actor, id)
	if errors.Is(err, sql.ErrNoRows) {
		return "", office, requestError{http.StatusForbidden, "无权访问此资料", ""}
	}
	if err != nil {
		return "", office, err
	}
	version, err := recordwire.Version(record)
	return version, office, err
}

func readDaily(ctx context.Context, tx *sql.Tx, input dailyRequest, office sql.NullString) (*dailyRecord, error) {
	record := dailyRecord{AccountID: input.ID, Day: input.Date, OfficeIDs: officeIDs(office)}
	err := tx.QueryRowContext(ctx, "SELECT published FROM board_candidate.daily WHERE account_id=$1 AND day=$2",
		input.ID, input.Date).Scan(&record.Published)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return &record, err
}

func writeDaily(ctx context.Context, tx *sql.Tx, actor session.User, prior *dailyRecord, next dailyRecord) error {
	_, err := tx.ExecContext(ctx, `
		INSERT INTO board_candidate.daily(account_id,day,published) VALUES($1,$2,$3)
		ON CONFLICT(account_id,day) DO UPDATE SET published=excluded.published`,
		next.AccountID, next.Day, next.Published)
	if err != nil {
		return err
	}
	now := time.Now().Unix()
	if prior == nil || prior.Published != next.Published {
		groupID := make([]byte, 16)
		if _, err := rand.Read(groupID); err != nil {
			return err
		}
		groupID[6] = (groupID[6] & 0x0f) | 0x40
		groupID[8] = (groupID[8] & 0x3f) | 0x80
		beforeJSON, err := recordwire.Encode(prior)
		if err != nil {
			return err
		}
		afterJSON, err := recordwire.Encode(next)
		if err != nil {
			return err
		}
		var scope any
		if len(next.OfficeIDs) > 0 {
			scopeJSON, err := recordwire.Encode(next.OfficeIDs)
			if err != nil {
				return err
			}
			scope = string(scopeJSON)
		}
		_, err = tx.ExecContext(ctx, `
			INSERT INTO board_candidate.operations_history
				(group_id,at,actor_id,actor_name,action,entity,before_json,after_json,scope_offices)
			VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)`,
			hex.EncodeToString(groupID), now, actor.ID, actor.Name, dailyPath, dailyKey(next.AccountID, next.Day),
			string(beforeJSON), string(afterJSON), scope)
		if err != nil {
			return err
		}
	}
	_, err = tx.ExecContext(ctx, `
		INSERT INTO board_candidate.auth_audit(at,actor_id,actor_name,action,target)
		VALUES($1,$2,$3,$4,'')`, now, actor.ID, actor.Name, dailyPath)
	return err
}
