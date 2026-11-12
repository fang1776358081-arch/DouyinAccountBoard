package backuprestore

import (
	"bytes"
	"context"
	"crypto/pbkdf2"
	"crypto/sha256"
	"crypto/subtle"
	"database/sql"
	"encoding/hex"
	"errors"
	"math"
	"strconv"
	"strings"
	"unicode/utf8"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/session"
)

const restoreReason = "恢复后请核对资料，再手动继续采集"

type staging struct {
	tx      *sql.Tx
	current table
	chunk   bytes.Buffer
	rows    int
}

func lockBusiness(ctx context.Context, tx *sql.Tx, definition catalog) error {
	names := make([]string, 0, len(definition.tables)+2)
	for _, spec := range definition.tables {
		names = append(names, schema+spec.name)
	}
	names = append(names, schema+"collection_jobs", schema+"collection_worker")
	_, err := tx.ExecContext(ctx, `LOCK TABLE `+strings.Join(names, ",")+` IN SHARE ROW EXCLUSIVE MODE`)
	return err
}

func newStaging(ctx context.Context, tx *sql.Tx, definition catalog) (*staging, error) {
	for _, spec := range definition.tables {
		_, err := tx.ExecContext(ctx, `CREATE TEMP TABLE restore_`+spec.name+` ON COMMIT DROP AS TABLE `+schema+spec.name+` WITH NO DATA`)
		if err != nil {
			return nil, err
		}
	}
	return &staging{tx: tx}, nil
}

func (stage *staging) add(ctx context.Context, spec table, row []byte) error {
	if stage.rows > 0 && (stage.current.name != spec.name || stage.rows == 256 || stage.chunk.Len()+len(row) > maxRecord) {
		if err := stage.flush(ctx); err != nil {
			return err
		}
	}
	stage.current = spec
	if stage.rows == 0 {
		stage.chunk.WriteByte('[')
	} else {
		stage.chunk.WriteByte(',')
	}
	stage.chunk.Write(row)
	stage.rows++
	return nil
}

func (stage *staging) flush(ctx context.Context) error {
	if stage.rows == 0 {
		return nil
	}
	name := stage.current.name
	stage.chunk.WriteByte(']')
	result, err := stage.tx.ExecContext(ctx, `INSERT INTO pg_temp.restore_`+name+`
SELECT * FROM json_populate_recordset(NULL::`+schema+name+`,$1::json)`, stage.chunk.String())
	if err != nil {
		return err
	}
	count, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if count != int64(stage.rows) {
		return errBadArchive
	}
	stage.rows = 0
	stage.chunk.Reset()
	return nil
}

func replaceBusiness(ctx context.Context, tx *sql.Tx, definition catalog, counts map[string]int64) error {
	if _, err := tx.ExecContext(ctx, `SET CONSTRAINTS ALL DEFERRED`); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `DELETE FROM board_candidate.collection_jobs`); err != nil {
		return err
	}
	for i := len(definition.tables) - 1; i >= 0; i-- {
		if _, err := tx.ExecContext(ctx, `DELETE FROM `+schema+definition.tables[i].name); err != nil {
			return err
		}
	}
	for _, spec := range definition.tables {
		result, err := tx.ExecContext(ctx, `INSERT INTO `+schema+spec.name+` SELECT * FROM pg_temp.restore_`+spec.name)
		if err != nil {
			return err
		}
		count, err := result.RowsAffected()
		if err != nil {
			return err
		}
		if count != counts[spec.name] {
			return errBadArchive
		}
	}
	if _, err := tx.ExecContext(ctx, `SET CONSTRAINTS ALL IMMEDIATE`); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO board_candidate.collection_settings(id,auto_enabled,paused,pause_reason) VALUES(1,0,1,$1)
ON CONFLICT(id) DO UPDATE SET auto_enabled=0,paused=1,pause_reason=excluded.pause_reason`, restoreReason); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `UPDATE board_candidate.collection_worker SET seen=0,status='',command='pause' WHERE id=1`); err != nil {
		return err
	}
	return ensureCardIdentity(ctx, tx)
}

func ensureCardIdentity(ctx context.Context, tx *sql.Tx) error {
	var maxID, last int64
	var used bool
	if err := tx.QueryRowContext(ctx, `SELECT COALESCE(MAX(id),0) FROM board_candidate.phone_cards`).Scan(&maxID); err != nil {
		return err
	}
	if err := tx.QueryRowContext(ctx, `SELECT last_value,is_called FROM board_candidate.phone_cards_id_seq`).Scan(&last, &used); err != nil {
		return err
	}
	if maxID < last || maxID == last && used {
		return nil
	}
	if maxID == math.MaxInt64 {
		return errors.New("phone card identity exhausted")
	}
	// ALTER SEQUENCE RESTART is transactional, unlike setval. It only moves
	// forward if an explicitly assigned archived ID exceeds the live sequence.
	_, err := tx.ExecContext(ctx, `ALTER SEQUENCE board_candidate.phone_cards_id_seq RESTART WITH `+strconv.FormatInt(maxID+1, 10))
	return err
}

func (s *Service) rotateGeneration(ctx context.Context, tx *sql.Tx, actor session.User, archive string) error {
	newID, err := accountwrite.NewGroup()
	if err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `DELETE FROM board_candidate.operations_previews`); err != nil {
		return err
	}
	if err := accountwrite.InsertHistory(ctx, tx, actor, "reload", newID, "sync:reload", nil, nil, nil); err != nil {
		return err
	}
	if err := accountwrite.Audit(ctx, tx, actor, "恢复业务资料", archive); err != nil {
		return err
	}
	result, err := tx.ExecContext(ctx, `UPDATE board_candidate.runtime_control SET restore_generation=$1
WHERE id=1 AND mode='open' AND writer_epoch=$2 AND restore_generation=$3`, "restore-"+newID, s.write.WriterEpoch, s.write.RestoreGeneration)
	if err != nil {
		return err
	}
	count, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if count != 1 {
		return accountwrite.Conflict("恢复代次已改变，请重新读取")
	}
	return nil
}

func verifyOwnerPassword(ctx context.Context, tx *sql.Tx, actor session.User, supplied any) error {
	password, ok := supplied.(string)
	if !ok || utf8.RuneCountInString(password) > 128 {
		return accountwrite.Invalid("总管理员密码不正确")
	}
	var encoded string
	if err := tx.QueryRowContext(ctx, `SELECT password_hash FROM board_candidate.auth_users WHERE id=$1 AND role='owner' AND active=1`, actor.ID).Scan(&encoded); err != nil {
		return err
	}
	parts := strings.Split(encoded, "$")
	if len(parts) == 4 && parts[0] == "pbkdf2_sha256" && len(parts[2]) > 0 && len(parts[2]) <= 256 {
		iterations, itErr := strconv.Atoi(parts[1])
		wanted, hexErr := hex.DecodeString(parts[3])
		if itErr == nil && hexErr == nil && iterations > 0 && iterations <= 1200000 && len(wanted) == sha256.Size {
			result, err := pbkdf2.Key(sha256.New, password, []byte(parts[2]), iterations, sha256.Size)
			if err == nil && subtle.ConstantTimeCompare(result, wanted) == 1 {
				return nil
			}
		}
	}
	return accountwrite.Invalid("总管理员密码不正确")
}
