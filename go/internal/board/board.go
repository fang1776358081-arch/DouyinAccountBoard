// Package board reads scoped workbench snapshots from PostgreSQL.
package board

import (
	"bytes"
	"compress/gzip"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"

	"douyinboard/internal/inventory"
	"douyinboard/internal/session"
)

type Handler struct {
	DB                  *sql.DB
	BackupStatus        func() map[string]any
	CollectionJobPrefix string
}

type row = map[string]any

var boardStatuses = []string{"正常", "养号7天", "短期封禁", "封禁", "发不了", "停用", "待确认"}
var china = time.FixedZone("CST", 8*60*60)

func (h Handler) Board(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()
	tx, actor, err := h.beginRead(ctx, r)
	if err != nil {
		readFailure(w, err)
		return
	}
	defer tx.Rollback()

	today := time.Now().In(china).Format(time.DateOnly)
	view, compact, versionScope, day := queryValue(r, "view"), queryValue(r, "compact"), queryValue(r, "version_scope"), queryValue(r, "date")
	switch {
	case view != "" && view != "work" && view != "detail":
		fail(w, http.StatusBadRequest, "读取范围不正确")
		return
	case (compact != "" && compact != "accounts") || (compact != "" && view != ""):
		fail(w, http.StatusBadRequest, "传输范围不正确")
		return
	case versionScope != "" && versionScope != "day":
		fail(w, http.StatusBadRequest, "版本读取范围不正确")
		return
	}
	if day == "" {
		day = today
	}
	if _, ok := parseDay(day); !ok {
		fail(w, http.StatusBadRequest, "日期格式不正确")
		return
	}

	result, err := snapshot(ctx, tx, actor, day, today, view, versionScope, compact != "", h.CollectionJobPrefix)
	if err != nil {
		readFailure(w, err)
		return
	}
	if err := tx.Commit(); err != nil {
		readFailure(w, err)
		return
	}
	if view == "" && h.BackupStatus != nil {
		result["backup"] = h.BackupStatus()
	}
	WriteNegotiatedJSON(w, r, result)
}

func (h Handler) beginRead(ctx context.Context, r *http.Request) (*sql.Tx, session.User, error) {
	tx, err := h.DB.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
	if err != nil {
		return nil, session.User{}, err
	}
	actor, err := session.AuthenticateWith(ctx, tx, r)
	if err != nil {
		_ = tx.Rollback()
		return nil, session.User{}, err
	}
	return tx, actor, nil
}

func snapshot(ctx context.Context, tx *sql.Tx, actor session.User, day, today, view, versionScope string, compact bool, collectionJobPrefix string) (row, error) {
	scope, err := loadScope(ctx, tx, actor)
	if err != nil {
		return nil, err
	}
	computers, accounts, err := boardEntities(ctx, tx, scope, day)
	if err != nil {
		return nil, err
	}
	enrichLifecycle(accounts, today)
	cards, err := inventory.Cards(ctx, tx, actor, accounts)
	if err != nil {
		return nil, err
	}
	devices, err := inventory.Devices(ctx, tx, actor, accounts)
	if err != nil {
		return nil, err
	}
	workflow, err := workflowOverview(ctx, tx, scope, accounts, entityRows(devices, "devices"), entityRows(cards, "cards"))
	if err != nil {
		return nil, err
	}
	layout, err := publishingLayout(ctx, tx, scope)
	if err != nil {
		return nil, err
	}
	nurture, err := nurtureOverview(ctx, tx, scope, accounts, today, view != "work")
	if err != nil {
		return nil, err
	}
	offices, err := selectRows(ctx, tx, `SELECT to_jsonb(o) FROM board_candidate.offices o
		WHERE $1 OR o.id=ANY($2::text[]) ORDER BY o.name,o.id`, actor.Global(), actor.OfficeIDs)
	if err != nil {
		return nil, err
	}
	var cursor int64
	if err := tx.QueryRowContext(ctx, `SELECT COALESCE(MAX(id),0) FROM board_candidate.operations_history`).Scan(&cursor); err != nil {
		return nil, err
	}
	result := row{
		"computers": computers, "accounts": accounts, "date": day, "today": today,
		"phone_cards": cards, "phone_devices": devices, "device_workflow": workflow, "user": actor,
		"publishing_layout": layout, "nurture": nurture, "record_versions": row{}, "offices": offices, "cursor": cursor,
	}
	if view == "work" {
		applySummary(result)
		return result, nil
	}
	result["statuses"] = boardStatuses
	if view == "detail" {
		result["workspace_view"] = "detail"
		return result, nil
	}
	collection, err := collectionOverview(ctx, tx, scope, accounts, collectionJobPrefix)
	if err != nil {
		return nil, err
	}
	if versionScope == "" {
		day = ""
	}
	versions, err := boardVersions(ctx, tx, scope, day)
	if err != nil {
		return nil, err
	}
	result["collection"] = collection
	result["backup"] = row{"enabled": false, "last_backup": "", "error": "", "directory": ""}
	result["record_versions"] = versions
	if compact {
		applyCompact(result)
	}
	return result, nil
}

