// Package backuprestore implements PostgreSQL business snapshots for the
// owner-only workbench. It does not execute SQL from backup files.
package backuprestore

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"runtime"
	"slices"
	"strings"
	"sync"
	"time"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/session"
)

const (
	ListPath    = "/api/workspace/backups"
	ManualPath  = "/api/workspace/backup-now"
	PreviewPath = "/api/workspace/restore-preview"
	ApplyPath   = "/api/workspace/restore-apply"
)

const previewKind = "postgres-business-restore"

type Config struct {
	Write     accountwrite.Service
	Directory string
}

type Service struct {
	write     accountwrite.Service
	directory *os.Root
	mu        sync.RWMutex
	enabled   bool
	last      string
	failure   string
}

func New(config Config) (*Service, error) {
	if config.Write.DB == nil || config.Directory == "" {
		return nil, errors.New("PostgreSQL backup directory and database are required")
	}
	rootPath, err := filepath.Abs(config.Directory)
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(rootPath, 0o700); err != nil {
		return nil, err
	}
	if entry, err := os.Lstat(rootPath); err != nil || !privateDirectory(entry) {
		return nil, errors.New("PostgreSQL backup directory must be private")
	}
	directory, err := os.OpenRoot(rootPath)
	if err != nil {
		return nil, err
	}
	if err := directory.Mkdir("automatic", 0o700); err != nil && !errors.Is(err, os.ErrExist) {
		directory.Close()
		return nil, err
	}
	if entry, err := directory.Lstat("automatic"); err != nil || !privateDirectory(entry) {
		directory.Close()
		return nil, errors.New("automatic backup directory must be private")
	}
	return &Service{write: config.Write, directory: directory}, nil
}

func privateDirectory(entry os.FileInfo) bool {
	return entry.IsDir() && (runtime.GOOS == "windows" || entry.Mode().Perm()&0o077 == 0)
}

func (s *Service) Close() error { return s.directory.Close() }

func authorizeOwner(_ context.Context, _ *sql.Tx, actor session.User, _ map[string]any) error {
	if !actor.Has("owner") {
		return accountwrite.Forbidden("只有总管理员可以备份和恢复资料")
	}
	return nil
}

func (s *Service) List(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	actor, err := (session.Store{DB: s.write.DB}).Authenticate(ctx, r)
	if errors.Is(err, session.ErrUnauthorized) {
		respond(w, http.StatusUnauthorized, map[string]string{"error": "请先登录工作台"})
		return
	}
	if err != nil {
		respond(w, http.StatusServiceUnavailable, map[string]string{"error": "暂时无法读取备份"})
		return
	}
	if err := authorizeOwner(ctx, nil, actor, nil); err != nil {
		respond(w, http.StatusForbidden, map[string]string{"error": err.Error()})
		return
	}
	manual, err := s.list(".")
	if err == nil {
		var automatic []listItem
		automatic, err = s.list("automatic")
		manual = append(manual, automatic...)
	}
	if err != nil {
		respond(w, http.StatusServiceUnavailable, map[string]string{"error": "暂时无法读取备份"})
		return
	}
	sortNewest(manual)
	respond(w, http.StatusOK, map[string]any{"backups": manual})
}

func (s *Service) list(folder string) ([]listItem, error) {
	dir, err := s.directory.Open(folder)
	if err != nil {
		return nil, err
	}
	defer dir.Close()
	entries, err := dir.ReadDir(-1)
	if err != nil {
		return nil, err
	}
	result := []listItem{}
	for _, entry := range entries {
		id := path.Join(folder, entry.Name())
		if !validArchiveID(id) || entry.Type()&os.ModeSymlink != 0 {
			continue
		}
		info, err := entry.Info()
		if err == nil && privateArchive(info) {
			result = append(result, listItem{id, entry.Name(), info.Size(), info.ModTime().Unix()})
		}
	}
	return result, nil
}

func sortNewest(items []listItem) {
	slices.SortFunc(items, func(a, b listItem) int {
		if a.At > b.At {
			return -1
		}
		if a.At < b.At {
			return 1
		}
		return strings.Compare(a.ID, b.ID)
	})
}

func (s *Service) Manual() http.HandlerFunc {
	return s.write.Direct(100000, false, authorizeOwner, func(ctx context.Context, tx *sql.Tx, actor session.User, _ map[string]any) (map[string]any, error) {
		definition, err := readCatalog(ctx, tx)
		if err != nil {
			return nil, err
		}
		if err := lockBusiness(ctx, tx, definition); err != nil {
			return nil, err
		}
		id, err := s.writeArchive(ctx, tx, definition, ".", "manual")
		if err == nil {
			err = accountwrite.Audit(ctx, tx, actor, "手动备份业务资料", id)
		}
		return map[string]any{"ok": true, "name": id}, err
	})
}

type previewCount struct {
	Current int64 `json:"current"`
	Backup  int64 `json:"backup"`
}

type restorePreview struct {
	BackupID   string                  `json:"backup_id"`
	FileHash   string                  `json:"file_hash"`
	StateHash  string                  `json:"state_hash"`
	SchemaHash string                  `json:"schema_hash"`
	Generation string                  `json:"generation"`
	Counts     map[string]previewCount `json:"counts"`
}

