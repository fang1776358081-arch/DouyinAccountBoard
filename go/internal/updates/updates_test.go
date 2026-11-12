package updates

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"reflect"
	"strconv"
	"testing"
	"time"

	"douyinboard/internal/session"

	_ "github.com/jackc/pgx/v5/stdlib"
)

const (
	historyAccount = `{"id":"go-updates-probe","name":"东区","rank":9007199254740993}`
	pythonVersion  = "5cabd930b1a8941ad3a80ad48b16a6f0c978e61f990ea5449bf4d105e98ec096"
)

func TestQueryAndVersionFallback(t *testing.T) {
	for _, raw := range []string{"cursor=-1", "cursor=a", "cursor=9223372036854775808", "cursor=0&limit=0",
		"cursor=1&limit=501", "cursor=1&wait=-1", "cursor=1&wait=6", "cursor=1&wait=1.5"} {
		values, _ := url.ParseQuery(raw)
		if _, err := parseQuery(values); err == nil {
			t.Errorf("accepted %q", raw)
		}
	}
	values, _ := url.ParseQuery("cursor=9223372036854775807&limit=500&wait=5")
	if got, err := parseQuery(values); got != (query{9223372036854775807, 500, 5 * time.Second}) || err != nil {
		t.Fatalf("unexpected query: %+v, %v", got, err)
	}
	service := Service{Fallback: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("version") != "v1" {
			t.Error("legacy version query was changed")
		}
		w.WriteHeader(http.StatusNoContent)
	})}
	call(t, service, "/api/board-updates?version=v1", "", http.StatusNoContent)
}

func TestOfficeProjection(t *testing.T) {
	east := session.User{Role: "operator", OfficeIDs: []string{"go-east"}}
	west := session.User{Role: "operator", OfficeIDs: []string{"go-west"}}
	owner := session.User{Role: "owner"}
	raw := `{"previous":["go-east","go-east"],"next":["go-west"],"account":` + historyAccount + `}`
	for _, actor := range []session.User{west, owner} {
		item, err := projectChange(actor, 12, "sync:office", raw, "")
		if err != nil || item == nil || item.Kind != "change" || item.Entity != "account:go-updates-probe" ||
			!reflect.DeepEqual(item.Offices, []string{"go-west"}) || item.RecordVersion == nil || *item.RecordVersion != pythonVersion {
			t.Fatalf("invalid destination change: %+v, %v", item, err)
		}
	}
	item, err := projectChange(east, 12, "sync:office", raw, "")
	if err != nil || item == nil || item.Kind != "remove" || item.After != nil || item.RecordVersion != nil ||
		!reflect.DeepEqual(item.Offices, []string{"go-east"}) {
		t.Fatalf("invalid move tombstone: %+v, %v", item, err)
	}
	for _, invalid := range []string{`["go-east",null]`, `["go-east",8]`, `{"id":"go-east"}`, ""} {
		item, err := projectChange(east, 12, "account:go-updates-probe", historyAccount, invalid)
		if err != nil || item != nil {
			t.Fatalf("invalid scope must be invisible: %q", invalid)
		}
	}
	item, err = projectChange(east, 12, "account:go-updates-probe", historyAccount, `["go-east","go-west"]`)
	if err != errScopeRefresh || item != nil {
		t.Fatalf("partially visible raw history should request a scoped reload: %+v, %v", item, err)
	}
	item, err = projectChange(east, 12, "account:go-updates-probe", "null", `["go-east"]`)
	if err != nil || item == nil || item.Kind != "remove" || item.After != nil || item.RecordVersion != nil {
		t.Fatalf("invalid direct tombstone: %+v, %v", item, err)
	}
}

