package exchange

import (
	"context"
	"database/sql"
	"encoding/base64"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"douyinboard/internal/accountwrite"
	"douyinboard/internal/inventory"
	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

const (
	officeDualKind          = "office-three-dual-import"
	officeDualConfirmation  = "导入办公室三"
	officeScopeConfirmation = "仅分配电脑到办公室三"
	officeSourceURL         = "https://docs.qq.com/sheet/DRWxWc1huUWFsVkZp?tab=nh0fs2"
	officeDualPath          = "/api/workspace/office-three-import-apply"
	officeScopePath         = "/api/workspace/office-three-import-computer-scopes"
	officeLayoutRule        = 1
)

var (
	officeAccountHeaders = []string{"来源行", "注册手机号", "昵称", "登录手机序号", "抖音号", "小号说明",
		"门头", "负责人", "登记电脑序号", "来源类型", "来源账号状态", "违规备注", "使用状态", "注册日期"}
	officeProgressHeaders = []string{"来源行", "电脑序号", "负责人", "临时负责人", "类别", "发布设备1",
		"发布设备2", "发布设备3", "发布设备4", "发布设备5", "发布设备6", "发布设备7", "发布设备8", "发布设备9", "发布设备10"}
	officeAccountColumns    = []int{2, 5, 6, 7, 8, 9, 10, 11, 12, 15, 16, 17, 18}
	officeProgressColumns   = []int{0, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 17}
	officeRowNumber         = regexp.MustCompile(`^[1-9][0-9]{0,5}$`)
	officeComputerNumber    = regexp.MustCompile(`^([1-9][0-9]{0,5})(?:号(?:电脑|设备))?$`)
	officePhoneNumber       = regexp.MustCompile(`^([1-9][0-9]{0,5})(?:号手机|多开(?:抖音)?|小号)?$`)
	officeDouyinID          = regexp.MustCompile(`^[A-Za-z0-9_.-]{2,100}$`)
	officeFullPhone         = regexp.MustCompile(`^1[0-9]{10}$`)
	officeSourceComputerID  = regexp.MustCompile(`^office3:[1-9][0-9]{0,5}$`)
	officeHandleLabel       = regexp.MustCompile(`^(?:抖音号|douyin_id)\s*[:：]\s*([A-Za-z0-9_.-]{2,100})$`)
	officeNumericDate       = regexp.MustCompile(`^[0-9]{1,6}(?:\.0+)?$`)
	officeChineseDate       = regexp.MustCompile(`^([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})日$`)
	officeGeneratedNickname = regexp.MustCompile(`^昵称待补充(?:（手机[1-9][0-9]{0,5}）)?$`)
	officeComputerEntity    = entity{table: "computers", mapping: "computer_offices", ref: "computer_id"}
)

type officeSourceRow struct {
	Number int
	Fields map[string]string
}

func readOfficeAccounts(file map[string]any) ([]officeSourceRow, error) {
	return readOfficeSource(file, true)
}

func officeFile(file map[string]any) (string, string, error) {
	filename, fileOK := file["filename"].(string)
	content, contentOK := file["content"].(string)
	if !fileOK || !contentOK {
		return "", "", accountwrite.Invalid("请分别选择账号情况表和工作进度表")
	}
	return filename, content, nil
}

func readOfficeSource(file map[string]any, accounts bool) ([]officeSourceRow, error) {
	filename, content, err := officeFile(file)
	if err != nil {
		return nil, err
	}
	rows, err := readSheet(filename, content)
	if err != nil {
		return nil, accountwrite.Invalid(err.Error())
	}
	header := slices.Clone(rows[0])
	for i, cell := range header {
		header[i] = strings.TrimSpace(cell)
	}
	safe, columns, title := officeProgressHeaders, officeProgressColumns, "工作进度表"
	if accounts {
		safe, columns, title = officeAccountHeaders, officeAccountColumns, "账号情况表"
	}
	compact := len(header) >= len(safe) && slices.Equal(header[:len(safe)], safe)
	if !compact && !validOfficeRawHeader(header, accounts) {
		return nil, accountwrite.Invalid(title + "列结构不匹配，请使用安全模板或原始双表")
	}
	clean := []officeSourceRow{}
	for i, row := range rows[1:] {
		item := officeSourceRow{Number: i + 2, Fields: map[string]string{}}
		hasValue := false
		for fieldIndex, index := range columns {
			if compact {
				index = fieldIndex + 1
			}
			value, err := officeCell(row, index)
			if err != nil {
				return nil, err
			}
			item.Fields[safe[fieldIndex+1]] = value
			hasValue = hasValue || value != ""
		}
		if !hasValue {
			continue
		}
		if compact {
			source, err := officeCell(row, 0)
			if err != nil {
				return nil, err
			}
			if source != "" {
				if !officeRowNumber.MatchString(source) {
					return nil, accountwrite.Invalid("来源行号格式不正确")
				}
				item.Number, _ = strconv.Atoi(source)
			}
		}
		clean = append(clean, item)
	}
	return clean, nil
}

func officeCell(row []string, index int) (string, error) {
	if index >= len(row) {
		return "", nil
	}
	value := strings.TrimSpace(row[index])
	if utf8.RuneCountInString(value) > 2000 {
		return "", accountwrite.Invalid("可导入资料单元格超过2000字，请先整理表格")
	}
	if strings.HasPrefix(value, "=") {
		return "", accountwrite.Invalid("导入不接受公式，请先粘贴为值")
	}
	if strings.IndexByte(value, 0) >= 0 {
		return "", accountwrite.Invalid("可导入资料单元格含无效字符")
	}
	return value, nil
}

func validOfficeRawHeader(header []string, accounts bool) bool {
	if accounts {
		return len(header) >= 19 && header[2] == "注册抖音用的手机号" && header[5] == "抖音名称" &&
			header[6] == "抖音在哪个手机上" && header[7] == "抖音号" && header[11] == "对应电脑"
	}
	if len(header) < 18 {
		return false
	}
	for _, i := range officeProgressColumns[4:] {
		if !strings.Contains(header[i], "发布") || !strings.Contains(header[i], "设备") {
			return false
		}
	}
	return true
}

func officeDualSources(body map[string]any) ([]officeSourceRow, []officeSourceRow, error) {
	accounts, _ := body["accounts_file"].(map[string]any)
	progress, _ := body["progress_file"].(map[string]any)
	size := 0
	for _, file := range []map[string]any{accounts, progress} {
		_, content, err := officeFile(file)
		if err != nil {
			return nil, nil, err
		}
		if len(content) > base64.StdEncoding.EncodedLen(maxFileSize) {
			return nil, nil, accountwrite.Invalid("两张表文件合计不能超过8MB")
		}
		// Estimate the decoded length without making an additional complete copy.
		// readSheet performs strict decoding and validation before processing rows.
		size += base64.StdEncoding.DecodedLen(len(content)) - min(2, len(content)-len(strings.TrimRight(content, "=")))
	}
	if size > maxFileSize {
		return nil, nil, accountwrite.Invalid("两张表文件合计不能超过8MB")
	}
	accountRows, err := readOfficeAccounts(accounts)
	if err != nil {
		return nil, nil, err
	}
	progressRows, err := readOfficeSource(progress, false)
	return accountRows, progressRows, err
}

func officeDigits(value string, computer bool) string {
	value = strings.Map(func(r rune) rune {
		if unicode.IsSpace(r) {
			return -1
		}
		return r
	}, value)
	pattern := officePhoneNumber
	if computer {
		pattern = officeComputerNumber
	}
	if match := pattern.FindStringSubmatch(value); match != nil {
		return match[1]
	}
	return ""
}

func officeLabelHandle(label string) string {
	if match := officeHandleLabel.FindStringSubmatch(label); match != nil {
		return match[1]
	}
	return ""
}

func officeRegistered(value, day string) (string, bool) {
	if value == "" {
		return "", true
	}
	var date time.Time
	var err error
	if officeNumericDate.MatchString(value) {
		raw, _, _ := strings.Cut(value, ".")
		days, _ := strconv.Atoi(raw)
		date = time.Date(1899, 12, 30, 0, 0, 0, 0, time.UTC).AddDate(0, 0, days)
	} else {
		if match := officeChineseDate.FindStringSubmatch(value); match != nil {
			value = match[1] + "-" + strings.Repeat("0", 2-len(match[2])) + match[2] +
				"-" + strings.Repeat("0", 2-len(match[3])) + match[3]
		}
		layout := time.DateOnly
		if len(value) == 8 {
			layout = "20060102"
		}
		date, err = time.Parse(layout, value)
	}
	result := date.Format(time.DateOnly)
	if err != nil || date.Year() < 1 || result > day {
		return "", false
	}
	return result, true
}

type officePlanComputer struct {
	ID, Name, Operator, Category string
}

type officePlanDevice struct {
	ID, Name string
}

type officePlanAccount struct {
	ID, ComputerID, DeviceID, Name, Status, Note, Handle, RegisteredOn, Phone, LifecycleNote, DeviceNote string
	Slot                                                                                                 int
}

type officePlanLayout struct {
	ComputerID, AccountID, Label, SourceCell, Reason string
	Slot                                             int
}

type officeScopeCandidate struct {
	ID string `json:"id"`
}

type officePlan struct {
	Office          string
	Guard           string
	Batch           string
	LayoutRule      int
	Computers       []officePlanComputer
	Devices         []officePlanDevice
	Accounts        []officePlanAccount
	Layouts         []officePlanLayout
	ScopeCandidates []officeScopeCandidate
	Summary         map[string]int
}

type officePublicRow struct {
	Kind   string `json:"kind"`
	Row    int    `json:"row"`
	Action string `json:"action"`
	Label  string `json:"label"`
	Reason string `json:"reason"`
}

func officeScopeExactly(record map[string]any) bool {
	values, ok := stringList(record["office_ids"])
	return ok && len(values) == 1 && values[0] == officeThreeID
}

type officePlanner struct {
	plan          officePlan
	rows          []officePublicRow
	warnings      []string
	computers     map[string]map[string]any
	devices       map[string]map[string]any
	deviceScope   map[string][]map[string]any
	serials       map[string]string
	availablePC   map[string]bool
	createdDevice map[string]bool
}

func (p *officePlanner) item(kind string, row int, action, label, reason string) {
	p.rows = append(p.rows, officePublicRow{Kind: kind, Row: row, Action: action, Label: label, Reason: reason})
}

func (p *officePlanner) computer(id string, source officeSourceRow, name, operator, category string) bool {
	if available, known := p.availablePC[id]; known {
		return available
	}
	old := p.computers[id]
	if old != nil && !officeScopeExactly(old) {
		p.item("computer", source.Number, "conflict", id, "编号已存在，但并非明确且仅属于办公室三；保留原资料")
		p.availablePC[id] = false
		return false
	}
	if old == nil {
		p.plan.Computers = append(p.plan.Computers, officePlanComputer{id, name, operator, category})
		p.item("computer", source.Number, "create", name, "")
	}
	p.availablePC[id] = true
	return true
}

func (p *officePlanner) device(serial string, row int) string {
	if serial == "" {
		return ""
	}
	if ident, known := p.serials[serial]; known {
		return ident
	}
	matches := p.deviceScope[serial]
	id := "office3:" + serial
	switch {
	case len(matches) > 1 || (len(matches) == 1 && !officeScopeExactly(matches[0])):
		p.item("device", row, "conflict", "手机"+serial, "同一手机序号有多条资料或并非仅属于办公室三；保留原资料")
		id = ""
	case len(matches) == 1:
		id = String(matches[0]["id"])
	case p.devices[id] != nil:
		p.item("device", row, "conflict", id, "原来源编号已存在，但并非明确且仅属于办公室三；保留原资料")
		id = ""
	}
	p.serials[serial] = id
	return id
}

func (p *officePlanner) ensureDevice(id, serial string, row int) {
	if p.devices[id] != nil || p.createdDevice[id] {
		return
	}
	name := "办公室三 · " + serial + "号手机"
	p.plan.Devices = append(p.plan.Devices, officePlanDevice{id, name})
	p.item("device", row, "create", name, "")
	p.createdDevice[id] = true
}

func planOfficeThree(ctx context.Context, tx *sql.Tx, sourceAccounts, progress []officeSourceRow) (officePlan, []officePublicRow, []string, []map[string]any, error) {
	p := officePlanner{
		plan: officePlan{Office: officeThreeID, LayoutRule: officeLayoutRule, Computers: []officePlanComputer{}, Devices: []officePlanDevice{},
			Accounts: []officePlanAccount{}, Layouts: []officePlanLayout{}, ScopeCandidates: []officeScopeCandidate{}},
		rows: []officePublicRow{},
		warnings: []string{"仅新增安全资料；已有账号、设备、电脑、发布格子和手工关联一律保留，不导入密码、远控信息或每日发布历史。",
			"发布格只有标明完整抖音号并唯一对应本办公室未占位账号时自动关联。未匹配或冲突项在空位置保留原文，供人工核对。",
			"来源手机编号只作资料登记，登录状态与实物手机均待确认，不视为已登录或已换机。"},
		computers: map[string]map[string]any{}, devices: map[string]map[string]any{}, deviceScope: map[string][]map[string]any{},
		serials: map[string]string{}, availablePC: map[string]bool{}, createdDevice: map[string]bool{},
	}
	if err := tx.QueryRowContext(ctx, `SELECT COALESCE((SELECT batch FROM publishing_layout_state WHERE id=1),'')`).Scan(&p.plan.Batch); err != nil {
		return officePlan{}, nil, nil, nil, err
	}
	computerCounts, progressHandles := map[string]int{}, map[string]int{}
	for _, row := range progress {
		computerCounts[officeDigits(row.Fields["电脑序号"], true)]++
		for slot := 1; slot <= 10; slot++ {
			if handle := officeLabelHandle(row.Fields["发布设备"+strconv.Itoa(slot)]); handle != "" {
				progressHandles[handle]++
			}
		}
	}
	computerIDs := []string{"office3:unassigned"}
	for serial := range computerCounts {
		if serial != "" {
			computerIDs = append(computerIDs, "office3:"+serial)
		}
	}
	computers, err := matching(ctx, tx, officeComputerEntity, "r.id=ANY($1)", computerIDs)
	if err != nil {
		return officePlan{}, nil, nil, nil, err
	}
	for _, row := range computers {
		p.computers[String(row["id"])] = row
	}
	devices, err := matching(ctx, tx, entities["devices"], `r.id LIKE 'office3:%' OR EXISTS(
		SELECT 1 FROM phone_device_offices m WHERE m.device_id=r.id AND m.office_id=$1)`, officeThreeID)
	if err != nil {
		return officePlan{}, nil, nil, nil, err
	}
	for _, row := range devices {
		id := String(row["id"])
		p.devices[id] = row
		offices, _ := stringList(row["office_ids"])
		if serial := serialFor(id); serial != "" && slices.Contains(offices, officeThreeID) {
			p.deviceScope[serial] = append(p.deviceScope[serial], row)
		}
	}

	sourceComputers := map[string]officeSourceRow{}
	computerOrder := []string{}
	for _, row := range progress {
		values := row.Fields
		serial := officeDigits(values["电脑序号"], true)
		if serial == "" {
			for slot := 1; slot <= 10; slot++ {
				if values["发布设备"+strconv.Itoa(slot)] != "" {
					p.item("computer", row.Number, "conflict", "电脑编号待核对", "未识别到明确电脑编号，整行发布安排不导入")
					break
				}
			}
			continue
		}
		if computerCounts[serial] != 1 {
			p.item("computer", row.Number, "conflict", "电脑"+serial, "工作进度表电脑编号重复，整行保留待核对")
			continue
		}
		id := "office3:" + serial
		if utf8.RuneCountInString(values["负责人"]) > 100 || utf8.RuneCountInString(values["临时负责人"]) > 100 || utf8.RuneCountInString(values["类别"]) > 100 {
			p.item("computer", row.Number, "conflict", id, "电脑负责人或类别内容过长")
			p.availablePC[id] = false
			continue
		}
		sourceComputers[id] = row
		computerOrder = append(computerOrder, id)
		old := p.computers[id]
		if offices, _ := stringList(old["office_ids"]); old != nil && len(offices) == 0 {
			p.plan.ScopeCandidates = append(p.plan.ScopeCandidates, officeScopeCandidate{id})
		}
		operator := values["临时负责人"]
		if operator == "" {
			operator = values["负责人"]
		}
		p.computer(id, row, "办公室三 · "+serial+"号电脑", operator, values["类别"])
	}
	slices.SortFunc(p.plan.ScopeCandidates, func(a, b officeScopeCandidate) int {
		x, _ := strconv.Atoi(strings.TrimPrefix(a.ID, "office3:"))
		y, _ := strconv.Atoi(strings.TrimPrefix(b.ID, "office3:"))
		return x - y
	})

	counts := map[string]int{}
	for _, row := range sourceAccounts {
		counts[row.Fields["抖音号"]]++
	}
	handles := []string{}
	for handle, count := range counts {
		if count == 1 && officeDouyinID.MatchString(handle) {
			handles = append(handles, handle)
		}
	}
	for handle := range progressHandles {
		if counts[handle] != 1 {
			handles = append(handles, handle)
		}
	}
	accountRows, err := matching(ctx, tx, entities["accounts"], "r.douyin_id=ANY($1)", handles)
	if err != nil {
		return officePlan{}, nil, nil, nil, err
	}
	existing := map[string][]map[string]any{}
	localAccounts := map[string][]string{}
	for _, row := range accountRows {
		handle := String(row["douyin_id"])
		existing[handle] = append(existing[handle], row)
		if officeScopeExactly(row) {
			localAccounts[handle] = append(localAccounts[handle], String(row["id"]))
		}
	}
	slots, err := officeAccountSlots(ctx, tx, computerIDs)
	if err != nil {
		return officePlan{}, nil, nil, nil, err
	}
	sourceAccountCount := 0
	day := today()
	suggestions := []map[string]any{}
	for _, row := range sourceAccounts {
		fields := row.Fields
		handle, name := fields["抖音号"], fields["昵称"]
		if handle == "" && name == "" {
			continue
		}
		sourceAccountCount++
		label := name
		if label == "" {
			label = handle
		}
		if counts[handle] != 1 || !officeDouyinID.MatchString(handle) {
			p.item("account", row.Number, "conflict", label, "真实抖音号缺失、不明确或在来源表重复；不新增猜测身份")
			continue
		}
		old := existing[handle]
		if len(old) > 1 || (len(old) == 1 && !officeScopeExactly(old[0])) {
			p.item("account", row.Number, "conflict", label, "同抖音号存在多条资料，或属于其他办公室/待分配区；不移动、不覆盖")
			continue
		}
		serial := officeDigits(fields["登录手机序号"], false)
		if len(old) == 1 {
			id, oldName := String(old[0]["id"]), String(old[0]["name"])
			if name != "" && utf8.RuneCountInString(name) <= 100 && !strings.HasPrefix(name, "昵称待补充") &&
				(oldName == "" || officeGeneratedNickname.MatchString(oldName)) {
				suggestions = append(suggestions, map[string]any{"row": row.Number, "account_id": id,
					"douyin_id": handle, "current_name": oldName, "source_name": name})
			}
			p.item("account", row.Number, "reuse", label, "仅复用真实抖音号对应的办公室三账号，原字段全部保留")
			continue
		}
		if serial == "" {
			p.item("account", row.Number, "conflict", label, "来源登录手机编号不明确，保留待核对")
			continue
		}
		computerID := "office3:unassigned"
		if raw := fields["登记电脑序号"]; raw != "" {
			computerID = "office3:" + officeDigits(raw, true)
			if _, found := sourceComputers[computerID]; !found {
				p.item("account", row.Number, "conflict", label, "登记电脑编号不明确或工作进度表缺少唯一对应电脑")
				continue
			}
		} else {
			p.computer(computerID, row, "办公室三 · 未分配电脑", "", "")
		}
		if !p.availablePC[computerID] {
			p.item("account", row.Number, "conflict", label, "登记电脑存在归属冲突")
			continue
		}
		if utf8.RuneCountInString(name) > 100 {
			p.item("account", row.Number, "conflict", label, "昵称超过100字")
			continue
		}
		slot := slots[computerID] + 1
		if slot > 100 {
			p.item("account", row.Number, "conflict", label, "登记电脑账号位置超过100，需先核对安排")
			continue
		}
		deviceID := p.device(serial, row.Number)
		if deviceID == "" {
			p.item("account", row.Number, "conflict", label, "手机编号存在归属冲突")
			continue
		}
		p.ensureDevice(deviceID, serial, row.Number)
		item := p.account(row, slot, computerID, deviceID, serial, day)
		p.plan.Accounts = append(p.plan.Accounts, item)
		p.item("account", row.Number, "create", item.Name, "")
		slots[computerID] = slot
		localAccounts[handle] = append(localAccounts[handle], item.ID)
	}
	if err := p.planLayouts(ctx, tx, computerOrder, sourceComputers, localAccounts, counts, progressHandles); err != nil {
		return officePlan{}, nil, nil, nil, err
	}
	p.plan.Summary = map[string]int{"new_accounts": len(p.plan.Accounts), "reused_accounts": 0,
		"new_computers": len(p.plan.Computers), "new_devices": len(p.plan.Devices), "new_layouts": len(p.plan.Layouts),
		"pending_layouts": 0, "conflicts": 0, "source_accounts": sourceAccountCount, "source_computers": len(sourceComputers)}
	for _, item := range p.rows {
		if item.Action == "conflict" {
			p.plan.Summary["conflicts"]++
		}
		if item.Action == "pending" {
			p.plan.Summary["pending_layouts"]++
		}
		if item.Kind == "account" && item.Action == "reuse" {
			p.plan.Summary["reused_accounts"]++
		}
	}
	return p.plan, p.rows, p.warnings, suggestions, nil
}

func officeAccountSlots(ctx context.Context, tx *sql.Tx, ids []string) (map[string]int, error) {
	rows, err := tx.QueryContext(ctx, `SELECT computer_id,COALESCE(MAX(slot),0) FROM accounts WHERE computer_id=ANY($1) GROUP BY computer_id`, ids)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	slots := map[string]int{}
	for rows.Next() {
		var id string
		var slot int
		if err := rows.Scan(&id, &slot); err != nil {
			return nil, err
		}
		slots[id] = slot
	}
	return slots, rows.Err()
}

func (p *officePlanner) account(row officeSourceRow, slot int, computer, device, serial, day string) officePlanAccount {
	fields := row.Fields
	rowLabel := "账号情况表第" + strconv.Itoa(row.Number) + "行"
	date, valid := officeRegistered(fields["注册日期"], day)
	if !valid {
		p.warnings = append(p.warnings, rowLabel+"注册日期无效或晚于今天，未导入该日期。")
	}
	phone := fields["注册手机号"]
	if phone != "" && !officeFullPhone.MatchString(phone) {
		p.warnings = append(p.warnings, rowLabel+"注册手机号不完整，未导入该号码。")
		phone = ""
	}
	name := fields["昵称"]
	if name == "" {
		name = "昵称待补充（手机" + serial + "）"
		p.warnings = append(p.warnings, rowLabel+"昵称缺失，使用明确的待补充占位名。")
	}
	status := map[string]string{"正常": "正常", "未使用": "停用", "账号永久封禁": "封禁", "账号短期封禁": "短期封禁",
		"养号7天": "养号7天"}[fields["来源账号状态"]]
	if status == "" {
		status = "待确认"
	}
	notes := []string{"腾讯文档账号情况表，第" + strconv.Itoa(row.Number) + "行；办公室三双表导入"}
	for _, field := range [][2]string{{"来源账号状态", "原账号状态"}, {"使用状态", "使用情况"}, {"负责人", "负责人"},
		{"来源类型", "类型说明"}, {"违规备注", "违规/处理备注"}, {"门头", "门头"}} {
		if value := fields[field[0]]; value != "" {
			notes = append(notes, field[1]+"："+value)
		}
	}
	lifecycle := "来源未确认大小号类型和关联关系，待核对"
	if value := fields["小号说明"]; value != "" {
		lifecycle = limitRunes("原小号说明："+value+"；类型及关联关系待核对", 1000)
	}
	return officePlanAccount{ID: "account-" + newHexID(), ComputerID: computer, DeviceID: device, Slot: slot,
		Name: name, Status: status, Note: limitRunes(strings.Join(notes, "\n"), 2000), Handle: fields["抖音号"],
		RegisteredOn: date, Phone: phone, LifecycleNote: lifecycle, DeviceNote: "来源表第" + strconv.Itoa(row.Number) + "行手机编号，实物与登录待核对"}
}

func (p *officePlanner) planLayouts(ctx context.Context, tx *sql.Tx, computers []string, progress map[string]officeSourceRow,
	localAccounts map[string][]string, accountCounts, progressCounts map[string]int) error {
	type coordinate struct {
		computer string
		slot     int
	}
	type cell struct{ account, label string }
	existing := map[coordinate]cell{}
	placed := map[string]bool{}
	if p.plan.Batch != "" {
		rows, err := tx.QueryContext(ctx, `SELECT computer_id,slot,COALESCE(account_id,''),label FROM publishing_layout WHERE batch=$1`, p.plan.Batch)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var key coordinate
			var value cell
			if err := rows.Scan(&key.computer, &key.slot, &value.account, &value.label); err != nil {
				return err
			}
			existing[key] = value
			if value.account != "" {
				placed[value.account] = true
			}
		}
		if err := rows.Err(); err != nil {
			return err
		}
	}
	for _, id := range computers {
		row := progress[id]
		for slot := 1; slot <= 10; slot++ {
			slotText := strconv.Itoa(slot)
			label := row.Fields["发布设备"+slotText]
			switch label {
			case "", ".", "。", "-", "—":
				continue
			}
			if p.plan.Batch == "" {
				p.item("layout", row.Number, "conflict", label, "当前没有启用的发布批次；不会创建或切换全局批次")
				continue
			}
			if !p.availablePC[id] {
				p.item("layout", row.Number, "conflict", label, "发布电脑归属冲突，需先人工核对电脑")
				continue
			}
			handle := officeLabelHandle(label)
			matches := localAccounts[handle]
			var candidate, reason string
			switch {
			case handle == "":
				reason = "未标明完整抖音号，请人工核对原文"
			case accountCounts[handle] > 1 || len(matches) > 1:
				reason = "真实抖音号对应多条账号资料，请人工核对"
			case progressCounts[handle] > 1:
				reason = "工作进度表中此抖音号有多个发布位置，请人工确认一个位置"
			case len(matches) != 1:
				reason = "未找到明确属于本办公室的唯一账号，请人工核对"
			default:
				candidate = matches[0]
			}
			if old, found := existing[coordinate{id, slot}]; found {
				if old.label == label && (old.account == "" || old.account == candidate || candidate == "") {
					p.item("layout", row.Number, "reuse", label, "原发布位置已有相同来源文字，保留现有处理")
				} else {
					p.item("layout", row.Number, "conflict", label, "该位置已有发布行，请人工核对原安排")
				}
				continue
			}
			if placed[candidate] {
				candidate = ""
				reason = "该账号已有发布位置，请人工核对安排"
			}
			if candidate == "" {
				p.item("layout", row.Number, "pending", label, reason)
			} else {
				p.item("layout", row.Number, "create", label, "完整抖音号唯一匹配，新增并关联发布格")
				placed[candidate] = true
			}
			p.plan.Layouts = append(p.plan.Layouts, officePlanLayout{ComputerID: id, Slot: slot, AccountID: candidate, Label: label,
				SourceCell: "导入行" + strconv.Itoa(row.Number) + "/设备" + slotText, Reason: reason})
		}
	}
	return nil
}

