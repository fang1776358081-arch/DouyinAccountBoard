package inventory

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"net/http"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/session"
)

const (
	CardPath         = "/api/phone-card"
	ImportCardsPath  = "/api/phone-cards/import"
	DevicePath       = "/api/phone-device"
	CreateDevicePath = "/api/phone-device/create"
)

// Handler routes inventory writes through the shared same-origin, authenticated,
// admitted and idempotent write transaction.
func Handler(write accountwrite.Service, path string) http.HandlerFunc {
	switch path {
	case CardPath:
		return write.Handle(path, "保存手机卡必须带请求标识", AuthorizeCard, SaveCard)
	case ImportCardsPath:
		return write.Handle(path, "补录手机卡必须带请求标识", authorizeImportCards, importCards)
	case DevicePath, CreateDevicePath:
		create := path == CreateDevicePath
		message := "修改手机设备必须带请求标识"
		if create {
			message = "新增手机设备必须带请求标识"
		}
		return write.Handle(path, message, func(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) error {
			return AuthorizeDevice(ctx, tx, actor, body, create)
		}, func(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
			return SaveDevice(ctx, tx, actor, body, create)
		})
	default:
		return http.NotFound
	}
}

var cardTaskFields = []string{"status", "code_state", "code_note", "return_state", "return_due", "returned_on"}

func valueString(value any) string {
	switch value := value.(type) {
	case nil:
		return ""
	case string:
		return value
	case json.Number:
		return value.String()
	case int64:
		return strconv.FormatInt(value, 10)
	case int:
		return strconv.Itoa(value)
	default:
		return ""
	}
}

func boundedText(body map[string]any, key, fallback string, limit int, message string) (string, error) {
	value := fallback
	if raw, exists := body[key]; exists {
		if raw != nil && valueString(raw) == "" && raw != "" {
			return "", accountwrite.Invalid(message)
		}
		value = strings.TrimSpace(valueString(raw))
	}
	if utf8.RuneCountInString(value) > limit || strings.IndexByte(value, 0) >= 0 {
		return "", accountwrite.Invalid(message)
	}
	return value, nil
}

func enumField(body map[string]any, key, fallback string, options []string, message string) (string, error) {
	value := fallback
	if raw, exists := body[key]; exists {
		value, _ = raw.(string)
	}
	if !slices.Contains(options, value) {
		return "", accountwrite.Invalid(message)
	}
	return value, nil
}

func parseCardID(value any) (int64, bool) {
	raw := valueString(value)
	id, err := strconv.ParseInt(raw, 10, 64)
	return id, err == nil && strconv.FormatInt(id, 10) == raw
}

func carrier(body map[string]any, fallback string) (string, error) {
	value, custom := fallback, ""
	if raw, exists := body["carrier"]; exists {
		var ok bool
		if value, ok = raw.(string); !ok {
			return "", accountwrite.Invalid("运营商名称格式不正确")
		}
	}
	if raw, exists := body["custom_carrier"]; exists {
		var ok bool
		if custom, ok = raw.(string); !ok {
			return "", accountwrite.Invalid("运营商名称格式不正确")
		}
	}
	value, custom = strings.TrimSpace(value), strings.TrimSpace(custom)
	if custom != "" {
		if value != "其他" {
			return "", accountwrite.Invalid("自定义运营商请选择“其他”")
		}
		value = custom
	}
	if n := utf8.RuneCountInString(value); n == 0 || n > 40 || strings.ContainsFunc(value, func(character rune) bool { return character < 32 }) {
		return "", accountwrite.Invalid("运营商名称需为1至40字，不能包含换行")
	}
	return value, nil
}

// AuthorizeCard checks both scope and the distinction between card management
// and card task permissions.
func AuthorizeCard(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) error {
	if _, inline := body["changes"]; inline {
		_, _, _, _, err := authorizeCardInline(ctx, tx, actor, body)
		return err
	}
	_, _, err := authorizeCard(ctx, tx, actor, body)
	return err
}

