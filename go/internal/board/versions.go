package board

import (
	"context"
	"database/sql"
	"net/http"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"douyinboard/internal/recordwire"
)

type tableVersion struct {
	table, prefix string
	fields        []string
	workflow      bool
}

var tables = []tableVersion{
	{"computers", "computer", []string{"id"}, false},
	{"accounts", "account", []string{"id"}, false},
	{"phone_cards", "card", []string{"id"}, false},
	{"phone_devices", "device", []string{"id"}, false},
	{"daily", "daily", []string{"account_id", "day"}, false},
	{"publishing_layout", "layout", []string{"batch", "computer_id", "slot"}, false},
	{"nurture_records", "nurture", []string{"account_id"}, false},
	{"phone_units", "unit", []string{"id"}, true},
	{"phone_card_placements", "placement", []string{"card_id"}, true},
	{"phone_account_access", "access", []string{"account_id"}, true},
	{"device_cases", "case", []string{"id"}, true},
	{"device_workflow_events", "workflow_event", []string{"id"}, true},
}

func boardVersions(ctx context.Context, tx *sql.Tx, scope *scopeMap, day string) (row, error) {
	result := row{}
	for _, table := range tables {
		if table.workflow && scope.units == nil {
			continue
		}
		query := `SELECT to_jsonb(t) FROM board_candidate.` + table.table + ` t`
		args := []any{}
		if day != "" && table.table == "daily" {
			query += ` WHERE t.day=$1`
			args = append(args, day)
		}
		items, err := selectRows(ctx, tx, query, args...)
		if err != nil {
			return nil, err
		}
		for _, item := range items {
			if !scope.recordVisible(table.table, item) {
				continue
			}
			item["office_ids"] = scope.recordOffices(table.table, item)
			version, err := recordwire.Version(item)
			if err != nil {
				return nil, err
			}
			result[joinedKey(table.prefix, table.fields, item)] = version
		}
	}
	return result, nil
}

func (h Handler) RecordVersions(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()
	tx, actor, err := h.beginRead(ctx, r)
	if err != nil {
		readFailure(w, err)
		return
	}
	defer tx.Rollback()
	parsed := make([]versionKey, 0)
	for _, raw := range strings.Split(queryValue(r, "keys"), ",") {
		if raw == "" {
			continue
		}
		key, ok := parseKey(raw)
		if !ok || len(parsed) == 80 {
			fail(w, http.StatusBadRequest, "资料版本编号不正确")
			return
		}
		parsed = append(parsed, key)
	}
	scope, err := loadScope(ctx, tx, actor)
	if err != nil {
		readFailure(w, err)
		return
	}
	result := row{}
	workflowChecked := false
	for _, key := range parsed {
		if key.workflow && !workflowChecked {
			if err := loadWorkflowRefs(ctx, tx, scope); err != nil {
				readFailure(w, err)
				return
			}
			workflowChecked = true
		}
		value, err := versionFor(ctx, tx, scope, key)
		if err != nil {
			readFailure(w, err)
			return
		}
		result[key.raw] = value
	}
	if err := tx.Commit(); err != nil {
		readFailure(w, err)
		return
	}
	reply(w, http.StatusOK, row{"record_versions": result})
}

type versionKey struct {
	tableVersion
	raw    string
	values []any
}

func parseKey(raw string) (versionKey, bool) {
	prefix, tail, ok := strings.Cut(raw, ":")
	if !ok || tail == "" || utf8.RuneCountInString(raw) > 300 || strings.ContainsRune(raw, 0) {
		return versionKey{}, false
	}
	var table tableVersion
	for _, t := range tables {
		if prefix == t.prefix {
			table = t
			break
		}
	}
	key := versionKey{tableVersion: table, raw: raw}
	switch prefix {
	case "daily":
		index := strings.LastIndexByte(tail, ':')
		if index < 1 || index == len(tail)-1 {
			return key, false
		}
		key.values = []any{tail[:index], tail[index+1:]}
	case "layout":
		batch, rest, ok := strings.Cut(tail, ":")
		index := strings.LastIndexByte(rest, ':')
		if !ok || batch == "" || index < 1 || index == len(rest)-1 {
			return key, false
		}
		slot := rest[index+1:]
		numeric, err := strconv.ParseInt(slot, 10, 64)
		if err != nil || strconv.FormatInt(numeric, 10) != slot {
			return key, false
		}
		key.values = []any{batch, rest[:index], numeric}
	case "card", "placement":
		numeric, err := strconv.ParseInt(tail, 10, 64)
		if err != nil || strconv.FormatInt(numeric, 10) != tail {
			return key, false
		}
		key.values = []any{numeric}
	default:
		key.values = []any{tail}
	}
	return key, table.table != ""
}

func versionFor(ctx context.Context, tx *sql.Tx, scope *scopeMap, key versionKey) (any, error) {
	if key.workflow && scope.units == nil {
		return nil, nil
	}
	query := `SELECT to_jsonb(t) FROM board_candidate.` + key.table + ` t WHERE `
	for i, field := range key.fields {
		if i > 0 {
			query += ` AND `
		}
		query += `t.` + field + `=$` + strconv.Itoa(i+1)
	}
	rows, err := selectRows(ctx, tx, query, key.values...)
	if err != nil || len(rows) == 0 {
		return nil, err
	}
	item := rows[0]
	if !scope.recordVisible(key.table, item) {
		return nil, nil
	}
	item["office_ids"] = scope.recordOffices(key.table, item)
	return recordwire.Version(item)
}
