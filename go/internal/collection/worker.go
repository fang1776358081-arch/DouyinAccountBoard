package collection

import (
	"context"
	"database/sql"
	"encoding/json"
	"strings"
	"time"

	"douyinboard/internal/account"
	"douyinboard/internal/accountwrite"
	"douyinboard/internal/publishreview"
	"douyinboard/internal/session"
)

const (
	headlessSuffix = "（无界面）"
	leaseDuration  = 10 * time.Minute
)

var workerActor = session.User{Role: "owner", Name: "本机作品采集器"}

func heartbeat(ctx context.Context, tx *sql.Tx, body payload, now time.Time) (payload, error) {
	status := stringField(body, "status")
	switch status {
	case "待命", "正在采集", "等待手动登录或验证", "采集器异常":
	default:
		return nil, accountwrite.Invalid("采集器状态不正确")
	}
	graphical, ok := body["browser_control"].(bool)
	if !ok {
		return nil, accountwrite.Invalid("采集器状态不正确")
	}
	if !graphical {
		status += headlessSuffix
	}
	var seen float64
	var command string
	if err := tx.QueryRowContext(ctx, `SELECT seen,command FROM board_candidate.collection_worker WHERE id=1`).Scan(&seen, &command); err != nil {
		return nil, err
	}
	_, err := tx.ExecContext(ctx, `UPDATE board_candidate.collection_worker SET seen=$1,status=$2,command='' WHERE id=1`, float64(now.UnixNano())/1e9, status)
	if !onlineAt(seen, now) || (command == "open" && !graphical) || (command != "open" && command != "resume") {
		command = ""
	}
	return payload{"command": command}, err
}

type jobIdentity struct{ id, homepage, handle, state, startedAt string }

func (j jobIdentity) matches(record payload) bool {
	homepage, ok := normalizedHomepage(stringField(record, "homepage"))
	return ok && homepage == j.homepage && stringField(record, "douyin_id") == j.handle
}

func nextJob(ctx context.Context, tx *sql.Tx, _ payload, now time.Time) (payload, error) {
	result := payload{"job": nil}
	var paused bool
	if err := tx.QueryRowContext(ctx, `SELECT paused<>0 FROM board_candidate.collection_settings WHERE id=1`).Scan(&paused); err != nil || paused {
		return result, err
	}
	if _, err := tx.ExecContext(ctx, `UPDATE board_candidate.collection_jobs SET state='queued'
		WHERE starts_with(token,$1) AND state='running' AND started_at<=$2`, jobPrefix, now.Add(-leaseDuration).UTC().Format(time.RFC3339)); err != nil {
		return nil, err
	}
	var running bool
	if err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM board_candidate.collection_jobs WHERE starts_with(token,$1) AND state='running')`, jobPrefix).Scan(&running); err != nil || running {
		return result, err
	}
	for range maxBatch {
		var job jobIdentity
		err := tx.QueryRowContext(ctx, `SELECT j.account_id,j.homepage,j.douyin_id FROM board_candidate.collection_jobs j
			JOIN board_candidate.accounts a ON a.id=j.account_id WHERE starts_with(j.token,$1) AND j.state='queued'
			ORDER BY (j.started_at=''),a.computer_id,a.slot,j.account_id LIMIT 1`, jobPrefix).
			Scan(&job.id, &job.homepage, &job.handle)
		if noRow(err) {
			return result, nil
		}
		if err != nil {
			return nil, err
		}
		before, _, err := account.ReadScoped(ctx, tx, workerActor, job.id)
		if err != nil {
			return nil, err
		}
		if !job.matches(before) {
			if _, err := tx.ExecContext(ctx, `UPDATE board_candidate.collection_jobs SET state='cancelled' WHERE account_id=$1`, job.id); err != nil {
				return nil, err
			}
			continue
		}
		lease, err := newJobToken()
		if err != nil {
			return nil, err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE board_candidate.collection_jobs SET state='running',started_at=$2,token=$3
			WHERE account_id=$1`, job.id, now.UTC().Format(time.RFC3339), lease); err != nil {
			return nil, err
		}
		if err := updateState(ctx, tx, job.id, "采集中", "", now.In(beijing).Format(time.RFC3339)); err != nil {
			return nil, err
		}
		if err := recordWorkerChange(ctx, tx, "/api/collection/next", job.id, before); err != nil {
			return nil, err
		}
		result["job"] = payload{"account_id": job.id, "homepage": job.homepage, "douyin_id": job.handle, "token": lease}
		return result, nil
	}
	return result, nil
}

