// Package accountwrite implements the existing admitted, idempotent write protocol.
package accountwrite

import (
	"context"
	"crypto/subtle"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net/http"
	"regexp"
	"strings"
	"time"

	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

type Service struct {
	DB                *sql.DB
	WriterEpoch       int64
	RestoreGeneration string
	PublicOrigin      string
}

type Authorize func(context.Context, *sql.Tx, session.User, map[string]any) error
type Mutate func(context.Context, *sql.Tx, session.User, map[string]any) (map[string]any, error)

type APIError struct {
	Status  int
	Message string
	Code    string
}

func (e APIError) Error() string { return e.Message }

func Invalid(message string) error   { return APIError{http.StatusBadRequest, message, ""} }
func Forbidden(message string) error { return APIError{http.StatusForbidden, message, ""} }
func Conflict(message string) error  { return APIError{http.StatusConflict, message, ""} }

var requestIDPattern = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

const resultRetentionSeconds = 30 * 24 * 60 * 60

func CheckVersions(body, actual map[string]any) error {
	if len(actual) == 0 {
		return nil
	}
	expected, ok := body["_expected_versions"].(map[string]any)
	if !ok {
		return Conflict("页面版本过旧，请刷新后重新操作；未保存任何修改")
	}
	for key, token := range actual {
		value := expected[key]
		if value != nil {
			if _, ok := value.(string); !ok {
				return Conflict("这条资料已被其他人修改，请刷新核对后再保存；未覆盖对方修改")
			}
		}
		if value != token {
			return Conflict("这条资料已被其他人修改，请刷新核对后再保存；未覆盖对方修改")
		}
	}
	return nil
}

// Handle reauthenticates, rechecks authorization and reserves a request before
// calling mutate under a savepoint. Mutate must leave transaction control here.
func (s Service) Handle(action, requiredRequestMessage string, authorize Authorize, mutate Mutate) http.HandlerFunc {
	return s.HandleLimit(action, requiredRequestMessage, 100000, authorize, mutate)
}

// HandleLimit allows the existing admitted write protocol to handle a bounded
// upload while keeping large request content out of the stored receipt.
func (s Service) HandleLimit(action, requiredRequestMessage string, maxBytes int64, authorize Authorize, mutate Mutate) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.Header().Set("Allow", http.MethodPost)
			writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "请求方法不正确"})
			return
		}
		body, err := s.readRequest(w, r, maxBytes)
		if err != nil {
			writeError(w, err)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
		defer cancel()
		for range 2 {
			result, err := s.apply(ctx, r, action, requiredRequestMessage, body, authorize, mutate)
			if err != nil {
				writeError(w, err)
				return
			}
			if !result.reserved {
				writeJSON(w, result.status, result.body)
				return
			}
		}
		writeError(w, errors.New("request was reserved twice"))
	}
}

func (s Service) readRequest(w http.ResponseWriter, r *http.Request, maxBytes int64) (map[string]any, error) {
	origin := strings.TrimRight(s.PublicOrigin, "/")
	if origin == "" {
		scheme := "http"
		if r.TLS != nil {
			scheme = "https"
		}
		origin = scheme + "://" + r.Host
	}
	if sent := r.Header.Get("Origin"); sent != "" && sent != origin {
		return nil, Forbidden("请求来源不正确")
	}
	media, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || media != "application/json" {
		return nil, Invalid("需要 JSON 请求")
	}
	if r.ContentLength == 0 || r.ContentLength > maxBytes {
		return nil, Invalid("请求过大或为空")
	}
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxBytes))
	decoder.UseNumber()
	var body map[string]any
	if err := decoder.Decode(&body); err != nil || body == nil {
		return nil, Invalid("请求内容应为对象")
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return nil, Invalid("请求内容应为对象")
	}
	return body, nil
}

