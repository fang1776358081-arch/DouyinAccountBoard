package backuprestore

import (
	"context"
	"database/sql"
	"path/filepath"
	"time"
)

const automaticInterval = 4 * time.Hour
const automaticRetention = 30 * 24 * time.Hour

// Status returns the legacy board's backup indicator fields. The directory is
// a display name only, so ordinary board readers do not receive a local path.
func (s *Service) Status() map[string]any {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return map[string]any{"enabled": s.enabled, "last_backup": s.last, "error": s.failure, "directory": filepath.Base(s.directory.Name())}
}

// RunAutomatic performs a consistent read-only snapshot every four hours and
// prunes only expired automatic archives after a successful new snapshot.
func (s *Service) RunAutomatic(ctx context.Context) {
	s.mu.Lock()
	if s.enabled {
		s.mu.Unlock()
		return
	}
	s.enabled = true
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		s.enabled = false
		s.mu.Unlock()
	}()
	ticker := time.NewTicker(time.Minute)
	defer ticker.Stop()
	for {
		attempt, cancel := context.WithTimeout(ctx, 5*time.Minute)
		created, err := s.automatic(attempt, time.Now())
		cancel()
		if ctx.Err() != nil {
			return
		}
		s.mu.Lock()
		if err != nil {
			s.failure = "自动备份失败，请检查备份目录权限和磁盘空间；软件将自动重试"
		} else {
			s.last = created.Format(time.RFC3339)
			s.failure = ""
		}
		s.mu.Unlock()
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func (s *Service) automatic(ctx context.Context, now time.Time) (time.Time, error) {
	files, err := s.list("automatic")
	if err != nil {
		return time.Time{}, err
	}
	sortNewest(files)
	if len(files) > 0 {
		last := time.Unix(files[0].At, 0)
		if now.Sub(last) < automaticInterval {
			return last, nil
		}
	}
	tx, err := s.write.DB.BeginTx(ctx, &sql.TxOptions{ReadOnly: true, Isolation: sql.LevelRepeatableRead})
	if err != nil {
		return time.Time{}, err
	}
	defer tx.Rollback()
	definition, err := readCatalog(ctx, tx)
	if err != nil {
		return time.Time{}, err
	}
	id, err := s.writeArchive(ctx, tx, definition, "automatic", "auto")
	if err != nil {
		return time.Time{}, err
	}
	if err := tx.Commit(); err != nil {
		return time.Time{}, err
	}
	info, err := s.directory.Stat(id)
	if err != nil {
		return time.Time{}, err
	}
	for _, old := range files {
		if now.Sub(time.Unix(old.At, 0)) > automaticRetention {
			if err := s.directory.Remove(old.ID); err != nil {
				return info.ModTime(), err
			}
		}
	}
	return info.ModTime(), nil
}
