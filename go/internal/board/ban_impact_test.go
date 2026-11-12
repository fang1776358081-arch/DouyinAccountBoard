package board

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"reflect"
	"strconv"
	"testing"

	"douyinboard/internal/session"
)

func TestBanCardAdvice(t *testing.T) {
	east := []string{"east"}
	for _, tc := range []struct {
		name, cardStatus string
		accountStatuses  []string
		offices          []string
		global           bool
		want             string
	}{
		{"exclusive", "正常", []string{"封禁"}, east, true, "cancel_review"},
		{"shared", "正常", []string{"封禁", "正常"}, east, true, "in_use"},
		{"other status unknown", "正常", []string{"封禁", "待确认"}, east, true, "review"},
		{"short ban", "正常", []string{"短期封禁"}, east, true, "review"},
		{"scoped cannot confirm complete use", "正常", []string{"封禁"}, east, false, "review"},
		{"unknown card office", "正常", []string{"封禁"}, nil, true, "review"},
		{"multi-office mismatch", "正常", []string{"封禁"}, []string{"west", "south"}, true, "review"},
		{"card out of service", "停用", []string{"封禁"}, east, true, "review"},
		{"cancelled with normal account", "已注销", []string{"封禁", "正常"}, east, true, "review"},
		{"cancel already in progress", "待注销", []string{"封禁"}, east, true, "cancel_pending"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			accounts := make([]row, 0, len(tc.accountStatuses))
			for index, status := range tc.accountStatuses {
				accounts = append(accounts, row{"id": strconv.Itoa(index + 1), "status": status, "registration_phone": "19900000009", "office_ids": east})
			}
			cards := []row{{"id": 1, "phone": "19900000009", "status": tc.cardStatus, "office_ids": tc.offices}, {"id": 2, "phone": "19900000008", "status": "正常"}}
			result := banCards(tc.global, accounts, cards)
			if len(result) != 1 || result[0]["state"] != tc.want || result[0]["id"] != 1 || result[0]["reason"] == "" {
				t.Fatalf("unexpected advice: %#v", result)
			}
		})
	}
}

func TestBanImpactSyntheticPostgreSQL(t *testing.T) {
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
	defer db.Close()
	ctx := context.Background()
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	var marker string
	if err := tx.QueryRowContext(ctx, "SELECT kind FROM board_app.environment").Scan(&marker); err != nil || marker != "synthetic-only" {
		t.Fatalf("expected disposable synthetic fixture: %v", err)
	}
	_, err = tx.ExecContext(ctx, `
		INSERT INTO phone_devices (id,name) VALUES ('9811','Synthetic current device'),('9812','Synthetic other office device');
		INSERT INTO phone_device_offices (device_id,office_id) VALUES ('9811','go-east'),('9812','go-west');
		INSERT INTO phone_units (id,device_id,original_device_id,generation,active,created_at) VALUES
			('ban-old-east','9811','9811',1,0,0),('ban-new-east','9811','9811',2,1,0),('ban-west','9812','9812',1,1,0);
		INSERT INTO phone_cards (id,phone,status) VALUES (9811,'19900009811','正常'),(9812,'19900009812','正常');
		INSERT INTO phone_card_offices (card_id,office_id) VALUES (9811,'go-east'),(9812,'go-west');
		UPDATE accounts SET status='封禁',registration_phone='19900009811',phone_device_id='9811' WHERE id='go-east-account';
		UPDATE accounts SET status='短期封禁',registration_phone='19900009812',phone_device_id='9812' WHERE id='go-east-missing';
		UPDATE accounts SET registration_phone='19900009811',phone_device_id='9811' WHERE id='go-west-account';
		INSERT INTO phone_account_access(account_id,last_unit_id,target_unit_id) VALUES
			('go-east-account','ban-old-east','ban-new-east'),('go-east-missing','ban-west',NULL),('go-west-account','ban-old-east',NULL);
	`)
	if err != nil {
		t.Fatal(err)
	}
	read := func(user session.User) banImpactResult {
		t.Helper()
		scope, err := loadScope(ctx, tx, user)
		if err != nil {
			t.Fatal(err)
		}
		result, err := readBanImpact(ctx, tx, scope)
		if err != nil {
			t.Fatal(err)
		}
		return result
	}
	owner := session.User{ID: 9101, Role: "owner"}
	employee := session.User{ID: 9102, Role: "employee", OfficeIDs: []string{"go-east"}}
	global := read(owner)
	if len(global.Slots) != 2 || global.Slots[0]["computer_id"] != "go-east-pc" || number(global.Slots[0]["slot"]) != 1 {
		t.Fatalf("legacy matrix positions missing: %#v", global.Slots)
	}
	globalCards, globalPhones := byID(global.Cards), byID(global.Phones)
	phoneOthers, _ := globalPhones["ban-old-east"]["other_accounts"].([]row)
	if globalCards["9811"]["state"] != "in_use" || globalPhones["ban-old-east"]["device_id"] != "9811" || globalPhones["ban-new-east"] != nil ||
		len(phoneOthers) != 1 || phoneOthers[0]["id"] != "go-west-account" {
		t.Fatalf("sharing and physical login links not respected: %#v", global)
	}
	local := read(employee)
	if len(local.Cards) != 1 || local.Cards[0]["state"] != "review" || len(local.Cards[0]["other_accounts"].([]row)) != 0 ||
		len(local.Phones) != 1 || local.Phones[0]["id"] != "ban-old-east" || len(local.Phones[0]["other_accounts"].([]row)) != 0 {
		t.Fatalf("employee report exceeded its scope or assumed exclusivity: %#v", local)
	}
	_, err = tx.ExecContext(ctx, `
		INSERT INTO publishing_layout_state(id,batch) VALUES (1,'synthetic-ban-test');
		INSERT INTO publishing_layout(batch,computer_id,slot,account_id,label,office,source_cell,source_url,reason) VALUES
			('synthetic-ban-test','go-east-pc',4,'go-east-account','Old label','go-east','B4','',''),
			('synthetic-ban-test','go-west-pc',6,'go-east-account','Old label','go-west','C6','','');
	`)
	if err != nil {
		t.Fatal(err)
	}
	local = read(employee)
	if len(local.Slots) != 1 || number(local.Slots[0]["slot"]) != 4 || local.Slots[0]["source_cell"] != "B4" {
		t.Fatalf("imported layout or scoped computer was ignored: %#v", local.Slots)
	}
	missing := byID(local.Accounts)["go-east-missing"]["missing"].([]row)
	kinds := []string{}
	for _, item := range missing {
		kinds = append(kinds, str(item["kind"]))
	}
	if !reflect.DeepEqual(kinds, []string{"card", "phone", "slot"}) {
		t.Fatalf("unavailable relations were guessed or omitted: %#v", missing)
	}
}
