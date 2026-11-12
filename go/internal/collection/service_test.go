package collection

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/board"
	"douyinboard/internal/session"
	"douyinboard/internal/updates"
	_ "github.com/jackc/pgx/v5/stdlib"
)

func TestCollectionWorkerBoundaryAndHomepage(t *testing.T) {
	secret := strings.Repeat("x", 48)
	service := Service{Token: secret}
	for _, remote := range []string{"127.0.0.1:10001", "[::1]:10001", "192.0.2.8:10001"} {
		r := httptest.NewRequest("GET", "http://127.0.0.1:10002/api/collection/next", nil)
		r.RemoteAddr = remote
		r.Header.Set("X-Collection-Engine", "f2")
		r.Header.Set("X-Worker-Token", secret)
		if service.workerAllowed(r) != localAddress(remote) {
			t.Fatalf("worker network boundary failed for %s", remote)
		}
		r.Header.Set("X-Forwarded-For", "192.0.2.8")
		if service.workerAllowed(r) {
			t.Fatal("allowed a forwarded worker request")
		}
	}
	for _, raw := range []string{"/user/MS4wTest", "https://evil.example/user/MS4wTest", "http://www.douyin.com/user/MS4wTest",
		"https://www.douyin.com:443/user/MS4wTest", "https://wrong@www.douyin.com/user/MS4wTest", "https://www.douyin.com/user/%4DS4wTest"} {
		if _, ok := normalizedHomepage(raw); ok {
			t.Fatalf("accepted invalid homepage %s", raw)
		}
	}
	if normalized, ok := normalizedHomepage(" https://www.douyin.com/user/MS4wTest/?source=share#profile "); !ok || normalized != "https://www.douyin.com/user/MS4wTest" {
		t.Fatal("sharing decorations were not removed")
	}
}

