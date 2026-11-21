package board

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"slices"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

const (
	accountsOfficeID = "three"
	accountsPageSize = 50
	pagedComputerID  = `CASE WHEN $3 OR co.office_id=ANY($4::text[]) THEN a.computer_id ELSE 'unassigned' END`
)

type accountsQuery struct {
	day        string
	page       int
	where      string
	args       []any
	query      string
	douyinID   string
	accountID  string
	computerID string
	status     string
	published  string
}

var accountsQueryKeys = map[string]bool{
	"office_id": true, "date": true, "page": true, "query": true,
	"douyin_id": true, "account_id": true, "computer_id": true,
	"status": true, "published": true,
}

// Accounts returns a fixed-size, server-filtered Office 3 account page. It is
// deliberately separate from Board so the pilot can migrate without changing
// the legacy snapshot contract.
func (h Handler) Accounts(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	tx, actor, err := h.beginRead(ctx, r)
	if err != nil {
		readFailure(w, err)
		return
	}
	defer tx.Rollback()

	parsed, err := parseAccountsQuery(r.URL.Query(), time.Now().In(china))
	if err != nil {
		fail(w, http.StatusBadRequest, err.Error())
		return
	}
	if !actor.Global() && !slices.Contains(actor.OfficeIDs, accountsOfficeID) {
		fail(w, http.StatusForbidden, "无权访问办公室三账号")
		return
	}
	result, err := accountsPage(ctx, tx, actor, parsed)
	if err != nil {
		readFailure(w, err)
		return
	}
	if err := tx.Commit(); err != nil {
		readFailure(w, err)
		return
	}
	WriteNegotiatedJSON(w, r, result)
}

func parseAccountsQuery(values url.Values, now time.Time) (accountsQuery, error) {
	for key, entries := range values {
		if !accountsQueryKeys[key] {
			return accountsQuery{}, errors.New("账号筛选条件不正确")
		}
		if len(entries) != 1 {
			return accountsQuery{}, errors.New("账号筛选条件不能重复")
		}
	}
	if values.Get("office_id") != accountsOfficeID {
		return accountsQuery{}, errors.New("当前接口仅支持办公室三")
	}
	day := values.Get("date")
	if day == "" {
		day = now.Format(time.DateOnly)
	}
	if _, ok := parseDay(day); !ok {
		return accountsQuery{}, errors.New("日期格式不正确")
	}
	page := 1
	if raw := values.Get("page"); raw != "" {
		parsed, err := strconv.Atoi(raw)
		if err != nil || parsed < 1 || parsed > 1_000_000 {
			return accountsQuery{}, errors.New("账号页码不正确")
		}
		page = parsed
	}
	published := values.Get("published")
	if published != "" && published != "yes" && published != "no" {
		return accountsQuery{}, errors.New("发布筛选条件不正确")
	}
	query := accountsQuery{
		day: day, page: page, query: strings.TrimSpace(values.Get("query")),
		douyinID:   strings.TrimSpace(values.Get("douyin_id")),
		accountID:  strings.TrimSpace(values.Get("account_id")),
		computerID: values.Get("computer_id"), status: values.Get("status"), published: published,
	}
	for _, value := range []string{query.query, query.douyinID, query.accountID, query.computerID, query.status} {
		if strings.ContainsRune(value, 0) || utf8.RuneCountInString(value) > 200 {
			return accountsQuery{}, errors.New("账号筛选内容过长")
		}
	}
	return query, nil
}

func (q *accountsQuery) buildWhere(actor session.User) {
	clauses := []string{"ao.office_id=$1"}
	q.args = []any{accountsOfficeID, q.day, actor.Global(), actor.OfficeIDs}
	add := func(clause string, value any) {
		q.args = append(q.args, value)
		clauses = append(clauses, fmt.Sprintf(clause, len(q.args)))
	}
	if q.query != "" {
		add(`strpos(lower(a.name),lower($%d))>0`, q.query)
	}
	if q.douyinID != "" {
		add(`lower(regexp_replace(a.douyin_id,'^[[:space:]]+|[[:space:]]+$','','g'))=lower($%d)`, q.douyinID)
	}
	if q.accountID != "" {
		add(`lower(regexp_replace(a.id,'^[[:space:]]+|[[:space:]]+$','','g'))=lower($%d)`, q.accountID)
	}
	if q.computerID != "" {
		add(`(`+pagedComputerID+`)=$%d`, q.computerID)
	}
	if q.status != "" {
		add(`a.status=$%d`, q.status)
	}
	if q.published != "" {
		add(`COALESCE(d.published,0)=$%d`, map[string]int{"yes": 1, "no": 0}[q.published])
	}
	q.where = strings.Join(clauses, " AND ")
}

