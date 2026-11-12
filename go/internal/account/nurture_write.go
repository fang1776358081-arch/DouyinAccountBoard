package account

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/base64"
	"errors"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"time"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/session"
)

const NurturePathPrefix = "/api/workspace/nurture/"

var nurtureRequests = map[string]string{
	"join":         "加入养号必须带请求标识",
	"start":        "开始养号必须带请求标识",
	"verification": "保存核对记录必须带请求标识",
	"pause":        "暂停养号必须带请求标识",
	"resume":       "继续养号必须带请求标识",
	"finish":       "结束养号并转正常必须带请求标识",
	"upload":       "保存养号截图必须带请求标识",
	"delete-image": "移除养号截图必须带请求标识",
	"purge":        "清理养号截图必须带请求标识",
}

func NurtureActions() []string {
	return []string{"join", "start", "verification", "pause", "resume", "finish", "upload", "delete-image", "purge"}
}

func (h WriteHandler) Nurture(action string) http.HandlerFunc {
	path := NurturePathPrefix + action
	var maxBytes int64 = 100000
	if action == "upload" {
		maxBytes = 12 << 20
	}
	return h.HandleLimit(path, nurtureRequests[action], maxBytes, func(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) error {
		if _, found := nurtureRequests[action]; !found {
			return accountwrite.APIError{Status: http.StatusNotFound, Message: "接口不存在"}
		}
		if _, err := requireAccount(ctx, tx, actor, identifier(body["id"])); err != nil {
			return err
		}
		if !actor.Has("nurture_manage") {
			return accountwrite.Forbidden(permissionDenied)
		}
		return nil
	}, func(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
		id := identifier(body["id"])
		old, err := requireAccount(ctx, tx, actor, id)
		if err != nil {
			return nil, err
		}
		set, err := captureChanges(ctx, tx, path, id, old, nil)
		if err != nil {
			return nil, err
		}
		if err := addVersion(set.checked, "nurture:"+id, set.nurture); err != nil {
			return nil, err
		}
		if err := accountwrite.CheckVersions(body, set.checked); err != nil {
			return nil, err
		}
		day := boardToday()
		if err := applyNurtureAction(ctx, tx, actor, action, set, body, day); err != nil {
			return nil, err
		}
		result, _, err := set.finish(ctx, tx, actor, day)
		delete(result, "id")
		return result, err
	})
}

