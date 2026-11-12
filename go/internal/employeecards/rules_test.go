package employeecards

import (
	"bytes"
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

func TestQuotaRetainsUnconfirmedRelease(t *testing.T) {
	const today = "2026-11-02"
	operator := quotaRow{"id": "synthetic-issuer", "release_mode": "confirmed", "source": "合成凭据", "effective_from": "2026-01-01"}
	record := quotaRow{"id": "synthetic-unit", "operator_id": "synthetic-issuer", "phase": "released",
		"activated_on": "2026-01-01", "release_confirmed_on": "2026-11-01"}
	if unit := quotaUnitInfo(record, operator, today); !unit.current || unit.released != "" {
		t.Fatalf("release without evidence must remain occupied: %+v", unit)
	}
	record["release_source"] = "合成释放凭据"
	if unit := quotaUnitInfo(record, operator, today); unit.current || !unit.issued || unit.released != "2026-11-01" {
		t.Fatalf("confirmed release changed lifetime or current counts: %+v", unit)
	}
}

func TestQuotaIncompleteCompanyLedgerDoesNotInventTotal(t *testing.T) {
	const today = "2026-11-02"
	operator := quotaRow{"id": "synthetic-issuer"}
	rule := quotaRow{"id": "all", "scope": "all", "scope_id": "", "period": "stock", "limit": 10,
		"confirmed": true, "source": "合成规则", "effective_from": "2026-01-01", "pending_counts": true}
	check := quotaRow{"id": "synthetic-check", "scope": "all", "period": "stock", "verified_on": today,
		"valid_until": today, "count": 2, "source": "合成核验", "coverage": "external",
		"unit_ids": []string{}, "pending_unit_ids": []string{}}
	result := quotaEvaluateLimit(rule, operator, nil, []quotaRow{check}, today)
	if result.verified != nil || result.available != nil || result.other == nil || *result.other != 2 {
		t.Fatalf("unconfirmed company inventory produced a combined total: %+v", result)
	}
}

// The optional file captures exact input/output from the frozen Python suite.
func TestQuotaLegacyFixtureParity(t *testing.T) {
	file := os.Getenv("BOARD_GO_QUOTA_PARITY_FILE")
	if file == "" {
		t.Skip("requires the synthetic legacy quota reference")
	}
	raw, err := os.ReadFile(file)
	if err != nil {
		t.Fatal(err)
	}
	type input struct {
		Employee      quotaRow
		Operators     []quotaRow
		Units         []quotaRow
		Verifications []quotaRow
		Applications  []quotaRow
		Rules         []quotaRow
		Today         string
	}
	var cases []struct {
		Input    input
		Expected quotaRow
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&cases); err != nil || len(cases) < 50 {
		t.Fatalf("invalid legacy reference: %v", err)
	}
	for i, test := range cases {
		c := test.Input
		got, err := evaluateEmployee(c.Employee, c.Operators, c.Units, c.Verifications, c.Applications, c.Rules, c.Today)
		if err != nil {
			t.Fatalf("case %d: %v", i, err)
		}
		wire, err := json.Marshal(got)
		if err != nil {
			t.Fatal(err)
		}
		decoder := json.NewDecoder(bytes.NewReader(wire))
		decoder.UseNumber()
		var actual quotaRow
		if err := decoder.Decode(&actual); err != nil {
			t.Fatal(err)
		}
		for key, expected := range test.Expected {
			if !reflect.DeepEqual(actual[key], expected) {
				t.Fatalf("case %d key %q: got %v, want %v", i, key, actual[key], expected)
			}
		}
		if len(actual) != len(test.Expected) {
			t.Fatalf("case %d returned unexpected fields", i)
		}
	}
	t.Logf("matched %d synthetic legacy calculations", len(cases))
}