func TestManualCollectionSyntheticPostgreSQL(t *testing.T) {
	setting := os.Getenv("BOARD_GO_COLLECTION_TEST_PORT")
	if setting == "" {
		t.Skip("requires a dedicated synthetic standalone fixture")
	}
	port, err := strconv.ParseUint(setting, 10, 16)
	if err != nil || port == 0 {
		t.Fatal("invalid synthetic PostgreSQL port")
	}
	const fixture = "synthetic_go_standalone_collection_review_1111"
	db, err := sql.Open("pgx", fmt.Sprintf("postgres://synthetic_owner@127.0.0.1:%d/%s?sslmode=disable&search_path=board_candidate,pg_catalog", port, fixture))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Second)
	defer cancel()
	var marker, database string
	if err := db.QueryRowContext(ctx, `SELECT current_database(),kind FROM board_app.environment`).Scan(&database, &marker); err != nil || marker != "synthetic-only" || database != fixture {
		t.Fatal("the test requires its own disposable synthetic fixture")
	}
	exec := func(query string, args ...any) {
		t.Helper()
		if _, err := db.ExecContext(ctx, query, args...); err != nil {
			t.Fatal(err)
		}
	}
	exec(`DELETE FROM collection_jobs; DELETE FROM idempotency_records WHERE action='/api/collection/enqueue';
		DELETE FROM publish_review_baselines WHERE account_id IN ('go-east-account','go-west-account');
		DELETE FROM publish_review_samples WHERE source='collection';
		UPDATE auth_users SET permissions='["collection_manage"]' WHERE id=9102;
		UPDATE auth_sessions SET expires_at=extract(epoch from now())::bigint+3600;
		UPDATE accounts SET homepage='',work_count=18,last_collected='2026-11-09T09:00:00+08:00',collection_state='',collection_error='',last_attempted='' WHERE id IN ('go-east-account','go-west-account');
		UPDATE accounts SET douyin_id='0042',status='正常' WHERE id='go-east-account';
		UPDATE collection_settings SET auto_enabled=1,paused=0,pause_reason='' WHERE id=1;
		UPDATE collection_worker SET seen=0,status='',command='' WHERE id=1;
		INSERT INTO collection_jobs(account_id,homepage,douyin_id,token,state) VALUES('go-three-existing','','006767','manual-test-old','queued');`)
	var epoch int64
	var generation string
	if err := db.QueryRowContext(ctx, "SELECT writer_epoch,restore_generation FROM runtime_control WHERE id=1").Scan(&epoch, &generation); err != nil {
		t.Fatal(err)
	}
	write := accountwrite.Service{DB: db, WriterEpoch: epoch, RestoreGeneration: generation}
	service := Service{Write: write, Token: strings.Repeat("x", 48)}
	mux := http.NewServeMux()
	service.Register(mux)
	boardRead := board.Handler{DB: db, CollectionJobPrefix: service.ActiveJobPrefix()}
	mux.HandleFunc("GET /api/collection/status", boardRead.CollectionStatus)
	mux.HandleFunc("GET /api/idempotency/result", write.RequestStatus(func(ctx context.Context, tx *sql.Tx, actor session.User, _ string, result map[string]any) error {
		return PermitReceipt(ctx, tx, actor, result)
	}))
	mux.Handle("GET /api/board-updates", updates.Service{DB: db})
	call := func(method, path, role string, body payload, status int) payload {
		t.Helper()
		var raw []byte
		if body != nil {
			raw, _ = json.Marshal(body)
		}
		r := httptest.NewRequestWithContext(ctx, method, "http://127.0.0.1:8078"+path, bytes.NewReader(raw))
		r.RemoteAddr = "127.0.0.1:45000"
		r.Header.Set("Content-Type", "application/json")
		if role == "worker" {
			r.Header.Set("X-Collection-Engine", "f2")
			r.Header.Set("X-Worker-Token", service.Token)
		} else if role != "" {
			r.AddCookie(&http.Cookie{Name: "board_session", Value: "synthetic-go-daily-" + role + "-cookie"})
			r.Header.Set("Origin", "http://127.0.0.1:8078")
			r.Header.Set("X-CSRF-Token", "synthetic-go-daily-"+role+"-csrf")
		}
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, r)
		if w.Code != status || w.Header().Get("Cache-Control") != "no-store" {
			t.Fatalf("%s %s for %s returned %d, want %d", method, path, role, w.Code, status)
		}
		var result payload
		if err := json.Unmarshal(w.Body.Bytes(), &result); err != nil {
			t.Fatal(err)
		}
		return result
	}
	request := func(n int, ids ...string) payload {
		return payload{"ids": ids, "request_id": fmt.Sprintf("7935d90b-2660-433b-9e2e-%012d", n)}
	}
	call("GET", "/api/collection/jobs", "", nil, 401)
	call("GET", "/api/collection/jobs", "viewer", nil, 403)
	call("GET", "/api/collection/next", "owner", nil, 403)
	call("POST", EnqueuePath, "owner", request(1, "go-east-account"), 503)
	call("POST", "/api/collection/heartbeat", "worker", payload{"status": "待命", "browser_control": false}, 200)
	if c := call("GET", "/api/collection/status", "employee", nil, 200); c["can_manage"] != true || c["can_control"] != false || c["collection"].(payload)["auto_enabled"] != false {
		t.Fatal("collection status changed permissions or reported automatic collection")
	}
	call("POST", SettingsPath, "employee", payload{"pause": true}, 403)
	call("POST", SettingsPath, "owner", payload{"auto_enabled": true}, 400)
	call("POST", EnqueuePath, "viewer", request(2, "go-east-account"), 403)
	call("POST", EnqueuePath, "employee", request(2, "go-east-account", "go-west-account"), 403)
	// A profile alias alone cannot be resolved by F2. Keep it out of the queue.
	exec(`UPDATE accounts SET homepage='https://www.douyin.com/user/legacyAlias',douyin_id='' WHERE id='go-east-account'`)
	if reply := call("POST", EnqueuePath, "employee", request(20, "go-east-account"), 200); reply["invalid"] != float64(1) || reply["queued"] != float64(0) {
		t.Fatal("an unsupported homepage without a handle was queued")
	}
	exec(`UPDATE accounts SET douyin_id='0042' WHERE id='go-east-account'`)
	var cursor int64
	if err := db.QueryRowContext(ctx, "SELECT COALESCE(MAX(id),0) FROM operations_history").Scan(&cursor); err != nil {
		t.Fatal(err)
	}
	first := request(3, "go-east-account", "go-east-account")
	if reply := call("POST", EnqueuePath, "employee", first, 200); reply["queued"] != float64(1) {
		t.Fatal("duplicate selection was not merged")
	}
	if reply := call("GET", "/api/idempotency/result?request_id="+first["request_id"].(string), "employee", nil, 200); reply["queued"] != float64(1) {
		t.Fatal("the request receipt does not match the original submission")
	}
	if reply := call("POST", EnqueuePath, "employee", request(4, "go-east-account"), 200); reply["duplicate"] != float64(1) || reply["queued"] != float64(0) {
		t.Fatal("duplicate queued jobs were not deduplicated")
	}
	call("POST", EnqueuePath, "owner", request(5, "go-west-account"), 200)
	if reply := call("GET", "/api/collection/jobs", "employee", nil, 200); reply["total"] != float64(1) || reply["jobs"].([]any)[0].(payload)["office_id"] != "go-east" {
		t.Fatal("queue output leaked another office")
	}
	call("POST", SettingsPath, "owner", payload{"pause": true}, 200)
	if reply := call("GET", "/api/collection/next", "worker", nil, 200); reply["job"] != nil {
		t.Fatal("worker took a job while paused")
	}
	call("POST", SettingsPath, "owner", payload{"resume": true}, 200)
	job := call("GET", "/api/collection/next", "worker", nil, 200)["job"].(payload)
	if job["account_id"] != "go-east-account" || job["douyin_id"] != "0042" {
		t.Fatal("worker received the wrong account or lost leading zeroes")
	}
	if reply := call("POST", SettingsPath, "owner", payload{"cancel_waiting": true}, 200); reply["cancelled"] != float64(1) {
		t.Fatal("cancel did not preserve running and older jobs")
	}
	result := payload{"account_id": job["account_id"], "token": job["token"], "matched_douyin_id": "42",
		"count": 27, "source_url": "https://www.douyin.com/user/MS4wSyntheticFixture", "sampled_at": time.Now().UTC().Format(time.RFC3339Nano)}
	call("POST", "/api/collection/result", "worker", result, 400)
	result["matched_douyin_id"] = "0042"
	call("POST", "/api/collection/result", "worker", result, 200)
	if reply := call("POST", "/api/collection/result", "worker", result, 200); reply["duplicate"] != true {
		t.Fatal("result replay was not detected")
	}
	update := call("GET", fmt.Sprintf("/api/board-updates?cursor=%d", cursor), "employee", nil, 200)
	if update["reload"] != true && len(update["changes"].([]any)) == 0 {
		t.Fatal("collection changes were omitted from the live feed")
	}
	call("POST", EnqueuePath, "employee", request(6, "go-east-account"), 200)
	job = call("GET", "/api/collection/next", "worker", nil, 200)["job"].(payload)
	failed := payload{"account_id": job["account_id"], "token": job["token"], "error": "抖音登录已失效，请在独立采集浏览器中重新登录",
		"blocked": true, "sampled_at": time.Now().UTC().Format(time.RFC3339Nano)}
	call("POST", "/api/collection/result", "worker", failed, 200)
	var count, success, failures, oldJobs int
	var state, last, accountStatus, westState string
	if err := db.QueryRowContext(ctx, `SELECT work_count,collection_state,last_collected,status,
		(SELECT count(*) FROM publish_review_samples WHERE source='collection' AND result='ok'),
		(SELECT count(*) FROM publish_review_samples WHERE source='collection' AND result='failed'),
		(SELECT count(*) FROM collection_jobs WHERE token='manual-test-old' AND state='queued'),
		(SELECT collection_state FROM accounts WHERE id='go-west-account') FROM accounts WHERE id='go-east-account'`).
		Scan(&count, &state, &last, &accountStatus, &success, &failures, &oldJobs, &westState); err != nil {
		t.Fatal(err)
	}
	if count != 27 || state != "需要验证" || last == "2026-11-09T09:00:00+08:00" || accountStatus != "正常" || success != 1 || failures != 1 || oldJobs != 1 || westState != "已取消" {
		t.Fatal("result, failure, legacy queue or sample did not keep the required behavior")
	}
	call("POST", EnqueuePath, "employee", request(7, "go-east-account"), 503)
	call("POST", SettingsPath, "owner", payload{"resume": true}, 200)
	call("POST", EnqueuePath, "employee", request(8, "go-east-account"), 200)
	job = call("GET", "/api/collection/next", "worker", nil, 200)["job"].(payload)
	exec(`UPDATE collection_jobs SET started_at=$1 WHERE account_id='go-east-account'`, time.Now().Add(-11*time.Minute).UTC().Format(time.RFC3339))
	result["token"] = job["token"]
	if reply := call("POST", "/api/collection/result", "worker", result, 200); reply["ignored"] != true {
		t.Fatal("result for expired lease was accepted before reclaim")
	}
	claimed := call("GET", "/api/collection/next", "worker", nil, 200)["job"].(payload)
	if claimed["token"] == job["token"] || claimed["account_id"] != job["account_id"] {
		t.Fatal("expired lease was not renewed")
	}
	result["token"] = job["token"]
	if reply := call("POST", "/api/collection/result", "worker", result, 200); reply["ignored"] != true {
		t.Fatal("stale lease changed the result")
	}
	exec(`UPDATE accounts SET douyin_id='0043' WHERE id='go-east-account'`)
	result["token"] = claimed["token"]
	if reply := call("POST", "/api/collection/result", "worker", result, 200); reply["ignored"] != true {
		t.Fatal("stale identity was accepted")
	}
}