func accountsPage(ctx context.Context, tx *sql.Tx, actor session.User, query accountsQuery) (row, error) {
	query.buildWhere(actor)
	from := ` FROM board_candidate.accounts a
		JOIN board_candidate.account_offices ao ON ao.account_id=a.id
		LEFT JOIN board_candidate.computer_offices co ON co.computer_id=a.computer_id
		LEFT JOIN board_candidate.daily d ON d.account_id=a.id AND d.day=$2
		WHERE ` + query.where
	order := ` ORDER BY ` + integerOrder(pagedComputerID) + `,a.slot,a.id`
	matching, publishedTotal, computerTotal, err := accountMatches(ctx, tx, `SELECT a.id,COALESCE(d.published,0),`+pagedComputerID+from+order, query.args...)
	if err != nil {
		return nil, err
	}
	total := len(matching)
	pages := (total + accountsPageSize - 1) / accountsPageSize
	if pages < 1 {
		pages = 1
	}
	page := min(query.page, pages)
	start := (page - 1) * accountsPageSize
	accounts, versions, err := accountPageRecords(ctx, tx, matching[start:min(start+accountsPageSize, total)], query.day)
	if err != nil {
		return nil, err
	}
	visible := map[string]map[string]bool{}
	if !actor.Global() {
		visible, err = visibleAccountReferences(ctx, tx, accounts, actor.OfficeIDs)
		if err != nil {
			return nil, err
		}
	}
	for _, account := range accounts {
		if !actor.Global() {
			if !visible["accounts"][str(account["parent_account_id"])] {
				account["parent_account_id"] = nil
			}
			if !visible["phone_devices"][str(account["phone_device_id"])] {
				account["phone_device_id"] = nil
			}
			if !visible["computers"][str(account["computer_id"])] {
				account["computer_id"] = "unassigned"
			}
		}
	}
	today := time.Now().In(china).Format(time.DateOnly)
	if err := enrichPagedLifecycle(ctx, tx, actor, accounts, today); err != nil {
		return nil, err
	}
	bootstrap, err := accountsBootstrap(ctx, tx, actor, accounts, today, versions)
	if err != nil {
		return nil, err
	}
	var cursor int64
	if err := tx.QueryRowContext(ctx, `SELECT COALESCE(MAX(id),0) FROM board_candidate.operations_history`).Scan(&cursor); err != nil {
		return nil, err
	}
	result := row{
		"office_id": accountsOfficeID, "date": query.day,
		"page": page, "page_size": accountsPageSize, "total": total, "pages": pages,
		"published_total": publishedTotal, "computer_total": computerTotal,
		"accounts": accounts, "matching_ids": matching, "record_versions": versions, "cursor": cursor,
		"user": actor, "statuses": boardStatuses, "today": today,
		"workspace_view": "accounts_page",
	}
	for key, value := range bootstrap {
		result[key] = value
	}
	return result, nil
}

func accountPageRecords(ctx context.Context, tx *sql.Tx, ids []string, day string) ([]row, row, error) {
	accounts := make([]row, 0, len(ids))
	versions := row{}
	if len(ids) == 0 {
		return accounts, versions, nil
	}
	// The IDs were scoped and ordered in this same repeatable-read transaction.
	items, err := selectRows(ctx, tx, `SELECT jsonb_build_object('account',to_jsonb(a),'daily',to_jsonb(d))
		FROM unnest($1::text[]) WITH ORDINALITY p(id,position)
		JOIN board_candidate.accounts a ON a.id=p.id
		LEFT JOIN board_candidate.daily d ON d.account_id=a.id AND d.day=$2
		ORDER BY p.position`, ids, day)
	if err != nil {
		return nil, nil, err
	}
	for _, item := range items {
		account := item["account"].(row)
		id := str(account["id"])
		if err := addOfficeVersion(account, "account:"+id, versions); err != nil {
			return nil, nil, err
		}
		key := "daily:" + id + ":" + day
		account["published"], versions[key] = 0, nil
		if daily, ok := item["daily"].(row); ok {
			account["published"] = daily["published"]
			if err := addOfficeVersion(daily, key, versions); err != nil {
				return nil, nil, err
			}
		}
		accounts = append(accounts, account)
	}
	return accounts, versions, nil
}

