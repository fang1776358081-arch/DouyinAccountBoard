package gateway

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"douyinboard/internal/realtime"
	"douyinboard/internal/session"
	"github.com/coder/websocket"
)

func TestExistingPageAndSocketUseSameSession(t *testing.T) {
	var committed atomic.Bool
	legacy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/live-sync.js":
			if r.Header.Get("If-None-Match") != "" || r.Header.Get("Accept-Encoding") != "identity" {
				t.Error("legacy script requested with cache validators or compression")
			}
			w.Header().Set("ETag", "legacy-tag")
			io.WriteString(w, "window.LiveSync = {};")
		case "/api/auth/me", "/api/board-updates":
			cookie, err := r.Cookie("board_session")
			if err != nil || cookie.Value != "synthetic-session" {
				http.Error(w, "login", http.StatusUnauthorized)
				return
			}
			if r.URL.Path == "/api/auth/me" {
				io.WriteString(w, `{"user":{"id":1}}`)
			} else if committed.Load() {
				io.WriteString(w, `{"cursor":8,"changes":[{"entity":"account:sample","after":{"name":"test row"}}]}`)
			} else {
				io.WriteString(w, `{"cursor":7,"changes":[]}`)
			}
		default:
			http.NotFound(w, r)
		}
	}))
	defer legacy.Close()
	origin, err := url.Parse(legacy.URL)
	if err != nil {
		t.Fatal(err)
	}
	service := httptest.NewServer(New(Config{LegacyOrigin: origin}))
	defer service.Close()

	script, _ := http.NewRequest(http.MethodGet, service.URL+"/live-sync.js", nil)
	script.Header.Set("If-None-Match", "legacy-tag")
	response, err := service.Client().Do(script)
	if err != nil {
		t.Fatal(err)
	}
	content, readErr := io.ReadAll(response.Body)
	response.Body.Close()
	if readErr != nil || response.StatusCode != http.StatusOK || response.Header.Get("ETag") != "" ||
		!strings.HasPrefix(string(content), "window.LiveSync = {};") || !strings.Contains(string(content), "/api/go/live") {
		t.Fatalf("browser bootstrap invalid, status %d, read error %v", response.StatusCode, readErr)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 6*time.Second)
	defer cancel()
	address := "ws" + strings.TrimPrefix(service.URL, "http") + "/api/go/live?cursor=7"
	conn, _, err := websocket.Dial(ctx, address, &websocket.DialOptions{
		HTTPHeader: http.Header{"Origin": {service.URL}, "Cookie": {"board_session=synthetic-session"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	defer conn.CloseNow()
	committed.Store(true)
	_, payload, err := conn.Read(ctx)
	if err != nil {
		t.Fatal(err)
	}
	var update realtime.Update
	if err := json.Unmarshal(payload, &update); err != nil {
		t.Fatal(err)
	}
	if update.Cursor != 8 || !update.Changed || len(update.Changes) != 0 || strings.Contains(string(payload), "test row") {
		t.Fatalf("socket must send a wake-up hint only: %s", payload)
	}
}

func TestVisibilityUsesCurrentOfficeGrant(t *testing.T) {
	employee := session.User{Role: "employee", OfficeIDs: []string{"three"}}
	if !visibleChange(employee, "account:a", `["three"]`, "") ||
		visibleChange(employee, "account:a", `["other"]`, "") ||
		visibleChange(employee, "account:a", `["three",null]`, "") ||
		visibleChange(employee, "account:a", "invalid", "") ||
		!visibleChange(employee, "sync:office", "", `{"previous":["three"],"next":["other"]}`) {
		t.Fatal("office-scoped notifications do not match the workbench grant")
	}
}
