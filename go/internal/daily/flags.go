// Package daily implements the shared board's PostgreSQL daily publication API.
package daily

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
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

type flagsResponse struct {
	Date           string              `json:"date"`
	Published      map[string]bool     `json:"published"`
	RecordVersions *map[string]*string `json:"record_versions,omitempty"`
}

type dailyRecord struct {
	AccountID string   `json:"account_id"`
	Day       string   `json:"day"`
	OfficeIDs []string `json:"office_ids"`
	Published int64    `json:"published"`
}

var beijing = time.FixedZone("CST", 8*60*60)

func validDay(day string) bool {
	parsed, err := time.Parse(time.DateOnly, day)
	return err == nil && parsed.Year() >= 1
}

func firstQuery(req *http.Request, name string) string {
	for _, value := range req.URL.Query()[name] {
		if value != "" {
			return value
		}
	}
	return ""
}

func (s Service) Flags(w http.ResponseWriter, r *http.Request) {
	day := firstQuery(r, "date")
	if day == "" {
		day = time.Now().In(beijing).Format(time.DateOnly)
	}
	if !validDay(day) {
		writeError(w, http.StatusBadRequest, "日期格式不正确", "")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()
	tx, err := s.DB.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
	if err != nil {
		databaseError(w, err, "")
		return
	}
	defer tx.Rollback()
	actor, err := session.AuthenticateWith(ctx, tx, r)
	if !writeAuthError(w, err, "") {
		return
	}
	mode := firstQuery(r, "versions")
	if mode != "" && mode != "1" {
		writeError(w, http.StatusBadRequest, "版本读取范围不正确", "")
		return
	}
	out, err := loadFlags(ctx, tx, actor, day, mode == "1")
	if err != nil {
		databaseError(w, err, "")
		return
	}
	if err := tx.Commit(); err != nil {
		databaseError(w, err, "")
		return
	}
	writeJSON(w, http.StatusOK, out)
}

func loadFlags(ctx context.Context, tx *sql.Tx, actor session.User, day string, versions bool) (flagsResponse, error) {
	const query = `
		SELECT a.id, ao.office_id, d.published
		FROM board_candidate.accounts AS a
		LEFT JOIN board_candidate.account_offices AS ao ON ao.account_id = a.id
		LEFT JOIN board_candidate.daily AS d ON d.account_id = a.id AND d.day = $1
		WHERE $2 OR EXISTS (
			SELECT 1 FROM board_candidate.user_office_grants AS g
			JOIN board_candidate.offices AS o ON o.id = g.office_id AND o.active = 1
			WHERE g.user_id = $3 AND g.office_id = ao.office_id
		)`
	out := flagsResponse{Date: day, Published: make(map[string]bool)}
	var recordVersions map[string]*string
	if versions {
		recordVersions = make(map[string]*string)
		out.RecordVersions = &recordVersions
	}
	rows, err := tx.QueryContext(ctx, query, day, actor.Global(), actor.ID)
	if err != nil {
		return out, err
	}
	defer rows.Close()
	for rows.Next() {
		var id string
		var office sql.NullString
		var published sql.NullInt64
		if err := rows.Scan(&id, &office, &published); err != nil {
			return out, err
		}
		out.Published[id] = published.Int64 != 0
		if versions {
			key := dailyKey(id, day)
			if !published.Valid {
				recordVersions[key] = nil
				continue
			}
			version, err := recordwire.Version(dailyRecord{id, day, officeIDs(office), published.Int64})
			if err != nil {
				return out, err
			}
			recordVersions[key] = &version
		}
	}
	return out, rows.Err()
}

func dailyKey(accountID, day string) string {
	return "daily:" + accountID + ":" + day
}

func officeIDs(office sql.NullString) []string {
	if office.Valid {
		return []string{office.String}
	}
	return []string{}
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

func writeError(w http.ResponseWriter, status int, message, code string) {
	out := struct {
		Error string `json:"error"`
		Code  string `json:"code,omitempty"`
	}{message, code}
	writeJSON(w, status, out)
}

func writeAuthError(w http.ResponseWriter, err error, requestID string) bool {
	if err == nil {
		return true
	}
	if errors.Is(err, session.ErrUnauthorized) {
		writeError(w, http.StatusUnauthorized, "请先登录工作台", "")
	} else {
		databaseError(w, err, requestID)
	}
	return false
}

func databaseError(w http.ResponseWriter, err error, requestID string) {
	code := "DB_UNAVAILABLE"
	var state interface{ SQLState() string }
	if errors.As(err, &state) {
		switch state.SQLState() {
		case "55P03":
			code = "DB_BUSY"
		case "57014":
			code = "DB_TIMEOUT"
		}
	} else if errors.Is(err, context.DeadlineExceeded) {
		code = "DB_TIMEOUT"
	}
	result := struct {
		Error     string `json:"error"`
		Code      string `json:"code"`
		RequestID string `json:"request_id,omitempty"`
	}{"数据服务暂时不可用，请稍后刷新核对操作结果", code, requestID}
	writeJSON(w, http.StatusServiceUnavailable, result)
}
