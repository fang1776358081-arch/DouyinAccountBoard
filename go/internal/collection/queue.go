package collection

import (
	"context"
	"database/sql"
	"errors"
	"net/http"
	"strings"
	"time"

	"douyinboard/internal/account"
	"douyinboard/internal/accountwrite"
	"douyinboard/internal/session"
)

var beijing = time.FixedZone("Asia/Shanghai", 8*60*60)

const f2HomepagePrefix = "https://www.douyin.com/user/MS4w"

func normalizedHomepage(raw string) (string, bool) {
	homepage, err := account.NormalizeHomepage(raw)
	return homepage, err == nil
}

func idsFrom(body payload) ([]string, error) {
	values, ok := body["ids"].([]any)
	if !ok || len(values) == 0 || len(values) > maxBatch {
		return nil, accountwrite.Invalid("请选择 1 至 2000 个账号")
	}
	ids := make([]string, 0, len(values))
	seen := make(map[string]bool, len(values))
	for _, raw := range values {
		id, ok := raw.(string)
		if !ok || len(id) == 0 || len(id) > 256 || strings.ContainsRune(id, 0) {
			return nil, accountwrite.Invalid("采集账号标识格式不正确")
		}
		if !seen[id] {
			seen[id] = true
			ids = append(ids, id)
		}
	}
	return ids, nil
}

func authorizeEnqueue(ctx context.Context, tx *sql.Tx, actor session.User, body payload) error {
	if !actor.Has("collection_manage") {
		return accountwrite.Forbidden("需要作品采集管理权限")
	}
	ids, err := idsFrom(body)
	if err != nil {
		return err
	}
	var visible int
	err = tx.QueryRowContext(ctx, `SELECT count(*) FROM board_candidate.accounts a
		LEFT JOIN board_candidate.account_offices o ON o.account_id=a.id
		WHERE a.id=ANY($1::text[]) AND ($2 OR o.office_id=ANY($3::text[]))`, ids, actor.Global(), actor.OfficeIDs).Scan(&visible)
	if err == nil && visible != len(ids) {
		return accountwrite.Forbidden("所选账号中有不属于当前授权范围的资料")
	}
	return err
}

func PermitReceipt(ctx context.Context, tx *sql.Tx, actor session.User, result payload) error {
	if result["ids"] == nil && actor.Has("collection_manage") {
		return nil
	}
	return authorizeEnqueue(ctx, tx, actor, result)
}

func unavailable(message string) error {
	return accountwrite.APIError{Status: http.StatusServiceUnavailable, Message: message, Code: "COLLECTION_UNAVAILABLE"}
}

func enqueue(ctx context.Context, tx *sql.Tx, actor session.User, body payload) (payload, error) {
	ids, err := idsFrom(body)
	if err != nil {
		return nil, err
	}
	var paused bool
	var seen float64
	if err := tx.QueryRowContext(ctx, `SELECT s.paused<>0,w.seen FROM board_candidate.collection_settings s
		JOIN board_candidate.collection_worker w ON w.id=s.id WHERE s.id=1`).Scan(&paused, &seen); err != nil {
		return nil, err
	}
	if paused {
		return nil, unavailable("采集队列已暂停，请联系有权限的管理员继续")
	}
	if !onlineAt(seen, time.Now()) {
		return nil, unavailable("采集器离线，请启动已配置的本机采集器")
	}
	group, err := accountwrite.NewGroup()
	if err != nil {
		return nil, err
	}
	var queued, missing, invalid, duplicate int
	for _, id := range ids {
		before, _, err := account.ReadScoped(ctx, tx, actor, id)
		if err != nil {
			return nil, err
		}
		homepage, ok := normalizedHomepage(stringField(before, "homepage"))
		handle := stringField(before, "douyin_id")
		// F2 can use a canonical profile URL directly, or search by the exact handle.
		if !ok || (homepage != "" && handle == "" && !strings.HasPrefix(homepage, f2HomepagePrefix)) {
			invalid++
			continue
		}
		if homepage == "" && handle == "" {
			missing++
			continue
		}
		token, err := newJobToken()
		if err != nil {
			return nil, err
		}
		change, err := tx.ExecContext(ctx, `INSERT INTO board_candidate.collection_jobs(account_id,homepage,douyin_id,token,state)
			VALUES($1,$2,$3,$4,'queued') ON CONFLICT(account_id) DO UPDATE SET homepage=excluded.homepage,douyin_id=excluded.douyin_id,
			token=excluded.token,state='queued',started_at='' WHERE NOT (starts_with(collection_jobs.token,$5)
			AND collection_jobs.state IN ('queued','running') AND collection_jobs.homepage=$2 AND collection_jobs.douyin_id=$3)`, id, homepage, handle, token, jobPrefix)
		if err != nil {
			return nil, err
		}
		affected, err := change.RowsAffected()
		if err != nil {
			return nil, err
		}
		if affected == 0 {
			duplicate++
			continue
		}
		if err := updateState(ctx, tx, id, "等待采集", "", ""); err != nil {
			return nil, err
		}
		if err := recordChange(ctx, tx, actor, EnqueuePath, group, id, before); err != nil {
			return nil, err
		}
		queued++
	}
	if queued > 0 {
		if err := accountwrite.Audit(ctx, tx, actor, EnqueuePath, "手动提交作品采集"); err != nil {
			return nil, err
		}
	}
	return payload{"queued": queued, "missing": missing, "invalid": invalid, "duplicate": duplicate, "ids": ids}, nil
}

