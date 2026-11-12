package exchange

import (
	"context"
	"database/sql"
	"time"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

const officeThreeID = "three"

// A preview fingerprints only business records and their authoritative
// relationships. Login sessions and unrelated request receipts may advance.
var officeGuardTables = []string{
	"accounts", "computers", "daily", "phone_cards", "phone_devices", "publishing_layout",
	"nurture_records", "phone_units", "phone_card_placements", "phone_account_access",
	"device_cases", "device_workflow_events", "offices", "account_offices", "computer_offices",
	"phone_card_offices", "phone_device_offices", "publishing_layout_state", "publishing_layout_links",
}

func officeGuard(ctx context.Context, tx *sql.Tx, exceptNames bool) (string, error) {
	sums := map[string]string{}
	for _, table := range officeGuardTables {
		var present bool
		if err := tx.QueryRowContext(ctx, "SELECT to_regclass($1) IS NOT NULL", "board_candidate."+table).Scan(&present); err != nil {
			return "", err
		}
		if !present {
			continue
		}
		json := "to_jsonb(r)"
		if exceptNames && table == "accounts" {
			json += "-'name'"
		}
		statement := "SELECT md5(COALESCE(jsonb_agg(value ORDER BY value),'[]'::jsonb)::text) FROM (SELECT " +
			json + " AS value FROM board_candidate." + table + " r) AS rows"
		var sum string
		if err := tx.QueryRowContext(ctx, statement).Scan(&sum); err != nil {
			return "", err
		}
		sums[table] = sum
	}
	return recordwire.Version(sums)
}

func AuthorizeOfficeThree(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) error {
	if actor.Role != "owner" {
		return accountwrite.Forbidden("当前账号没有此操作权限")
	}
	if body["office_id"] != officeThreeID {
		return accountwrite.Invalid("此双表导入仅允许办公室三，不能导入其他办公室或待分配区")
	}
	_, err := requestedScope(ctx, tx, actor, "accounts", body)
	return err
}

func today() string {
	return time.Now().In(time.FixedZone("UTC+8", 8*3600)).Format(time.DateOnly)
}

func limitRunes(value string, limit int) string {
	runes := []rune(value)
	return string(runes[:min(len(runes), limit)])
}

func noteReload(ctx context.Context, tx *sql.Tx, actor session.User, action string) error {
	_, err := tx.ExecContext(ctx, `INSERT INTO operations_history(group_id,at,actor_id,actor_name,action,entity)
		VALUES($1,$2,$3,$4,$5,'sync:reload')`, newHexID(), time.Now().Unix(), actor.ID, actor.Name, action)
	return err
}