func complete(ctx context.Context, tx *sql.Tx, body payload, now time.Time) (payload, error) {
	id, lease := stringField(body, "account_id"), stringField(body, "token")
	if id == "" || len(id) > 256 || strings.ContainsRune(id, 0) || !strings.HasPrefix(lease, jobPrefix) || len(lease) != len(jobPrefix)+32 {
		return nil, accountwrite.Invalid("缺少有效的采集任务标识")
	}
	job := jobIdentity{id: id}
	err := tx.QueryRowContext(ctx, `SELECT homepage,douyin_id,state,started_at FROM board_candidate.collection_jobs WHERE account_id=$1 AND token=$2`, id, lease).
		Scan(&job.homepage, &job.handle, &job.state, &job.startedAt)
	if noRow(err) {
		return payload{"ignored": true}, nil
	}
	if err != nil {
		return nil, err
	}
	if job.state == "done" {
		return payload{"ok": true, "duplicate": true}, nil
	}
	if job.state != "running" {
		return payload{"ignored": true}, nil
	}
	started, err := time.Parse(time.RFC3339, job.startedAt)
	if err != nil || !now.Before(started.Add(leaseDuration)) {
		return payload{"ignored": true}, nil
	}
	before, _, err := account.ReadScoped(ctx, tx, workerActor, id)
	if err != nil {
		return nil, err
	}
	if !job.matches(before) {
		_, err := tx.ExecContext(ctx, `UPDATE board_candidate.collection_jobs SET state='cancelled' WHERE account_id=$1`, id)
		return payload{"ignored": true}, err
	}
	message := stringField(body, "error")
	failed := message != ""
	var total *int64
	if failed {
		blocked, _ := body["blocked"].(bool)
		state := "采集失败"
		message = safeMessage(message)
		if blocked {
			state = "需要验证"
			if _, err := tx.ExecContext(ctx, `UPDATE board_candidate.collection_settings SET paused=1,auto_enabled=0,pause_reason=$1 WHERE id=1`, message); err != nil {
				return nil, err
			}
		}
		if err := updateState(ctx, tx, id, state, message, ""); err != nil {
			return nil, err
		}
	} else {
		value, valid := body["count"].(json.Number)
		count, err := value.Int64()
		if !valid || err != nil || count < 0 || count > 10000000 {
			return nil, accountwrite.Invalid("作品数量必须是有效的非负整数")
		}
		source, ok := normalizedHomepage(stringField(body, "source_url"))
		if !ok || source == "" || (job.handle != "" && stringField(body, "matched_douyin_id") != job.handle) ||
			(job.homepage != "" && (job.handle == "" || strings.HasPrefix(job.homepage, f2HomepagePrefix)) && source != job.homepage) {
			return nil, accountwrite.Invalid("采集结果与账号身份或已确认主页不一致，未更新数量")
		}
		if _, err := tx.ExecContext(ctx, `UPDATE board_candidate.accounts SET homepage=$2,work_count=$3,last_collected=$4,
			collection_state='采集成功',collection_error='' WHERE id=$1`, id, source, count, now.In(beijing).Format(time.RFC3339)); err != nil {
			return nil, err
		}
		total = &count
	}
	if raw := stringField(body, "sampled_at"); raw != "" {
		sampledAt, err := time.Parse(time.RFC3339Nano, raw)
		if err != nil || sampledAt.After(now) || sampledAt.Before(started) {
			return nil, accountwrite.Invalid("采样时间无效，未写入采集结果")
		}
		if err := publishreview.IngestCollectionSample(ctx, tx, id, job.handle, lease, sampledAt, total, now); err != nil {
			return nil, err
		}
	}
	if _, err := tx.ExecContext(ctx, `UPDATE board_candidate.collection_jobs SET state='done' WHERE account_id=$1`, id); err != nil {
		return nil, err
	}
	if err := recordWorkerChange(ctx, tx, "/api/collection/result", id, before); err != nil {
		return nil, err
	}
	return payload{"ok": true}, nil
}

func safeMessage(message string) string {
	const repeated = "；连续3次失败，队列暂停，请检查后继续"
	base := strings.TrimSuffix(message, repeated)
	switch base {
	case "抖音搜索需要安全验证，请在采集浏览器中手动完成", "抖音登录已失效，请在独立采集浏览器中重新登录",
		"没有搜索到抖音号完全一致的账号，请核对真实抖音号", "账号身份校验失败，未更新数量",
		"公开作品数量不可用或账号私密", "采集程序未返回有效报告，请检查运行环境和浏览器",
		"抖音采集超时，请检查采集器网络", "采集结果校验失败，未更新数量":
		return message
	default:
		return "采集未完成，请检查本机采集器和网络"
	}
}
