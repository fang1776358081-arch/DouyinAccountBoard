// Package publishreview reads and records dated net-growth evidence.
package publishreview

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"

	"douyinboard/internal/board"
	"douyinboard/internal/session"
)

type Handler struct {
	DB                *sql.DB
	WriterEpoch       int64
	RestoreGeneration string
	PublicOrigin      string
	now               func() time.Time
}

type row = map[string]any

// The reviewed Git blob used by the original upgrade tool and Go bootstrap.
const migrationHash = "5fb8227b5e280f7c651934eb626881a870d3264348b2385c3e4e4f797d77939d"
const schemaRequired = "PUBLISH_REVIEW_SCHEMA_REQUIRED"
const timestamp = "2006-01-02T15:04:05.000000-07:00"

var beijing = time.FixedZone("Asia/Shanghai", 8*60*60)

func (h Handler) clock() time.Time {
	if h.now != nil {
		return h.now().In(beijing)
	}
	return time.Now().In(beijing)
}

func available(ctx context.Context, tx *sql.Tx) (bool, error) {
	var present bool
	err := tx.QueryRowContext(ctx, `SELECT
		EXISTS(SELECT 1 FROM board_candidate.schema_migrations WHERE version=6 AND sha256=$1)
		AND to_regclass('board_candidate.publish_review_tasks') IS NOT NULL
		AND to_regclass('board_candidate.publish_review_samples') IS NOT NULL
		AND to_regclass('board_candidate.publish_review_baselines') IS NOT NULL
		AND to_regclass('board_candidate.publish_review_requests') IS NOT NULL`, migrationHash).Scan(&present)
	return present, err
}

func (h Handler) Overview(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()
	tx, err := h.DB.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
	if err != nil {
		failure(w, err)
		return
	}
	defer tx.Rollback()
	actor, err := session.AuthenticateWith(ctx, tx, r)
	if err != nil {
		failure(w, err)
		return
	}
	if present, err := available(ctx, tx); err != nil || !present {
		if err == nil {
			err = apiError{http.StatusServiceUnavailable, "发布核对的数据结构尚未升级；现有页面仍可使用", schemaRequired}
		}
		failure(w, err)
		return
	}
	current := h.clock()
	request, err := parseFilters(r, current.Format(time.DateOnly))
	if err != nil {
		failure(w, err)
		return
	}
	result, err := overview(ctx, tx, actor, request, current)
	if err != nil {
		failure(w, err)
		return
	}
	if err := tx.Commit(); err != nil {
		failure(w, err)
		return
	}
	board.WriteNegotiatedJSON(w, r, result)
}

type filters struct {
	day, office, state, computer string
	page                         int64
}

func parseFilters(r *http.Request, today string) (filters, error) {
	query := r.URL.Query()
	get := func(key string) string {
		for i := len(query[key]) - 1; i >= 0; i-- {
			if value := query[key][i]; value != "" {
				return value
			}
		}
		return ""
	}
	out := filters{day: get("date"), office: get("office"), state: get("state"), computer: get("computer"), page: 1}
	if out.day == "" {
		out.day = today
	}
	if !validDay(out.day) {
		return out, invalid("业务日期格式不正确")
	}
	if out.day > today {
		return out, invalid("未来日期尚无作品统计，请先查看今天或历史日期")
	}
	if out.state != "" && out.state != "pending" && out.state != "review" {
		return out, invalid("核对状态筛选不正确")
	}
	if raw := get("page"); raw != "" {
		page, err := strconv.ParseInt(strings.TrimSpace(raw), 10, 64)
		if err != nil && !errors.Is(err, strconv.ErrRange) {
			return out, invalid("页码不正确")
		}
		out.page = max(1, page)
	}
	return out, nil
}

