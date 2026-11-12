package board

import (
	"context"
	"database/sql"
	"net/http"
	"slices"
	"time"

	"douyinboard/internal/inventory"
)

type banImpactResult struct {
	Accounts []row `json:"accounts"`
	Cards    []row `json:"cards"`
	Phones   []row `json:"phones"`
	Slots    []row `json:"slots"`
}

// BanImpact reads current relationships in the same office scope as the board.
// It does not write card, device, publishing, or history state.
func (h Handler) BanImpact(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()
	tx, actor, err := h.beginRead(ctx, r)
	if err != nil {
		readFailure(w, err)
		return
	}
	defer tx.Rollback()
	scope, err := loadScope(ctx, tx, actor)
	if err != nil {
		readFailure(w, err)
		return
	}
	result, err := readBanImpact(ctx, tx, scope)
	if err == nil {
		err = tx.Commit()
	}
	if err != nil {
		readFailure(w, err)
		return
	}
	WriteNegotiatedJSON(w, r, result)
}

func readBanImpact(ctx context.Context, tx *sql.Tx, scope *scopeMap) (banImpactResult, error) {
	result := banImpactResult{Accounts: []row{}, Cards: []row{}, Phones: []row{}, Slots: []row{}}
	computers, accounts, err := boardEntities(ctx, tx, scope, "")
	if err != nil {
		return result, err
	}
	for _, account := range accounts {
		if bannedAccount(account) {
			result.Accounts = append(result.Accounts, picked(account, "id", "name", "douyin_id", "status", "office_ids"))
		}
	}
	if len(result.Accounts) == 0 {
		return result, nil
	}
	cards, err := inventory.Cards(ctx, tx, scope.actor, accounts)
	if err != nil {
		return result, err
	}
	phoneLinks, err := banPhoneLinks(ctx, tx, scope)
	if err != nil {
		return result, err
	}
	layout, err := publishingLayout(ctx, tx, scope)
	if err != nil {
		return result, err
	}
	result.Cards = banCards(scope.actor.Global(), accounts, entityRows(cards, "cards"))
	result.Phones = banPhones(scope, accounts, phoneLinks)
	result.Slots = banSlots(accounts, computers, layout)
	addMissingBanLinks(&result)
	return result, nil
}

func bannedAccount(account row) bool {
	return account["status"] == "封禁" || account["status"] == "短期封禁"
}

func banCards(global bool, accounts, cards []row) []row {
	byPhone := make(map[string][]row)
	for _, account := range accounts {
		if phone := str(account["registration_phone"]); phone != "" {
			byPhone[phone] = append(byPhone[phone], account)
		}
	}
	result := []row{}
	for _, card := range cards {
		linked := byPhone[str(card["phone"])]
		banned, others := banAccountGroup(linked)
		if len(banned) == 0 {
			continue
		}
		state, reason := banCardReason(global, card, linked)
		item := picked(card, "id", "phone", "status", "office_ids")
		item["account_ids"], item["other_accounts"] = banned, others
		item["state"], item["reason"] = state, reason
		result = append(result, item)
	}
	return result
}

func banAccountGroup(accounts []row) ([]string, []row) {
	banned, others := []string{}, []row{}
	for _, account := range accounts {
		if bannedAccount(account) {
			banned = append(banned, str(account["id"]))
		} else {
			others = append(others, picked(account, "id", "name", "douyin_id", "status"))
		}
	}
	return banned, others
}

func banCardReason(global bool, card row, accounts []row) (string, string) {
	cardOffices, _ := card["office_ids"].([]string)
	var active, uncertain, mismatched bool
	for _, account := range accounts {
		switch account["status"] {
		case "正常", "养号7天":
			active = true
		case "封禁", "停用":
		default:
			uncertain = true
		}
		offices, _ := account["office_ids"].([]string)
		if !slices.ContainsFunc(offices, func(office string) bool { return slices.Contains(cardOffices, office) }) {
			mismatched = true
		}
	}
	switch {
	case card["status"] == "已注销" && active:
		return "review", "卡片已登记注销，仍有关联账号处于正常或养号状态，请核对注册卡记录。"
	case card["status"] == "已注销":
		return "cancelled", "此卡已登记注销，请核对历史关联。"
	case card["status"] == "停用" || card["status"] == "停机" || card["status"] == "欠费":
		return "review", "卡片已登记为" + str(card["status"]) + "，请核对当前卡片状态和关联账号后再处理。"
	case active:
		return "in_use", "仍有关联账号正常使用或养号，请保留此卡并核对清单。"
	case mismatched:
		return "review", "手机卡与关联账号的办公室归属不明确或不一致，请先核对关系。"
	case uncertain:
		return "review", "有关联账号处于短期封禁或使用情况待确认，请先核对。"
	case card["status"] == "待注销":
		return "cancel_pending", "此卡已列入待注销，请核对已有办理记录。"
	case !global:
		return "review", "可见范围内未找到仍在使用的账号，请核对完整关联后再决定是否注销。"
	default:
		return "cancel_review", "已登记的关联账号均已封禁或停用，请人工确认是否需要注销。"
	}
}

