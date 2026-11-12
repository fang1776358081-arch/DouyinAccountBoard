package inventory

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"maps"
	"net/http"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"time"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

const WorkflowPath = "/api/device-workflow"

var (
	workflowUnitStates     = []string{"待确认", "在用", "待清理", "可用闲置", "故障", "送修", "返修待验", "回收"}
	workflowUsable         = []string{"在用", "可用闲置"}
	workflowReplaceable    = []string{"故障", "送修", "返修待验", "回收"}
	workflowCaseStates     = []string{"待处理", "处理中", "暂挂", "已结案"}
	workflowRecoveryStates = []string{"待处理", "等待协助", "恢复成功", "暂不恢复", "无法恢复"}
	placementRelation      = relation{table: "phone_card_placements", id: "card_id"}
	nurtureRelation        = relation{table: "nurture_records", id: "account_id"}
)

// WorkflowHandler serves physical inventory actions using the same-origin,
// permission-checked and idempotent write protocol.
func WorkflowHandler(write accountwrite.Service) http.HandlerFunc {
	return write.Handle(WorkflowPath, "设备流程操作必须带请求标识", authorizeWorkflow, applyWorkflow)
}

type workflowRefs struct {
	devices, units, accounts, cards, cases map[string]bool
}

func newWorkflowRefs() workflowRefs {
	return workflowRefs{map[string]bool{}, map[string]bool{}, map[string]bool{}, map[string]bool{}, map[string]bool{}}
}

func addWorkflowRef(set map[string]bool, id string) {
	if id != "" {
		set[id] = true
	}
}

func workflowCardID(value any) (int64, error) {
	id, valid := parseCardID(value)
	if _, stringID := value.(string); stringID || !valid || id < 1 {
		return 0, accountwrite.Invalid("手机卡编号不正确")
	}
	return id, nil
}

func workflowStringList(raw any, limit int, message string) ([]string, error) {
	list, ok := raw.([]any)
	if !ok || len(list) > limit {
		return nil, accountwrite.Invalid(message)
	}
	items := make([]string, 0, len(list))
	for _, value := range list {
		id, ok := value.(string)
		if !ok || id == "" || strings.IndexByte(id, 0) >= 0 {
			return nil, accountwrite.Invalid(message)
		}
		items = append(items, id)
	}
	return items, nil
}

func workflowSelectIDs(ctx context.Context, tx *sql.Tx, query string, arg any, refs map[string]bool) error {
	rows, err := tx.QueryContext(ctx, query, arg)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return err
		}
		addWorkflowRef(refs, id)
	}
	return rows.Err()
}

func workflowActiveIDs(ctx context.Context, tx *sql.Tx, id string, refs map[string]bool) error {
	return workflowSelectIDs(ctx, tx, `SELECT id FROM board_candidate.phone_units WHERE device_id=$1 AND active=1`, id, refs)
}

func workflowLoadRefs(ctx context.Context, tx *sql.Tx, body map[string]any) (workflowRefs, error) {
	refs := newWorkflowRefs()
	if id := valueString(body["device_id"]); id != "" && body["action"] != "reassign_unit" {
		refs.devices[id] = true
		if err := workflowActiveIDs(ctx, tx, id, refs.units); err != nil {
			return refs, err
		}
	}
	for _, field := range []string{"unit_id", "target_unit_id"} {
		addWorkflowRef(refs.units, valueString(body[field]))
	}
	addWorkflowRef(refs.cases, valueString(body["case_id"]))
	addWorkflowRef(refs.accounts, valueString(body["account_id"]))
	if raw := body["card_id"]; raw != nil {
		id, err := workflowCardID(raw)
		if err != nil {
			return refs, err
		}
		addWorkflowRef(refs.cards, strconv.FormatInt(id, 10))
	}
	if raw, present := body["affected_account_ids"]; present {
		items, err := workflowStringList(raw, 1000, "受影响账号格式不正确")
		if err != nil {
			return refs, err
		}
		for _, id := range items {
			addWorkflowRef(refs.accounts, id)
		}
	}
	for id := range refs.accounts {
		access, err := readRecord(ctx, tx, accessRelation, id)
		if err != nil {
			return refs, err
		}
		if access != nil {
			addWorkflowRef(refs.units, stringField(access, "last_unit_id"))
			addWorkflowRef(refs.units, stringField(access, "target_unit_id"))
			addWorkflowRef(refs.cases, stringField(access, "case_id"))
		} else {
			account, err := readRecord(ctx, tx, accountRelation, id)
			if err != nil {
				return refs, err
			}
			if device := stringField(account, "phone_device_id"); device != "" {
				addWorkflowRef(refs.devices, device)
				if err := workflowActiveIDs(ctx, tx, device, refs.units); err != nil {
					return refs, err
				}
			}
		}
	}
	for id := range refs.cards {
		cardID, _ := strconv.ParseInt(id, 10, 64)
		placement, err := readRecord(ctx, tx, placementRelation, cardID)
		if err != nil {
			return refs, err
		}
		addWorkflowRef(refs.units, stringField(placement, "unit_id"))
	}
	for id := range refs.cases {
		item, err := readRecord(ctx, tx, caseRelation, id)
		if err != nil {
			return refs, err
		}
		if item == nil {
			return refs, accountwrite.Invalid("故障处理记录不存在")
		}
		addWorkflowRef(refs.units, stringField(item, "unit_id"))
		var accounts []string
		if err := json.Unmarshal([]byte(stringField(item, "account_ids")), &accounts); err != nil {
			return refs, err
		}
		for _, id := range accounts {
			addWorkflowRef(refs.accounts, id)
		}
	}
	for id := range refs.units {
		unit, err := readRecord(ctx, tx, unitRelation, id)
		if err != nil {
			return refs, err
		}
		if unit == nil {
			return refs, accountwrite.Invalid("实物手机不存在")
		}
		addWorkflowRef(refs.devices, stringField(unit, "device_id"))
		addWorkflowRef(refs.devices, stringField(unit, "original_device_id"))
		if err := workflowSelectIDs(ctx, tx, `SELECT card_id::text FROM board_candidate.phone_card_placements WHERE unit_id=$1`, id, refs.cards); err != nil {
			return refs, err
		}
		if err := workflowSelectIDs(ctx, tx, `SELECT account_id FROM board_candidate.phone_account_access WHERE last_unit_id=$1 OR target_unit_id=$1`, id, refs.accounts); err != nil {
			return refs, err
		}
	}
	return refs, nil
}

