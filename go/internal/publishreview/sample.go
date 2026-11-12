package publishreview

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"errors"
	"fmt"
	"time"
)

// ingestSample only stores authenticated evidence. It has no collector or queue.
func ingestSample(ctx context.Context, tx *sql.Tx, account account, requestID string, body row, current time.Time) (row, error) {
	var failed bool
	if raw, exists := body["failed"]; exists {
		var ok bool
		failed, ok = raw.(bool)
		if !ok {
			return nil, invalid("成功采样须提供有效的累计作品总数")
		}
	}
	if account.Handle != "" && !failed && body["matched_douyin_id"] != account.Handle {
		return nil, invalid("采样结果与账号当前真实抖音号不一致，未写入")
	}
	sampledAt, ok := body["sampled_at"].(string)
	if !ok {
		return nil, invalid("缺少实际采样时间")
	}
	instant, ok := parseSampleTime(sampledAt)
	if !ok {
		return nil, invalid("采样时间须包含时区，不能用上传时间代替")
	}
	if instant.After(current) {
		return nil, invalid("采样时间不能在未来")
	}
	var total sql.NullInt64
	status := "failed"
	if !failed {
		value, ok := number(body["total"])
		if !ok || value < 0 || value > 10000000 {
			return nil, invalid("成功采样须提供有效的累计作品总数")
		}
		total = sql.NullInt64{Int64: value, Valid: true}
		status = "ok"
	}
	return storeSample(ctx, tx, account, "external:"+requestID, "external", status, total, instant, current)
}

// IngestCollectionSample records evidence from an authenticated, leased worker
// in the same transaction as the latest count. Older databases retain the count.
func IngestCollectionSample(ctx context.Context, tx *sql.Tx, accountID, handle, lease string, instant time.Time, count *int64, current time.Time) error {
	ready, err := available(ctx, tx)
	if err != nil || !ready {
		return err
	}
	var total sql.NullInt64
	status := "failed"
	if count != nil {
		total = sql.NullInt64{Int64: *count, Valid: true}
		status = "ok"
	}
	id := fmt.Sprintf("collection:%x", sha256.Sum256([]byte(lease)))
	_, err = storeSample(ctx, tx, account{ID: accountID, Handle: handle}, id, "collection", status, total, instant, current)
	return err
}

func storeSample(ctx context.Context, tx *sql.Tx, account account, id, source, status string, total sql.NullInt64, instant, current time.Time) (row, error) {
	instant = instant.In(beijing)
	day, stamp := instant.Format(time.DateOnly), instant.Format(timestamp)
	previous := instant.AddDate(0, 0, -1)
	if previous.Year() < 1 || instant.Year() > 9999 {
		return nil, invalid("采样时间超出业务日期范围")
	}
	var oldAccount, oldDay, oldStamp, oldStatus, oldSource, oldIdentity string
	var oldTotal sql.NullInt64
	err := tx.QueryRowContext(ctx, `SELECT account_id,day,sampled_at,total,result,source,account_identity FROM board_candidate.publish_review_samples WHERE id=$1`, id).
		Scan(&oldAccount, &oldDay, &oldStamp, &oldTotal, &oldStatus, &oldSource, &oldIdentity)
	if err == nil {
		if oldAccount != account.ID || oldDay != day || oldStamp != stamp || oldTotal != total || oldStatus != status || oldSource != source || oldIdentity != account.Handle {
			return nil, conflict("同一采样标识的内容不同，未覆盖原采样")
		}
		return row{"ok": true, "duplicate": true}, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO board_candidate.publish_review_samples(id,account_id,day,sampled_at,total,result,source,account_identity)
		VALUES($1,$2,$3,$4,$5,$6,$7,$8)`, id, account.ID, day, stamp, total, status, source, account.Handle)
	if err != nil {
		return nil, err
	}
	// The first attempt seals the latest successful observation from yesterday.
	// A late import must never replace an already sealed baseline.
	_, err = tx.ExecContext(ctx, `INSERT INTO board_candidate.publish_review_baselines(account_id,day,sample_id,sealed_at)
		SELECT $1,$2,id,$3 FROM board_candidate.publish_review_samples WHERE account_id=$1 AND day=$4 AND result='ok'
		ORDER BY sampled_at DESC,id DESC LIMIT 1 ON CONFLICT(account_id,day) DO NOTHING`, account.ID, day, current.In(beijing).Format(timestamp), previous.Format(time.DateOnly))
	return row{"ok": true, "duplicate": false}, err
}

func parseSampleTime(value string) (time.Time, bool) {
	for _, format := range []string{
		time.RFC3339Nano, "2006-01-02 15:04:05Z07:00", "2006-01-02T15:04Z07:00", "2006-01-02 15:04Z07:00",
		"2006-01-02T15:04:05Z0700", "2006-01-02 15:04:05Z0700", "2006-01-02T15:04:05Z07", "2006-01-02 15:04:05Z07",
	} {
		if parsed, err := time.Parse(format, value); err == nil && parsed.Year() > 0 {
			return parsed.Truncate(time.Microsecond), true
		}
	}
	return time.Time{}, false
}
