package gateway

import (
	"bytes"
	"context"
	"database/sql"
	_ "embed"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strconv"
	"time"

	"douyinboard/internal/realtime"
	"douyinboard/internal/session"
	"douyinboard/internal/webassets"
)

//go:embed browser-sync.js
var browserSync []byte

const updateLimit = 200

type Config struct {
	LegacyOrigin *url.URL
	PublicOrigin string
	Database     *sql.DB
}

func New(cfg Config) *http.ServeMux {
	mux := http.NewServeMux()
	transport := &http.Transport{
		DialContext:           (&net.Dialer{Timeout: 3 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		MaxIdleConns:          32,
		MaxIdleConnsPerHost:   32,
		IdleConnTimeout:       60 * time.Second,
		ResponseHeaderTimeout: 120 * time.Second,
	}
	legacy := legacyReader{target: cfg.LegacyOrigin, transport: transport}
	proxy := &httputil.ReverseProxy{
		Rewrite: func(request *httputil.ProxyRequest) {
			request.SetURL(cfg.LegacyOrigin)
			// The Python application checks browser Origin against the external Host.
			request.Out.Host = request.In.Host
			if request.Out.URL.Path == "/live-sync.js" {
				for _, header := range []string{"If-None-Match", "If-Modified-Since", "If-Match", "If-Unmodified-Since", "If-Range", "Range"} {
					request.Out.Header.Del(header)
				}
				request.Out.Header.Set("Accept-Encoding", "identity")
			}
		},
		Transport:      transport,
		ModifyResponse: installBrowserSync,
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, _ error) {
			writeJSON(w, http.StatusBadGateway, map[string]any{"error": "现有工作台接口暂不可用"})
		},
	}
	authenticate, load := legacy.authenticate, legacy.load
	if cfg.Database != nil {
		native := postgresFeed{db: cfg.Database}
		authenticate, load = native.authenticate, native.load
	}
	mux.Handle("GET /api/go/live", realtime.Handler(authenticate, load, realtime.Options{
		PublicOrigin: cfg.PublicOrigin, PollInterval: 2 * time.Second,
		OperationTimeout: 4 * time.Second, MaxConnections: 64,
	}))
	mux.HandleFunc("GET /api/go/health", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"service": "douyin-board-go", "websocket": true, "postgres_feed": cfg.Database != nil, "standalone": cfg.LegacyOrigin == nil})
	})
	if cfg.LegacyOrigin == nil {
		mux.Handle("/", webassets.Handler(browserSync))
	} else {
		mux.Handle("/", proxy)
	}
	return mux
}

type combinedBody struct {
	io.Reader
	io.Closer
}

func installBrowserSync(response *http.Response) error {
	if response.Request.Method != http.MethodGet || response.Request.URL.Path != "/live-sync.js" || response.StatusCode != http.StatusOK {
		return nil
	}
	encoding := response.Header.Get("Content-Encoding")
	if encoding != "" && encoding != "identity" {
		return errors.New("unexpected encoding for live-sync.js")
	}
	original := response.Body
	response.Body = combinedBody{Reader: io.MultiReader(original, bytes.NewReader([]byte("\n")), bytes.NewReader(browserSync)), Closer: original}
	for _, header := range []string{"Content-Length", "Content-Encoding", "Content-MD5", "Digest", "ETag", "Last-Modified", "Accept-Ranges"} {
		response.Header.Del(header)
	}
	response.Header.Set("Cache-Control", "no-store")
	response.ContentLength = -1
	return nil
}

type legacyReader struct {
	target    *url.URL
	transport http.RoundTripper
}

func (legacy legacyReader) get(ctx context.Context, original *http.Request, path string, values url.Values, destination any) error {
	target := *legacy.target
	target.Path, target.RawQuery = path, values.Encode()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, target.String(), nil)
	if err != nil {
		return err
	}
	request.Host = original.Host
	for _, cookie := range original.Header.Values("Cookie") {
		request.Header.Add("Cookie", cookie)
	}
	response, err := legacy.transport.RoundTrip(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden {
		return realtime.ErrUnauthorized
	}
	if response.StatusCode != http.StatusOK {
		return errors.New("legacy synchronization request failed")
	}
	return json.NewDecoder(io.LimitReader(response.Body, 4<<20)).Decode(destination)
}