// OfficeThreePreview builds an actor-bound plan from only the safe columns.
func OfficeThreePreview(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	if err := authorizeOfficeDual(ctx, tx, actor, body); err != nil {
		return nil, err
	}
	accounts, progress, err := officeDualSources(body)
	if err != nil {
		return nil, err
	}
	plan, items, warnings, suggestions, err := planOfficeThree(ctx, tx, accounts, progress)
	if err != nil {
		return nil, err
	}
	plan.Guard, _, err = officeDualGuard(ctx, tx, nil)
	if err != nil {
		return nil, err
	}
	previewID, err := storePreview(ctx, tx, actor.ID, officeDualKind, plan)
	if err != nil {
		return nil, err
	}
	return map[string]any{"preview_id": previewID, "office_id": officeThreeID, "summary": plan.Summary, "rows": items, "warnings": warnings,
		"confirmation": officeDualConfirmation, "scope_candidates": plan.ScopeCandidates, "missing_nickname_suggestions": suggestions,
		"missing_nickname_suggestion_count": len(suggestions)}, nil
}

func authorizeOfficeDual(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) error {
	if err := AuthorizeOfficeThree(ctx, tx, actor, body); err != nil {
		return err
	}
	if mode := body["mode"]; mode != nil && mode != "" {
		return accountwrite.Invalid("导入方式不正确，请重新预览")
	}
	return nil
}

