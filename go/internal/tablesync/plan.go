package tablesync

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"maps"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/exchange"
	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

// Keep only the reviewed account fields and structured source columns.
var accountFields = []string{"name", "registration_phone", "registered_on", "status", "computer_id", "slot"}
var sourceColumns = []struct{ field, heading string }{
	{"responsible", "负责人"}, {"usage", "使用状态"}, {"violation", "违规备注"}, {"business_type", "来源类型"}, {"storefront", "门头"},
}
var sourceStates = map[string]string{"正常": "正常", "未使用": "停用", "停用": "停用", "账号永久封禁": "封禁", "封禁": "封禁", "账号短期封禁": "短期封禁", "短期封禁": "短期封禁", "养号7天": "养号7天", "发不了": "发不了", "待确认": "待确认"}
var handlePattern = regexp.MustCompile(`^[A-Za-z0-9_.-]{2,100}$`)
var labelledHandle = regexp.MustCompile(`^(?:抖音号|douyin_id)\s*[:：]\s*([A-Za-z0-9_.-]{2,100})$`)
var phonePattern = regexp.MustCompile(`^1[0-9]{10}$`)
var computerPattern = regexp.MustCompile(`^([1-9][0-9]{0,5})(?:号(?:电脑|设备))?$`)
var excelDate = regexp.MustCompile(`^[0-9]{1,6}(?:\.0+)?$`)
var chineseDate = regexp.MustCompile(`^([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})日$`)

type changePlan struct {
	RowKey        string `json:"row_key"`
	AccountID     string `json:"account_id"`
	Office        string `json:"office_id"`
	Handle        string `json:"douyin_id"`
	SourceRows    []item `json:"source_rows"`
	Action        string `json:"action"`
	BeforeAccount item   `json:"before_account"`
	AfterAccount  item   `json:"after_account"`
	BeforeSource  item   `json:"before_source"`
	AfterSource   item   `json:"after_source"`
}

type preview struct {
	Office        string       `json:"office_id"`
	Mode          string       `json:"mode"`
	Source        item         `json:"source"`
	Rows          []item       `json:"rows"`
	Plans         []changePlan `json:"plans"`
	Summary       item         `json:"summary"`
	Guard         string       `json:"guard"`
	Day           string       `json:"day"`
	Generation    string       `json:"generation"`
	PilotRevision int64        `json:"pilot_revision"`
	Reverses      *string      `json:"reverses_batch_id"`
	Expires       int64        `json:"expires_at"`
}

func makePreview(ctx context.Context, tx *sql.Tx, actor session.User, office, generation, day string, body item) (item, error) {
	if err := onlyFields(body, "mode", "source_round", "accounts_file", "progress_file"); err != nil {
		return nil, err
	}
	revision, err := enabled(ctx, tx, office)
	if err != nil {
		return nil, err
	}
	mode := text(body["mode"])
	if mode != "add" && mode != "update" {
		return nil, invalid("请选择安全新增或更新已有账号")
	}
	accounts, progress, metadata, err := exchange.ReadOfficeSources(body)
	if err != nil {
		return nil, invalid(err.Error())
	}
	before, err := readSnapshot(ctx, tx)
	if err != nil {
		return nil, err
	}
	plans, rows, err := planRows(ctx, tx, actor, office, mode, day, accounts, progress, metadata, before)
	if err != nil {
		return nil, err
	}
	return storePreview(ctx, tx, actor, before, preview{Office: office, Mode: mode, Day: day, Generation: generation,
		PilotRevision: revision, Source: metadata, Rows: rows, Plans: plans})
}

type sourceIndex struct {
	byNumber map[string]int
	byHandle map[string][]progressRef
	orphans  []item
}

type progressRef struct {
	row      any
	computer string
}

