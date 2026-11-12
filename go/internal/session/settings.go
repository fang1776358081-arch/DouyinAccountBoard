package session

import (
	"context"
	"crypto/pbkdf2"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"database/sql"
	"encoding/hex"
	"errors"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

var usernamePattern = regexp.MustCompile(`^[A-Za-z0-9_.-]{3,40}$`)

func (h Handler) ChangePassword(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Old string `json:"old_password"`
		New string `json:"password"`
	}
	if !h.readWriteRequest(w, r, &body) {
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), loginTimeout)
	defer cancel()
	tx, actor, err := h.settingsSession(ctx, r)
	if err != nil {
		settingsError(w, err)
		return
	}
	defer tx.Rollback()
	var old string
	err = tx.QueryRowContext(ctx, "SELECT password_hash FROM auth_users WHERE id=$1", actor.ID).Scan(&old)
	if err != nil {
		settingsError(w, err)
		return
	}
	if !passwordMatches(body.Old, old) {
		replyError(w, http.StatusBadRequest, "原密码不正确")
		return
	}
	if size := utf8.RuneCountInString(body.New); size < 10 || size > 128 {
		replyError(w, http.StatusBadRequest, "密码请设置为10至128位")
		return
	}
	encoded, err := encodePassword(body.New)
	if err != nil {
		settingsError(w, err)
		return
	}
	now := time.Now().Unix()
	if _, err := tx.ExecContext(ctx, "UPDATE auth_users SET password_hash=$1 WHERE id=$2", encoded, actor.ID); err != nil {
		settingsError(w, err)
		return
	}
	if _, err := tx.ExecContext(ctx, "DELETE FROM auth_sessions WHERE user_id=$1", actor.ID); err != nil {
		settingsError(w, err)
		return
	}
	token, err := newSession(ctx, tx, actor.ID, now)
	if err == nil {
		err = auditSettings(ctx, tx, actor, "修改本人密码", "")
	}
	if err == nil {
		err = tx.Commit()
	}
	if err != nil {
		settingsError(w, err)
		return
	}
	http.SetCookie(w, h.cookie(r, token, sessionSeconds))
	replyJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

func encodePassword(password string) (string, error) {
	var saltBytes [16]byte
	rand.Read(saltBytes[:])
	salt := hex.EncodeToString(saltBytes[:])
	key, err := pbkdf2.Key(sha256.New, password, []byte(salt), 600000, 32)
	if err != nil {
		return "", err
	}
	return "pbkdf2_sha256$600000$" + salt + "$" + hex.EncodeToString(key), nil
}

func (h Handler) ChangeUsername(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Current string `json:"current_password"`
		Name    string `json:"username"`
	}
	if !h.readWriteRequest(w, r, &body) {
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), loginTimeout)
	defer cancel()
	tx, actor, err := h.settingsSession(ctx, r)
	if err != nil {
		settingsError(w, err)
		return
	}
	defer tx.Rollback()
	if actor.Role != "owner" {
		replyError(w, http.StatusForbidden, "只有总管理员可以修改本人登录名")
		return
	}
	now := time.Now().Unix()
	key := digest("username-change:" + strconv.FormatInt(actor.ID, 10))
	var blocked bool
	err = tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM auth_limits WHERE key=$1 AND expires_at>$2 AND failures>=5)`, key, now).Scan(&blocked)
	if err != nil {
		settingsError(w, err)
		return
	}
	if blocked {
		replyError(w, http.StatusTooManyRequests, "验证尝试过多，请15分钟后再试")
		return
	}
	var encoded string
	if err := tx.QueryRowContext(ctx, "SELECT password_hash FROM auth_users WHERE id=$1", actor.ID).Scan(&encoded); err != nil {
		settingsError(w, err)
		return
	}
	if !passwordMatches(body.Current, encoded) {
		_, err = tx.ExecContext(ctx, `INSERT INTO auth_limits(key,failures,expires_at) VALUES($1,1,$2)
			ON CONFLICT(key) DO UPDATE SET failures=CASE WHEN auth_limits.expires_at<=$3 THEN 1 ELSE auth_limits.failures+1 END,
			expires_at=CASE WHEN auth_limits.expires_at<=$3 THEN excluded.expires_at ELSE auth_limits.expires_at END`, key, now+limitSeconds, now)
		if err == nil {
			err = tx.Commit()
		}
		if err != nil {
			settingsError(w, err)
			return
		}
		replyError(w, http.StatusBadRequest, "当前密码不正确")
		return
	}
	name := strings.TrimSpace(body.Name)
	if !usernamePattern.MatchString(name) {
		replyError(w, http.StatusBadRequest, "登录名请使用3至40位字母、数字、点、横线或下划线")
		return
	}
	if name == actor.Username {
		replyError(w, http.StatusBadRequest, "新登录名不能与当前登录名相同")
		return
	}
	var exists bool
	err = tx.QueryRowContext(ctx, "SELECT EXISTS(SELECT 1 FROM auth_users WHERE username=$1)", name).Scan(&exists)
	if err != nil {
		settingsError(w, err)
		return
	}
	if exists {
		replyError(w, http.StatusBadRequest, "登录名已存在")
		return
	}
	if _, err = tx.ExecContext(ctx, "UPDATE auth_users SET username=$1 WHERE id=$2", name, actor.ID); err == nil {
		_, err = tx.ExecContext(ctx, "DELETE FROM auth_sessions WHERE user_id=$1", actor.ID)
	}
	if err == nil {
		_, err = tx.ExecContext(ctx, "DELETE FROM auth_limits WHERE key=$1", key)
	}
	if err == nil {
		err = auditSettings(ctx, tx, actor, "修改本人登录名", actor.Username+" → "+name)
	}
	if err == nil {
		err = tx.Commit()
	}
	if err != nil {
		settingsError(w, err)
		return
	}
	http.SetCookie(w, h.cookie(r, "", -1))
	replyJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

var errSettingsCSRF = errors.New("页面已过期，请刷新后重试")

func (h Handler) settingsSession(ctx context.Context, r *http.Request) (*sql.Tx, User, error) {
	tx, err := BeginWrite(ctx, h.Store.DB, h.WriterEpoch, h.RestoreGeneration)
	if err != nil {
		return nil, User{}, err
	}
	actor, err := AuthenticateWith(ctx, tx, r)
	if err == nil && subtle.ConstantTimeCompare([]byte(r.Header.Get("X-CSRF-Token")), []byte(actor.CSRF)) != 1 {
		err = errSettingsCSRF
	}
	if err != nil {
		_ = tx.Rollback()
		return nil, User{}, err
	}
	return tx, actor, nil
}

func settingsError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, ErrUnauthorized):
		replyError(w, http.StatusUnauthorized, err.Error())
	case errors.Is(err, errSettingsCSRF):
		replyError(w, http.StatusForbidden, err.Error())
	default:
		replyWriteFailure(w, err)
	}
}

func auditSettings(ctx context.Context, tx *sql.Tx, actor User, action, target string) error {
	_, err := tx.ExecContext(ctx, "INSERT INTO auth_audit(at,actor_id,actor_name,action,target) VALUES($1,$2,$3,$4,$5)",
		time.Now().Unix(), actor.ID, actor.Name, action, target)
	return err
}