func applyNurtureAction(ctx context.Context, tx *sql.Tx, actor session.User, action string, set accountChangeSet, body map[string]any, day time.Time) error {
	id, record := set.id, set.nurture
	completed, workflow := textField(record, "completed_on"), textField(record, "workflow")
	today := day.Format(time.DateOnly)
	switch action {
	case "join":
		if record == nil {
			_, err := tx.ExecContext(ctx, `INSERT INTO board_candidate.nurture_records(account_id) VALUES($1)`, id)
			return err
		}
		if completed == "" {
			return accountwrite.Invalid("账号已在养号面板")
		}
		_, err := tx.ExecContext(ctx, `UPDATE board_candidate.nurture_records SET
legacy_started_on='',started_on='',completed_on='',cycle=cycle+1,image_count=0,verification_state='待检查',
verification_owner='',verification_note='',verification_updated_on='',workflow='待开始',active_since='',
banked_days=0,paused_on='',periods='[]',revision=revision+1 WHERE account_id=$1`, id)
		return err
	case "purge":
		if completed == "" {
			return accountwrite.Invalid("请先结束本轮养号，再清理本轮截图")
		}
		if _, err := tx.ExecContext(ctx, `DELETE FROM board_candidate.nurture_images WHERE account_id=$1`, id); err != nil {
			return err
		}
		_, err := tx.ExecContext(ctx, `UPDATE board_candidate.nurture_records SET image_count=0,revision=revision+1 WHERE account_id=$1`, id)
		return err
	case "verification":
		for _, field := range []string{"verification_state", "verification_owner", "verification_note"} {
			if value, ok := body[field]; ok {
				if text, ok := value.(string); !ok || strings.ContainsRune(text, 0) {
					return accountwrite.Invalid("字段应为文本")
				}
			}
		}
		return applyVerification(ctx, tx, id, record, body, day)
	}
	if record == nil || completed != "" {
		return accountwrite.Invalid("该账号没有进行中的养号记录")
	}
	ready := slices.Contains([]string{"无需核对，可使用", "核对已通过"}, textField(record, "verification_state"))
	if action == "start" || action == "resume" || action == "finish" {
		if !actor.Has("account_status") {
			return accountwrite.Forbidden(permissionDenied)
		}
		if !slices.Contains([]string{"正常", "待确认", "养号7天"}, textField(set.before, "status")) {
			return accountwrite.Invalid("账号状态异常或停用，请先在账号管理核对状态")
		}
	}
	switch action {
	case "start":
		if !ready {
			return accountwrite.Invalid("请先确认无需核对可使用，或核对已通过")
		}
		if workflow != "待开始" {
			return accountwrite.Invalid("本轮已经开始；暂停后请使用继续养号，不能重置已完成时间")
		}
		started := identifier(body["started"])
		if started == "" {
			started = today
		}
		date, err := isoDate(started)
		if err != nil {
			return accountwrite.Invalid("开始日期格式不正确")
		}
		if date.After(day) {
			return accountwrite.Invalid("开始日期不能在未来")
		}
		if registered, err := isoDate(textField(set.before, "registered_on")); err == nil && date.Before(registered) {
			return accountwrite.Invalid("实际开始日期不能早于注册日期")
		}
		if err := updateAccount(ctx, tx, id, map[string]any{"nurture_started": started, "status": "养号7天"}); err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, `UPDATE board_candidate.nurture_records SET started_on=$1,active_since=$1,workflow='运行',revision=revision+1 WHERE account_id=$2`, started, id)
		return err
	case "pause":
		if workflow != "运行" {
			return accountwrite.Invalid("只有进行中的养号可以暂停")
		}
		return pauseNurture(ctx, tx, id, record, day)
	case "resume":
		if workflow != "暂停" {
			return accountwrite.Invalid("只有暂停的养号可以继续")
		}
		if !ready {
			return accountwrite.Invalid("请先确认账号已能正常使用，再继续养号")
		}
		if err := updateAccount(ctx, tx, id, map[string]any{"status": "养号7天"}); err != nil {
			return err
		}
		_, err := tx.ExecContext(ctx, `UPDATE board_candidate.nurture_records SET active_since=$1,workflow='运行',revision=revision+1 WHERE account_id=$2`, today, id)
		return err
	case "finish":
		if workflow != "运行" || !ready {
			return accountwrite.Invalid("请先解决核对并继续养号，才能确认结束")
		}
		elapsed := elapsedNurture(record, day)
		if elapsed < 7 {
			return accountwrite.Invalid("实际养号尚未满7天，暂停时间不计入")
		}
		periods, err := appendNurturePeriod(textField(record, "periods"), textField(record, "active_since"), today, false)
		if err != nil {
			return accountwrite.Invalid("养号时段资料格式不正确")
		}
		if err := updateAccount(ctx, tx, id, map[string]any{"status": "正常"}); err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, `UPDATE board_candidate.nurture_records SET completed_on=$1,workflow='已结束',
banked_days=$2,active_since='',periods=$3,revision=revision+1 WHERE account_id=$4`, today, elapsed, periods, id)
		return err
	case "upload":
		return saveNurtureImage(ctx, tx, actor, id, record, body, day)
	case "delete-image":
		imageID := identifier(body["image_id"])
		if strings.ContainsRune(imageID, 0) {
			return accountwrite.Invalid("截图不存在或不属于本轮养号")
		}
		cycle, _ := integer(record["cycle"])
		removed, err := tx.ExecContext(ctx, `DELETE FROM board_candidate.nurture_images WHERE id=$1 AND account_id=$2 AND cycle=$3`, imageID, id, cycle)
		if err != nil {
			return err
		}
		count, err := removed.RowsAffected()
		if err != nil {
			return err
		}
		if count == 0 {
			return accountwrite.Invalid("截图不存在或不属于本轮养号")
		}
		_, err = tx.ExecContext(ctx, `UPDATE board_candidate.nurture_records SET image_count=image_count-1,revision=revision+1 WHERE account_id=$1`, id)
		return err
	default:
		return accountwrite.Invalid("养号操作不存在")
	}
}