func indexProgress(rows []item, counts map[string]int) sourceIndex {
	index := sourceIndex{byNumber: map[string]int{}, byHandle: map[string][]progressRef{}}
	handleOrder := make([]string, 0)
	for _, row := range rows {
		number := computerNumber(text(row["电脑序号"]))
		if number != "" {
			index.byNumber[number]++
		}
		for slot := 1; slot <= 10; slot++ {
			column := "发布设备" + strconv.Itoa(slot)
			label := text(row[column])
			if label == "" || label == "." || label == "。" || label == "-" || label == "—" {
				continue
			}
			if match := labelledHandle.FindStringSubmatch(label); match != nil {
				if len(index.byHandle[match[1]]) == 0 {
					handleOrder = append(handleOrder, match[1])
				}
				index.byHandle[match[1]] = append(index.byHandle[match[1]], progressRef{row["来源行"], number})
				continue
			}
			item := pendingRow(fmt.Sprintf("progress:%v:%d", row["来源行"], slot), "", "", []item{{"sheet": "progress", "row": row["来源行"], "column": column}})
			item["action"] = "skip"
			item["issues"] = []map[string]any{issue("progress_identity_unverified", "工作进度表此位置没有明确真实抖音号，只作来源核对，不按昵称或手机编号匹配")}
			index.orphans = append(index.orphans, item)
		}
	}
	for _, handle := range handleOrder {
		if counts[handle] != 0 {
			continue
		}
		refs := make([]item, 0)
		for _, ref := range index.byHandle[handle] {
			refs = append(refs, item{"sheet": "progress", "row": ref.row})
		}
		entry := pendingRow("progress-unmatched:"+strconv.Itoa(len(index.orphans)), handle, "", refs)
		entry["issues"] = []item{issue("progress_unmatched", "账号情况表没有此真实抖音号，双表来源不完整；不新增或猜测匹配")}
		index.orphans = append(index.orphans, entry)
	}
	return index
}

func planRows(ctx context.Context, tx *sql.Tx, actor session.User, office, mode, day string, accounts, progress []item, metadata item, before snapshot) ([]changePlan, []item, error) {
	counts := make(map[string]int)
	for _, row := range accounts {
		counts[text(row["抖音号"])]++
	}
	index := indexProgress(progress, counts)
	byHandle := make(map[string][]item)
	occupied := make(map[position]bool)
	for _, value := range before.Accounts {
		handle := text(value["douyin_id"])
		byHandle[handle] = append(byHandle[handle], value)
		if slot, ok := intValue(value["slot"]); ok {
			occupied[position{text(value["computer_id"]), slot}] = true
		}
	}
	plans, rows := make([]changePlan, 0), make([]item, 0)
	for number, source := range accounts {
		handle, name := text(source["抖音号"]), text(source["昵称"])
		if handle == "" && name == "" {
			continue
		}
		matched := index.byHandle[handle]
		refs := []item{{"sheet": "accounts", "row": source["来源行"]}}
		for _, ref := range matched {
			refs = append(refs, item{"sheet": "progress", "row": ref.row})
		}
		key := fmt.Sprintf("account:%d:%v", number, source["来源行"])
		entry := pendingRow(key, handle, name, refs)
		plan, err := propose(ctx, tx, actor, office, mode, day, key, handle, source, refs, matched, byHandle[handle], counts[handle], index.byNumber, occupied, metadata, before, entry)
		if err != nil {
			reason, known := safeIssue(err)
			if !known {
				return nil, nil, err
			}
			entry["reason"], entry["issues"] = reason, []item{issue("account_conflict", reason)}
		} else if plan != nil {
			changes := planChanges(*plan)
			entry["account_id"], entry["changes"] = plan.AccountID, changes
			entry["unchanged_fields"] = unchangedFields(source, *plan)
			entry["reason"] = "仅修改预览的允许字段；不会移动实际发布位置"
			if len(changes) == 0 {
				entry["action"] = "same"
			} else {
				entry["action"], entry["selected"], entry["blocked"] = plan.Action, true, false
				plans = append(plans, *plan)
				if old, ok := intValue(plan.BeforeAccount["slot"]); ok && !same(plan.BeforeAccount["computer_id"], plan.AfterAccount["computer_id"]) {
					delete(occupied, position{text(plan.BeforeAccount["computer_id"]), old})
				}
				if slot, ok := intValue(plan.AfterAccount["slot"]); ok {
					occupied[position{text(plan.AfterAccount["computer_id"]), slot}] = true
				}
			}
		}
		rows = append(rows, entry)
	}
	return plans, append(rows, index.orphans...), nil
}