type officeExclusion struct {
	where string
	args  []any
}

// The same fingerprint is used before and after writing. On verification, only
// exact known new rows are excluded. Any changed, deleted or unrelated inserted
// business row still changes the fingerprint and rolls back the whole action.
func officeDualGuard(ctx context.Context, tx *sql.Tx, exclusions map[string]officeExclusion) (string, int64, error) {
	sums := map[string]string{}
	var records int64
	for i, table := range officeGuardTables {
		var present bool
		if err := tx.QueryRowContext(ctx, `SELECT to_regclass($1) IS NOT NULL`, "board_candidate."+table).Scan(&present); err != nil {
			return "", 0, err
		}
		if !present {
			continue
		}
		excluded := exclusions[table]
		statement := `SELECT encode(sha256(convert_to(COALESCE(jsonb_agg(value ORDER BY value),'[]'::jsonb)::text,'UTF8')),'hex'),count(*)
			FROM (SELECT to_jsonb(r) AS value FROM board_candidate.` + table + ` r ` + excluded.where + `) AS original`
		var sum string
		var count int64
		if err := tx.QueryRowContext(ctx, statement, excluded.args...).Scan(&sum, &count); err != nil {
			return "", 0, err
		}
		if i < 12 {
			records += count
		}
		sums[table] = sum
	}
	version, err := recordwire.Version(sums)
	return version, records, err
}

