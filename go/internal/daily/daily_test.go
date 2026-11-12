package daily

import (
	"bytes"
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
	_ "github.com/jackc/pgx/v5/stdlib"
)

const (
	pythonAccountToken = "fbe72c42119c53f613e7027d574a24acf7ddcf059fa7df4b732d74d091ca82b1"
	pythonDailyToken   = "48474e18afec1dbda2570089a835269376599dd7f09be3098391c4825caf61fd"
	pythonFingerprint  = "9138c4946c57ee5e82536546a93868614aaa85a243fee625c6878a50dbc341da"
)

func TestPythonFingerprint(t *testing.T) {
	input, err := parseRequest(strings.NewReader(fmt.Sprintf(`{
		"published":true,"id":"go-east-account","date":"2026-11-09",
		"_expected_versions":{"account:go-east-account":%q,"daily:go-east-account:2026-11-09":%q},
		"request_id":"12e524c0-3310-4b11-b4ca-a758e0e1f555"}`, pythonAccountToken, pythonDailyToken)))
	if err != nil || input.Fingerprint != pythonFingerprint {
		t.Fatalf("fingerprint = %s, error = %v", input.Fingerprint, err)
	}
}

func TestDatabaseFailureReceipts(t *testing.T) {
	const requestID = "12e524c0-3310-4b11-b4ca-a758e0e1f555"
	for _, tt := range []struct {
		err  error
		code string
	}{
		{&pgconn.PgError{Code: "55P03", Detail: "never disclose detail"}, "DB_BUSY"},
		{&pgconn.PgError{Code: "57014"}, "DB_TIMEOUT"},
		{context.DeadlineExceeded, "DB_TIMEOUT"},
		{errors.New("never disclose detail"), "DB_UNAVAILABLE"},
	} {
		w := httptest.NewRecorder()
		writeSaveError(w, tt.err, requestID)
		var reply map[string]string
		if err := json.Unmarshal(w.Body.Bytes(), &reply); err != nil || w.Code != http.StatusServiceUnavailable ||
			reply["code"] != tt.code || reply["request_id"] != requestID || strings.Contains(w.Body.String(), "never disclose") {
			t.Fatalf("invalid %s response: %d %s", tt.code, w.Code, w.Body.String())
		}
	}
}

