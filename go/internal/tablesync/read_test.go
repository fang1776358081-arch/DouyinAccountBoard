package tablesync

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"testing"
	"time"

	"douyinboard/internal/session"
	_ "github.com/jackc/pgx/v5/stdlib"
)

func TestTableSyncSyntheticPostgreSQL(t *testing.T) {
	db := syntheticTestDB(t)
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	w := httptest.NewRecorder()
	Handler{DB: db}.Read(w, httptest.NewRequest(http.MethodGet, "/api/table-sync", nil))
	if w.Code != http.StatusUnauthorized || w.Header().Get("Cache-Control") != "no-store" {
		t.Fatal("unauthenticated read was not rejected")
	}
	checkSyntheticReads(t, db, ctx)
}

func syntheticTestDB(t *testing.T) *sql.DB {
	t.Helper()
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
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	var synthetic bool
	if err := db.QueryRowContext(ctx, `SELECT COUNT(*)=1 AND MIN(kind)='synthetic-only' FROM board_app.environment`).Scan(&synthetic); err != nil || !synthetic {
		t.Fatalf("expected disposable fixture: %v", err)
	}
	return db
}

func checkSyntheticReads(t *testing.T, db *sql.DB, ctx context.Context) {
	// The data and migration-absence simulation stay inside this rolled-back fixture transaction.
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	for _, query := range []string{
		`INSERT INTO offices(id,name,active) VALUES ('three','Synthetic Office Three',1) ON CONFLICT DO NOTHING`,
		`INSERT INTO accounts(id,name) VALUES ('go-table-sync-three','合成可见'),('go-table-sync-hidden','合成隐藏')`,
		`INSERT INTO account_offices VALUES ('go-table-sync-three','three'),('go-table-sync-hidden','go-west')`,
		`INSERT INTO table_sync_account_sources(account_id,office_id,responsible,source_data,revision) VALUES
			('go-table-sync-three','three','合成负责人','{"progress":{"cell":"D10"}}',1),
			('go-table-sync-hidden','three','合成外部数据','{}',1)`,
		`INSERT INTO table_sync_batches VALUES ('go-table-sync-batch','three',9101,1,'add',NULL,'{"summary":{"updated":1},"issues":[]}','[]')`,
		`INSERT INTO table_sync_review_items VALUES ('go-table-sync-issue','go-table-sync-batch','three','corrected','{"id":"go-table-sync-issue"}','合成确认',2)`,
		`INSERT INTO table_sync_requests VALUES (9101,'b7d1687a-9707-47c0-8c81-bc8946a6c5be','go-daily-synthetic-generation','unused','{"ok":true}'),
			(9101,'91afbe5e-b013-4605-b791-f37a94e11af7','older-generation','unused','{"ok":true}')`,
	} {
		if _, err := tx.ExecContext(ctx, query); err != nil {
			t.Fatal(err)
		}
	}
	owner := session.User{ID: 9101, Role: "owner", Permissions: []string{"data_export"}}
	employee := session.User{ID: 9102, Role: "employee", OfficeIDs: []string{"three"}}
	outsider := session.User{ID: 9103, Role: "viewer", OfficeIDs: []string{"go-west"}}
	readAs := func(actor session.User, path string, expectedStatus int, expectedCode string) item {
		t.Helper()
		value, err := read(ctx, tx, actor, httptest.NewRequest(http.MethodGet, path, nil))
		if expectedStatus == http.StatusOK {
			if err != nil {
				t.Fatal(err)
			}
			return value.(item)
		}
		var failure readError
		if !errors.As(err, &failure) || failure.status != expectedStatus || failure.code != expectedCode {
			t.Fatalf("%s returned %v, want %d %s", path, err, expectedStatus, expectedCode)
		}
		return nil
	}
	readAs(outsider, "/api/table-sync/account-sources", 403, "")
	readAs(owner, "/api/table-sync?office_id=go-east", 400, "")
	readAs(employee, "/api/table-sync/batch?id=go-table-sync-batch", 403, "")
	pilot := readAs(owner, "/api/table-sync", 200, "")
	if pilot["available"] != true || pilot["can_manage"] != true || pilot["can_export"] != true || len(pilot["recent_batches"].([]item)) == 0 {
		t.Fatal("owner pilot summary did not load")
	}
	view := readAs(employee, "/api/table-sync/account-sources", 200, "")
	var got []item
	for _, source := range view["sources"].([]item) {
		if source["account_id"] == "go-table-sync-hidden" {
			t.Fatal("source leaked a different office's account")
		}
		if source["account_id"] == "go-table-sync-three" {
			got = append(got, source)
		}
	}
	if len(got) != 1 || got[0]["source_data"].(item)["progress"].(item)["cell"] != "D10" || view["version"] == "" {
		t.Fatal("structured source and its version did not load")
	}
	batch := readAs(owner, "/api/table-sync/batch?id=go-table-sync-batch", 200, "")
	if issues := batch["issues"].([]item); len(issues) != 1 || issues[0]["state"] != "corrected" || issues[0]["note"] != "合成确认" {
		t.Fatal("latest review decision missing from receipt")
	}
	if got := readAs(owner, "/api/table-sync/request?request_id=b7d1687a-9707-47c0-8c81-bc8946a6c5be", 200, ""); got["ok"] != true {
		t.Fatal("stored request result missing")
	}
	readAs(owner, "/api/table-sync/request?request_id=91afbe5e-b013-4605-b791-f37a94e11af7", 409, "RESTORE_GENERATION_EXPIRED")
	readAs(owner, "/api/table-sync/request?request_id=2918d426-4db3-4362-9478-a4a623e4dce8", 404, "REQUEST_NOT_FOUND")
	if _, err := tx.ExecContext(ctx, `DELETE FROM schema_migrations WHERE version=7`); err != nil {
		t.Fatal(err)
	}
	unavailable := readAs(employee, "/api/table-sync/account-sources", 200, "")
	if raw, err := json.Marshal(unavailable["sources"]); unavailable["available"] != false || err != nil || string(raw) != "[]" {
		t.Fatal("legacy schema should return unavailable with an empty source array")
	}
	readAs(owner, "/api/table-sync/batch?id=go-table-sync-batch", 400, "")
}
