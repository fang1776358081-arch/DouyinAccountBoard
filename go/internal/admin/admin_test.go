package admin

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strconv"
	"strings"
	"testing"
	"time"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/stdlib"
)

func TestPostgresOwnerAdministration(t *testing.T) {
	db := adminTestDB(t)
	ctx := t.Context()
	read := Handler{DB: db}
	writer := accountwrite.Service{DB: db, WriterEpoch: 17, RestoreGeneration: "go-daily-synthetic-generation"}
	save := writer.Handle(UsersPath, "保存人员必须带请求标识", AuthorizeOwner, SaveUser)
	create := writer.Handle(OfficesPath, "新增办公室必须带请求标识", AuthorizeOwner, CreateOffice)
	assign := writer.Handle(ScopePath, "分配归属必须带请求标识", AuthorizeOwner, Reassign)
	group, err := accountwrite.NewGroup()
	if err != nil {
		t.Fatal(err)
	}
	adminCall(t, read.Users, http.MethodGet, UsersPath, "", nil, http.StatusUnauthorized)
	adminCall(t, read.Users, http.MethodGet, UsersPath, "viewer", nil, http.StatusForbidden)
	users := adminCall(t, read.Users, http.MethodGet, UsersPath, "owner", nil, http.StatusOK)
	if users["scope_enabled"] != true || len(users["users"].([]any)) < 3 {
		t.Fatalf("owner personnel read returned %v", users)
	}
	for _, value := range users["users"].([]any) {
		row := value.(map[string]any)
		if row["password_hash"] != nil || row["csrf"] != nil || row["authorization_version"] != nil {
			t.Fatal("unexpected sensitive field in personnel list")
		}
	}
	adminCall(t, read.Audit, http.MethodGet, "/api/auth/audit", "viewer", nil, http.StatusForbidden)
	adminCall(t, read.Audit, http.MethodGet, "/api/auth/audit", "owner", nil, http.StatusOK)
	input := map[string]any{"name": "合成办公室-" + group, "request_id": testRequestID(t)}
	adminCall(t, create, http.MethodPost, OfficesPath, "viewer", input, http.StatusForbidden)
	created := adminCall(t, create, http.MethodPost, OfficesPath, "owner", input, http.StatusOK)
	if retry := adminCall(t, create, http.MethodPost, OfficesPath, "owner", input, http.StatusOK); !reflect.DeepEqual(created, retry) {
		t.Fatalf("office creation replay differs: %v vs %v", created, retry)
	}
	officeID := created["id"].(string)
	loginName := "test-admin-" + group[:16]
	const password = "synthetic-passphrase-2026"
	newUser := map[string]any{"username": loginName, "name": "合成人员", "role": "viewer", "office_ids": []string{"go-east"}, "password": password,
		"request_id": testRequestID(t)}
	person := adminCall(t, save, http.MethodPost, UsersPath, "owner", newUser, http.StatusOK)
	personID := int64(person["id"].(float64))
	login := session.Handler{Store: session.Store{DB: db}, WriterEpoch: writer.WriterEpoch, RestoreGeneration: writer.RestoreGeneration}
	loginBody, err := recordwire.Encode(map[string]any{"username": loginName, "password": password})
	if err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest(http.MethodPost, "/api/auth/login", bytes.NewReader(loginBody))
	req.Header.Set("Content-Type", "application/json")
	response := httptest.NewRecorder()
	login.Login(response, req)
	if response.Code != http.StatusOK || len(response.Result().Cookies()) != 1 {
		t.Fatal("the compatible password hash did not permit synthetic login")
	}
	cookie := response.Result().Cookies()[0]
	updated := map[string]any{"id": personID, "username": "ignored-change", "name": "合成改名", "role": "employee",
		"permissions": []string{"publish", "publish"}, "office_ids": []string{officeID}, "request_id": testRequestID(t)}
	adminCall(t, save, http.MethodPost, UsersPath, "owner", updated, http.StatusOK)
	adminCall(t, save, http.MethodPost, UsersPath, "owner", updated, http.StatusOK)
	var version int64
	var username, permissions, grant string
	if err := db.QueryRowContext(ctx, `SELECT u.username,u.authorization_version,u.permissions,g.office_id
FROM auth_users u JOIN user_office_grants g ON g.user_id=u.id WHERE u.id=$1`, personID).Scan(&username, &version, &permissions, &grant); err != nil ||
		username != loginName || version != 3 || permissions != `["publish"]` || grant != officeID {
		t.Fatalf("grant update = %q, %d, %q, %q, err=%v", username, version, permissions, grant, err)
	}
	oldSession := httptest.NewRequest(http.MethodGet, "/api/auth/me", nil)
	oldSession.AddCookie(cookie)
	if _, err := (session.Store{DB: db}).Authenticate(ctx, oldSession); err != session.ErrUnauthorized {
		t.Fatalf("prior session was not revoked: %v", err)
	}
	editOwner := map[string]any{"id": 9101, "role": "employee", "request_id": testRequestID(t)}
	adminCall(t, save, http.MethodPost, UsersPath, "owner", editOwner, http.StatusForbidden)

	entities := adminCall(t, read.EntityScopes, http.MethodGet, "/api/auth/entity-scopes?kind=computers", "owner", nil, http.StatusOK)
	var current string
	for _, value := range entities["entities"].([]any) {
		row := value.(map[string]any)
		if row["id"] == "go-east-pc" {
			current = row["scope_version"].(string)
		}
	}
	if current == "" {
		t.Fatal("scope inventory missing test computer")
	}
	move := map[string]any{"kind": "computers", "id": "go-east-pc", "office_ids": []string{officeID}, "expected_scope": current, "request_id": testRequestID(t)}
	changed := adminCall(t, assign, http.MethodPost, ScopePath, "owner", move, http.StatusOK)
	if changed["scope_version"] == current {
		t.Fatal("scope version did not advance")
	}
	move["request_id"] = testRequestID(t)
	adminCall(t, assign, http.MethodPost, ScopePath, "owner", move, http.StatusConflict)
	var reloads int
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM operations_history WHERE entity='sync:reload'`).Scan(&reloads); err != nil || reloads < 3 {
		t.Fatalf("expected durable reload markers after grant and scope updates, got %d, err=%v", reloads, err)
	}
}

func testRequestID(t *testing.T) string {
	t.Helper()
	raw, err := accountwrite.NewGroup()
	if err != nil {
		t.Fatal(err)
	}
	return strings.Join([]string{raw[:8], raw[8:12], raw[12:16], raw[16:20], raw[20:]}, "-")
}

func adminCall(t *testing.T, handler http.HandlerFunc, method, path, role string, body map[string]any, status int) map[string]any {
	t.Helper()
	var data []byte
	if body != nil {
		var err error
		data, err = recordwire.Encode(body)
		if err != nil {
			t.Fatal(err)
		}
	}
	req := httptest.NewRequest(method, path, bytes.NewReader(data))
	if role != "" {
		req.AddCookie(&http.Cookie{Name: "board_session", Value: "synthetic-go-daily-" + role + "-cookie"})
		req.Header.Set("X-CSRF-Token", "synthetic-go-daily-"+role+"-csrf")
	}
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	handler(w, req)
	if w.Code != status {
		t.Fatalf("%s %s = %d, want %d. Body: %s", method, path, w.Code, status, w.Body)
	}
	var response map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	return response
}

func adminTestDB(t *testing.T) *sql.DB {
	t.Helper()
	port := os.Getenv("BOARD_GO_ADMIN_TEST_PORT")
	if port == "" {
		t.Skip("set BOARD_GO_ADMIN_TEST_PORT to run against the disposable loopback fixture")
	}
	if value, err := strconv.Atoi(port); err != nil || value < 1024 || value > 65535 {
		t.Fatal("test port must be a valid nonprivileged loopback port")
	}
	config, err := pgx.ParseConfig(fmt.Sprintf("host=127.0.0.1 port=%s dbname=synthetic_admin user=synthetic_owner sslmode=disable", port))
	if err != nil {
		t.Fatal(err)
	}
	config.Fallbacks = nil
	config.RuntimeParams["search_path"] = "board_candidate,pg_catalog"
	db := stdlib.OpenDB(*config)
	t.Cleanup(func() { _ = db.Close() })
	ctx, cancel := context.WithTimeout(t.Context(), 3*time.Second)
	defer cancel()
	var safe bool
	if err := db.QueryRowContext(ctx, `SELECT current_database()='synthetic_admin'
AND (SELECT array_agg(kind) FROM board_app.environment)=ARRAY['synthetic-only']::text[]`).Scan(&safe); err != nil || !safe {
		t.Fatalf("requires the admin-only synthetic clone: %v", err)
	}
	return db
}
