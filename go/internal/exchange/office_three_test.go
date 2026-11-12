package exchange

import (
	"context"
	"database/sql"
	"encoding/base64"
	"encoding/json"
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
	"douyinboard/internal/publishing"
	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"

	_ "github.com/jackc/pgx/v5/stdlib"
)

func officeTestFile(t *testing.T, rows ...[]string) map[string]any {
	t.Helper()
	raw, err := writeCSV(rows)
	if err != nil {
		t.Fatal(err)
	}
	return map[string]any{"filename": "synthetic.csv", "content": base64.StdEncoding.EncodeToString(raw)}
}

func TestOfficeThreeAllowlist(t *testing.T) {
	header := []string{"姓名", "远控信息", "注册抖音用的手机号", "验证码", "密码", "抖音名称", "抖音在哪个手机上",
		"抖音号", "小号说明", "门头", "负责人", "对应电脑", "类型", "账号密码", "恢复码", "状态", "备注", "使用", "注册日期"}
	row := []string{"秘密姓名", "私密远控", "19900000004", "密钥", "原始密码", "测试名字", "29",
		"0042", "测试小号", "测试门头", "测试负责人", "1", "主账号", "不进入预览", "不保留", "正常", "", "正常", ""}
	clean, err := readOfficeAccounts(officeTestFile(t, header, row))
	if err != nil || len(clean) != 1 || clean[0].Fields["抖音号"] != "0042" || clean[0].Fields["昵称"] != "测试名字" {
		t.Fatalf("safe source mapping invalid: %+v %v", clean, err)
	}
	payload, _ := json.Marshal(clean)
	for _, private := range []string{"秘密", "私密", "密码", "密钥", "不进入预览", "不保留"} {
		if strings.Contains(string(payload), private) {
			t.Fatal("a disallowed value survived source parsing")
		}
	}
}

