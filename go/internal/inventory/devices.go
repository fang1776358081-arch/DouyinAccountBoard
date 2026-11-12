package inventory

import (
	"context"
	"database/sql"
	"errors"
	"slices"
	"strings"
	"time"
	"unicode/utf8"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

// AuthorizeDevice checks existing device scope or the new device's destination.
func AuthorizeDevice(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any, create bool) error {
	if _, inline := body["changes"]; inline {
		if create {
			return accountwrite.Invalid("新手机请填写完整资料")
		}
		_, _, _, _, err := authorizeDeviceInline(ctx, tx, actor, body)
		return err
	}
	if create {
		if _, err := requestedOffices(ctx, tx, actor, body); err != nil {
			return err
		}
		return requirePermission(actor, "device_create")
	}
	id, _ := deviceIdentity(valueString(body["id"]))
	if id == "" {
		return accountwrite.Invalid("手机编号请填1至6位正整数")
	}
	old, err := DeviceRecord(ctx, tx, id)
	if err != nil {
		return err
	}
	if err := requireEdit(actor, old); err != nil {
		return err
	}
	return requirePermission(actor, "device_manage")
}

func authorizeDeviceInline(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, string, string, string, error) {
	changes, ok := body["changes"].(map[string]any)
	if !ok || len(changes) != 1 {
		return nil, "", "", "", accountwrite.Invalid("请选择一个手机资料字段修改")
	}
	for key := range body {
		switch key {
		case "id", "changes", "_expected_versions", "request_id":
		default:
			return nil, "", "", "", accountwrite.Invalid("手机单元格修改包含多余字段")
		}
	}
	id, _ := deviceIdentity(valueString(body["id"]))
	if id == "" || id != valueString(body["id"]) {
		return nil, "", "", "", accountwrite.Invalid("请使用当前手机的完整编号")
	}
	if err := AuthorizeDevice(ctx, tx, actor, map[string]any{"id": id}, false); err != nil {
		return nil, "", "", "", err
	}
	for field, raw := range changes {
		limit := 100
		switch field {
		case "name", "owner":
		case "location":
			limit = 200
		default:
			return nil, "", "", "", accountwrite.Invalid("此手机字段请在详情中处理")
		}
		value, ok := raw.(string)
		if !ok || strings.ContainsRune(value, 0) {
			return nil, "", "", "", accountwrite.Invalid("手机资料格式不正确")
		}
		value = strings.TrimSpace(value)
		if utf8.RuneCountInString(value) > limit || field == "name" && value == "" {
			return nil, "", "", "", accountwrite.Invalid("名称不能为空，手机资料不能超过字段长度限制")
		}
		old, err := DeviceRecord(ctx, tx, id)
		return old, id, field, value, err
	}
	return nil, "", "", "", accountwrite.Invalid("请选择一个手机资料字段修改")
}

func saveDeviceInline(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	old, id, field, value, err := authorizeDeviceInline(ctx, tx, actor, body)
	if err != nil {
		return nil, err
	}
	key := "device:" + id
	before := snapshot{key: old}
	if err := before.check(body); err != nil {
		return nil, err
	}
	// Metadata editing never changes physical state, scope, or a related record.
	query := `UPDATE board_candidate.phone_devices SET name=$1 WHERE id=$2`
	switch field {
	case "owner":
		query = `UPDATE board_candidate.phone_devices SET owner=$1 WHERE id=$2`
	case "location":
		query = `UPDATE board_candidate.phone_devices SET location=$1 WHERE id=$2`
	}
	if old[field] != value {
		if _, err := tx.ExecContext(ctx, query, value, id); err != nil {
			return nil, err
		}
	}
	next, err := DeviceRecord(ctx, tx, id)
	if err != nil {
		return nil, err
	}
	versions, err := before.finish(ctx, tx, actor, DevicePath, id, snapshot{key: next})
	return map[string]any{"ok": true, "id": id, "_record_versions": versions}, err
}

func deviceScope(ctx context.Context, tx *sql.Tx, id, serial string, old map[string]any, wanted []string) error {
	if old != nil && slices.Equal(recordOffices(old), wanted) {
		return nil
	}
	if len(wanted) != 1 {
		return accountwrite.Invalid("每台手机必须明确选择一个办公室，不能共享或清空归属")
	}
	rows, err := tx.QueryContext(ctx, `SELECT device_id FROM board_candidate.phone_device_offices
		WHERE office_id=$1 AND device_id<>$2`, wanted[0], id)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var existingID string
		if err := rows.Scan(&existingID); err != nil {
			return err
		}
		if _, existing := deviceIdentity(existingID); existing == serial {
			return accountwrite.Invalid("这个办公室已经有" + serial + "号手机，请选择其他序号；原资料未改动")
		}
	}
	return rows.Err()
}

