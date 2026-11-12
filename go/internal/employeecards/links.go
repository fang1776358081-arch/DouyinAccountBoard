package employeecards

import (
	"fmt"
	"slices"
	"strconv"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/recordwire"
)

func normalizeBatch(data dataRow, today string) (dataRow, error) {
	if err := validateFields(data, "cards", "source"); err != nil {
		return nil, err
	}
	source, err := employeeText(valueOr(data, "source", ""), "实名关联依据", 500, true)
	if err != nil {
		return nil, err
	}
	cards, ok := strictRows(data["cards"])
	if !ok || len(cards) == 0 || len(cards) > 200 {
		return nil, accountwrite.Invalid("每批请选择1至200张手机卡")
	}
	seen := map[int64]bool{}
	result := make([]dataRow, 0, len(cards))
	for _, item := range cards {
		if err := validateFields(item, "card_id", "operator_id", "activated_on"); err != nil {
			return nil, err
		}
		p := employeeInput{row: item}
		card := p.number("card_id", "手机卡编号", nil, 1, 9223372036854775807, false)
		op := p.text("operator_id", "每张卡的发卡运营商", "", 80, true)
		activated := p.date("activated_on", "办理日期", "", true, today)
		if p.err != nil {
			return nil, p.err
		}
		id := numberValue(card)
		if seen[id] {
			return nil, accountwrite.Invalid("同一批手机卡编号不能重复")
		}
		seen[id] = true
		result = append(result, dataRow{"card_id": id, "operator_id": op, "activated_on": activated})
	}
	slices.SortFunc(result, func(a, b dataRow) int {
		left, right := numberValue(a["card_id"]), numberValue(b["card_id"])
		switch {
		case left < right:
			return -1
		case left > right:
			return 1
		default:
			return 0
		}
	})
	return dataRow{"cards": result, "source": source}, nil
}

type realLink struct {
	id                string
	card, operator    dataRow
	activated, source string
}

func (w employeeMutation) insertLink(record realLink, newUnit bool, action, batch string) error {
	employee, card := w.plan.Employee, record.card
	cardID := numberValue(card["id"])
	if newUnit {
		network, err := encodeRow(networkPolicy(record.operator, record.activated, w.today))
		if err != nil {
			return err
		}
		if _, err := w.tx.ExecContext(w.ctx, `INSERT INTO board_candidate.employee_card_units
			(id,employee_id,operator_id,phase,requested_on,activated_on,current_card_id,source,network_policy)
			VALUES($1,$2,$3,'active',$4,$4,$5,$6,$7)`, record.id, employee, record.operator["id"], record.activated, cardID, record.source, network); err != nil {
			return err
		}
	}
	key, err := employeeKey()
	if err != nil {
		return err
	}
	if _, err := w.tx.ExecContext(w.ctx, `INSERT INTO board_candidate.employee_card_links
		(id,unit_id,card_id,phone_snapshot,source,linked_on) VALUES($1,$2,$3,$4,$5,$6)`,
		key, record.id, cardID, card["phone"], record.source, w.today); err != nil {
		return err
	}
	detail := dataRow{"unit_id": record.id, "card_id": cardID, "source": record.source}
	if batch != "" {
		detail["batch_id"] = batch
	}
	return w.event(employee, action, detail)
}

