package tablesync

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"douyinboard/internal/session"
)

func TestTableSyncWriteHTTPFence(t *testing.T) {
	db := syntheticTestDB(t)
	writer := Writer{DB: db, WriterEpoch: 17, RestoreGeneration: "go-daily-synthetic-generation", PublicOrigin: "http://127.0.0.1:8080"}
	for _, check := range []struct {
		role, csrf, origin string
		epoch              int64
		status             int
	}{
		{"owner", "owner", "http://wrong.invalid", 17, 403},
		{"", "", "", 17, 401},
		{"owner", "viewer", "", 17, 403},
		{"employee", "employee", "", 17, 403},
		{"owner", "owner", "", 16, 409},
		{"owner", "owner", "http://127.0.0.1:8080", 17, 400},
	} {
		r := httptest.NewRequest(http.MethodPost, "/api/table-sync/pilot", strings.NewReader(`{"office_id":"outside-pilot"}`))
		r.Header.Set("Content-Type", "application/json")
		r.Header.Set("Origin", check.origin)
		if check.role != "" {
			r.AddCookie(&http.Cookie{Name: "board_session", Value: "synthetic-go-daily-" + check.role + "-cookie"})
			r.Header.Set("X-CSRF-Token", "synthetic-go-daily-"+check.csrf+"-csrf")
		}
		w := httptest.NewRecorder()
		writer.WriterEpoch = check.epoch
		writer.Write(w, r)
		if w.Code != check.status || w.Header().Get("Cache-Control") != "no-store" {
			t.Fatalf("table sync admission = %d, want %d: %s", w.Code, check.status, w.Body.String())
		}
	}
}