// Direct is for authenticated queries using POST and for preview records that
// do not use a durable request ID. The callback shares the caller transaction.
func (s Service) Direct(maxBytes int64, readOnly bool, authorize Authorize, mutate Mutate) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.Header().Set("Allow", http.MethodPost)
			writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "请求方法不正确"})
			return
		}
		body, err := s.readRequest(w, r, maxBytes)
		if err != nil {
			writeError(w, err)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
		defer cancel()
		var tx *sql.Tx
		if readOnly {
			tx, err = s.DB.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
		} else {
			tx, err = session.BeginWrite(ctx, s.DB, s.WriterEpoch, s.RestoreGeneration)
		}
		if err != nil {
			writeError(w, err)
			return
		}
		defer tx.Rollback()
		actor, err := authorizeRequest(ctx, tx, r, body, authorize)
		var result map[string]any
		if err == nil {
			result, err = mutate(ctx, tx, actor, body)
		}
		if err == nil {
			err = tx.Commit()
		}
		if err != nil {
			writeError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, result)
	}
}

type receipt struct {
	reserved bool
	status   int
	body     any
}

func (s Service) apply(ctx context.Context, r *http.Request, action, requiredRequestMessage string, body map[string]any, authorize Authorize, mutate Mutate) (receipt, error) {
	tx, err := session.BeginWrite(ctx, s.DB, s.WriterEpoch, s.RestoreGeneration)
	if err != nil {
		return receipt{}, err
	}
	defer tx.Rollback()
	actor, err := authorizeRequest(ctx, tx, r, body, authorize)
	if err != nil {
		return receipt{}, err
	}
	requestID, _ := body["request_id"].(string)
	if requestID == "" && (body["request_id"] == nil || body["request_id"] == "") {
		return receipt{}, APIError{http.StatusBadRequest, requiredRequestMessage, "REQUEST_ID_REQUIRED"}
	}
	if !requestIDPattern.MatchString(requestID) {
		return receipt{}, Invalid("请求标识不正确")
	}
	claimed, proceed, err := s.claim(ctx, tx, actor.ID, action, requestID, body)
	if err != nil || !proceed {
		return claimed, err
	}
	if _, err := tx.ExecContext(ctx, "SAVEPOINT idem_business"); err != nil {
		return receipt{}, err
	}
	result, err := mutate(ctx, tx, actor, body)
	status, state := http.StatusOK, "succeeded"
	if err != nil {
		var failure APIError
		if !errors.As(err, &failure) || failure.Status != http.StatusBadRequest {
			return receipt{}, err
		}
		if _, err := tx.ExecContext(ctx, "ROLLBACK TO SAVEPOINT idem_business"); err != nil {
			return receipt{}, err
		}
		status, state = failure.Status, "failed"
		result = map[string]any{"error": failure.Message}
		if failure.Code != "" {
			result["code"] = failure.Code
		}
	}
	if err := s.complete(ctx, tx, actor.ID, requestID, state, status, result); err != nil {
		return receipt{}, err
	}
	if err := tx.Commit(); err != nil {
		return receipt{}, err
	}
	return receipt{status: status, body: result}, nil
}

func authorizeRequest(ctx context.Context, tx *sql.Tx, r *http.Request, body map[string]any, authorize Authorize) (session.User, error) {
	actor, err := session.AuthenticateWith(ctx, tx, r)
	if err != nil {
		return actor, err
	}
	if csrf := r.Header.Get("X-CSRF-Token"); csrf == "" || subtle.ConstantTimeCompare([]byte(csrf), []byte(actor.CSRF)) != 1 {
		return actor, Forbidden("页面已过期，请刷新后重试")
	}
	return actor, authorize(ctx, tx, actor, body)
}