func (w employeeMutation) linkCard() (dataRow, error) {
	data, employee := w.plan.Data, w.plan.Employee
	if err := validateFields(data, "unit_id", "card_id", "operator_id", "activated_on", "source"); err != nil {
		return nil, err
	}
	card, err := lookupCard(w.ctx, w.tx, w.actor, data["card_id"], false)
	if err != nil {
		return nil, err
	}
	source, err := employeeText(valueOr(data, "source", ""), "实名关联依据", 500, true)
	if err != nil {
		return nil, err
	}
	var unit dataRow
	if id := field(data, "unit_id"); id != "" {
		unit, err = readUnit(w.ctx, w.tx, employee, id)
		if err != nil {
			return nil, err
		}
	}
	links, err := getRows(w.ctx, w.tx, `SELECT to_json(l)::text FROM board_candidate.employee_card_links l
		WHERE card_id=$1 OR phone_snapshot=$2 ORDER BY id`, card["id"], card["phone"])
	if err != nil {
		return nil, err
	}
	if len(links) != 0 {
		if len(links) != 1 || numberValue(links[0]["card_id"]) != numberValue(card["id"]) {
			return nil, accountwrite.Invalid("此号码已有历史实名关联，需核对原占额单元，不能猜测合并")
		}
		old, err := readUnit(w.ctx, w.tx, employee, field(links[0], "unit_id"))
		if err != nil {
			return nil, err
		}
		op := field(data, "operator_id")
		if (unit != nil && unit["id"] != old["id"]) || (op != "" && op != old["operator_id"]) {
			return nil, accountwrite.Invalid("手机卡已关联其他占额单元或运营商")
		}
		return w.success(field(old, "id"), employee, "该卡已关联，保持原占额单元；不重复计算"), nil
	}
	var occupied bool
	if err := w.tx.QueryRowContext(w.ctx, `SELECT EXISTS(SELECT 1 FROM board_candidate.employee_card_units
		WHERE current_card_id=$1 AND id<>$2)`, card["id"], field(unit, "id")).Scan(&occupied); err != nil {
		return nil, err
	}
	if occupied {
		return nil, accountwrite.Invalid("该卡已占用其他实名单元，请核对原关联")
	}
	id := w.plan.NewID
	if w.plan.Action == "replacement" {
		if unit["phase"] != "active" {
			return nil, accountwrite.Invalid("补换卡请选择尚未释放的在用占额单元")
		}
		id = field(unit, "id")
		if _, err := w.tx.ExecContext(w.ctx, `UPDATE board_candidate.employee_card_units
			SET current_card_id=$2,revision=revision+1 WHERE id=$1`, id, card["id"]); err != nil {
			return nil, err
		}
		err = w.insertLink(realLink{id: id, card: card, source: source}, false, w.plan.Action, "")
	} else {
		op, err := readOperator(w.ctx, w.tx, field(data, "operator_id"))
		if _, supplied := data["operator_id"]; !supplied && unit != nil {
			op, err = readOperator(w.ctx, w.tx, field(unit, "operator_id"))
		}
		if err != nil {
			return nil, err
		}
		activated, err := employeeDate(valueOr(data, "activated_on", ""), "办理日期", true, w.today)
		if err != nil {
			return nil, err
		}
		if unit != nil {
			if unit["phase"] != "pending" || unit["operator_id"] != op["id"] ||
				(activated != "" && activated < field(unit, "requested_on")) {
				return nil, accountwrite.Invalid("请将同一运营商的未完成在途单元关联到实际办卡结果")
			}
			id = field(unit, "id")
			network, err := encodeRow(networkPolicy(op, activated, w.today))
			if err != nil {
				return nil, err
			}
			if _, err := w.tx.ExecContext(w.ctx, `UPDATE board_candidate.employee_card_units
				SET phase='active',activated_on=$2,current_card_id=$3,source=$4,network_policy=$5,revision=revision+1 WHERE id=$1`,
				id, activated, card["id"], source, network); err != nil {
				return nil, err
			}
		}
		err = w.insertLink(realLink{id, card, op, activated, source}, unit == nil, w.plan.Action, "")
		if err != nil {
			return nil, err
		}
	}
	return w.success(id, employee, "保存明确实名关系；不修改原卡主、使用人、保管人或卡状态"), err
}

