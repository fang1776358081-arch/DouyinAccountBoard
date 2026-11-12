package account

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
	"douyinboard/internal/inventory"
	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/stdlib"
)

func TestNurturePausePeriodMatchesPythonStorage(t *testing.T) {
	raw := `[{"start":"2026-11-05","end":"2026-11-06","说明":"货车🙂"},3]`
	want := `[{"start": "2026-11-05", "end": "2026-11-06", "\u8bf4\u660e": "\u8d27\u8f66\ud83d\ude42"}, {"start": "2026-11-07", "end": "2026-11-09"}]`
	if got, err := appendNurturePeriod(raw, "2026-11-07", "2026-11-09", true); err != nil || got != want {
		t.Fatalf("periods = %s, want %s", got, want)
	}
}

func TestPostgresAccountWrites(t *testing.T) {
	db := accountTestDB(t)
	ctx := t.Context()
	writer := accountwrite.Service{DB: db, WriterEpoch: 17, RestoreGeneration: "go-daily-synthetic-generation"}
	h := WriteHandler{Service: writer}
	owner := "owner"
	group, err := accountwrite.NewGroup()
	if err != nil {
		t.Fatal(err)
	}
	id := "account-test-" + group
	if _, err := db.ExecContext(ctx, `INSERT INTO accounts(id,name,status,note) VALUES($1,'合成账号','正常','初始备注')`, id); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(ctx, `INSERT INTO account_offices(account_id,office_id) VALUES($1,'go-east')`, id); err != nil {
		t.Fatal(err)
	}
	start := readVersion(t, db, id)
	request := func(version, field string, value any) map[string]any {
		return map[string]any{"id": id, "changes": map[string]any{field: value}, "return_account": true,
			"_expected_versions": map[string]any{"account:" + id: version}, "request_id": accountRequestID(t)}
	}
	name := request(start, "name", "  合成昵称  ")
	first := accountCall(t, h.Patch(), http.MethodPost, patchPath, owner, name, http.StatusOK)
	if first["account"].(map[string]any)["name"] != "合成昵称" || first["record_version"] != readVersion(t, db, id) {
		t.Fatalf("unexpected inline receipt: %v", first)
	}
	again := accountCall(t, h.Patch(), http.MethodPost, patchPath, owner, name, http.StatusOK)
	if !reflect.DeepEqual(first, again) {
		t.Fatalf("replay differs: %v versus %v", first, again)
	}
	lookup := writer.RequestStatus(PermitReceipt)
	result := accountCall(t, lookup, http.MethodGet, "/api/idempotency/result?request_id="+name["request_id"].(string), owner, nil, http.StatusOK)
	if result["code"] != "REQUEST_SUCCEEDED" || result["record_version"] != first["record_version"] {
		t.Fatalf("request lookup = %v", result)
	}
	var histories, audits int
	if err := db.QueryRowContext(ctx, `SELECT (SELECT count(*) FROM operations_history WHERE entity=$1),
(SELECT count(*) FROM auth_audit WHERE target=$2 AND action=$3)`, "account:"+id, id, patchPath).Scan(&histories, &audits); err != nil || histories != 1 || audits != 1 {
		t.Fatalf("history = %d, audit = %d, error = %v", histories, audits, err)
	}
	bad := request(start, "note", "旧版本写入")
	accountCall(t, h.Patch(), http.MethodPost, patchPath, owner, bad, http.StatusConflict)
	pending := accountCall(t, lookup, http.MethodGet, "/api/idempotency/result?request_id="+bad["request_id"].(string), owner, nil, http.StatusConflict)
	if pending["code"] != "REQUEST_PENDING" {
		t.Fatalf("expected retained pending outcome, got %v", pending)
	}
	current := readVersion(t, db, id)
	badName := request(current, "name", " ")
	accountCall(t, h.Patch(), http.MethodPost, patchPath, owner, badName, http.StatusBadRequest)
	failed := accountCall(t, lookup, http.MethodGet, "/api/idempotency/result?request_id="+badName["request_id"].(string), owner, nil, http.StatusBadRequest)
	if failed["code"] != "REQUEST_FAILED" || current != readVersion(t, db, id) {
		t.Fatalf("failed outcome did not roll back, got %v", failed)
	}
	if _, err := db.ExecContext(ctx, `UPDATE auth_users SET permissions='["publish"]' WHERE username='go-daily-employee'`); err != nil {
		t.Fatal(err)
	}
	accountCall(t, h.Patch(), http.MethodPost, patchPath, "employee", request(current, "name", "不能修改"), http.StatusForbidden)
	if _, err := db.ExecContext(ctx, `UPDATE auth_users SET permissions='["account_manage"]' WHERE username='go-daily-employee'`); err != nil {
		t.Fatal(err)
	}
	foreign := request(current, "name", "不可见账号")
	foreign["id"] = "go-west-account"
	accountCall(t, h.Patch(), http.MethodPost, patchPath, "employee", foreign, http.StatusForbidden)
	allowed := request(current, "name", "办公室内允许修改")
	accountCall(t, h.Patch(), http.MethodPost, patchPath, "employee", allowed, http.StatusOK)
	if _, err := db.ExecContext(ctx, `UPDATE account_offices SET office_id='go-west' WHERE account_id=$1`, id); err != nil {
		t.Fatal(err)
	}
	accountCall(t, lookup, http.MethodGet, "/api/idempotency/result?request_id="+allowed["request_id"].(string), "employee", nil, http.StatusForbidden)
	accountCall(t, h.Patch(), http.MethodPost, patchPath, "employee", allowed, http.StatusForbidden)

	accountCall(t, h.Patch(), http.MethodPost, patchPath, owner, request(readVersion(t, db, id), "status", "养号7天"), http.StatusOK)
	day := boardToday()
	active := day.AddDate(0, 0, -2).Format(time.DateOnly)
	if _, err := db.ExecContext(ctx, `UPDATE nurture_records SET workflow='运行',active_since=$1,banked_days=2 WHERE account_id=$2`, active, id); err != nil {
		t.Fatal(err)
	}
	accountCall(t, h.Patch(), http.MethodPost, patchPath, owner, request(readVersion(t, db, id), "status", "停用"), http.StatusOK)
	var banked int
	var workflow, periods string
	wantPeriods, _ := appendNurturePeriod("[]", active, day.Format(time.DateOnly), true)
	if err := db.QueryRowContext(ctx, `SELECT workflow,banked_days,periods FROM nurture_records WHERE account_id=$1`, id).Scan(&workflow, &banked, &periods); err != nil || workflow != "暂停" || banked != 4 || periods != wantPeriods {
		t.Fatalf("nurture side effect = %s, %d, %s, err=%v", workflow, banked, periods, err)
	}

	create := map[string]any{"name": "合成新增", "computer_id": "unassigned", "slot": 1, "status": "待确认", "office_id": "go-east", "request_id": accountRequestID(t)}
	created := accountCall(t, h.Save(true), http.MethodPost, createPath, owner, create, http.StatusOK)
	createdID := created["id"].(string)
	edit := map[string]any{"id": createdID, "name": "合成详情改名", "computer_id": "unassigned", "slot": 1, "status": "待确认", "office_id": "go-east",
		"request_id": accountRequestID(t), "_expected_versions": map[string]any{"account:" + createdID: readVersion(t, db, createdID)}}
	modified := accountCall(t, h.Save(false), http.MethodPost, editPath, owner, edit, http.StatusOK)
	if modified["account"] != nil || modified["id"] != createdID {
		t.Fatalf("full edit result = %v", modified)
	}
	var updatedName string
	if err := db.QueryRowContext(ctx, `SELECT name FROM accounts WHERE id=$1`, createdID).Scan(&updatedName); err != nil || updatedName != "合成详情改名" {
		t.Fatalf("full editor did not persist new name: %q, err=%v", updatedName, err)
	}
	bulk := map[string]any{"ids": []string{id, createdID}, "status": "正常", "request_id": accountRequestID(t), "_expected_versions": map[string]any{
		"account:" + id: readVersion(t, db, id), "account:" + createdID: readVersion(t, db, createdID)}}
	accountCall(t, h.BulkStatus(), http.MethodPost, bulkPath, owner, bulk, http.StatusOK)
	var changed int
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM accounts WHERE id IN ($1,$2) AND status='正常'`, id, createdID).Scan(&changed); err != nil || changed != 2 {
		t.Fatalf("bulk update matched %d, err=%v", changed, err)
	}
	device := "account-device-test-" + group
	if _, err := db.ExecContext(ctx, `INSERT INTO phone_devices(id,name) VALUES($1,'合成手机')`, device); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(ctx, `INSERT INTO phone_device_offices(device_id,office_id) VALUES($1,'go-east')`, device); err != nil {
		t.Fatal(err)
	}
	tx, err := db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		t.Fatal(err)
	}
	records, err := inventory.AssignmentRecords(ctx, tx, createdID, device)
	_ = tx.Rollback()
	if err != nil {
		t.Fatal(err)
	}
	versions := map[string]any{"account:" + createdID: readVersion(t, db, createdID)}
	for key, row := range records {
		if err := addVersion(versions, key, row); err != nil {
			t.Fatal(err)
		}
	}
	assign := map[string]any{"id": createdID, "phone_device_id": device, "request_id": accountRequestID(t), "_expected_versions": versions}
	accountCall(t, h.Field(phonePath), http.MethodPost, phonePath, owner, assign, http.StatusOK)
	var assigned sql.NullString
	if err := db.QueryRowContext(ctx, `SELECT phone_device_id FROM accounts WHERE id=$1`, createdID).Scan(&assigned); err != nil || assigned.String != device {
		t.Fatalf("phone assignment = %q, err=%v", assigned.String, err)
	}
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM operations_history WHERE entity=$1`, "access:"+createdID).Scan(&histories); err != nil || histories != 1 {
		t.Fatalf("phone assignment did not record the workflow history: %d, err=%v", histories, err)
	}
	dayKey := "daily:" + createdID + ":2026-11-08"
	batch := map[string]any{"kind": "daily", "ids": []string{createdID}, "patch": map[string]any{"published": true, "date": "2026-11-08"},
		"request_id": accountRequestID(t), "_expected_versions": map[string]any{"account:" + createdID: readVersion(t, db, createdID), dayKey: nil}}
	accountCall(t, h.Batch(), http.MethodPost, batchPath, owner, batch, http.StatusOK)
	var published int64
	if err := db.QueryRowContext(ctx, `SELECT published FROM daily WHERE account_id=$1 AND day='2026-11-08'`, createdID).Scan(&published); err != nil || published != 1 {
		t.Fatalf("batch publish = %d, err=%v", published, err)
	}
	importTx, err := db.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer importTx.Rollback()
	edit["nurture_started"] = "2026-10-01"
	importActor := session.User{ID: 9101, Name: "Synthetic Owner", Role: "owner", Permissions: []string{"account_manage", "account_status"}}
	if _, err := SaveAccount(ctx, importTx, importActor, edit, false); err != nil {
		t.Fatal(err)
	}
	var nurtureDate string
	if err := importTx.QueryRowContext(ctx, `SELECT nurture_started FROM accounts WHERE id=$1`, createdID).Scan(&nurtureDate); err != nil || nurtureDate != edit["nurture_started"] {
		t.Fatalf("imported date = %q, err=%v", nurtureDate, err)
	}
}

