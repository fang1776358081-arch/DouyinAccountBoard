package board

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"douyinboard/internal/session"
	_ "github.com/jackc/pgx/v5/stdlib"
)

func TestParseAccountsQuery(t *testing.T) {
	now := time.Date(2026, 11, 21, 12, 0, 0, 0, china)
	parsed, err := parseAccountsQuery(url.Values{
		"office_id": {"three"}, "page": {"2"}, "query": {"  小王  "},
		"published": {"yes"}, "status": {"正常"},
	}, now)
	if err != nil {
		t.Fatal(err)
	}
	parsed.buildWhere(session.User{OfficeIDs: []string{"three"}})
	if parsed.day != "2026-11-21" || parsed.page != 2 || parsed.query != "小王" || len(parsed.args) != 7 {
		t.Fatalf("unexpected parsed query: %#v", parsed)
	}
	if !strings.Contains(parsed.where, "ao.office_id=$1") || !strings.Contains(parsed.where, "COALESCE(d.published,0)=$7") {
		t.Fatalf("unexpected where clause: %s", parsed.where)
	}
}

func TestParseAccountsQueryRejectsAmbiguousInput(t *testing.T) {
	now := time.Now()
	tests := []url.Values{
		{"office_id": {"other"}},
		{"office_id": {"three"}, "page": {"0"}},
		{"office_id": {"three"}, "published": {"maybe"}},
		{"office_id": {"three"}, "unknown": {"value"}},
		{"office_id": {"three", "three"}},
		{"office_id": {"three"}, "date": {"2026-02-30"}},
		{"office_id": {"three"}, "query": {strings.Repeat("超", 201)}},
	}
	for _, values := range tests {
		if _, err := parseAccountsQuery(values, now); err == nil {
			t.Fatalf("expected rejection for %#v", values)
		}
	}
}

