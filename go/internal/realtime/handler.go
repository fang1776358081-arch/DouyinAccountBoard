// Package realtime delivers authorized board updates over a read-only WebSocket.
package realtime

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/coder/websocket"
)

const maxBrowserCursor = 1<<53 - 1

// ErrUnauthorized indicates a missing, expired or revoked session.
var ErrUnauthorized = errors.New("realtime: unauthorized")

// Update follows the board-updates response format. Changed can signal an
// authorized change without including account details in the WebSocket frame.
type Update struct {
	Cursor  int64             `json:"cursor"`
	Reload  bool              `json:"reload,omitempty"`
	Changed bool              `json:"changed,omitempty"`
	Changes []json.RawMessage `json:"changes"`
}

type Options struct {
	// PublicOrigin is the exact browser origin when a reverse proxy terminates TLS.
	// The server never derives it from client-supplied forwarding headers.
	PublicOrigin     string
	PollInterval     time.Duration
	PingInterval     time.Duration
	OperationTimeout time.Duration
	MaxConnections   int
	MaxMessageBytes  int
}

// Handler accepts authenticated same-origin subscriptions. The load callback
// must revalidate the session and access in its own read transaction.
// The caller's data source, rather than the WebSocket, is the durable cursor.
func Handler(
	authenticate func(*http.Request) error,
	load func(context.Context, *http.Request, int64) (Update, error),
	opts Options,
) http.Handler {
	if opts.PollInterval <= 0 {
		opts.PollInterval = 2 * time.Second
	}
	if opts.PingInterval <= 0 {
		opts.PingInterval = 25 * time.Second
	}
	if opts.OperationTimeout <= 0 {
		opts.OperationTimeout = 4 * time.Second
	}
	if opts.MaxConnections <= 0 {
		opts.MaxConnections = 128
	}
	if opts.MaxMessageBytes <= 0 {
		opts.MaxMessageBytes = 128 << 10
	}

	admitted := make(chan struct{}, opts.MaxConnections)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		if r.Method != http.MethodGet {
			w.Header().Set("Allow", http.MethodGet)
			http.Error(w, "仅支持 GET", http.StatusMethodNotAllowed)
			return
		}
		if !sameOrigin(r, opts.PublicOrigin) {
			http.Error(w, "实时连接来源不正确", http.StatusForbidden)
			return
		}
		cursor, ok := requestCursor(r.URL)
		if !ok {
			http.Error(w, "同步记号不正确", http.StatusBadRequest)
			return
		}
		select {
		case admitted <- struct{}{}:
			defer func() { <-admitted }()
		default:
			http.Error(w, "实时连接数已达上限", http.StatusServiceUnavailable)
			return
		}

		authContext, cancel := context.WithTimeout(r.Context(), opts.OperationTimeout)
		err := authenticate(r.WithContext(authContext))
		cancel()
		if err != nil {
			if errors.Is(err, ErrUnauthorized) {
				http.Error(w, "请先登录工作台", http.StatusUnauthorized)
			} else {
				http.Error(w, "暂时无法验证登录", http.StatusServiceUnavailable)
			}
			return
		}

		// The exact Origin check above also covers a configured proxy origin.
		conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
		if err != nil {
			return
		}
		defer conn.CloseNow()
		conn.SetReadLimit(1024)
		ctx := conn.CloseRead(r.Context())

		poll := time.NewTicker(opts.PollInterval)
		defer poll.Stop()
		ping := time.NewTicker(opts.PingInterval)
		defer ping.Stop()

		if !sendUpdate(ctx, r, conn, load, &cursor, opts) {
			return
		}
		for {
			select {
			case <-ctx.Done():
				return
			case <-ping.C:
				pingContext, cancel := context.WithTimeout(ctx, opts.OperationTimeout)
				err := conn.Ping(pingContext)
				cancel()
				if err != nil {
					return
				}
			case <-poll.C:
				if !sendUpdate(ctx, r, conn, load, &cursor, opts) {
					return
				}
			}
		}
	})
}

func requestCursor(u *url.URL) (int64, bool) {
	query, err := url.ParseQuery(u.RawQuery)
	if err != nil || len(query["cursor"]) != 1 {
		return 0, false
	}
	cursor, err := strconv.ParseUint(query["cursor"][0], 10, 53)
	return int64(cursor), err == nil
}

func sameOrigin(r *http.Request, configured string) bool {
	header := r.Header.Values("Origin")
	if len(header) != 1 {
		return false
	}
	origin, ok := bareOrigin(header[0])
	if !ok {
		return false
	}
	if configured == "" {
		scheme := "http"
		if r.TLS != nil {
			scheme = "https"
		}
		configured = scheme + "://" + r.Host
	}
	expected, ok := bareOrigin(configured)
	return ok && strings.EqualFold(origin, expected)
}

func bareOrigin(value string) (string, bool) {
	origin, err := url.Parse(value)
	if err != nil || origin.Hostname() == "" || origin.User != nil || origin.Path != "" ||
		origin.Opaque != "" || origin.RawQuery != "" || origin.ForceQuery || strings.Contains(value, "#") ||
		(origin.Scheme != "http" && origin.Scheme != "https") {
		return "", false
	}
	return origin.Scheme + "://" + origin.Host, true
}

func sendUpdate(
	ctx context.Context,
	r *http.Request,
	conn *websocket.Conn,
	load func(context.Context, *http.Request, int64) (Update, error),
	cursor *int64,
	opts Options,
) bool {
	readContext, cancel := context.WithTimeout(ctx, opts.OperationTimeout)
	defer cancel()
	request := r.WithContext(readContext)
	update, err := load(readContext, request, *cursor)
	if err == nil {
		err = readContext.Err()
	}
	if err != nil {
		if ctx.Err() == nil {
			if errors.Is(err, ErrUnauthorized) {
				_ = conn.Close(websocket.StatusPolicyViolation, "login expired")
			} else {
				_ = conn.Close(websocket.StatusInternalError, "update unavailable")
			}
		}
		return false
	}
	if update.Cursor < 0 || update.Cursor > maxBrowserCursor || (update.Cursor < *cursor && !update.Reload) {
		_ = conn.Close(websocket.StatusInternalError, "invalid cursor")
		return false
	}
	if update.Cursor == *cursor && !update.Reload && !update.Changed && len(update.Changes) == 0 {
		return true
	}
	payload, err := encodeUpdate(update, opts.MaxMessageBytes)
	if err != nil {
		_ = conn.Close(websocket.StatusInternalError, "invalid update")
		return false
	}
	writeContext, cancelWrite := context.WithTimeout(ctx, opts.OperationTimeout)
	defer cancelWrite()
	if err := conn.Write(writeContext, websocket.MessageText, payload); err != nil {
		return false
	}
	*cursor = update.Cursor
	return true
}

func encodeUpdate(update Update, maxBytes int) ([]byte, error) {
	reload := func() ([]byte, error) {
		return json.Marshal(struct {
			Cursor int64 `json:"cursor"`
			Reload bool  `json:"reload"`
		}{Cursor: update.Cursor, Reload: true})
	}
	if update.Reload || len(update.Changes) > 500 {
		return reload()
	}
	remaining := maxBytes
	for _, change := range update.Changes {
		if len(change) > remaining {
			return reload()
		}
		remaining -= len(change)
	}
	if update.Changes == nil {
		update.Changes = []json.RawMessage{}
	}
	payload, err := json.Marshal(update)
	if err == nil && len(payload) > maxBytes {
		return reload()
	}
	return payload, err
}