func (s Service) claim(ctx context.Context, tx *sql.Tx, actorID int64, action, requestID string, body map[string]any) (receipt, bool, error) {
	payload := make(map[string]any, len(body)-1)
	for key, value := range body {
		if key != "request_id" {
			payload[key] = value
		}
	}
	digest, err := recordwire.Version(map[string]any{"action": action, "body": payload})
	if err != nil {
		return receipt{}, false, err
	}
	stored, err := s.lookup(ctx, tx, actorID, requestID, true)
	if errors.Is(err, sql.ErrNoRows) {
		_, err = tx.ExecContext(ctx, `
INSERT INTO board_candidate.idempotency_records
    (restore_generation,user_id,request_id,action,request_sha256,state,created_at)
VALUES($1,$2,$3,$4,$5,'pending',$6)`, s.RestoreGeneration, actorID, requestID, action, digest, time.Now().Unix())
		if err == nil {
			err = tx.Commit()
		}
		return receipt{reserved: err == nil}, false, err
	}
	if err != nil {
		return receipt{}, false, err
	}
	if stored.generation != s.RestoreGeneration {
		return receipt{}, false, expiredGeneration()
	}
	if stored.digest != digest || stored.action != action {
		return receipt{}, false, APIError{http.StatusConflict, "同一请求标识不能用于不同内容", "REQUEST_ID_REUSED"}
	}
	if stored.state == "pending" {
		return receipt{}, true, nil
	}
	if stored.state != "succeeded" && stored.state != "failed" {
		return receipt{}, false, APIError{http.StatusConflict, "请求标识状态不正确", "REQUEST_ID_REUSED"}
	}
	if stored.expired() {
		if stored.result.Valid {
			_, err = tx.ExecContext(ctx, `
UPDATE board_candidate.idempotency_records SET result_status=NULL,result_json=NULL
WHERE restore_generation=$1 AND user_id=$2 AND request_id=$3`, stored.generation, actorID, requestID)
			if err == nil {
				err = tx.Commit()
			}
			if err != nil {
				return receipt{}, false, err
			}
		}
		return receipt{}, false, expiredResult()
	}
	if !stored.status.Valid || !json.Valid([]byte(stored.result.String)) {
		return receipt{}, false, errors.New("stored write receipt is invalid")
	}
	return receipt{status: int(stored.status.Int64), body: json.RawMessage(stored.result.String)}, false, nil
}

func (s Service) complete(ctx context.Context, tx *sql.Tx, actorID int64, requestID, state string, status int, result map[string]any) error {
	if result == nil {
		return errors.New("empty write receipt")
	}
	for _, key := range []string{"password", "password_hash", "csrf", "token", "cookie", "content", "image", "bytes"} {
		if _, exists := result[key]; exists {
			return errors.New("write receipt contains a prohibited field")
		}
	}
	encoded, err := recordwire.Encode(result)
	if err != nil {
		return err
	}
	updated, err := tx.ExecContext(ctx, `
UPDATE board_candidate.idempotency_records
SET state=$1,result_status=$2,result_json=$3,completed_at=$4
WHERE restore_generation=$5 AND user_id=$6 AND request_id=$7 AND state='pending'`, state, status, string(encoded), time.Now().Unix(), s.RestoreGeneration, actorID, requestID)
	if err != nil {
		return err
	}
	if count, err := updated.RowsAffected(); err != nil || count != 1 {
		return APIError{http.StatusConflict, "请求标识已结束，不能重复写入", "REQUEST_ID_REUSED"}
	}
	return nil
}

func writeError(w http.ResponseWriter, err error) {
	var failure APIError
	var admission session.AdmissionError
	switch {
	case errors.As(err, &failure):
	case errors.Is(err, session.ErrUnauthorized):
		failure = APIError{http.StatusUnauthorized, "请先登录工作台", ""}
	case errors.As(err, &admission):
		status := http.StatusServiceUnavailable
		if admission.Code == "WRITER_VERSION_RETIRED" || admission.Code == "RESTORE_GENERATION_EXPIRED" {
			status = http.StatusConflict
		}
		failure = APIError{status, "当前运行状态不允许写入，请刷新后重试", admission.Code}
	default:
		failure = APIError{http.StatusServiceUnavailable, "工作台暂时不可用", ""}
	}
	body := map[string]string{"error": failure.Message}
	if failure.Code != "" {
		body["code"] = failure.Code
	}
	writeJSON(w, failure.Status, body)
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
