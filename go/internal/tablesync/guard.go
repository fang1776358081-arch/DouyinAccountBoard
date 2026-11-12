package tablesync

import (
	"context"
	"database/sql"
	"encoding/json"
	"maps"
	"regexp"
	"strings"

	"douyinboard/internal/recordwire"
)

var protectedTables = map[string]bool{
	"computers": true, "phone_cards": true, "phone_devices": true, "daily": true, "nurture_records": true, "nurture_images": true,
	"phone_units": true, "phone_card_placements": true, "phone_account_access": true, "device_cases": true, "device_workflow_events": true,
	"publishing_layout": true, "publishing_layout_state": true, "publishing_layout_links": true, "computer_offices": true,
	"phone_card_offices": true, "phone_device_offices": true, "offices": true, "user_office_grants": true, "auth_users": true,
	"collection_jobs": true, "collection_settings": true, "collection_worker": true,
}

var sqlName = regexp.MustCompile(`^[a-z][a-z0-9_]*$`)

type snapshot struct {
	Accounts  map[string]item
	Sources   map[string]item
	Offices   map[string]string
	Protected map[string]string
	History   ledgerStamp
	Audit     ledgerStamp
}

type ledgerStamp struct {
	Max   int64
	Count int64
	Hash  string
}

func readSnapshot(ctx context.Context, tx *sql.Tx) (snapshot, error) {
	accounts, err := jsonRows(ctx, tx, `SELECT to_jsonb(t) FROM board_candidate.accounts t`)
	if err != nil {
		return snapshot{}, err
	}
	sources, err := jsonRows(ctx, tx, `SELECT to_jsonb(t) FROM board_candidate.table_sync_account_sources t`)
	if err != nil {
		return snapshot{}, err
	}
	offices, err := jsonRows(ctx, tx, `SELECT to_jsonb(t) FROM board_candidate.account_offices t`)
	if err != nil {
		return snapshot{}, err
	}
	allTables, err := jsonRows(ctx, tx, `SELECT jsonb_build_object('name',tablename) FROM pg_catalog.pg_tables WHERE schemaname='board_candidate' ORDER BY tablename`)
	if err != nil {
		return snapshot{}, err
	}
	state := snapshot{Accounts: indexed(accounts, "id"), Sources: indexed(sources, "account_id"), Offices: make(map[string]string), Protected: make(map[string]string)}
	for _, row := range offices {
		state.Offices[text(row["account_id"])] = text(row["office_id"])
	}
	for _, row := range allTables {
		table := text(row["name"])
		if !sqlName.MatchString(table) || (!protectedTables[table] && !strings.HasPrefix(table, "employee_card_") && !strings.HasPrefix(table, "publish_review_")) {
			continue
		}
		stamp, err := tableStamp(ctx, tx, table, false, 0)
		if err != nil {
			return snapshot{}, err
		}
		state.Protected[table] = stamp.Hash
	}
	if state.History, err = tableStamp(ctx, tx, "operations_history", true, 0); err != nil {
		return snapshot{}, err
	}
	state.Audit, err = tableStamp(ctx, tx, "auth_audit", true, 0)
	return state, err
}

// The digests leave protected tables' raw values inside PostgreSQL, including
// account password hashes and uploaded screenshot bytes.
func tableStamp(ctx context.Context, tx *sql.Tx, table string, ledger bool, ceiling int64) (ledgerStamp, error) {
	var result ledgerStamp
	maxID := "0::bigint"
	if ledger {
		maxID = "COALESCE(MAX(id),0)"
	}
	query := `SELECT ` + maxID + `,COUNT(*),encode(sha256(convert_to(COALESCE(string_agg(hash,',' ORDER BY hash),''),'UTF8')),'hex')
		FROM (SELECT `
	if ledger {
		query += `id,`
	}
	query += `encode(sha256(convert_to(to_jsonb(t)::text,'UTF8')),'hex') AS hash FROM board_candidate.` + table + ` t`
	var args []any
	if ledger && ceiling > 0 {
		query += ` WHERE id<=$1`
		args = []any{ceiling}
	}
	query += `) AS hashed`
	err := tx.QueryRowContext(ctx, query, args...).Scan(&result.Max, &result.Count, &result.Hash)
	return result, err
}

func indexed(rows []item, key string) map[string]item {
	result := make(map[string]item, len(rows))
	for _, row := range rows {
		result[text(row[key])] = row
	}
	return result
}

func same(left, right any) bool {
	if left == nil || right == nil {
		return left == nil && right == nil
	}
	leftVersion, leftErr := recordwire.Version(left)
	rightVersion, rightErr := recordwire.Version(right)
	return leftErr == nil && rightErr == nil && leftVersion == rightVersion
}

func expectedSnapshot(before snapshot, plans []changePlan) snapshot {
	expected := before
	expected.Accounts, expected.Sources, expected.Offices = maps.Clone(before.Accounts), maps.Clone(before.Sources), maps.Clone(before.Offices)
	for _, plan := range plans {
		if plan.AfterAccount == nil {
			delete(expected.Accounts, plan.AccountID)
			delete(expected.Offices, plan.AccountID)
		} else {
			expected.Accounts[plan.AccountID] = plan.AfterAccount
			if plan.Action == "create" {
				expected.Offices[plan.AccountID] = plan.Office
			}
		}
		if plan.AfterSource == nil {
			delete(expected.Sources, plan.AccountID)
		} else {
			expected.Sources[plan.AccountID] = plan.AfterSource
		}
	}
	return expected
}

func verifyWrite(ctx context.Context, tx *sql.Tx, before snapshot, plans []changePlan, historyDelta, auditDelta int64) error {
	actual, err := readSnapshot(ctx, tx)
	if err != nil {
		return err
	}
	expected := expectedSnapshot(before, plans)
	if !same(actual.Accounts, expected.Accounts) || !same(actual.Sources, expected.Sources) || !maps.Equal(actual.Offices, expected.Offices) || !maps.Equal(actual.Protected, expected.Protected) {
		return conflict("事务保护失败：出现计划外资料、身份、设备、发布或办公室变化，整批已撤销")
	}
	for _, ledger := range []struct {
		table         string
		prior, latest ledgerStamp
		increase      int64
	}{
		{"operations_history", before.History, actual.History, historyDelta}, {"auth_audit", before.Audit, actual.Audit, auditDelta},
	} {
		if ledger.latest.Count-ledger.prior.Count != ledger.increase {
			return conflict("产生计划外业务历史，整批已撤销")
		}
		if ledger.increase == 0 && ledger.prior != ledger.latest {
			return conflict("原历史或审计发生变化，整批已撤销")
		}
		if ledger.increase > 0 && ledger.prior.Max > 0 {
			retained, err := tableStamp(ctx, tx, ledger.table, true, ledger.prior.Max)
			if err != nil {
				return err
			}
			if retained != ledger.prior {
				return conflict("原历史或审计发生变化，整批已撤销")
			}
		}
	}
	return nil
}

func savepoint(ctx context.Context, tx *sql.Tx, run func() error) error {
	if _, err := tx.ExecContext(ctx, `SAVEPOINT table_sync_validation`); err != nil {
		return err
	}
	operationErr := run()
	if _, err := tx.ExecContext(ctx, `ROLLBACK TO SAVEPOINT table_sync_validation`); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `RELEASE SAVEPOINT table_sync_validation`); err != nil {
		return err
	}
	return operationErr
}

func decodeStructured(raw []byte, target any) error {
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.UseNumber()
	return decoder.Decode(target)
}