func propose(ctx context.Context, tx *sql.Tx, actor session.User, office, mode, day, key, handle string, source item, refs []item,
	matched []progressRef, found []item, count int, progress map[string]int, occupied map[position]bool, metadata item, before snapshot, entry item) (*changePlan, error) {
	if !handlePattern.MatchString(handle) {
		return nil, invalid("真实抖音号为空、含空白或格式不明确；不归一化或猜测匹配")
	}
	if count != 1 || len(found) > 1 || len(matched) > 1 {
		return nil, invalid("任一来源或系统中存在重复真实抖音号，整名账号保留待核对")
	}
	var old item
	if len(found) == 1 {
		old = found[0]
		if before.Offices[text(old["id"])] != office {
			return nil, invalid("同抖音号属于其他办公室、待分配区或归属不明确；不复制、不移动")
		}
		entry["account_id"], entry["current_name"] = old["id"], old["name"]
	}
	if mode == "add" && old != nil {
		entry["action"], entry["reason"] = "skip", "已有账号由更新模式处理"
		return nil, nil
	}
	if mode == "update" && old == nil {
		entry["action"], entry["reason"] = "skip", "系统无此账号；更新模式不会新增"
		return nil, nil
	}
	computer := text(source["登记电脑序号"])
	if len(matched) == 1 && computer != "" && computerNumber(computer) != matched[0].computer {
		return nil, invalid("双表对同一真实抖音号的登记电脑存在矛盾；不修改发布安排")
	}
	after, action := maps.Clone(old), "update"
	if old == nil {
		after, action = item{"id": "account-" + nextID(false), "computer_id": "", "slot": nil, "name": "昵称待补充", "status": "待确认", "note": "",
			"douyin_id": handle, "homepage": "", "last_collected": "", "registered_on": "", "registration_phone": ""}, "create"
	}
	if name := text(source["昵称"]); name != "" {
		if utf8.RuneCountInString(name) > 100 {
			return nil, invalid("昵称超过100字")
		}
		after["name"] = name
	}
	if phone := text(source["注册手机号"]); phone != "" {
		if !phonePattern.MatchString(phone) {
			return nil, invalid("注册手机号不完整，整名账号待核对")
		}
		after["registration_phone"] = phone
	}
	if registered := text(source["注册日期"]); registered != "" {
		value, err := registrationDay(registered, day)
		if err != nil {
			return nil, err
		}
		after["registered_on"] = value
	}
	if state := text(source["来源账号状态"]); state != "" {
		mapped := sourceStates[state]
		if mapped == "" {
			return nil, invalid("来源状态未有明确映射，保留系统原值并列入待核对")
		}
		if old != nil && mapped == "养号7天" && old["status"] != mapped {
			return nil, invalid("此状态更新会改变养号流程，需单独人工处理")
		}
		after["status"] = mapped
	}
	if computer != "" || old == nil {
		id, slot, err := chooseComputer(ctx, tx, office, computer, progress, occupied, old)
		if err != nil {
			return nil, err
		}
		after["computer_id"], after["slot"] = id, slot
	}
	id := text(after["id"])
	oldSource := before.Sources[id]
	newSource, err := updateSource(oldSource, id, office, source, refs, metadata)
	if err != nil {
		return nil, err
	}
	plan := &changePlan{key, id, office, handle, refs, action, old, after, oldSource, newSource}
	if err := validatePlan(ctx, tx, actor, day, plan); err != nil {
		return nil, err
	}
	return plan, nil
}

type position struct {
	computer string
	slot     int64
}

