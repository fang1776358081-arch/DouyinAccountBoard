package exchange

import (
	"context"
	"database/sql"
	"regexp"
	"slices"
	"strings"
	"time"
	"unicode/utf8"

	"douyinboard/internal/account"
	"douyinboard/internal/accountwrite"
	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

const OfficePreviewPath = "/api/workspace/office-three-import-preview"
const OfficeApplyPath = "/api/workspace/office-three-import-apply"
const OfficeComputerScopePath = "/api/workspace/office-three-import-computer-scopes"
const NicknameMode = "fill_missing_nicknames"
const nicknameKind = "office-three-nickname-fill"
const nicknameConfirmation = "仅补全办公室三缺失昵称"
const nicknameAudit = "办公室三缺失昵称来源"
const nicknameURL = "https://docs.qq.com/sheet/DRWxWc1huUWFsVkZp?tab=BB08J2"
const nicknameSheet = "BB08J2"

var officeHandle = regexp.MustCompile(`^[A-Za-z0-9_.-]{2,100}$`)
var missingNickname = regexp.MustCompile(`^昵称待补充(?:（手机[1-9][0-9]{0,5}）)?$`)

type nicknameRow struct {
	Row         int    `json:"row"`
	ID          string `json:"account_id"`
	Handle      string `json:"douyin_id"`
	CurrentName string `json:"current_name"`
	SourceName  string `json:"source_name"`
	Reason      string `json:"reason"`
	Action      string `json:"action"`
}

type nicknamePreview struct {
	Mode    string         `json:"mode"`
	Office  string         `json:"office_id"`
	Day     string         `json:"day"`
	Guard   string         `json:"guard"`
	Plan    []nicknameRow  `json:"plan"`
	Summary map[string]int `json:"summary"`
}

func acceptableNickname(name string) bool {
	return name != "" && utf8.RuneCountInString(name) <= 100 && !strings.HasPrefix(name, "昵称待补充")
}

func needsNickname(name string) bool { return name == "" || missingNickname.MatchString(name) }

func OfficePreview(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	if body["mode"] == NicknameMode {
		return previewNicknames(ctx, tx, actor, body)
	}
	return OfficeThreePreview(ctx, tx, actor, body)
}

func OfficeApply(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	if body["mode"] == NicknameMode {
		return applyNicknames(ctx, tx, actor, body)
	}
	return OfficeThreeApply(ctx, tx, actor, body)
}

func nicknameRows(ctx context.Context, tx *sql.Tx, actor session.User, source []officeSourceRow, strict bool) ([]nicknameRow, int, error) {
	counts := map[string]int{}
	for _, item := range source {
		counts[item.Fields["抖音号"]]++
	}
	rows := []nicknameRow{}
	matched := 0
	for _, item := range source {
		handle, name := item.Fields["抖音号"], item.Fields["昵称"]
		if handle == "" && name == "" {
			continue
		}
		row := nicknameRow{Row: item.Number, Handle: handle, SourceName: name, Action: "conflict"}
		switch {
		case !officeHandle.MatchString(handle):
			row.Reason = "真实抖音号缺失或格式不明确；不按名称、手机号或手机编号猜测"
		case counts[handle] != 1:
			row.Reason = "来源有重复抖音号，整组保留待核对"
		default:
			found, err := matching(ctx, tx, entities["accounts"], "r.douyin_id=$1 LIMIT 2", handle)
			if err != nil {
				return nil, 0, err
			}
			switch len(found) {
			case 0:
				row.Action, row.Reason = "unmatched", "工作台没有此抖音号；补名入口不新增账号"
			case 1:
				old := found[0]
				offices, _ := stringList(old["office_ids"])
				if !slices.Equal(offices, []string{officeThreeID}) {
					row.Reason = "账号并非明确且仅属于办公室三；不移动或覆盖"
					break
				}
				matched++
				row.ID, row.CurrentName = String(old["id"]), String(old["name"])
				switch {
				case name == "":
					row.Action, row.Reason = "skip", "来源昵称为空，保留系统原值"
				case !acceptableNickname(name):
					row.Reason = "来源昵称过长或仍为待补充标记，保留原值"
				case name == row.CurrentName:
					row.Action, row.Reason = "same", "昵称已经一致，不重复修改"
				case !needsNickname(row.CurrentName):
					row.Reason = "两边已有不同真实名称；本入口仅补空白或占位名称"
				default:
					row.Action, row.Reason = "update", "唯一抖音号匹配；仅补全缺失昵称"
					if strict {
						extra, err := account.NicknameChangesNurture(ctx, tx, actor, row.ID)
						if err != nil {
							return nil, 0, err
						}
						if extra {
							row.Action, row.Reason = "conflict", "补名会触发额外资料变化或校验冲突，整行保留"
						}
					}
				}
			default:
				row.Reason = "工作台存在重复抖音号，整组保留待核对"
			}
		}
		rows = append(rows, row)
	}
	return rows, matched, nil
}

func nicknameSuggestions(ctx context.Context, tx *sql.Tx, actor session.User, source []officeSourceRow) ([]nicknameRow, error) {
	rows, _, err := nicknameRows(ctx, tx, actor, source, false)
	return slices.DeleteFunc(rows, func(row nicknameRow) bool { return row.Action != "update" }), err
}

func previewNicknames(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	file, _ := body["accounts_file"].(map[string]any)
	source, err := readOfficeAccounts(file)
	if err != nil {
		return nil, err
	}
	guard, err := officeGuard(ctx, tx, false)
	if err != nil {
		return nil, err
	}
	rows, matched, err := nicknameRows(ctx, tx, actor, source, true)
	if err != nil {
		return nil, err
	}
	summary := map[string]int{"source_rows": len(rows), "matched_accounts": matched, "updated_accounts": 0,
		"conflicts": 0, "unmatched": 0, "unchanged": 0, "skipped": 0}
	plan := nicknamePreview{NicknameMode, officeThreeID, today(), guard, []nicknameRow{}, summary}
	counters := map[string]string{"conflict": "conflicts", "unmatched": "unmatched", "same": "unchanged", "skip": "skipped", "update": "updated_accounts"}
	for _, row := range rows {
		summary[counters[row.Action]]++
		if row.Action == "update" {
			plan.Plan = append(plan.Plan, row)
		}
	}
	id, err := storePreview(ctx, tx, actor.ID, nicknameKind, plan)
	if err != nil {
		return nil, err
	}
	return map[string]any{"mode": NicknameMode, "preview_id": id, "office_id": officeThreeID,
		"confirmation": nicknameConfirmation, "summary": summary, "rows": rows,
		"source_url": nicknameURL, "source_sheet": nicknameSheet,
		"warnings": []string{"仅补全办公室三唯一账号的空白或严格占位昵称；不新增账号，不修改电脑、手机、卡或其他业务字段。",
			"源空白保留；真实名称不一致、重复标识、未匹配或归属不明确的记录均不修改。"}}, nil
}

func applyNicknames(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	for key, value := range body {
		switch key {
		case "mode", "office_id", "preview_id", "confirmation", "request_id":
		case "_expected_versions":
			if versions, ok := value.(map[string]any); ok && len(versions) == 0 {
				continue
			}
			return nil, accountwrite.Invalid("确认请求不能包含客户端修改计划或整行资料")
		default:
			return nil, accountwrite.Invalid("确认请求不能包含客户端修改计划或整行资料")
		}
	}
	if body["confirmation"] != nicknameConfirmation {
		return nil, accountwrite.Invalid("请明确输入“" + nicknameConfirmation + "”确认")
	}
	ident := String(body["preview_id"])
	var plan nicknamePreview
	if err := readPreview(ctx, tx, actor.ID, nicknameKind, ident, &plan); err != nil {
		return nil, err
	}
	guard, err := officeGuard(ctx, tx, false)
	if err != nil {
		return nil, err
	}
	if plan.Mode != NicknameMode || plan.Office != officeThreeID || plan.Day != today() || plan.Guard != guard {
		return nil, accountwrite.Conflict("预览后资料、归属或日期已变化，请重新预览；未保存任何补名")
	}
	fieldsBefore, err := officeGuard(ctx, tx, true)
	if err != nil {
		return nil, err
	}
	changes := []map[string]any{}
	seen := map[string]bool{}
	group := newHexID()
	requiresReload := false
	for _, item := range plan.Plan {
		if seen[item.ID] || !acceptableNickname(item.SourceName) || !needsNickname(item.CurrentName) {
			return nil, accountwrite.Conflict("补名计划身份、归属或名称不正确，整批已撤销")
		}
		seen[item.ID] = true
		before, office, err := account.ReadScoped(ctx, tx, actor, item.ID)
		if err != nil {
			return nil, err
		}
		if office.String != officeThreeID || before["douyin_id"] != item.Handle || before["name"] != item.CurrentName {
			return nil, accountwrite.Conflict("账号身份、归属或名称已变化，请重新预览")
		}
		if _, err := tx.ExecContext(ctx, "UPDATE accounts SET name=$1 WHERE id=$2", item.SourceName, item.ID); err != nil {
			return nil, err
		}
		after, _, err := account.ReadScoped(ctx, tx, actor, item.ID)
		if err != nil {
			return nil, err
		}
		scope, err := nicknameHistoryScope(ctx, tx, before)
		if err != nil {
			return nil, err
		}
		requiresReload = requiresReload || scope == nil
		prevJSON, err := recordwire.Encode(before)
		if err != nil {
			return nil, err
		}
		nextJSON, err := recordwire.Encode(after)
		if err != nil {
			return nil, err
		}
		var history int64
		err = tx.QueryRowContext(ctx, `INSERT INTO operations_history
			(group_id,at,actor_id,actor_name,action,entity,before_json,after_json,scope_offices)
			VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id`, group, time.Now().Unix(), actor.ID, actor.Name,
			OfficeApplyPath, "account:"+item.ID, string(prevJSON), string(nextJSON), scope).Scan(&history)
		if err != nil {
			return nil, err
		}
		audit, err := recordwire.Encode(map[string]any{"source": nicknameURL, "row": item.Row, "history_id": history, "preview": ident})
		if err != nil {
			return nil, err
		}
		if err := accountwrite.Audit(ctx, tx, actor, nicknameAudit, string(audit)); err != nil {
			return nil, err
		}
		version, err := recordwire.Version(after)
		if err != nil {
			return nil, err
		}
		changes = append(changes, map[string]any{"account_id": item.ID, "douyin_id": item.Handle,
			"before_name": item.CurrentName, "after_name": item.SourceName, "source_row": item.Row,
			"source_url": nicknameURL, "source_sheet": nicknameSheet, "after_version": version, "history_id": history})
	}
	fieldsAfter, err := officeGuard(ctx, tx, true)
	if err != nil {
		return nil, err
	}
	if fieldsBefore != fieldsAfter {
		return nil, accountwrite.Conflict("补名保留校验失败：名称之外的资料、关系或历史发生变化，整批已撤销")
	}
	if requiresReload {
		if err := noteReload(ctx, tx, actor, OfficeApplyPath); err != nil {
			return nil, err
		}
	}
	if _, err := tx.ExecContext(ctx, "DELETE FROM operations_previews WHERE id=$1", ident); err != nil {
		return nil, err
	}
	return map[string]any{"ok": true, "mode": NicknameMode, "office_id": officeThreeID, "summary": plan.Summary,
		"updated_accounts": len(changes), "changes": changes, "requires_refresh": true,
		"verification": map[string]any{"only_name_fields_changed": true, "other_offices_unchanged": true,
			"associations_preserved": true, "history_preserved": true, "updated_accounts": len(changes)},
		"note": "仅保存已预览昵称；来源及修改前后值已记录，其他业务资料保持。"}, nil
}

func nicknameHistoryScope(ctx context.Context, tx *sql.Tx, row map[string]any) (any, error) {
	for _, reference := range []struct{ field, table, key string }{
		{"computer_id", "computer_offices", "computer_id"},
		{"parent_account_id", "account_offices", "account_id"},
		{"phone_device_id", "phone_device_offices", "device_id"},
	} {
		id := String(row[reference.field])
		if id == "" || reference.field == "computer_id" && id == "unassigned" {
			continue
		}
		query := "SELECT count(*)=1 AND min(office_id)=$2 FROM " + reference.table + " WHERE " + reference.key + "=$1"
		var match sql.NullBool
		if err := tx.QueryRowContext(ctx, query, id, officeThreeID).Scan(&match); err != nil {
			return nil, err
		}
		if !match.Bool {
			return nil, nil
		}
	}
	return `["three"]`, nil
}