func selectRows(ctx context.Context, tx *sql.Tx, query string, args ...any) ([]row, error) {
	rows, err := tx.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make([]row, 0)
	for rows.Next() {
		var raw []byte
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		var item row
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.UseNumber()
		if err := decoder.Decode(&item); err != nil {
			return nil, err
		}
		out = append(out, item)
	}
	return out, rows.Err()
}

func str(value any) string {
	switch value := value.(type) {
	case string:
		return value
	case json.Number:
		return value.String()
	case int64:
		return strconv.FormatInt(value, 10)
	case int:
		return strconv.Itoa(value)
	default:
		return ""
	}
}

func number(value any) int64 {
	parsed, _ := strconv.ParseInt(str(value), 10, 64)
	return parsed
}

func parseDay(value string) (time.Time, bool) {
	parsed, err := time.Parse(time.DateOnly, value)
	return parsed, err == nil && parsed.Year() > 0
}

func queryValue(r *http.Request, key string) string {
	for _, value := range r.URL.Query()[key] {
		if value != "" {
			return value
		}
	}
	return ""
}

func entityRows(object row, key string) []row {
	value, _ := object[key].([]row)
	return value
}

func picked(value row, keys ...string) row {
	result := make(row, len(keys))
	for _, key := range keys {
		if v, ok := value[key]; ok {
			result[key] = v
		}
	}
	return result
}

func pickRows(values []row, keys ...string) []row {
	result := make([]row, 0, len(values))
	for _, value := range values {
		result = append(result, picked(value, keys...))
	}
	return result
}

func readFailure(w http.ResponseWriter, err error) {
	if errors.Is(err, session.ErrUnauthorized) {
		fail(w, http.StatusUnauthorized, "请先登录工作台")
		return
	}
	reply(w, http.StatusServiceUnavailable, row{"error": "数据服务暂时不可用，请稍后刷新核对操作结果", "code": "DB_UNAVAILABLE"})
}

func fail(w http.ResponseWriter, status int, message string) { reply(w, status, row{"error": message}) }

func reply(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

// WriteNegotiatedJSON sends a successful no-store JSON read with negotiated gzip.
func WriteNegotiatedJSON(w http.ResponseWriter, r *http.Request, result any) {
	var encoded bytes.Buffer
	encoder := json.NewEncoder(&encoded)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(result); err != nil {
		fail(w, http.StatusInternalServerError, "页面数据暂时读不出来，请稍后刷新")
		return
	}
	data := encoded.Bytes()
	w.Header().Set("Vary", "Accept-Encoding")
	gzipQuality, identityQuality := encodingQuality(strings.Join(r.Header.Values("Accept-Encoding"), ","))
	if gzipQuality > 0 && gzipQuality >= identityQuality && (len(data) >= 1024 || identityQuality == 0) {
		var compressed bytes.Buffer
		writer, _ := gzip.NewWriterLevel(&compressed, 4)
		_, err := writer.Write(data)
		if closeErr := writer.Close(); err == nil && closeErr == nil && (compressed.Len() < len(data) || identityQuality == 0) {
			data = compressed.Bytes()
			w.Header().Set("Content-Encoding", "gzip")
		}
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	if identityQuality == 0 && w.Header().Get("Content-Encoding") == "" {
		w.Header().Set("Content-Length", "0")
		w.WriteHeader(http.StatusNotAcceptable)
		return
	}
	w.Header().Set("Content-Length", strconv.Itoa(len(data)))
	_, _ = w.Write(data)
}

func encodingQuality(header string) (gzip, identity int) {
	weights := map[string]int{}
	for _, item := range strings.Split(header, ",") {
		pieces := strings.Split(item, ";")
		coding := strings.ToLower(strings.TrimSpace(pieces[0]))
		if coding != "gzip" && coding != "identity" && coding != "*" {
			continue
		}
		weight := 1000
		if len(pieces) > 1 {
			weight = 0
			name, value, ok := strings.Cut(strings.TrimSpace(pieces[1]), "=")
			if len(pieces) == 2 && ok && strings.EqualFold(strings.TrimSpace(name), "q") {
				weight = qualityValue(strings.TrimSpace(value))
			}
		}
		if old, ok := weights[coding]; !ok || weight < old {
			weights[coding] = weight
		}
	}
	gzip, ok := weights["gzip"]
	if !ok {
		gzip = weights["*"]
	}
	identity, ok = weights["identity"]
	if !ok {
		if wildcard, exists := weights["*"]; !exists || wildcard > 0 {
			identity = 1000
		}
	}
	return gzip, identity
}

func qualityValue(raw string) int {
	whole, fraction, _ := strings.Cut(raw, ".")
	if (whole != "0" && whole != "1") || len(fraction) > 3 {
		return 0
	}
	for _, digit := range fraction {
		if digit < '0' || digit > '9' || (whole == "1" && digit != '0') {
			return 0
		}
	}
	if whole == "1" {
		return 1000
	}
	weight, _ := strconv.Atoi(fraction + strings.Repeat("0", 3-len(fraction)))
	return weight
}
