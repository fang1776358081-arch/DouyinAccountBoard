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

func TestProfileRequiredProtocol(t *testing.T) {
	for _, tt := range []struct {
		name    string
		body    payload
		message string
		want    bool
	}{
		{"missing", payload{"profile_required": true, "blocked": false}, missingProfileError, true},
		{"conflict", payload{"profile_required": true, "blocked": false}, conflictProfileError, true},
		{"missing flag", payload{"blocked": false}, missingProfileError, false},
		{"false flag", payload{"profile_required": false, "blocked": false}, missingProfileError, false},
		{"string flag", payload{"profile_required": "true", "blocked": false}, missingProfileError, false},
		{"missing blocked", payload{"profile_required": true}, missingProfileError, false},
		{"blocked", payload{"profile_required": true, "blocked": true}, missingProfileError, false},
		{"string blocked", payload{"profile_required": true, "blocked": "false"}, missingProfileError, false},
		{"unknown error", payload{"profile_required": true, "blocked": false}, "missing:其他错误", false},
		{"tagged missing error", payload{"profile_required": true, "blocked": false}, "missing:" + missingProfileError, false},
		{"tagged conflict error", payload{"profile_required": true, "blocked": false}, "conflict:" + conflictProfileError, false},
		{"decorated error", payload{"profile_required": true, "blocked": false}, missingProfileError + " ", false},
		{"empty error", payload{"profile_required": true, "blocked": false}, "", false},
	} {
		t.Run(tt.name, func(t *testing.T) {
			if got := isProfileRequired(tt.body, tt.message); got != tt.want {
				t.Fatalf("profile protocol classification = %t, want %t", got, tt.want)
			}
		})
	}
}