func authorizeWorkflow(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) error {
	if err := requirePermission(actor, "device_manage"); err != nil {
		return err
	}
	enabled, err := workflowAvailable(ctx, tx)
	if err != nil {
		return err
	}
	if !enabled {
		return accountwrite.Invalid("设备流程尚未完成受控迁移，请由维护人员完成迁移后使用")
	}
	if body["action"] == "place_card" {
		if err := requirePermission(actor, "card_manage"); err != nil {
			return err
		}
	}
	if body["action"] == "reassign_unit" {
		if err := requirePermission(actor, "device_create"); err != nil {
			return err
		}
	}
	if body["pause_nurture"] == true {
		if err := requirePermission(actor, "nurture_manage"); err != nil {
			return err
		}
	}
	refs, err := workflowLoadRefs(ctx, tx, body)
	if err != nil {
		return err
	}
	for _, group := range []struct {
		kind string
		refs map[string]bool
	}{{"device", refs.devices}, {"account", refs.accounts}, {"card", refs.cards}} {
		for id := range group.refs {
			row, err := workflowReadVersion(ctx, tx, group.kind+":"+id)
			if err != nil {
				return err
			}
			if err := requireEdit(actor, row); err != nil {
				return err
			}
		}
	}
	return nil
}

func (refs workflowRefs) keys(pauseNurture bool) map[string]bool {
	result := map[string]bool{}
	for _, group := range []struct {
		prefixes []string
		refs     map[string]bool
	}{{[]string{"device:"}, refs.devices}, {[]string{"unit:"}, refs.units}, {[]string{"case:"}, refs.cases},
		{[]string{"account:", "access:"}, refs.accounts}, {[]string{"card:", "placement:"}, refs.cards}} {
		for id := range group.refs {
			for _, prefix := range group.prefixes {
				result[prefix+id] = true
			}
		}
	}
	if pauseNurture {
		for id := range refs.accounts {
			result["nurture:"+id] = true
		}
	}
	return result
}

func workflowReadVersion(ctx context.Context, tx *sql.Tx, key string) (map[string]any, error) {
	kind, id, _ := strings.Cut(key, ":")
	switch kind {
	case "account":
		return readRecord(ctx, tx, accountRelation, id)
	case "device":
		return DeviceRecord(ctx, tx, id)
	case "unit":
		return readUnit(ctx, tx, id)
	case "access":
		return readAccess(ctx, tx, id)
	case "case":
		return workflowReadCase(ctx, tx, id)
	case "card", "placement":
		number, err := strconv.ParseInt(id, 10, 64)
		if err != nil {
			return nil, err
		}
		card, err := CardRecord(ctx, tx, number)
		if err != nil || kind == "card" {
			return card, err
		}
		placement, err := readRecord(ctx, tx, placementRelation, number)
		if err != nil || placement == nil {
			return placement, err
		}
		placement["office_ids"] = recordOffices(card)
		if unitID := stringField(placement, "unit_id"); unitID != "" {
			unit, err := readUnit(ctx, tx, unitID)
			if err != nil {
				return nil, err
			}
			placement["office_ids"] = mergeOfficeReferences(recordOffices(card), recordOffices(unit))
		}
		return placement, nil
	case "nurture":
		row, err := readRecord(ctx, tx, nurtureRelation, id)
		if err != nil || row == nil {
			return row, err
		}
		account, err := readRecord(ctx, tx, accountRelation, id)
		if err != nil {
			return nil, err
		}
		row["office_ids"] = recordOffices(account)
		return row, nil
	default:
		return nil, errors.New("unknown workflow version entity")
	}
}

func workflowReadCase(ctx context.Context, tx *sql.Tx, id string) (map[string]any, error) {
	item, err := readRecord(ctx, tx, caseRelation, id)
	if err != nil || item == nil {
		return item, err
	}
	unit, err := readUnit(ctx, tx, stringField(item, "unit_id"))
	if err != nil {
		return nil, err
	}
	scopes := [][]string{recordOffices(unit)}
	var accounts []string
	if err := json.Unmarshal([]byte(stringField(item, "account_ids")), &accounts); err != nil {
		return nil, err
	}
	for _, id := range accounts {
		account, err := readRecord(ctx, tx, accountRelation, id)
		if err != nil {
			return nil, err
		}
		scopes = append(scopes, recordOffices(account))
	}
	item["office_ids"] = mergeOfficeReferences(scopes...)
	return item, nil
}

func workflowSnapshot(ctx context.Context, tx *sql.Tx, keys map[string]bool) (snapshot, error) {
	records := make(snapshot, len(keys))
	for key := range keys {
		row, err := workflowReadVersion(ctx, tx, key)
		if err != nil {
			return nil, err
		}
		records[key] = row
	}
	return records, nil
}

type workflowWrite struct {
	ctx    context.Context
	tx     *sql.Tx
	actor  session.User
	body   map[string]any
	now    int64
	keys   map[string]bool
	events snapshot
}

func applyWorkflow(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	refs, err := workflowLoadRefs(ctx, tx, body)
	if err != nil {
		return nil, err
	}
	keys := refs.keys(body["pause_nurture"] == true)
	before, err := workflowSnapshot(ctx, tx, keys)
	if err != nil {
		return nil, err
	}
	if err := before.check(body); err != nil {
		return nil, err
	}
	work := workflowWrite{ctx: ctx, tx: tx, actor: actor, body: body, now: time.Now().Unix(), keys: keys, events: snapshot{}}
	var result map[string]any
	switch body["action"] {
	case "unit_status":
		result, err = work.unitStatus()
	case "place_card":
		result, err = work.placeCard()
	case "replace_unit":
		result, err = work.replaceUnit()
	case "open_case":
		result, err = work.openCase()
	case "update_case":
		result, err = work.updateCase()
	case "account_recovery":
		result, err = work.recoverAccount()
	case "reassign_unit":
		result, err = work.reassignUnit()
	default:
		return nil, accountwrite.Invalid("设备流程操作不存在")
	}
	if err != nil {
		return nil, err
	}
	if err := work.syncChangedAccounts(before, refs.accounts); err != nil {
		return nil, err
	}
	after, err := workflowSnapshot(ctx, tx, work.keys)
	if err != nil {
		return nil, err
	}
	maps.Copy(after, work.events)
	versions, err := work.finish(before, after)
	if err != nil {
		return nil, err
	}
	result["_record_versions"] = versions
	return result, nil
}

