package board

import (
	"compress/gzip"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"strings"
	"testing"

	"douyinboard/internal/session"

	_ "github.com/jackc/pgx/v5/stdlib"
)

func TestBoardEncodingNegotiation(t *testing.T) {
	result := row{"row": strings.Repeat("测试", 1000)}
	for _, tt := range []struct {
		accept string
		status int
		coding string
	}{
		{"", 200, ""}, {"gzip, deflate", 200, "gzip"}, {"gzip;q=0.5", 200, ""},
		{"gzip;q=0, *;q=0", 406, ""}, {"gzip;q=0.5, identity;q=0", 200, "gzip"},
		{"gzip, gzip;q=0", 200, ""}, {"gzip;q=invalid, *", 200, ""},
	} {
		r := httptest.NewRequest(http.MethodGet, "/api/board", nil)
		r.Header.Set("Accept-Encoding", tt.accept)
		w := httptest.NewRecorder()
		WriteNegotiatedJSON(w, r, result)
		if w.Code != tt.status || w.Header().Get("Content-Encoding") != tt.coding || w.Header().Get("Vary") != "Accept-Encoding" ||
			w.Header().Get("Cache-Control") != "no-store" || w.Header().Get("Content-Length") != strconv.Itoa(w.Body.Len()) {
			t.Fatalf("response did not honor %q: %d %v", tt.accept, w.Code, w.Header())
		}
		if tt.coding == "gzip" {
			reader, err := gzip.NewReader(w.Body)
			if err != nil {
				t.Fatal(err)
			}
			var restored row
			if err := json.NewDecoder(reader).Decode(&restored); err != nil || restored["row"] != result["row"] {
				t.Fatal("gzip changed the JSON")
			}
			if err := reader.Close(); err != nil {
				t.Fatal(err)
			}
		}
	}
}

