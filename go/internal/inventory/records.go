package inventory

import (
	"context"
	"database/sql"
	"errors"
	"maps"
	"slices"
	"strings"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

type relation struct {
	table, id, mapping, mappingID string
}

var (
	cardRelation    = relation{"phone_cards", "id", "phone_card_offices", "card_id"}
	deviceRelation  = relation{"phone_devices", "id", "phone_device_offices", "device_id"}
	accountRelation = relation{"accounts", "id", "account_offices", "account_id"}
	unitRelation    = relation{table: "phone_units", id: "id"}
	accessRelation  = relation{table: "phone_account_access", id: "account_id"}
	caseRelation    = relation{table: "device_cases", id: "id"}
	eventRelation   = relation{table: "device_workflow_events", id: "id"}
)

// CardRecord reads the unmodified record used for optimistic version tokens.
// Callers must check authorization before exposing its contents.
func CardRecord(ctx context.Context, tx *sql.Tx, id int64) (map[string]any, error) {
	return readRecord(ctx, tx, cardRelation, id)
}

// DeviceRecord reads the unmodified record used for optimistic version tokens.
// Callers must check authorization before exposing its contents.
func DeviceRecord(ctx context.Context, tx *sql.Tx, id string) (map[string]any, error) {
	return readRecord(ctx, tx, deviceRelation, id)
}

func readRecord(ctx context.Context, tx *sql.Tx, entity relation, id any) (map[string]any, error) {
	// These identifiers are drawn solely from the package's static relation list.
	query := `SELECT to_json(p)::text, '[]' FROM board_candidate.` + entity.table + ` p WHERE p.` + entity.id + `=$1`
	if entity.mapping != "" {
		query = `SELECT to_json(p)::text, COALESCE(
			(SELECT json_agg(m.office_id ORDER BY m.office_id)::text FROM board_candidate.` + entity.mapping +
			` m WHERE m.` + entity.mappingID + `=p.` + entity.id + `), '[]')
			FROM board_candidate.` + entity.table + ` p WHERE p.` + entity.id + `=$1`
	}
	var raw, offices string
	err := tx.QueryRowContext(ctx, query, id).Scan(&raw, &offices)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return inventoryRow(raw, offices)
}

func recordOffices(record map[string]any) []string {
	offices, _ := record["office_ids"].([]string)
	return offices
}

func canEdit(actor session.User, record map[string]any) bool {
	if record == nil {
		return false
	}
	if actor.Global() {
		return true
	}
	offices := recordOffices(record)
	if len(offices) == 0 {
		return false
	}
	for _, office := range offices {
		if !slices.Contains(actor.OfficeIDs, office) {
			return false
		}
	}
	return true
}

func requireEdit(actor session.User, record map[string]any) error {
	if !canEdit(actor, record) {
		return accountwrite.Forbidden("无权访问此资料")
	}
	return nil
}

func requirePermission(actor session.User, permission string) error {
	if !actor.Has(permission) {
		return accountwrite.Forbidden("当前账号没有此操作权限")
	}
	return nil
}

func requestedOffices(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) ([]string, error) {
	value, supplied := body["office_ids"]
	if !supplied && !actor.Global() {
		return nil, accountwrite.Invalid("新增资料必须明确选择办公室")
	}
	offices := []string{}
	switch values := value.(type) {
	case nil:
	case []string:
		offices = append(offices, values...)
	case []any:
		for _, value := range values {
			office, ok := value.(string)
			if !ok {
				return nil, accountwrite.Invalid("办公室范围格式不正确")
			}
			offices = append(offices, office)
		}
	default:
		return nil, accountwrite.Invalid("办公室范围格式不正确")
	}
	if len(offices) > 100 {
		return nil, accountwrite.Invalid("办公室范围格式不正确")
	}
	slices.Sort(offices)
	for i, office := range offices {
		if office == "" || len([]rune(office)) > 80 || strings.IndexByte(office, 0) >= 0 {
			return nil, accountwrite.Invalid("办公室范围格式不正确")
		}
		if i > 0 && offices[i-1] == office {
			return nil, accountwrite.Invalid("办公室范围不能重复")
		}
		if !slices.Contains(actor.OfficeIDs, office) {
			var exists bool
			if err := tx.QueryRowContext(ctx, `SELECT EXISTS(
				SELECT 1 FROM board_candidate.offices WHERE id=$1 AND active=1)`, office).Scan(&exists); err != nil {
				return nil, err
			}
			if !exists {
				return nil, accountwrite.Invalid("办公室不存在或已停用")
			}
		}
	}
	if !actor.Global() {
		if len(offices) == 0 {
			return nil, accountwrite.Forbidden("目标办公室不在授权范围内")
		}
		for _, office := range offices {
			if !slices.Contains(actor.OfficeIDs, office) {
				return nil, accountwrite.Forbidden("目标办公室不在授权范围内")
			}
		}
	}
	return offices, nil
}

func saveOffices(ctx context.Context, tx *sql.Tx, entity relation, id any, offices []string) error {
	// The owning service has already validated the requested offices and
	// acquired the shared write-admission lock before reaching this mutation.
	if _, err := tx.ExecContext(ctx, `DELETE FROM board_candidate.`+entity.mapping+` WHERE `+entity.mappingID+`=$1`, id); err != nil {
		return err
	}
	for _, office := range offices {
		if _, err := tx.ExecContext(ctx, `INSERT INTO board_candidate.`+entity.mapping+`(`+entity.mappingID+`,office_id) VALUES($1,$2)`, id, office); err != nil {
			return err
		}
	}
	return nil
}

type snapshot map[string]map[string]any

func (records snapshot) versions() (map[string]any, error) {
	versions := make(map[string]any, len(records))
	for key, row := range records {
		var version any
		if row != nil {
			token, err := recordwire.Version(row)
			if err != nil {
				return nil, err
			}
			version = token
		}
		versions[key] = version
	}
	return versions, nil
}

func (records snapshot) check(body map[string]any) error {
	versions, err := records.versions()
	if err != nil {
		return err
	}
	return accountwrite.CheckVersions(body, versions)
}

func (before snapshot) finish(ctx context.Context, tx *sql.Tx, actor session.User, action, target string, after snapshot) (map[string]any, error) {
	batchAction, group := accountwrite.HistoryGroup(ctx)
	if group == "" {
		var err error
		group, err = accountwrite.NewGroup()
		if err != nil {
			return nil, err
		}
	} else {
		action = batchAction
	}
	keys := map[string]bool{}
	for key := range before {
		keys[key] = true
	}
	for key := range after {
		keys[key] = true
	}
	reload := false
	for _, key := range slices.Sorted(maps.Keys(keys)) {
		old, next := before[key], after[key]
		oldToken, err := recordwire.Version(old)
		if err != nil {
			return nil, err
		}
		nextToken, err := recordwire.Version(next)
		if err != nil {
			return nil, err
		}
		if oldToken == nextToken {
			continue
		}
		scope := recordOffices(next)
		if next == nil {
			scope = recordOffices(old)
		}
		moved := old != nil && next != nil && !slices.Equal(recordOffices(old), recordOffices(next))
		multipleReferences := strings.HasPrefix(key, "access:") || strings.HasPrefix(key, "unit:") || strings.HasPrefix(key, "workflow_event:")
		if moved || (multipleReferences && (len(recordOffices(old)) > 1 || len(recordOffices(next)) > 1)) {
			// Related records can require access to more than one office. Let
			// listeners refresh their scoped boards without disclosing payloads.
			scope = nil
			reload = true
		}
		if err := accountwrite.InsertHistory(ctx, tx, actor, action, group, key, old, next, scope); err != nil {
			return nil, err
		}
	}
	if reload {
		if err := accountwrite.InsertHistory(ctx, tx, actor, action, group, "sync:reload", nil, nil, nil); err != nil {
			return nil, err
		}
	}
	if batchAction == "" {
		if err := accountwrite.Audit(ctx, tx, actor, action, target); err != nil {
			return nil, err
		}
	}
	return after.versions()
}