func (legacy legacyReader) authenticate(request *http.Request) error {
	var result struct {
		User json.RawMessage `json:"user"`
	}
	if err := legacy.get(request.Context(), request, "/api/auth/me", nil, &result); err != nil {
		return err
	}
	if len(result.User) == 0 || bytes.Equal(result.User, []byte("null")) {
		return realtime.ErrUnauthorized
	}
	return nil
}

func (legacy legacyReader) load(ctx context.Context, request *http.Request, cursor int64) (realtime.Update, error) {
	var result realtime.Update
	query := url.Values{"cursor": {strconv.FormatInt(cursor, 10)}, "limit": {strconv.Itoa(updateLimit)}}
	if err := legacy.get(ctx, request, "/api/board-updates", query, &result); err != nil {
		return realtime.Update{}, err
	}
	// Use a wake-up hint. The browser applies the original, scope-checked HTTP feed.
	result.Changed = len(result.Changes) != 0
	result.Changes = nil
	return result, nil
}

type postgresFeed struct {
	db *sql.DB
}

func (feed postgresFeed) authenticate(request *http.Request) error {
	_, err := (session.Store{DB: feed.db}).Authenticate(request.Context(), request)
	if errors.Is(err, session.ErrUnauthorized) {
		return realtime.ErrUnauthorized
	}
	return err
}

func (feed postgresFeed) load(ctx context.Context, request *http.Request, cursor int64) (realtime.Update, error) {
	tx, err := feed.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
	if err != nil {
		return realtime.Update{}, err
	}
	defer tx.Rollback()
	user, err := session.AuthenticateWith(ctx, tx, request)
	if errors.Is(err, session.ErrUnauthorized) {
		return realtime.Update{}, realtime.ErrUnauthorized
	}
	if err != nil {
		return realtime.Update{}, err
	}
	var latest int64
	var known bool
	err = tx.QueryRowContext(ctx, `SELECT COALESCE(MAX(id), 0),
		($1 = 0 OR EXISTS(SELECT 1 FROM board_candidate.operations_history WHERE id=$1))
		FROM board_candidate.operations_history`, cursor).Scan(&latest, &known)
	if err != nil {
		return realtime.Update{}, err
	}
	update := realtime.Update{Cursor: latest}
	if !known {
		update.Reload = true
		return update, nil
	}
	if latest == cursor {
		return update, nil
	}
	rows, err := tx.QueryContext(ctx, `SELECT entity, scope_offices,
		CASE WHEN entity='sync:office' THEN after_json ELSE NULL END
		FROM board_candidate.operations_history WHERE id>$1 ORDER BY id LIMIT $2`, cursor, updateLimit+1)
	if err != nil {
		return realtime.Update{}, err
	}
	defer rows.Close()
	count := 0
	for rows.Next() {
		var entity string
		var offices, move sql.NullString
		if err := rows.Scan(&entity, &offices, &move); err != nil {
			return realtime.Update{}, err
		}
		count++
		if count > updateLimit || entity == "sync:reload" {
			update.Reload = true
			return update, nil
		}
		update.Changed = update.Changed || visibleChange(user, entity, offices.String, move.String)
	}
	return update, rows.Err()
}

func visibleChange(user session.User, entity, offices, movement string) bool {
	if user.Global() {
		return true
	}
	var affected []string
	if entity == "sync:office" {
		var move struct {
			Previous []string `json:"previous"`
			Next     []string `json:"next"`
		}
		if json.Unmarshal([]byte(movement), &move) != nil {
			return false
		}
		affected = append(move.Previous, move.Next...)
	} else {
		var scope []any
		if json.Unmarshal([]byte(offices), &scope) != nil {
			return false
		}
		for _, entry := range scope {
			office, ok := entry.(string)
			if !ok {
				return false
			}
			affected = append(affected, office)
		}
	}
	for _, granted := range user.OfficeIDs {
		for _, changed := range affected {
			if granted == changed {
				return true
			}
		}
	}
	return false
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(payload); err != nil {
		slog.Debug("response write failed")
	}
}