func enrichPagedLifecycle(ctx context.Context, tx *sql.Tx, actor session.User, accounts []row, today string) error {
	if len(accounts) == 0 {
		return nil
	}
	relations, err := selectRows(ctx, tx, `SELECT jsonb_build_object('id',a.id,'name',a.name,'parent_account_id',a.parent_account_id)
		FROM board_candidate.accounts a LEFT JOIN board_candidate.account_offices o ON o.account_id=a.id
		WHERE a.id<>ALL($1::text[]) AND (a.parent_account_id=ANY($1::text[]) OR a.id=ANY($2::text[]))
		AND ($3 OR o.office_id=ANY($4::text[]))
		ORDER BY `+integerOrder("a.computer_id")+`,a.slot,a.id`, accountFieldValues(accounts, "id"),
		accountFieldValues(accounts, "parent_account_id"), actor.Global(), actor.OfficeIDs)
	if err != nil {
		return err
	}
	enrichLifecycle(append(slices.Clone(accounts), relations...), today)
	return nil
}

func accountsBootstrap(ctx context.Context, tx *sql.Tx, actor session.User, accounts []row, today string, versions row) (row, error) {
	ids := accountFieldValues(accounts, "id")
	layout, err := selectRows(ctx, tx, `SELECT jsonb_build_object('enabled',true,'batch',s.batch,'cells',COALESCE((
		SELECT jsonb_agg(jsonb_build_object('account_id',p.account_id,'computer_id',p.computer_id,'slot',p.slot)
			ORDER BY CASE WHEN p.computer_id LIKE 'office3:%' THEN 1 ELSE 0 END,`+integerOrder("replace(p.computer_id,'office3:','')")+`,p.slot,p.computer_id)
		FROM board_candidate.publishing_layout p
		LEFT JOIN board_candidate.computer_offices o ON o.computer_id=p.computer_id
		WHERE p.batch=s.batch AND p.account_id=ANY($1::text[]) AND ($2 OR o.office_id=ANY($3::text[]))
	), '[]'::jsonb)) FROM board_candidate.publishing_layout_state s WHERE s.id=1`, ids, actor.Global(), actor.OfficeIDs)
	if err != nil {
		return nil, err
	}
	publishing := row{"enabled": false, "cells": []row{}}
	computerIDs := accountFieldValues(accounts, "computer_id")
	if len(layout) > 0 {
		publishing = layout[0]
		for _, cell := range publishing["cells"].([]any) {
			computerIDs = append(computerIDs, str(cell.(row)["computer_id"]))
		}
	}
	computers, err := selectRows(ctx, tx, `SELECT jsonb_build_object('id',c.id,'name',c.name,'office_ids',
		CASE WHEN o.office_id IS NULL THEN ARRAY[]::text[] ELSE ARRAY[o.office_id] END)
		FROM board_candidate.computers c LEFT JOIN board_candidate.computer_offices o ON o.computer_id=c.id
		WHERE o.office_id=$1 OR (c.id=ANY($2::text[]) AND ($3 OR o.office_id=ANY($4::text[])))
		ORDER BY `+integerOrder("CASE WHEN c.id LIKE 'office3:%' THEN substr(c.id,9) ELSE c.id END")+`,c.id`,
		accountsOfficeID, computerIDs, actor.Global(), actor.OfficeIDs)
	if err != nil {
		return nil, err
	}
	if slices.Contains(computerIDs, "unassigned") && byID(computers)["unassigned"] == nil {
		computers = append(computers, row{"id": "unassigned", "name": "未分配电脑", "office_ids": []string{}})
	}
	devices, err := selectRows(ctx, tx, `SELECT jsonb_build_object('id',p.id,'name',p.name,'office_ids',
		COALESCE(array_agg(m.office_id ORDER BY m.office_id) FILTER (WHERE m.office_id IS NOT NULL),ARRAY[]::text[]))
		FROM (SELECT device_id FROM board_candidate.phone_device_offices WHERE office_id=$1 UNION SELECT unnest($2::text[])) relevant
		JOIN board_candidate.phone_devices p ON p.id=relevant.device_id
		LEFT JOIN board_candidate.phone_device_offices m ON m.device_id=p.id
		GROUP BY p.id
		HAVING bool_or(m.office_id=$1) OR $3 OR bool_or(m.office_id=ANY($4::text[]))
		ORDER BY `+integerOrder(`regexp_replace(p.id,'^.*:','')`)+`,p.id`,
		accountsOfficeID, accountFieldValues(accounts, "phone_device_id"), actor.Global(), actor.OfficeIDs)
	if err != nil {
		return nil, err
	}
	deviceByID := byID(devices)
	for _, account := range accounts {
		account["phone_device_name"] = str(deviceByID[str(account["phone_device_id"])]["name"])
	}
	offices, err := selectRows(ctx, tx, `SELECT jsonb_build_object('id',o.id,'name',o.name,'active',o.active)
		FROM board_candidate.offices o WHERE $1 OR o.id=ANY($2::text[]) ORDER BY o.name,o.id`, actor.Global(), actor.OfficeIDs)
	if err != nil {
		return nil, err
	}
	verification, err := selectRows(ctx, tx, `SELECT to_jsonb(n) FROM board_candidate.nurture_records n WHERE n.account_id=ANY($1::text[])`, ids)
	if err != nil {
		return nil, err
	}
	for _, id := range ids {
		versions["nurture:"+id] = nil
	}
	for _, record := range verification {
		if err := addOfficeVersion(record, "nurture:"+str(record["account_id"]), versions); err != nil {
			return nil, err
		}
	}
	verification = pickRows(verification, "account_id", "verification_state", "verification_owner", "verification_note", "verification_updated_on", "completed_on")
	return row{
		"computers": computers, "offices": offices, "phone_devices": row{"devices": devices},
		"phone_cards": row{"cards": []row{}}, "publishing_layout": publishing,
		"nurture":         row{"records": verification, "today": today, "verification_states": verificationStates},
		"device_workflow": row{"enabled": false, "units": []row{}, "access": []row{}, "todos": []row{}},
		"collection":      nil, "backup": row{"enabled": false, "last_backup": "", "error": "", "directory": ""},
	}, nil
}

