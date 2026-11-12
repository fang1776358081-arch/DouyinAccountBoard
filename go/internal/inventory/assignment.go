package inventory

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"time"

	"douyinboard/internal/accountwrite"
)

func workflowAvailable(ctx context.Context, tx *sql.Tx) (bool, error) {
	const query = `SELECT to_regclass('board_candidate.phone_units') IS NOT NULL
		AND to_regclass('board_candidate.phone_card_placements') IS NOT NULL
		AND to_regclass('board_candidate.phone_account_access') IS NOT NULL
		AND to_regclass('board_candidate.device_cases') IS NOT NULL
		AND to_regclass('board_candidate.device_workflow_events') IS NOT NULL`
	var available bool
	err := tx.QueryRowContext(ctx, query).Scan(&available)
	return available, err
}

func activeUnit(ctx context.Context, tx *sql.Tx, deviceID string) (map[string]any, error) {
	var id string
	err := tx.QueryRowContext(ctx, `SELECT id FROM board_candidate.phone_units WHERE device_id=$1 AND active=1`, deviceID).Scan(&id)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return readUnit(ctx, tx, id)
}

func ensureUnit(ctx context.Context, tx *sql.Tx, deviceID string) (map[string]any, error) {
	row, err := activeUnit(ctx, tx, deviceID)
	if err != nil || row != nil {
		return row, err
	}
	var previous bool
	if err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM board_candidate.phone_units WHERE device_id=$1)`, deviceID).Scan(&previous); err != nil {
		return nil, err
	}
	if previous {
		return nil, accountwrite.Invalid("该编号没有现用实物，请通过新机补号登记")
	}
	group, err := accountwrite.NewGroup()
	if err != nil {
		return nil, err
	}
	id := "unit-" + group
	result, err := tx.ExecContext(ctx, `INSERT INTO board_candidate.phone_units
		(id,device_id,original_device_id,generation,status,created_at)
		SELECT $1,id,id,1,state,$3 FROM board_candidate.phone_devices WHERE id=$2`, id, deviceID, time.Now().Unix())
	if err != nil {
		return nil, err
	}
	if count, err := result.RowsAffected(); err != nil || count != 1 {
		if err != nil {
			return nil, err
		}
		return nil, accountwrite.Invalid("手机编号不存在")
	}
	return readUnit(ctx, tx, id)
}

func mergeOfficeReferences(groups ...[]string) []string {
	result := []string{}
	for _, list := range groups {
		if len(list) == 0 {
			return []string{}
		}
		result = append(result, list...)
	}
	slices.Sort(result)
	return slices.Compact(result)
}

func readUnit(ctx context.Context, tx *sql.Tx, id string) (map[string]any, error) {
	unit, err := readRecord(ctx, tx, unitRelation, id)
	if err != nil || unit == nil {
		return unit, err
	}
	device, err := DeviceRecord(ctx, tx, stringField(unit, "device_id"))
	if err != nil {
		return nil, err
	}
	original := device
	if value := stringField(unit, "original_device_id"); value != stringField(unit, "device_id") {
		original, err = DeviceRecord(ctx, tx, value)
		if err != nil {
			return nil, err
		}
	}
	unit["office_ids"] = mergeOfficeReferences(recordOffices(device), recordOffices(original))
	return unit, nil
}

func readAccess(ctx context.Context, tx *sql.Tx, accountID string) (map[string]any, error) {
	row, err := readRecord(ctx, tx, accessRelation, accountID)
	if err != nil || row == nil {
		return row, err
	}
	account, err := readRecord(ctx, tx, accountRelation, accountID)
	if err != nil {
		return nil, err
	}
	scopes := [][]string{recordOffices(account)}
	for _, key := range []string{"last_unit_id", "target_unit_id"} {
		if id := stringField(row, key); id != "" {
			unit, err := readUnit(ctx, tx, id)
			if err != nil {
				return nil, err
			}
			scopes = append(scopes, recordOffices(unit))
		}
	}
	if id := stringField(row, "case_id"); id != "" {
		item, err := readRecord(ctx, tx, caseRelation, id)
		if err != nil {
			return nil, err
		}
		unit, err := readUnit(ctx, tx, stringField(item, "unit_id"))
		if err != nil {
			return nil, err
		}
		scopes = append(scopes, recordOffices(unit))
		var accounts []string
		if item != nil {
			if err := json.Unmarshal([]byte(stringField(item, "account_ids")), &accounts); err != nil {
				return nil, err
			}
		}
		for _, id := range accounts {
			account, err := readRecord(ctx, tx, accountRelation, id)
			if err != nil {
				return nil, err
			}
			scopes = append(scopes, recordOffices(account))
		}
	}
	row["office_ids"] = mergeOfficeReferences(scopes...)
	return row, nil
}

// AssignmentRecords loads the exact related rows covered by the account-device
// edit's optimistic versions. Callers must authorize the account and devices.
func AssignmentRecords(ctx context.Context, tx *sql.Tx, accountID, targetID string) (map[string]map[string]any, error) {
	var old sql.NullString
	if accountID != "" {
		err := tx.QueryRowContext(ctx, `SELECT phone_device_id FROM board_candidate.accounts WHERE id=$1`, accountID).Scan(&old)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return nil, err
		}
	}
	return assignmentRecords(ctx, tx, accountID, old.String, targetID)
}

func assignmentRecords(ctx context.Context, tx *sql.Tx, accountID, oldID, targetID string) (snapshot, error) {
	records := snapshot{}
	workflow, err := workflowAvailable(ctx, tx)
	if err != nil {
		return nil, err
	}
	for _, id := range []string{oldID, targetID} {
		if id == "" {
			continue
		}
		record, err := DeviceRecord(ctx, tx, id)
		if err != nil {
			return nil, err
		}
		records["device:"+id] = record
		if workflow && oldID != targetID {
			unit, err := activeUnit(ctx, tx, id)
			if err != nil {
				return nil, err
			}
			if unit != nil {
				records["unit:"+stringField(unit, "id")] = unit
			}
		}
	}
	if workflow && oldID != targetID && accountID != "" {
		access, err := readAccess(ctx, tx, accountID)
		if err != nil {
			return nil, err
		}
		records["access:"+accountID] = access
		for _, key := range []string{"last_unit_id", "target_unit_id"} {
			if id := stringField(access, key); id != "" {
				unit, err := readUnit(ctx, tx, id)
				if err != nil {
					return nil, err
				}
				records["unit:"+id] = unit
			}
		}
	}
	return records, nil
}

// RefreshAssignmentRecords rereads checked rows and any units created during
// assignment. previousDevice is the account's phone before the edit.
func RefreshAssignmentRecords(ctx context.Context, tx *sql.Tx, accountID, previousDevice, targetID string, before map[string]map[string]any) (map[string]map[string]any, error) {
	after, err := assignmentRecords(ctx, tx, accountID, previousDevice, targetID)
	if err != nil {
		return nil, err
	}
	for key := range before {
		if _, found := after[key]; found {
			continue
		}
		prefix, id, _ := strings.Cut(key, ":")
		var row map[string]any
		switch prefix {
		case "unit":
			row, err = readUnit(ctx, tx, id)
		case "access":
			row, err = readAccess(ctx, tx, id)
		case "device":
			row, err = DeviceRecord(ctx, tx, id)
		default:
			continue
		}
		if err != nil {
			return nil, err
		}
		after[key] = row
	}
	return after, nil
}

// AssignAccountDevice updates only the explicitly assigned login phone and its
// workflow records. The caller must enforce authorization and record versions.
func AssignAccountDevice(ctx context.Context, tx *sql.Tx, accountID, targetID string) error {
	var old sql.NullString
	err := tx.QueryRowContext(ctx, `SELECT phone_device_id FROM board_candidate.accounts WHERE id=$1`, accountID).Scan(&old)
	if errors.Is(err, sql.ErrNoRows) {
		return accountwrite.Invalid("账号不存在")
	}
	if err != nil || old.String == targetID {
		return err
	}
	if targetID != "" {
		target, err := DeviceRecord(ctx, tx, targetID)
		if err != nil {
			return err
		}
		if target == nil {
			return accountwrite.Invalid("请选择已有手机设备，或先新增手机")
		}
		if stringField(target, "state") == "故障" {
			return accountwrite.Invalid("该手机标记为故障，请先核对设备状态")
		}
	}
	workflow, err := workflowAvailable(ctx, tx)
	if err != nil {
		return err
	}
	if workflow {
		if err := syncAssignment(ctx, tx, accountID, old.String, targetID); err != nil {
			return err
		}
	}
	if _, err := tx.ExecContext(ctx, `UPDATE board_candidate.accounts
		SET phone_device_id=NULLIF($2,''),phone_device_source='manual',
		    office_group=CASE WHEN $2='' THEN office_group WHEN $2 LIKE 'office3:%' THEN 'three' ELSE '' END
		WHERE id=$1`, accountID, targetID); err != nil {
		return err
	}
	if targetID != "" {
		if _, err := tx.ExecContext(ctx, `UPDATE board_candidate.phone_devices SET state_source='auto' WHERE id=$1 AND state<>'故障'`, targetID); err != nil {
			return err
		}
	}
	if old.Valid {
		if _, err := tx.ExecContext(ctx, `UPDATE board_candidate.phone_devices SET state='待确认',state_source='auto'
			WHERE id=$1 AND state='在用' AND NOT EXISTS(SELECT 1 FROM board_candidate.accounts WHERE phone_device_id=$1)`, old.String); err != nil {
			return err
		}
	}
	return nil
}

func syncAssignment(ctx context.Context, tx *sql.Tx, accountID, oldDevice, targetDevice string) error {
	targetUnit := ""
	if targetDevice != "" {
		unit, err := ensureUnit(ctx, tx, targetDevice)
		if err != nil {
			return err
		}
		targetUnit = stringField(unit, "id")
	}
	access, err := readRecord(ctx, tx, accessRelation, accountID)
	if err != nil {
		return err
	}
	if access == nil {
		oldUnit := ""
		if oldDevice != "" {
			unit, err := ensureUnit(ctx, tx, oldDevice)
			if err != nil {
				return err
			}
			oldUnit = stringField(unit, "id")
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO board_candidate.phone_account_access(account_id,last_unit_id)
			VALUES($1,NULLIF($2,''))`, accountID, oldUnit); err != nil {
			return err
		}
		access = map[string]any{"last_unit_id": oldUnit}
	}
	oldUnit := stringField(access, "last_unit_id")
	if targetUnit != "" && oldUnit == targetUnit {
		return nil
	}
	if stringField(access, "login_state") == "登录受阻" {
		return accountwrite.Invalid("账号登录受阻，请通过恢复记录处理，不能改写最后登录实物")
	}
	if caseID := stringField(access, "case_id"); caseID != "" {
		var state string
		err := tx.QueryRowContext(ctx, `SELECT status FROM board_candidate.device_cases WHERE id=$1`, caseID).Scan(&state)
		if errors.Is(err, sql.ErrNoRows) {
			return accountwrite.Invalid("原处理记录不存在")
		}
		if err != nil {
			return err
		}
		if state != "已结案" {
			return accountwrite.Invalid("账号故障处理尚未结案，请通过恢复记录处理设备变更")
		}
	}
	if _, err := tx.ExecContext(ctx, `UPDATE board_candidate.phone_account_access
		SET last_unit_id=NULLIF($2,''),target_unit_id=NULL,login_state='待确认',recovery_state='待处理',
		    reason='',owner='',followup_on='',case_id=NULL,confirmed_at=0,revision=revision+1 WHERE account_id=$1`, accountID, targetUnit); err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `UPDATE board_candidate.phone_units SET revision=revision+1
		WHERE id IN ($1,$2) AND id<>''`, oldUnit, targetUnit)
	return err
}