func TestBoardSyntheticPostgreSQL(t *testing.T) {
	setting := os.Getenv("BOARD_GO_DAILY_TEST_PORT")
	if setting == "" {
		t.Skip("requires the disposable loopback fixture")
	}
	port, err := strconv.ParseUint(setting, 10, 16)
	if err != nil || port == 0 {
		t.Fatal("invalid synthetic PostgreSQL port")
	}
	db, err := sql.Open("pgx", fmt.Sprintf("postgres://synthetic_owner@127.0.0.1:%d/synthetic_probe?sslmode=disable&search_path=board_candidate,pg_catalog", port))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	var marker string
	if err := db.QueryRow("SELECT kind FROM board_app.environment").Scan(&marker); err != nil || marker != "synthetic-only" {
		t.Fatalf("expected disposable synthetic fixture: %v", err)
	}
	h := Handler{DB: db}
	invoke := func(fn http.HandlerFunc, path, role string, expected int) row {
		t.Helper()
		r := httptest.NewRequest(http.MethodGet, path, nil)
		if role != "" {
			r.AddCookie(&http.Cookie{Name: "board_session", Value: "synthetic-go-daily-" + role + "-cookie"})
		}
		w := httptest.NewRecorder()
		fn(w, r)
		if w.Code != expected || w.Header().Get("Cache-Control") != "no-store" {
			t.Fatalf("%s for %q returned %d, want %d (%s)", path, role, w.Code, expected, w.Body.String())
		}
		var data row
		if err := json.Unmarshal(w.Body.Bytes(), &data); err != nil {
			t.Fatal(err)
		}
		return data
	}
	invoke(h.Board, "/api/board", "", 401)
	invoke(h.Board, "/api/board?view=other", "employee", 400)
	invoke(h.Board, "/api/board?view=work&compact=accounts", "employee", 400)
	invoke(h.Board, "/api/board?version_scope=week", "employee", 400)
	const day = "2026-11-09"
	full := invoke(h.Board, "/api/board?version_scope=day&date="+day, "employee", 200)
	const token = "fbe72c42119c53f613e7027d574a24acf7ddcf059fa7df4b732d74d091ca82b1"
	versions := full["record_versions"].(row)
	if full["date"] != day || versions["account:go-east-account"] != token || versions["daily:go-east-account:2026-11-08"] != nil || versions["account:go-west-account"] != nil {
		t.Fatal("day-scoped tokens differ from Python")
	}
	accounts := jsonRows(full["accounts"])
	if accounts["go-east-account"]["published"] != float64(0) || len(accounts) < 2 || accounts["go-west-account"] != nil || accounts["go-unassigned"] != nil || accounts["go-inactive-office"] != nil {
		t.Fatal("unexpected employee access to account rows")
	}
	owner := invoke(h.Board, "/api/board?date="+day, "owner", 200)
	if rows := jsonRows(owner["accounts"]); rows["go-west-account"]["published"] != float64(1) || rows["go-unassigned"] == nil || rows["go-inactive-office"] == nil {
		t.Fatal("owner's global scope changed")
	}
	work := invoke(h.Board, "/api/board?view=work", "employee", 200)
	if work["workspace_view"] != "summary" || work["collection"] != nil || len(work["record_versions"].(row)) != 0 || jsonRows(work["accounts"])["go-east-account"]["note"] != nil {
		t.Fatal("work summary is overbroad or missing its wire marker")
	}
	detail := invoke(h.Board, "/api/board?view=detail", "employee", 200)
	if detail["workspace_view"] != "detail" || detail["statuses"] == nil || detail["nurture"] == nil || detail["collection"] != nil || jsonRows(detail["accounts"])["go-east-account"]["note"] == nil {
		t.Fatal("detail fields changed")
	}
	compact := invoke(h.Board, "/api/board?compact=accounts", "employee", 200)
	if compact["device_workflow"].(row)["access"] != nil || compact["record_versions"].(row)["account:go-east-account"] != token {
		t.Fatal("compaction lost versions or left attached duplicate access")
	}
	versions = invoke(h.RecordVersions, "/api/record-versions?keys=account:go-east-account,account:go-west-account,daily:go-east-account:2026-11-08", "employee", 200)["record_versions"].(row)
	if len(versions) != 3 || versions["account:go-east-account"] != token || versions["account:go-west-account"] != nil || versions["daily:go-east-account:2026-11-08"] == nil {
		t.Fatal("targeted version results changed")
	}
	invoke(h.RecordVersions, "/api/record-versions?keys=layout:batch:office3:123:abc", "employee", 400)
	invoke(h.CollectionStatus, "/api/collection/status", "", 401)
	collection := invoke(h.CollectionStatus, "/api/collection/status", "employee", 200)
	if collection["can_manage"] != false || collection["collection"].(row)["enabled"] != false ||
		collection["collection"].(row)["linked"] != full["collection"].(row)["linked"] {
		t.Fatal("collection endpoint changed counts or enabled the collector")
	}
	invoke(h.History, "/api/workspace/history", "", 401)
	invoke(h.History, "/api/workspace/history", "employee", 403)
	invoke(h.History, "/api/workspace/history?before=not-a-number", "owner", 400)
	history := invoke(h.History, "/api/workspace/history?entity=account:go-east-account", "owner", 200)
	if _, ok := history["entries"].([]any); !ok {
		t.Fatal("history did not return its paged entry list")
	}
}

func TestHistoryRequiresRecordedAndCurrentOffices(t *testing.T) {
	scope := scopeMap{actor: session.User{Role: "employee", OfficeIDs: []string{"east"}},
		ids: map[string]map[string][]string{"accounts": {"a": {"east"}, "b": {"west"}}}}
	if !scope.visibleHistory("daily:a:2026-11-09", `["east"]`, nil, row{"account_id": "a"}) ||
		scope.visibleHistory("daily:a:2026-11-09", `["east","west"]`, nil, row{"account_id": "a"}) ||
		scope.visibleHistory("daily:b:2026-11-09", `["east"]`, nil, row{"account_id": "b"}) ||
		scope.visibleHistory("daily:a:2026-11-09", `null`, nil, row{"account_id": "a"}) {
		t.Fatal("history scope was widened")
	}
}

func jsonRows(value any) map[string]row {
	result := make(map[string]row)
	for _, item := range value.([]any) {
		fields := item.(row)
		result[str(fields["id"])] = fields
	}
	return result
}