// These wire examples are the exact result_body outputs for ProfileMissing/ProfileConflict.
// Keep them independent of the Go messages so a producer/consumer wording drift fails the tests.
func workerProfileResult(t *testing.T, code string, job payload) payload {
	t.Helper()
	var raw string
	switch code {
	case "ProfileMissing":
		raw = `{"error":"缺少已确认主页，请补充主页后再采集","profile_required":true,"blocked":false}`
	case "ProfileConflict":
		raw = `{"error":"历史主页记录有冲突，请核对主页后再采集","profile_required":true,"blocked":false}`
	default:
		t.Fatal("unknown worker profile wire example")
	}
	var body payload
	if err := json.Unmarshal([]byte(raw), &body); err != nil {
		t.Fatal(err)
	}
	body["account_id"], body["token"] = job["account_id"], job["token"]
	return body
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
	mux.HandleFunc("GET /api/board", boardRead.Board)
	mux.HandleFunc("GET /api/collection/status", boardRead.CollectionStatus)
	mux.HandleFunc("GET /api/collection/disabled-status", (board.Handler{DB: db}).CollectionStatus)
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

	// Missing/conflicting confirmed profiles are per-account follow-up work, not a queue-wide challenge.
	beforeEmployee := call("GET", "/api/collection/status", "employee", nil, 200)["collection"].(payload)
	beforeOwner := call("GET", "/api/collection/status", "owner", nil, 200)["collection"].(payload)
	exec(`UPDATE accounts SET homepage='',douyin_id='0043' WHERE id='go-east-account';
		UPDATE accounts SET homepage='',douyin_id='0050' WHERE id='go-west-account'`)
	if reply := call("POST", EnqueuePath, "owner", request(100, "go-east-account", "go-west-account"), 200); reply["queued"] != float64(2) {
		t.Fatal("profile follow-up fixture did not queue both accounts")
	}
	job = call("GET", "/api/collection/next", "worker", nil, 200)["job"].(payload)
	if job["account_id"] != "go-east-account" {
		t.Fatal("profile follow-up fixture claimed the wrong first account")
	}
	profileResult := workerProfileResult(t, "ProfileMissing", job)
	profileResult["sampled_at"] = time.Now().UTC().Format(time.RFC3339Nano)
	call("POST", "/api/collection/result", "worker", profileResult, 200)
	if reply := call("POST", "/api/collection/result", "worker", profileResult, 200); reply["duplicate"] != true {
		t.Fatal("profile result replay was not detected")
	}
	var message string
	var paused int
	var preservedLast string
	if err := db.QueryRowContext(ctx, `SELECT work_count,collection_state,collection_error,last_collected,status,
		(SELECT paused FROM collection_settings WHERE id=1) FROM accounts WHERE id='go-east-account'`).
		Scan(&count, &state, &message, &preservedLast, &accountStatus, &paused); err != nil {
		t.Fatal(err)
	}
	if count != 27 || state != "待补主页" || message != missingProfileError || preservedLast != last || accountStatus != "正常" || paused != 0 {
		t.Fatal("missing profile changed the saved count, successful time, business status or queue pause")
	}
	if reply := call("GET", "/api/collection/jobs?state=待补主页", "employee", nil, 200); reply["total"] != float64(1) || reply["jobs"].([]any)[0].(payload)["account_id"] != "go-east-account" {
		t.Fatal("profile follow-up filter lost the account or office isolation")
	}
	job = call("GET", "/api/collection/next", "worker", nil, 200)["job"].(payload)
	if job["account_id"] != "go-west-account" {
		t.Fatal("a missing profile prevented the next account from running")
	}
	call("POST", "/api/collection/result", "worker", workerProfileResult(t, "ProfileConflict", job), 200)
	if err := db.QueryRowContext(ctx, `SELECT work_count,collection_state,collection_error,last_collected,
		(SELECT paused FROM collection_settings WHERE id=1) FROM accounts WHERE id='go-west-account'`).
		Scan(&count, &state, &message, &preservedLast, &paused); err != nil {
		t.Fatal(err)
	}
	if count != 18 || state != "待补主页" || message != conflictProfileError || preservedLast != "2026-11-09T09:00:00+08:00" || paused != 0 {
		t.Fatal("conflicting profile changed the previous success or paused the queue")
	}
	employeeCollection := call("GET", "/api/collection/status", "employee", nil, 200)["collection"].(payload)
	ownerCollection := call("GET", "/api/collection/status", "owner", nil, 200)["collection"].(payload)
	if employeeCollection["needs_profile"].(float64) != beforeEmployee["needs_profile"].(float64)+1 ||
		ownerCollection["needs_profile"].(float64) != beforeOwner["needs_profile"].(float64)+2 ||
		employeeCollection["failed"] != beforeEmployee["failed"] || ownerCollection["failed"] != beforeOwner["failed"] ||
		employeeCollection["profile_only"] != true {
		t.Fatal("profile counts changed failure semantics, office scope or collector capabilities")
	}
	for _, role := range []string{"employee", "owner"} {
		status := employeeCollection
		if role == "owner" {
			status = ownerCollection
		}
		boardCollection := call("GET", "/api/board?compact=accounts&version_scope=day", role, nil, 200)["collection"].(payload)
		if boardCollection["needs_profile"] != status["needs_profile"] || boardCollection["failed"] != status["failed"] || boardCollection["profile_only"] != true {
			t.Fatal("board and lightweight collection status disagree")
		}
	}
	if disabled := call("GET", "/api/collection/disabled-status", "employee", nil, 200)["collection"].(payload); disabled["profile_only"] != false || disabled["enabled"] != false {
		t.Fatal("disabled collection advertised an active profile-only engine")
	}
	if reply := call("GET", "/api/collection/jobs?state=待补主页", "employee", nil, 200); reply["total"] != float64(1) {
		t.Fatal("profile filter exposed another office's follow-up account")
	}
	// A retry may legitimately return zero. Unknown protocol errors must stay ordinary failures.
	call("POST", EnqueuePath, "employee", request(101, "go-east-account"), 200)
	job = call("GET", "/api/collection/next", "worker", nil, 200)["job"].(payload)
	call("POST", "/api/collection/result", "worker", payload{"account_id": job["account_id"], "token": job["token"],
		"count": 0, "source_url": "https://www.douyin.com/user/MS4wSyntheticFixture", "matched_douyin_id": "0043"}, 200)
	call("POST", EnqueuePath, "employee", request(102, "go-east-account"), 200)
	job = call("GET", "/api/collection/next", "worker", nil, 200)["job"].(payload)
	var permissionsBefore, lastBeforeFailure string
	if err := db.QueryRowContext(ctx, `SELECT last_collected,
		(SELECT jsonb_object_agg(id::text,permissions)::text FROM auth_users)
		FROM accounts WHERE id='go-east-account'`).Scan(&lastBeforeFailure, &permissionsBefore); err != nil {
		t.Fatal(err)
	}
	// Leave the next account queued before the failure, so cancelling or pausing the
	// remaining queue would fail this regression instead of being hidden by a new enqueue.
	if reply := call("POST", EnqueuePath, "owner", request(103, "go-west-account"), 200); reply["queued"] != float64(1) {
		t.Fatal("the following account did not enter the waiting queue")
	}
	call("POST", "/api/collection/result", "worker", payload{"account_id": job["account_id"], "token": job["token"],
		"error": "missing:其他错误", "profile_required": true, "blocked": false}, 200)
	if err := db.QueryRowContext(ctx, `SELECT work_count,collection_state,collection_error,
		(SELECT paused FROM collection_settings WHERE id=1) FROM accounts WHERE id='go-east-account'`).Scan(&count, &state, &message, &paused); err != nil {
		t.Fatal(err)
	}
	if count != 0 || state != "采集失败" || message != safeMessage("missing:其他错误") || paused != 0 {
		t.Fatal("zero result or unknown profile protocol was misclassified")
	}
	job = call("GET", "/api/collection/next", "worker", nil, 200)["job"].(payload)
	if job["account_id"] != "go-west-account" || job["douyin_id"] != "0050" {
		t.Fatal("an ordinary account failure interrupted the following queued account")
	}
	call("POST", "/api/collection/result", "worker", payload{"account_id": job["account_id"], "token": job["token"],
		"count": 12, "source_url": "https://www.douyin.com/user/MS4wSyntheticAfterFailure", "matched_douyin_id": "0050",
		"sampled_at": time.Now().UTC().Format(time.RFC3339Nano)}, 200)
	var followingCount int
	var followingState, permissionsAfter string
	if err := db.QueryRowContext(ctx, `SELECT work_count,collection_state,last_collected,status,
		(SELECT work_count FROM accounts WHERE id='go-west-account'),
		(SELECT collection_state FROM accounts WHERE id='go-west-account'),
		(SELECT paused FROM collection_settings WHERE id=1),
		(SELECT jsonb_object_agg(id::text,permissions)::text FROM auth_users)
		FROM accounts WHERE id='go-east-account'`).
		Scan(&count, &state, &preservedLast, &accountStatus, &followingCount, &followingState, &paused, &permissionsAfter); err != nil {
		t.Fatal(err)
	}
	if count != 0 || state != "采集失败" || preservedLast != lastBeforeFailure || accountStatus != "正常" ||
		followingCount != 12 || followingState != "采集成功" || paused != 0 || permissionsAfter != permissionsBefore {
		t.Fatal("failure continuation changed prior success, business status, permissions or queue state")
	}
	if reply := call("GET", "/api/collection/jobs?state=采集成功", "employee", nil, 200); reply["total"] != float64(0) {
		t.Fatal("the following success leaked outside the employee office")
	}
	if reply := call("GET", "/api/collection/next", "worker", nil, 200); reply["job"] != nil {
		t.Fatal("completion left an unexpected current worker job")
	}
}