const overviewQuery = `
WITH raw AS (
 SELECT a.id AS account_id,
  CASE WHEN $1=$2 THEN COALESCE(a.name,'') ELSE COALESCE(t.account_name,'') END AS name,
  CASE WHEN $1=$2 THEN COALESCE(a.douyin_id,'') ELSE COALESCE(t.douyin_id,'') END AS douyin_id,
  COALESCE(t.computer_id,c.id,'') AS computer_id,COALESCE(t.computer_name,c.name,'未分配电脑') AS computer_name,
  COALESCE(t.operator,c.operator,'') AS operator,t.task_state,t.target,t.deadline,COALESCE(t.revision,0) AS revision,
  COALESCE(t.account_status,a.status,'') AS account_status,t.reason AS target_reason,
  s.total AS current_total,s.sampled_at AS current_at,s.day AS sample_day,s.account_identity AS sample_identity,
  bs.total AS baseline_total,bs.sampled_at AS baseline_at,bs.account_identity AS baseline_identity,b.sealed_at,
  attempt.result AS attempt_result,attempt.day AS attempt_day,attempt.sampled_at AS attempt_at,
  a.work_count AS legacy_total,a.last_collected AS legacy_at,a.collection_state AS legacy_state,
  a.last_attempted AS legacy_attempt_at,a.registered_on,
  CASE WHEN t.task_state IS NULL AND a.status IN ('停用','封禁','短期封禁','养号7天') THEN 1 ELSE 0 END AS paused_account
 FROM board_candidate.accounts a
 LEFT JOIN board_candidate.account_offices ao ON ao.account_id=a.id
 LEFT JOIN board_candidate.computers c ON c.id=a.computer_id AND ($3 OR EXISTS(
  SELECT 1 FROM board_candidate.computer_offices co WHERE co.computer_id=c.id AND co.office_id=ANY($4::text[])))
 LEFT JOIN board_candidate.publish_review_tasks t ON t.account_id=a.id AND t.day=$1
 LEFT JOIN board_candidate.publish_review_baselines b ON b.account_id=a.id AND b.day=$1
 LEFT JOIN board_candidate.publish_review_samples bs ON bs.id=b.sample_id
 LEFT JOIN LATERAL (SELECT total,sampled_at,day,account_identity FROM board_candidate.publish_review_samples
  WHERE account_id=a.id AND day<=$1 AND result='ok' ORDER BY sampled_at DESC,id DESC LIMIT 1) s ON true
 LEFT JOIN LATERAL (SELECT result,day,sampled_at FROM board_candidate.publish_review_samples
  WHERE account_id=a.id AND day<=$1 ORDER BY sampled_at DESC,id DESC LIMIT 1) attempt ON true
 WHERE (t.account_id IS NOT NULL OR $1=$2)
 AND ($3 OR (ao.office_id=ANY($4::text[]) AND (t.account_id IS NULL OR t.office_id=ANY($4::text[]))))
 AND ($5='' OR COALESCE(t.office_id,ao.office_id,'')=$5)
), calculated AS (
 SELECT raw.*,CASE WHEN sample_day=$1 AND sample_identity=baseline_identity AND sample_identity=douyin_id
  THEN current_total-baseline_total ELSE NULL END AS net FROM raw
), judged AS (
 SELECT calculated.*,CASE
  WHEN task_state='paused' OR paused_account=1 THEN 'paused'
  WHEN task_state='none' THEN 'no_task'
  WHEN task_state IS NULL OR target IS NULL OR computer_id='' OR current_at IS NULL OR sample_day<>$1
   OR (attempt_day=$1 AND attempt_result='failed')
   OR (substr(legacy_attempt_at,1,10)=$1 AND legacy_state IN ('采集失败','需要验证') AND (current_at IS NULL OR legacy_attempt_at>=current_at))
   OR baseline_total IS NULL OR net<0 OR baseline_identity<>sample_identity OR sample_identity<>douyin_id THEN 'review'
  WHEN net>=target THEN 'met' ELSE 'pending' END AS state FROM calculated
), stats AS (
 SELECT count(*) FILTER (WHERE state='met') AS met,count(*) FILTER (WHERE state='pending') AS pending,
  count(*) FILTER (WHERE state='review') AS review,count(*) FILTER (WHERE state='paused') AS paused,
  count(*) FILTER (WHERE state='no_task') AS no_task FROM judged
), computers AS (
 SELECT computer_id,MIN(computer_name) AS computer_name,
  CASE WHEN count(DISTINCT operator)>1 THEN '多位负责人（见账号）' ELSE MIN(operator) END AS operator,
  count(*) AS accounts,count(*) FILTER(WHERE task_state='active') AS scheduled,
  count(*) FILTER(WHERE state='met') AS met,count(*) FILTER(WHERE state='pending') AS pending,
  count(*) FILTER(WHERE state='review') AS review FROM judged GROUP BY computer_id
), filtered AS (
 SELECT * FROM judged WHERE ($6='' OR state=$6)
 AND ($7='' OR computer_id=CASE WHEN $7='unassigned' THEN '' ELSE $7 END)
), counted AS (
 SELECT count(*) AS total,GREATEST(1,(count(*)+49)/50) AS pages FROM filtered
), pagination AS (
 SELECT total,pages,LEAST($8::bigint,pages) AS page FROM counted
), page_rows AS (
 SELECT * FROM filtered ORDER BY computer_name,account_id LIMIT 50 OFFSET (SELECT (page-1)*50 FROM pagination)
), visible_offices AS (
 SELECT id,name FROM board_candidate.offices WHERE active=1 AND ($3 OR id=ANY($4::text[]))
)
SELECT jsonb_build_object('summary',(SELECT to_jsonb(s) FROM stats s),
 'computers',COALESCE((SELECT jsonb_agg(c ORDER BY c.computer_name,c.computer_id) FROM computers c),'[]'::jsonb),
 'rows',COALESCE((SELECT jsonb_agg(p ORDER BY p.computer_name,p.account_id) FROM page_rows p),'[]'::jsonb),
 'offices',COALESCE((SELECT jsonb_agg(o ORDER BY o.name) FROM visible_offices o),'[]'::jsonb),
 'total',p.total,'page',p.page,'pages',p.pages) FROM pagination p`