func takeOfficeDual(ctx context.Context, tx *sql.Tx, actor session.User, ident string) (officePlan, int64, error) {
	var plan officePlan
	if err := readPreview(ctx, tx, actor.ID, officeDualKind, ident, &plan); err != nil {
		return plan, 0, err
	}
	if plan.LayoutRule != officeLayoutRule {
		return plan, 0, accountwrite.Conflict("发布格匹配规则已更新，请重新预览；未保存任何修改")
	}
	guard, records, err := officeDualGuard(ctx, tx, nil)
	if err != nil {
		return plan, 0, err
	}
	if plan.Office != officeThreeID || plan.Guard != guard {
		return plan, 0, accountwrite.Conflict("预览后资料或归属已变化，请重新预览；未保存任何修改")
	}
	return plan, records, nil
}

func officeSavepoint(ctx context.Context, tx *sql.Tx, perform func() error) error {
	if _, err := tx.ExecContext(ctx, "SAVEPOINT office_three_preserve"); err != nil {
		return err
	}
	if err := perform(); err != nil {
		if _, rollbackErr := tx.ExecContext(ctx, "ROLLBACK TO SAVEPOINT office_three_preserve"); rollbackErr != nil {
			return rollbackErr
		}
		_, _ = tx.ExecContext(ctx, "RELEASE SAVEPOINT office_three_preserve")
		return err
	}
	_, err := tx.ExecContext(ctx, "RELEASE SAVEPOINT office_three_preserve")
	return err
}