func (s *Service) Preview() http.HandlerFunc {
	return s.write.Direct(100000, false, authorizeOwner, func(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
		id, _ := body["backup_id"].(string)
		if !validArchiveID(id) {
			return nil, accountwrite.Invalid("请选择备份列表中的 PostgreSQL 业务快照")
		}
		definition, err := readCatalog(ctx, tx)
		if err != nil {
			return nil, err
		}
		archive, err := s.readArchive(ctx, id, definition, nil)
		if errors.Is(err, errBadArchive) {
			return nil, accountwrite.Invalid("备份完整性检查失败或结构不一致")
		}
		if err != nil {
			return nil, err
		}
		hash, current, err := stateFingerprint(ctx, tx, definition)
		if err != nil {
			return nil, err
		}
		counts := make(map[string]previewCount, len(current))
		for name, n := range current {
			counts[name] = previewCount{n, archive.Counts[name]}
		}
		plan := restorePreview{id, archive.Hash, hash, definition.hash, s.write.RestoreGeneration, counts}
		raw, err := json.Marshal(plan)
		if err != nil {
			return nil, err
		}
		previewID, err := accountwrite.NewGroup()
		if err != nil {
			return nil, err
		}
		now := time.Now().Unix()
		if _, err := tx.ExecContext(ctx, `DELETE FROM board_candidate.operations_previews WHERE expires_at<$1`, now); err != nil {
			return nil, err
		}
		_, err = tx.ExecContext(ctx, `INSERT INTO board_candidate.operations_previews(id,actor_id,expires_at,kind,payload)
VALUES($1,$2,$3,$4,$5)`, previewID, actor.ID, now+1800, previewKind, string(raw))
		return map[string]any{"preview_id": previewID, "backup_id": id, "file_hash": archive.Hash, "state_hash": hash,
			"counts": counts, "note": "恢复业务资料和资料归属，保留当前登录人员、权限和审计历史。恢复后暂停采集，并需重启 Go 服务。"}, err
	})
}

var previewIDPattern = regexp.MustCompile(`^[a-f0-9]{32}$`)

func (s *Service) Apply() http.HandlerFunc {
	return s.write.Direct(100000, false, authorizeOwner, s.apply)
}

func (s *Service) apply(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	confirmation, _ := body["confirmation"].(string)
	if confirmation != "恢复业务数据" {
		return nil, accountwrite.Invalid("请输入“恢复业务数据”确认")
	}
	if err := verifyOwnerPassword(ctx, tx, actor, body["password"]); err != nil {
		return nil, err
	}
	id, _ := body["preview_id"].(string)
	const expired = "预览已过期或不属于当前登录人员，请重新预览"
	if !previewIDPattern.MatchString(id) {
		return nil, accountwrite.Invalid(expired)
	}
	var raw string
	err := tx.QueryRowContext(ctx, `SELECT payload FROM board_candidate.operations_previews
WHERE id=$1 AND actor_id=$2 AND kind=$3 AND expires_at>$4`, id, actor.ID, previewKind, time.Now().Unix()).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, accountwrite.Invalid(expired)
	}
	if err != nil {
		return nil, err
	}
	var plan restorePreview
	if json.Unmarshal([]byte(raw), &plan) != nil || plan.Generation != s.write.RestoreGeneration {
		return nil, accountwrite.Conflict("恢复代次已改变，请重新预览")
	}
	definition, err := readCatalog(ctx, tx)
	if err != nil {
		return nil, err
	}
	if definition.hash != plan.SchemaHash {
		return nil, accountwrite.Conflict("数据库结构已改变，请重新预览")
	}
	if err := lockBusiness(ctx, tx, definition); err != nil {
		return nil, err
	}
	state, _, err := stateFingerprint(ctx, tx, definition)
	if err != nil {
		return nil, err
	}
	if state != plan.StateHash {
		return nil, accountwrite.Conflict("预览后业务资料发生变化，请重新预览，避免覆盖新记录")
	}
	stage, err := newStaging(ctx, tx, definition)
	if err != nil {
		return nil, err
	}
	archive, err := s.readArchive(ctx, plan.BackupID, definition, func(spec table, row []byte) error {
		return stage.add(ctx, spec, row)
	})
	if errors.Is(err, errBadArchive) || err == nil && archive.Hash != plan.FileHash {
		return nil, accountwrite.Conflict("备份文件已改变，请重新预览")
	}
	if err != nil {
		return nil, err
	}
	if err := stage.flush(ctx); err != nil {
		return nil, err
	}
	rollback, err := s.writeArchive(ctx, tx, definition, ".", "before-restore")
	if err != nil {
		return nil, err
	}
	if err := replaceBusiness(ctx, tx, definition, archive.Counts); err != nil {
		var state interface{ SQLState() string }
		if errors.As(err, &state) && strings.HasPrefix(state.SQLState(), "23") {
			return nil, accountwrite.Invalid("恢复资料关系检查失败，原数据已保留")
		}
		return nil, err
	}
	if err := s.rotateGeneration(ctx, tx, actor, plan.BackupID); err != nil {
		return nil, err
	}
	return map[string]any{"ok": true, "backup_before_restore": rollback, "restart_required": true,
		"note": "业务资料已恢复，登录人员、权限及审计历史已保留，采集已暂停。请重启 Go 服务后继续写入。"}, nil
}

func respond(w http.ResponseWriter, status int, data any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(data)
}