func authorizeCardInline(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, int64, string, string, error) {
	changes, ok := body["changes"].(map[string]any)
	if !ok || len(changes) != 1 || body["id"] == nil {
		return nil, 0, "", "", accountwrite.Invalid("请选择一个手机卡字段修改")
	}
	for key := range body {
		switch key {
		case "id", "changes", "_expected_versions", "request_id":
		default:
			return nil, 0, "", "", accountwrite.Invalid("手机卡单元格修改包含多余字段")
		}
	}
	for field, raw := range changes {
		value, ok := raw.(string)
		if !ok {
			return nil, 0, "", "", accountwrite.Invalid("手机卡字段值不正确")
		}
		value = strings.TrimSpace(value)
		switch field {
		case "owner":
			if !actor.Has("card_manage") {
				return nil, 0, "", "", accountwrite.Forbidden("修改卡主需要手机卡管理权限")
			}
			if utf8.RuneCountInString(value) > 100 {
				return nil, 0, "", "", accountwrite.Invalid("卡主不能超过100字")
			}
		case "status":
			if !actor.Has("card_tasks") {
				return nil, 0, "", "", accountwrite.Forbidden("修改卡状态需要手机卡待办权限")
			}
			if !slices.Contains(cardStatuses, value) {
				return nil, 0, "", "", accountwrite.Invalid("手机卡状态不正确")
			}
		default:
			return nil, 0, "", "", accountwrite.Invalid("此手机卡字段请在详情中修改")
		}
		old, id, err := authorizeCard(ctx, tx, actor, map[string]any{"id": body["id"], field: value})
		return old, id, field, value, err
	}
	return nil, 0, "", "", accountwrite.Invalid("请选择一个手机卡字段修改")
}

func saveCardInline(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	old, id, field, value, err := authorizeCardInline(ctx, tx, actor, body)
	if err != nil {
		return nil, err
	}
	target := strconv.FormatInt(id, 10)
	key := "card:" + target
	before := snapshot{key: old}
	if err := before.check(body); err != nil {
		return nil, err
	}
	// Select a fixed statement so legacy values in unrelated fields are preserved.
	update := `UPDATE board_candidate.phone_cards SET owner=$1 WHERE id=$2`
	if field == "status" {
		update = `UPDATE board_candidate.phone_cards SET status=$1 WHERE id=$2`
	}
	if old[field] != value {
		if _, err := tx.ExecContext(ctx, update, value, id); err != nil {
			return nil, err
		}
	}
	next, err := CardRecord(ctx, tx, id)
	if err != nil {
		return nil, err
	}
	versions, err := before.finish(ctx, tx, actor, CardPath, target, snapshot{key: next})
	return map[string]any{"ok": true, "id": id, "_record_versions": versions}, err
}

func authorizeCard(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, int64, error) {
	if body["id"] == nil {
		if _, err := requestedOffices(ctx, tx, actor, body); err != nil {
			return nil, 0, err
		}
		if _, present := body["custom_carrier"]; present {
			if _, err := carrier(body, "待确认"); err != nil {
				return nil, 0, err
			}
		}
		return nil, 0, requirePermission(actor, "card_create")
	}
	id, ok := parseCardID(body["id"])
	if !ok {
		return nil, 0, accountwrite.Forbidden("无权访问此资料")
	}
	old, err := CardRecord(ctx, tx, id)
	if err != nil {
		return nil, 0, err
	}
	if err := requireEdit(actor, old); err != nil {
		return nil, 0, err
	}
	if _, custom := body["custom_carrier"]; custom {
		value, err := carrier(body, stringField(old, "carrier"))
		if err != nil {
			return nil, 0, err
		}
		body = maps.Clone(body)
		body["carrier"] = value
		delete(body, "custom_carrier")
	}
	if actor.Has("card_manage") {
		if !actor.Has("card_tasks") {
			for _, key := range cardTaskFields {
				if value, exists := body[key]; exists && !reflect.DeepEqual(old[key], value) {
					return nil, 0, accountwrite.Forbidden("修改卡状态、接码和归还需要手机卡待办权限")
				}
			}
		}
		return old, id, nil
	}
	if err := requirePermission(actor, "card_tasks"); err != nil {
		return nil, 0, err
	}
	for key, value := range body {
		switch key {
		case "id", "_expected_versions", "request_id":
			continue
		}
		if key == "office_ids" {
			return nil, 0, accountwrite.Forbidden("只能修改卡状态、接码及归还事项，不能修改基础资料")
		}
		if !slices.Contains(cardTaskFields, key) && !reflect.DeepEqual(old[key], value) {
			return nil, 0, accountwrite.Forbidden("只能修改卡状态、接码及归还事项，不能修改基础资料")
		}
	}
	return old, id, nil
}