func chooseComputer(ctx context.Context, tx *sql.Tx, office, raw string, progress map[string]int, occupied map[position]bool, old item) (string, any, error) {
	number := computerNumber(raw)
	if number == "" || progress[number] != 1 {
		return "", nil, invalid("资料登记电脑需在双表中明确且唯一，不能猜测编号")
	}
	id := "office3:" + number
	var found bool
	if err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM board_candidate.computers c JOIN board_candidate.computer_offices o ON o.computer_id=c.id WHERE c.id=$1 AND o.office_id=$2)`, id, office).Scan(&found); err != nil {
		return "", nil, err
	}
	if !found {
		return "", nil, invalid("登记电脑尚不存在或并非仅属于办公室三；不会创建或移动设备")
	}
	if old != nil && old["computer_id"] == id {
		return id, old["slot"], nil
	}
	for slot := int64(1); slot <= 100; slot++ {
		if !occupied[position{id, slot}] {
			return id, slot, nil
		}
	}
	return "", nil, invalid("登记电脑没有可用资料位置")
}

func computerNumber(raw string) string {
	clean := strings.Map(func(r rune) rune {
		if unicode.IsSpace(r) {
			return -1
		}
		return r
	}, raw)
	if match := computerPattern.FindStringSubmatch(clean); match != nil {
		return match[1]
	}
	return ""
}

func registrationDay(value, day string) (string, error) {
	var found time.Time
	if excelDate.MatchString(value) {
		integer, _, _ := strings.Cut(value, ".")
		count, _ := strconv.Atoi(integer)
		found = time.Date(1899, 12, 30, 0, 0, 0, 0, time.UTC).AddDate(0, 0, count)
	} else {
		if match := chineseDate.FindStringSubmatch(value); match != nil {
			month, _ := strconv.Atoi(match[2])
			date, _ := strconv.Atoi(match[3])
			value = fmt.Sprintf("%s-%02d-%02d", match[1], month, date)
		}
		var err error
		found, err = time.Parse(time.DateOnly, value)
		if err != nil || found.Year() < 1 {
			return "", invalid("注册日期格式不正确，整名账号待核对")
		}
	}
	if found.Format(time.DateOnly) > day {
		return "", invalid("注册日期晚于今天")
	}
	return found.Format(time.DateOnly), nil
}

func updateSource(old item, id, office string, source item, refs []item, metadata item) (item, error) {
	if old != nil && (old["office_id"] != office || old["account_id"] != id) {
		return nil, invalid("账号来源记录归属不一致，保留待核对")
	}
	result := maps.Clone(old)
	if result == nil {
		result = item{"account_id": id, "office_id": office, "revision": int64(1), "source_data": "{}"}
		for _, column := range sourceColumns {
			result[column.field] = ""
		}
	}
	for _, column := range sourceColumns {
		if value := text(source[column.heading]); value != "" {
			limit := 1000
			if column.field == "responsible" || column.field == "business_type" {
				limit = 100
			}
			if utf8.RuneCountInString(value) > limit {
				return nil, invalid("结构化来源字段过长")
			}
			result[column.field] = value
		}
	}
	data, err := decodeObject([]byte(text(result["source_data"])))
	if err != nil {
		return nil, err
	}
	for key, column := range map[string]string{"source_phone_number": "登录手机序号", "auxiliary_note": "小号说明", "original_status": "来源账号状态"} {
		if value := text(source[column]); value != "" {
			data[key] = value
		}
	}
	data["source_round"], data["files"], data["rows"] = metadata["round"], item{"accounts": metadata["accounts"], "progress": metadata["progress"]}, refs
	encoded, err := recordwire.Encode(data)
	if err != nil {
		return nil, err
	}
	result["source_data"] = string(encoded)
	if old != nil && !same(old, result) {
		revision, _ := intValue(old["revision"])
		result["revision"] = revision + 1
	}
	return result, nil
}

func pendingRow(key, handle, name string, refs []item) item {
	return item{"row_key": key, "douyin_id": handle, "source_rows": refs, "account_id": "", "current_name": "", "source_name": name,
		"action": "conflict", "selected": false, "blocked": true, "changes": []item{}, "issues": []item{}, "reason": ""}
}

func issue(code, message string) item { return item{"code": code, "message": message} }

func safeIssue(err error) (string, bool) {
	var read readError
	var api accountwrite.APIError
	var sqlState interface{ SQLState() string }
	switch {
	case errors.As(err, &read) && read.status < 500:
		return read.message, true
	case errors.As(err, &api) && api.Status < 500:
		return api.Message, true
	case errors.As(err, &sqlState) && strings.HasPrefix(sqlState.SQLState(), "23"):
		return "本行不能满足现有资料约束，整名账号保留待核对", true
	default:
		return "", false
	}
}

func planChanges(plan changePlan) []item {
	result := make([]item, 0)
	for _, field := range accountFields {
		if old, next := plan.BeforeAccount[field], plan.AfterAccount[field]; !same(old, next) {
			result = append(result, item{"field": field, "before": old, "after": next})
		}
	}
	for _, field := range sourceColumns {
		if old, next := plan.BeforeSource[field.field], plan.AfterSource[field.field]; !same(old, next) {
			result = append(result, item{"field": "source." + field.field, "before": old, "after": next})
		}
	}
	old, _ := decodeObject([]byte(text(plan.BeforeSource["source_data"])))
	next, _ := decodeObject([]byte(text(plan.AfterSource["source_data"])))
	if !same(old, next) {
		result = append(result, item{"field": "source.source_data", "before": old, "after": next})
	}
	return result
}

func unchangedFields(source item, plan changePlan) int {
	count := 0
	for _, pair := range []struct{ field, heading string }{{"name", "昵称"}, {"registration_phone", "注册手机号"}, {"registered_on", "注册日期"},
		{"status", "来源账号状态"}, {"computer_id", "登记电脑序号"}, {"slot", "登记电脑序号"}} {
		if plan.BeforeAccount != nil && text(source[pair.heading]) != "" && same(plan.BeforeAccount[pair.field], plan.AfterAccount[pair.field]) {
			count++
		}
	}
	for _, pair := range sourceColumns {
		if text(source[pair.heading]) != "" && same(plan.BeforeSource[pair.field], plan.AfterSource[pair.field]) {
			count++
		}
	}
	if same(plan.BeforeSource["source_data"], plan.AfterSource["source_data"]) {
		count++
	}
	return count
}