func (w *workflowWrite) finish(before, after snapshot) (map[string]any, error) {
	group, err := accountwrite.NewGroup()
	if err != nil {
		return nil, err
	}
	for _, key := range slices.Sorted(maps.Keys(after)) {
		old, next := before[key], after[key]
		oldToken, err := recordwire.Version(old)
		if err != nil {
			return nil, err
		}
		nextToken, err := recordwire.Version(next)
		if err != nil {
			return nil, err
		}
		if oldToken == nextToken {
			continue
		}
		scope := recordOffices(next)
		if next == nil {
			scope = recordOffices(old)
		}
		kind, _, _ := strings.Cut(key, ":")
		if len(scope) > 1 && kind != "device" && kind != "card" {
			// Workflow references require access to each related record.
			scope = nil
		}
		if old != nil && next != nil && !slices.Equal(recordOffices(old), recordOffices(next)) {
			scope = nil
		}
		if kind == "account" {
			for _, row := range []map[string]any{old, next} {
				private, err := w.accountCrossesScope(row)
				if err != nil {
					return nil, err
				}
				if private {
					scope = nil
					break
				}
			}
		}
		if err := accountwrite.InsertHistory(w.ctx, w.tx, w.actor, WorkflowPath, group, key, old, next, scope); err != nil {
			return nil, err
		}
	}
	if err := accountwrite.Audit(w.ctx, w.tx, w.actor, WorkflowPath, ""); err != nil {
		return nil, err
	}
	if err := accountwrite.InsertHistory(w.ctx, w.tx, w.actor, WorkflowPath, group, "sync:reload", nil, nil, nil); err != nil {
		return nil, err
	}
	return after.versions()
}

func (w *workflowWrite) text(field, fallback string, limit int, required bool) (string, error) {
	value := fallback
	if raw, supplied := w.body[field]; supplied {
		var ok bool
		if value, ok = raw.(string); !ok {
			return "", accountwrite.Invalid("设备流程字段格式不正确")
		}
	}
	value = strings.TrimSpace(value)
	if len([]rune(value)) > limit || strings.IndexByte(value, 0) >= 0 || (required && value == "") {
		return "", accountwrite.Invalid("设备流程字段内容为空或过长")
	}
	return value, nil
}

func (w *workflowWrite) unit() (map[string]any, error) {
	if id := valueString(w.body["unit_id"]); id != "" {
		unit, err := readUnit(w.ctx, w.tx, id)
		if err != nil {
			return nil, err
		}
		if unit == nil {
			return nil, accountwrite.Invalid("实物手机不存在")
		}
		return unit, nil
	}
	id, err := w.text("device_id", "", 80, true)
	if err != nil {
		return nil, err
	}
	unit, err := ensureUnit(w.ctx, w.tx, id)
	if err == nil {
		w.keys["unit:"+stringField(unit, "id")] = true
	}
	return unit, err
}

func workflowActive(unit map[string]any) bool {
	return valueString(unit["active"]) == "1"
}

func (w *workflowWrite) occupied(id string) (bool, error) {
	var occupied bool
	err := w.tx.QueryRowContext(w.ctx, `SELECT EXISTS(
		SELECT 1 FROM board_candidate.phone_card_placements WHERE unit_id=$1
		UNION ALL SELECT 1 FROM board_candidate.phone_account_access WHERE last_unit_id=$1
	)`, id).Scan(&occupied)
	return occupied, err
}

func (w *workflowWrite) touch(ids ...string) error {
	_, err := w.tx.ExecContext(w.ctx, `UPDATE board_candidate.phone_units SET revision=revision+1 WHERE id=ANY($1) AND id<>''`, ids)
	return err
}

func (w *workflowWrite) deviceState(id, state string) error {
	if !slices.Contains(deviceStates, state) {
		state = "故障"
	}
	_, err := w.tx.ExecContext(w.ctx, `UPDATE board_candidate.phone_devices SET state=$2,state_source='manual' WHERE id=$1`, id, state)
	return err
}

func workflowDetail(value any) (string, error) {
	raw, err := recordwire.Encode(value)
	if err != nil {
		return "", err
	}
	return spacedWorkflowJSON(raw), nil
}

func spacedWorkflowJSON(raw []byte) string {
	var text strings.Builder
	quoted, escaped := false, false
	for _, character := range raw {
		text.WriteByte(character)
		if escaped {
			escaped = false
		} else if character == '\\' && quoted {
			escaped = true
		} else if character == '"' {
			quoted = !quoted
		} else if !quoted && (character == ':' || character == ',') {
			text.WriteByte(' ')
		}
	}
	return text.String()
}

func (w *workflowWrite) event(action string, detail map[string]any, unitID, caseID string, cardID any, accountID string, scopes ...[]string) error {
	id, err := accountwrite.NewGroup()
	if err != nil {
		return err
	}
	raw, err := workflowDetail(detail)
	if err != nil {
		return err
	}
	id = "event-" + id
	if _, err := w.tx.ExecContext(w.ctx, `INSERT INTO board_candidate.device_workflow_events
		(id,unit_id,case_id,card_id,account_id,actor_id,actor_name,at,action,detail)
		VALUES($1,NULLIF($2,''),NULLIF($3,''),$4,NULLIF($5,''),$6,$7,$8,$9,$10)`,
		id, unitID, caseID, cardID, accountID, w.actor.ID, w.actor.Name, w.now, action, raw); err != nil {
		return err
	}
	row, err := readRecord(w.ctx, w.tx, eventRelation, id)
	if err != nil {
		return err
	}
	row["office_ids"] = mergeOfficeReferences(scopes...)
	w.events["workflow_event:"+id] = row
	return nil
}

