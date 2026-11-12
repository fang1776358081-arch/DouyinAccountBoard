// Package bootstrap initializes a new database with the existing reviewed schema.
package bootstrap

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"embed"
	"encoding/hex"
	"errors"
	"fmt"
	"io/fs"
)

//go:embed migrations/*.sql
var migrations embed.FS

func Initialize(ctx context.Context, db *sql.DB) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("无法开始空库初始化: %w", err)
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, "SET LOCAL lock_timeout='5s'"); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, "SELECT pg_advisory_xact_lock(811032001)"); err != nil {
		return err
	}
	var existing bool
	if err := tx.QueryRowContext(ctx, `SELECT to_regnamespace('board_candidate') IS NOT NULL OR EXISTS (
		SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
		WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname !~ '^pg_'
		AND c.relkind IN ('r','p','v','m','f','S')
		AND NOT (n.nspname='board_app' AND c.relname='environment'))`).Scan(&existing); err != nil {
		return err
	}
	if existing {
		return errors.New("目标已有资料或工作台结构，空库初始化只接受空数据库")
	}
	if _, err := tx.ExecContext(ctx, `CREATE SCHEMA board_candidate;
		CREATE TABLE board_candidate.schema_migrations(version bigint PRIMARY KEY,sha256 text NOT NULL);
		SET LOCAL search_path=board_candidate,pg_catalog`); err != nil {
		return err
	}
	names, err := fs.Glob(migrations, "migrations/*.sql")
	if err != nil {
		return err
	}
	if len(names) != 7 {
		return errors.New("初始化所需的迁移文件不完整")
	}
	for i, name := range names {
		source, err := migrations.ReadFile(name)
		if err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, string(source)); err != nil {
			return fmt.Errorf("无法应用工作台结构 %04d: %w", i+1, err)
		}
		digest := sha256.Sum256(source)
		if _, err := tx.ExecContext(ctx, "INSERT INTO schema_migrations(version,sha256) VALUES($1,$2)", i+1, hex.EncodeToString(digest[:])); err != nil {
			return err
		}
	}
	var generation [16]byte
	rand.Read(generation[:])
	if _, err := tx.ExecContext(ctx, "UPDATE runtime_control SET mode='open',restore_generation=$1 WHERE id=1", hex.EncodeToString(generation[:])); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO collection_settings(id,auto_enabled,paused) VALUES(1,0,1);
		INSERT INTO collection_worker(id) VALUES(1)`); err != nil {
		return err
	}
	return tx.Commit()
}