func (w employeeMutation) linkBatch(previewing bool) (dataRow, error) {
	employee, data := w.plan.Employee, w.plan.Data
	items := resultRows(data["cards"])
	cards, ops := map[int64]dataRow{}, map[string]dataRow{}
	ids, phones := []int64{}, []string{}
	for _, card := range w.plan.Cards {
		id := numberValue(card["id"])
		cards[id] = card
		ids, phones = append(ids, id), append(phones, field(card, "phone"))
	}
	for _, item := range items {
		key := field(item, "operator_id")
		if ops[key] == nil {
			op, err := readOperator(w.ctx, w.tx, key)
			if err != nil {
				return nil, err
			}
			ops[key] = op
		}
	}
	links, err := getRows(w.ctx, w.tx, `SELECT (to_jsonb(l)||jsonb_build_object(
		'employee_id',u.employee_id,'operator_id',u.operator_id,'activated_on',u.activated_on))::text
		FROM board_candidate.employee_card_links l LEFT JOIN board_candidate.employee_card_units u ON u.id=l.unit_id
		WHERE l.card_id=ANY($1) OR l.phone_snapshot=ANY($2) ORDER BY l.id`, ids, phones)
	if err != nil {
		return nil, err
	}
	digest, err := recordwire.Version(links)
	if err != nil {
		return nil, err
	}
	unlinked, err := getRows(w.ctx, w.tx, `SELECT json_build_object('id',u.current_card_id)::text
		FROM board_candidate.employee_card_units u WHERE u.current_card_id=ANY($1) AND
		NOT EXISTS(SELECT 1 FROM board_candidate.employee_card_links l WHERE l.unit_id=u.id AND l.card_id=u.current_card_id)`, ids)
	if err != nil {
		return nil, err
	}
	unlinkedCards := map[int64]bool{}
	for _, item := range unlinked {
		unlinkedCards[numberValue(item["id"])] = true
	}
	rows, fresh := []dataRow{}, []realLink{}
	counts := map[string]int{"total": len(items), "new": 0, "existing": 0, "conflict": 0}
	for _, item := range items {
		id := numberValue(item["card_id"])
		card, op := cards[id], ops[field(item, "operator_id")]
		if card == nil {
			return nil, accountwrite.Conflict("手机卡资料或范围已变化，请重新预览；未保存")
		}
		phone := field(card, "phone")
		prior := []dataRow{}
		for _, link := range links {
			if numberValue(link["card_id"]) == id || link["phone_snapshot"] == phone {
				prior = append(prior, link)
			}
		}
		status, reason, activated := "new", "新建明确实名关联；原手机卡资料保持不变", field(item, "activated_on")
		switch {
		case phone == "":
			status, reason = "conflict", "当前卡号码缺失，请先核对原手机卡资料"
		case unlinkedCards[id]:
			status, reason = "conflict", "已有占额单元缺少实名关联，请先核对历史；本批不会覆盖"
		case len(prior) != 0:
			old := prior[0]
			if len(prior) == 1 && numberValue(old["card_id"]) == id && old["phone_snapshot"] == phone &&
				old["employee_id"] == employee && old["operator_id"] == op["id"] {
				status, reason, activated = "existing", "已关联同一员工和运营商，保留原办理日期，不重复新增", field(old, "activated_on")
			} else {
				status, reason = "conflict", "已有其他实名关联、运营商或号码历史冲突，请核对；本批不会重绑或覆盖"
			}
		}
		counts[status]++
		rows = append(rows, dataRow{"card_id": id, "phone": phone, "operator_id": op["id"], "operator_name": op["name"], "status": status,
			"status_label": map[string]string{"new": "新关联", "existing": "已有关联", "conflict": "冲突待核对"}[status], "reason": reason, "activated_on": activated})
		if status == "new" {
			key := w.plan.BatchIDs[strconv.FormatInt(id, 10)]
			if key == "" {
				return nil, accountwrite.Conflict("本批实名关联预览不完整，请重新预览")
			}
			fresh = append(fresh, realLink{key, card, op, activated, field(data, "source")})
		}
	}
	conflict := counts["conflict"] != 0
	if previewing {
		w.plan.BatchBlocked, w.plan.BatchDigest = &conflict, digest
	} else {
		if w.plan.BatchBlocked == nil || *w.plan.BatchBlocked {
			return nil, accountwrite.Conflict("该批预览存在冲突，不能确认；请核对后重新预览；整批未保存")
		}
		if conflict || w.plan.BatchDigest != digest {
			return nil, accountwrite.Conflict("手机卡实名关联已变化，请重新预览；整批未保存")
		}
	}
	if !conflict {
		for _, record := range fresh {
			if err := w.insertLink(record, true, "card_link_batch", w.plan.NewID); err != nil {
				return nil, err
			}
		}
	}
	message := "仅保存实名关联，原卡资料不变"
	if conflict {
		message = "存在冲突，整批禁止确认"
	}
	return dataRow{"id": w.plan.NewID, "employee_id": employee, "counts": counts, "rows": rows, "has_conflicts": conflict,
		"can_apply": !conflict, "summary": []string{fmt.Sprintf("本批%d张：新关联%d张，已有关联%d张，冲突%d张；%s",
			counts["total"], counts["new"], counts["existing"], counts["conflict"], message),
			"本次登记存量实名关系，不自动完成在途申请；已有申请请在高级办卡管理逐项核对。"}}, nil
}
