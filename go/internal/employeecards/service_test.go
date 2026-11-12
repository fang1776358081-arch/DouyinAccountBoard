package employeecards

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"reflect"
	"strconv"
	"testing"
	"time"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"

	_ "github.com/jackc/pgx/v5/stdlib"
)

func TestEmployeeCardsSyntheticPostgres(t *testing.T) {
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
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
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
		token, err := employeeKey()
		if err != nil {
			t.Fatal(err)
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO auth_sessions(token_hash,user_id,csrf,expires_at)
			SELECT encode(sha256(convert_to($1,'UTF8')),'hex'),id,'synthetic-employee-card-csrf',extract(epoch from now())::bigint+600
			FROM auth_users WHERE username=$2`, token, "go-daily-"+role); err != nil {
			t.Fatal(err)
		}
		request := httptest.NewRequest(http.MethodGet, "/", nil)
		request.AddCookie(&http.Cookie{Name: "board_session", Value: token})
		user, err := session.AuthenticateWith(ctx, tx, request)
		if err != nil {
			t.Fatal(err)
		}
		return user
	}
	owner, employee, viewer := actor("owner"), actor("employee"), actor("viewer")
	s := Service{Write: accountwrite.Service{DB: db, WriterEpoch: epoch, RestoreGeneration: generation}}
	statusIs := func(err error, status int) {
		t.Helper()
		var failure accountwrite.APIError
		if !errors.As(err, &failure) || failure.Status != status {
			t.Fatalf("expected %d, got %v", status, err)
		}
	}
	today := employeeToday()
	get := func(as session.User, query url.Values) dataRow {
		t.Helper()
		result, err := s.get(ctx, tx, as, query, today)
		if err != nil {
			t.Fatal(err)
		}
		return result
	}
	preview := func(action, id string, data dataRow) dataRow {
		t.Helper()
		version, err := employeeVersion(ctx, tx, owner, id, generation)
		if err != nil {
			t.Fatal(err)
		}
		result, err := s.preview(ctx, tx, owner, dataRow{"action": action, "employee_id": id, "data": data, "expected_version": version})
		if err != nil {
			t.Fatalf("%s preview: %v", action, err)
		}
		return result
	}
	apply := func(preview dataRow) (dataRow, dataRow) {
		t.Helper()
		requestID, err := employeeKey()
		if err != nil {
			t.Fatal(err)
		}
		request := dataRow{"preview_id": preview["preview_id"], "confirmation": "确认保存", "request_id": requestID}
		result, err := s.apply(ctx, tx, owner, request)
		if err != nil {
			t.Fatalf("%s apply: %v", preview["action"], err)
		}
		return result, request
	}
	metadata := get(owner, url.Values{})
	if metadata["available"] != true || len(resultRows(metadata["operators"])) < 6 {
		t.Fatalf("canonical migration was not recognized: %+v", metadata)
	}
	draft := preview("employee", "", dataRow{"name": "合成员工甲", "employee_no": "synthetic-ec-a",
		"office_id": "go-east", "employment_status": "active", "auth_user_id": employee.ID,
		"birth_date": "2005-11-10", "consent_source": "测试授权", "consent_on": today})
	var amount int
	if err := tx.QueryRowContext(ctx, "SELECT count(*) FROM employee_card_employees WHERE employee_no='synthetic-ec-a'").Scan(&amount); err != nil || amount != 0 {
		t.Fatalf("preview saved employee prematurely: %d %v", amount, err)
	}
	created, receipt := apply(draft)
	id := field(created, "employee_id")
	again, err := s.apply(ctx, tx, owner, receipt)
	if err != nil || !reflect.DeepEqual(created, again) {
		t.Fatalf("same request repeated the write: %+v %+v %v", created, again, err)
	}
	found := get(owner, url.Values{"request_id": {field(receipt, "request_id")}})
	if found["request_found"] != true || resultRow(found["result"])["employee_id"] != id {
		t.Fatalf("receipt lookup failed: %+v", found)
	}
	changed := dataRow{"preview_id": "different", "confirmation": "确认保存", "request_id": receipt["request_id"]}
	_, err = s.apply(ctx, tx, owner, changed)
	statusIs(err, http.StatusBadRequest)
	changedGeneration := s
	changedGeneration.Write.RestoreGeneration += "-new"
	_, err = changedGeneration.requestResult(ctx, tx, owner, field(receipt, "request_id"))
	statusIs(err, http.StatusConflict)
	if get(viewer, url.Values{})["total"] != 0 {
		t.Fatal("unlinked viewer could see an employee")
	}
	if self := get(employee, url.Values{"employee_id": {id}}); resultRow(self["employee"])["id"] != id {
		t.Fatal("employee could not read their own record")
	}
	list := get(owner, url.Values{"query": {"synthetic-ec-a"}})
	if rows := resultRows(list["employees"]); len(rows) != 1 || rows[0]["birth_date"] != nil {
		t.Fatal("list exposed employee birth date or lost row")
	}
	card := func(phone, ownerName, office, state string) int64 {
		t.Helper()
		var cardID int64
		if err := tx.QueryRowContext(ctx, `INSERT INTO phone_cards(phone,owner,status,code_state,return_state)
			VALUES($1,$2,$3,'无待办','不需归还') RETURNING id`, phone, ownerName, state).Scan(&cardID); err != nil {
			t.Fatal(err)
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO phone_card_offices(card_id,office_id) VALUES($1,$2)`, cardID, office); err != nil {
			t.Fatal(err)
		}
		return cardID
	}
	const phone = "19700000001"
	cardID := card(phone, "合成员工甲", "go-east", "待注销")
	replacementID := card("19700000002", "不同保管文字", "go-east", "正常")
	if _, err := tx.ExecContext(ctx, `UPDATE accounts SET registration_phone=$1,account_type='小号',card_stability='已稳定',
		password_state='已修改' WHERE id='go-east-missing'`, phone); err != nil {
		t.Fatal(err)
	}
	detail := func(as session.User) dataRow { return get(as, url.Values{"employee_id": {id}, "view": {"cards"}}) }
	auto := detail(owner)
	if ledger := resultRow(auto["card_ledger"]); numberValue(ledger["registered"]) != 1 || numberValue(ledger["name_matched_count"]) != 1 ||
		numberValue(ledger["candidates"]) != 1 || numberValue(ledger["pending_cancel"]) != 1 {
		t.Fatalf("auto-linked physical ledger wrong: %+v", ledger)
	}
	if summary := resultRow(auto["summary"]); summary["company_current"] != nil || summary["name_association_quota_unverified"] != true {
		t.Fatalf("name association was used as quota: %+v", summary)
	}
	batch := dataRow{"source": "合成实名来源", "cards": []dataRow{{"card_id": cardID, "operator_id": "minsheng", "activated_on": today}}}
	beforeCard, err := lookupCard(ctx, tx, owner, cardID, true)
	if err != nil {
		t.Fatal(err)
	}
	batchPreview := preview("card_link_batch", id, batch)
	if batchPreview["can_apply"] != true {
		t.Fatalf("expected allowed link: %+v", batchPreview)
	}
	apply(batchPreview)
	first := detail(owner)
	units := resultRows(first["units"])
	if len(units) != 1 || units[0]["phase"] != "active" {
		t.Fatalf("explicit link has no stable unit: %+v", units)
	}
	unitID := field(units[0], "id")
	duplicate := preview("card_link_batch", id, batch)
	counts := duplicate["counts"].(map[string]int)
	if counts["existing"] != 1 || counts["new"] != 0 {
		t.Fatalf("duplicate import produced a new unit: %+v", counts)
	}
	apply(duplicate)
	apply(preview("replacement", id, dataRow{"card_id": replacementID, "unit_id": unitID, "source": "合成补换卡"}))
	after := detail(owner)
	if len(resultRows(after["units"])) != 1 || len(resultRows(after["linked_cards"])) != 2 {
		t.Fatal("replacement was counted as a second quota or lost historical card")
	}
	apply(preview("cancel_request", id, dataRow{"unit_id": unitID, "requested_on": today, "source": "合成注销申请"}))
	if unit, err := readUnit(ctx, tx, id, unitID); err != nil || unit["phase"] != "active" {
		t.Fatalf("cancellation released an unconfirmed slot: %+v %v", unit, err)
	}
	apply(preview("operator", "", dataRow{"id": "minsheng", "release_mode": "confirmed", "effective_from": today}))
	apply(preview("release", id, dataRow{"unit_id": unitID, "released_on": today, "source": "合成运营商确认"}))
	apply(preview("release", id, dataRow{"unit_id": unitID, "released_on": today, "source": "合成运营商确认"}))
	if unit, err := readUnit(ctx, tx, id, unitID); err != nil || unit["phase"] != "released" || unit["release_source"] != "合成运营商确认" {
		t.Fatalf("release record incorrect: %+v %v", unit, err)
	}
	afterCard, err := lookupCard(ctx, tx, owner, cardID, true)
	tokenBefore, _ := recordwire.Version(beforeCard)
	tokenAfter, _ := recordwire.Version(afterCard)
	if err != nil || tokenBefore != tokenAfter {
		t.Fatal("employee workflow changed original phone card fields")
	}
	pendingCard := card("19700000004", "", "go-east", "正常")
	apply(preview("application", id, dataRow{"operator_id": "minsheng", "requested_on": today, "quantity": 2, "result": "pending",
		"source": "合成申请记录"}))
	pendingUnits := []string{}
	for _, row := range resultRows(detail(owner)["units"]) {
		if row["phase"] == "pending" {
			pendingUnits = append(pendingUnits, field(row, "id"))
		}
	}
	if len(pendingUnits) != 2 {
		t.Fatal("application did not create two stable pending units")
	}
	apply(preview("card_link", id, dataRow{"unit_id": pendingUnits[0], "card_id": pendingCard,
		"operator_id": "minsheng", "activated_on": today, "source": "合成运营商办理"}))
	if linked, err := readUnit(ctx, tx, id, pendingUnits[0]); err != nil || linked["phase"] != "active" || numberValue(linked["current_card_id"]) != pendingCard {
		t.Fatalf("pending activation created a different slot: %+v %v", linked, err)
	}
	apply(preview("rules", "", dataRow{"rules": []dataRow{{"id": "global-stock", "scope": "all", "period": "stock", "limit": 10,
		"source": "合成上限确认", "effective_from": today, "confirmed": true, "pending_counts": true}}}))
	apply(preview("verification", id, dataRow{"scope": "all", "period": "stock", "period_start": today, "period_end": today,
		"count": 5, "coverage": "total", "verified_on": today, "valid_until": today, "source": "合成名下核验",
		"pending_unit_ids": []string{pendingUnits[1]}, "company_inventory_complete": true}))
	checks := resultRows(detail(owner)["verifications"])
	if len(checks) != 1 || numberValue(checks[0]["count"]) != 5 {
		t.Fatalf("verified total was not preserved: %+v", checks)
	}
	if covered, _ := employeeStrings(checks[0]["unit_ids"]); !reflect.DeepEqual(covered, []string{pendingUnits[0]}) {
		t.Fatalf("verification should contain only the active unit: %+v", covered)
	}
	other, _ := apply(preview("employee", "", dataRow{"name": "合成员工乙", "employee_no": "synthetic-ec-b",
		"office_id": "go-west", "employment_status": "active"}))
	conflicted := preview("card_link_batch", field(other, "employee_id"), batch)
	if conflicted["can_apply"] != false || conflicted["has_conflicts"] != true {
		t.Fatalf("existing foreign unit should block an entire batch: %+v", conflicted)
	}
	requestID, err := employeeKey()
	if err != nil {
		t.Fatal(err)
	}
	_, err = s.apply(ctx, tx, owner, dataRow{"preview_id": conflicted["preview_id"], "confirmation": "确认保存", "request_id": requestID})
	statusIs(err, http.StatusConflict)
	card("19700000003", "合成员工甲", "go-west", "已注销")
	employee.Permissions = append(employee.Permissions, "card_manage")
	_, err = s.get(ctx, tx, employee, url.Values{"employee_id": {id}}, today)
	statusIs(err, http.StatusForbidden)
	scoped := get(employee, url.Values{"query": {"synthetic-ec-a"}, "view": {"cards"}})
	if ledger := resultRow(scoped["card_ledger_totals"]); ledger["registered"] != nil || ledger["scope_complete"] != false {
		t.Fatalf("unauthorized card counts leaked: %+v", ledger)
	}
}
