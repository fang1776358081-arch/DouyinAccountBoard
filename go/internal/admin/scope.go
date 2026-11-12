package admin

import (
	"context"
	"database/sql"
	"slices"
	"strconv"
	"strings"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/inventory"
	"douyinboard/internal/session"
)

func Reassign(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	if err := AuthorizeOwner(ctx, tx, actor, nil); err != nil {
		return nil, err
	}
	kind := text(body["kind"])
	entity, found := scopeKinds[kind]
	if !found {
		return nil, accountwrite.Invalid("资料类型不正确")
	}
	ident := text(body["id"])
	var id any
	if entity.numeric {
		var number int64
		var err error
		if ident == "" {
			number, err = numericID(body["id"])
			ident = strconv.FormatInt(number, 10)
		} else {
			number, err = strconv.ParseInt(ident, 10, 64)
			if err == nil && strconv.FormatInt(number, 10) != ident {
				return nil, accountwrite.Forbidden("无权访问此资料")
			}
		}
		if err != nil {
			return nil, accountwrite.Forbidden("无权访问此资料")
		}
		id = number
	} else {
		id = ident
	}
	if ident == "" || strings.ContainsRune(ident, 0) {
		return nil, accountwrite.Forbidden("无权访问此资料")
	}
	var exists bool
	if err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM board_candidate.`+entity.table+` WHERE id=$1)`, id).Scan(&exists); err != nil {
		return nil, err
	}
	if !exists {
		return nil, accountwrite.Forbidden("无权访问此资料")
	}
	prior, err := readOfficeIDs(ctx, tx, `SELECT office_id FROM board_candidate.`+entity.mapping+` WHERE `+entity.key+`=$1`, id)
	if err != nil {
		return nil, err
	}
	current, err := scopeToken(kind, ident, prior)
	if err != nil {
		return nil, err
	}
	if text(body["expected_scope"]) != current {
		return nil, accountwrite.Conflict("资料归属已变化，请重新读取后核对；未覆盖新的分配")
	}
	wanted, err := readOfficeList(body["office_ids"])
	if err != nil {
		return nil, err
	}
	if err := validateActiveOffices(ctx, tx, wanted); err != nil {
		return nil, err
	}
	if entity.single && len(wanted) > 1 {
		return nil, accountwrite.Invalid("账号和电脑只能属于一个办公室")
	}
	if kind == "devices" {
		if err := inventory.ValidateScopeChange(ctx, tx, ident, prior, wanted); err != nil {
			return nil, err
		}
	}
	if !slices.Equal(prior, wanted) {
		if err := replaceScope(ctx, tx, entity.mapping, entity.key, id, wanted); err != nil {
			return nil, err
		}
		if err := enqueueReload(ctx, tx); err != nil {
			return nil, err
		}
	}
	if err := auditDetails(ctx, tx, actor, "分配资料办公室", map[string]any{"kind": kind, "id": body["id"], "before": prior, "after": wanted}); err != nil {
		return nil, err
	}
	version, err := scopeToken(kind, ident, wanted)
	return map[string]any{"ok": true, "scope_version": version, "office_ids": wanted}, err
}
