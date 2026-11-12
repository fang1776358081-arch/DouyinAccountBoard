package account

import (
	"context"
	"database/sql"
	"errors"
	"slices"
	"strings"
	"unicode/utf8"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/session"
)

const noAccess = "无权访问此资料"
const permissionDenied = "当前账号没有此操作权限"

type relatedEntity struct {
	field, table, mapping, column, prefix string
}

var relatedEntities = []relatedEntity{
	{"computer_id", "computers", "computer_offices", "computer_id", "computer"},
	{"parent_account_id", "accounts", "account_offices", "account_id", "account"},
	{"phone_device_id", "phone_devices", "phone_device_offices", "device_id", "device"},
}

func hasReference(entity relatedEntity, id string) bool {
	return id != "" && !(entity.field == "computer_id" && id == "unassigned")
}

func requireAccount(ctx context.Context, tx *sql.Tx, actor session.User, id string) (map[string]any, error) {
	if strings.ContainsRune(id, 0) {
		return nil, accountwrite.Forbidden(noAccess)
	}
	row, _, err := ReadScoped(ctx, tx, actor, id)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, accountwrite.Forbidden(noAccess)
	}
	return row, err
}

func referenceOffices(ctx context.Context, tx *sql.Tx, entity relatedEntity, id string) ([]string, error) {
	rows, err := tx.QueryContext(ctx, `SELECT office_id FROM board_candidate.`+entity.mapping+
		` WHERE `+entity.column+`=$1 ORDER BY office_id`, id)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []string{}
	for rows.Next() {
		var value string
		if err := rows.Scan(&value); err != nil {
			return nil, err
		}
		result = append(result, value)
	}
	return result, rows.Err()
}

func requireReference(ctx context.Context, tx *sql.Tx, actor session.User, entity relatedEntity, id string, write bool) error {
	if strings.ContainsRune(id, 0) {
		return accountwrite.Forbidden(noAccess)
	}
	var exists bool
	err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM board_candidate.`+entity.table+` WHERE id=$1)`, id).Scan(&exists)
	if err != nil {
		return err
	}
	if !exists {
		return accountwrite.Forbidden(noAccess)
	}
	if actor.Global() {
		return nil
	}
	scopes, err := referenceOffices(ctx, tx, entity, id)
	if err != nil {
		return err
	}
	matched := 0
	for _, scope := range scopes {
		if slices.Contains(actor.OfficeIDs, scope) {
			matched++
		}
	}
	if matched == 0 || write && matched != len(scopes) {
		return accountwrite.Forbidden(noAccess)
	}
	return nil
}

func authorizeReferences(ctx context.Context, tx *sql.Tx, actor session.User, old, changes map[string]any) error {
	for _, entity := range relatedEntities {
		value, set := changes[entity.field]
		if !set && entity.field == "computer_id" {
			_, set = changes["slot"]
			value = old["computer_id"]
		}
		if !set {
			continue
		}
		next := identifier(value)
		prev := textField(old, entity.field)
		if hasReference(entity, next) {
			if err := requireReference(ctx, tx, actor, entity, next, entity.field == "phone_device_id"); err != nil {
				return err
			}
		}
		if prev != next && hasReference(entity, prev) {
			if err := requireReference(ctx, tx, actor, entity, prev, entity.field == "phone_device_id"); err != nil {
				return err
			}
		}
	}
	return nil
}

func requestedOffice(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (string, error) {
	value, exists := body["office_id"]
	if !exists && !actor.Global() {
		return "", accountwrite.Invalid("新增资料必须明确选择办公室")
	}
	office, ok := value.(string)
	if value != nil && !ok {
		return "", accountwrite.Invalid("办公室编号格式不正确")
	}
	if office != "" {
		if utf8.RuneCountInString(office) > 80 || strings.ContainsRune(office, 0) {
			return "", accountwrite.Invalid("办公室范围格式不正确")
		}
		var exists bool
		err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM board_candidate.offices WHERE id=$1 AND active=1)`, office).Scan(&exists)
		if err != nil {
			return "", err
		}
		if !exists {
			return "", accountwrite.Invalid("办公室不存在或已停用")
		}
	}
	if !actor.Global() && !slices.Contains(actor.OfficeIDs, office) {
		return "", accountwrite.Forbidden("目标办公室不在授权范围内")
	}
	return office, nil
}

func setOffice(ctx context.Context, tx *sql.Tx, id, office string) error {
	if office == "" {
		_, err := tx.ExecContext(ctx, `DELETE FROM board_candidate.account_offices WHERE account_id=$1`, id)
		return err
	}
	_, err := tx.ExecContext(ctx, `INSERT INTO board_candidate.account_offices(account_id,office_id) VALUES($1,$2)
ON CONFLICT(account_id) DO UPDATE SET office_id=EXCLUDED.office_id`, id, office)
	return err
}

func accountHistoryScope(ctx context.Context, tx *sql.Tx, before, after map[string]any) ([]string, error) {
	all := []string{}
	for _, row := range []map[string]any{before, after} {
		if row == nil {
			continue
		}
		offices, _ := row["office_ids"].([]string)
		if len(offices) == 0 {
			return nil, nil
		}
		all = append(all, offices...)
		for _, entity := range relatedEntities {
			id := textField(row, entity.field)
			if !hasReference(entity, id) {
				continue
			}
			related, err := referenceOffices(ctx, tx, entity, id)
			if err != nil {
				return nil, err
			}
			if len(related) == 0 {
				return nil, nil
			}
			all = append(all, related...)
		}
	}
	slices.Sort(all)
	return slices.Compact(all), nil
}
