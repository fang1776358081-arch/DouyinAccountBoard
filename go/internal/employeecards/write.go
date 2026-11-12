package employeecards

import (
	"context"
	"database/sql"
	"errors"
	"maps"
	"net/http"
	"slices"
	"strconv"
	"time"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

type employeePlan struct {
	Action       string            `json:"action"`
	Employee     string            `json:"employee_id"`
	Data         dataRow           `json:"data"`
	NewID        string            `json:"new_id"`
	UnitIDs      []string          `json:"unit_ids"`
	Expected     string            `json:"expected_version"`
	Day          string            `json:"day"`
	Generation   string            `json:"restore_generation"`
	Cards        []dataRow         `json:"card_snapshots"`
	BatchIDs     map[string]string `json:"batch_unit_ids,omitempty"`
	BatchBlocked *bool             `json:"batch_blocked,omitempty"`
	BatchDigest  string            `json:"batch_link_digest,omitempty"`
}

type employeeMutation struct {
	ctx   context.Context
	tx    *sql.Tx
	actor session.User
	plan  *employeePlan
	today string
}

func (s Service) Preview() http.HandlerFunc {
	return s.Write.Direct(100000, false, authorizeManager, s.preview)
}

func (s Service) Apply() http.HandlerFunc {
	return s.Write.Direct(100000, false, authorizeManager, s.apply)
}

func enableWrite(ctx context.Context, tx *sql.Tx) error {
	enabled, err := available(ctx, tx)
	if err != nil {
		return err
	}
	if !enabled {
		return accountwrite.APIError{Status: http.StatusServiceUnavailable, Message: "员工办卡结构尚未升级，未保存任何资料"}
	}
	return nil
}

func employeeVersion(ctx context.Context, tx *sql.Tx, actor session.User, id, generation string) (string, error) {
	bundle, err := loadBundle(ctx, tx, actor)
	if err != nil {
		return "", err
	}
	return bundle.version(actor, id, generation)
}

func requireGeneration(current, old string) error {
	if current != old {
		return accountwrite.APIError{Status: http.StatusConflict, Message: "该预览或请求属于已关闭的恢复代次，请重新读取核对，不能再次执行旧请求",
			Code: "RESTORE_GENERATION_EXPIRED"}
	}
	return nil
}

func cardSnapshots(ctx context.Context, tx *sql.Tx, actor session.User, action string, data dataRow) ([]dataRow, error) {
	result := []dataRow{}
	if action == "card_link_batch" {
		for _, item := range resultRows(data["cards"]) {
			card, err := lookupCard(ctx, tx, actor, item["card_id"], true)
			if err != nil {
				return nil, err
			}
			result = append(result, card)
		}
	} else if id, supplied := data["card_id"]; supplied {
		card, err := lookupCard(ctx, tx, actor, id, false)
		if err != nil {
			return nil, err
		}
		result = append(result, card)
	}
	return result, nil
}

func (s Service) preview(ctx context.Context, tx *sql.Tx, actor session.User, body dataRow) (dataRow, error) {
	if err := enableWrite(ctx, tx); err != nil {
		return nil, err
	}
	if err := validateFields(body, "action", "employee_id", "data", "expected_version", "request_id"); err != nil {
		return nil, err
	}
	id, err := employeeText(valueOr(body, "employee_id", ""), "员工标识", 80, false)
	if err != nil {
		return nil, err
	}
	action := field(body, "action")
	if err := authorizeAction(ctx, tx, actor, action, id); err != nil {
		return nil, err
	}
	if id == "" && !slices.Contains([]string{"employee", "operator", "rules"}, action) {
		return nil, accountwrite.Invalid("请先选择员工")
	}
	generation := s.Write.RestoreGeneration
	current, err := employeeVersion(ctx, tx, actor, id, generation)
	if err != nil {
		return nil, err
	}
	if body["expected_version"] != current {
		return nil, accountwrite.Conflict("员工资料或规则已变化，请刷新核对后重新预览")
	}
	data := resultRow(body["data"])
	if data == nil {
		return nil, accountwrite.Invalid("请提交本次操作资料")
	}
	today := employeeToday()
	if action == "card_link_batch" {
		data, err = normalizeBatch(data, today)
		if err != nil {
			return nil, err
		}
	}
	newID, err := employeeKey()
	if err != nil {
		return nil, err
	}
	plan := employeePlan{Action: action, Employee: id, Data: data, NewID: newID, Day: today, Generation: generation,
		Expected: current, UnitIDs: []string{}}
	if action == "application" && field(data, "id") == "" {
		quantity, err := employeeNumber(valueOr(data, "quantity", 1), "申请数量", 1, 100, false)
		if err != nil {
			return nil, err
		}
		for range numberValue(quantity) {
			ident, err := employeeKey()
			if err != nil {
				return nil, err
			}
			plan.UnitIDs = append(plan.UnitIDs, ident)
		}
	}
	plan.Cards, err = cardSnapshots(ctx, tx, actor, action, data)
	if err != nil {
		return nil, err
	}
	if action == "card_link_batch" {
		plan.BatchIDs = map[string]string{}
		for _, card := range resultRows(data["cards"]) {
			ident, err := employeeKey()
			if err != nil {
				return nil, err
			}
			plan.BatchIDs[strconv.FormatInt(numberValue(card["card_id"]), 10)] = ident
		}
	}
	if _, err := tx.ExecContext(ctx, "SAVEPOINT employee_card_preview_check"); err != nil {
		return nil, err
	}
	work := employeeMutation{ctx, tx, actor, &plan, today}
	result, validation := work.execute(true)
	if _, err := tx.ExecContext(ctx, "ROLLBACK TO SAVEPOINT employee_card_preview_check"); err != nil {
		return nil, err
	}
	if _, err := tx.ExecContext(ctx, "RELEASE SAVEPOINT employee_card_preview_check"); err != nil {
		return nil, err
	}
	if validation != nil {
		return nil, validation
	}
	raw, err := encodeRow(plan)
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
	if _, err := tx.ExecContext(ctx, `INSERT INTO board_candidate.operations_previews(id,actor_id,expires_at,kind,payload)
		VALUES($1,$2,$3,$4,$5)`, previewID, actor.ID, now+1800, employeePreviewKind, raw); err != nil {
		return nil, err
	}
	response := dataRow{"preview_id": previewID, "action": action, "employee_id": id, "summary": result["summary"],
		"version": current, "expires_at": now + 1800}
	if action == "card_link_batch" {
		for _, key := range []string{"rows", "counts", "has_conflicts", "can_apply"} {
			response[key] = result[key]
		}
	}
	return response, nil
}

func employeeRequest(ctx context.Context, tx *sql.Tx, actor session.User, id string) (dataRow, error) {
	if !employeeUUID.MatchString(id) {
		return nil, accountwrite.Invalid("请求标识不正确")
	}
	return getOne(ctx, tx, `SELECT to_json(r)::text FROM board_candidate.employee_card_requests r WHERE actor_id=$1 AND request_id=$2`, actor.ID, id)
}

func (s Service) checkedResult(ctx context.Context, tx *sql.Tx, actor session.User, record dataRow) (dataRow, error) {
	var actual string
	if err := tx.QueryRowContext(ctx, `SELECT restore_generation FROM board_candidate.runtime_control WHERE id=1`).Scan(&actual); err != nil {
		return nil, err
	}
	if err := requireGeneration(actual, s.Write.RestoreGeneration); err != nil {
		return nil, err
	}
	if err := requireGeneration(actual, field(record, "restore_generation")); err != nil {
		return nil, err
	}
	if err := authorizeAction(ctx, tx, actor, field(record, "action"), field(record, "employee_id")); err != nil {
		return nil, err
	}
	var result dataRow
	err := decodeJSON(field(record, "result"), &result)
	return result, err
}

func (s Service) requestResult(ctx context.Context, tx *sql.Tx, actor session.User, id string) (dataRow, error) {
	if err := authorizeManager(ctx, tx, actor, nil); err != nil {
		return nil, err
	}
	record, err := employeeRequest(ctx, tx, actor, id)
	if err != nil {
		return nil, err
	}
	if record == nil {
		return nil, accountwrite.APIError{Status: http.StatusNotFound, Message: "请求未完成或不存在，请使用原请求核对"}
	}
	result, err := s.checkedResult(ctx, tx, actor, record)
	if err != nil {
		return nil, err
	}
	return dataRow{"request_found": true, "result": result}, nil
}

func (s Service) apply(ctx context.Context, tx *sql.Tx, actor session.User, body dataRow) (dataRow, error) {
	if err := enableWrite(ctx, tx); err != nil {
		return nil, err
	}
	if err := validateFields(body, "preview_id", "request_id", "confirmation"); err != nil {
		return nil, err
	}
	requestID := field(body, "request_id")
	stored, err := employeeRequest(ctx, tx, actor, requestID)
	if err != nil {
		return nil, err
	}
	hashInput := maps.Clone(body)
	delete(hashInput, "request_id")
	fingerprint, err := recordwire.Version(hashInput)
	if err != nil {
		return nil, err
	}
	if stored != nil {
		if err := requireGeneration(s.Write.RestoreGeneration, field(stored, "restore_generation")); err != nil {
			return nil, err
		}
		if stored["fingerprint"] != fingerprint {
			return nil, accountwrite.Invalid("同一请求标识不能用于不同员工办卡内容")
		}
		return s.checkedResult(ctx, tx, actor, stored)
	}
	if body["confirmation"] != "确认保存" {
		return nil, accountwrite.Invalid("请输入“确认保存”后再提交")
	}
	id, err := employeeText(valueOr(body, "preview_id", ""), "预览标识", 80, true)
	if err != nil {
		return nil, err
	}
	var raw string
	err = tx.QueryRowContext(ctx, `SELECT payload FROM board_candidate.operations_previews
		WHERE id=$1 AND actor_id=$2 AND kind=$3 AND expires_at>$4`, id, actor.ID, employeePreviewKind, time.Now().Unix()).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, accountwrite.Invalid("预览不存在或已过期，请重新预览")
	}
	if err != nil {
		return nil, err
	}
	var plan employeePlan
	if err := decodeJSON(raw, &plan); err != nil {
		return nil, err
	}
	if err := requireGeneration(s.Write.RestoreGeneration, plan.Generation); err != nil {
		return nil, err
	}
	if err := authorizeAction(ctx, tx, actor, plan.Action, plan.Employee); err != nil {
		return nil, err
	}
	today := employeeToday()
	current, err := employeeVersion(ctx, tx, actor, plan.Employee, s.Write.RestoreGeneration)
	if err != nil {
		return nil, err
	}
	if plan.Day != today || current != plan.Expected {
		return nil, accountwrite.Conflict("员工资料、权限、日期或规则已变化，请重新预览；未保存")
	}
	cards, err := cardSnapshots(ctx, tx, actor, plan.Action, plan.Data)
	if err != nil {
		return nil, err
	}
	left, err := recordwire.Version(cards)
	if err != nil {
		return nil, err
	}
	right, err := recordwire.Version(plan.Cards)
	if err != nil {
		return nil, err
	}
	if left != right {
		return nil, accountwrite.Conflict("手机卡资料或范围已变化，请重新预览；未保存")
	}
	work := employeeMutation{ctx, tx, actor, &plan, today}
	saved, err := work.execute(false)
	if err != nil {
		return nil, err
	}
	employee := field(saved, "employee_id")
	version, err := employeeVersion(ctx, tx, actor, employee, s.Write.RestoreGeneration)
	if err != nil {
		return nil, err
	}
	result := dataRow{"ok": true, "id": saved["id"], "employee_id": employee, "action": plan.Action, "version": version}
	if plan.Action == "card_link_batch" {
		result["counts"] = saved["counts"]
	}
	encoded, err := encodeRow(result)
	if err != nil {
		return nil, err
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO board_candidate.employee_card_requests
		(actor_id,request_id,restore_generation,fingerprint,employee_id,action,result,created_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8)`,
		actor.ID, requestID, s.Write.RestoreGeneration, fingerprint, employee, plan.Action, encoded, time.Now().Unix()); err != nil {
		return nil, err
	}
	_, err = tx.ExecContext(ctx, `DELETE FROM board_candidate.operations_previews WHERE id=$1 AND actor_id=$2`, id, actor.ID)
	return result, err
}

func (w employeeMutation) execute(previewing bool) (dataRow, error) {
	switch w.plan.Action {
	case "employee":
		return w.saveEmployee()
	case "operator":
		return w.saveOperator()
	case "rules":
		return w.saveRules()
	}
	if _, err := requireEmployee(w.ctx, w.tx, w.actor, w.plan.Employee); err != nil {
		return nil, err
	}
	switch w.plan.Action {
	case "card_link_batch":
		return w.linkBatch(previewing)
	case "application":
		return w.saveApplication()
	case "card_link", "replacement":
		return w.linkCard()
	case "cancel_request", "release":
		return w.recordRelease()
	case "verification":
		return w.saveVerification()
	default:
		return nil, accountwrite.Invalid("员工办卡操作不正确")
	}
}

func (w employeeMutation) success(id, employee, line string) dataRow {
	return dataRow{"id": id, "employee_id": employee, "summary": []string{line}}
}

func (w employeeMutation) event(employee, action string, detail dataRow) error {
	return updateEvent(w.ctx, w.tx, w.actor, employee, action, detail)
}

func (w employeeMutation) recordID() string {
	if id := field(w.plan.Data, "id"); id != "" {
		return id
	}
	return w.plan.NewID
}
