package backuprestore

import (
	"bytes"
	"compress/gzip"
	"context"
	"crypto/pbkdf2"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/recordwire"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/stdlib"
)

func TestArchiveRejectsInvalidIDsAndIncompleteFrames(t *testing.T) {
	name := "manual-20261110T200000-12345678123456781234567812345678.boardpg.gz"
	for _, id := range []string{"../" + name, "automatic/../" + name, "C:\\backup\\" + name, "before-v6.dump", "manual.db", "automatic/" + name} {
		if validArchiveID(id) {
			t.Fatalf("unsafe backup ID accepted: %q", id)
		}
	}
	if !validArchiveID(name) || !validArchiveID("automatic/"+strings.Replace(name, "manual-", "auto-", 1)) {
		t.Fatal("valid backup ID rejected")
	}
	definition := catalog{version: 7, hash: "test-schema", tables: []table{{tableSpec: tableSpec{"computers", "id", 1}, columns: []string{"id", "name"}}}}
	header := `{"format":"douyin-board-postgresql-business","version":1,"schema_version":7,"schema_hash":"test-schema"}` + "\n"
	valid := "T computers\nR {\"id\":\"1\",\"name\":\"fixture\"}\nE 1\nZ\n"
	encode := func(body string) []byte {
		t.Helper()
		var compressed bytes.Buffer
		writer := gzip.NewWriter(&compressed)
		if _, err := io.WriteString(writer, header+body); err != nil {
			t.Fatal(err)
		}
		if err := writer.Close(); err != nil {
			t.Fatal(err)
		}
		return compressed.Bytes()
	}
	if _, err := decodeArchive(t.Context(), bytes.NewReader(encode(valid)), definition, nil); err != nil {
		t.Fatal(err)
	}
	for _, body := range []string{strings.Replace(valid, "E 1", "E 2", 1), strings.Replace(valid, `,"name":"fixture"`, "", 1), strings.TrimSuffix(valid, "Z\n"), valid + "T auth_users\n"} {
		if _, err := decodeArchive(t.Context(), bytes.NewReader(encode(body)), definition, nil); err == nil {
			t.Fatal("invalid archive accepted")
		}
	}
	data := encode(valid)
	if _, err := decodeArchive(t.Context(), bytes.NewReader(data[:len(data)-1]), definition, nil); err == nil {
		t.Fatal("truncated gzip checksum accepted")
	}
}

