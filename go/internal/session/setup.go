package session

import (
	"context"
	"crypto/subtle"
	"database/sql"
	"net"
	"net/http"
	"strings"
	"time"
	"unicode/utf8"
)

const setupSeconds = 30 * 60
const setupDenied = "请在服务器本机授权创建总管理员"

func localSetupRequest(r *http.Request) bool {
	remote, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil || !net.ParseIP(remote).IsLoopback() {
		return false
	}
	host := r.Host
	if name, _, err := net.SplitHostPort(host); err == nil {
		host = name
	}
	return strings.EqualFold(host, "localhost") || net.ParseIP(strings.Trim(host, "[]")).IsLoopback()
}

func (h Handler) MintSetup(w http.ResponseWriter, r *http.Request) {
	var body map[string]any
	if !h.readWriteRequest(w, r, &body) {
		return
	}
	key := r.Header.Get("X-Worker-Token")
	if !localSetupRequest(r) || h.SetupKey == "" || key == "" ||
		subtle.ConstantTimeCompare([]byte(digest(key)), []byte(digest(h.SetupKey))) != 1 {
		replyError(w, http.StatusForbidden, setupDenied)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), loginTimeout)
	defer cancel()
	tx, err := BeginWrite(ctx, h.Store.DB, h.WriterEpoch, h.RestoreGeneration)
	if err != nil {
		replyWriteFailure(w, err)
		return
	}
	defer tx.Rollback()
	found, err := hasOwner(ctx, tx)
	if err != nil {
		replyWriteFailure(w, err)
		return
	}
	if found {
		replyError(w, http.StatusForbidden, "总管理员已经初始化")
		return
	}
	token, err := secureToken()
	if err == nil {
		_, err = tx.ExecContext(ctx, "DELETE FROM auth_setup")
	}
	if err == nil {
		_, err = tx.ExecContext(ctx, "INSERT INTO auth_setup(token_hash,expires_at) VALUES($1,$2)", digest(token), time.Now().Unix()+setupSeconds)
	}
	if err == nil {
		err = tx.Commit()
	}
	if err != nil {
		replyWriteFailure(w, err)
		return
	}
	h.setSetupCookie(w, r, token, setupSeconds)
	replyJSON(w, http.StatusOK, map[string]any{"setup_token": token})
}

func (h Handler) SetupOwner(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Username string `json:"username"`
		Name     string `json:"name"`
		Password string `json:"password"`
	}
	if !h.readWriteRequest(w, r, &body) {
		return
	}
	setup := requestCookie(r, "board_setup")
	if !localSetupRequest(r) || setup == "" {
		replyError(w, http.StatusForbidden, setupDenied)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), loginTimeout)
	defer cancel()
	tx, err := BeginWrite(ctx, h.Store.DB, h.WriterEpoch, h.RestoreGeneration)
	if err != nil {
		replyWriteFailure(w, err)
		return
	}
	defer tx.Rollback()
	now := time.Now().Unix()
	var allowed bool
	err = tx.QueryRowContext(ctx, `SELECT NOT EXISTS(SELECT 1 FROM auth_users WHERE role='owner')
		AND EXISTS(SELECT 1 FROM auth_setup WHERE token_hash=$1 AND expires_at>$2)`, digest(setup), now).Scan(&allowed)
	if err != nil {
		replyWriteFailure(w, err)
		return
	}
	if !allowed {
		replyError(w, http.StatusForbidden, setupDenied)
		return
	}
	username, name := strings.TrimSpace(body.Username), strings.TrimSpace(body.Name)
	if !usernamePattern.MatchString(username) {
		replyError(w, http.StatusBadRequest, "登录名请使用3至40位字母、数字、点、横线或下划线")
		return
	}
	if name == "" {
		name = username
	}
	if utf8.RuneCountInString(name) > 60 || strings.ContainsRune(name, 0) {
		replyError(w, http.StatusBadRequest, "姓名不能超过60字")
		return
	}
	if size := utf8.RuneCountInString(body.Password); size < 10 || size > 128 {
		replyError(w, http.StatusBadRequest, "密码请设置为10至128位")
		return
	}
	encoded, err := encodePassword(body.Password)
	var id int64
	if err == nil {
		err = tx.QueryRowContext(ctx, `INSERT INTO auth_users(username,name,role,password_hash,created_at)
			VALUES($1,$2,'owner',$3,$4) RETURNING id`, username, name, encoded, now).Scan(&id)
	}
	if err == nil {
		_, err = tx.ExecContext(ctx, "DELETE FROM auth_setup")
	}
	if err == nil {
		err = auditSettings(ctx, tx, User{ID: id, Name: name}, "创建总管理员", username)
	}
	var token string
	if err == nil {
		token, err = newSession(ctx, tx, id, now)
	}
	if err == nil {
		err = tx.Commit()
	}
	if err != nil {
		replyWriteFailure(w, err)
		return
	}
	h.setSetupCookie(w, r, "", -1)
	http.SetCookie(w, h.cookie(r, token, sessionSeconds))
	replyJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

func hasOwner(ctx context.Context, tx *sql.Tx) (bool, error) {
	var found bool
	err := tx.QueryRowContext(ctx, "SELECT EXISTS(SELECT 1 FROM auth_users WHERE role='owner')").Scan(&found)
	return found, err
}

func (h Handler) setSetupCookie(w http.ResponseWriter, r *http.Request, token string, age int) {
	cookie := h.cookie(r, token, age)
	cookie.Name = "board_setup"
	http.SetCookie(w, cookie)
}
