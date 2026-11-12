package inventory

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/session"
)

func TestWorkflowSyntheticPostgres(t *testing.T) {
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
	actor := session.User{Role: "owner", ID: 9101, Name: "Synthetic Owner",
		Permissions: []string{"device_manage", "device_create", "card_create", "card_manage", "nurture_manage"}}
	statusIs := func(err error, status int) {
		t.Helper()
		var failure accountwrite.APIError
		if !errors.As(err, &failure) || failure.Status != status {
			t.Fatalf("expected %d, got %v", status, err)
		}
	}
	action := func(body map[string]any, status int) map[string]any {
		t.Helper()
		if err := authorizeWorkflow(ctx, tx, actor, body); err != nil {
			t.Fatal(err)
		}
		refs, err := workflowLoadRefs(ctx, tx, body)
		if err != nil {
			t.Fatal(err)
		}
		records, err := workflowSnapshot(ctx, tx, refs.keys(body["pause_nurture"] == true))
		if err != nil {
			t.Fatal(err)
		}
		versions, err := records.versions()
		if err != nil {
			t.Fatal(err)
		}
		if _, ok := body["_expected_versions"]; !ok {
			body["_expected_versions"] = versions
		}
		result, err := applyWorkflow(ctx, tx, actor, body)
		if status != http.StatusOK {
			statusIs(err, status)
			return nil
		}
		if err != nil {
			t.Fatal(err)
		}
		return result
	}
	makeCard := func(phone string) int64 {
		t.Helper()
		card, err := SaveCard(ctx, tx, actor, map[string]any{"phone": phone, "office_ids": []string{"go-east"}})
		if err != nil {
			t.Fatal(err)
		}
		return card["id"].(int64)
	}
	device, err := SaveDevice(ctx, tx, actor, map[string]any{"number": "819001", "office_ids": []string{"go-east"}, "state": "可用闲置"}, true)
	if err != nil {
		t.Fatal(err)
	}
	deviceID := device["id"].(string)
	unit, err := activeUnit(ctx, tx, deviceID)
	if err != nil || unit == nil {
		t.Fatalf("synthetic unit missing: %v", err)
	}
	oldID := stringField(unit, "id")
	card, second := makeCard("19900000010"), makeCard("19900000011")
	action(map[string]any{"action": "place_card", "card_id": card, "unit_id": oldID, "slot": "1"}, http.StatusBadRequest)
	action(map[string]any{"action": "place_card", "card_id": card, "unit_id": oldID, "slot": "1", "confirmed_usable": true}, http.StatusOK)
	action(map[string]any{"action": "place_card", "card_id": second, "unit_id": oldID, "slot": "1", "confirmed_usable": true}, http.StatusBadRequest)
	action(map[string]any{"action": "unit_status", "unit_id": oldID, "status": "可用闲置"}, http.StatusBadRequest)
	action(map[string]any{"action": "unit_status", "unit_id": oldID, "status": "故障", "_expected_versions": map[string]any{}}, http.StatusConflict)
	const accountA, accountB = "go-east-missing", "go-east-account"
	for _, id := range []string{accountA, accountB} {
		if err := AssignAccountDevice(ctx, tx, id, deviceID); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO nurture_records(account_id,workflow,active_since,banked_days,periods)
		VALUES($1,'运行','2026-10-10',1,'[]') ON CONFLICT(account_id) DO UPDATE SET
		workflow=excluded.workflow,active_since=excluded.active_since,banked_days=excluded.banked_days,periods=excluded.periods`, accountA); err != nil {
		t.Fatal(err)
	}
	action(map[string]any{"action": "unit_status", "unit_id": oldID, "status": "故障"}, http.StatusOK)
	caseResult := action(map[string]any{"action": "open_case", "unit_id": oldID, "reason": "合成故障",
		"affected_account_ids": []any{accountA, accountB}, "pause_nurture": true}, http.StatusOK)
	caseID := caseResult["case_id"].(string)
	action(map[string]any{"action": "open_case", "unit_id": oldID, "reason": "重复故障"}, http.StatusBadRequest)
	action(map[string]any{"action": "update_case", "case_id": caseID, "status": "暂挂"}, http.StatusBadRequest)
	action(map[string]any{"action": "update_case", "case_id": caseID, "status": "暂挂", "followup_on": "2026-12-01"}, http.StatusOK)
	action(map[string]any{"action": "update_case", "case_id": caseID, "status": "已结案", "closure_reason": "尚未处理"}, http.StatusBadRequest)
	nurture, err := readRecord(ctx, tx, nurtureRelation, accountA)
	if err != nil || nurture["workflow"] != "暂停" || !strings.Contains(stringField(nurture, "periods"), `"start": "2026-10-10"`) {
		t.Fatalf("requested nurture pause missing: %+v %v", nurture, err)
	}
	replacement := action(map[string]any{"action": "replace_unit", "device_id": deviceID, "new_status": "可用闲置"}, http.StatusOK)
	newID := replacement["unit_id"].(string)
	if newID == oldID || replacement["old_unit_id"] != oldID {
		t.Fatalf("replacement identity wrong: %+v", replacement)
	}
	placement, err := readRecord(ctx, tx, placementRelation, card)
	if err != nil || stringField(placement, "unit_id") != oldID {
		t.Fatalf("new phone inherited old SIM: %+v %v", placement, err)
	}
	access, err := readRecord(ctx, tx, accessRelation, accountA)
	if err != nil || stringField(access, "last_unit_id") != oldID || access["login_state"] != "登录受阻" {
		t.Fatalf("new phone inherited login or old device was overwritten: %+v %v", access, err)
	}
	action(map[string]any{"action": "account_recovery", "account_id": accountA, "case_id": caseID, "target_unit_id": newID,
		"recovery_state": "恢复成功"}, http.StatusBadRequest)
	action(map[string]any{"action": "account_recovery", "account_id": accountA, "case_id": caseID, "target_unit_id": newID,
		"recovery_state": "等待协助", "reason": "旧机核验"}, http.StatusOK)
	access, err = readRecord(ctx, tx, accessRelation, accountA)
	if err != nil || stringField(access, "last_unit_id") != oldID || access["login_state"] != "登录受阻" {
		t.Fatalf("unconfirmed recovery moved a login: %+v %v", access, err)
	}
	action(map[string]any{"action": "account_recovery", "account_id": accountA, "case_id": caseID, "target_unit_id": newID,
		"recovery_state": "恢复成功", "confirmed_login": true}, http.StatusOK)
	action(map[string]any{"action": "account_recovery", "account_id": accountB, "case_id": caseID,
		"recovery_state": "暂不恢复", "reason": "待授权"}, http.StatusOK)
	action(map[string]any{"action": "update_case", "case_id": caseID, "status": "已结案", "closure_reason": "一台恢复，另一台待授权"}, http.StatusOK)
	access, err = readRecord(ctx, tx, accessRelation, accountA)
	if err != nil || stringField(access, "last_unit_id") != newID || access["login_state"] != "可操作" {
		t.Fatalf("confirmed recovery did not move login: %+v %v", access, err)
	}
	action(map[string]any{"action": "place_card", "card_id": card, "unit_id": newID, "slot": "2", "confirmed_usable": true}, http.StatusOK)
	placement, err = readRecord(ctx, tx, placementRelation, card)
	if err != nil || stringField(placement, "unit_id") != newID || stringField(placement, "slot") != "2" {
		t.Fatalf("explicit move failed: %+v %v", placement, err)
	}
	action(map[string]any{"action": "reassign_unit", "unit_id": oldID, "number": "819001", "confirmed_usable": true}, http.StatusBadRequest)
	action(map[string]any{"action": "reassign_unit", "unit_id": oldID, "number": "819002"}, http.StatusBadRequest)
	reassigned := action(map[string]any{"action": "reassign_unit", "unit_id": oldID, "number": "819002", "confirmed_usable": true}, http.StatusOK)
	reassignedID := reassigned["device_id"].(string)
	account, err := readRecord(ctx, tx, accountRelation, accountB)
	if err != nil || stringField(account, "phone_device_id") != reassignedID {
		t.Fatalf("registered device did not follow the same physical unit: %+v %v", account, err)
	}
	access, err = readRecord(ctx, tx, accessRelation, accountB)
	if err != nil || stringField(access, "last_unit_id") != oldID || access["login_state"] != "登录受阻" {
		t.Fatalf("renumbering changed physical unit or confirmed login: %+v %v", access, err)
	}
	employee := session.User{Role: "employee", ID: 9102, OfficeIDs: []string{"go-west"}, Permissions: []string{"device_manage", "card_manage"}}
	statusIs(authorizeWorkflow(ctx, tx, employee, map[string]any{"action": "place_card", "card_id": card, "unit_id": newID}), http.StatusForbidden)
	var auditCount, reloadCount int
	err = tx.QueryRowContext(ctx, `SELECT count(*) FROM auth_audit WHERE action=$1`, WorkflowPath).Scan(&auditCount)
	if err != nil || auditCount < 4 {
		t.Fatalf("workflow audit absent: %d %v", auditCount, err)
	}
	err = tx.QueryRowContext(ctx, `SELECT count(*) FROM operations_history WHERE action=$1 AND entity='sync:reload'`, WorkflowPath).Scan(&reloadCount)
	if err != nil || reloadCount < 4 {
		t.Fatalf("workflow reload absent: %d %v", reloadCount, err)
	}
}