func TestAccountsSyntheticPostgreSQL(t *testing.T) {
	setting := os.Getenv("BOARD_GO_DAILY_TEST_PORT")
	if setting == "" {
		t.Skip("requires the disposable loopback fixture")
	}
	port, err := strconv.ParseUint(setting, 10, 16)
	if err != nil || port == 0 {
		t.Fatal("invalid synthetic PostgreSQL port")
	}
	db, err := sql.Open("pgx", fmt.Sprintf("postgres://synthetic_owner@127.0.0.1:%d/synthetic_probe?sslmode=disable&search_path=board_candidate,pg_catalog", port))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	var marker string
	if err := db.QueryRow("SELECT kind FROM board_app.environment").Scan(&marker); err != nil || marker != "synthetic-only" {
		t.Fatalf("expected disposable synthetic fixture: %v", err)
	}

	const prefix = "go-accounts-page-"
	createdOffice := inserted(t, db, `INSERT INTO offices(id,name,active) VALUES('three','Accounts API Synthetic Three',1) ON CONFLICT DO NOTHING RETURNING 1`)
	createdGrant := inserted(t, db, `INSERT INTO user_office_grants(user_id,office_id) VALUES(9102,'three') ON CONFLICT DO NOTHING RETURNING 1`)
	createdLayout := inserted(t, db, `INSERT INTO publishing_layout_state(id,batch) VALUES(1,'go-accounts-page') ON CONFLICT DO NOTHING RETURNING 1`)
	t.Cleanup(func() {
		if createdLayout {
			_, _ = db.Exec(`DELETE FROM publishing_layout WHERE batch='go-accounts-page'`)
			_, _ = db.Exec(`DELETE FROM publishing_layout_state WHERE batch='go-accounts-page'`)
		}
		_, _ = db.Exec(`DELETE FROM nurture_records WHERE account_id LIKE $1`, prefix+"%")
		_, _ = db.Exec(`DELETE FROM daily WHERE account_id LIKE $1`, prefix+"%")
		_, _ = db.Exec(`DELETE FROM account_offices WHERE account_id LIKE $1`, prefix+"%")
		_, _ = db.Exec(`DELETE FROM accounts WHERE id LIKE $1`, prefix+"%")
		_, _ = db.Exec(`DELETE FROM phone_device_offices WHERE device_id LIKE $1`, prefix+"%")
		_, _ = db.Exec(`DELETE FROM phone_devices WHERE id LIKE $1`, prefix+"%")
		_, _ = db.Exec(`DELETE FROM computer_offices WHERE computer_id LIKE $1`, prefix+"%")
		_, _ = db.Exec(`DELETE FROM computers WHERE id LIKE $1`, prefix+"%")
		if createdGrant {
			_, _ = db.Exec(`DELETE FROM user_office_grants WHERE user_id=9102 AND office_id='three'`)
		}
		if createdOffice {
			_, _ = db.Exec(`DELETE FROM offices WHERE id='three' AND name='Accounts API Synthetic Three'`)
		}
	})

	mustExec(t, db, `INSERT INTO computers(id,name) VALUES($1,'Visible Office3 PC'),($2,'Hidden West PC')`, prefix+"visible-pc", prefix+"hidden-pc")
	mustExec(t, db, `INSERT INTO computer_offices(computer_id,office_id) VALUES($1,'three'),($2,'go-west')`, prefix+"visible-pc", prefix+"hidden-pc")
	mustExec(t, db, `INSERT INTO phone_devices(id,name) VALUES($1,'Visible Office3 Phone'),($2,'Hidden West Phone')`, prefix+"visible-phone", prefix+"hidden-phone")
	mustExec(t, db, `INSERT INTO phone_device_offices(device_id,office_id) VALUES($1,'three'),($2,'go-west')`, prefix+"visible-phone", prefix+"hidden-phone")
	mustExec(t, db, `INSERT INTO accounts(id,computer_id,slot,name,status,douyin_id) VALUES($1,$2,1,'Hidden Parent','正常','parent')`, prefix+"hidden-parent", prefix+"hidden-pc")
	mustExec(t, db, `INSERT INTO account_offices(account_id,office_id) VALUES($1,'go-west')`, prefix+"hidden-parent")
	for index := 0; index < 55; index++ {
		id := fmt.Sprintf("%s%03d", prefix, index)
		computer, phone, parent := prefix+"visible-pc", prefix+"visible-phone", any(nil)
		if index == 54 {
			computer, phone, parent = prefix+"hidden-pc", prefix+"hidden-phone", prefix+"hidden-parent"
		}
		mustExec(t, db, `INSERT INTO accounts(id,computer_id,slot,name,status,douyin_id,registration_phone,phone_device_id,parent_account_id)
			VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)`, id, computer, index+2, fmt.Sprintf("Office3 Account %03d", index), map[bool]string{true: "封禁", false: "正常"}[index == 7], fmt.Sprintf("dy%03d", index), fmt.Sprintf("1380000%04d", index), phone, parent)
		mustExec(t, db, `INSERT INTO account_offices(account_id,office_id) VALUES($1,'three')`, id)
	}
	mustExec(t, db, `INSERT INTO daily(account_id,day,published) VALUES($1,'2026-11-09',1)`, prefix+"007")
	mustExec(t, db, `UPDATE accounts SET name=name||' %_',douyin_id=E'\t '||douyin_id||' ' WHERE id=$1`, prefix+"007")
	mustExec(t, db, `UPDATE accounts SET account_type='大号',auxiliary_state='未注册' WHERE id=$1`, prefix+"002")
	mustExec(t, db, `UPDATE accounts SET account_type='小号',parent_account_id=$1 WHERE id=$2`, prefix+"002", prefix+"003")
	mustExec(t, db, `INSERT INTO nurture_records(account_id,verification_note) VALUES($1,'Current fixture note')`, prefix+"007")
	if createdLayout {
		mustExec(t, db, `INSERT INTO publishing_layout(batch,computer_id,slot,account_id,label,office,source_cell,source_url,reason)
			VALUES('go-accounts-page',$1,1,$2,'Visible placement','three','','',''),('go-accounts-page',$3,1,$2,'West placement','go-west','','','')`,
			prefix+"visible-pc", prefix+"007", prefix+"hidden-pc")
	}

	h := Handler{DB: db}
	invoke := func(path, role string, expected int) row {
		t.Helper()
		r := httptest.NewRequest(http.MethodGet, path, nil)
		r.AddCookie(&http.Cookie{Name: "board_session", Value: "synthetic-go-daily-" + role + "-cookie"})
		w := httptest.NewRecorder()
		if r.URL.Path == "/api/record-versions" {
			h.RecordVersions(w, r)
		} else {
			h.Accounts(w, r)
		}
		if w.Code != expected {
			t.Fatalf("%s for %s returned %d, want %d: %s", path, role, w.Code, expected, w.Body.String())
		}
		var result row
		if err := json.Unmarshal(w.Body.Bytes(), &result); err != nil {
			t.Fatal(err)
		}
		return result
	}
	invoke("/api/accounts?office_id=three", "viewer", http.StatusForbidden)
	page := invoke("/api/accounts?office_id=three&date=2026-11-09&page=2&query=Office3", "employee", http.StatusOK)
	if page["total"] != float64(55) || page["pages"] != float64(2) || page["page"] != float64(2) || len(page["accounts"].([]any)) != 5 || len(page["matching_ids"].([]any)) != 55 {
		t.Fatalf("unexpected paged result: %#v", page)
	}
	if page["workspace_view"] != "accounts_page" || page["user"].(row)["id"] != float64(9102) || page["published_total"] != float64(1) || page["computer_total"] != float64(1) || len(page["phone_devices"].(row)["devices"].([]any)) != 1 || len(page["offices"].([]any)) != 2 {
		t.Fatal("lightweight account bootstrap metadata changed")
	}
	for _, item := range page["accounts"].([]any) {
		id := item.(row)["id"].(string)
		if _, exists := page["record_versions"].(row)["account:"+id]; !exists {
			t.Fatal("account version missing from current page")
		}
		if _, exists := page["record_versions"].(row)["daily:"+id+":2026-11-09"]; !exists {
			t.Fatal("daily version missing from current page")
		}
	}
	published := invoke("/api/accounts?office_id=three&date=2026-11-09&published=yes", "employee", http.StatusOK)
	if published["total"] != float64(1) || published["accounts"].([]any)[0].(row)["id"] != prefix+"007" {
		t.Fatal("published filter changed")
	}
	if createdLayout {
		cells := published["publishing_layout"].(row)["cells"].([]any)
		if len(cells) != 1 || cells[0].(row)["computer_id"] != prefix+"visible-pc" || len(page["publishing_layout"].(row)["cells"].([]any)) != 0 {
			t.Fatal("publishing positions must only reference visible computers and current page accounts")
		}
	}
	for _, filter := range []string{"query=" + url.QueryEscape("%_"), "douyin_id=DY007"} {
		match := invoke("/api/accounts?office_id=three&"+filter, "employee", http.StatusOK)
		if match["total"] != float64(1) || match["matching_ids"].([]any)[0] != prefix+"007" {
			t.Fatal("account search diverged from the legacy field filter")
		}
	}
	if records, ok := page["nurture"].(row)["records"].([]any); !ok || len(records) != 0 {
		t.Fatal("response sent off-page verification records")
	}
	unassigned := invoke("/api/accounts?office_id=three&computer_id=unassigned", "employee", http.StatusOK)
	if unassigned["total"] != float64(1) || unassigned["computer_total"] != float64(0) || unassigned["matching_ids"].([]any)[0] != prefix+"054" {
		t.Fatal("computer filtering must match displayed computer assignment")
	}
	versionKeys := []string{"account:" + prefix + "007", "daily:" + prefix + "007:2026-11-09", "nurture:" + prefix + "007"}
	reference := invoke("/api/record-versions?keys="+url.QueryEscape(strings.Join(versionKeys, ",")), "employee", http.StatusOK)
	for _, key := range versionKeys {
		version := published["record_versions"].(row)[key]
		if version == nil || version != reference["record_versions"].(row)[key] {
			t.Fatalf("page version does not match the record version for %s", key)
		}
	}
	parent := invoke("/api/accounts?office_id=three&account_id="+prefix+"002", "employee", http.StatusOK)["accounts"].([]any)[0].(row)
	if parent["effective_auxiliary_state"] != "已注册" || parent["child_accounts"].([]any)[0].(row)["id"] != prefix+"003" {
		t.Fatal("filtering the parent incorrectly removed its known child")
	}
	child := invoke("/api/accounts?office_id=three&account_id="+prefix+"003", "employee", http.StatusOK)["accounts"].([]any)[0].(row)
	if child["parent_name"] != "Office3 Account 002" {
		t.Fatal("filtering the child incorrectly removed its known parent")
	}
	masked := invoke("/api/accounts?office_id=three&account_id="+url.QueryEscape(prefix+"054"), "employee", http.StatusOK)["accounts"].([]any)[0].(row)
	if masked["computer_id"] != "unassigned" || masked["phone_device_id"] != nil || masked["parent_account_id"] != nil {
		t.Fatalf("cross-office references leaked: %#v", masked)
	}
	owner := invoke("/api/accounts?office_id=three&account_id="+url.QueryEscape(prefix+"054"), "owner", http.StatusOK)["accounts"].([]any)[0].(row)
	if owner["computer_id"] != prefix+"hidden-pc" || owner["phone_device_id"] != prefix+"hidden-phone" || owner["parent_account_id"] != prefix+"hidden-parent" {
		t.Fatal("global view was unexpectedly masked")
	}
}

func inserted(t *testing.T, db *sql.DB, statement string) bool {
	t.Helper()
	var value int
	err := db.QueryRow(statement).Scan(&value)
	if err == nil {
		return true
	}
	if err == sql.ErrNoRows {
		return false
	}
	t.Fatal(err)
	return false
}

func mustExec(t *testing.T, db *sql.DB, statement string, args ...any) {
	t.Helper()
	if _, err := db.Exec(statement, args...); err != nil {
		t.Fatal(err)
	}
}