// OfficeThreeAssignComputers assigns only the selected unassigned source PCs
// from the existing plan. It consumes the plan without importing other records.
func OfficeThreeAssignComputers(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	if err := authorizeOfficeDual(ctx, tx, actor, body); err != nil {
		return nil, err
	}
	if body["confirmation"] != officeScopeConfirmation {
		return nil, accountwrite.Invalid("请明确输入“仅分配电脑到办公室三”后确认")
	}
	ids, valid := stringList(body["computer_ids"])
	if !valid || len(ids) == 0 || len(ids) > 100 {
		return nil, accountwrite.Invalid("请选择1至100台不重复的来源电脑")
	}
	ids = slices.Clone(ids)
	slices.Sort(ids)
	for i, id := range ids {
		if !officeSourceComputerID.MatchString(id) || (i > 0 && ids[i-1] == id) {
			return nil, accountwrite.Invalid("请选择1至100台不重复的来源电脑")
		}
	}
	ident := String(body["preview_id"])
	plan, _, err := takeOfficeDual(ctx, tx, actor, ident)
	if err != nil {
		return nil, err
	}
	for _, id := range ids {
		if !slices.Contains(plan.ScopeCandidates, officeScopeCandidate{id}) {
			return nil, accountwrite.Invalid("只能确认本次预览列出的尚未分配来源电脑，请重新预览")
		}
	}
	err = officeSavepoint(ctx, tx, func() error {
		for _, id := range ids {
			outcome, err := tx.ExecContext(ctx, `INSERT INTO computer_offices(computer_id,office_id)
				SELECT c.id,$2 FROM computers c WHERE c.id=$1
				AND NOT EXISTS (SELECT 1 FROM computer_offices m WHERE m.computer_id=c.id)`, id, officeThreeID)
			if err != nil {
				return err
			}
			if count, err := outcome.RowsAffected(); err != nil || count != 1 {
				return accountwrite.Conflict("所选电脑已经有办公室归属，请重新预览；未移动任何电脑")
			}
		}
		exclusions := map[string]officeExclusion{"computer_offices": scopedOfficeExclusion("computer_id", ids)}
		guard, _, err := officeDualGuard(ctx, tx, exclusions)
		if err != nil {
			return err
		}
		if guard != plan.Guard {
			return accountwrite.Conflict("电脑归属保留校验失败：并非仅分配所选电脑，整次操作已撤销")
		}
		return finishOfficePlan(ctx, tx, actor, ident, officeScopePath)
	})
	if err != nil {
		return nil, err
	}
	return map[string]any{"ok": true, "office_id": officeThreeID, "assigned_computer_ids": ids,
		"verification":         map[string]bool{"business_fields_preserved": true, "only_requested_computers_assigned": true, "other_entity_scopes_preserved": true},
		"requires_new_preview": true}, nil
}