func (w *workflowWrite) unitStatus() (map[string]any, error) {
	unit, err := w.unit()
	if err != nil {
		return nil, err
	}
	status, err := w.text("status", "", 30, true)
	if err != nil {
		return nil, err
	}
	if !slices.Contains(workflowUnitStates, status) {
		return nil, accountwrite.Invalid("实物手机状态不正确")
	}
	if !workflowActive(unit) && slices.Contains(workflowUsable, status) {
		return nil, accountwrite.Invalid("旧机已被新机补号，请保留返修待验并安排其他空号，不能恢复占用原编号")
	}
	id := stringField(unit, "id")
	if status == "可用闲置" {
		occupied, err := w.occupied(id)
		if err != nil {
			return nil, err
		}
		if occupied {
			return nil, accountwrite.Invalid("这台实物仍有卡或账号记录，请先核对并移出后再标记空闲")
		}
	}
	note, err := w.text("note", stringField(unit, "note"), 2000, false)
	if err != nil {
		return nil, err
	}
	if _, err := w.tx.ExecContext(w.ctx, `UPDATE board_candidate.phone_units
		SET status=$2,note=$3,revision=revision+1 WHERE id=$1`, id, status, note); err != nil {
		return nil, err
	}
	if workflowActive(unit) {
		if err := w.deviceState(stringField(unit, "device_id"), status); err != nil {
			return nil, err
		}
	}
	err = w.event("更新实物状态", map[string]any{"before_status": unit["status"], "status": status, "note": note}, id, "", nil, "", recordOffices(unit))
	return map[string]any{"ok": true, "unit_id": id}, err
}

func (w *workflowWrite) replaceUnit() (map[string]any, error) {
	deviceID, err := w.text("device_id", "", 80, true)
	if err != nil {
		return nil, err
	}
	old, err := ensureUnit(w.ctx, w.tx, deviceID)
	if err != nil {
		return nil, err
	}
	if !slices.Contains(workflowReplaceable, stringField(old, "status")) {
		return nil, accountwrite.Invalid("现用手机须先明确登记故障、送修、返修待验或回收，才能补号")
	}
	status, err := w.text("new_status", "待确认", 30, false)
	if err != nil {
		return nil, err
	}
	if status != "待确认" && status != "可用闲置" {
		return nil, accountwrite.Invalid("新机初始状态请选择待确认或可用闲置")
	}
	model, err := w.text("model", "", 100, false)
	if err != nil {
		return nil, err
	}
	note, err := w.text("note", "", 2000, false)
	if err != nil {
		return nil, err
	}
	token, err := accountwrite.NewGroup()
	if err != nil {
		return nil, err
	}
	id, oldID := "unit-"+token, stringField(old, "id")
	var generation int64
	if err := w.tx.QueryRowContext(w.ctx, `SELECT COALESCE(MAX(generation),0)+1
		FROM board_candidate.phone_units WHERE device_id=$1`, deviceID).Scan(&generation); err != nil {
		return nil, err
	}
	if _, err := w.tx.ExecContext(w.ctx, `UPDATE board_candidate.phone_units SET active=0,revision=revision+1 WHERE id=$1`, oldID); err != nil {
		return nil, err
	}
	if _, err := w.tx.ExecContext(w.ctx, `INSERT INTO board_candidate.phone_units
		(id,device_id,original_device_id,generation,status,model,note,created_at)
		VALUES($1,$2,$2,$3,$4,$5,$6,$7)`, id, deviceID, generation, status, model, note, w.now); err != nil {
		return nil, err
	}
	if err := w.deviceState(deviceID, status); err != nil {
		return nil, err
	}
	w.keys["unit:"+oldID], w.keys["unit:"+id] = true, true
	next, err := readUnit(w.ctx, w.tx, id)
	if err != nil {
		return nil, err
	}
	scopes := [][]string{recordOffices(old), recordOffices(next)}
	if err := w.event("新机补号", map[string]any{"old_unit_id": oldID, "new_unit_id": id, "generation": generation,
		"cards_and_logins_inherited": false}, oldID, "", nil, "", scopes...); err != nil {
		return nil, err
	}
	err = w.event("新机登记", map[string]any{"replaces_unit_id": oldID, "generation": generation}, id, "", nil, "", scopes...)
	return map[string]any{"ok": true, "unit_id": id, "old_unit_id": oldID}, err
}