func accountFieldValues(rows []row, field string) []string {
	values := make([]string, 0, len(rows))
	for _, item := range rows {
		if value := str(item[field]); value != "" {
			values = append(values, value)
		}
	}
	return values
}

func addOfficeVersion(item row, key string, versions row) error {
	item["office_ids"] = []string{accountsOfficeID}
	version, err := recordwire.Version(item)
	if err != nil {
		return err
	}
	versions[key] = version
	return nil
}

func visibleAccountReferences(ctx context.Context, tx *sql.Tx, accounts []row, officeIDs []string) (map[string]map[string]bool, error) {
	ids := map[string][]string{"accounts": {}, "phone_devices": {}, "computers": {}}
	seen := map[string]map[string]bool{"accounts": {}, "phone_devices": {}, "computers": {}}
	add := func(kind, id string) {
		if id != "" && !seen[kind][id] {
			seen[kind][id] = true
			ids[kind] = append(ids[kind], id)
		}
	}
	for _, account := range accounts {
		add("accounts", str(account["parent_account_id"]))
		add("phone_devices", str(account["phone_device_id"]))
		add("computers", str(account["computer_id"]))
	}
	rows, err := tx.QueryContext(ctx, `
		SELECT 'accounts',account_id FROM board_candidate.account_offices WHERE account_id=ANY($1::text[]) AND office_id=ANY($4::text[])
		UNION ALL SELECT 'phone_devices',device_id FROM board_candidate.phone_device_offices WHERE device_id=ANY($2::text[]) AND office_id=ANY($4::text[])
		UNION ALL SELECT 'computers',computer_id FROM board_candidate.computer_offices WHERE computer_id=ANY($3::text[]) AND office_id=ANY($4::text[])`,
		ids["accounts"], ids["phone_devices"], ids["computers"], officeIDs)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	visible := map[string]map[string]bool{"accounts": {}, "phone_devices": {}, "computers": {}}
	for rows.Next() {
		var kind, id string
		if err := rows.Scan(&kind, &id); err != nil {
			return nil, err
		}
		visible[kind][id] = true
	}
	return visible, rows.Err()
}

func accountMatches(ctx context.Context, tx *sql.Tx, statement string, args ...any) ([]string, int, int, error) {
	rows, err := tx.QueryContext(ctx, statement, args...)
	if err != nil {
		return nil, 0, 0, err
	}
	defer rows.Close()
	result := make([]string, 0)
	computers := make(map[string]struct{})
	publishedTotal := 0
	for rows.Next() {
		var id string
		var published int
		var computer sql.NullString
		if err := rows.Scan(&id, &published, &computer); err != nil {
			return nil, 0, 0, err
		}
		result = append(result, id)
		publishedTotal += published
		if computer.Valid && computer.String != "" && computer.String != "unassigned" {
			computers[computer.String] = struct{}{}
		}
	}
	return result, publishedTotal, len(computers), rows.Err()
}
