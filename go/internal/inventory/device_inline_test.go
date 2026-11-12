package inventory

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/http"
	"os"
	"reflect"
	"strconv"
	"testing"
	"time"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/session"
)

func TestDeviceInlineSyntheticPostgres(t *testing.T) {
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
	owner := session.User{ID: 9101, Name: "Synthetic Owner", Role: "owner", Permissions: []string{"device_create", "device_manage"}}
	created, err := SaveDevice(ctx, tx, owner, map[string]any{
		"number": "800091", "office_ids": []string{"go-east"}, "name": "合成旧名称", "state": "故障",
		"owner": "原负责人", "location": "原位置", "note": "完整备注应保留",
	}, true)
	if err != nil {
		t.Fatal(err)
	}
	id := created["id"].(string)
	beforeDevice, err := DeviceRecord(ctx, tx, id)
	if err != nil {
		t.Fatal(err)
	}
	beforeUnit, err := activeUnit(ctx, tx, id)
	if err != nil {
		t.Fatal(err)
	}
	manager := session.User{ID: 9102, Name: "Synthetic Employee", Role: "employee", Permissions: []string{"device_manage"}, OfficeIDs: []string{"go-east"}}
	versions := created["_record_versions"].(map[string]any)
	payload := map[string]any{"id": id, "changes": map[string]any{"name": "合成改名"}, "_expected_versions": versions}
	status := func(err error, want int) {
		t.Helper()
		var failure accountwrite.APIError
		if !errors.As(err, &failure) || failure.Status != want {
			t.Fatalf("expected %d, got %v", want, err)
		}
	}
	viewer := manager
	viewer.Permissions = nil
	status(AuthorizeDevice(ctx, tx, viewer, payload, false), http.StatusForbidden)
	west := manager
	west.OfficeIDs = []string{"go-west"}
	status(AuthorizeDevice(ctx, tx, west, payload, false), http.StatusForbidden)
	for _, changes := range []map[string]any{{"state": "可用闲置"}, {"name": ""}, {"owner": "a", "location": "b"}} {
		bad := map[string]any{"id": id, "changes": changes, "_expected_versions": versions}
		status(AuthorizeDevice(ctx, tx, manager, bad, false), http.StatusBadRequest)
	}
	withScope := map[string]any{"id": id, "changes": map[string]any{"name": "误改"}, "office_ids": []string{"go-west"}}
	status(AuthorizeDevice(ctx, tx, manager, withScope, false), http.StatusBadRequest)
	result, err := SaveDevice(ctx, tx, manager, payload, false)
	if err != nil {
		t.Fatal(err)
	}
	_, err = SaveDevice(ctx, tx, manager, payload, false)
	status(err, http.StatusConflict)
	for _, field := range []string{"owner", "location"} {
		result, err = SaveDevice(ctx, tx, manager, map[string]any{
			"id": id, "changes": map[string]any{field: "新" + field}, "_expected_versions": result["_record_versions"],
		}, false)
		if err != nil {
			t.Fatal(err)
		}
	}
	afterDevice, err := DeviceRecord(ctx, tx, id)
	if err != nil {
		t.Fatal(err)
	}
	for _, field := range []string{"name", "owner", "location"} {
		beforeDevice[field] = afterDevice[field]
	}
	if !reflect.DeepEqual(beforeDevice, afterDevice) || afterDevice["name"] != "合成改名" {
		t.Fatal("metadata edit changed unrelated device fields")
	}
	afterUnit, err := activeUnit(ctx, tx, id)
	if err != nil || !reflect.DeepEqual(beforeUnit, afterUnit) {
		t.Fatalf("metadata edit changed the physical unit: %v", err)
	}
	other, err := SaveDevice(ctx, tx, owner, map[string]any{
		"number": "800091", "office_ids": []string{"go-west"}, "name": "另一办同号",
	}, true)
	if err != nil || other["id"] == id {
		t.Fatalf("repeated serial across offices was not preserved: %v", err)
	}
	otherDevice, err := DeviceRecord(ctx, tx, other["id"].(string))
	if err != nil || otherDevice["name"] != "另一办同号" {
		t.Fatal("the other office record changed")
	}
}
