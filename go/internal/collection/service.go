// Package collection manages explicitly submitted, office-scoped work-count jobs.
package collection

import (
	"context"
	"crypto/subtle"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/session"
)

const (
	EnqueuePath  = "/api/collection/enqueue"
	SettingsPath = "/api/collection/settings"
	jobPrefix    = "go-manual-"
	maxBatch     = 2000
)

type payload = map[string]any

type Service struct {
	Write accountwrite.Service
	Token string
}

func ValidToken(token string) bool {
	return len(token) >= 32 && len(token) <= 256 && strings.IndexFunc(token, func(r rune) bool { return r < 33 || r > 126 }) == -1
}

func (s Service) Register(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/collection/jobs", s.jobs)
	if s.Token == "" {
		return
	}
	mux.HandleFunc("POST "+EnqueuePath, s.Write.HandleLimit(EnqueuePath, "提交采集必须带请求标识", 512<<10, authorizeEnqueue, enqueue))
	mux.HandleFunc("POST "+SettingsPath, s.Write.Direct(4096, false, authorizeControl, control))
	// The existing F2 worker uses these endpoints. They accept no browser session.
	mux.HandleFunc("GET /api/collection/next", s.worker(false, nextJob))
	mux.HandleFunc("POST /api/collection/heartbeat", s.worker(true, heartbeat))
	mux.HandleFunc("POST /api/collection/result", s.worker(true, complete))
}

func (s Service) ActiveJobPrefix() string {
	if s.Token == "" {
		return ""
	}
	return jobPrefix
}

func localAddress(addr string) bool {
	host, _, err := net.SplitHostPort(addr)
	return err == nil && net.ParseIP(host).IsLoopback()
}

func (s Service) workerAllowed(r *http.Request) bool {
	return localAddress(r.RemoteAddr) && localAddress(r.Host) && r.Header.Get("Origin") == "" &&
		r.Header.Get("Forwarded") == "" && r.Header.Get("X-Forwarded-For") == "" &&
		r.Header.Get("X-Collection-Engine") == "f2" && ValidToken(s.Token) &&
		subtle.ConstantTimeCompare([]byte(r.Header.Get("X-Worker-Token")), []byte(s.Token)) == 1
}

type workerAction func(context.Context, *sql.Tx, payload, time.Time) (payload, error)

func (s Service) worker(post bool, action workerAction) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !s.workerAllowed(r) {
			failure(w, accountwrite.Forbidden("仅已配置的本机采集器可调用"))
			return
		}
		var body payload
		if post {
			mediaType, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
			decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8192))
			decoder.UseNumber()
			if err != nil || mediaType != "application/json" || decoder.Decode(&body) != nil || body == nil || decoder.Decode(new(any)) != io.EOF {
				failure(w, accountwrite.Invalid("采集请求格式不正确"))
				return
			}
		}
		ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
		defer cancel()
		tx, err := session.BeginWrite(ctx, s.Write.DB, s.Write.WriterEpoch, s.Write.RestoreGeneration)
		if err != nil {
			failure(w, err)
			return
		}
		defer tx.Rollback()
		result, err := action(ctx, tx, body, time.Now())
		if err == nil {
			err = tx.Commit()
		}
		if err != nil {
			failure(w, err)
			return
		}
		respond(w, http.StatusOK, result)
	}
}

func (s Service) jobs(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 8*time.Second)
	defer cancel()
	tx, err := s.Write.DB.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
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
	if !actor.Has("collection_manage") {
		failure(w, accountwrite.Forbidden("需要作品采集管理权限"))
		return
	}
	page := 1
	if value := r.URL.Query().Get("page"); value != "" {
		page, err = strconv.Atoi(value)
		if err != nil || page < 1 || page > 1000 {
			failure(w, accountwrite.Invalid("页码不正确"))
			return
		}
	}
	state := r.URL.Query().Get("state")
	switch state {
	case "", "pending", "采集成功", "采集失败", "需要验证", "已取消":
	default:
		failure(w, accountwrite.Invalid("采集状态筛选不正确"))
		return
	}
	const pageSize = 50
	const visible = ` FROM board_candidate.collection_jobs j JOIN board_candidate.accounts a ON a.id=j.account_id
		LEFT JOIN board_candidate.account_offices o ON o.account_id=a.id
		LEFT JOIN board_candidate.offices office ON office.id=o.office_id
		WHERE ($1 OR o.office_id=ANY($2::text[]))
		AND ($3='' OR ($3='pending' AND j.state IN ('queued','running')) OR ($3<>'pending' AND a.collection_state=$3))`
	var total int
	if err := tx.QueryRowContext(ctx, `SELECT count(*)`+visible, actor.Global(), actor.OfficeIDs, state).Scan(&total); err != nil {
		failure(w, err)
		return
	}
	page = min(page, max(1, (total+pageSize-1)/pageSize))
	rows, err := tx.QueryContext(ctx, `SELECT a.id,COALESCE(a.name,''),COALESCE(a.douyin_id,''),COALESCE(o.office_id,''),COALESCE(office.name,''),
		j.state,a.collection_state,a.collection_error,a.work_count,COALESCE(a.last_collected,''),a.last_attempted,
		starts_with(j.token,$4)`+visible+` ORDER BY CASE j.state WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
		a.last_attempted DESC,a.id LIMIT $5 OFFSET $6`, actor.Global(), actor.OfficeIDs, state, jobPrefix, pageSize, (page-1)*pageSize)
	if err != nil {
		failure(w, err)
		return
	}
	defer rows.Close()
	result := make([]payload, 0, pageSize)
	for rows.Next() {
		var id, name, handle, officeID, officeName, jobState, accountState, message, lastCollected, lastAttempted string
		var count *int64
		var current bool
		if err := rows.Scan(&id, &name, &handle, &officeID, &officeName, &jobState, &accountState, &message, &count, &lastCollected, &lastAttempted, &current); err != nil {
			failure(w, err)
			return
		}
		result = append(result, payload{"account_id": id, "name": name, "douyin_id": handle, "office_id": officeID, "office_name": officeName,
			"job_state": jobState, "state": accountState, "error": message, "count": count,
			"last_collected": lastCollected, "last_attempted": lastAttempted, "current": current})
	}
	if err := rows.Err(); err != nil {
		failure(w, err)
		return
	}
	respond(w, http.StatusOK, payload{"user": actor, "jobs": result, "page": page, "page_size": pageSize, "total": total})
}

func failure(w http.ResponseWriter, err error) {
	var userError accountwrite.APIError
	var admission session.AdmissionError
	switch {
	case errors.As(err, &userError):
		respond(w, userError.Status, payload{"error": userError.Message, "code": userError.Code})
	case errors.As(err, &admission):
		respond(w, http.StatusServiceUnavailable, payload{"error": "当前工作台暂不可写入", "code": admission.Code})
	case errors.Is(err, session.ErrUnauthorized):
		respond(w, http.StatusUnauthorized, payload{"error": "请先登录工作台"})
	default:
		respond(w, http.StatusServiceUnavailable, payload{"error": "暂时无法读取或保存作品采集资料"})
	}
}

func respond(w http.ResponseWriter, status int, body payload) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}
