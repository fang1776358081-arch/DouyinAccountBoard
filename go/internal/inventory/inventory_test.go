package inventory

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/session"

	_ "github.com/jackc/pgx/v5/stdlib"
)

func TestInventoryValidation(t *testing.T) {
	if id, serial := deviceIdentity("office3:00029"); id != "office3:29" || serial != "29" {
		t.Fatalf("legacy identity = %q %q", id, serial)
	}
	if id, _ := deviceIdentity("29号手机"); id != "" {
		t.Fatal("display label accepted as internal ID")
	}
	if validPhone("１9000000000") || !validPhone("19000000000") {
		t.Fatal("unexpected phone validation")
	}
	if _, err := cardValues(map[string]any{"phone": "19000000000", "return_state": "已归还"}, nil); err == nil {
		t.Fatal("return was accepted without its actual date")
	}
}

func TestInventorySyntheticPostgres(t *testing.T) {
	rawPort := os.Getenv("BOARD_GO_DAILY_TEST_PORT")
	if rawPort == "" {
		t.Skip("requires disposable PostgreSQL fixture")
	}
	port, err := strconv.ParseUint(rawPort, 10, 16)
	if err != nil || port == 0 {
		t.Fatal("invalid synthetic fixture port")
	}
	db, err := sql.Open("pgx", fmt.Sprintf("postgres://synthetic_owner@127.0.0.1:%d/synthetic_probe?sslmode=disable&search_path=board_candidate,pg_catalog", port))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	var marker string
	if err := db.QueryRowContext(ctx, "SELECT kind FROM board_app.environment").Scan(&marker); err != nil || marker != "synthetic-only" {
		t.Fatalf("synthetic guard failed: %v", err)
	}
	var epoch int64
	var generation string
	if err := db.QueryRowContext(ctx, "SELECT writer_epoch,restore_generation FROM runtime_control WHERE id=1").Scan(&epoch, &generation); err != nil {
		t.Fatal(err)
	}
	tx, err := session.BeginWrite(ctx, db, epoch, generation)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	actor := func(role string) session.User {
		t.Helper()
		r := httptest.NewRequest(http.MethodGet, "/", nil)
		r.AddCookie(&http.Cookie{Name: "board_session", Value: "synthetic-go-daily-" + role + "-cookie"})
		user, err := session.AuthenticateWith(ctx, tx, r)
		if err != nil {
			t.Fatal(err)
		}
		return user
	}
	owner, employee := actor("owner"), actor("employee")
	employee.Permissions = []string{"card_tasks", "card_import", "device_create", "device_manage"}
	mustStatus := func(err error, want int) {
		t.Helper()
		var failure accountwrite.APIError
		if !errors.As(err, &failure) || failure.Status != want {
			t.Fatalf("expected status %d, got %v", want, err)
		}
	}
	receipt, err := SaveCard(ctx, tx, owner, map[string]any{
		"id": nil, "phone": "19000000007", "owner": "合成实名持有人", "label": "synthetic",
		"status": "正常", "carrier": "其他", "custom_carrier": "中兴", "office_ids": []string{"go-east"},
	})
	if err != nil {
		t.Fatal(err)
	}
	cardID := receipt["id"].(int64)
	key := fmt.Sprintf("card:%d", cardID)
	versions := receipt["_record_versions"].(map[string]any)
	_, err = SaveCard(ctx, tx, employee, map[string]any{"id": cardID, "owner": "意外覆盖", "_expected_versions": versions})
	mustStatus(err, http.StatusForbidden)
	_, err = SaveCard(ctx, tx, employee, map[string]any{"id": cardID, "status": "待注销"})
	mustStatus(err, http.StatusConflict)
	receipt, err = SaveCard(ctx, tx, employee, map[string]any{"id": cardID, "status": "待注销", "_expected_versions": versions})
	if err != nil {
		t.Fatal(err)
	}
	card, err := CardRecord(ctx, tx, cardID)
	if err != nil || card["owner"] != "合成实名持有人" || card["carrier"] != "中兴" || card["status"] != "待注销" {
		t.Fatalf("task update changed base fields or lost the status: %+v %v", card, err)
	}
	_, err = SaveCard(ctx, tx, employee, map[string]any{"id": cardID, "status": "正常", "_expected_versions": versions})
	mustStatus(err, http.StatusConflict)
	var scoped int
	if err := tx.QueryRowContext(ctx, `SELECT count(*) FROM operations_history WHERE entity=$1 AND scope_offices='["go-east"]'`, key).Scan(&scoped); err != nil || scoped != 2 {
		t.Fatalf("expected two scoped history rows, got %d: %v", scoped, err)
	}

	// Inline writes must preserve legacy values in all other fields.
	if _, err := tx.ExecContext(ctx, `UPDATE phone_cards SET code_state='历史接码状态' WHERE id=$1`, cardID); err != nil {
		t.Fatal(err)
	}
	card, err = CardRecord(ctx, tx, cardID)
	if err != nil {
		t.Fatal(err)
	}
	versions, err = (snapshot{key: card}).versions()
	if err != nil {
		t.Fatal(err)
	}
	manager := employee
	manager.Permissions = []string{"card_manage"}
	inlineOwner := map[string]any{"id": cardID, "changes": map[string]any{"owner": "合成新卡主"}, "_expected_versions": versions}
	inlineStatus := map[string]any{"id": cardID, "changes": map[string]any{"status": "停用"}, "_expected_versions": versions}
	mustStatus(AuthorizeCard(ctx, tx, employee, inlineOwner), http.StatusForbidden)
	mustStatus(AuthorizeCard(ctx, tx, manager, inlineStatus), http.StatusForbidden)
	crossOffice := manager
	crossOffice.OfficeIDs = []string{"go-west"}
	mustStatus(AuthorizeCard(ctx, tx, crossOffice, inlineOwner), http.StatusForbidden)
	receipt, err = SaveCard(ctx, tx, manager, inlineOwner)
	if err != nil {
		t.Fatal(err)
	}
	_, err = SaveCard(ctx, tx, employee, inlineStatus)
	mustStatus(err, http.StatusConflict)
	inlineStatus["_expected_versions"] = receipt["_record_versions"]
	if _, err := SaveCard(ctx, tx, employee, inlineStatus); err != nil {
		t.Fatal(err)
	}
	card, err = CardRecord(ctx, tx, cardID)
	if err != nil || card["owner"] != "合成新卡主" || card["status"] != "停用" ||
		card["code_state"] != "历史接码状态" || card["carrier"] != "中兴" || card["label"] != "synthetic" ||
		strings.Join(recordOffices(card), ",") != "go-east" {
		t.Fatalf("inline write did not preserve unrelated values and scope: %+v %v", card, err)
	}

	first, err := SaveDevice(ctx, tx, employee, map[string]any{"number": "800001", "office_ids": []string{"go-east"}, "state": "auto"}, true)
	if err != nil {
		t.Fatal(err)
	}
	eastID := first["id"].(string)
	_, err = SaveDevice(ctx, tx, employee, map[string]any{"number": "800001", "office_ids": []string{"go-east"}}, true)
	mustStatus(err, http.StatusBadRequest)
	_, err = SaveDevice(ctx, tx, employee, map[string]any{"number": "800002", "office_ids": []string{"go-west"}}, true)
	mustStatus(err, http.StatusForbidden)
	second, err := SaveDevice(ctx, tx, owner, map[string]any{"number": "800001", "office_ids": []string{"go-west"}}, true)
	if err != nil {
		t.Fatal(err)
	}
	westID := second["id"].(string)
	_, err = SaveDevice(ctx, tx, employee, map[string]any{"id": westID, "_expected_versions": second["_record_versions"]}, false)
	mustStatus(err, http.StatusForbidden)

	const accountID = "go-east-missing"
	before, err := AssignmentRecords(ctx, tx, accountID, eastID)
	if err != nil || before["device:"+eastID] == nil {
		t.Fatalf("expected tracked versions: %v", err)
	}
	if err := AssignAccountDevice(ctx, tx, accountID, eastID); err != nil {
		t.Fatal(err)
	}
	after, err := RefreshAssignmentRecords(ctx, tx, accountID, "", eastID, before)
	if err != nil || stringField(after["access:"+accountID], "last_unit_id") == "" {
		t.Fatalf("physical unit relation was not set: %v", err)
	}
	nextVersions, err := snapshot(after).versions()
	if err != nil {
		t.Fatal(err)
	}
	_, err = SaveDevice(ctx, tx, employee, map[string]any{"id": eastID, "state": "可用闲置", "_expected_versions": nextVersions}, false)
	mustStatus(err, http.StatusBadRequest)
	if _, err := tx.ExecContext(ctx, "UPDATE phone_account_access SET login_state='登录受阻' WHERE account_id=$1", accountID); err != nil {
		t.Fatal(err)
	}
	mustStatus(AssignAccountDevice(ctx, tx, accountID, ""), http.StatusBadRequest)
	if _, err := tx.ExecContext(ctx, "UPDATE phone_account_access SET login_state='待确认' WHERE account_id=$1", accountID); err != nil {
		t.Fatal(err)
	}
	accounts := []map[string]any{{"id": accountID, "status": "停用", "phone_device_id": eastID}}
	devices, err := Devices(ctx, tx, employee, accounts)
	if err != nil {
		t.Fatal(err)
	}
	var foundEast bool
	for _, row := range devices["devices"].([]map[string]any) {
		switch row["id"] {
		case westID:
			t.Fatal("West device leaked")
		case eastID:
			foundEast = row["number"] == "800001" && row["state"] == "待清理" && row["account_count"] == 1
		}
	}
	if !foundEast || !strings.HasSuffix(stringField(accounts[0], "phone_device_name"), "800001号手机") {
		t.Fatal("device summary did not preserve association/occupancy")
	}
	if _, err := tx.ExecContext(ctx, "UPDATE accounts SET registration_phone=CASE id WHEN 'go-east-missing' THEN '19000000008' ELSE '19000000009' END WHERE id IN ('go-east-missing','go-west-account')"); err != nil {
		t.Fatal(err)
	}
	options, err := Cards(ctx, tx, employee, nil)
	if err != nil || options["importable"] != 1 {
		t.Fatalf("visible import count wrong: %v", err)
	}
	for _, added := range []int{1, 0} {
		result, err := importCards(ctx, tx, employee, map[string]any{"office_ids": []string{"go-east"}})
		if err != nil || result["added"] != added {
			t.Fatalf("expected %d deduplicated import: %+v, %v", added, result, err)
		}
	}
}