func overview(ctx context.Context, tx *sql.Tx, actor session.User, filters filters, current time.Time) (row, error) {
	var raw []byte
	today := current.Format(time.DateOnly)
	if err := tx.QueryRowContext(ctx, overviewQuery, filters.day, today, actor.Global(), actor.OfficeIDs,
		filters.office, filters.state, filters.computer, filters.page).Scan(&raw); err != nil {
		return nil, err
	}
	result, err := decodeRow(raw)
	if err != nil {
		return nil, err
	}
	historical := filters.day < today
	for _, item := range result["rows"].([]any) {
		formatRow(item.(row), filters.day, current.Format(timestamp), historical)
	}
	result["date"], result["timezone"], result["page_size"] = filters.day, "Asia/Shanghai", 50
	result["user"] = row{"id": actor.ID, "name": actor.Name, "role": actor.Role, "permissions": actor.Permissions, "csrf": actor.CSRF}
	result["freshness"] = "沿用原采集每日一次的口径：只有所选北京时间业务日的成功采样有效，不代表实时数据。"
	result["historical"], result["can_manage"] = historical, actor.Has("publishing_manage")
	return result, nil
}

func formatRow(item row, day, current string, historical bool) {
	if historical {
		item["legacy_total"], item["legacy_at"] = nil, nil
	}
	reasons := make([]string, 0)
	var label string
	item["remaining"], item["progress"] = nil, nil
	switch item["state"] {
	case "review":
		label = "数据待核验"
		reasons = reviewReasons(item, day)
	case "paused":
		label = "暂停任务"
		reasons = append(reasons, "当日任务暂停或账号未安排任务且处于停用/封禁/养号状态，不计入待完成")
	case "no_task":
		label = "无当日任务"
		reasons = append(reasons, "已明确登记本日无发布任务，不计入待完成")
	case "met", "pending":
		label = "净增达标"
		if item["state"] == "pending" {
			label = "待完成"
			if deadline := str(item["deadline"]); deadline != "" {
				label = "进行中"
				if current > deadline {
					label = "未达标"
				}
			}
		}
		target, _ := number(item["target"])
		net, _ := number(item["net"])
		item["remaining"] = max(0, target-net)
		if target > 0 {
			item["progress"] = min(100, max(0, float64(net)*100/float64(target)))
		} else if item["state"] == "met" {
			item["progress"] = 100
		}
	}
	item["label"], item["reasons"] = label, reasons
}

