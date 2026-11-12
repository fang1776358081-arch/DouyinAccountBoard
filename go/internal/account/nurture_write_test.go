package account

import (
	"bytes"
	"database/sql"
	"encoding/base64"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
	"time"

	"douyinboard/internal/accountwrite"
)

func TestPostgresNurtureLifecycleAndImageScope(t *testing.T) {
	db := accountTestDB(t)
	ctx := t.Context()
	writer := accountwrite.Service{DB: db, WriterEpoch: 17, RestoreGeneration: "go-daily-synthetic-generation"}
	h := WriteHandler{Service: writer}
	group, err := accountwrite.NewGroup()
	if err != nil {
		t.Fatal(err)
	}
	id := "nurture-test-" + group
	if _, err := db.ExecContext(ctx, `INSERT INTO accounts(id,name,status) VALUES($1,'合成养号','正常')`, id); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(ctx, `INSERT INTO account_offices(account_id,office_id) VALUES($1,'go-west')`, id); err != nil {
		t.Fatal(err)
	}
	request := func() map[string]any {
		versions := map[string]any{"account:" + id: readVersion(t, db, id)}
		tx, err := db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
		if err != nil {
			t.Fatal(err)
		}
		defer tx.Rollback()
		row, err := readNurture(ctx, tx, id, []string{"go-west"})
		if err != nil {
			t.Fatal(err)
		}
		if err := addVersion(versions, "nurture:"+id, row); err != nil {
			t.Fatal(err)
		}
		return map[string]any{"id": id, "request_id": accountRequestID(t), "_expected_versions": versions}
	}
	post := func(action string, payload map[string]any, want int) map[string]any {
		return accountCall(t, h.Nurture(action), http.MethodPost, NurturePathPrefix+action, "owner", payload, want)
	}
	post("join", request(), http.StatusOK)
	post("start", request(), http.StatusBadRequest)
	verify := request()
	verify["verification_state"] = "核对已通过"
	post("verification", verify, http.StatusOK)
	start := request()
	start["started"] = boardToday().AddDate(0, 0, -7).Format(time.DateOnly)
	post("start", start, http.StatusOK)
	post("purge", request(), http.StatusBadRequest)
	image := []byte("\x89PNG\r\n\x1a\nsynthetic screenshot")
	upload := request()
	upload["day"] = boardToday().Format(time.DateOnly)
	upload["content"] = base64.StdEncoding.EncodeToString(image)
	first := post("upload", upload, http.StatusOK)
	if again := post("upload", upload, http.StatusOK); !reflect.DeepEqual(first, again) {
		t.Fatalf("upload receipt changed on retry: %v vs %v", first, again)
	}
	var imageID string
	if err := db.QueryRowContext(ctx, `SELECT id FROM nurture_images WHERE account_id=$1`, id).Scan(&imageID); err != nil {
		t.Fatal(err)
	}
	get := func(role string, want int) *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodGet, "/api/nurture/image?id="+imageID, nil)
		if role != "" {
			req.AddCookie(&http.Cookie{Name: "board_session", Value: "synthetic-go-daily-" + role + "-cookie"})
		}
		w := httptest.NewRecorder()
		Handler{DB: db}.NurtureImage(w, req)
		if w.Code != want {
			t.Fatalf("image status for %q = %d, want %d", role, w.Code, want)
		}
		return w
	}
	get("", http.StatusUnauthorized)
	get("viewer", http.StatusForbidden)
	served := get("owner", http.StatusOK)
	if served.Header().Get("Content-Type") != "image/png" || served.Header().Get("Cache-Control") != "no-store" || !bytes.Equal(served.Body.Bytes(), image) {
		t.Fatal("image response did not preserve scoped PNG bytes and no-store headers")
	}
	remove := request()
	remove["image_id"] = imageID
	post("delete-image", remove, http.StatusOK)
	get("owner", http.StatusNotFound)
	reupload := request()
	reupload["day"], reupload["content"] = upload["day"], upload["content"]
	post("upload", reupload, http.StatusOK)
	if err := db.QueryRowContext(ctx, `SELECT id FROM nurture_images WHERE account_id=$1`, id).Scan(&imageID); err != nil {
		t.Fatal(err)
	}
	post("pause", request(), http.StatusOK)
	post("resume", request(), http.StatusOK)
	post("finish", request(), http.StatusOK)
	var status, workflow string
	var banked int64
	if err := db.QueryRowContext(ctx, `SELECT a.status,n.workflow,n.banked_days FROM nurture_records n JOIN accounts a ON a.id=n.account_id WHERE a.id=$1`, id).Scan(&status, &workflow, &banked); err != nil || status != "正常" || workflow != "已结束" || banked != 7 {
		t.Fatalf("finished nurture = %q, %q, %d, err=%v", status, workflow, banked, err)
	}
	post("purge", request(), http.StatusOK)
	get("owner", http.StatusNotFound)
}