func (w *workflowWrite) placeCard() (map[string]any, error) {
	cardID, err := workflowCardID(w.body["card_id"])
	if err != nil {
		return nil, err
	}
	card, err := CardRecord(w.ctx, w.tx, cardID)
	if err != nil {
		return nil, err
	}
	if card == nil {
		return nil, accountwrite.Invalid("手机卡不存在")
	}
	placement, err := readRecord(w.ctx, w.tx, placementRelation, cardID)
	if err != nil {
		return nil, err
	}
	oldUnit := stringField(placement, "unit_id")
	if placement != nil {
		delete(placement, "office_ids")
	}
	target, slot, storage := valueString(w.body["unit_id"]), "", ""
	existing := w.body["confirm_existing"] == true
	scopes := [][]string{recordOffices(card)}
	if oldUnit != "" {
		old, err := readUnit(w.ctx, w.tx, oldUnit)
		if err != nil {
			return nil, err
		}
		scopes = append(scopes, recordOffices(old))
	}
	if target != "" {
		unit, err := readUnit(w.ctx, w.tx, target)
		if err != nil {
			return nil, err
		}
		if unit == nil {
			return nil, accountwrite.Invalid("目标实物不存在")
		}
		scopes = append(scopes, recordOffices(unit))
		slot = valueString(w.body["slot"])
		if slot != "1" && slot != "2" && !(existing && slot == "待确认") {
			return nil, accountwrite.Invalid("请选择卡槽1或卡槽2")
		}
		if !existing && (!workflowActive(unit) || !slices.Contains(workflowUsable, stringField(unit, "status"))) {
			return nil, accountwrite.Invalid("目标手机必须是已确认在用或可用闲置的现用实物")
		}
		if !existing && w.body["confirmed_usable"] != true {
			return nil, accountwrite.Invalid("请确认目标手机实际可用且手机卡已插入所选卡槽")
		}
		if slot != "待确认" {
			var occupied bool
			err := w.tx.QueryRowContext(w.ctx, `SELECT EXISTS(SELECT 1 FROM board_candidate.phone_card_placements
				WHERE unit_id=$1 AND slot=$2 AND card_id<>$3)`, target, slot, cardID).Scan(&occupied)
			if err != nil {
				return nil, err
			}
			if occupied {
				return nil, accountwrite.Invalid("目标卡槽已有其他手机卡，请先核对位置")
			}
		}
		if !existing {
			if _, err := w.tx.ExecContext(w.ctx, `UPDATE board_candidate.phone_units SET status='在用' WHERE id=$1`, target); err != nil {
				return nil, err
			}
			if err := w.deviceState(stringField(unit, "device_id"), "在用"); err != nil {
				return nil, err
			}
		}
	} else {
		storage, err = w.text("storage", "", 200, true)
		if err != nil {
			return nil, err
		}
	}
	if _, err := w.tx.ExecContext(w.ctx, `INSERT INTO board_candidate.phone_card_placements
		(card_id,unit_id,slot,storage,updated_at) VALUES($1,NULLIF($2,''),$3,$4,$5)
		ON CONFLICT(card_id) DO UPDATE SET unit_id=excluded.unit_id,slot=excluded.slot,
		storage=excluded.storage,updated_at=excluded.updated_at,revision=phone_card_placements.revision+1`,
		cardID, target, slot, storage, w.now); err != nil {
		return nil, err
	}
	if err := w.touch(oldUnit, target); err != nil {
		return nil, err
	}
	action := "移动手机卡"
	if existing {
		action = "核对手机卡位置"
	}
	var unitID any
	if target != "" {
		unitID = target
	}
	err = w.event(action, map[string]any{"before": placement, "after": map[string]any{"unit_id": unitID, "slot": slot,
		"storage": storage}}, target, "", cardID, "", scopes...)
	return map[string]any{"ok": true, "card_id": cardID}, err
}

func (w *workflowWrite) accountCrossesScope(row map[string]any) (bool, error) {
	computerRelation := relation{"computers", "id", "computer_offices", "computer_id"}
	for _, ref := range []struct {
		field  string
		entity relation
	}{{"computer_id", computerRelation}, {"parent_account_id", accountRelation}, {"phone_device_id", deviceRelation}} {
		id := stringField(row, ref.field)
		if id == "" || (ref.field == "computer_id" && id == "unassigned") {
			continue
		}
		target, err := readRecord(w.ctx, w.tx, ref.entity, id)
		if err != nil {
			return false, err
		}
		if !slices.Equal(recordOffices(row), recordOffices(target)) {
			return true, nil
		}
	}
	return false, nil
}

func (w *workflowWrite) followup(fallback string) (string, error) {
	value, err := w.text("followup_on", fallback, 10, false)
	if err != nil || value == "" {
		return value, err
	}
	date, err := time.Parse(time.DateOnly, value)
	if err != nil || date.Year() < 1 || date.Format(time.DateOnly) != value {
		return "", accountwrite.Invalid("跟进日期格式不正确")
	}
	return value, nil
}

func (w *workflowWrite) access(id string) (map[string]any, error) {
	row, err := readRecord(w.ctx, w.tx, accessRelation, id)
	if err != nil || row != nil {
		return row, err
	}
	account, err := readRecord(w.ctx, w.tx, accountRelation, id)
	if err != nil {
		return nil, err
	}
	if account == nil {
		return nil, accountwrite.Invalid("账号不存在")
	}
	unitID := ""
	if device := stringField(account, "phone_device_id"); device != "" {
		unit, err := ensureUnit(w.ctx, w.tx, device)
		if err != nil {
			return nil, err
		}
		unitID = stringField(unit, "id")
		w.keys["unit:"+unitID] = true
	}
	if _, err := w.tx.ExecContext(w.ctx, `INSERT INTO board_candidate.phone_account_access(account_id,last_unit_id)
		VALUES($1,NULLIF($2,''))`, id, unitID); err != nil {
		return nil, err
	}
	return readRecord(w.ctx, w.tx, accessRelation, id)
}

func (w *workflowWrite) openCase() (map[string]any, error) {
	unit, err := w.unit()
	if err != nil {
		return nil, err
	}
	unitID := stringField(unit, "id")
	reason, err := w.text("reason", "", 2000, true)
	if err != nil {
		return nil, err
	}
	var ongoing bool
	if err := w.tx.QueryRowContext(w.ctx, `SELECT EXISTS(SELECT 1 FROM board_candidate.device_cases
		WHERE unit_id=$1 AND status<>'已结案')`, unitID).Scan(&ongoing); err != nil {
		return nil, err
	}
	if ongoing {
		return nil, accountwrite.Invalid("该实物已有进行中的处理记录，请继续原记录")
	}
	affected := []string{}
	if raw, found := w.body["affected_account_ids"]; found {
		ids, err := workflowStringList(raw, 1000, "受影响账号格式不正确")
		if err != nil {
			return nil, err
		}
		seen := map[string]bool{}
		for _, id := range ids {
			if !seen[id] {
				affected = append(affected, id)
				seen[id] = true
			}
		}
	}
	scopes := [][]string{recordOffices(unit)}
	for _, id := range affected {
		access, err := w.access(id)
		if err != nil {
			return nil, err
		}
		if stringField(access, "last_unit_id") != unitID {
			return nil, accountwrite.Invalid("所选账号不关联这台实物手机，请先核对")
		}
		if caseID := stringField(access, "case_id"); caseID != "" {
			previous, err := readRecord(w.ctx, w.tx, caseRelation, caseID)
			if err != nil {
				return nil, err
			}
			if stringField(previous, "status") != "已结案" {
				return nil, accountwrite.Invalid("所选账号已有进行中的处理记录")
			}
		}
		account, err := readRecord(w.ctx, w.tx, accountRelation, id)
		if err != nil {
			return nil, err
		}
		scopes = append(scopes, recordOffices(account))
	}
	owner, err := w.text("owner", w.actor.Name, 100, false)
	if err != nil {
		return nil, err
	}
	followup, err := w.followup("")
	if err != nil {
		return nil, err
	}
	note, err := w.text("note", "", 2000, false)
	if err != nil {
		return nil, err
	}
	key, err := accountwrite.NewGroup()
	if err != nil {
		return nil, err
	}
	caseID := "case-" + key
	ids, err := workflowDetail(affected)
	if err != nil {
		return nil, err
	}
	if _, err := w.tx.ExecContext(w.ctx, `INSERT INTO board_candidate.device_cases
		(id,unit_id,reason,owner,followup_on,note,created_at,updated_at,account_ids)
		VALUES($1,$2,$3,$4,$5,$6,$7,$7,$8)`, caseID, unitID, reason, owner, followup, note, w.now, ids); err != nil {
		return nil, err
	}
	if _, err := w.tx.ExecContext(w.ctx, `UPDATE board_candidate.phone_units
		SET status='故障',revision=revision+1 WHERE id=$1`, unitID); err != nil {
		return nil, err
	}
	if workflowActive(unit) {
		if err := w.deviceState(stringField(unit, "device_id"), "故障"); err != nil {
			return nil, err
		}
	}
	w.keys["case:"+caseID] = true
	for _, accountID := range affected {
		if _, err := w.tx.ExecContext(w.ctx, `UPDATE board_candidate.phone_account_access
			SET login_state='登录受阻',recovery_state='待处理',target_unit_id=NULL,
			    reason=$2,owner=$3,followup_on=$4,case_id=$5,revision=revision+1 WHERE account_id=$1`,
			accountID, reason, owner, followup, caseID); err != nil {
			return nil, err
		}
		if w.body["pause_nurture"] == true {
			if err := w.pauseNurture(accountID); err != nil {
				return nil, err
			}
		}
		if err := w.event("登记账号登录受阻", map[string]any{"reason": reason}, unitID, caseID, nil, accountID, scopes...); err != nil {
			return nil, err
		}
	}
	err = w.event("登记设备故障", map[string]any{"reason": reason, "affected_account_ids": affected}, unitID, caseID, nil, "", scopes...)
	return map[string]any{"ok": true, "case_id": caseID, "unit_id": unitID}, err
}

