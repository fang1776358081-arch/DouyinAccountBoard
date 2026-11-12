// Package session reads the existing workbench's PostgreSQL sessions.
package session

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"slices"
	"time"
)

var ErrUnauthorized = errors.New("请先登录工作台")

const cookieName = "board_session"

type User struct {
	ID                   int64    `json:"id"`
	Username             string   `json:"username"`
	Name                 string   `json:"name"`
	Role                 string   `json:"role"`
	Active               bool     `json:"active"`
	Permissions          []string `json:"permissions"`
	CSRF                 string   `json:"csrf"`
	AuthorizationVersion int64    `json:"authorization_version"`
	OfficeIDs            []string `json:"office_ids"`
	ScopeMode            string   `json:"scope_mode"`
	ExpiresAt            int64    `json:"-"`
}

// Global reports access to all offices. It does not grant operation permissions.
func (u User) Global() bool {
	return u.Role == "owner" || u.Role == "admin"
}

func (u User) Has(permission string) bool {
	if permission == "owner" {
		return u.Role == "owner"
	}
	return slices.Contains(u.Permissions, permission)
}

type Store struct{ DB *sql.DB }

type Queryer interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}

func (s Store) Authenticate(ctx context.Context, r *http.Request) (User, error) {
	return AuthenticateWith(ctx, s.DB, r)
}

// AuthenticateWith also accepts the transaction used by a protected write.
func AuthenticateWith(ctx context.Context, q Queryer, r *http.Request) (User, error) {
	token := requestCookie(r, cookieName)
	if token == "" {
		return User{}, ErrUnauthorized
	}
	return authenticateToken(ctx, q, token, time.Now().Unix())
}

const sessionQuery = `
SELECT u.id, u.username, u.name, u.role, u.permissions, s.csrf,
       u.authorization_version, s.expires_at,
       COALESCE((
           SELECT json_agg(o.id ORDER BY o.id)::text
           FROM offices AS o
           WHERE o.active = 1 AND (
               u.role IN ('owner', 'admin') OR EXISTS (
                   SELECT 1 FROM user_office_grants AS g
                   WHERE g.user_id = u.id AND g.office_id = o.id
               )
           )
       ), '[]')
FROM auth_sessions AS s
JOIN auth_users AS u ON u.id = s.user_id
WHERE s.token_hash = $1 AND s.expires_at > $2 AND u.active = 1
`

func authenticateToken(ctx context.Context, q Queryer, token string, now int64) (User, error) {
	var user User
	var permissions, offices string
	err := q.QueryRowContext(ctx, sessionQuery, digest(token), now).Scan(
		&user.ID, &user.Username, &user.Name, &user.Role, &permissions,
		&user.CSRF, &user.AuthorizationVersion, &user.ExpiresAt, &offices,
	)
	if errors.Is(err, sql.ErrNoRows) {
		return User{}, ErrUnauthorized
	}
	if err != nil {
		return User{}, fmt.Errorf("read session: %w", err)
	}
	if _, valid := roles[user.Role]; !valid || user.CSRF == "" {
		return User{}, ErrUnauthorized
	}
	user.Active = true
	user.Permissions = permissionsForRole(user.Role, permissions)
	if err := json.Unmarshal([]byte(offices), &user.OfficeIDs); err != nil {
		return User{}, fmt.Errorf("decode office grants: %w", err)
	}
	user.ScopeMode = "assigned"
	if user.Global() {
		user.ScopeMode = "all"
	}
	return user, nil
}

func permissionsForRole(role, encoded string) []string {
	if role == "owner" {
		return slices.Clone(permissionOrder)
	}
	list := []string{}
	var stored []any
	if err := json.Unmarshal([]byte(encoded), &stored); err != nil {
		return list
	}
	for _, value := range stored {
		if permission, ok := value.(string); ok {
			list = append(list, permission)
		}
	}
	return list
}

func requestCookie(r *http.Request, name string) string {
	cookies := r.CookiesNamed(name)
	if len(cookies) != 1 || len(cookies[0].Value) > 128 {
		return ""
	}
	return cookies[0].Value
}

func digest(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}