func scopedOfficeExclusion(column string, ids []string) officeExclusion {
	return officeExclusion{"WHERE NOT (r.office_id=$2 AND r." + column + "=ANY($1))", []any{ids, officeThreeID}}
}

func rowOfficeExclusion(column string, ids []string) officeExclusion {
	return officeExclusion{"WHERE NOT (r." + column + "=ANY($1))", []any{ids}}
}

func finishOfficePlan(ctx context.Context, tx *sql.Tx, actor session.User, preview, path string) error {
	if _, err := tx.ExecContext(ctx, `DELETE FROM operations_previews WHERE id=$1 AND actor_id=$2`, preview, actor.ID); err != nil {
		return err
	}
	if err := accountwrite.Audit(ctx, tx, actor, path, ""); err != nil {
		return err
	}
	return noteReload(ctx, tx, actor, path)
}

// OfficeThreeApply inserts only rows authorized by the original preview.
func OfficeThreeApply(ctx context.Context, tx *sql.Tx, actor session.User, body map[string]any) (map[string]any, error) {
	if err := authorizeOfficeDual(ctx, tx, actor, body); err != nil {
		return nil, err
	}
	if body["confirmation"] != officeDualConfirmation {
		return nil, accountwrite.Invalid("请明确输入“导入办公室三”后确认，仅新增安全资料")
	}
	ident := String(body["preview_id"])
	plan, count, err := takeOfficeDual(ctx, tx, actor, ident)
	if err != nil {
		return nil, err
	}
	versions := map[string]any{}
	err = officeSavepoint(ctx, tx, func() error {
		if err := executeOfficePlan(ctx, tx, actor, plan, versions); err != nil {
			return err
		}
		return finishOfficePlan(ctx, tx, actor, ident, officeDualPath)
	})
	if err != nil {
		return nil, err
	}
	return map[string]any{"ok": true, "office_id": officeThreeID, "summary": plan.Summary, "verification": map[string]any{
		"original_records_preserved": true, "other_offices_unchanged": true, "active_batch_unchanged": true, "original_record_count": count,
	}, "imported": len(plan.Accounts) + len(plan.Computers) + len(plan.Devices) + len(plan.Layouts), "_record_versions": versions}, nil
}

