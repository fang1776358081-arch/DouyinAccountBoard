package backuprestore

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"hash"
	"strconv"
)

const schema = "board_candidate."

// Foreign-key parents precede their children. Auth, grants, audit, operation
// history, worker state and all durable request ledgers are deliberately absent.
var businessTables = []tableSpec{
	{"computers", "id", 1},
	{"phone_devices", "id", 1},
	{"phone_cards", "id", 1},
	{"accounts", "id", 1},
	{"daily", "account_id,day", 1},
	{"imports", "name", 1},
	{"collection_settings", "id", 1},
	{"publishing_layout", "batch,computer_id,slot", 1},
	{"publishing_layout_state", "id", 1},
	{"publishing_layout_links", "batch,computer_id,slot", 1},
	{"nurture_records", "account_id", 1},
	{"nurture_images", "id", 1},
	{"computer_offices", "computer_id", 2},
	{"account_offices", "account_id", 2},
	{"phone_card_offices", "card_id,office_id", 2},
	{"phone_device_offices", "device_id,office_id", 2},
	{"phone_units", "id", 4},
	{"device_cases", "id", 4},
	{"phone_card_placements", "card_id", 4},
	{"phone_account_access", "account_id", 4},
	{"device_workflow_events", "id", 4},
	{"employee_card_employees", "id", 5},
	{"employee_card_operators", "id", 5},
	{"employee_card_rules", "id", 5},
	{"employee_card_applications", "id", 5},
	{"employee_card_units", "id", 5},
	{"employee_card_links", "id", 5},
	{"employee_card_verifications", "id", 5},
	{"employee_card_events", "id", 5},
	{"publish_review_samples", "id", 6},
	{"publish_review_baselines", "account_id,day", 6},
	{"publish_review_tasks", "account_id,day", 6},
	{"table_sync_account_sources", "account_id", 7},
	{"table_sync_batches", "id", 7},
	{"table_sync_review_items", "id", 7},
	{"table_sync_events", "id", 7},
}

type tableSpec struct {
	name, order string
	since       int
}

type table struct {
	tableSpec
	columns []string
}

type catalog struct {
	version int
	hash    string
	tables  []table
}

func readCatalog(ctx context.Context, tx *sql.Tx) (catalog, error) {
	var result catalog
	digest := sha256.New()
	rows, err := tx.QueryContext(ctx, `SELECT version,sha256 FROM board_candidate.schema_migrations ORDER BY version`)
	if err != nil {
		return result, err
	}
	for rows.Next() {
		var version int
		var checksum string
		if err := rows.Scan(&version, &checksum); err != nil {
			rows.Close()
			return result, err
		}
		if version != result.version+1 || version > 7 || checksum == "" {
			rows.Close()
			return result, errors.New("unsupported PostgreSQL migration ledger")
		}
		result.version = version
		hashValue(digest, strconv.Itoa(version))
		hashValue(digest, checksum)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return result, err
	}
	rows.Close()
	if result.version < 2 {
		return result, errors.New("unsupported PostgreSQL business schema")
	}
	for _, spec := range businessTables {
		if spec.since > result.version {
			continue
		}
		columns, err := readColumns(ctx, tx, spec.name, digest)
		if err != nil {
			return result, err
		}
		result.tables = append(result.tables, table{tableSpec: spec, columns: columns})
	}
	result.hash = sum(digest)
	return result, nil
}

func readColumns(ctx context.Context, tx *sql.Tx, name string, digest hash.Hash) ([]string, error) {
	rows, err := tx.QueryContext(ctx, `SELECT a.attname,format_type(a.atttypid,a.atttypmod)
FROM pg_attribute a WHERE a.attrelid=to_regclass($1) AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum`, schema+name)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	columns := []string{}
	hashValue(digest, name)
	for rows.Next() {
		var field, kind string
		if err := rows.Scan(&field, &kind); err != nil {
			return nil, err
		}
		columns = append(columns, field)
		hashValue(digest, field)
		hashValue(digest, kind)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if len(columns) == 0 {
		return nil, errors.New("required PostgreSQL business table is absent")
	}
	return columns, nil
}

func eachRow(ctx context.Context, tx *sql.Tx, spec tableSpec, use func([]byte) error) (int64, error) {
	rows, err := tx.QueryContext(ctx, `SELECT to_jsonb(t)::text FROM `+schema+spec.name+` t ORDER BY `+spec.order)
	if err != nil {
		return 0, err
	}
	defer rows.Close()
	var count int64
	for rows.Next() {
		var row []byte
		if err := rows.Scan(&row); err != nil {
			return 0, err
		}
		if err := use(row); err != nil {
			return 0, err
		}
		count++
	}
	return count, rows.Err()
}

func stateFingerprint(ctx context.Context, tx *sql.Tx, definition catalog) (string, map[string]int64, error) {
	digest := sha256.New()
	counts := map[string]int64{}
	for _, spec := range definition.tables {
		hashValue(digest, spec.name)
		count, err := eachRow(ctx, tx, spec.tableSpec, func(row []byte) error {
			_, _ = digest.Write(row)
			_, _ = digest.Write([]byte{'\n'})
			return nil
		})
		if err != nil {
			return "", nil, err
		}
		counts[spec.name] = count
	}
	// The queue contains worker tokens. Report the number that will be cleared
	// while keeping its payload out of both the archive and the preview.
	var queue int64
	if err := tx.QueryRowContext(ctx, `SELECT count(*) FROM board_candidate.collection_jobs`).Scan(&queue); err != nil {
		return "", nil, err
	}
	counts["collection_jobs"] = queue
	hashValue(digest, strconv.FormatInt(queue, 10))
	return sum(digest), counts, nil
}

func hashValue(digest hash.Hash, value string) {
	_, _ = digest.Write([]byte(value))
	_, _ = digest.Write([]byte{0})
}

func sum(digest hash.Hash) string { return hex.EncodeToString(digest.Sum(nil)) }