// ValidateScopeChange reuses device-number uniqueness and one-office rules
// when the owner explicitly assigns a device to another office.
func ValidateScopeChange(ctx context.Context, tx *sql.Tx, id string, previous, wanted []string) error {
	if slices.Equal(previous, wanted) {
		return nil
	}
	if len(wanted) != 1 {
		return accountwrite.Invalid("每台手机必须明确选择一个办公室，不能共享或清空归属")
	}
	_, serial := deviceIdentity(id)
	if serial == "" {
		return accountwrite.Invalid("该旧设备的手机序号待核对，不能自动更改归属")
	}
	return deviceScope(ctx, tx, id, serial, map[string]any{"office_ids": previous}, wanted)
}

// DeviceVersions returns the unmodified device and active-unit tokens used
// by saves. Callers must establish permission to read the selected device.
func DeviceVersions(ctx context.Context, tx *sql.Tx, id string) (map[string]any, error) {
	device, err := DeviceRecord(ctx, tx, id)
	if err != nil {
		return nil, err
	}
	records := snapshot{"device:" + id: device}
	workflow, err := workflowAvailable(ctx, tx)
	if err != nil {
		return nil, err
	}
	if workflow && device != nil {
		unit, err := activeUnit(ctx, tx, id)
		if err != nil {
			return nil, err
		}
		if unit != nil {
			records["unit:"+stringField(unit, "id")] = unit
		}
	}
	return records.versions()
}

// SaveDevice saves a device and its current physical unit in the caller's
// admitted transaction. Version and history records include affected units.
func SaveDevice(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any, create bool) (map[string]any, error) {
	if _, inline := body["changes"]; inline && !create {
		return saveDeviceInline(ctx, tx, actor, body)
	}
	if err := AuthorizeDevice(ctx, tx, actor, body, create); err != nil {
		return nil, err
	}
	action := DevicePath
	id, serial := deviceIdentity(valueString(body["id"]))
	if create {
		action = CreateDevicePath
		if valueString(body["id"]) == "" {
			serial = serialNumber(valueString(body["number"]))
			if serial == "" {
				return nil, accountwrite.Invalid("手机序号请填1至6位正整数")
			}
			key, err := accountwrite.NewGroup()
			if err != nil {
				return nil, err
			}
			id = "phone:" + key + ":" + serial
		}
	}
	if id == "" {
		return nil, accountwrite.Invalid("手机编号请填1至6位正整数")
	}
	if value, supplied := body["number"]; supplied && serialNumber(valueString(value)) != serial {
		return nil, accountwrite.Invalid("手机序号与内部编号不一致；已有设备请通过重新编号流程处理")
	}
	old, err := DeviceRecord(ctx, tx, id)
	if err != nil {
		return nil, err
	}
	if create && old != nil {
		return nil, accountwrite.Invalid("这个手机编号已经存在，请编辑已有设备")
	}
	before := snapshot{}
	workflow, err := workflowAvailable(ctx, tx)
	if err != nil {
		return nil, err
	}
	if !create {
		before["device:"+id] = old
		if workflow {
			unit, err := activeUnit(ctx, tx, id)
			if err != nil {
				return nil, err
			}
			if unit != nil {
				before["unit:"+stringField(unit, "id")] = unit
			}
		}
		if err := before.check(body); err != nil {
			return nil, err
		}
	}
	offices := recordOffices(old)
	_, changing := body["office_ids"]
	if create || changing {
		offices, err = requestedOffices(ctx, tx, actor, body)
		if err != nil {
			return nil, err
		}
		if err := deviceScope(ctx, tx, id, serial, old, offices); err != nil {
			return nil, err
		}
	}
	state, err := enumField(body, "state", "待确认", append(slices.Clone(deviceStates), "auto"), "手机设备状态不正确")
	if err != nil {
		return nil, err
	}
	source := "manual"
	if state == "auto" {
		state, source = "待确认", "auto"
	}
	if state == "可用闲置" {
		linked, err := hasDeviceOccupancy(ctx, tx, id, workflow)
		if err != nil {
			return nil, err
		}
		if linked {
			return nil, accountwrite.Invalid("仍有当前账号或手机卡关联，请先移出并确认清理完成，再标记可用闲置")
		}
	}
	values := make(map[string]string)
	for _, rule := range []textRule{{"name", 100}, {"owner", 100}, {"location", 200}, {"note", 2000}} {
		value, err := boundedText(body, rule.key, "", rule.limit, "手机资料内容过长")
		if err != nil {
			return nil, err
		}
		values[rule.key] = value
	}
	if values["name"] == "" {
		values["name"] = serial + "号手机"
		if len(offices) == 1 {
			var name string
			err := tx.QueryRowContext(ctx, `SELECT name FROM board_candidate.offices WHERE id=$1`, offices[0]).Scan(&name)
			if err != nil && !errors.Is(err, sql.ErrNoRows) {
				return nil, err
			}
			if name != "" {
				values["name"] = name + " · " + values["name"]
			}
		}
	}
	params := []any{id, values["name"], state, source, values["owner"], values["location"], values["note"]}
	if create {
		_, err = tx.ExecContext(ctx, `INSERT INTO board_candidate.phone_devices
			(id,name,state,state_source,owner,location,note) VALUES($1,$2,$3,$4,$5,$6,$7)`, params...)
	} else {
		_, err = tx.ExecContext(ctx, `UPDATE board_candidate.phone_devices
			SET name=$2,state=$3,state_source=$4,owner=$5,location=$6,note=$7 WHERE id=$1`, params...)
	}
	if err != nil {
		return nil, err
	}
	if create || changing {
		if err := saveOffices(ctx, tx, deviceRelation, id, offices); err != nil {
			return nil, err
		}
	}
	after := snapshot{}
	if workflow {
		unit, err := ensureUnit(ctx, tx, id)
		if err != nil {
			return nil, err
		}
		unitID := stringField(unit, "id")
		if stringField(unit, "status") != state && (create || state != old["state"] || source != old["state_source"]) {
			event, err := changeUnitState(ctx, tx, actor, unit, state)
			if err != nil {
				return nil, err
			}
			after["workflow_event:"+stringField(event, "id")] = event
			unit, err = readUnit(ctx, tx, unitID)
			if err != nil {
				return nil, err
			}
		}
		after["unit:"+unitID] = unit
	}
	next, err := DeviceRecord(ctx, tx, id)
	if err != nil {
		return nil, err
	}
	after["device:"+id] = next
	versions, err := before.finish(ctx, tx, actor, action, id, after)
	return map[string]any{"ok": true, "id": id, "_record_versions": versions}, err
}

