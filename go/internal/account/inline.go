// Package account implements the workbench's narrow account read.
package account

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"
	"unicode/utf8"

	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

type Handler struct{ DB *sql.DB }

type inlineFields struct {
	ID     string  `json:"id"`
	Name   *string `json:"name"`
	Status *string `json:"status"`
	Note   *string `json:"note"`
}

type inlineResponse struct {
	Account       inlineFields `json:"account"`
	RecordVersion string       `json:"record_version"`
	Cursor        int64        `json:"cursor"`
}

func (h Handler) Inline(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		replyError(w, http.StatusMethodNotAllowed, "请求方法不正确")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	tx, err := h.DB.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
	if err != nil {
		replyError(w, http.StatusServiceUnavailable, "工作台暂时不可用")
		return
	}
	defer tx.Rollback()

	actor, err := session.AuthenticateWith(ctx, tx, r)
	if errors.Is(err, session.ErrUnauthorized) {
		replyError(w, http.StatusUnauthorized, "请先登录工作台")
		return
	}
	if err != nil {
		replyError(w, http.StatusServiceUnavailable, "工作台暂时不可用")
		return
	}
	id := accountID(r)
	if id == "" || utf8.RuneCountInString(id) > 500 || strings.ContainsRune(id, 0) {
		replyError(w, http.StatusBadRequest, "账号编号不正确")
		return
	}
	record, _, err := ReadScoped(ctx, tx, actor, id)
	if errors.Is(err, sql.ErrNoRows) {
		replyError(w, http.StatusForbidden, "无权访问此资料")
		return
	}
	if err != nil {
		replyError(w, http.StatusServiceUnavailable, "工作台暂时不可用")
		return
	}
	result, err := inlineRecord(record)
	if err == nil {
		err = tx.QueryRowContext(ctx, "SELECT COALESCE(MAX(id),0) FROM board_candidate.operations_history").
			Scan(&result.Cursor)
	}
	if err != nil || tx.Commit() != nil {
		replyError(w, http.StatusServiceUnavailable, "工作台暂时不可用")
		return
	}
	reply(w, http.StatusOK, result)
}

// ReadScoped reads an account and its office in the caller's transaction.
// It returns sql.ErrNoRows for both absent and inaccessible records.
func ReadScoped(ctx context.Context, tx *sql.Tx, actor session.User, id string) (map[string]any, sql.NullString, error) {
	var raw []byte
	var office sql.NullString
	err := tx.QueryRowContext(ctx, scopedAccountQuery, id, actor.Global(), actor.ID).Scan(&raw, &office)
	if err != nil {
		return nil, office, err
	}
	record, err := decodeRow(raw)
	if err != nil {
		return nil, office, err
	}
	offices := []string{}
	if office.Valid {
		offices = append(offices, office.String)
	}
	record["office_ids"] = offices
	return record, office, nil
}

func decodeRow(raw []byte) (map[string]any, error) {
	var record map[string]any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	err := decoder.Decode(&record)
	return record, err
}

const scopedAccountQuery = `
SELECT to_jsonb(a)::text, ao.office_id
FROM board_candidate.accounts AS a
LEFT JOIN board_candidate.account_offices AS ao ON ao.account_id = a.id
WHERE a.id = $1 AND ($2 OR EXISTS (
    SELECT 1 FROM board_candidate.user_office_grants AS g
    JOIN board_candidate.offices AS o ON o.id = g.office_id AND o.active = 1
    WHERE g.user_id = $3 AND g.office_id = ao.office_id
))
`

func inlineRecord(record map[string]any) (inlineResponse, error) {
	id, ok := record["id"].(string)
	if !ok {
		return inlineResponse{}, errors.New("account record is missing its identifier")
	}
	version, err := recordwire.Version(record)
	if err != nil {
		return inlineResponse{}, err
	}
	return inlineResponse{
		Account: inlineFields{
			ID:     id,
			Name:   nullableText(record["name"]),
			Status: nullableText(record["status"]),
			Note:   nullableText(record["note"]),
		},
		RecordVersion: version,
	}, nil
}

func nullableText(value any) *string {
	if text, ok := value.(string); ok {
		return &text
	}
	return nil
}

func accountID(r *http.Request) string {
	for _, id := range r.URL.Query()["id"] {
		if id != "" {
			return id
		}
	}
	return ""
}

func replyError(w http.ResponseWriter, status int, message string) {
	reply(w, status, map[string]string{"error": message})
}

func reply(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