func (w *workflowWrite) pauseNurture(id string) error {
	row, err := readRecord(w.ctx, w.tx, nurtureRelation, id)
	if err != nil || stringField(row, "workflow") != "运行" {
		return err
	}
	active := stringField(row, "active_since")
	day := w.boardDate()
	today := day.Format(time.DateOnly)
	banked, _ := strconv.ParseInt(valueString(row["banked_days"]), 10, 64)
	if from, err := workflowDate(active); err == nil && day.After(from) {
		banked += (day.Unix() - from.Unix()) / 86400
	}
	var periods []json.RawMessage
	if err := json.Unmarshal([]byte(stringField(row, "periods")), &periods); err != nil {
		periods = nil
	}
	entries := []string{}
	for _, item := range periods {
		if len(item) > 0 && item[0] == '{' {
			var compact bytes.Buffer
			if err := json.Compact(&compact, item); err != nil {
				return err
			}
			entries = append(entries, spacedWorkflowJSON(compact.Bytes()))
		}
	}
	added, err := workflowDetail(struct {
		Start string `json:"start"`
		End   string `json:"end"`
	}{active, today})
	if err != nil {
		return err
	}
	entries = append(entries, added)
	_, err = w.tx.ExecContext(w.ctx, `UPDATE board_candidate.nurture_records
		SET banked_days=$2,periods=$3,active_since='',paused_on=$4,workflow='暂停',revision=revision+1 WHERE account_id=$1`,
		id, banked, "["+strings.Join(entries, ", ")+"]", today)
	return err
}

func (w *workflowWrite) boardDate() time.Time {
	now := time.Unix(w.now, 0).In(time.FixedZone("UTC+8", 8*3600))
	return time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, time.UTC)
}

func workflowDate(raw string) (time.Time, error) {
	layout := time.DateOnly
	if len(raw) == 8 {
		layout = "20060102"
	}
	return time.Parse(layout, raw)
}

// The legacy operation finisher synchronizes nurture for changed account rows.
// Workflow does not re-evaluate unrelated accounts or add optimistic keys.
func (w *workflowWrite) syncChangedAccounts(before snapshot, ids map[string]bool) error {
	if w.body["action"] != "account_recovery" && w.body["action"] != "reassign_unit" {
		return nil
	}
	day := w.boardDate()
	for id := range ids {
		account, err := readRecord(w.ctx, w.tx, accountRelation, id)
		if err != nil {
			return err
		}
		if account == nil || reflect.DeepEqual(before["account:"+id], account) {
			continue
		}
		key := "nurture:" + id
		record, err := workflowReadVersion(w.ctx, w.tx, key)
		if err != nil {
			return err
		}
		status := stringField(account, "status")
		switch {
		case record == nil:
			started := stringField(account, "nurture_started")
			if started == "" {
				started = stringField(account, "registered_on")
			}
			when, dateErr := workflowDate(started)
			recent := dateErr == nil && when.Year() > 0 && !when.After(day) && when.After(day.AddDate(0, 0, -7))
			if status != "养号7天" && !recent {
				continue
			}
			_, err = w.tx.ExecContext(w.ctx, `INSERT INTO board_candidate.nurture_records(account_id,legacy_started_on) VALUES($1,$2)`, id, started)
		case stringField(record, "completed_on") != "":
			continue
		case stringField(record, "workflow") == "待开始" && stringField(record, "started_on") != "":
			_, err = w.tx.ExecContext(w.ctx, `UPDATE board_candidate.nurture_records
				SET legacy_started_on=started_on,started_on='',revision=revision+1 WHERE account_id=$1`, id)
		case stringField(record, "workflow") == "运行" && status != "正常" && status != "待确认" && status != "养号7天":
			if err = w.pauseNurture(id); err == nil {
				_, err = w.tx.ExecContext(w.ctx, `UPDATE board_candidate.nurture_records
					SET verification_state='待检查',verification_updated_on=$2 WHERE account_id=$1`, id, day.Format(time.DateOnly))
			}
		default:
			continue
		}
		if err != nil {
			return err
		}
		if _, checked := before[key]; !checked {
			before[key] = record
		}
		w.keys[key] = true
	}
	return nil
}

