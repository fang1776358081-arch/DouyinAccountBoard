package session

import (
	"context"
	"crypto/pbkdf2"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

const (
	sessionSeconds = 12 * 60 * 60
	limitSeconds   = 15 * 60
	loginTimeout   = 15 * time.Second
)

const absentPasswordHash = "pbkdf2_sha256$600000$unavailable$0000000000000000000000000000000000000000000000000000000000000000"

type loginCredentials struct {
	Username string `json:"username"`
	Password string `json:"password"`
}

func (h Handler) Login(w http.ResponseWriter, r *http.Request) {
	var credentials *loginCredentials
	if !h.readWriteRequest(w, r, &credentials) {
		return
	}
	if credentials == nil {
		replyError(w, http.StatusBadRequest, "请求内容应为对象")
		return
	}
	username := strings.TrimSpace(credentials.Username)
	if len(username) > 256 {
		replyError(w, http.StatusBadRequest, "登录名过长")
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

	token, allowed, err := createSession(ctx, tx, username, credentials.Password, remoteHost(r), time.Now().Unix())
	if err != nil {
		replyWriteFailure(w, err)
		return
	}
	if !allowed {
		replyError(w, http.StatusTooManyRequests, "登录尝试过多，请15分钟后再试")
		return
	}
	if err := tx.Commit(); err != nil {
		replyWriteFailure(w, err)
		return
	}
	if token == "" {
		replyError(w, http.StatusUnauthorized, "登录名或密码不正确，或账号已停用")
		return
	}
	http.SetCookie(w, h.cookie(r, token, sessionSeconds))
	replyJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

func createSession(ctx context.Context, tx *sql.Tx, username, password, ip string, now int64) (string, bool, error) {
	if _, err := tx.ExecContext(ctx, "DELETE FROM auth_limits WHERE expires_at <= $1", now); err != nil {
		return "", false, err
	}
	keys := [2]string{digest("login:" + ip + ":" + strings.ToLower(username)), digest("ip:" + ip)}
	limits := [2]int64{5, 30}
	for index, key := range keys {
		var count int64
		err := tx.QueryRowContext(ctx, "SELECT failures FROM auth_limits WHERE key = $1 AND expires_at > $2", key, now).Scan(&count)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return "", false, err
		}
		if count >= limits[index] {
			return "", false, nil
		}
	}
	var id, active int64
	var name, encoded string
	err := tx.QueryRowContext(ctx, "SELECT id, name, active, password_hash FROM auth_users WHERE username = $1", username).
		Scan(&id, &name, &active, &encoded)
	if errors.Is(err, sql.ErrNoRows) {
		encoded = absentPasswordHash
	} else if err != nil {
		return "", false, err
	}
	valid := passwordMatches(password, encoded)
	if !valid || active != 1 {
		for _, key := range keys {
			_, err := tx.ExecContext(ctx, `
INSERT INTO auth_limits(key, failures, expires_at) VALUES ($1, 1, $2)
ON CONFLICT (key) DO UPDATE SET
    failures = CASE WHEN auth_limits.expires_at <= $3 THEN 1 ELSE auth_limits.failures + 1 END,
    expires_at = CASE WHEN auth_limits.expires_at <= $3 THEN excluded.expires_at ELSE auth_limits.expires_at END
`, key, now+limitSeconds, now)
			if err != nil {
				return "", false, err
			}
		}
		return "", true, nil
	}
	if _, err := tx.ExecContext(ctx, "DELETE FROM auth_limits WHERE key = $1", keys[0]); err != nil {
		return "", false, err
	}
	if _, err := tx.ExecContext(ctx, `
INSERT INTO auth_audit(at, actor_id, actor_name, action, target) VALUES ($1, $2, $3, '登录', $4)
`, now, id, name, username); err != nil {
		return "", false, err
	}
	if _, err := tx.ExecContext(ctx, "DELETE FROM auth_sessions WHERE expires_at <= $1", now); err != nil {
		return "", false, err
	}
	token, err := newSession(ctx, tx, id, now)
	return token, true, err
}

func newSession(ctx context.Context, tx *sql.Tx, id, now int64) (string, error) {
	token, err := secureToken()
	if err != nil {
		return "", err
	}
	csrf, err := secureToken()
	if err != nil {
		return "", err
	}
	_, err = tx.ExecContext(ctx, `
INSERT INTO auth_sessions(token_hash, user_id, csrf, expires_at) VALUES ($1, $2, $3, $4)
`, digest(token), id, csrf, now+sessionSeconds)
	return token, err
}

func (h Handler) Logout(w http.ResponseWriter, r *http.Request) {
	var body map[string]json.RawMessage
	if !h.readWriteRequest(w, r, &body) {
		return
	}
	if body == nil {
		replyError(w, http.StatusBadRequest, "请求内容应为对象")
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
	user, err := AuthenticateWith(ctx, tx, r)
	if errors.Is(err, ErrUnauthorized) {
		replyError(w, http.StatusUnauthorized, err.Error())
		return
	}
	if err != nil {
		replyWriteFailure(w, err)
		return
	}
	if subtle.ConstantTimeCompare([]byte(r.Header.Get("X-CSRF-Token")), []byte(user.CSRF)) != 1 {
		replyError(w, http.StatusForbidden, "页面已过期，请刷新后重试")
		return
	}
	if _, err = tx.ExecContext(ctx, "DELETE FROM auth_sessions WHERE token_hash = $1", digest(requestCookie(r, cookieName))); err != nil {
		replyWriteFailure(w, err)
		return
	}
	if _, err = tx.ExecContext(ctx, `
INSERT INTO auth_audit(at, actor_id, actor_name, action, target) VALUES ($1, $2, $3, '退出登录', '')
`, time.Now().Unix(), user.ID, user.Name); err != nil {
		replyWriteFailure(w, err)
		return
	}
	if err = tx.Commit(); err != nil {
		replyWriteFailure(w, err)
		return
	}
	http.SetCookie(w, h.cookie(r, "", -1))
	replyJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

func (h Handler) readWriteRequest(w http.ResponseWriter, r *http.Request, target any) bool {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		replyError(w, http.StatusMethodNotAllowed, "请求方法不正确")
		return false
	}
	if origin := r.Header.Get("Origin"); origin != "" && origin != h.requestOrigin(r) {
		replyError(w, http.StatusForbidden, "请求来源不正确")
		return false
	}
	mediaType, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || mediaType != "application/json" {
		replyError(w, http.StatusBadRequest, "需要 JSON 请求")
		return false
	}
	defer r.Body.Close()
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 100000))
	if err := decoder.Decode(target); err != nil || decoder.Decode(new(any)) != io.EOF {
		replyError(w, http.StatusBadRequest, "请求内容应为 JSON 对象")
		return false
	}
	return true
}

func (h Handler) requestOrigin(r *http.Request) string {
	if h.Origin != "" {
		return strings.TrimRight(h.Origin, "/")
	}
	if r.TLS != nil {
		return "https://" + r.Host
	}
	return "http://" + r.Host
}

func (h Handler) cookie(r *http.Request, token string, maxAge int) *http.Cookie {
	return &http.Cookie{
		Name: cookieName, Value: token, Path: "/", MaxAge: maxAge, HttpOnly: true,
		Secure: strings.HasPrefix(h.requestOrigin(r), "https://"), SameSite: http.SameSiteStrictMode,
	}
}

func remoteHost(r *http.Request) string {
	if host, _, err := net.SplitHostPort(r.RemoteAddr); err == nil {
		return host
	}
	return r.RemoteAddr
}

func passwordMatches(password, encoded string) bool {
	if utf8.RuneCountInString(password) > 128 {
		return false
	}
	parts := strings.Split(encoded, "$")
	if len(parts) != 4 || parts[0] != "pbkdf2_sha256" || len(parts[2]) == 0 || len(parts[2]) > 256 {
		return false
	}
	iterations, err := strconv.Atoi(parts[1])
	if err != nil || iterations <= 0 || iterations > 1_200_000 {
		return false
	}
	expected, err := hex.DecodeString(parts[3])
	if err != nil || len(expected) != sha256.Size {
		return false
	}
	// The previous writer uses the literal ASCII hex salt as the PBKDF2 salt.
	derived, err := pbkdf2.Key(sha256.New, password, []byte(parts[2]), iterations, sha256.Size)
	return err == nil && subtle.ConstantTimeCompare(derived, expected) == 1
}

func secureToken() (string, error) {
	var raw [32]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(raw[:]), nil
}