func TestPostgresInventoryBatchSharesHistoryGroup(t *testing.T) {
	db := accountTestDB(t)
	ctx := t.Context()
	h := WriteHandler{Service: accountwrite.Service{DB: db, WriterEpoch: 17, RestoreGeneration: "go-daily-synthetic-generation"}}
	group, err := accountwrite.NewGroup()
	if err != nil {
		t.Fatal(err)
	}
	seed, err := strconv.ParseUint(group[:8], 16, 32)
	if err != nil {
		t.Fatal(err)
	}
	var cards []string
	for i := range 2 {
		var id int64
		phone := fmt.Sprintf("139%08d", (seed+uint64(i))%100000000)
		if err := db.QueryRowContext(ctx, `INSERT INTO phone_cards(phone,owner) VALUES($1,'合成旧归属') RETURNING id`, phone).Scan(&id); err != nil {
			t.Fatal(err)
		}
		if _, err := db.ExecContext(ctx, `INSERT INTO phone_card_offices(card_id,office_id) VALUES($1,'go-east')`, id); err != nil {
			t.Fatal(err)
		}
		cards = append(cards, strconv.FormatInt(id, 10))
	}
	deviceID := "phone:" + group + ":905"
	if _, err := db.ExecContext(ctx, `INSERT INTO phone_devices(id,name) VALUES($1,'合成设备')`, deviceID); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(ctx, `INSERT INTO phone_device_offices(device_id,office_id) VALUES($1,'go-east')`, deviceID); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(ctx, `INSERT INTO phone_units(id,device_id,original_device_id,generation,status,created_at)
VALUES($1,$2,$2,1,'待确认',0)`, "unit-"+group, deviceID); err != nil {
		t.Fatal(err)
	}
	tx, err := db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	versions := map[string]any{}
	for _, id := range cards {
		number, _ := strconv.ParseInt(id, 10, 64)
		card, err := inventory.CardRecord(ctx, tx, number)
		if err != nil {
			t.Fatal(err)
		}
		if err := addVersion(versions, "card:"+id, card); err != nil {
			t.Fatal(err)
		}
	}
	device, err := inventory.DeviceRecord(ctx, tx, deviceID)
	if err != nil {
		t.Fatal(err)
	}
	if err := addVersion(versions, "device:"+deviceID, device); err != nil {
		t.Fatal(err)
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	var priorAudits int
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM auth_audit WHERE action=$1`, batchPath).Scan(&priorAudits); err != nil {
		t.Fatal(err)
	}
	batch := func(kind string, ids []string) map[string]any {
		return map[string]any{"kind": kind, "ids": ids, "patch": map[string]any{"owner": "批量合成归属"},
			"request_id": accountRequestID(t), "_expected_versions": versions}
	}
	accountCall(t, h.Batch(), http.MethodPost, batchPath, "owner", batch("cards", cards), http.StatusOK)
	accountCall(t, h.Batch(), http.MethodPost, batchPath, "owner", batch("devices", []string{deviceID}), http.StatusOK)
	var histories, groups, audits int
	if err := db.QueryRowContext(ctx, `SELECT count(*),count(DISTINCT group_id) FROM operations_history
WHERE action=$1 AND entity IN ($2,$3)`, batchPath, "card:"+cards[0], "card:"+cards[1]).Scan(&histories, &groups); err != nil || histories != 2 || groups != 1 {
		t.Fatalf("batch histories = %d in %d groups, err=%v", histories, groups, err)
	}
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM auth_audit WHERE action=$1`, batchPath).Scan(&audits); err != nil || audits != priorAudits+2 {
		t.Fatalf("batch audit count = %d, prior=%d, err=%v", audits, priorAudits, err)
	}
}