func authorizeControl(_ context.Context, _ *sql.Tx, actor session.User, _ payload) error {
	if !actor.Has("collection_manage") || !actor.Global() {
		return accountwrite.Forbidden("需要全局作品采集管理权限")
	}
	return nil
}

func control(ctx context.Context, tx *sql.Tx, actor session.User, body payload) (payload, error) {
	command := ""
	for key, value := range body {
		switch key {
		case "_expected_versions":
			continue
		case "pause", "resume", "cancel_waiting", "open_browser":
			if value != true || command != "" {
				return nil, accountwrite.Invalid("每次只可选择一项采集操作")
			}
			command = key
		default:
			return nil, accountwrite.Invalid("只支持手动采集与队列控制")
		}
	}
	var cancelled int
	var err error
	switch command {
	case "pause":
		_, err = tx.ExecContext(ctx, `UPDATE board_candidate.collection_settings SET auto_enabled=0,paused=1,
			pause_reason='已手动暂停，当前采集完成后停止' WHERE id=1`)
	case "resume":
		_, err = tx.ExecContext(ctx, `UPDATE board_candidate.collection_settings SET auto_enabled=0,paused=0,pause_reason='' WHERE id=1;
			UPDATE board_candidate.collection_worker SET command='resume' WHERE id=1`)
	case "open_browser":
		var seen float64
		var status string
		if err := tx.QueryRowContext(ctx, `SELECT seen,status FROM board_candidate.collection_worker WHERE id=1`).Scan(&seen, &status); err != nil {
			return nil, err
		}
		if !onlineAt(seen, time.Now()) || strings.HasSuffix(status, headlessSuffix) {
			return nil, unavailable("当前采集器未在线或没有图形界面，请在采集电脑登录抖音")
		}
		_, err = tx.ExecContext(ctx, `UPDATE board_candidate.collection_worker SET command='open' WHERE id=1`)
	case "cancel_waiting":
		cancelled, err = cancelWaiting(ctx, tx, actor)
	default:
		return nil, accountwrite.Invalid("请选择一项采集操作")
	}
	if err != nil {
		return nil, err
	}
	if err := accountwrite.Audit(ctx, tx, actor, SettingsPath, command); err != nil {
		return nil, err
	}
	return payload{"ok": true, "cancelled": cancelled}, nil
}

func cancelWaiting(ctx context.Context, tx *sql.Tx, actor session.User) (int, error) {
	rows, err := tx.QueryContext(ctx, `UPDATE board_candidate.collection_jobs SET state='cancelled'
		WHERE starts_with(token,$1) AND state='queued' RETURNING account_id`, jobPrefix)
	if err != nil {
		return 0, err
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return 0, err
		}
		ids = append(ids, id)
	}
	if err := rows.Err(); err != nil {
		return 0, err
	}
	group, err := accountwrite.NewGroup()
	if err != nil {
		return 0, err
	}
	for _, id := range ids {
		before, _, err := account.ReadScoped(ctx, tx, actor, id)
		if err != nil {
			return 0, err
		}
		if before["collection_state"] != "等待采集" {
			continue
		}
		if err := updateState(ctx, tx, id, "已取消", "", ""); err != nil {
			return 0, err
		}
		if err := recordChange(ctx, tx, actor, SettingsPath, group, id, before); err != nil {
			return 0, err
		}
	}
	return len(ids), nil
}

func updateState(ctx context.Context, tx *sql.Tx, id, state, message, attempted string) error {
	_, err := tx.ExecContext(ctx, `UPDATE board_candidate.accounts SET collection_state=$2,collection_error=$3,
		last_attempted=COALESCE(NULLIF($4,''),last_attempted) WHERE id=$1`, id, state, message, attempted)
	return err
}

func recordChange(ctx context.Context, tx *sql.Tx, actor session.User, action, group, id string, before payload) error {
	after, office, err := account.ReadScoped(ctx, tx, actor, id)
	if err != nil {
		return err
	}
	scope := []string{}
	if office.Valid {
		scope = append(scope, office.String)
	}
	return accountwrite.InsertHistory(ctx, tx, actor, action, group, "account:"+id, before, after, scope)
}

func recordWorkerChange(ctx context.Context, tx *sql.Tx, action, id string, before payload) error {
	group, err := accountwrite.NewGroup()
	if err != nil {
		return err
	}
	return recordChange(ctx, tx, workerActor, action, group, id, before)
}

func stringField(row payload, key string) string {
	value, _ := row[key].(string)
	return value
}

func newJobToken() (string, error) {
	token, err := accountwrite.NewGroup()
	if err != nil {
		return "", err
	}
	return jobPrefix + token, nil
}

func onlineAt(seen float64, now time.Time) bool {
	delta := float64(now.UnixNano())/1e9 - seen
	return delta >= 0 && delta < 30
}

func noRow(err error) bool { return errors.Is(err, sql.ErrNoRows) }