func TestPostgresBackupRestore(t *testing.T) {
	db := backupTestDB(t)
	ctx := t.Context()
	var generation string
	var epoch int64
	if err := db.QueryRowContext(ctx, `SELECT writer_epoch,restore_generation FROM runtime_control WHERE mode='open'`).Scan(&epoch, &generation); err != nil {
		t.Fatal(err)
	}
	writer := accountwrite.Service{DB: db, WriterEpoch: epoch, RestoreGeneration: generation}
	root := t.TempDir()
	service, err := New(Config{Write: writer, Directory: root})
	if err != nil {
		t.Fatal(err)
	}
	defer service.Close()
	suffix, err := accountwrite.NewGroup()
	if err != nil {
		t.Fatal(err)
	}
	const ownerPassword = "synthetic-backup-passphrase"
	const ownerID int64 = 9101
	const accountID = "go-east-account"
	const workerMarker = "synthetic-never-export-worker-token"
	key, err := pbkdf2.Key(sha256.New, ownerPassword, []byte("synthetic-backup-salt"), 600000, sha256.Size)
	if err != nil {
		t.Fatal(err)
	}
	encoded := "pbkdf2_sha256$600000$synthetic-backup-salt$" + hex.EncodeToString(key)
	mustExec := func(query string, args ...any) {
		t.Helper()
		if _, err := db.ExecContext(ctx, query, args...); err != nil {
			t.Fatal(err)
		}
	}
	mustExec(`UPDATE auth_users SET password_hash=$1 WHERE id=$2`, encoded, ownerID)
	mustExec(`UPDATE auth_sessions SET expires_at=extract(epoch FROM now())::bigint+3600 WHERE user_id IN (9101,9103)`)
	mustExec(`UPDATE accounts SET name='备份中的合成昵称' WHERE id=$1`, accountID)
	mustExec(`INSERT INTO nurture_images(id,account_id,cycle,day,mime,content,actor) VALUES($1,$2,1,'2026-11-09','image/png',$3,'Synthetic')`, suffix, accountID, []byte{0, 7, 10, 128, 255})
	mustExec(`INSERT INTO collection_jobs(account_id,homepage,token,state) VALUES($1,'https://example.test/', $2,'queued') ON CONFLICT(account_id) DO UPDATE SET token=EXCLUDED.token`, accountID, workerMarker)
	if err := os.WriteFile(filepath.Join(root, "before-v6.dump"), []byte("legacy custom archive"), 0o600); err != nil {
		t.Fatal(err)
	}
	call := func(h http.HandlerFunc, method, path, role string, body map[string]any, status int) map[string]any {
		t.Helper()
		return backupCall(t, h, method, path, role, body, status)
	}
	call(service.List, http.MethodGet, ListPath, "", nil, http.StatusUnauthorized)
	call(service.List, http.MethodGet, ListPath, "viewer", nil, http.StatusForbidden)
	call(service.Manual(), http.MethodPost, ManualPath, "viewer", map[string]any{}, http.StatusForbidden)
	validArchive := call(service.Manual(), http.MethodPost, ManualPath, "owner", map[string]any{}, http.StatusOK)["name"].(string)
	listing := call(service.List, http.MethodGet, ListPath, "owner", nil, http.StatusOK)
	if rows := listing["backups"].([]any); len(rows) != 1 || rows[0].(map[string]any)["id"] != validArchive {
		t.Fatalf("unexpected native backup list: %v", listing)
	}
	file, err := service.openArchive(validArchive)
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := gzip.NewReader(file)
	if err != nil {
		t.Fatal(err)
	}
	plain, err := io.ReadAll(decoded)
	_ = decoded.Close()
	_ = file.Close()
	if err != nil || bytes.Contains(plain, []byte(encoded)) || bytes.Contains(plain, []byte(workerMarker)) || bytes.Contains(plain, []byte("T auth_users\n")) {
		t.Fatalf("backup archive contains a protected field or could not decode: %v", err)
	}
	preview := func(id string) map[string]any {
		return call(service.Preview(), http.MethodPost, PreviewPath, "owner", map[string]any{"backup_id": id}, http.StatusOK)
	}
	apply := func(preview map[string]any, password string, status int) map[string]any {
		return call(service.Apply(), http.MethodPost, ApplyPath, "owner", map[string]any{"preview_id": preview["preview_id"], "confirmation": "恢复业务数据", "password": password}, status)
	}
	plan := preview(validArchive)
	apply(plan, "wrong-synthetic-password", http.StatusBadRequest)
	mustExec(`UPDATE accounts SET name='预览后的合成修改' WHERE id=$1`, accountID)
	apply(plan, ownerPassword, http.StatusConflict)
	plan = preview(validArchive)
	archivePath := filepath.Join(root, validArchive)
	original, err := os.ReadFile(archivePath)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(archivePath, append(bytes.Clone(original), 1, 2, 3), 0o600); err != nil {
		t.Fatal(err)
	}
	apply(plan, ownerPassword, http.StatusConflict)
	if err := os.WriteFile(archivePath, original, 0o600); err != nil {
		t.Fatal(err)
	}

	missingOffice := "backup-restore-fixture-" + suffix
	mustExec(`INSERT INTO offices(id,name) VALUES($1,$1)`, missingOffice)
	mustExec(`UPDATE account_offices SET office_id=$1 WHERE account_id=$2`, missingOffice, accountID)
	brokenFK := call(service.Manual(), http.MethodPost, ManualPath, "owner", map[string]any{}, http.StatusOK)["name"].(string)
	mustExec(`UPDATE account_offices SET office_id='go-east' WHERE account_id=$1`, accountID)
	mustExec(`DELETE FROM offices WHERE id=$1`, missingOffice)
	apply(preview(brokenFK), ownerPassword, http.StatusBadRequest)
	var current, afterGeneration string
	if err := db.QueryRowContext(ctx, `SELECT a.name,c.restore_generation FROM accounts a CROSS JOIN runtime_control c WHERE a.id=$1`, accountID).Scan(&current, &afterGeneration); err != nil || current != "预览后的合成修改" || afterGeneration != generation {
		t.Fatalf("failed restore changed the live fixture or generation: %q, %q, err=%v", current, afterGeneration, err)
	}

	mustExec(`UPDATE nurture_images SET content=$1 WHERE id=$2`, []byte{19}, suffix)
	mustExec(`INSERT INTO user_office_grants(user_id,office_id) VALUES(9103,'go-west') ON CONFLICT DO NOTHING`)
	mustExec(`INSERT INTO operations_history(group_id,at,actor_id,actor_name,action,entity) VALUES($1,0,9101,'Synthetic','fixture','account:synthetic-history')`, suffix)
	mustExec(`INSERT INTO idempotency_records(restore_generation,user_id,request_id,action,request_sha256,state,created_at)
VALUES($1,9101,$2,'/api/daily','synthetic-restore-retained','pending',0)`, generation, suffix)
	result := apply(preview(validArchive), ownerPassword, http.StatusOK)
	if result["restart_required"] != true || !strings.HasPrefix(result["backup_before_restore"].(string), "before-restore-") {
		t.Fatalf("restore receipt = %v", result)
	}
	var storedPassword string
	var image []byte
	var queue, history, receipt, granted, paused, autoEnabled int
	err = db.QueryRowContext(ctx, `SELECT a.name,c.restore_generation,u.password_hash,i.content,
(SELECT count(*) FROM collection_jobs),(SELECT count(*) FROM operations_history WHERE group_id=$3),
(SELECT count(*) FROM idempotency_records WHERE request_id=$3),(SELECT count(*) FROM user_office_grants WHERE user_id=9103 AND office_id='go-west'),
(SELECT paused FROM collection_settings WHERE id=1),(SELECT auto_enabled FROM collection_settings WHERE id=1)
FROM accounts a CROSS JOIN runtime_control c CROSS JOIN auth_users u JOIN nurture_images i ON i.id=$3 WHERE a.id=$1 AND u.id=$2`, accountID, ownerID, suffix).
		Scan(&current, &afterGeneration, &storedPassword, &image, &queue, &history, &receipt, &granted, &paused, &autoEnabled)
	if err != nil || current != "备份中的合成昵称" || generation == afterGeneration || storedPassword != encoded || !bytes.Equal(image, []byte{0, 7, 10, 128, 255}) || queue != 0 || history != 1 || receipt != 1 || granted != 1 || paused != 1 || autoEnabled != 0 {
		t.Fatalf("restored business/retained system rows do not match expectations, err=%v", err)
	}
	old := call(service.Preview(), http.MethodPost, PreviewPath, "owner", map[string]any{"backup_id": validArchive}, http.StatusConflict)
	if old["code"] != "RESTORE_GENERATION_EXPIRED" {
		t.Fatalf("old writer was not frozen after restore: %v", old)
	}
	writer.RestoreGeneration = afterGeneration
	restarted, err := New(Config{Write: writer, Directory: root})
	if err != nil {
		t.Fatal(err)
	}
	defer restarted.Close()
	call(restarted.Preview(), http.MethodPost, PreviewPath, "owner", map[string]any{"backup_id": result["backup_before_restore"]}, http.StatusOK)
	if _, err := restarted.automatic(ctx, time.Now()); err != nil {
		t.Fatal(err)
	}
	if _, err := restarted.automatic(ctx, time.Now()); err != nil {
		t.Fatal(err)
	}
	files, err := restarted.list("automatic")
	if err != nil || len(files) != 1 {
		t.Fatalf("automatic retry made more than one archive: %d, err=%v", len(files), err)
	}
	expired := time.Now().Add(-31 * 24 * time.Hour)
	if err := os.Chtimes(filepath.Join(root, filepath.FromSlash(files[0].ID)), expired, expired); err != nil {
		t.Fatal(err)
	}
	if _, err := restarted.automatic(ctx, time.Now()); err != nil {
		t.Fatal(err)
	}
	retained, err := restarted.list("automatic")
	if err != nil || len(retained) != 1 || retained[0].ID == files[0].ID {
		t.Fatalf("expired automatic snapshot was not pruned after success: %v", err)
	}
	if _, err := os.Stat(filepath.Join(root, validArchive)); err != nil {
		t.Fatalf("manual snapshot was removed by automatic retention: %v", err)
	}
}

