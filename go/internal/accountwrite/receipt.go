package accountwrite

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"

	"douyinboard/internal/session"
)

type storedReceipt struct {
	generation, action, digest, state string
	status, completed                 sql.NullInt64
	result                            sql.NullString
}

func (r storedReceipt) expired() bool {
	return !r.result.Valid || r.completed.Valid && time.Now().Unix()-r.completed.Int64 > resultRetentionSeconds
}

func (s Service) lookup(ctx context.Context, tx *sql.Tx, actorID int64, requestID string, lock bool) (storedReceipt, error) {
	query := `SELECT restore_generation,action,request_sha256,state,result_status,result_json,completed_at
FROM board_candidate.idempotency_records WHERE user_id=$1 AND request_id=$2
ORDER BY (restore_generation=$3) DESC LIMIT 1`
	if lock {
		query += " FOR UPDATE"
	}
	var found storedReceipt
	err := tx.QueryRowContext(ctx, query, actorID, requestID, s.RestoreGeneration).Scan(
		&found.generation, &found.action, &found.digest, &found.state, &found.status, &found.result, &found.completed)
	return found, err
}

func expiredGeneration() APIError {
	return APIError{http.StatusConflict, "请求属于已关闭的恢复代次，不能再次执行", "RESTORE_GENERATION_EXPIRED"}
}

func expiredResult() APIError {
	return APIError{http.StatusConflict, "该请求的结果已超过保留期，不能再次执行", "RESULT_EXPIRED"}
}

// PermitReceipt checks the original action and any account or office data
// returned by the receipt against the reader's current authorization.
type PermitReceipt func(context.Context, *sql.Tx, session.User, string, map[string]any) error

func (s Service) RequestStatus(permit PermitReceipt) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
		defer cancel()
		body, status, err := s.readReceipt(ctx, r, permit)
		if err != nil {
			writeError(w, err)
			return
		}
		writeJSON(w, status, body)
	}
}

func (s Service) readReceipt(ctx context.Context, r *http.Request, permit PermitReceipt) (map[string]any, int, error) {
	tx, err := s.DB.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
	if err != nil {
		return nil, 0, err
	}
	defer tx.Rollback()
	actor, err := session.AuthenticateWith(ctx, tx, r)
	if err != nil {
		return nil, 0, err
	}
	var requestID string
	for _, value := range r.URL.Query()["request_id"] {
		if value != "" {
			requestID = value
			break
		}
	}
	if !requestIDPattern.MatchString(requestID) {
		return nil, 0, Invalid("请求标识不正确")
	}
	var generation string
	if err := tx.QueryRowContext(ctx, "SELECT restore_generation FROM board_candidate.runtime_control WHERE id=1").Scan(&generation); err != nil {
		return nil, 0, err
	}
	if generation != s.RestoreGeneration {
		return nil, 0, expiredGeneration()
	}
	stored, err := s.lookup(ctx, tx, actor.ID, requestID, false)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, 0, APIError{http.StatusNotFound, "没有这条请求记录", "REQUEST_NOT_FOUND"}
	}
	if err != nil {
		return nil, 0, err
	}
	if permit == nil {
		return nil, 0, Forbidden("当前账号没有此操作权限")
	}
	var body map[string]any
	isExpired := stored.expired()
	if stored.generation == s.RestoreGeneration && !isExpired && (stored.state == "succeeded" || stored.state == "failed") {
		decoder := json.NewDecoder(strings.NewReader(stored.result.String))
		decoder.UseNumber()
		if !stored.status.Valid || decoder.Decode(&body) != nil || body == nil {
			return nil, 0, errors.New("stored write receipt is invalid")
		}
	}
	if err := permit(ctx, tx, actor, stored.action, body); err != nil {
		return nil, 0, err
	}
	switch {
	case stored.generation != s.RestoreGeneration:
		return nil, 0, expiredGeneration()
	case stored.state == "pending":
		return nil, 0, APIError{http.StatusConflict, "结果待确认", "REQUEST_PENDING"}
	case stored.state != "succeeded" && stored.state != "failed":
		return nil, 0, APIError{http.StatusConflict, "请求标识状态不正确", "REQUEST_ID_REUSED"}
	case isExpired:
		return nil, 0, expiredResult()
	}
	status := int(stored.status.Int64)
	if status == http.StatusOK {
		body["code"] = "REQUEST_SUCCEEDED"
	} else {
		body["code"] = "REQUEST_FAILED"
	}
	return body, status, tx.Commit()
}