func executeOfficePlan(ctx context.Context, tx *sql.Tx, actor session.User, plan officePlan, versions map[string]any) error {
	group, err := accountwrite.NewGroup()
	if err != nil {
		return err
	}
	exclusions := map[string]officeExclusion{}
	assets := []string{}
	for _, item := range plan.Computers {
		if _, err := tx.ExecContext(ctx, `INSERT INTO computers(id,name,shadow_account,ip,operator,category)
			VALUES($1,$2,'','',$3,$4)`, item.ID, item.Name, item.Operator, item.Category); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO computer_offices(computer_id,office_id) VALUES($1,$2)`, item.ID, officeThreeID); err != nil {
			return err
		}
		if err := recordOfficeInsert(ctx, tx, actor, group, "computer:"+item.ID, "computers", "r.id=$1", versions, item.ID); err != nil {
			return err
		}
		assets = append(assets, item.ID)
	}
	if len(assets) > 0 {
		exclusions["computers"] = rowOfficeExclusion("id", assets)
		exclusions["computer_offices"] = scopedOfficeExclusion("computer_id", assets)
	}
	units, devices := []string{}, []string{}
	deviceCtx := accountwrite.WithHistoryGroup(ctx, officeDualPath, group)
	for _, item := range plan.Devices {
		result, err := inventory.SaveDevice(deviceCtx, tx, actor, map[string]any{"id": item.ID, "name": item.Name,
			"state": "待确认", "office_ids": []string{officeThreeID}, "note": "办公室三来源表编号，实物与登录情况待核对"}, true)
		if err != nil {
			return err
		}
		recordVersions, _ := result["_record_versions"].(map[string]any)
		for key, value := range recordVersions {
			versions[key] = value
			if strings.HasPrefix(key, "unit:") {
				units = append(units, strings.TrimPrefix(key, "unit:"))
			}
		}
		devices = append(devices, item.ID)
	}
	if len(devices) > 0 {
		exclusions["phone_devices"] = rowOfficeExclusion("id", devices)
		exclusions["phone_device_offices"] = scopedOfficeExclusion("device_id", devices)
	}
	if len(units) > 0 {
		exclusions["phone_units"] = rowOfficeExclusion("id", units)
	}
	workflow, err := officeWorkflowAvailable(ctx, tx)
	if err != nil {
		return err
	}
	accounts, nurture := []string{}, []string{}
	day, _ := time.Parse(time.DateOnly, today())
	for _, item := range plan.Accounts {
		if _, err := tx.ExecContext(ctx, `INSERT INTO accounts(id,computer_id,slot,name,status,note,douyin_id,homepage,last_collected,
			registered_on,registration_phone,lifecycle_note,phone_device_id,phone_device_source,phone_device_note,office_group)
			VALUES($1,$2,$3,$4,$5,$6,$7,'','',$8,$9,$10,$11,'tencent-office3-preview',$12,'three')`,
			item.ID, item.ComputerID, item.Slot, item.Name, item.Status, item.Note, item.Handle, item.RegisteredOn, item.Phone,
			item.LifecycleNote, item.DeviceID, item.DeviceNote); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO account_offices(account_id,office_id) VALUES($1,$2)`, item.ID, officeThreeID); err != nil {
			return err
		}
		if err := recordOfficeInsert(ctx, tx, actor, group, "account:"+item.ID, "accounts", "r.id=$1", versions, item.ID); err != nil {
			return err
		}
		if workflow {
			// The source's phone label establishes a registration link only.
			// The physical last-unit and login confirmation remain unknown.
			if _, err := tx.ExecContext(ctx, `INSERT INTO phone_account_access(account_id,reason)
				VALUES($1,'来源表手机编号仅为资料登记，实物与登录待核对')`, item.ID); err != nil {
				return err
			}
			if err := recordOfficeInsert(ctx, tx, actor, group, "access:"+item.ID, "phone_account_access", "r.account_id=$1", versions, item.ID); err != nil {
				return err
			}
		}
		registered, err := time.Parse(time.DateOnly, item.RegisteredOn)
		recent := err == nil && !registered.After(day) && registered.After(day.AddDate(0, 0, -7))
		if item.Status == "养号7天" || recent {
			if _, err := tx.ExecContext(ctx, `INSERT INTO nurture_records(account_id,legacy_started_on) VALUES($1,$2)`,
				item.ID, item.RegisteredOn); err != nil {
				return err
			}
			if err := recordOfficeInsert(ctx, tx, actor, group, "nurture:"+item.ID, "nurture_records", "r.account_id=$1", versions, item.ID); err != nil {
				return err
			}
			nurture = append(nurture, item.ID)
		}
		accounts = append(accounts, item.ID)
	}
	if len(accounts) > 0 {
		exclusions["accounts"] = rowOfficeExclusion("id", accounts)
		exclusions["account_offices"] = scopedOfficeExclusion("account_id", accounts)
		if workflow {
			exclusions["phone_account_access"] = rowOfficeExclusion("account_id", accounts)
		}
	}
	if len(nurture) > 0 {
		exclusions["nurture_records"] = rowOfficeExclusion("account_id", nurture)
	}
	cells := []string{}
	for _, item := range plan.Layouts {
		if _, err := tx.ExecContext(ctx, `INSERT INTO publishing_layout(batch,computer_id,slot,account_id,label,office,source_cell,source_url,reason)
			VALUES($1,$2,$3,NULLIF($4,''),$5,$6,$7,$8,$9)`, plan.Batch, item.ComputerID, item.Slot, item.AccountID, item.Label,
			officeThreeID, item.SourceCell, officeSourceURL, item.Reason); err != nil {
			return err
		}
		cell := item.ComputerID + ":" + strconv.Itoa(item.Slot)
		if err := recordOfficeInsert(ctx, tx, actor, group, "layout:"+plan.Batch+":"+cell, "publishing_layout",
			"r.batch=$1 AND r.computer_id=$2 AND r.slot=$3", versions, plan.Batch, item.ComputerID, item.Slot); err != nil {
			return err
		}
		cells = append(cells, cell)
	}
	if len(cells) > 0 {
		exclusions["publishing_layout"] = officeExclusion{"WHERE NOT (r.batch=$2 AND (r.computer_id||':'||r.slot::text)=ANY($1))", []any{cells, plan.Batch}}
		if err := verifyOfficeLayouts(ctx, tx, plan.Batch, plan.Layouts); err != nil {
			return err
		}
	}
	if err := verifyOfficeAdditions(ctx, tx, assets, devices, units, accounts, workflow); err != nil {
		return err
	}
	guard, _, err := officeDualGuard(ctx, tx, exclusions)
	if err != nil {
		return err
	}
	if guard != plan.Guard {
		return accountwrite.Conflict("导入保留校验失败：原资料、其他办公室或发布批次变化，整次导入已撤销")
	}
	return nil
}