type textRule struct {
	key   string
	limit int
}

func cardValues(body, old map[string]any) (map[string]string, error) {
	phone := strings.TrimSpace(valueString(body["phone"]))
	if !validPhone(phone) {
		return nil, accountwrite.Invalid("请填写11位手机号码")
	}
	result := map[string]string{"phone": phone}
	for _, rule := range []textRule{{"label", 100}, {"owner", 100}, {"location", 200}, {"note", 2000}} {
		value, err := boundedText(body, rule.key, "", rule.limit, "手机卡字段内容过长，请缩短后保存")
		if err != nil {
			return nil, err
		}
		result[rule.key] = value
	}
	fallbackCarrier := "待确认"
	if old != nil {
		fallbackCarrier = stringField(old, "carrier")
	}
	var err error
	if result["carrier"], err = carrier(body, fallbackCarrier); err != nil {
		return nil, err
	}
	if result["status"], err = enumField(body, "status", "待确认", cardStatuses, "运营商或手机卡状态不正确"); err != nil {
		return nil, err
	}
	for _, rule := range []textRule{{"keeper", 100}, {"code_contact", 200}, {"code_note", 1000}} {
		value, err := boundedText(body, rule.key, stringField(old, rule.key), rule.limit, "接码或负责人信息过长，请缩短后保存")
		if err != nil {
			return nil, err
		}
		result[rule.key] = value
	}
	for key, options := range map[string][]string{"ownership": ownerships, "code_state": codeStates, "return_state": returnStates} {
		fallback := options[0]
		if old != nil {
			fallback = stringField(old, key)
		}
		value, err := enumField(body, key, fallback, options, "归属、接码或归还状态不正确")
		if err != nil {
			return nil, err
		}
		result[key] = value
	}
	for _, key := range []string{"expires_on", "return_due", "returned_on"} {
		fallback := ""
		if key != "expires_on" {
			fallback = stringField(old, key)
		}
		message := "归还日期不正确"
		if key == "expires_on" {
			message = "到期日期不正确"
		}
		value, err := boundedText(body, key, fallback, 10, message)
		if err != nil || !validOptionalDate(value) {
			return nil, accountwrite.Invalid(message)
		}
		result[key] = value
	}
	if result["return_state"] == "已归还" && result["returned_on"] == "" {
		return nil, accountwrite.Invalid("标记已归还时请填写实际归还日期")
	}
	return result, nil
}

func validOptionalDate(value string) bool {
	if value == "" {
		return true
	}
	layout := time.DateOnly
	if len(value) == 8 {
		layout = "20060102"
	}
	date, err := time.Parse(layout, value)
	return err == nil && date.Year() > 0
}

