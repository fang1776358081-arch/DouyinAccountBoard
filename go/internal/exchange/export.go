package exchange

import (
	"context"
	"database/sql"
	"encoding/base64"
	"errors"
	"slices"
	"strings"
	"time"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/session"
)

func Export(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	kind := String(body["kind"])
	entity, ok := entities[kind]
	if !ok {
		return nil, accountwrite.Invalid("导出类型不正确")
	}
	selected := map[string]bool{}
	if value := body["ids"]; value != nil {
		list, ok := value.([]any)
		if !ok || len(list) > 5000 {
			return nil, accountwrite.Invalid("导出筛选范围不正确")
		}
		for _, id := range list {
			if String(id) == "" {
				return nil, accountwrite.Invalid("导出筛选范围不正确")
			}
			selected[String(id)] = true
		}
	}
	template := body["template"] == true
	header := make([]string, len(entity.fields))
	for i, f := range entity.fields {
		header[i] = f.label
	}
	if kind == "accounts" && !template {
		header = append(header, "登录手机序号", "资料登记电脑", "实际发布位置", "公开作品数量", "上次成功采集", "所选日期发布")
	}
	sheet := [][]string{header}
	day := String(body["date"])
	if day == "" {
		day = time.Now().In(time.FixedZone("UTC+8", 8*3600)).Format(time.DateOnly)
	}
	if _, err := time.Parse(time.DateOnly, day); err != nil {
		return nil, accountwrite.Invalid("日期格式不正确")
	}
	if !template || body["ids"] != nil {
		predicate := "($1 OR EXISTS(SELECT 1 FROM " + entity.mapping + " m WHERE m." + entity.ref + "=r.id AND m.office_id=ANY($2)))"
		records, err := matching(ctx, tx, entity, predicate+" ORDER BY r.id", actor.Global(), actor.OfficeIDs)
		if err != nil {
			return nil, err
		}
		found := 0
		for _, record := range records {
			if id := String(record["id"]); body["ids"] != nil {
				if !selected[id] {
					continue
				}
				found++
			}
			if template {
				continue
			}
			row := make([]string, len(entity.fields))
			for i, f := range entity.fields {
				row[i] = String(record[f.key])
				if kind == "devices" && f.key == "number" {
					row[i] = serialFor(String(record["id"]))
				}
				if kind == "accounts" && f.key == "parent_account_id" && !actor.Global() && row[i] != "" {
					parent, err := visibleOffice(ctx, tx, actor, "account", row[i])
					if err != nil {
						return nil, err
					}
					if !parent {
						row[i] = ""
					}
				}
			}
			if kind == "accounts" {
				extra, err := exportAccount(ctx, tx, actor, record, day)
				if err != nil {
					return nil, err
				}
				row = append(row, extra...)
			}
			sheet = append(sheet, row)
		}
		if body["ids"] != nil && len(selected) != found {
			return nil, accountwrite.Forbidden("导出包含无权访问的资料")
		}
	}
	format := String(body["format"])
	if format == "" {
		format = "xlsx"
	}
	var data []byte
	var mime string
	var err error
	switch format {
	case "csv":
		data, err = writeCSV(sheet)
		mime = "text/csv;charset=utf-8"
	case "xlsx":
		data, err = writeXLSX(sheet)
		mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
	default:
		return nil, accountwrite.Invalid("导出格式不正确")
	}
	if err != nil {
		return nil, err
	}
	filename := "工作台资料"
	if template {
		filename = "导入模板"
	}
	return map[string]any{"filename": filename + "." + format, "mime": mime,
		"count": len(sheet) - 1, "content": base64.StdEncoding.EncodeToString(data)}, nil
}

func visibleOffice(ctx context.Context, tx *sql.Tx, actor session.User, kind, ident string) (bool, error) {
	if actor.Global() {
		return true, nil
	}
	var statement string
	switch kind {
	case "account":
		statement = "SELECT office_id FROM account_offices WHERE account_id=$1"
	case "computer":
		statement = "SELECT office_id FROM computer_offices WHERE computer_id=$1"
	case "device":
		statement = "SELECT office_id FROM phone_device_offices WHERE device_id=$1"
	}
	rows, err := tx.QueryContext(ctx, statement, ident)
	if err != nil {
		return false, err
	}
	defer rows.Close()
	for rows.Next() {
		var office string
		if err := rows.Scan(&office); err != nil {
			return false, err
		}
		if slices.Contains(actor.OfficeIDs, office) {
			return true, nil
		}
	}
	return false, rows.Err()
}

func exportAccount(ctx context.Context, tx *sql.Tx, actor session.User, row map[string]any, day string) ([]string, error) {
	id := String(row["id"])
	device, computer := String(row["phone_device_id"]), String(row["computer_id"])
	if visible, err := visibleOffice(ctx, tx, actor, "device", device); err != nil {
		return nil, err
	} else if !visible {
		device = ""
	}
	name := ""
	if visible, err := visibleOffice(ctx, tx, actor, "computer", computer); err != nil {
		return nil, err
	} else if visible {
		err = tx.QueryRowContext(ctx, "SELECT name FROM computers WHERE id=$1", computer).Scan(&name)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return nil, err
		}
	}
	positions, err := tx.QueryContext(ctx, `SELECT c.name,p.slot,p.source_cell FROM publishing_layout p JOIN computers c ON c.id=p.computer_id
		WHERE p.account_id=$1 AND p.batch=(SELECT batch FROM publishing_layout_state WHERE id=1)
		AND ($2 OR EXISTS(SELECT 1 FROM computer_offices s WHERE s.computer_id=p.computer_id AND s.office_id=ANY($3)))`,
		id, actor.Global(), actor.OfficeIDs)
	if err != nil {
		return nil, err
	}
	locations := []string{}
	for positions.Next() {
		var host, slot, cell string
		if err := positions.Scan(&host, &slot, &cell); err != nil {
			positions.Close()
			return nil, err
		}
		locations = append(locations, host+"/设备"+slot+"/"+cell)
	}
	positions.Close()
	if err := positions.Err(); err != nil {
		return nil, err
	}
	var published bool
	err = tx.QueryRowContext(ctx, "SELECT EXISTS(SELECT 1 FROM daily WHERE account_id=$1 AND day=$2 AND published<>0)", id, day).Scan(&published)
	if err != nil {
		return nil, err
	}
	label := "未发"
	if published {
		label = "已发"
	}
	return []string{device, name, strings.Join(locations, "；"), String(row["work_count"]), String(row["last_collected"]), day + " · " + label}, nil
}