func (w *workflowWrite) updateCase() (map[string]any, error) {
	id, err := w.text("case_id", "", 2000, true)
	if err != nil {
		return nil, err
	}
	item, err := workflowReadCase(w.ctx, w.tx, id)
	if err != nil {
		return nil, err
	}
	if item == nil {
		return nil, accountwrite.Invalid("处理记录不存在")
	}
	oldStatus := stringField(item, "status")
	status, err := w.text("status", oldStatus, 30, false)
	if err != nil {
		return nil, err
	}
	if !slices.Contains(workflowCaseStates, status) {
		return nil, accountwrite.Invalid("处理进度不正确")
	}
	if oldStatus == "已结案" {
		return nil, accountwrite.Invalid("已结案记录不可改写，请新建后续处理记录")
	}
	followup, err := w.followup(stringField(item, "followup_on"))
	if err != nil {
		return nil, err
	}
	if status == "暂挂" && followup == "" {
		return nil, accountwrite.Invalid("暂挂必须填写下次跟进日期")
	}
	closure, err := w.text("closure_reason", stringField(item, "closure_reason"), 2000, false)
	if err != nil {
		return nil, err
	}
	closedAt := int64(0)
	if status == "已结案" {
		if closure == "" {
			return nil, accountwrite.Invalid("结案必须填写处理结果及未恢复账号的说明")
		}
		var affected []string
		if err := json.Unmarshal([]byte(stringField(item, "account_ids")), &affected); err != nil {
			return nil, err
		}
		resolved := map[string]bool{}
		if err := workflowSelectIDs(w.ctx, w.tx, `SELECT account_id FROM board_candidate.phone_account_access
			WHERE case_id=$1 AND recovery_state IN ('恢复成功','暂不恢复','无法恢复')`, id, resolved); err != nil {
			return nil, err
		}
		for _, id := range affected {
			if !resolved[id] {
				return nil, accountwrite.Invalid("仍有账号未明确处理结果，请逐个恢复或明确暂不恢复/无法恢复")
			}
		}
		closedAt = w.now
	}
	reason, err := w.text("reason", stringField(item, "reason"), 2000, true)
	if err != nil {
		return nil, err
	}
	owner, err := w.text("owner", stringField(item, "owner"), 100, false)
	if err != nil {
		return nil, err
	}
	note, err := w.text("note", stringField(item, "note"), 2000, false)
	if err != nil {
		return nil, err
	}
	if _, err := w.tx.ExecContext(w.ctx, `UPDATE board_candidate.device_cases
		SET status=$2,reason=$3,owner=$4,followup_on=$5,note=$6,closure_reason=$7,updated_at=$8,closed_at=$9,revision=revision+1
		WHERE id=$1`, id, status, reason, owner, followup, note, closure, w.now, closedAt); err != nil {
		return nil, err
	}
	unitID := stringField(item, "unit_id")
	if err := w.touch(unitID); err != nil {
		return nil, err
	}
	err = w.event("更新故障处理", map[string]any{"before_status": oldStatus, "status": status, "closure_reason": closure},
		unitID, id, nil, "", recordOffices(item))
	return map[string]any{"ok": true, "case_id": id}, err
}

func (w *workflowWrite) recoverAccount() (map[string]any, error) {
	accountID, err := w.text("account_id", "", 100, true)
	if err != nil {
		return nil, err
	}
	access, err := w.access(accountID)
	if err != nil {
		return nil, err
	}
	oldState := stringField(access, "recovery_state")
	state, err := w.text("recovery_state", oldState, 30, false)
	if err != nil {
		return nil, err
	}
	if !slices.Contains(workflowRecoveryStates, state) {
		return nil, accountwrite.Invalid("账号恢复进度不正确")
	}
	oldCase := stringField(access, "case_id")
	caseID := oldCase
	if raw, supplied := w.body["case_id"]; supplied {
		caseID = valueString(raw)
	}
	if oldCase != "" && oldCase != caseID {
		previous, err := readRecord(w.ctx, w.tx, caseRelation, oldCase)
		if err != nil {
			return nil, err
		}
		if stringField(previous, "status") != "已结案" {
			return nil, accountwrite.Invalid("当前故障处理尚未结案，不能移除或改换处理记录")
		}
	}
	var currentCase map[string]any
	oldUnit := stringField(access, "last_unit_id")
	if caseID != "" {
		currentCase, err = workflowReadCase(w.ctx, w.tx, caseID)
		if err != nil {
			return nil, err
		}
		if currentCase == nil {
			return nil, accountwrite.Invalid("故障处理记录不存在")
		}
		if currentCase["status"] == "已结案" {
			return nil, accountwrite.Invalid("该故障记录已经结案，请建立新的处理记录")
		}
		if oldCase != caseID && currentCase["unit_id"] != oldUnit {
			return nil, accountwrite.Invalid("账号与该故障实物不关联")
		}
	}
	oldTarget := stringField(access, "target_unit_id")
	targetID := oldTarget
	if raw, supplied := w.body["target_unit_id"]; supplied {
		targetID = valueString(raw)
	}
	var target map[string]any
	if targetID != "" {
		target, err = readUnit(w.ctx, w.tx, targetID)
		if err != nil {
			return nil, err
		}
		if target == nil {
			return nil, accountwrite.Invalid("目标实物不存在")
		}
	}
	reason, err := w.text("reason", stringField(access, "reason"), 2000, false)
	if err != nil {
		return nil, err
	}
	if (state == "等待协助" || state == "暂不恢复" || state == "无法恢复") && reason == "" {
		return nil, accountwrite.Invalid("请填写账号受阻原因或处理结果")
	}
	followup, err := w.followup(stringField(access, "followup_on"))
	if err != nil {
		return nil, err
	}
	owner, err := w.text("owner", stringField(access, "owner"), 100, false)
	if err != nil {
		return nil, err
	}
	lastID, login := oldUnit, "登录受阻"
	confirmed, _ := strconv.ParseInt(valueString(access["confirmed_at"]), 10, 64)
	if state == "恢复成功" {
		if w.body["confirmed_login"] != true {
			return nil, accountwrite.Invalid("只有实际登录成功后才能确认恢复")
		}
		if target == nil || !workflowActive(target) || !slices.Contains(workflowUsable, stringField(target, "status")) {
			return nil, accountwrite.Invalid("恢复成功必须选择已确认可用的现用实物手机")
		}
		lastID, login, confirmed = targetID, "可操作", w.now
		device := stringField(target, "device_id")
		if _, err := w.tx.ExecContext(w.ctx, `UPDATE board_candidate.accounts
			SET phone_device_id=$2,phone_device_source='manual' WHERE id=$1`, accountID, device); err != nil {
			return nil, err
		}
		if _, err := w.tx.ExecContext(w.ctx, `UPDATE board_candidate.phone_units
			SET status='在用',revision=revision+1 WHERE id=$1`, targetID); err != nil {
			return nil, err
		}
		if err := w.deviceState(device, "在用"); err != nil {
			return nil, err
		}
	}
	if _, err := w.tx.ExecContext(w.ctx, `UPDATE board_candidate.phone_account_access
		SET last_unit_id=NULLIF($2,''),target_unit_id=NULLIF($3,''),login_state=$4,recovery_state=$5,reason=$6,owner=$7,
		    followup_on=$8,case_id=NULLIF($9,''),confirmed_at=$10,revision=revision+1 WHERE account_id=$1`,
		accountID, lastID, targetID, login, state, reason, owner, followup, caseID, confirmed); err != nil {
		return nil, err
	}
	if err := w.touch(oldUnit, oldTarget, lastID, targetID); err != nil {
		return nil, err
	}
	account, err := readRecord(w.ctx, w.tx, accountRelation, accountID)
	if err != nil {
		return nil, err
	}
	scopes := [][]string{recordOffices(account)}
	for _, id := range []string{oldUnit, lastID, targetID} {
		if id == "" {
			continue
		}
		unit, err := readUnit(w.ctx, w.tx, id)
		if err != nil {
			return nil, err
		}
		scopes = append(scopes, recordOffices(unit))
	}
	if currentCase != nil {
		scopes = append(scopes, recordOffices(currentCase))
	}
	var targetRef any
	if targetID != "" {
		targetRef = targetID
	}
	err = w.event("更新账号恢复", map[string]any{"before_state": oldState, "recovery_state": state,
		"from_unit_id": access["last_unit_id"], "target_unit_id": targetRef, "reason": reason,
		"confirmed_login": state == "恢复成功"}, lastID, caseID, nil, accountID, scopes...)
	return map[string]any{"ok": true, "account_id": accountID}, err
}