func backupCall(t *testing.T, handler http.HandlerFunc, method, path, role string, body map[string]any, status int) map[string]any {
	t.Helper()
	var raw []byte
	if body != nil {
		var err error
		raw, err = recordwire.Encode(body)
		if err != nil {
			t.Fatal(err)
		}
	}
	req := httptest.NewRequest(method, path, bytes.NewReader(raw))
	req.Header.Set("Content-Type", "application/json")
	if role != "" {
		req.AddCookie(&http.Cookie{Name: "board_session", Value: "synthetic-go-daily-" + role + "-cookie"})
		req.Header.Set("X-CSRF-Token", "synthetic-go-daily-"+role+"-csrf")
	}
	result := httptest.NewRecorder()
	handler(result, req)
	if result.Code != status {
		t.Fatalf("%s %s = %d, want %d. Body: %s", method, path, result.Code, status, result.Body)
	}
	var decoded map[string]any
	if err := json.Unmarshal(result.Body.Bytes(), &decoded); err != nil {
		t.Fatal(err)
	}
	return decoded
}

func backupTestDB(t *testing.T) *sql.DB {
	t.Helper()
	port := os.Getenv("BOARD_GO_BACKUP_TEST_PORT")
	if port == "" {
		t.Skip("set BOARD_GO_BACKUP_TEST_PORT to run against the disposable loopback fixture")
	}
	if value, err := strconv.Atoi(port); err != nil || value < 1024 || value > 65535 {
		t.Fatal("test port must be a valid nonprivileged loopback port")
	}
	config, err := pgx.ParseConfig(fmt.Sprintf("host=127.0.0.1 port=%s dbname=synthetic_backup_restore user=synthetic_owner sslmode=disable", port))
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
	if err := db.QueryRowContext(ctx, `SELECT current_database()='synthetic_backup_restore'
AND (SELECT array_agg(kind) FROM board_app.environment)=ARRAY['synthetic-only']::text[]`).Scan(&safe); err != nil || !safe {
		t.Fatalf("requires the restore-only synthetic clone: %v", err)
	}
	return db
}