func saveNurtureImage(ctx context.Context, tx *sql.Tx, actor session.User, id string, record, body map[string]any, day time.Time) error {
	startedOn := textField(record, "started_on")
	workflow := textField(record, "workflow")
	if startedOn == "" || workflow != "运行" && workflow != "暂停" {
		return accountwrite.Invalid("请先确认实际开始养号，再提交养号截图")
	}
	count, _ := integer(record["image_count"])
	if count >= 35 {
		return accountwrite.Invalid("每轮养号最多保存35张截图")
	}
	imageDay := identifier(body["day"])
	date, err := isoDate(imageDay)
	if err != nil {
		return accountwrite.Invalid("截图日期格式不正确")
	}
	started, startedErr := isoDate(startedOn)
	if date.After(day) || startedErr == nil && date.Before(started) {
		return accountwrite.Invalid("截图日期需在开始日期至今天之间")
	}
	encoded := identifier(body["content"])
	if strings.ContainsAny(encoded, "\r\n") {
		return accountwrite.Invalid("图片内容无效")
	}
	data, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		return accountwrite.Invalid("图片内容无效")
	}
	if len(data) == 0 || len(data) > 4<<20 {
		return accountwrite.Invalid("单张截图最多4MB")
	}
	mime := ""
	switch {
	case bytes.HasPrefix(data, []byte("\x89PNG\r\n\x1a\n")):
		mime = "image/png"
	case bytes.HasPrefix(data, []byte{0xff, 0xd8, 0xff}):
		mime = "image/jpeg"
	case len(data) >= 12 && string(data[:4]) == "RIFF" && string(data[8:12]) == "WEBP":
		mime = "image/webp"
	default:
		return accountwrite.Invalid("仅支持PNG、JPEG和WebP截图")
	}
	imageID, err := accountwrite.NewGroup()
	if err != nil {
		return err
	}
	cycle, _ := integer(record["cycle"])
	if _, err := tx.ExecContext(ctx, `INSERT INTO board_candidate.nurture_images(id,account_id,cycle,day,mime,content,actor)
VALUES($1,$2,$3,$4,$5,$6,$7)`, imageID, id, cycle, imageDay, mime, data, actor.Name); err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `UPDATE board_candidate.nurture_records SET image_count=image_count+1,revision=revision+1 WHERE account_id=$1`, id)
	return err
}

func (h Handler) NurtureImage(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	tx, err := h.DB.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
	if err != nil {
		replyError(w, http.StatusServiceUnavailable, "工作台暂时不可用")
		return
	}
	defer tx.Rollback()
	actor, err := session.AuthenticateWith(ctx, tx, r)
	if errors.Is(err, session.ErrUnauthorized) {
		replyError(w, http.StatusUnauthorized, "请先登录")
		return
	}
	if err != nil {
		replyError(w, http.StatusServiceUnavailable, "工作台暂时不可用")
		return
	}
	id := accountID(r)
	var accountID, mime string
	if strings.ContainsRune(id, 0) {
		replyError(w, http.StatusNotFound, "截图不存在")
		return
	}
	err = tx.QueryRowContext(ctx, `SELECT account_id,mime FROM board_candidate.nurture_images WHERE id=$1`, id).Scan(&accountID, &mime)
	if errors.Is(err, sql.ErrNoRows) {
		replyError(w, http.StatusNotFound, "截图不存在")
		return
	}
	if err == nil {
		_, err = requireAccount(ctx, tx, actor, accountID)
	}
	var denied accountwrite.APIError
	if errors.As(err, &denied) {
		replyError(w, denied.Status, denied.Message)
		return
	}
	if err != nil {
		replyError(w, http.StatusServiceUnavailable, "工作台暂时不可用")
		return
	}
	if mime != "image/png" && mime != "image/jpeg" && mime != "image/webp" {
		replyError(w, http.StatusUnsupportedMediaType, "截图格式不支持")
		return
	}
	var content []byte
	err = tx.QueryRowContext(ctx, `SELECT content FROM board_candidate.nurture_images WHERE id=$1`, id).Scan(&content)
	if err != nil || tx.Commit() != nil {
		replyError(w, http.StatusServiceUnavailable, "工作台暂时不可用")
		return
	}
	w.Header().Set("Content-Type", mime)
	w.Header().Set("Content-Length", strconv.Itoa(len(content)))
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	_, _ = w.Write(content)
}