func recordOfficeInsert(ctx context.Context, tx *sql.Tx, actor session.User, group, key, table, where string, versions map[string]any, params ...any) error {
	// The relation and predicate are fixed by the distinct insertion paths.
	var raw string
	if err := tx.QueryRowContext(ctx, `SELECT to_json(r)::text FROM board_candidate.`+table+` r WHERE `+where, params...).Scan(&raw); err != nil {
		return err
	}
	row, err := decodeRecord(raw)
	if err != nil {
		return err
	}
	row["office_ids"] = []string{officeThreeID}
	token, err := recordwire.Version(row)
	if err != nil {
		return err
	}
	versions[key] = token
	return accountwrite.InsertHistory(ctx, tx, actor, officeDualPath, group, key, nil, row, []string{officeThreeID})
}

func officeWorkflowAvailable(ctx context.Context, tx *sql.Tx) (bool, error) {
	const query = `SELECT to_regclass('board_candidate.phone_units') IS NOT NULL
		AND to_regclass('board_candidate.phone_card_placements') IS NOT NULL
		AND to_regclass('board_candidate.phone_account_access') IS NOT NULL
		AND to_regclass('board_candidate.device_cases') IS NOT NULL
		AND to_regclass('board_candidate.device_workflow_events') IS NOT NULL`
	var enabled bool
	err := tx.QueryRowContext(ctx, query).Scan(&enabled)
	return enabled, err
}

func verifyOfficeAdditions(ctx context.Context, tx *sql.Tx, computers, devices, units, accounts []string, workflow bool) error {
	for _, entry := range []struct {
		table, column string
		ids           []string
	}{{"computer_offices", "computer_id", computers}, {"phone_device_offices", "device_id", devices},
		{"account_offices", "account_id", accounts}} {
		if len(entry.ids) == 0 {
			continue
		}
		var valid bool
		stmt := `SELECT count(*)=$3 AND count(*) FILTER(WHERE office_id=$2)=$3 FROM ` + entry.table + ` WHERE ` + entry.column + `=ANY($1)`
		if err := tx.QueryRowContext(ctx, stmt, entry.ids, officeThreeID, len(entry.ids)).Scan(&valid); err != nil {
			return err
		}
		if !valid {
			return accountwrite.Conflict("新增资料不属于办公室三，整次导入已撤销")
		}
	}
	const checkAccountLinks = `SELECT count(*)=$3 AND count(*) FILTER(WHERE
		(SELECT array_agg(m.office_id) FROM computer_offices m WHERE m.computer_id=a.computer_id)=ARRAY[$2::text] AND
		(SELECT array_agg(m.office_id) FROM phone_device_offices m WHERE m.device_id=a.phone_device_id)=ARRAY[$2::text]
		)=$3 FROM accounts a WHERE a.id=ANY($1)`
	const checkUnitLinks = `SELECT count(*)=$3 AND count(*) FILTER(WHERE
		(SELECT array_agg(m.office_id) FROM phone_device_offices m WHERE m.device_id=u.device_id)=ARRAY[$2::text] AND
		(SELECT array_agg(m.office_id) FROM phone_device_offices m WHERE m.device_id=u.original_device_id)=ARRAY[$2::text]
		)=$3 FROM phone_units u WHERE u.id=ANY($1)`
	const checkAccessLinks = `SELECT count(*)=$3 AND count(*) FILTER(WHERE
		a.last_unit_id IS NULL AND a.target_unit_id IS NULL AND a.case_id IS NULL AND a.confirmed_at=0 AND a.login_state='待确认'
		AND EXISTS (SELECT 1 FROM account_offices m WHERE m.account_id=a.account_id AND m.office_id=$2)
		)=$3 FROM phone_account_access a WHERE a.account_id=ANY($1)`
	type relationCheck struct {
		query string
		ids   []string
	}
	checks := []relationCheck{{checkAccountLinks, accounts}, {checkUnitLinks, units}}
	if workflow {
		checks = append(checks, relationCheck{checkAccessLinks, accounts})
	}
	for _, check := range checks {
		if len(check.ids) == 0 {
			continue
		}
		var valid bool
		if err := tx.QueryRowContext(ctx, check.query, check.ids, officeThreeID, len(check.ids)).Scan(&valid); err != nil {
			return err
		}
		if !valid {
			return accountwrite.Conflict("新增账号与电脑、手机或登录记录的关联校验失败，整次导入已撤销")
		}
	}
	return nil
}

func verifyOfficeLayouts(ctx context.Context, tx *sql.Tx, batch string, layouts []officePlanLayout) error {
	computers, accounts, labels, reasons := []string{}, []string{}, []string{}, []string{}
	slots := []int{}
	for _, item := range layouts {
		computers = append(computers, item.ComputerID)
		slots = append(slots, item.Slot)
		accounts = append(accounts, item.AccountID)
		labels = append(labels, item.Label)
		reasons = append(reasons, item.Reason)
	}
	const query = `SELECT count(*)=$3 FROM unnest($4::text[],$5::bigint[],$6::text[],$7::text[],$8::text[])
		AS expected(computer_id,slot,account_id,label,reason)
		JOIN publishing_layout p ON p.batch=$1 AND p.computer_id=expected.computer_id AND p.slot=expected.slot
		WHERE p.office=$2 AND COALESCE(p.account_id,'')=expected.account_id AND p.label=expected.label AND p.reason=expected.reason
		AND (SELECT array_agg(m.office_id) FROM computer_offices m WHERE m.computer_id=p.computer_id)=ARRAY[$2::text]
		AND (p.account_id IS NULL OR (
			(SELECT array_agg(m.office_id) FROM account_offices m WHERE m.account_id=p.account_id)=ARRAY[$2::text]
			AND NOT EXISTS(SELECT 1 FROM publishing_layout other WHERE other.batch=p.batch AND other.account_id=p.account_id
				AND (other.computer_id,other.slot)<>(p.computer_id,p.slot))))`
	var valid bool
	if err := tx.QueryRowContext(ctx, query, batch, officeThreeID, len(layouts), computers, slots, accounts, labels, reasons).Scan(&valid); err != nil {
		return err
	}
	if !valid {
		return accountwrite.Conflict("新增发布格与预览的账号、位置或归属不一致，整次导入已撤销")
	}
	return nil
}