func TestSyntheticPostgreSQLCursorMode(t *testing.T) {
	portString := os.Getenv("BOARD_GO_DAILY_TEST_PORT")
	if portString == "" {
		t.Skip("requires the disposable loopback fixture")
	}
	port, err := strconv.ParseUint(portString, 10, 16)
	if err != nil || port == 0 {
		t.Fatal("invalid synthetic PostgreSQL port")
	}
	db, err := sql.Open("pgx", fmt.Sprintf("postgres://synthetic_owner@127.0.0.1:%d/synthetic_probe?sslmode=disable&search_path=board_candidate,pg_catalog", port))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	var marker string
	if err := db.QueryRowContext(ctx, "SELECT kind FROM board_app.environment").Scan(&marker); err != nil || marker != "synthetic-only" {
		t.Fatalf("expected disposable synthetic fixture: %v", err)
	}
	service := Service{DB: db}
	get := func(cursor int64, otherQuery, role string) response {
		t.Helper()
		body := call(t, service, fmt.Sprintf("/api/board-updates?cursor=%d%s", cursor, otherQuery), role, http.StatusOK)
		var out response
		if err := json.Unmarshal(body, &out); err != nil {
			t.Fatalf("invalid cursor response: %v", err)
		}
		return out
	}
	call(t, service, "/api/board-updates?cursor=0", "", http.StatusUnauthorized)
	call(t, service, "/api/board-updates?cursor=-1", "employee", http.StatusBadRequest)
	call(t, service, "/api/board-updates?cursor=0&limit=501", "employee", http.StatusBadRequest)
	call(t, service, "/api/board-updates?cursor=0&wait=6", "employee", http.StatusBadRequest)

	group := fmt.Sprintf("go-updates-test-%d", time.Now().UnixNano())
	if _, err := db.ExecContext(ctx, `INSERT INTO accounts(id,name,status) VALUES('go-updates-probe','Synthetic Probe','正常')`); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_, _ = db.Exec("DELETE FROM board_candidate.operations_history WHERE group_id=$1", group)
		_, _ = db.Exec("DELETE FROM account_offices WHERE account_id='go-updates-probe'")
		_, _ = db.Exec("DELETE FROM accounts WHERE id='go-updates-probe'")
	})
	if _, err := db.ExecContext(ctx, `INSERT INTO account_offices(account_id,office_id) VALUES('go-updates-probe','go-east')`); err != nil {
		t.Fatal(err)
	}
	insert := func(entity string, after, scope any) int64 {
		t.Helper()
		var id int64
		err := db.QueryRowContext(ctx, `INSERT INTO board_candidate.operations_history
			(group_id, at, actor_name, action, entity, after_json, scope_offices)
			VALUES ($1, 0, 'synthetic probe', 'test', $2, $3, $4) RETURNING id`, group, entity, after, scope).Scan(&id)
		if err != nil {
			t.Fatal(err)
		}
		return id
	}
	cursor := insert("updates-probe:start", nil, nil)
	insert("account:updates-probe-west", `{"id":"updates-probe-west"}`, `["go-west"]`)
	insert("account:updates-probe-malformed", `{"id":"updates-probe-malformed"}`, `["go-east",null]`)
	latest := insert("account:go-updates-probe", historyAccount, `["go-east"]`)
	east := get(cursor, "&limit=20", "employee")
	if east.Reload || east.Cursor != latest || east.Changes == nil || len(*east.Changes) != 1 ||
		(*east.Changes)[0].RecordVersion == nil || *(*east.Changes)[0].RecordVersion != pythonVersion {
		t.Fatalf("employee cursor response wrong: %+v", east)
	}
	owner := get(cursor, "&limit=20", "owner")
	if owner.Changes == nil || len(*owner.Changes) != 3 {
		t.Fatalf("owner cursor response wrong: %+v", owner)
	}
	if capped := get(cursor, "&limit=1", "employee"); !capped.Reload || capped.Cursor != latest || capped.Changes != nil {
		t.Fatalf("limit must count hidden rows: %+v", capped)
	}
	next := insert("sync:office", `{"id":"go-updates-probe","previous":["go-east"],"next":["go-west"],"account":`+historyAccount+`}`, nil)
	if _, err := db.ExecContext(ctx, `UPDATE account_offices SET office_id='go-west' WHERE account_id='go-updates-probe'`); err != nil {
		t.Fatal(err)
	}
	if hidden := get(cursor, "&limit=20", "employee"); !hidden.Reload || hidden.Changes != nil {
		t.Fatalf("historical account payload survived a current scope move: %+v", hidden)
	}
	moved := get(latest, "&limit=20", "employee")
	if moved.Cursor != next || moved.Changes == nil || len(*moved.Changes) != 1 ||
		(*moved.Changes)[0].Kind != "remove" || (*moved.Changes)[0].After != nil {
		t.Fatalf("office move should deliver a payload-free tombstone: %+v", moved)
	}
	reloadID := insert("sync:reload", nil, nil)
	if forced := get(next, "", "employee"); !forced.Reload || forced.Cursor != reloadID || forced.Changes != nil {
		t.Fatalf("expected reload marker: %+v", forced)
	}
	if restored := get(reloadID+100000, "&limit=20", "employee"); !restored.Reload || restored.Cursor != reloadID+100000 {
		t.Fatalf("expected compatible backward reload: %+v", restored)
	}

	db.SetMaxOpenConns(1)
	start := time.Now()
	done := make(chan *httptest.ResponseRecorder, 1)
	go func() {
		request := httptest.NewRequest(http.MethodGet, fmt.Sprintf("/api/board-updates?cursor=%d&wait=1", reloadID), nil)
		request.AddCookie(&http.Cookie{Name: "board_session", Value: "synthetic-go-daily-employee-cookie"})
		recorder := httptest.NewRecorder()
		service.ServeHTTP(recorder, request)
		done <- recorder
	}()
	time.Sleep(150 * time.Millisecond)
	pingCtx, stopPing := context.WithTimeout(ctx, 400*time.Millisecond)
	defer stopPing()
	if err := db.PingContext(pingCtx); err != nil {
		t.Fatalf("idle wait held the only DB lease: %v", err)
	}
	recorder := <-done
	var waiting response
	if err := json.Unmarshal(recorder.Body.Bytes(), &waiting); err != nil || recorder.Code != http.StatusOK ||
		!waiting.WaitSupported || waiting.Reload || waiting.Cursor != reloadID || waiting.Changes == nil ||
		len(*waiting.Changes) != 0 || time.Since(start) < time.Second {
		t.Fatalf("unexpected idle wait result: %+v, %v", waiting, err)
	}
}

func call(t *testing.T, handler http.Handler, path, role string, status int) []byte {
	t.Helper()
	r := httptest.NewRequest(http.MethodGet, path, nil)
	if role != "" {
		r.AddCookie(&http.Cookie{Name: "board_session", Value: "synthetic-go-daily-" + role + "-cookie"})
	}
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, r)
	if w.Code != status {
		t.Fatalf("got HTTP %d, want %d: %s", w.Code, status, w.Body.String())
	}
	if status != http.StatusNoContent && w.Header().Get("Cache-Control") != "no-store" {
		t.Fatal("cursor responses must not be stored in caches")
	}
	return w.Body.Bytes()
}
