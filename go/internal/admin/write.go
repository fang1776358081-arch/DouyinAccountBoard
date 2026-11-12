package admin

import (
	"context"
	"crypto/pbkdf2"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"regexp"
	"slices"
	"strings"
	"time"
	"unicode/utf8"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

var usernamePattern = regexp.MustCompile(`^[A-Za-z0-9_.-]{3,40}$`)

func SaveUser(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	if err := AuthorizeOwner(ctx, tx, actor, nil); err != nil {
		return nil, err
	}
	role := "employee"
	if requested, found := body["role"]; found {
		role, _ = requested.(string)
	}
	permissions, validRole := session.DefaultPermissions(role)
	if !validRole {
		return nil, accountwrite.Invalid("角色或权限不正确；不能新增总管理员")
	}
	if raw, found := body["permissions"]; found {
		input, ok := raw.([]any)
		if !ok {
			return nil, accountwrite.Invalid("角色或权限不正确；不能新增总管理员")
		}
		permissions = make([]string, 0, len(input))
		for _, value := range input {
			permission, ok := value.(string)
			if !ok || !session.ValidPermission(permission) {
				return nil, accountwrite.Invalid("角色或权限不正确；不能新增总管理员")
			}
			permissions = append(permissions, permission)
		}
	}
	slices.Sort(permissions)
	permissions = slices.Compact(permissions)
	encodedPermissions, err := json.Marshal(permissions)
	if err != nil {
		return nil, err
	}
	active := true
	if raw, found := body["active"]; found {
		var ok bool
		active, ok = raw.(bool)
		if !ok {
			return nil, accountwrite.Invalid("启用状态不正确")
		}
	}
	id, err := numericID(body["id"])
	if err != nil {
		return nil, accountwrite.Invalid("人员不存在")
	}
	username := text(body["username"])
	priorOffices := []string{}
	if id != 0 {
		var previousRole string
		err := tx.QueryRowContext(ctx, `SELECT username,role FROM board_candidate.auth_users WHERE id=$1`, id).Scan(&username, &previousRole)
		if errors.Is(err, sql.ErrNoRows) {
			return nil, accountwrite.Invalid("人员不存在")
		}
		if err != nil {
			return nil, err
		}
		if previousRole == "owner" {
			return nil, accountwrite.Forbidden("总管理员不能被修改角色、停用或由人员管理重置密码")
		}
		priorOffices, err = readOfficeIDs(ctx, tx, `SELECT office_id FROM board_candidate.user_office_grants WHERE user_id=$1 ORDER BY office_id`, id)
		if err != nil {
			return nil, err
		}
	}
	username = strings.TrimSpace(username)
	if !usernamePattern.MatchString(username) {
		return nil, accountwrite.Invalid("登录名请使用3至40位字母、数字、点、横线或下划线")
	}
	name := strings.TrimSpace(text(body["name"]))
	if name == "" {
		name = username
	}
	if utf8.RuneCountInString(name) > 60 || strings.ContainsRune(name, 0) {
		return nil, accountwrite.Invalid("姓名不能超过60字")
	}
	var encodedPassword string
	if raw := body["password"]; raw != nil && raw != "" {
		password, ok := raw.(string)
		if !ok || utf8.RuneCountInString(password) < 10 || utf8.RuneCountInString(password) > 128 {
			return nil, accountwrite.Invalid("密码请设置为10至128位")
		}
		encodedPassword, err = hashPassword(password)
		if err != nil {
			return nil, err
		}
	}
	wanted := priorOffices
	if raw, found := body["office_ids"]; found {
		wanted, err = readOfficeList(raw)
		if err != nil {
			return nil, err
		}
	}
	if err := validateActiveOffices(ctx, tx, wanted); err != nil {
		return nil, err
	}
	activeValue := 0
	if active {
		activeValue = 1
	}
	action := "更新人员与权限"
	if id == 0 {
		if encodedPassword == "" {
			return nil, accountwrite.Invalid("请设置初始密码（10至128位）")
		}
		action = "新增人员"
		err = tx.QueryRowContext(ctx, `INSERT INTO board_candidate.auth_users(username,name,role,permissions,active,password_hash,created_at)
VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id`, username, name, role, string(encodedPermissions), activeValue, encodedPassword, time.Now().Unix()).Scan(&id)
		if hasSQLState(err, "23505") {
			return nil, accountwrite.Invalid("登录名已存在")
		}
	} else {
		_, err = tx.ExecContext(ctx, `UPDATE board_candidate.auth_users SET name=$1,role=$2,permissions=$3,active=$4,
password_hash=COALESCE(NULLIF($5,''),password_hash) WHERE id=$6`, name, role, string(encodedPermissions), activeValue, encodedPassword, id)
	}
	if err != nil {
		return nil, err
	}
	if err := grantOffices(ctx, tx, actor, id, priorOffices, wanted); err != nil {
		return nil, err
	}
	if err := accountwrite.Audit(ctx, tx, actor, action, username); err != nil {
		return nil, err
	}
	return map[string]any{"ok": true, "id": id}, nil
}

func hashPassword(password string) (string, error) {
	var salt [16]byte
	if _, err := rand.Read(salt[:]); err != nil {
		return "", err
	}
	saltHex := hex.EncodeToString(salt[:])
	value, err := pbkdf2.Key(sha256.New, password, []byte(saltHex), 600000, sha256.Size)
	if err != nil {
		return "", err
	}
	return "pbkdf2_sha256$600000$" + saltHex + "$" + hex.EncodeToString(value), nil
}

func grantOffices(ctx context.Context, tx *sql.Tx, actor session.User, userID int64, prior, wanted []string) error {
	if err := replaceScope(ctx, tx, "user_office_grants", "user_id", userID, wanted); err != nil {
		return err
	}
	var version int64
	if err := tx.QueryRowContext(ctx, `UPDATE board_candidate.auth_users SET authorization_version=authorization_version+1 WHERE id=$1 RETURNING authorization_version`, userID).Scan(&version); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `DELETE FROM board_candidate.auth_sessions WHERE user_id=$1`, userID); err != nil {
		return err
	}
	if !slices.Equal(prior, wanted) {
		if err := enqueueReload(ctx, tx); err != nil {
			return err
		}
	}
	return auditDetails(ctx, tx, actor, "更新办公室授权", map[string]any{"user_id": userID, "before": prior, "after": wanted, "authorization_version": version})
}

