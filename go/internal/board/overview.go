package board

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"strings"
	"time"
)

func enrichLifecycle(accounts []row, today string) {
	lookup := byID(accounts)
	children := make(map[string][]row)
	for _, account := range accounts {
		if parent := str(account["parent_account_id"]); parent != "" {
			children[parent] = append(children[parent], picked(account, "id", "name"))
		}
	}
	current, _ := parseDay(today)
	for _, account := range accounts {
		own := children[str(account["id"])]
		if own == nil {
			own = []row{}
		}
		account["child_accounts"] = own
		account["parent_name"] = ""
		if parent := lookup[str(account["parent_account_id"])]; parent != nil {
			account["parent_name"] = parent["name"]
		}
		aux := str(account["auxiliary_state"])
		if len(own) > 0 {
			aux = "已注册"
		}
		account["effective_auxiliary_state"] = aux
		account["mature_on"], account["days_remaining"] = "", nil
		remaining := int64(0)
		if registered, ok := parseDay(str(account["registered_on"])); ok {
			mature := registered.AddDate(0, 0, 30)
			if mature.Year() <= 9999 {
				remaining = max(0, (mature.Unix()-current.Unix())/86400)
				account["mature_on"], account["days_remaining"] = mature.Format(time.DateOnly), remaining
			}
		}
		switch {
		case account["account_type"] == "小号":
			account["lifecycle"] = "小号"
		case account["account_type"] != "大号":
			account["lifecycle"] = "账号类型待确认"
		case aux == "已注册":
			account["lifecycle"] = "已注册辅助账号"
		case aux == "暂无法注册":
			account["lifecycle"] = "暂无法注册"
		case account["days_remaining"] == nil:
			account["lifecycle"] = "注册日期待补充"
		case remaining > 0:
			account["lifecycle"] = "未满30天"
		case aux == "未注册":
			account["lifecycle"] = "满30天未注册"
		default:
			account["lifecycle"] = "满30天注册情况待确认"
		}
	}
}

