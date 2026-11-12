// Package admin exposes the owner-only personnel and office management routes.
package admin

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"time"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

const UsersPath, OfficesPath, ScopePath = "/api/auth/users", "/api/auth/offices", "/api/auth/entity-scope"

type Handler struct{ DB *sql.DB }

func AuthorizeOwner(_ context.Context, _ *sql.Tx, actor session.User, _ map[string]any) error {
	if !actor.Has("owner") {
		return accountwrite.Forbidden("只有总管理员可以管理人员和分配权限")
	}
	return nil
}

func (h Handler) read(w http.ResponseWriter, r *http.Request, load func(context.Context, *sql.Tx) (any, error)) {
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	tx, err := h.DB.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
	if err != nil {
		replyFailure(w, err)
		return
	}
	defer tx.Rollback()
	actor, err := session.AuthenticateWith(ctx, tx, r)
	if err == nil {
		err = AuthorizeOwner(ctx, tx, actor, nil)
	}
	if err != nil {
		replyFailure(w, err)
		return
	}
	result, err := load(ctx, tx)
	if err == nil {
		err = tx.Commit()
	}
	if err != nil {
		replyFailure(w, err)
		return
	}
	reply(w, http.StatusOK, result)
}

func (h Handler) Users(w http.ResponseWriter, r *http.Request) {
	h.read(w, r, func(ctx context.Context, tx *sql.Tx) (any, error) {
		users, err := listUsers(ctx, tx)
		if err != nil {
			return nil, err
		}
		offices, err := jsonRows(ctx, tx, `SELECT to_jsonb(o)::text FROM board_candidate.offices o ORDER BY name,id`)
		return map[string]any{"users": users, "offices": offices, "scope_enabled": true}, err
	})
}

func listUsers(ctx context.Context, tx *sql.Tx) ([]map[string]any, error) {
	const query = `SELECT u.id,u.username,u.name,u.role,u.active,u.permissions,
COALESCE((SELECT json_agg(g.office_id ORDER BY g.office_id)::text FROM board_candidate.user_office_grants g WHERE g.user_id=u.id),'[]')
FROM board_candidate.auth_users u ORDER BY u.id`
	rows, err := tx.QueryContext(ctx, query)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	users := []map[string]any{}
	for rows.Next() {
		var id, active int64
		var username, name, role, permissions string
		var grants []byte
		if err := rows.Scan(&id, &username, &name, &role, &active, &permissions, &grants); err != nil {
			return nil, err
		}
		var offices []string
		if err := json.Unmarshal(grants, &offices); err != nil {
			return nil, err
		}
		mode := "assigned"
		if (session.User{Role: role}).Global() {
			mode = "all"
		}
		users = append(users, map[string]any{"id": id, "username": username, "name": name, "role": role, "active": active == 1,
			"permissions": session.PublicPermissions(role, permissions), "office_ids": offices, "scope_mode": mode})
	}
	return users, rows.Err()
}

func (h Handler) Audit(w http.ResponseWriter, r *http.Request) {
	h.read(w, r, func(ctx context.Context, tx *sql.Tx) (any, error) {
		entries, err := jsonRows(ctx, tx, `SELECT to_jsonb(a)::text FROM board_candidate.auth_audit a ORDER BY a.id DESC LIMIT 200`)
		return map[string]any{"entries": entries}, err
	})
}

func jsonRows(ctx context.Context, tx *sql.Tx, statement string) ([]json.RawMessage, error) {
	rows, err := tx.QueryContext(ctx, statement)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []json.RawMessage{}
	for rows.Next() {
		var data []byte
		if err := rows.Scan(&data); err != nil {
			return nil, err
		}
		result = append(result, json.RawMessage(data))
	}
	return result, rows.Err()
}

type scopeKind struct {
	table, mapping, key, label string
	numeric, single            bool
}

var scopeKinds = map[string]scopeKind{
	"computers": {"computers", "computer_offices", "computer_id", "name", false, true},
	"accounts":  {"accounts", "account_offices", "account_id", "name", false, true},
	"cards":     {"phone_cards", "phone_card_offices", "card_id", "phone", true, false},
	"devices":   {"phone_devices", "phone_device_offices", "device_id", "name", false, false},
}

func (h Handler) EntityScopes(w http.ResponseWriter, r *http.Request) {
	h.read(w, r, func(ctx context.Context, tx *sql.Tx) (any, error) {
		kind := queryValue(r, "kind")
		if kind == "" {
			kind = "computers"
		}
		entity, ok := scopeKinds[kind]
		if !ok {
			return nil, accountwrite.Invalid("资料类型不正确")
		}
		after := queryValue(r, "after")
		if strings.ContainsRune(after, 0) {
			return nil, accountwrite.Invalid("资料编号不正确")
		}
		return listEntityScopes(ctx, tx, kind, entity, after)
	})
}

func listEntityScopes(ctx context.Context, tx *sql.Tx, kind string, entity scopeKind, after string) (map[string]any, error) {
	query := `SELECT a.id::text,a.` + entity.label + `,
COALESCE((SELECT json_agg(o.office_id ORDER BY o.office_id)::text FROM board_candidate.` + entity.mapping + ` o WHERE o.` + entity.key + `=a.id),'[]')
FROM board_candidate.` + entity.table + ` a WHERE a.id::text>$1 ORDER BY a.id::text LIMIT 101`
	rows, err := tx.QueryContext(ctx, query, after)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	list := []map[string]any{}
	last, next := "", ""
	for rows.Next() {
		if len(list) == 100 {
			next = last
			break
		}
		var id string
		var name sql.NullString
		var grants []byte
		if err := rows.Scan(&id, &name, &grants); err != nil {
			return nil, err
		}
		var offices []string
		if err := json.Unmarshal(grants, &offices); err != nil {
			return nil, err
		}
		slices.Sort(offices)
		version, err := scopeToken(kind, id, offices)
		if err != nil {
			return nil, err
		}
		var label any
		if name.Valid {
			label = name.String
		}
		var responseID any = id
		if entity.numeric {
			responseID, err = strconv.ParseInt(id, 10, 64)
			if err != nil {
				return nil, err
			}
		}
		list = append(list, map[string]any{"id": responseID, "name": label, "office_ids": offices, "scope_version": version})
		last = id
	}
	return map[string]any{"entities": list, "next_after": next}, rows.Err()
}

func scopeToken(kind, id string, offices []string) (string, error) {
	return recordwire.Version(map[string]any{"kind": kind, "id": id, "office_ids": offices})
}

func queryValue(r *http.Request, key string) string {
	for _, value := range r.URL.Query()[key] {
		if value != "" {
			return value
		}
	}
	return ""
}

func replyFailure(w http.ResponseWriter, err error) {
	var failure accountwrite.APIError
	if errors.As(err, &failure) {
		reply(w, failure.Status, map[string]string{"error": failure.Message})
	} else if errors.Is(err, session.ErrUnauthorized) {
		reply(w, http.StatusUnauthorized, map[string]string{"error": "请先登录工作台"})
	} else {
		reply(w, http.StatusServiceUnavailable, map[string]string{"error": "工作台暂时不可用"})
	}
}

func reply(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
