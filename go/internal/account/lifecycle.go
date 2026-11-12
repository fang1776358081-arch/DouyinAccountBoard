package account

import (
	"context"
	"database/sql"
	"errors"
	"slices"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/session"
)

var lifecycleFields = []string{"account_type", "parent_account_id", "registered_on", "auxiliary_state", "lifecycle_note", "registration_phone"}

func pythonSpace(r rune) bool   { return unicode.IsSpace(r) || r >= 0x1c && r <= 0x1f }
func strip(value string) string { return strings.TrimFunc(value, pythonSpace) }

func validateLifecycle(ctx context.Context, tx *sql.Tx, actor session.User, id string, patch, old map[string]any, today time.Time) (map[string]any, error) {
	state := make(map[string]any, len(lifecycleFields))
	for _, field := range lifecycleFields {
		state[field] = old[field]
		if value, ok := patch[field]; ok {
			state[field] = value
		}
	}
	kind := textField(state, "account_type")
	aux := textField(state, "auxiliary_state")
	registered := strip(textField(state, "registered_on"))
	parent := strip(textField(state, "parent_account_id"))
	phone := strip(textField(state, "registration_phone"))
	if phone != "" && (utf8.RuneCountInString(phone) != 11 || phone[0] != '1' || strings.IndexFunc(phone, func(r rune) bool { return !unicode.IsDigit(r) }) >= 0) {
		return nil, accountwrite.Invalid("注册手机号应为11位手机号，未知可留空")
	}
	if !slices.Contains([]string{"待确认", "大号", "小号"}, kind) || !slices.Contains([]string{"待确认", "未注册", "已注册", "暂无法注册"}, aux) {
		return nil, accountwrite.Invalid("账号类型或辅助账号注册状态不正确")
	}
	if registered != "" {
		day, err := isoDate(registered)
		if err != nil {
			return nil, accountwrite.Invalid("注册日期格式不正确")
		}
		if day.After(today) {
			return nil, accountwrite.Invalid("注册日期不能晚于今天")
		}
	}
	if kind == "小号" && phone != "" && parent == "" {
		rows, err := tx.QueryContext(ctx, `SELECT id FROM board_candidate.accounts
WHERE account_type='大号' AND registration_phone=$1 AND id<>$2 LIMIT 2`, phone, id)
		if err != nil {
			return nil, err
		}
		defer rows.Close()
		for rows.Next() {
			if parent != "" {
				return nil, accountwrite.Invalid("同一手机号对应多个大号，请手动确认关联大号")
			}
			if err := rows.Scan(&parent); err != nil {
				return nil, err
			}
		}
		if err := rows.Err(); err != nil {
			return nil, err
		}
	}
	if parent != "" {
		if parent != textField(old, "parent_account_id") {
			if err := requireReference(ctx, tx, actor, relatedEntities[1], parent, true); err != nil {
				return nil, err
			}
		}
		var parentKind, parentPhone string
		err := tx.QueryRowContext(ctx, `SELECT account_type,registration_phone FROM board_candidate.accounts WHERE id=$1`, parent).Scan(&parentKind, &parentPhone)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return nil, err
		}
		if kind != "小号" || parent == id || parentKind != "大号" {
			return nil, accountwrite.Invalid("小号只能关联另一个已标记为大号的账号")
		}
		if phone != "" && parentPhone != "" && phone != parentPhone {
			return nil, accountwrite.Invalid("大小号的注册手机号不一致，请核对配对关系")
		}
		exists, err := rowExists(ctx, tx, `SELECT EXISTS(SELECT 1 FROM board_candidate.accounts WHERE parent_account_id=$1 AND id<>$2)`, parent, id)
		if err != nil {
			return nil, err
		}
		if exists {
			return nil, accountwrite.Invalid("这个大号已经关联小号，请先核对原有关系")
		}
	}
	if kind != "小号" {
		parent = ""
	}
	hasChildren, err := rowExists(ctx, tx, `SELECT EXISTS(SELECT 1 FROM board_candidate.accounts WHERE parent_account_id=$1)`, id)
	if err != nil {
		return nil, err
	}
	if hasChildren {
		if kind != "大号" || aux == "未注册" || aux == "暂无法注册" {
			return nil, accountwrite.Invalid("已有小号关联，不能将大号改为其他类型或标记未注册")
		}
		if phone != textField(old, "registration_phone") {
			mismatch, err := rowExists(ctx, tx, `SELECT EXISTS(SELECT 1 FROM board_candidate.accounts
WHERE parent_account_id=$1 AND registration_phone<>'' AND registration_phone<>$2)`, id, phone)
			if err != nil {
				return nil, err
			}
			if mismatch {
				return nil, accountwrite.Invalid("修改后会与已关联小号的注册手机号不一致，请先核对或解除配对关系")
			}
		}
	}
	state["registered_on"] = registered
	state["lifecycle_note"] = accountwrite.Truncate(textField(state, "lifecycle_note"), 1000)
	state["registration_phone"] = phone
	state["parent_account_id"] = nil
	if parent != "" {
		state["parent_account_id"] = parent
	}
	return state, nil
}

func rowExists(ctx context.Context, tx *sql.Tx, statement string, args ...any) (bool, error) {
	var exists bool
	err := tx.QueryRowContext(ctx, statement, args...).Scan(&exists)
	return exists, err
}