func TestOfficeThreeSyntheticPostgres(t *testing.T) {
	rawPort := os.Getenv("BOARD_GO_DAILY_TEST_PORT")
	if rawPort == "" {
		t.Skip("requires disposable PostgreSQL fixture")
	}
	port, err := strconv.ParseUint(rawPort, 10, 16)
	if err != nil || port == 0 {
		t.Fatal("invalid synthetic fixture port")
	}
	database := os.Getenv("BOARD_GO_EXCHANGE_TEST_DB")
	if database == "" {
		database = "synthetic_probe"
	}
	if !strings.HasPrefix(database, "synthetic_") || strings.ContainsFunc(database, func(r rune) bool {
		return r != '_' && (r < 'a' || r > 'z') && (r < '0' || r > '9')
	}) {
		t.Fatal("invalid synthetic fixture database")
	}
	db, err := sql.Open("pgx", fmt.Sprintf("postgres://synthetic_owner@127.0.0.1:%d/%s?sslmode=disable&search_path=board_candidate,pg_catalog", port, database))
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
	request := httptest.NewRequest(http.MethodPost, "/", nil)
	request.AddCookie(&http.Cookie{Name: "board_session", Value: "synthetic-go-daily-owner-cookie"})
	actor, err := session.AuthenticateWith(ctx, tx, request)
	if err != nil {
		t.Fatal(err)
	}
	exec := func(query string, params ...any) {
		t.Helper()
		if _, err := tx.ExecContext(ctx, query, params...); err != nil {
			t.Fatal(err)
		}
	}
	exec(`INSERT INTO offices(id,name,active) VALUES('three','Synthetic Office Three',1) ON CONFLICT(id) DO UPDATE SET active=1`)
	exec(`INSERT INTO computers(id,name,operator) VALUES('office3:818001','保留原电脑名称','原负责人')`)
	exec(`INSERT INTO publishing_layout_state(id,batch) VALUES(1,'synthetic-office-three-batch') ON CONFLICT(id) DO UPDATE SET batch=excluded.batch`)
	exec(`INSERT INTO publishing_layout(batch,computer_id,slot,account_id,label,office,source_cell,source_url,reason)
		VALUES('synthetic-office-three-batch','go-west-pc',8,'go-west-account','保留原发布格','go-west','','','')`)
	exec(`INSERT INTO publishing_layout_links(batch,computer_id,slot,previous_reason,actor_id,updated_at)
		VALUES('synthetic-office-three-batch','go-west-pc',8,'原手工关联',9101,0)`)

	const hidden = "synthetic_secret_disallowed"
	accountHeader := append(append([]string{}, officeAccountHeaders...), "密码")
	accountRow := []string{"19", "19900000004", "合成昵称", "818001号手机", "000818001", "", "", "负责人",
		"818001", "", "养号7天", "", "", "2025年1月2日", hidden}
	foreignAccountRow := []string{"20", "", "其他办公室原账号", "818001号手机", "0099", "", "", "", "818001"}
	progress := []string{"25", "818001", "新来源负责人", "", "类别", "抖音号：000818001"}
	body := map[string]any{"office_id": "three", "accounts_file": officeTestFile(t, accountHeader, accountRow, foreignAccountRow),
		"progress_file": officeTestFile(t, officeProgressHeaders, progress)}
	preview := func() map[string]any {
		t.Helper()
		result, err := OfficeThreePreview(ctx, tx, actor, body)
		if err != nil {
			t.Fatal(err)
		}
		return result
	}
	checkStatus := func(err error, status int) {
		t.Helper()
		var apiErr accountwrite.APIError
		if !errors.As(err, &apiErr) || apiErr.Status != status {
			t.Fatalf("expected HTTP %d, got %v", status, err)
		}
	}
	initial := preview()
	if candidates := initial["scope_candidates"].([]officeScopeCandidate); len(candidates) != 1 || candidates[0].ID != "office3:818001" ||
		initial["summary"].(map[string]int)["new_accounts"] != 0 {
		t.Fatalf("preassignment preview should block account and offer PC: %+v", initial)
	}
	scopeBody := map[string]any{"office_id": "three", "preview_id": initial["preview_id"], "confirmation": officeScopeConfirmation,
		"computer_ids": []string{"go-west-pc"}}
	_, err = OfficeThreeAssignComputers(ctx, tx, actor, scopeBody)
	checkStatus(err, http.StatusBadRequest)
	scopeBody["computer_ids"] = []string{"office3:818001"}
	scopeResult, err := OfficeThreeAssignComputers(ctx, tx, actor, scopeBody)
	if err != nil || scopeResult["requires_new_preview"] != true {
		t.Fatalf("PC scope assignment failed: %+v %v", scopeResult, err)
	}

	next := preview()
	summary := next["summary"].(map[string]int)
	if summary["new_accounts"] != 1 || summary["new_devices"] != 1 || summary["new_layouts"] != 1 || summary["conflicts"] != 1 {
		t.Fatalf("unexpected import plan: %+v", summary)
	}
	confirm := map[string]any{"office_id": "three", "preview_id": next["preview_id"], "confirmation": officeDualConfirmation}
	var savedPreview string
	if err := tx.QueryRowContext(ctx, `SELECT payload FROM operations_previews WHERE id=$1`, next["preview_id"]).Scan(&savedPreview); err != nil ||
		strings.Contains(savedPreview, hidden) {
		t.Fatalf("preview did not safely persist allowlisted fields: %v", err)
	}
	var originalOperator sql.NullString
	if err := tx.QueryRowContext(ctx, `SELECT operator FROM computers WHERE id='go-west-pc'`).Scan(&originalOperator); err != nil {
		t.Fatal(err)
	}
	exec(`UPDATE computers SET operator='并发变化' WHERE id='go-west-pc'`)
	_, err = OfficeThreeApply(ctx, tx, actor, confirm)
	checkStatus(err, http.StatusConflict)
	exec(`UPDATE computers SET operator=$1 WHERE id='go-west-pc'`, originalOperator)

	// Simulate an unexpected side effect during the write. Preservation checks
	// must roll back both the new records and the existing row modification.
	exec(`CREATE FUNCTION board_candidate.synthetic_office_three_side_effect() RETURNS trigger AS $$
		BEGIN UPDATE computers SET operator='意外改动' WHERE id='go-west-pc'; RETURN NEW; END;
		$$ LANGUAGE plpgsql`)
	exec(`CREATE TRIGGER synthetic_office_three_guard AFTER INSERT ON accounts FOR EACH ROW
		WHEN (NEW.douyin_id='000818001') EXECUTE FUNCTION board_candidate.synthetic_office_three_side_effect()`)
	_, err = OfficeThreeApply(ctx, tx, actor, confirm)
	checkStatus(err, http.StatusConflict)
	if !strings.Contains(err.Error(), "保留校验失败") {
		t.Fatalf("unexpected side effect did not reach preservation guard: %v", err)
	}
	exec(`DROP TRIGGER synthetic_office_three_guard ON accounts`)
	result, err := OfficeThreeApply(ctx, tx, actor, confirm)
	if err != nil || result["imported"] != 3 {
		t.Fatalf("final import failed: %+v %v", result, err)
	}
	_, err = OfficeThreeApply(ctx, tx, actor, confirm)
	checkStatus(err, http.StatusBadRequest)
	var name, computerName, operator, date, layout, previous, nurtureStart string
	var lastUnit sql.NullString
	err = tx.QueryRowContext(ctx, `SELECT a.name,c.name,c.operator,a.registered_on,p.label,old.previous_reason,access.last_unit_id,n.legacy_started_on
		FROM accounts a JOIN computers c ON c.id=a.computer_id JOIN publishing_layout p ON p.account_id=a.id
		JOIN phone_account_access access ON access.account_id=a.id JOIN publishing_layout_links old ON old.batch=p.batch AND old.computer_id='go-west-pc'
		JOIN nurture_records n ON n.account_id=a.id
		WHERE a.douyin_id='000818001'`).Scan(&name, &computerName, &operator, &date, &layout, &previous, &lastUnit, &nurtureStart)
	if err != nil || name != "合成昵称" || computerName != "保留原电脑名称" || operator != "原负责人" || date != "2025-01-02" ||
		layout != "抖音号：000818001" || previous != "原手工关联" || lastUnit.Valid || nurtureStart != date {
		t.Fatalf("business data not preserved or login inferred: %q %q %q %q %q %q %+v %q %v", name, computerName, operator, date, layout, previous, lastUnit, nurtureStart, err)
	}

	exec(`INSERT INTO accounts(id,name,douyin_id) VALUES
		('exact-local','测试完整抖音号','000042'),('wrong-leading-zeros','缺少前导零','42'),
		('source-repeat','测试重复位置','same-progress'),('duplicate-local-1','测试重号甲','same-account'),
		('duplicate-local-2','测试重号乙','same-account'),('unassigned-local','待分配资料','unknown-office'),
		('placed-local','已有位置','occupied-handle')`)
	exec(`INSERT INTO account_offices(account_id,office_id)
		SELECT id,'three' FROM accounts WHERE id IN ('exact-local','wrong-leading-zeros','source-repeat',
			'duplicate-local-1','duplicate-local-2','placed-local')`)
	exec(`INSERT INTO publishing_layout(batch,computer_id,slot,label,office,source_cell,source_url,reason)
		VALUES('synthetic-office-three-batch','office3:818001',10,'原有待核对','three','old','','原原因')`)
	exec(`INSERT INTO publishing_layout(batch,computer_id,slot,account_id,label,office,source_cell,source_url,reason)
		VALUES('synthetic-office-three-batch','office3:818001',11,'placed-local','原已关联','three','old-placed','','')`)
	progress = []string{"25", "818001", "新来源负责人", "", "类别", "抖音号：000818001", "818001-合成昵称",
		"抖音号：000042", "抖音号：same-progress", "douyin_id:same-progress", "抖音号：same-account", "抖音号：0099",
		"抖音号：unknown-office", "抖音号：occupied-handle", "抖音号：42"}
	body["progress_file"] = officeTestFile(t, officeProgressHeaders, progress)
	next = preview()
	summary = next["summary"].(map[string]int)
	if summary["new_accounts"] != 0 || summary["new_layouts"] != 8 || summary["pending_layouts"] != 7 || summary["conflicts"] != 2 {
		t.Fatalf("unexpected safe matching summary: %+v", summary)
	}
	confirm["preview_id"] = next["preview_id"]
	oldID, err := storePreview(ctx, tx, actor.ID, officeDualKind, officePlan{Office: officeThreeID})
	if err != nil {
		t.Fatal(err)
	}
	_, err = OfficeThreeApply(ctx, tx, actor, map[string]any{"office_id": "three", "preview_id": oldID, "confirmation": officeDualConfirmation})
	checkStatus(err, http.StatusConflict)
	result, err = OfficeThreeApply(ctx, tx, actor, confirm)
	if err != nil || result["imported"] != 8 {
		t.Fatalf("safe matching import failed: %+v %v", result, err)
	}
	for slot := 2; slot <= 10; slot++ {
		var account sql.NullString
		var label, reason string
		err := tx.QueryRowContext(ctx, `SELECT account_id,label,reason FROM publishing_layout
			WHERE batch='synthetic-office-three-batch' AND computer_id='office3:818001' AND slot=$1`, slot).
			Scan(&account, &label, &reason)
		if err != nil {
			t.Fatal(err)
		}
		switch slot {
		case 3:
			if !account.Valid || account.String != "exact-local" || label != "抖音号：000042" || reason != "" {
				t.Fatalf("exact local match lost leading zeroes or was guessed: %+v %q %q", account, label, reason)
			}
		case 10:
			if account.Valid || label != "原有待核对" || reason != "原原因" {
				t.Fatal("an existing publishing cell was overwritten")
			}
		default:
			if account.Valid || label != progress[slot+4] || reason == "" {
				t.Fatalf("slot %d should preserve a pending cell: %+v %q %q", slot, account, label, reason)
			}
			if slot == 9 && !strings.Contains(reason, "已有发布位置") {
				t.Fatalf("slot %d did not report the existing account position: %s", slot, reason)
			}
		}
	}
	if summary := preview()["summary"].(map[string]int); summary["new_layouts"] != 0 || summary["new_accounts"] != 0 {
		t.Fatalf("reimport planned duplicate rows: %+v", summary)
	}
	link := map[string]any{"batch": "synthetic-office-three-batch", "computer_id": "office3:818001", "slot": json.Number("2"),
		"expected_account_id": nil, "account_id": "wrong-leading-zeros"}
	checkStatus(publishing.Authorize(ctx, tx, session.User{Role: "viewer", OfficeIDs: []string{"three"}}, link), http.StatusForbidden)
	if err := publishing.Authorize(ctx, tx, actor, link); err != nil {
		t.Fatal(err)
	}
	if linked, err := publishing.Link(ctx, tx, actor, link); err != nil || linked["account_id"] != "wrong-leading-zeros" {
		t.Fatalf("pending cell was not available for manual confirmation: %+v %v", linked, err)
	}

	exec(`INSERT INTO computers(id,name) VALUES('office3:929292','其他办公室合成电脑')`)
	exec(`INSERT INTO computer_offices(computer_id,office_id) VALUES('office3:929292','go-west')`)
	exec(`INSERT INTO publishing_layout(batch,computer_id,slot,label,office,source_cell,source_url,reason) VALUES
		('synthetic-office-three-batch','office3:929292',12,'','go-west','foreign-blank','',''),
		('synthetic-office-three-batch','office3:818001',12,'','three','local-blank','','')`)
	version := func(computer string, slot int, office string) string {
		t.Helper()
		var raw string
		if err := tx.QueryRowContext(ctx, `SELECT to_jsonb(p)::text FROM publishing_layout p
			WHERE batch='synthetic-office-three-batch' AND computer_id=$1 AND slot=$2`, computer, slot).Scan(&raw); err != nil {
			t.Fatal(err)
		}
		row, err := decodeRecord(raw)
		if err != nil {
			t.Fatal(err)
		}
		row["office_ids"] = []string{office}
		token, err := recordwire.Version(row)
		if err != nil {
			t.Fatal(err)
		}
		return token
	}
	move := map[string]any{"batch": "synthetic-office-three-batch", "computer_id": "office3:818001", "slot": json.Number("3"),
		"target_computer": "office3:929292", "target_slot": json.Number("12"), "action": "move"}
	move["_expected_versions"] = map[string]any{
		"layout:synthetic-office-three-batch:office3:818001:3":  version("office3:818001", 3, "three"),
		"layout:synthetic-office-three-batch:office3:929292:12": version("office3:929292", 12, "go-west"),
		"layout:synthetic-office-three-batch:office3:818001:12": version("office3:818001", 12, "three"),
	}
	_, err = publishing.Assign(ctx, tx, actor, move)
	checkStatus(err, http.StatusBadRequest)
	move["target_computer"] = "office3:818001"
	if moved, err := publishing.Assign(ctx, tx, actor, move); err != nil || moved["ok"] != true {
		t.Fatalf("same-office move with original versions should still work: %+v %v", moved, err)
	}
}