// Physical identity comes from the last recorded unit, including retired units.
// A reusable phone number or a recovery target cannot identify a login device.
func banPhoneLinks(ctx context.Context, tx *sql.Tx, scope *scopeMap) ([]row, error) {
	var available bool
	if err := tx.QueryRowContext(ctx, `SELECT to_regclass('board_candidate.phone_units') IS NOT NULL
		AND to_regclass('board_candidate.phone_account_access') IS NOT NULL`).Scan(&available); err != nil || !available {
		return nil, err
	}
	return selectRows(ctx, tx, `SELECT jsonb_build_object('id', u.id, 'device_id', u.device_id,
		'original_device_id', u.original_device_id, 'name', d.name, 'generation', u.generation,
		'active', u.active, 'status', u.status, 'account_id', x.account_id)
		FROM board_candidate.phone_account_access x
		JOIN board_candidate.phone_units u ON u.id=x.last_unit_id
		JOIN board_candidate.phone_devices d ON d.id=u.device_id
		WHERE $1 OR (
			EXISTS(SELECT 1 FROM board_candidate.account_offices o WHERE o.account_id=x.account_id AND o.office_id=ANY($2::text[]))
			AND EXISTS(SELECT 1 FROM board_candidate.phone_device_offices o WHERE o.device_id=u.device_id AND o.office_id=ANY($2::text[]))
			AND EXISTS(SELECT 1 FROM board_candidate.phone_device_offices o WHERE o.device_id=u.original_device_id AND o.office_id=ANY($2::text[]))
		) ORDER BY u.device_id,u.generation,x.account_id`, scope.actor.Global(), scope.actor.OfficeIDs)
}

func banPhones(scope *scopeMap, accounts, links []row) []row {
	lookup := byID(accounts)
	linked := make(map[string][]row)
	for _, link := range links {
		id := str(link["id"])
		if account := lookup[str(link["account_id"])]; account != nil {
			linked[id] = append(linked[id], account)
		}
	}
	result := []row{}
	for _, link := range links {
		id := str(link["id"])
		banned, others := banAccountGroup(linked[id])
		delete(linked, id)
		if len(banned) == 0 {
			continue
		}
		device := str(link["device_id"])
		label := str(link["name"]) + " · 第" + str(link["generation"]) + "台实物"
		if number(link["active"]) == 0 {
			label += "（旧机）"
		}
		reason := "该实物有封禁账号的登录记录，请人工核对设备和当前登录情况。"
		if len(others) > 0 {
			reason = "此实物还有其他账号的登录记录，请逐一核对设备和账号能否使用。"
		}
		result = append(result, row{"id": id, "device_id": device, "label": label, "status": link["status"],
			"office_ids": scope.offices("phone_devices", device), "account_ids": banned, "other_accounts": others,
			"state": "review", "reason": reason})
	}
	return result
}

func banSlots(accounts, computers []row, layout row) []row {
	accountByID, computerByID := byID(accounts), byID(computers)
	cells := entityRows(layout, "cells")
	if layout["enabled"] != true {
		// The classic matrix uses assigned computer and account slot when no
		// publishing layout is active. Once imported, only actual cells count.
		for _, account := range accounts {
			if bannedAccount(account) {
				cells = append(cells, row{"account_id": account["id"], "computer_id": account["computer_id"], "slot": account["slot"]})
			}
		}
	}
	result := []row{}
	for _, cell := range cells {
		computer := computerByID[str(cell["computer_id"])]
		account := accountByID[str(cell["account_id"])]
		if !bannedAccount(account) || computer == nil || computer["id"] == "unassigned" || number(cell["slot"]) < 1 {
			continue
		}
		item := picked(cell, "computer_id", "slot", "source_cell", "account_id")
		item["computer_name"], item["office_ids"] = computer["name"], computer["office_ids"]
		item["state"], item["reason"] = "exception", "此账号处于封禁状态，请核对这个发布位置是否需要调整。"
		result = append(result, item)
	}
	return result
}

func addMissingBanLinks(result *banImpactResult) {
	linked := make(map[string]map[string]bool)
	for kind, items := range map[string][]row{"card": result.Cards, "phone": result.Phones, "slot": result.Slots} {
		known := make(map[string]bool)
		for _, item := range items {
			if kind == "slot" {
				known[str(item["account_id"])] = true
				continue
			}
			for _, id := range item["account_ids"].([]string) {
				known[id] = true
			}
		}
		linked[kind] = known
	}
	for _, account := range result.Accounts {
		id := str(account["id"])
		missing := []row{}
		for _, item := range []struct{ kind, reason string }{
			{"card", "未找到可核对的注册手机卡，请确认注册号码和卡片归属。"},
			{"phone", "未找到可核对的登录实物，请确认该账号的登录手机。"},
			{"slot", "未找到可核对的发布格，请确认是否有发布安排。"},
		} {
			if !linked[item.kind][id] {
				missing = append(missing, row{"kind": item.kind, "reason": item.reason})
			}
		}
		account["missing"] = missing
	}
}
