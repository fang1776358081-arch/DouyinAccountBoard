package realtime

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"
)

func sessionCookie(r *http.Request) error {
	if cookie, err := r.Cookie("board_session"); err != nil || cookie.Value != "synthetic-session" {
		return ErrUnauthorized
	}
	return nil
}

func testDial(ctx context.Context, server *httptest.Server, query, origin string, authorized bool) (*websocket.Conn, *http.Response, error) {
	headers := make(http.Header)
	if origin != "" {
		headers.Set("Origin", origin)
	}
	if authorized {
		headers.Set("Cookie", "board_session=synthetic-session")
	}
	return websocket.Dial(ctx, "ws"+strings.TrimPrefix(server.URL, "http")+"/api/board-ws?"+query,
		&websocket.DialOptions{HTTPHeader: headers})
}

func readUpdate(t *testing.T, ctx context.Context, conn *websocket.Conn) Update {
	t.Helper()
	messageType, payload, err := conn.Read(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if messageType != websocket.MessageText {
		t.Fatalf("received message type %v", messageType)
	}
	var update Update
	if err := json.Unmarshal(payload, &update); err != nil {
		t.Fatal(err)
	}
	return update
}

func TestSocketDeliversChangesAndResumesAtClientCursor(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	server := httptest.NewServer(Handler(sessionCookie, func(_ context.Context, r *http.Request, cursor int64) (Update, error) {
		if err := sessionCookie(r); err != nil {
			return Update{}, err
		}
		switch cursor {
		case 7:
			return Update{Cursor: 8, Changed: true}, nil
		case 8:
			return Update{Cursor: 9, Changes: []json.RawMessage{json.RawMessage(`{"cursor":9,"entity":"daily:7:2026-11-10"}`)}}, nil
		default:
			return Update{Cursor: cursor}, nil
		}
	}, Options{PollInterval: time.Second}))
	defer server.Close()

	conn, _, err := testDial(ctx, server, "cursor=7", server.URL, true)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.CloseNow()
	if first := readUpdate(t, ctx, conn); first.Cursor != 8 || !first.Changed || first.Changes == nil || len(first.Changes) != 0 {
		t.Fatalf("unexpected first update %#v", first)
	}
	_ = conn.CloseNow()
	resumed, _, err := testDial(ctx, server, "cursor=8", server.URL, true)
	if err != nil {
		t.Fatal(err)
	}
	defer resumed.CloseNow()
	if next := readUpdate(t, ctx, resumed); next.Cursor != 9 || len(next.Changes) != 1 {
		t.Fatalf("unexpected resumed update %#v", next)
	}
}

func TestSocketRejectsInvalidOriginSessionAndCursor(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	server := httptest.NewServer(Handler(sessionCookie, func(_ context.Context, _ *http.Request, cursor int64) (Update, error) {
		return Update{Cursor: cursor}, nil
	}, Options{}))
	defer server.Close()

	cases := []struct {
		name, query, origin string
		authorized          bool
		wantStatus          int
	}{
		{"absent origin", "cursor=0", "", true, http.StatusForbidden},
		{"foreign origin", "cursor=0", "https://foreign.example", true, http.StatusForbidden},
		{"wrong scheme", "cursor=0", "https" + strings.TrimPrefix(server.URL, "http"), true, http.StatusForbidden},
		{"path in origin", "cursor=0", server.URL + "/board", true, http.StatusForbidden},
		{"missing session", "cursor=0", server.URL, false, http.StatusUnauthorized},
		{"ambiguous cursor", "cursor=1&cursor=2", server.URL, true, http.StatusBadRequest},
		{"cursor out of range", "cursor=9007199254740992", server.URL, true, http.StatusBadRequest},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			conn, response, err := testDial(ctx, server, test.query, test.origin, test.authorized)
			if conn != nil {
				defer conn.CloseNow()
			}
			if err == nil || response == nil || response.StatusCode != test.wantStatus {
				t.Fatalf("expected HTTP %d, received response=%v err=%v", test.wantStatus, response, err)
			}
		})
	}
}

func TestSocketRevocationClosesBeforeLoadingMoreData(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	var revoked atomic.Bool
	server := httptest.NewServer(Handler(sessionCookie, func(_ context.Context, r *http.Request, cursor int64) (Update, error) {
		if revoked.Load() {
			return Update{}, errors.Join(errors.New("synthetic read"), ErrUnauthorized)
		}
		if err := sessionCookie(r); err != nil {
			return Update{}, err
		}
		if cursor == 0 {
			return Update{Cursor: 1, Changed: true}, nil
		}
		return Update{Cursor: cursor}, nil
	}, Options{PollInterval: 10 * time.Millisecond}))
	defer server.Close()
	conn, _, err := testDial(ctx, server, "cursor=0", server.URL, true)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.CloseNow()
	_ = readUpdate(t, ctx, conn)
	revoked.Store(true)
	_, _, err = conn.Read(ctx)
	if websocket.CloseStatus(err) != websocket.StatusPolicyViolation {
		t.Fatalf("expected policy close, received %v", err)
	}
}

func TestSocketAcceptsConfiguredPublicOriginAndBoundsConnections(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	const publicOrigin = "https://board.example"
	server := httptest.NewServer(Handler(sessionCookie, func(_ context.Context, _ *http.Request, cursor int64) (Update, error) {
		return Update{Cursor: cursor}, nil
	}, Options{PublicOrigin: publicOrigin, MaxConnections: 1}))
	defer server.Close()
	conn, _, err := testDial(ctx, server, "cursor=0", publicOrigin, true)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.CloseNow()
	extra, response, err := testDial(ctx, server, "cursor=0", publicOrigin, true)
	if extra != nil {
		defer extra.CloseNow()
	}
	if err == nil || response == nil || response.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("expected bounded connection rejection, received response=%v err=%v", response, err)
	}
}

func TestReloadOmitsChangesEvenWhenCallbackIncludesThem(t *testing.T) {
	event := json.RawMessage(`{"cursor":9,"after":{"private":"sample"}}`)
	for _, update := range []Update{
		{Cursor: 9, Reload: true, Changes: []json.RawMessage{event}},
		{Cursor: 9, Changes: []json.RawMessage{event}},
	} {
		payload, err := encodeUpdate(update, 40)
		if err != nil || string(payload) != `{"cursor":9,"reload":true}` {
			t.Fatalf("expected only reload marker, received %s %v", payload, err)
		}
	}
}