func reviewReasons(item row, day string) []string {
	reasons := make([]string, 0)
	if item["task_state"] == nil {
		reasons = append(reasons, "未保留当日任务计划，请确认是否安排任务")
	} else if item["target"] == nil {
		reasons = append(reasons, "已安排任务，但当日目标尚未设置")
	}
	if str(item["computer_id"]) == "" {
		reasons = append(reasons, "当日任务电脑尚未确认")
	}
	if item["current_at"] == nil {
		reasons = append(reasons, "没有留存成功采样；旧版最新值不冒充每日快照")
	} else if item["sample_day"] != day {
		reasons = append(reasons, "采样已过期：本业务日没有成功采样")
	}
	legacyAt, legacyState := str(item["legacy_attempt_at"]), str(item["legacy_state"])
	if (item["attempt_day"] == day && item["attempt_result"] == "failed") ||
		(strings.HasPrefix(legacyAt, day) && (legacyState == "采集失败" || legacyState == "需要验证") && (item["current_at"] == nil || legacyAt >= str(item["current_at"]))) {
		reasons = append(reasons, "本日最近一次采样失败")
	}
	if item["baseline_total"] == nil {
		if item["registered_on"] == day {
			reasons = append(reasons, "新账号缺少昨日基线")
		} else {
			reasons = append(reasons, "缺少昨日收口基线")
		}
	}
	if net, ok := number(item["net"]); ok && net < 0 {
		reasons = append(reasons, "累计作品数量减少，保留负差值，需核对删除或隐藏等变化")
	}
	if item["baseline_identity"] != nil && item["sample_identity"] != nil && item["baseline_identity"] != item["sample_identity"] {
		reasons = append(reasons, "两次采样之间账号标识发生变化，不能直接相减")
	} else if item["sample_identity"] != nil && item["sample_identity"] != item["douyin_id"] {
		reasons = append(reasons, "现有采样与当日账号标识不一致，需重新核验")
	}
	return reasons
}

func validDay(value string) bool {
	day, err := time.Parse(time.DateOnly, value)
	return err == nil && day.Year() > 0
}

func str(value any) string {
	result, _ := value.(string)
	return result
}

func number(value any) (int64, bool) {
	switch value := value.(type) {
	case json.Number:
		result, err := value.Int64()
		return result, err == nil
	case int64:
		return value, true
	case int:
		return int64(value), true
	default:
		return 0, false
	}
}

func decodeRow(raw []byte) (row, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var value row
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	if value == nil {
		return nil, errors.New("invalid publish review object")
	}
	return value, nil
}

type apiError struct {
	status        int
	message, code string
}

func (err apiError) Error() string   { return err.message }
func invalid(message string) error   { return apiError{http.StatusBadRequest, message, ""} }
func conflict(message string) error  { return apiError{http.StatusConflict, message, ""} }
func forbidden(message string) error { return apiError{http.StatusForbidden, message, ""} }

func failure(w http.ResponseWriter, err error) {
	var public apiError
	if errors.Is(err, session.ErrUnauthorized) {
		public = apiError{http.StatusUnauthorized, "请先登录工作台", ""}
	} else if !errors.As(err, &public) {
		code := "DB_UNAVAILABLE"
		var state interface{ SQLState() string }
		if errors.As(err, &state) {
			switch state.SQLState() {
			case "55P03":
				code = "DB_BUSY"
			case "57014":
				code = "DB_TIMEOUT"
			}
		} else if errors.Is(err, context.DeadlineExceeded) {
			code = "DB_TIMEOUT"
		}
		public = apiError{http.StatusServiceUnavailable, "数据服务暂时不可用，请稍后刷新核对操作结果", code}
	}
	result := row{"error": public.message}
	if public.code != "" {
		result["code"] = public.code
	}
	writeJSON(w, public.status, result)
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
