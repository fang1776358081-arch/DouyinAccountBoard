package accountwrite

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"time"

	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

type groupContextKey struct{}
type historyGroup struct{ action, id string }

// WithHistoryGroup lets nested record saves join a caller's logical batch.
// The caller must record the batch audit once after all saves succeed.
func WithHistoryGroup(ctx context.Context, action, group string) context.Context {
	return context.WithValue(ctx, groupContextKey{}, historyGroup{action, group})
}

func HistoryGroup(ctx context.Context) (action, group string) {
	value, _ := ctx.Value(groupContextKey{}).(historyGroup)
	return value.action, value.id
}

func NewGroup() (string, error) {
	var raw [16]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return "", err
	}
	raw[6] = raw[6]&0x0f | 0x40
	raw[8] = raw[8]&0x3f | 0x80
	return hex.EncodeToString(raw[:]), nil
}

func InsertHistory(ctx context.Context, tx *sql.Tx, actor session.User, action, group, entity string, before, after any, scope []string) error {
	beforeJSON, err := recordwire.Encode(before)
	if err != nil {
		return err
	}
	afterJSON, err := recordwire.Encode(after)
	if err != nil {
		return err
	}
	var scopeJSON any
	if len(scope) > 0 {
		raw, err := recordwire.Encode(scope)
		if err != nil {
			return err
		}
		scopeJSON = string(raw)
	}
	_, err = tx.ExecContext(ctx, `
INSERT INTO board_candidate.operations_history
    (group_id,at,actor_id,actor_name,action,entity,before_json,after_json,scope_offices)
VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)`, group, time.Now().Unix(), actor.ID, actor.Name, action, entity,
		string(beforeJSON), string(afterJSON), scopeJSON)
	return err
}

func Audit(ctx context.Context, tx *sql.Tx, actor session.User, action, target string) error {
	_, err := tx.ExecContext(ctx, `
INSERT INTO board_candidate.auth_audit(at,actor_id,actor_name,action,target)
VALUES($1,$2,$3,$4,$5)`, time.Now().Unix(), actor.ID, actor.Name, action, Truncate(target, 200))
	return err
}

func Truncate(value string, maxRunes int) string {
	for i := range value {
		if maxRunes == 0 {
			return value[:i]
		}
		maxRunes--
	}
	return value
}