func (w *workflowWrite) reassignUnit() (map[string]any, error) {
	id, err := w.text("unit_id", "", 2000, true)
	if err != nil {
		return nil, err
	}
	unit, err := readUnit(w.ctx, w.tx, id)
	if err != nil {
		return nil, err
	}
	if unit == nil {
		return nil, accountwrite.Invalid("旧实物不存在")
	}
	if workflowActive(unit) {
		return nil, accountwrite.Invalid("只可为被新机补号后的旧实物重新编号")
	}
	if !slices.Contains([]string{"返修待验", "故障", "待确认"}, stringField(unit, "status")) || w.body["confirmed_usable"] != true {
		return nil, accountwrite.Invalid("请先核对旧机返修情况，并确认实际复检可用后重新编号")
	}
	previousID := stringField(unit, "device_id")
	previous, err := DeviceRecord(w.ctx, w.tx, previousID)
	if err != nil {
		return nil, err
	}
	offices := recordOffices(previous)
	if len(offices) != 1 {
		return nil, accountwrite.Invalid("原实物的办公室归属待核对，请先明确唯一办公室再重新编号")
	}
	if _, exists := w.body["number"]; !exists {
		return nil, accountwrite.Invalid("返修重新编号请填写手机序号")
	}
	serial := serialNumber(valueString(w.body["number"]))
	if serial == "" {
		return nil, accountwrite.Invalid("手机序号请填1至6位正整数")
	}
	name, err := w.text("name", "", 100, false)
	if err != nil {
		return nil, err
	}
	key, err := accountwrite.NewGroup()
	if err != nil {
		return nil, err
	}
	newID := "phone:" + key + ":" + serial
	offices, err = requestedOffices(w.ctx, w.tx, w.actor, map[string]any{"office_ids": offices})
	if err != nil {
		return nil, err
	}
	if err := deviceScope(w.ctx, w.tx, newID, serial, nil, offices); err != nil {
		return nil, err
	}
	occupied, err := w.occupied(id)
	if err != nil {
		return nil, err
	}
	state := "可用闲置"
	if occupied {
		state = "在用"
	}
	if name == "" {
		var officeName string
		if err := w.tx.QueryRowContext(w.ctx, `SELECT name FROM board_candidate.offices WHERE id=$1`, offices[0]).Scan(&officeName); err != nil {
			return nil, err
		}
		name = officeName + " · " + serial + "号手机"
	}
	if _, err := w.tx.ExecContext(w.ctx, `INSERT INTO board_candidate.phone_devices
		(id,name,state,state_source) VALUES($1,$2,$3,'manual')`, newID, name, state); err != nil {
		return nil, err
	}
	if err := saveOffices(w.ctx, w.tx, deviceRelation, newID, offices); err != nil {
		return nil, err
	}
	if _, err := w.tx.ExecContext(w.ctx, `UPDATE board_candidate.phone_units
		SET device_id=$2,generation=1,active=1,status=$3,revision=revision+1 WHERE id=$1`, id, newID, state); err != nil {
		return nil, err
	}
	if _, err := w.tx.ExecContext(w.ctx, `UPDATE board_candidate.accounts SET phone_device_id=$2,phone_device_source='manual'
		WHERE id IN (SELECT account_id FROM board_candidate.phone_account_access WHERE last_unit_id=$1)`, id, newID); err != nil {
		return nil, err
	}
	w.keys["device:"+newID] = true
	err = w.event("返修旧机重新编号", map[string]any{"old_device_id": previousID, "device_id": newID,
		"original_device_id": unit["original_device_id"], "confirmed_usable": true}, id, "", nil, "", recordOffices(unit), offices)
	return map[string]any{"ok": true, "unit_id": id, "device_id": newID}, err
}