func publishingLayout(ctx context.Context, tx *sql.Tx, scope *scopeMap) (row, error) {
	result := row{"enabled": false, "cells": []row{}}
	var anyComputer bool
	err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM board_candidate.computers c
		WHERE $1 OR EXISTS(SELECT 1 FROM board_candidate.computer_offices o WHERE o.computer_id=c.id AND o.office_id=ANY($2::text[])))`,
		scope.actor.Global(), scope.actor.OfficeIDs).Scan(&anyComputer)
	if err != nil || !anyComputer {
		return result, err
	}
	var batch string
	err = tx.QueryRowContext(ctx, `SELECT batch FROM board_candidate.publishing_layout_state WHERE id=1`).Scan(&batch)
	if errors.Is(err, sql.ErrNoRows) {
		return result, nil
	}
	if err != nil {
		return nil, err
	}
	cells, err := selectRows(ctx, tx, `SELECT to_jsonb(p)-'batch'||jsonb_build_object('manual_link',l.batch IS NOT NULL)
		FROM board_candidate.publishing_layout p
		LEFT JOIN board_candidate.publishing_layout_links l ON (l.batch,l.computer_id,l.slot)=(p.batch,p.computer_id,p.slot)
		WHERE p.batch=$1 AND ($2 OR (
			EXISTS(SELECT 1 FROM board_candidate.computer_offices o WHERE o.computer_id=p.computer_id AND o.office_id=ANY($3::text[]))
			AND (p.account_id IS NULL OR EXISTS(SELECT 1 FROM board_candidate.account_offices o WHERE o.account_id=p.account_id AND o.office_id=ANY($3::text[])))
		)) ORDER BY CASE WHEN p.computer_id LIKE 'office3:%' THEN 1 ELSE 0 END,`+integerOrder("replace(p.computer_id,'office3:','')")+`,p.slot`,
		batch, scope.actor.Global(), scope.actor.OfficeIDs)
	if err != nil {
		return nil, err
	}
	return row{"enabled": true, "cells": cells, "batch": batch}, nil
}

func collectionOverview(ctx context.Context, tx *sql.Tx, scope *scopeMap, accounts []row, jobPrefix string) (row, error) {
	settings, err := selectRows(ctx, tx, `SELECT to_jsonb(s) FROM board_candidate.collection_settings s WHERE id=1`)
	if err != nil {
		return nil, err
	}
	result := row{"auto_enabled": 0, "paused": 0, "pause_reason": ""}
	if len(settings) != 0 {
		result = settings[0]
	}
	var waiting int64
	err = tx.QueryRowContext(ctx, `SELECT COUNT(*) FROM board_candidate.collection_jobs j
		WHERE j.state IN ('queued','running') AND ($3='' OR starts_with(j.token,$3)) AND ($1 OR EXISTS(SELECT 1 FROM board_candidate.account_offices o
		WHERE o.account_id=j.account_id AND o.office_id=ANY($2::text[])))`, scope.actor.Global(), scope.actor.OfficeIDs, jobPrefix).Scan(&waiting)
	if err != nil {
		return nil, err
	}
	var linked, done, failed int64
	if accounts == nil {
		if err := tx.QueryRowContext(ctx, `SELECT count(*) FILTER (WHERE homepage<>'' OR douyin_id<>''),
			count(*) FILTER (WHERE collection_state='采集成功'),
			count(*) FILTER (WHERE collection_state IN ('采集失败','需要验证'))
			FROM board_candidate.accounts a WHERE $1 OR EXISTS(SELECT 1 FROM board_candidate.account_offices o
			WHERE o.account_id=a.id AND o.office_id=ANY($2::text[]))`, scope.actor.Global(), scope.actor.OfficeIDs).
			Scan(&linked, &done, &failed); err != nil {
			return nil, err
		}
	} else {
		for _, account := range accounts {
			if str(account["homepage"]) != "" || str(account["douyin_id"]) != "" {
				linked++
			}
			switch account["collection_state"] {
			case "采集成功":
				done++
			case "采集失败", "需要验证":
				failed++
			}
		}
	}
	result["enabled"], result["worker_online"], result["manual_only"], result["browser_control"] = jobPrefix != "", false, true, false
	result["auto_enabled"] = false
	result["worker_status"] = "采集暂未启用；已有数量、时间、错误和任务记录保留"
	if jobPrefix != "" {
		var seen float64
		var status string
		if err := tx.QueryRowContext(ctx, `SELECT seen,status FROM board_candidate.collection_worker WHERE id=1`).Scan(&seen, &status); err != nil {
			return nil, err
		}
		age := float64(time.Now().UnixNano())/1e9 - seen
		online := age >= 0 && age < 30
		result["worker_online"], result["browser_control"] = online, online && !strings.HasSuffix(status, "（无界面）")
		result["worker_status"] = "本机采集器离线"
		if online {
			result["worker_status"] = status
		}
		if result["pause_reason"] == "collection-disabled" || !scope.actor.Global() {
			result["pause_reason"] = "等待管理员继续采集"
		}
	}
	result["waiting"], result["linked"], result["done"], result["failed"] = waiting, linked, done, failed
	return result, nil
}

var verificationStates = []string{"待检查", "无需核对，可使用", "核对处理中", "核对已通过"}

func nurtureOverview(ctx context.Context, tx *sql.Tx, scope *scopeMap, accounts []row, today string, withImages bool) (row, error) {
	visibility := `($1 OR EXISTS(SELECT 1 FROM board_candidate.account_offices o WHERE o.account_id=n.account_id AND o.office_id=ANY($2::text[])))`
	records, err := selectRows(ctx, tx, `SELECT to_jsonb(n) FROM board_candidate.nurture_records n WHERE `+visibility,
		scope.actor.Global(), scope.actor.OfficeIDs)
	if err != nil {
		return nil, err
	}
	byAccount := make(map[string][]row)
	if withImages && len(records) > 0 {
		images, err := selectRows(ctx, tx, `SELECT jsonb_build_object('id',n.id,'account_id',n.account_id,'cycle',n.cycle,'day',n.day,'actor',n.actor)
			FROM board_candidate.nurture_images n WHERE `+visibility+` ORDER BY n.account_id,n.cycle,n.day,n.id`, scope.actor.Global(), scope.actor.OfficeIDs)
		if err != nil {
			return nil, err
		}
		for _, image := range images {
			id := str(image["account_id"])
			delete(image, "account_id")
			byAccount[id] = append(byAccount[id], image)
		}
	}
	lookup := byID(accounts)
	current, _ := parseDay(today)
	for _, record := range records {
		id := str(record["account_id"])
		nurtureFields(record, str(lookup[id]["status"]), current)
		if withImages {
			currentImages, pastImages := make([]row, 0), make([]row, 0)
			for _, image := range byAccount[id] {
				if image["cycle"] == record["cycle"] {
					currentImages = append(currentImages, picked(image, "id", "day", "actor"))
				} else {
					pastImages = append(pastImages, image)
				}
			}
			record["images"], record["past_images"] = currentImages, pastImages
		}
	}
	return row{"records": records, "today": today, "verification_states": verificationStates}, nil
}

func nurtureFields(record row, status string, current time.Time) {
	days := number(record["banked_days"])
	start, active := parseDay(str(record["active_since"]))
	due := ""
	if record["workflow"] == "运行" && active {
		due = start.AddDate(0, 0, int(max(0, 7-days))).Format(time.DateOnly)
		days += max(0, (current.Unix()-start.Unix())/86400)
	}
	var stage string
	switch {
	case str(record["completed_on"]) != "":
		stage = "已结束"
	case status != "正常" && status != "待确认" && status != "养号7天":
		stage = "状态待核对"
	case record["workflow"] == "暂停":
		stage = "养号暂停"
	case record["verification_state"] == "核对处理中":
		stage = "核对处理中"
	case record["verification_state"] == "待检查":
		stage = "待检查"
	case record["workflow"] == "待开始":
		stage = "待开始养号"
	case days >= 7:
		stage = "到期待转发布"
	default:
		stage = "养号中"
	}
	dayDates := make([]string, 0, 7)
	var periods []json.RawMessage
	_ = json.Unmarshal([]byte(str(record["periods"])), &periods)
	for _, raw := range periods {
		var period struct{ Start, End string }
		if json.Unmarshal(raw, &period) != nil {
			continue
		}
		begin, beginOK := parseDay(period.Start)
		end, endOK := parseDay(period.End)
		for day := begin; beginOK && endOK && day.Before(end) && len(dayDates) < 7; day = day.AddDate(0, 0, 1) {
			dayDates = append(dayDates, day.Format(time.DateOnly))
		}
	}
	for day := start; active && len(dayDates) < 7; day = day.AddDate(0, 0, 1) {
		dayDates = append(dayDates, day.Format(time.DateOnly))
	}
	record["elapsed"], record["remaining_days"], record["due_on"], record["stage"], record["day_dates"] = days, max(0, 7-days), due, stage, dayDates
}

func applySummary(result row) {
	result["workspace_view"] = "summary"
	result["computers"] = pickRows(entityRows(result, "computers"), "id", "office_ids")
	accounts := entityRows(result, "accounts")
	summary := make([]row, 0, len(accounts))
	for _, account := range accounts {
		item := picked(account, "id", "name", "douyin_id", "status", "lifecycle", "published", "computer_id", "nurture_started",
			"registration_phone", "phone_device_id", "office_ids", "office_group")
		item["device_access"] = nil
		if access, ok := account["device_access"].(row); ok && access != nil {
			item["device_access"] = picked(access, "login_state", "recovery_state")
		}
		summary = append(summary, item)
	}
	result["accounts"] = summary
	cards, devices := result["phone_cards"].(row), result["phone_devices"].(row)
	result["phone_cards"] = row{"cards": pickRows(entityRows(cards, "cards"), "id", "phone", "owner", "code_state", "return_state", "return_due", "code_note", "status", "office_ids")}
	result["phone_devices"] = row{"devices": pickRows(entityRows(devices, "devices"), "id", "name", "state", "attention", "office_ids")}
	layout, nurture, workflow := result["publishing_layout"].(row), result["nurture"].(row), result["device_workflow"].(row)
	result["publishing_layout"] = row{"enabled": layout["enabled"], "cells": pickRows(entityRows(layout, "cells"), "account_id", "label", "reason", "source_cell", "office", "computer_id", "slot")}
	result["nurture"] = row{"today": nurture["today"], "records": pickRows(entityRows(nurture, "records"), "account_id", "completed_on", "workflow", "stage", "started_on", "verification_owner", "verification_note")}
	result["device_workflow"] = row{"enabled": workflow["enabled"], "todos": workflow["todos"], "units": pickRows(entityRows(workflow, "units"), "id", "device_id", "label")}
}

func applyCompact(result row) {
	devices := result["phone_devices"].(row)
	for _, device := range entityRows(devices, "devices") {
		for _, key := range []string{"account_ids", "occupancy", "suggested_state", "unit_history"} {
			delete(device, key)
		}
	}
	workflow := result["device_workflow"].(row)
	// These copies were attached to the same owner rows by workflowOverview.
	delete(workflow, "access")
	delete(workflow, "placements")
	for _, event := range entityRows(workflow, "events") {
		delete(event, "detail")
	}
}