func TestDailySyntheticPostgreSQL(t *testing.T) {
	port, err := strconv.ParseUint(os.Getenv("BOARD_GO_DAILY_TEST_PORT"), 10, 16)
	if os.Getenv("BOARD_GO_DAILY_TEST_PORT") == "" {
		t.Skip("requires the disposable loopback fixture")
	}
	if err != nil || port == 0 {
		t.Fatal("invalid synthetic PostgreSQL port")
	}
	db, err := sql.Open("pgx", fmt.Sprintf("postgres://synthetic_owner@127.0.0.1:%d/synthetic_probe?sslmode=disable&search_path=board_candidate,pg_catalog", port))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	var marker string
	if err := db.QueryRowContext(ctx, "SELECT kind FROM board_app.environment").Scan(&marker); err != nil || marker != "synthetic-only" {
		t.Fatalf("expected disposable synthetic fixture: %v", err)
	}
	svc := Service{DB: db, WriterEpoch: 17, RestoreGeneration: "go-daily-synthetic-generation"}

	getFlags := func(role, query string, status int) flagsResponse {
		t.Helper()
		reply := call(t, svc.Flags, http.MethodGet, "/api/daily-flags?"+query, role, nil, status)
		var out flagsResponse
		if status == http.StatusOK && json.Unmarshal(reply, &out) != nil {
			t.Fatal("invalid flags response")
		}
		return out
	}
	getFlags("", "date=2026-11-09", http.StatusUnauthorized)
	getFlags("employee", "date=2026-11-09&versions=invalid", http.StatusBadRequest)
	flags := getFlags("employee", "date=2026-11-09&versions=1", http.StatusOK)
	wantFlags := map[string]bool{"go-east-account": false, "go-east-missing": false}
	if !reflect.DeepEqual(flags.Published, wantFlags) || flags.RecordVersions == nil {
		t.Fatalf("scoped flags = %+v", flags)
	}
	versions := *flags.RecordVersions
	if v := versions["daily:go-east-account:2026-11-09"]; v == nil || *v != pythonDailyToken {
		t.Fatal("daily version differs from Python")
	}
	if v, ok := versions["daily:go-east-missing:2026-11-09"]; !ok || v != nil || len(versions) != 2 {
		t.Fatal("missing row token should be null; unauthorized tokens should be absent")
	}
	old := getFlags("employee", "date=2026-11-08", http.StatusOK)
	if !old.Published["go-east-account"] || old.RecordVersions != nil {
		t.Fatal("historical date or optional versions response changed")
	}
	owner := getFlags("owner", "date=2026-11-09", http.StatusOK)
	if !owner.Published["go-west-account"] || len(owner.Published) < 5 {
		t.Fatal("global access lost visible flags")
	}

	const date = "2026-10-10"
	key := dailyKey("go-east-account", date)
	current := getFlags("employee", "date="+date+"&versions=1", http.StatusOK)
	expected := map[string]any{"account:go-east-account": pythonAccountToken, key: (*current.RecordVersions)[key]}
	request := map[string]any{"id": "go-east-account", "date": date, "published": !current.Published["go-east-account"],
		"request_id": testRequestID(t), "_expected_versions": expected}
	var historyBefore int
	if err := db.QueryRowContext(ctx, "SELECT COUNT(*) FROM operations_history WHERE entity=$1", key).Scan(&historyBefore); err != nil {
		t.Fatal(err)
	}
	post := func(service Service, role string, status int) map[string]any {
		t.Helper()
		reply := call(t, service.Save, http.MethodPost, dailyPath, role, request, status)
		var body map[string]any
		if err := json.Unmarshal(reply, &body); err != nil {
			t.Fatal(err)
		}
		return body
	}
	post(svc, "viewer", http.StatusForbidden)
	post(svc, "bad-csrf", http.StatusForbidden)
	request["id"] = "go-west-account"
	post(svc, "employee", http.StatusForbidden)
	request["id"] = "go-east-account"
	retired := svc
	retired.WriterEpoch--
	if post(retired, "employee", http.StatusConflict)["code"] != "WRITER_VERSION_RETIRED" {
		t.Fatal("retired writer accepted")
	}
	first := post(svc, "employee", http.StatusOK)
	if first["ok"] != true || first["daily"].(map[string]any)["published"] != request["published"] {
		t.Fatalf("publish did not save: %v", first)
	}
	if first["_record_versions"].(map[string]any)["account:go-east-account"] != pythonAccountToken {
		t.Fatal("account version differs from Python")
	}
	if again := post(svc, "employee", http.StatusOK); !reflect.DeepEqual(again, first) {
		t.Fatal("replay did not return the exact prior response")
	}
	request["published"] = current.Published["go-east-account"]
	if post(svc, "employee", http.StatusConflict)["code"] != "REQUEST_ID_REUSED" {
		t.Fatal("reused id with different data accepted")
	}
	request["request_id"] = testRequestID(t)
	post(svc, "employee", http.StatusConflict)
	var state string
	if err := db.QueryRowContext(ctx, "SELECT state FROM idempotency_records WHERE user_id=9102 AND request_id=$1", request["request_id"]).Scan(&state); err != nil || state != "pending" {
		t.Fatalf("stale write did not preserve its pending reservation: %v", err)
	}
	var historyAfter int
	if err := db.QueryRowContext(ctx, "SELECT COUNT(*) FROM operations_history WHERE entity=$1 AND scope_offices='[\"go-east\"]'", key).Scan(&historyAfter); err != nil || historyAfter != historyBefore+1 {
		t.Fatalf("history count = %d, previous count = %d, error = %v", historyAfter, historyBefore, err)
	}
	latest := getFlags("employee", "date="+date+"&versions=1", http.StatusOK)
	if latest.Published["go-east-account"] == current.Published["go-east-account"] ||
		*(*latest.RecordVersions)[key] != first["_record_versions"].(map[string]any)[key] {
		t.Fatal("publication status and version were not stored together")
	}
}

func call(t *testing.T, handler http.HandlerFunc, method, path, role string, body any, status int) []byte {
	t.Helper()
	var encoded []byte
	if body != nil {
		var err error
		encoded, err = json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
	}
	r := httptest.NewRequest(method, path, bytes.NewReader(encoded))
	if role != "" {
		cookieRole := role
		if role == "bad-csrf" {
			cookieRole = "employee"
		}
		r.AddCookie(&http.Cookie{Name: "board_session", Value: "synthetic-go-daily-" + cookieRole + "-cookie"})
		r.Header.Set("X-CSRF-Token", "synthetic-go-daily-"+role+"-csrf")
	}
	if body != nil {
		r.Header.Set("Content-Type", "application/json")
	}
	w := httptest.NewRecorder()
	handler(w, r)
	if w.Code != status || w.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("%s %s (%s) returned %d, want %d: %s", method, path, role, w.Code, status, w.Body.String())
	}
	return w.Body.Bytes()
}

func testRequestID(t *testing.T) string {
	t.Helper()
	var raw [16]byte
	if _, err := rand.Read(raw[:]); err != nil {
		t.Fatal(err)
	}
	id := hex.EncodeToString(raw[:])
	return id[:8] + "-" + id[8:12] + "-" + id[12:16] + "-" + id[16:20] + "-" + id[20:]
}