func hasDeviceOccupancy(ctx context.Context, tx *sql.Tx, id string, workflow bool) (bool, error) {
	query := `SELECT EXISTS(SELECT 1 FROM board_candidate.accounts WHERE phone_device_id=$1)`
	if workflow {
		query = `SELECT EXISTS(SELECT 1 FROM board_candidate.phone_units u
			WHERE u.device_id=$1 AND u.active=1 AND (
				EXISTS(SELECT 1 FROM board_candidate.phone_card_placements p WHERE p.unit_id=u.id)
				OR EXISTS(SELECT 1 FROM board_candidate.phone_account_access a WHERE a.last_unit_id=u.id)
			))`
	}
	var occupied bool
	err := tx.QueryRowContext(ctx, query, id).Scan(&occupied)
	return occupied, err
}

func changeUnitState(ctx context.Context, tx *sql.Tx, actor session.User, unit map[string]any, state string) (map[string]any, error) {
	id, err := accountwrite.NewGroup()
	if err != nil {
		return nil, err
	}
	unitID := stringField(unit, "id")
	note := stringField(unit, "note")
	if _, err := tx.ExecContext(ctx, `UPDATE board_candidate.phone_units
		SET status=$2,note=$3,revision=revision+1 WHERE id=$1`, unitID, state, note); err != nil {
		return nil, err
	}
	if _, err := tx.ExecContext(ctx, `UPDATE board_candidate.phone_devices
		SET state=$2,state_source='manual' WHERE id=$1`, stringField(unit, "device_id"), state); err != nil {
		return nil, err
	}
	previous, err := recordwire.Encode(stringField(unit, "status"))
	if err != nil {
		return nil, err
	}
	detailNote, err := recordwire.Encode(note)
	if err != nil {
		return nil, err
	}
	next, err := recordwire.Encode(state)
	if err != nil {
		return nil, err
	}
	detail := `{"before_status": ` + string(previous) + `, "note": ` + string(detailNote) + `, "status": ` + string(next) + `}`
	eventID := "event-" + id
	if _, err := tx.ExecContext(ctx, `INSERT INTO board_candidate.device_workflow_events
		(id,unit_id,actor_id,actor_name,at,action,detail) VALUES($1,$2,$3,$4,$5,'更新实物状态',$6)`,
		eventID, unitID, actor.ID, actor.Name, time.Now().Unix(), detail); err != nil {
		return nil, err
	}
	event, err := readRecord(ctx, tx, eventRelation, eventID)
	if err != nil {
		return nil, err
	}
	event["office_ids"] = recordOffices(unit)
	return event, nil
}
