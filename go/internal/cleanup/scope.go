package cleanup

import (
	"context"
	"database/sql"
	"fmt"
	"strings"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/recordwire"
)

// Parents appear after their dependent rows. System and authentication
// configuration and ordinary historical audit tables are outside this list.
var v6Tables = []string{
	"publish_review_requests",
	"publish_review_baselines",
	"publish_review_tasks",
	"publish_review_samples",
	"employee_card_requests",
	"employee_card_events",
	"employee_card_verifications",
	"employee_card_links",
	"employee_card_units",
	"employee_card_applications",
	"employee_card_employees",
	"device_workflow_events",
	"phone_account_access",
	"phone_card_placements",
	"device_cases",
	"phone_units",
	"phone_device_offices",
	"phone_card_offices",
	"account_offices",
	"nurture_images",
	"nurture_records",
	"daily",
	"collection_jobs",
	"publishing_layout_links",
	"accounts",
	"phone_cards",
	"phone_devices",
}

var v7Tables = []string{
	"table_sync_requests",
	"table_sync_events",
	"table_sync_review_items",
	"table_sync_batches",
	"table_sync_account_sources",
}

// Grid labels and reasons are source account hints, even when unmatched.
const gridFilter = "(p.account_id IS NOT NULL OR p.label<>'' OR p.reason<>'' OR EXISTS (" +
	"SELECT 1 FROM board_candidate.publishing_layout_links l WHERE " +
	"(l.batch,l.computer_id,l.slot)=(p.batch,p.computer_id,p.slot)))"

const gridAfterRow = "CASE WHEN " + gridFilter + " THEN " +
	"to_jsonb(p)||jsonb_build_object('account_id',NULL::text,'label','','reason','') " +
	"ELSE to_jsonb(p) END"

type tableStamp struct {
	Count  int64
	Digest string
}

type snapshot struct {
	guard       string
	counts      map[string]int64
	layoutAfter tableStamp
}

func cleanupTables(ctx context.Context, tx *sql.Tx) ([]string, int, error) {
	var count, first, version int
	err := tx.QueryRowContext(ctx, "SELECT COUNT(*),COALESCE(MIN(version),0),COALESCE(MAX(version),0) "+
		"FROM board_candidate.schema_migrations").Scan(&count, &first, &version)
	if err != nil {
		return nil, 0, err
	}
	if first != 1 || count != version {
		return nil, 0, accountwrite.Conflict("数据库迁移记录不完整，无法安全清理业务资料")
	}
	switch version {
	case 6:
		return v6Tables, version, nil
	case 7:
		tables := make([]string, 0, len(v6Tables)+len(v7Tables))
		tables = append(tables, v7Tables...)
		return append(tables, v6Tables...), version, nil
	default:
		return nil, 0, accountwrite.Conflict("清理功能仅支持已核对的第 6 或第 7 版业务结构")
	}
}

func stampQuery(key, relation, expression, where string) string {
	// Both inputs and predicates are fixed SQL owned by this package.
	return fmt.Sprintf("SELECT '%s' AS item, COUNT(*), "+
		"encode(sha256(convert_to(COALESCE(string_agg(hash,',' ORDER BY hash),''),'UTF8')),'hex') "+
		"FROM (SELECT encode(sha256(convert_to((%s)::text,'UTF8')),'hex') AS hash FROM %s %s) AS rows",
		key, expression, relation, where)
}

func readStamps(ctx context.Context, tx *sql.Tx, queries []string) (map[string]tableStamp, error) {
	rows, err := tx.QueryContext(ctx, strings.Join(queries, " UNION ALL "))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	stamps := make(map[string]tableStamp, len(queries))
	for rows.Next() {
		var name string
		var stamp tableStamp
		if err := rows.Scan(&name, &stamp.Count, &stamp.Digest); err != nil {
			return nil, err
		}
		stamps[name] = stamp
	}
	return stamps, rows.Err()
}

func readCurrent(ctx context.Context, tx *sql.Tx) ([]string, snapshot, error) {
	tables, version, err := cleanupTables(ctx, tx)
	if err != nil {
		return nil, snapshot{}, err
	}
	if err := lockAndValidate(ctx, tx, tables); err != nil {
		return nil, snapshot{}, err
	}
	queries := make([]string, 0, len(tables)+4)
	for _, table := range tables {
		queries = append(queries, stampQuery(table, schema+table+" t", "to_jsonb(t)", ""))
	}
	queries = append(queries,
		stampQuery("grid", schema+"publishing_layout p", "to_jsonb(p)", ""),
		stampQuery("grid_after", schema+"publishing_layout p", gridAfterRow, ""),
		"SELECT '"+gridCountKey+"' AS item,COUNT(*),'' FROM board_candidate.publishing_layout p WHERE "+gridFilter,
		stampQuery("grid_state", schema+"publishing_layout_state s", "to_jsonb(s)", ""),
	)
	stamps, err := readStamps(ctx, tx, queries)
	if err != nil {
		return nil, snapshot{}, err
	}
	if len(stamps) != len(queries) {
		return nil, snapshot{}, accountwrite.Conflict("清理范围统计不完整，未更改业务资料")
	}
	counts := make(map[string]int64, len(tables)+1)
	counts[gridCountKey] = stamps[gridCountKey].Count
	for _, table := range tables {
		counts[table] = stamps[table].Count
	}
	guard, err := recordwire.Version(struct {
		Version int
		Stamps  map[string]tableStamp
	}{version, stamps})
	if err != nil {
		return nil, snapshot{}, err
	}
	return tables, snapshot{guard: guard, counts: counts, layoutAfter: stamps["grid_after"]}, nil
}

func removeBusinessRows(ctx context.Context, tx *sql.Tx, tables []string, layoutAfter tableStamp) error {
	statements := []string{
		"UPDATE board_candidate.publishing_layout p SET account_id=NULL,label='',reason='' WHERE " + gridFilter,
	}
	for _, table := range tables {
		statements = append(statements, "DELETE FROM "+schema+table)
	}
	if _, err := tx.ExecContext(ctx, strings.Join(statements, "; ")); err != nil {
		return err
	}
	queries := make([]string, 0, len(tables)+1)
	for _, table := range tables {
		queries = append(queries, "SELECT '"+table+"' AS item,COUNT(*),'' FROM "+schema+table)
	}
	queries = append(queries, stampQuery("grid", schema+"publishing_layout p", "to_jsonb(p)", ""))
	stamps, err := readStamps(ctx, tx, queries)
	if err != nil {
		return err
	}
	if len(stamps) != len(queries) || stamps["grid"] != layoutAfter {
		return accountwrite.Conflict("发布格保留校验失败，整批未清理")
	}
	for _, table := range tables {
		if stamps[table].Count != 0 {
			return accountwrite.Conflict("仍存在应清理的业务资料，整批未清理")
		}
	}
	return nil
}