func accountTestDB(t *testing.T) *sql.DB {
	t.Helper()
	port := os.Getenv("BOARD_GO_ACCOUNT_TEST_PORT")
	if port == "" {
		t.Skip("set BOARD_GO_ACCOUNT_TEST_PORT to run against the disposable loopback fixture")
	}
	if value, err := strconv.Atoi(port); err != nil || value < 1024 || value > 65535 {
		t.Fatal("test port must be a valid nonprivileged loopback port")
	}
	config, err := pgx.ParseConfig(fmt.Sprintf("host=127.0.0.1 port=%s dbname=synthetic_account_write user=synthetic_owner sslmode=disable", port))
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
	if err := db.QueryRowContext(ctx, `SELECT current_database()='synthetic_account_write'
AND (SELECT array_agg(kind) FROM board_app.environment)=ARRAY['synthetic-only']::text[]`).Scan(&safe); err != nil || !safe {
		t.Fatalf("requires the account-only synthetic clone: %v", err)
	}
	return db
}

func readVersion(t *testing.T, db *sql.DB, id string) string {
	t.Helper()
	result := accountCall(t, Handler{DB: db}.Inline, http.MethodGet, "/api/account/inline?id="+id, "owner", nil, http.StatusOK)
	return result["record_version"].(string)
}

func accountRequestID(t *testing.T) string {
	t.Helper()
	raw, err := accountwrite.NewGroup()
	if err != nil {
		t.Fatal(err)
	}
	return strings.Join([]string{raw[:8], raw[8:12], raw[12:16], raw[16:20], raw[20:]}, "-")
}

func accountCall(t *testing.T, handler http.HandlerFunc, method, path, role string, body map[string]any, status int) map[string]any {
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
	req.AddCookie(&http.Cookie{Name: "board_session", Value: "synthetic-go-daily-" + role + "-cookie"})
	req.Header.Set("X-CSRF-Token", "synthetic-go-daily-"+role+"-csrf")
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