// SaveCard saves a card in the caller's admitted transaction, writes audit and
// sync history, and returns the same version receipts as the HTTP route.
func SaveCard(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	if _, inline := body["changes"]; inline {
		return saveCardInline(ctx, tx, actor, body)
	}
	old, id, err := authorizeCard(ctx, tx, actor, body)
	if err != nil {
		return nil, err
	}
	before := snapshot{}
	if old != nil {
		if _, stringID := body["id"].(string); stringID {
			return nil, accountwrite.Invalid("手机卡记录不存在，请刷新后重试")
		}
		before["card:"+strconv.FormatInt(id, 10)] = old
		if !actor.Has("card_manage") {
			tasks := maps.Clone(old)
			delete(tasks, "office_ids")
			for _, key := range cardTaskFields {
				if value, exists := body[key]; exists {
					tasks[key] = value
				}
			}
			tasks["_expected_versions"] = body["_expected_versions"]
			body = tasks
		} else if !actor.Has("card_tasks") {
			body = maps.Clone(body)
			for _, key := range cardTaskFields {
				body[key] = old[key]
			}
		}
	}
	if err := before.check(body); err != nil {
		return nil, err
	}
	var offices []string
	_, changeScope := body["office_ids"]
	if old == nil || changeScope {
		offices, err = requestedOffices(ctx, tx, actor, body)
		if err != nil {
			return nil, err
		}
	}
	values, err := cardValues(body, old)
	if err != nil {
		return nil, err
	}
	var duplicate bool
	if err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM board_candidate.phone_cards WHERE phone=$1 AND ($2::bigint IS NULL OR id<>$2))`,
		values["phone"], nullableCardID(old, id)).Scan(&duplicate); err != nil {
		return nil, err
	}
	if duplicate {
		return nil, accountwrite.Invalid("这个手机号已经录入，请编辑已有手机卡")
	}
	args := []any{values["phone"], values["label"], values["carrier"], values["status"], values["owner"], values["location"],
		values["expires_on"], values["note"], values["keeper"], values["code_contact"], values["code_note"],
		values["ownership"], values["code_state"], values["return_state"], values["return_due"], values["returned_on"]}
	if old == nil {
		err = tx.QueryRowContext(ctx, `INSERT INTO board_candidate.phone_cards(
			phone,label,carrier,status,owner,location,expires_on,note,keeper,code_contact,code_note,ownership,code_state,return_state,return_due,returned_on)
			VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16) RETURNING id`, args...).Scan(&id)
	} else {
		_, err = tx.ExecContext(ctx, `UPDATE board_candidate.phone_cards
			SET phone=$1,label=$2,carrier=$3,status=$4,owner=$5,location=$6,expires_on=$7,note=$8,keeper=$9,
			    code_contact=$10,code_note=$11,ownership=$12,code_state=$13,return_state=$14,return_due=$15,returned_on=$16
			WHERE id=$17`, append(args, id)...)
	}
	if err != nil {
		return nil, err
	}
	if old == nil || changeScope {
		if err := saveOffices(ctx, tx, cardRelation, id, offices); err != nil {
			return nil, err
		}
	}
	next, err := CardRecord(ctx, tx, id)
	if err != nil {
		return nil, err
	}
	target := strconv.FormatInt(id, 10)
	versions, err := before.finish(ctx, tx, actor, CardPath, target, snapshot{"card:" + target: next})
	return map[string]any{"ok": true, "id": id, "_record_versions": versions}, err
}

func nullableCardID(old map[string]any, id int64) any {
	if old == nil {
		return nil
	}
	return id
}

func authorizeImportCards(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) error {
	if _, err := requestedOffices(ctx, tx, actor, body); err != nil {
		return err
	}
	return requirePermission(actor, "card_import")
}

func importCards(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	offices, err := requestedOffices(ctx, tx, actor, body)
	if err != nil {
		return nil, err
	}
	phones, err := accountPhones(ctx, tx, actor)
	if err != nil {
		return nil, err
	}
	after := snapshot{}
	for _, phone := range slices.Sorted(maps.Keys(phones)) {
		if !validPhone(phone) {
			continue
		}
		var id int64
		err := tx.QueryRowContext(ctx, `INSERT INTO board_candidate.phone_cards(phone) VALUES($1)
			ON CONFLICT(phone) DO NOTHING RETURNING id`, phone).Scan(&id)
		if errors.Is(err, sql.ErrNoRows) {
			continue
		}
		if err != nil {
			return nil, err
		}
		if err := saveOffices(ctx, tx, cardRelation, id, offices); err != nil {
			return nil, err
		}
		row, err := CardRecord(ctx, tx, id)
		if err != nil {
			return nil, err
		}
		after[fmt.Sprintf("card:%d", id)] = row
	}
	versions, err := (snapshot{}).finish(ctx, tx, actor, ImportCardsPath, "", after)
	return map[string]any{"ok": true, "added": len(after), "_record_versions": versions}, err
}
