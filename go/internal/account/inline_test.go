package account

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
)

func TestInlineRecordUsesPythonFullRecordToken(t *testing.T) {
	name := "乐丰货车<>&"
	note := "line\u2028para\u2029 and \\u2028"
	record := map[string]any{
		"id": "00123", "name": name, "status": nil, "note": note,
		"collector_count": json.Number("9007199254740993"),
		"office_ids":      []string{"office3"},
		"phone":           "nonpublic",
	}
	result, err := inlineRecord(record)
	if err != nil {
		t.Fatal(err)
	}
	// Python json.dumps with ensure_ascii=False, sort_keys=True and compact separators.
	const pythonToken = "43f6f12f9bec4c4d77323e1d7455c6d2b76ab9101540b1775a0d3bb84c0532d6"
	if result.RecordVersion != pythonToken {
		t.Fatalf("version = %s, want %s", result.RecordVersion, pythonToken)
	}
	response := httptest.NewRecorder()
	reply(response, http.StatusOK, result)
	var projected struct {
		Account map[string]any `json:"account"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &projected); err != nil {
		t.Fatal(err)
	}
	want := map[string]any{"id": "00123", "name": name, "status": nil, "note": note}
	if !reflect.DeepEqual(projected.Account, want) {
		t.Fatalf("account = %#v, want %#v", projected.Account, want)
	}
	if response.Header().Get("Cache-Control") != "no-store" {
		t.Fatal("account response must disable caching")
	}
}

func TestAccountIDKeepsOriginalIdentifier(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/api/account/inline?id=&id=001%2B23&id=other", nil)
	if got := accountID(req); got != "001+23" {
		t.Fatalf("id = %q, want 001+23", got)
	}
}