func TestTableSyncWritesSyntheticPostgreSQL(t *testing.T) {
	db := syntheticTestDB(t)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	for _, query := range []string{
		`INSERT INTO board_candidate.offices(id,name,active) VALUES ('three','Synthetic Office Three',1) ON CONFLICT DO NOTHING`,
		`INSERT INTO board_candidate.computers(id,name) VALUES ('office3:901','合成901号电脑')`,
		`INSERT INTO board_candidate.computer_offices VALUES ('office3:901','three')`,
		`INSERT INTO board_candidate.accounts(id,name,douyin_id,status,computer_id,slot,note) VALUES
		 ('go-v7-update','旧昵称','v7-exact-007','正常','office3:901',1,'保留原备注'),
		 ('go-v7-west','外部昵称','v7-west-008','正常','go-west-pc',41,'其他办公室保留')`,
		`INSERT INTO board_candidate.account_offices VALUES ('go-v7-update','three'),('go-v7-west','go-west')`,
	} {
		if _, err := tx.ExecContext(ctx, query); err != nil {
			t.Fatal(err)
		}
	}
	const generation = "go-daily-synthetic-generation"
	const day = "2026-11-10"
	actor := session.User{ID: 9101, Role: "owner", Name: "Synthetic Owner"}
	call := func(action string, body item, status int) item {
		t.Helper()
		body["office_id"] = "three"
		if body["request_id"] == nil {
			body["request_id"] = nextID(true)
		}
		value, err := executeWrite(ctx, tx, actor, generation, "/api/table-sync/"+action, body, day)
		if status == 200 {
			if err != nil {
				t.Fatalf("%s failed: %v", action, err)
			}
			return value
		}
		var api readError
		if !errors.As(err, &api) || api.status != status {
			t.Fatalf("%s expected %d, got %v", action, status, err)
		}
		return nil
	}
	_, revision, err := currentPilot(ctx, tx, "three")
	if err != nil {
		t.Fatal(err)
	}
	stateBody := item{"state": "enabled", "confirmation": "开启办公室三试点", "expected_revision": revision}
	state := call("pilot", stateBody, 200)
	if !same(state, call("pilot", stateBody, 200)) {
		t.Fatal("pilot idempotency failed")
	}
	call("pilot", item{"state": "paused", "confirmation": "暂停办公室三试点", "expected_revision": revision}, 409)
	accountHeader := "来源行,注册手机号,昵称,登录手机序号,抖音号,小号说明,门头,负责人,登记电脑序号,来源类型,来源账号状态,违规备注,使用状态,注册日期,仅供测试的私密列\n"
	accountCSV := accountHeader +
		"2,,新昵称,29,v7-exact-007,,,合成负责人,901,,正常,,,2025-01-01,SYNTHETIC_PRIVATE_NEVER_PERSIST\n" +
		"3,,外部不更新,,v7-west-008,,,,901,,正常,,,,\n" +
		"4,,新增昵称,,v7-add-009,,,,901,,正常,,,,\n"
	progressHeader := "来源行,电脑序号,负责人,临时负责人,类别,发布设备1,发布设备2,发布设备3,发布设备4,发布设备5,发布设备6,发布设备7,发布设备8,发布设备9,发布设备10\n"
	progressCSV := progressHeader + "2,901,,,,抖音号:v7-exact-007,抖音号:v7-add-009,同名不匹配,,,,,,,\n"
	files := func(mode string) item {
		return item{"mode": mode, "source_round": "同一轮合成", "accounts_file": item{"filename": "account.csv", "content": base64.StdEncoding.EncodeToString([]byte(accountCSV))},
			"progress_file": item{"filename": "progress.csv", "content": base64.StdEncoding.EncodeToString([]byte(progressCSV))}}
	}
	findSelected := func(result item) []any {
		t.Helper()
		selected := make([]any, 0)
		for _, row := range asRows(result["rows"]) {
			if row["selected"] == true {
				selected = append(selected, row["row_key"])
			}
		}
		return selected
	}
	previewUpdate := call("preview", files("update"), 200)
	chosen := findSelected(previewUpdate)
	if len(chosen) != 1 || chosen[0] != "account:0:2" {
		t.Fatalf("only the intended unique same-office update should be selectable: %+v", previewUpdate["rows"])
	}
	if raw, err := json.Marshal(previewUpdate); err != nil || strings.Contains(string(raw), "SYNTHETIC_PRIVATE_NEVER_PERSIST") {
		t.Fatal("preview included a private source column")
	}
	var name, note string
	read := func(id string) {
		t.Helper()
		if err := tx.QueryRowContext(ctx, `SELECT name,note FROM board_candidate.accounts WHERE id=$1`, id).Scan(&name, &note); err != nil {
			t.Fatal(err)
		}
	}
	read("go-v7-update")
	if name != "旧昵称" {
		t.Fatal("preview changed an actual row")
	}
	applyBody := item{"preview_id": previewUpdate["preview_id"], "selected_rows": chosen, "confirmation": confirmation}
	if _, err := tx.ExecContext(ctx, `UPDATE board_candidate.accounts SET note='新并发备注' WHERE id='go-v7-update'`); err != nil {
		t.Fatal(err)
	}
	call("apply", applyBody, 409)
	previewUpdate = call("preview", files("update"), 200)
	applyBody = item{"preview_id": previewUpdate["preview_id"], "selected_rows": findSelected(previewUpdate), "confirmation": confirmation}
	updated := call("apply", applyBody, 200)
	if !same(updated, call("apply", applyBody, 200)) {
		t.Fatal("same apply request did not return its original receipt")
	}
	read("go-v7-update")
	if name != "新昵称" || note != "新并发备注" {
		t.Fatal("apply did not preserve unplanned fields")
	}
	read("go-v7-west")
	if name != "外部昵称" || note != "其他办公室保留" {
		t.Fatal("apply touched another office")
	}
	var rawSource string
	if err := tx.QueryRowContext(ctx, `SELECT source_data FROM board_candidate.table_sync_account_sources WHERE account_id='go-v7-update'`).Scan(&rawSource); err != nil || strings.Contains(rawSource, "SYNTHETIC_PRIVATE_NEVER_PERSIST") {
		t.Fatalf("source guard failed: %v", err)
	}
	batch := updated["batch_id"]
	issues := asRows(updated["receipt"].(item)["issues"])
	if len(issues) < 2 {
		t.Fatal("expected office and progress issues on receipt")
	}
	call("review", item{"batch_id": batch, "issue_id": issues[0]["id"], "state": "confirmed_no_action", "note": "合成人工核对"}, 200)
	if _, err := tx.ExecContext(ctx, `UPDATE board_candidate.accounts SET note='后续无关编辑' WHERE id='go-v7-update'`); err != nil {
		t.Fatal(err)
	}
	reverse := call("rollback-preview", item{"batch_id": batch}, 200)
	if len(findSelected(reverse)) != 1 {
		t.Fatalf("reverse should retain later unrelated note: %+v", reverse["rows"])
	}
	call("rollback-apply", item{"preview_id": reverse["preview_id"], "selected_rows": findSelected(reverse), "confirmation": reverseConfirmation}, 200)
	call("rollback-preview", item{"batch_id": batch}, 409)
	read("go-v7-update")
	if name != "旧昵称" || note != "后续无关编辑" {
		t.Fatal("rollback did not restore only original modified fields")
	}
	previewAdd := call("preview", files("add"), 200)
	if selected := findSelected(previewAdd); len(selected) != 1 || selected[0] != "account:2:4" {
		t.Fatalf("new account preview did not isolate candidate: %+v", previewAdd["rows"])
	}
	added := call("apply", item{"preview_id": previewAdd["preview_id"], "selected_rows": findSelected(previewAdd), "confirmation": confirmation}, 200)
	var newID string
	if err := tx.QueryRowContext(ctx, `SELECT id FROM board_candidate.accounts WHERE douyin_id='v7-add-009'`).Scan(&newID); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO board_candidate.daily VALUES($1,'2026-11-10',1)`, newID); err != nil {
		t.Fatal(err)
	}
	dependent := call("rollback-preview", item{"batch_id": added["batch_id"]}, 200)
	if len(findSelected(dependent)) != 0 {
		t.Fatal("rollback marked a new account with publication history safe to delete")
	}
	// Simulate a database trigger with an unintended side effect in the same
	// rollback-only test transaction. The preview must reject the entire bundle.
	for _, statement := range []string{
		`CREATE FUNCTION pg_temp.synthetic_table_sync_guard() RETURNS trigger LANGUAGE plpgsql AS $$
		 BEGIN UPDATE board_candidate.computers SET name='计划外值' WHERE id='office3:901'; RETURN NEW; END; $$`,
		`CREATE TRIGGER synthetic_table_sync_guard AFTER UPDATE ON board_candidate.accounts
		 FOR EACH ROW WHEN (NEW.id='go-v7-update') EXECUTE FUNCTION pg_temp.synthetic_table_sync_guard()`,
	} {
		if _, err := tx.ExecContext(ctx, statement); err != nil {
			t.Fatal(err)
		}
	}
	guarded := call("preview", files("update"), 200)
	if len(findSelected(guarded)) != 0 {
		t.Fatal("preview accepted a trigger side effect outside the plan")
	}
	if err := tx.QueryRowContext(ctx, `SELECT name FROM board_candidate.computers WHERE id='office3:901'`).Scan(&name); err != nil || name != "合成901号电脑" {
		t.Fatal("preview left a protected device changed")
	}
}
