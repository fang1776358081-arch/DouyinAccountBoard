package publishreview

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"douyinboard/internal/session"
	_ "github.com/jackc/pgx/v5/stdlib"
)

const goldenID = "10514682-33ca-43f9-9a17-5ad6f468b8e9"
const goldenFingerprint = "e2f4c0d6baa953e1612ea5a3a2ee3d8e5f34da5b89abb1ff6f696ac864153d63"

func goldenTask() row {
	return row{"account_id": "go-east-account", "day": "2026-11-10", "target": 5, "task_state": "active", "computer_id": "go-east-pc",
		"reason": "合成目标：, \\ + \u2028", "expected_revision": 0, "request_id": goldenID}
}

func TestLegacyPublishReviewFingerprint(t *testing.T) {
	encoded, err := legacyJSON(row{"path": taskPath, "body": goldenTask()})
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(encoded)
	if hex.EncodeToString(sum[:]) != goldenFingerprint {
		t.Fatalf("fingerprint differs from Python: %x", sum)
	}
}

func TestPublishReviewSyntheticPostgreSQL(t *testing.T) {
	setting := os.Getenv("BOARD_GO_PUBLISH_REVIEW_TEST_PORT")
	if setting == "" {
		t.Skip("requires isolated synthetic_go_publishreview fixture cloned from the base seed")
	}
	port, err := strconv.ParseUint(setting, 10, 16)
	if err != nil || port == 0 {
		t.Fatal("invalid synthetic PostgreSQL port")
	}
	db, err := sql.Open("pgx", fmt.Sprintf("postgresql://synthetic_owner@127.0.0.1:%d/synthetic_go_publishreview?sslmode=disable&search_path=board_candidate,pg_catalog", port))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	var synthetic bool
	if err := db.QueryRowContext(ctx, `SELECT COUNT(*)=1 AND MIN(kind)='synthetic-only' FROM board_app.environment`).Scan(&synthetic); err != nil || !synthetic {
		t.Fatalf("expected isolated fixture: %v", err)
	}
	clock := time.Date(2026, 11, 10, 12, 0, 0, 0, beijing)
	const generation = "go-daily-synthetic-generation"
	h := Handler{DB: db, WriterEpoch: 17, RestoreGeneration: generation, now: func() time.Time { return clock }}
	unauthorized := httptest.NewRecorder()
	h.Overview(unauthorized, httptest.NewRequest(http.MethodGet, "/api/publish-review", nil))
	if unauthorized.Code != 401 || unauthorized.Header().Get("Cache-Control") != "no-store" {
		t.Fatal("unauthenticated read must be rejected")
	}
	request := httptest.NewRequest(http.MethodPost, taskPath, strings.NewReader(`{}`))
	request.Header.Set("Content-Type", "application/json")
	unauthorized = httptest.NewRecorder()
	h.Write(unauthorized, request)
	if unauthorized.Code != 401 {
		t.Fatalf("unauthenticated task returned %d", unauthorized.Code)
	}
	// All writes are disposable and rolled back, preserving the cloned base seed.
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if present, err := available(ctx, tx); err != nil || !present {
		t.Fatalf("v6 schema not available: %v", err)
	}
	owner := session.User{ID: 9101, Name: "Synthetic Owner", Role: "owner", Permissions: []string{"publishing_manage", "collection_manage"}}
	employee := session.User{ID: 9102, Role: "employee", OfficeIDs: []string{"go-east"}, Permissions: []string{"publishing_manage"}}
	west := session.User{ID: 9103, Role: "employee", OfficeIDs: []string{"go-west"}}
	post := func(actor session.User, path string, body row, status int) row {
		t.Helper()
		result, err := apply(ctx, tx, actor, generation, path, body, clock)
		if status == 200 {
			if err != nil {
				t.Fatal(err)
			}
			return result
		}
		var rejected apiError
		if !errors.As(err, &rejected) || rejected.status != status {
			t.Fatalf("%s expected %d, got %v", path, status, err)
		}
		return nil
	}
	first := post(owner, taskPath, goldenTask(), 200)
	second := post(owner, taskPath, goldenTask(), 200)
	if one, _ := number(first["revision"]); one != 1 {
		t.Fatal("initial revision should be one")
	}
	if two, _ := number(second["revision"]); two != 1 {
		t.Fatal("replay incremented the revision")
	}
	var digest string
	if err := tx.QueryRowContext(ctx, `SELECT fingerprint FROM publish_review_requests WHERE actor_id=9101 AND request_id=$1`, goldenID).Scan(&digest); err != nil || digest != goldenFingerprint {
		t.Fatalf("stored fingerprint differs from legacy: %v", err)
	}
	other := goldenTask()
	other["target"] = 8
	post(owner, taskPath, other, 409)
	other["request_id"] = requestID(2)
	post(owner, taskPath, other, 409)
	other["computer_id"], other["request_id"] = "go-west-pc", requestID(3)
	post(owner, taskPath, other, 400)
	other["account_id"], other["request_id"] = "go-west-account", requestID(4)
	post(employee, taskPath, other, 403)
	post(owner, taskPath, other, 200)
	prior := goldenTask()
	prior["day"], prior["request_id"] = "2026-11-09", requestID(5)
	post(owner, taskPath, prior, 400)
	if _, err := apply(ctx, tx, owner, "old-generation", taskPath, goldenTask(), clock); err == nil {
		t.Fatal("a different restore generation replayed the receipt")
	}
	var dailyBefore, jobsBefore int64
	if err := tx.QueryRowContext(ctx, `SELECT (SELECT count(*) FROM daily),(SELECT count(*) FROM collection_jobs)`).Scan(&dailyBefore, &jobsBefore); err != nil {
		t.Fatal(err)
	}
	sample := func(account, handle, at string, n int, failed bool, request int, status int) {
		post(owner, samplePath, row{"account_id": account, "matched_douyin_id": handle, "sampled_at": at, "total": n, "failed": failed, "request_id": requestID(request)}, status)
	}
	sample("go-east-account", "0042", "2026-11-09T08:00:00+08:00", 100, false, 11, 200)
	sample("go-east-account", "0042", "2026-11-10T00:01:00Z", 109, false, 12, 200)
	sample("go-west-account", "0099", "2026-11-09T08:00:00+08:00", 100, false, 13, 200)
	sample("go-west-account", "0099", "2026-11-10T08:00:00+08:00", 97, false, 14, 200)
	sample("go-east-account", "0042", "2026-11-09T23:00:00+08:00", 108, false, 15, 200)
	sample("go-east-account", "wrong", "2026-11-10T08:00:00+08:00", 111, false, 16, 400)
	sample("go-east-account", "0042", "2026-11-10T08:00:00", 111, false, 17, 400)
	sample("go-east-account", "0042", "2026-11-11T08:00:00+08:00", 111, false, 18, 400)
	view := func(actor session.User, scope filters, now time.Time) row {
		t.Helper()
		result, err := overview(ctx, tx, actor, scope, now)
		if err != nil {
			t.Fatal(err)
		}
		return result
	}
	today := filters{day: "2026-11-10", page: 1}
	all := view(owner, today, clock)
	assertReview(t, all, "go-east-account", "met", 9, 100)
	assertReview(t, all, "go-west-account", "review", -3, 100)
	currentEast := view(employee, today, clock)
	if total, _ := number(currentEast["total"]); total != 2 || findReview(currentEast, "go-west-account") != nil {
		t.Fatal("east employee read across offices")
	}
	sample("go-east-account", "", "2026-11-10T09:00:00+08:00", 0, true, 19, 200)
	assertReview(t, view(owner, today, clock), "go-east-account", "review", 9, 100)
	if _, err := tx.ExecContext(ctx, `UPDATE account_offices SET office_id='go-west' WHERE account_id='go-east-account'`); err != nil {
		t.Fatal(err)
	}
	if findReview(view(west, today, clock), "go-east-account") != nil {
		t.Fatal("current office grant exposed a prior office's task")
	}
	transferred := goldenTask()
	transferred["expected_revision"], transferred["request_id"], transferred["computer_id"] = 1, requestID(20), "go-west-pc"
	post(west, taskPath, transferred, 403)
	if _, err := tx.ExecContext(ctx, `UPDATE accounts SET name='合成改名' WHERE id='go-east-account'`); err != nil {
		t.Fatal(err)
	}
	historical := view(owner, today, clock.AddDate(0, 0, 1))
	if got := findReview(historical, "go-east-account"); got["name"] != "合成账号" || historical["historical"] != true || got["legacy_at"] != nil {
		t.Fatal("historical task substituted current account data")
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO accounts(id,computer_id,slot,name,status)
		SELECT 'publish-review-bulk-'||n,'go-east-pc',n+200,'synthetic','正常' FROM generate_series(1,2000) n`); err != nil {
		t.Fatal(err)
	}
	today.page = 2
	large := view(owner, today, clock)
	if page, _ := number(large["page"]); page != 2 || len(large["rows"].([]any)) != 50 {
		t.Fatal("large review result bypassed bounded pagination")
	}
	var dailyAfter, jobsAfter int64
	if err := tx.QueryRowContext(ctx, `SELECT (SELECT count(*) FROM daily),(SELECT count(*) FROM collection_jobs)`).Scan(&dailyAfter, &jobsAfter); err != nil || dailyBefore != dailyAfter || jobsBefore != jobsAfter {
		t.Fatalf("tasks or external evidence altered collection/manual publication: %v", err)
	}
	if _, err := tx.ExecContext(ctx, `DELETE FROM schema_migrations WHERE version=6`); err != nil {
		t.Fatal(err)
	}
	if present, err := available(ctx, tx); err != nil || present {
		t.Fatalf("unmigrated schema reported available: %v", err)
	}
}

func requestID(number int) string { return fmt.Sprintf("c4b335e8-eeda-4c44-82c7-%012x", number) }

func findReview(board row, id string) row {
	for _, value := range board["rows"].([]any) {
		if row := value.(row); row["account_id"] == id {
			return row
		}
	}
	return nil
}

func assertReview(t *testing.T, board row, id, state string, net, baseline int64) {
	t.Helper()
	got := findReview(board, id)
	actualNet, netOK := number(got["net"])
	actualBaseline, baselineOK := number(got["baseline_total"])
	if got["state"] != state || !netOK || actualNet != net || !baselineOK || actualBaseline != baseline {
		t.Fatalf("unexpected %s review state/net/baseline", id)
	}
}