func CreateOffice(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	if err := AuthorizeOwner(ctx, tx, actor, nil); err != nil {
		return nil, err
	}
	name := strings.TrimSpace(text(body["name"]))
	if name == "" || utf8.RuneCountInString(name) > 100 || strings.ContainsRune(name, 0) {
		return nil, accountwrite.Invalid("办公室名称需为1至100字")
	}
	group, err := accountwrite.NewGroup()
	if err != nil {
		return nil, err
	}
	id := "office-" + group
	if _, err := tx.ExecContext(ctx, `INSERT INTO board_candidate.offices(id,name) VALUES($1,$2)`, id, name); err != nil {
		if hasSQLState(err, "23505") {
			return nil, accountwrite.Invalid("办公室名称已存在")
		}
		return nil, err
	}
	if err := accountwrite.Audit(ctx, tx, actor, "创建办公室", id); err != nil {
		return nil, err
	}
	return map[string]any{"ok": true, "id": id}, nil
}

func numericID(raw any) (int64, error) {
	switch value := raw.(type) {
	case nil:
		return 0, nil
	case json.Number:
		return value.Int64()
	case int64:
		return value, nil
	default:
		return 0, errors.New("invalid numeric identifier")
	}
}

func text(raw any) string {
	value, _ := raw.(string)
	return value
}

func readOfficeList(raw any) ([]string, error) {
	input, ok := raw.([]any)
	if !ok || len(input) > 100 {
		return nil, accountwrite.Invalid("办公室范围格式不正确")
	}
	offices := make([]string, 0, len(input))
	for _, value := range input {
		id := text(value)
		if id == "" || utf8.RuneCountInString(id) > 80 || strings.ContainsRune(id, 0) {
			return nil, accountwrite.Invalid("办公室范围格式不正确")
		}
		offices = append(offices, id)
	}
	slices.Sort(offices)
	if len(slices.Compact(slices.Clone(offices))) != len(offices) {
		return nil, accountwrite.Invalid("办公室范围不能重复")
	}
	return offices, nil
}

func validateActiveOffices(ctx context.Context, tx *sql.Tx, offices []string) error {
	for _, id := range offices {
		var active bool
		err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM board_candidate.offices WHERE id=$1 AND active=1)`, id).Scan(&active)
		if err != nil {
			return err
		}
		if !active {
			return accountwrite.Invalid("办公室不存在或已停用")
		}
	}
	return nil
}

func readOfficeIDs(ctx context.Context, tx *sql.Tx, statement string, id any) ([]string, error) {
	rows, err := tx.QueryContext(ctx, statement, id)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []string{}
	for rows.Next() {
		var office string
		if err := rows.Scan(&office); err != nil {
			return nil, err
		}
		result = append(result, office)
	}
	slices.Sort(result)
	return result, rows.Err()
}

// mapping and key are exclusively chosen from the static scope definitions.
func replaceScope(ctx context.Context, tx *sql.Tx, mapping, key string, id any, offices []string) error {
	if _, err := tx.ExecContext(ctx, `DELETE FROM board_candidate.`+mapping+` WHERE `+key+`=$1`, id); err != nil {
		return err
	}
	for _, office := range offices {
		if _, err := tx.ExecContext(ctx, `INSERT INTO board_candidate.`+mapping+`(`+key+`,office_id) VALUES($1,$2)`, id, office); err != nil {
			return err
		}
	}
	return nil
}

func enqueueReload(ctx context.Context, tx *sql.Tx) error {
	group, err := accountwrite.NewGroup()
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO board_candidate.operations_history(group_id,at,actor_name,action,entity)
VALUES($1,$2,'同步','reload','sync:reload')`, group, time.Now().Unix())
	return err
}

func auditDetails(ctx context.Context, tx *sql.Tx, actor session.User, action string, detail map[string]any) error {
	encoded, err := recordwire.Encode(detail)
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO board_candidate.auth_audit(at,actor_id,actor_name,action,target)
VALUES($1,$2,$3,$4,$5)`, time.Now().Unix(), actor.ID, actor.Name, action, string(encoded))
	return err
}

func hasSQLState(err error, code string) bool {
	var state interface{ SQLState() string }
	return errors.As(err, &state) && state.SQLState() == code
}
