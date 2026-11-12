package session

import (
	"context"
	"database/sql"
	"errors"
	"net/http"
)

type AdmissionError struct{ Code string }

func (err AdmissionError) Error() string { return err.Code }

// BeginWrite takes the same lock as the Python writer and maintenance tools.
// The caller checks permissions and performs its changes inside the transaction.
func BeginWrite(ctx context.Context, db *sql.DB, epoch int64, generation string) (*sql.Tx, error) {
	if epoch <= 0 || generation == "" {
		return nil, AdmissionError{Code: "WRITER_CONFIGURATION_REQUIRED"}
	}
	tx, err := db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	if err != nil {
		return nil, err
	}
	if err := admitWrite(ctx, tx, epoch, generation); err != nil {
		_ = tx.Rollback()
		return nil, err
	}
	return tx, nil
}

func admitWrite(ctx context.Context, tx *sql.Tx, epoch int64, generation string) error {
	if _, err := tx.ExecContext(ctx, "SET LOCAL lock_timeout = '5s'"); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, "SET LOCAL statement_timeout = '15s'"); err != nil {
		return err
	}
	var currentEpoch int64
	var mode, currentGeneration string
	err := tx.QueryRowContext(ctx, `
SELECT mode, writer_epoch, restore_generation
FROM runtime_control WHERE id = 1 FOR UPDATE
`).Scan(&mode, &currentEpoch, &currentGeneration)
	if errors.Is(err, sql.ErrNoRows) {
		return AdmissionError{Code: "CONTROL_STATE_MISSING"}
	}
	if err != nil {
		return err
	}
	switch {
	case mode != "open":
		return AdmissionError{Code: "MAINTENANCE"}
	case currentEpoch != epoch:
		return AdmissionError{Code: "WRITER_VERSION_RETIRED"}
	case currentGeneration != generation:
		return AdmissionError{Code: "RESTORE_GENERATION_EXPIRED"}
	default:
		return nil
	}
}

func replyWriteFailure(w http.ResponseWriter, err error) {
	var admission AdmissionError
	if errors.As(err, &admission) {
		status := http.StatusServiceUnavailable
		if admission.Code == "WRITER_VERSION_RETIRED" || admission.Code == "RESTORE_GENERATION_EXPIRED" {
			status = http.StatusConflict
		}
		replyJSON(w, status, map[string]string{
			"error": "当前运行状态不允许写入，请刷新后重试",
			"code":  admission.Code,
		})
		return
	}
	replyError(w, http.StatusServiceUnavailable, "登录服务暂时不可用")
}
