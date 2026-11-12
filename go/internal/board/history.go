package board

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"time"
)

func (h Handler) CollectionStatus(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()
	tx, actor, err := h.beginRead(ctx, r)
	if err != nil {
		readFailure(w, err)
		return
	}
	defer tx.Rollback()
	result, err := collectionOverview(ctx, tx, &scopeMap{actor: actor}, nil, h.CollectionJobPrefix)
	if err != nil {
		readFailure(w, err)
		return
	}
	if err := tx.Commit(); err != nil {
		readFailure(w, err)
		return
	}
	canManage := h.CollectionJobPrefix != "" && actor.Has("collection_manage")
	reply(w, http.StatusOK, row{"collection": result, "can_manage": canManage, "can_control": canManage && actor.Global()})
}

func (h Handler) History(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()
	tx, actor, err := h.beginRead(ctx, r)
	if err != nil {
		readFailure(w, err)
		return
	}
	defer tx.Rollback()
	if !actor.Has("history_view") {
		fail(w, http.StatusForbidden, "当前账号没有此操作权限")
		return
	}
	entity := queryValue(r, "entity")
	var before int64
	if value := queryValue(r, "before"); value != "" {
		before, err = strconv.ParseInt(value, 10, 64)
		if err != nil {
			fail(w, http.StatusBadRequest, "历史记录位置不正确")
			return
		}
	}
	scope, err := loadScope(ctx, tx, actor)
	if err != nil {
		readFailure(w, err)
		return
	}
	if !actor.Global() {
		if entity != "" && !scope.canReadHistory(entity) {
			fail(w, http.StatusForbidden, "无权访问此资料历史")
			return
		}
		if err := loadWorkflowRefs(ctx, tx, scope); err != nil {
			readFailure(w, err)
			return
		}
	}
	result, err := historyPage(ctx, tx, scope, entity, before)
	if err != nil {
		readFailure(w, err)
		return
	}
	if err := tx.Commit(); err != nil {
		readFailure(w, err)
		return
	}
	reply(w, http.StatusOK, result)
}

func (s *scopeMap) canReadHistory(entity string) bool {
	prefix, id, ok := strings.Cut(entity, ":")
	if !ok || id == "" {
		return false
	}
	switch prefix {
	case "account", "nurture":
		return s.canSee("accounts", id)
	case "card":
		return s.canSee("phone_cards", id)
	case "device":
		return s.canSee("phone_devices", id)
	case "computer":
		return s.canSee("computers", id)
	default:
		return false
	}
}

func historyPage(ctx context.Context, tx *sql.Tx, scope *scopeMap, entity string, before int64) (row, error) {
	account := strings.TrimPrefix(entity, "account:")
	if account == entity {
		account = ""
	}
	// A literal prefix avoids treating underscores in account identities as LIKE wildcards.
	items, err := selectRows(ctx, tx, `SELECT to_jsonb(h) FROM board_candidate.operations_history h WHERE
		($1='' OR h.entity=$1 OR ($2<>'' AND (h.entity='nurture:'||$2 OR starts_with(h.entity,'daily:'||$2||':') OR
		(h.entity LIKE 'layout:%' AND (h.before_json::jsonb->>'account_id'=$2 OR h.after_json::jsonb->>'account_id'=$2)))))
		AND ($3::bigint=0 OR h.id<$3) ORDER BY h.id DESC LIMIT 100`, entity, account, before)
	if err != nil {
		return nil, err
	}
	var next int64
	if len(items) == 100 {
		next = number(items[len(items)-1]["id"])
	}
	entries := make([]row, 0, len(items))
	for _, item := range items {
		entity := str(item["entity"])
		if strings.HasPrefix(entity, "sync:") {
			continue
		}
		old, new := historyRecord(str(item["before_json"])), historyRecord(str(item["after_json"]))
		if !scope.actor.Global() && !scope.visibleHistory(entity, str(item["scope_offices"]), old, new) {
			continue
		}
		delete(item, "before_json")
		delete(item, "after_json")
		delete(item, "scope_offices")
		item["changes"] = historyChanges(old, new)
		entries = append(entries, item)
	}
	return row{"entries": entries, "next_before": next}, nil
}

func (s *scopeMap) visibleHistory(entity, rawOffices string, old, new row) bool {
	var offices []string
	if json.Unmarshal([]byte(rawOffices), &offices) != nil || len(offices) == 0 {
		return false
	}
	for _, office := range offices {
		if !slices.Contains(s.actor.OfficeIDs, office) {
			return false
		}
	}
	prefix, _, _ := strings.Cut(entity, ":")
	for _, table := range tables {
		if table.prefix == prefix {
			item := new
			if len(new) == 0 {
				item = old
			}
			return s.recordVisible(table.table, item)
		}
	}
	return false
}

func historyRecord(value string) row {
	var item row
	decoder := json.NewDecoder(strings.NewReader(value))
	decoder.UseNumber()
	if decoder.Decode(&item) != nil {
		return nil
	}
	return item
}

func historyChanges(old, new row) []row {
	keys := make([]string, 0, len(old)+len(new))
	for field := range old {
		keys = append(keys, field)
	}
	for field := range new {
		if _, exists := old[field]; !exists {
			keys = append(keys, field)
		}
	}
	slices.Sort(keys)
	result := make([]row, 0)
	for _, field := range keys {
		if !reflect.DeepEqual(old[field], new[field]) {
			result = append(result, row{"field": field, "before": old[field], "after": new[field]})
		}
	}
	return result
}
