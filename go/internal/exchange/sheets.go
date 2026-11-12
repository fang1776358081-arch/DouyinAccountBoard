package exchange

import (
	"archive/zip"
	"bytes"
	"encoding/base64"
	"encoding/csv"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"path"
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"

	"golang.org/x/text/encoding/simplifiedchinese"
)

const maxFileSize = 8 << 20
const maxExpandedSize = 40 << 20
const maxRows = 5001
const maxColumns = 100

var sheetName = regexp.MustCompile(`^xl/worksheets/sheet[0-9]+\.xml$`)

func readSheet(name, encoded string) ([][]string, error) {
	if len(encoded) > base64.StdEncoding.EncodedLen(maxFileSize) || strings.ContainsAny(encoded, "\r\n") {
		return nil, errors.New("文件不能超过8MB且须为有效文件")
	}
	raw, err := base64.StdEncoding.Strict().DecodeString(encoded)
	if err != nil {
		return nil, errors.New("文件内容无效")
	}
	if len(raw) > maxFileSize {
		return nil, errors.New("文件不能超过8MB")
	}
	var rows [][]string
	switch strings.ToLower(path.Ext(name)) {
	case ".csv":
		rows, err = readCSV(raw)
	case ".xlsx":
		rows, err = readXLSX(raw)
	default:
		return nil, errors.New("请选择.xlsx或.csv文件，旧版.xls请另存为.xlsx")
	}
	if err == nil && (len(rows) == 0 || len(rows) > maxRows) {
		err = errors.New("表格需有标题行，最多5000条资料")
	}
	return rows, err
}

func readCSV(raw []byte) ([][]string, error) {
	raw = bytes.TrimPrefix(raw, []byte{0xEF, 0xBB, 0xBF})
	if !utf8.Valid(raw) {
		var err error
		raw, err = simplifiedchinese.GB18030.NewDecoder().Bytes(raw)
		if err != nil {
			return nil, errors.New("无法读取 CSV 编码")
		}
	}
	reader := csv.NewReader(bytes.NewReader(raw))
	reader.FieldsPerRecord = -1
	rows := [][]string{}
	for len(rows) <= maxRows {
		start := reader.InputOffset()
		row, err := reader.Read()
		// encoding/csv ignores blank lines. Preserve them for preview/source
		// row numbers without confusing multiline quoted cells with blanks.
		for skipped := raw[start:reader.InputOffset()]; len(skipped) != 0; {
			switch {
			case bytes.HasPrefix(skipped, []byte("\r\n")):
				skipped = skipped[2:]
			case skipped[0] == '\n':
				skipped = skipped[1:]
			default:
				skipped = nil
				continue
			}
			if len(rows) == maxRows {
				return nil, errors.New("表格需有标题行，最多5000条资料")
			}
			rows = append(rows, []string{})
		}
		if err == io.EOF {
			return rows, nil
		}
		if err != nil {
			return nil, errors.New("无法读取 CSV 表格，请重新另存后上传")
		}
		if len(row) > maxColumns {
			return nil, errors.New("表格列数过多")
		}
		rows = append(rows, row)
	}
	return nil, errors.New("表格需有标题行，最多5000条资料")
}

type workbookCell struct {
	Ref     string       `xml:"r,attr"`
	Type    string       `xml:"t,attr"`
	Formula *string      `xml:"f"`
	Value   string       `xml:"v"`
	Inline  workbookText `xml:"is"`
}

type workbookText struct {
	Text []string `xml:"t"`
	Runs []string `xml:"r>t"`
}

func (item workbookText) String() string {
	return strings.Join(item.Text, "") + strings.Join(item.Runs, "")
}

func readXLSX(raw []byte) ([][]string, error) {
	corrupt := errors.New("表格损坏或格式不支持，请重新另存为.xlsx或UTF-8 CSV")
	book, err := zip.NewReader(bytes.NewReader(raw), int64(len(raw)))
	if err != nil {
		return nil, corrupt
	}
	var sheet, shared *zip.File
	var expanded uint64
	for _, file := range book.File {
		if file.UncompressedSize64 > maxExpandedSize-expanded {
			return nil, errors.New("表格解压后过大")
		}
		expanded += file.UncompressedSize64
		if sheetName.MatchString(file.Name) {
			if sheet != nil {
				return nil, errors.New("请仅保留一张需要导入的工作表，再上传")
			}
			sheet = file
		}
		if file.Name == "xl/sharedStrings.xml" {
			shared = file
		}
	}
	if sheet == nil {
		return nil, errors.New("请仅保留一张需要导入的工作表，再上传")
	}
	var stringsTable struct {
		Items []workbookText `xml:"si"`
	}
	if shared != nil {
		data, err := shared.Open()
		if err != nil {
			return nil, corrupt
		}
		err = xml.NewDecoder(io.LimitReader(data, maxExpandedSize+1)).Decode(&stringsTable)
		data.Close()
		if err != nil {
			return nil, corrupt
		}
	}
	data, err := sheet.Open()
	if err != nil {
		return nil, corrupt
	}
	defer data.Close()
	decoder := xml.NewDecoder(io.LimitReader(data, maxExpandedSize+1))
	rows := [][]string{}
	for {
		token, err := decoder.Token()
		if err == io.EOF {
			return rows, nil
		}
		if err != nil {
			return nil, corrupt
		}
		start, ok := token.(xml.StartElement)
		if !ok || start.Name.Local != "row" {
			continue
		}
		if len(rows) == maxRows {
			return nil, errors.New("表格需有标题行，最多5000条资料")
		}
		var source struct {
			Cells []workbookCell `xml:"c"`
		}
		if err := decoder.DecodeElement(&source, &start); err != nil {
			return nil, corrupt
		}
		values := []string{}
		for _, cell := range source.Cells {
			if cell.Formula != nil {
				return nil, errors.New("导入不接受公式，请先粘贴为值")
			}
			column := columnIndex(cell.Ref)
			if column < 0 || column >= maxColumns {
				return nil, errors.New("表格列数过多或列编号无效")
			}
			value := cell.Value
			switch cell.Type {
			case "s":
				index, err := strconv.Atoi(value)
				if err != nil || index < 0 || index >= len(stringsTable.Items) {
					return nil, corrupt
				}
				value = stringsTable.Items[index].String()
			case "inlineStr":
				value = cell.Inline.String()
			}
			if column >= len(values) {
				values = append(values, make([]string, column+1-len(values))...)
			}
			values[column] = value
		}
		rows = append(rows, values)
	}
}

func columnIndex(reference string) int {
	if reference == "" {
		return 0
	}
	index := 0
	for _, value := range reference {
		if value < 'A' || value > 'Z' {
			break
		}
		index = index*26 + int(value-'A'+1)
		if index > maxColumns {
			return -1
		}
	}
	return index - 1
}

func columnName(index int) string {
	label := ""
	for index++; index > 0; index = (index - 1) / 26 {
		label = string(rune('A'+(index-1)%26)) + label
	}
	return label
}

func writeCSV(rows [][]string) ([]byte, error) {
	var output bytes.Buffer
	output.Write([]byte{0xEF, 0xBB, 0xBF})
	writer := csv.NewWriter(&output)
	for _, row := range rows {
		safe := make([]string, len(row))
		for i, field := range row {
			if start := strings.TrimLeft(field, " \r\n\t"); start != "" && strings.ContainsAny(start[:1], "=+-@") {
				field = "'" + field
			}
			safe[i] = field
		}
		if err := writer.Write(safe); err != nil {
			return nil, err
		}
	}
	writer.Flush()
	return output.Bytes(), writer.Error()
}

func writeXLSX(rows [][]string) ([]byte, error) {
	var output bytes.Buffer
	book := zip.NewWriter(&output)
	const main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
	const office = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
	const relationships = "http://schemas.openxmlformats.org/package/2006/relationships"
	metadata := map[string]string{
		"[Content_Types].xml":        `<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>`,
		"_rels/.rels":                `<Relationships xmlns="` + relationships + `"><Relationship Id="rId1" Type="` + office + `/officeDocument" Target="xl/workbook.xml"/></Relationships>`,
		"xl/workbook.xml":            `<workbook xmlns="` + main + `" xmlns:r="` + office + `"><sheets><sheet name="工作台" sheetId="1" r:id="rId1"/></sheets></workbook>`,
		"xl/_rels/workbook.xml.rels": `<Relationships xmlns="` + relationships + `"><Relationship Id="rId1" Type="` + office + `/worksheet" Target="worksheets/sheet1.xml"/></Relationships>`,
	}
	for name, content := range metadata {
		file, err := book.Create(name)
		if err != nil {
			return nil, err
		}
		if _, err := io.WriteString(file, content); err != nil {
			return nil, err
		}
	}
	file, err := book.Create("xl/worksheets/sheet1.xml")
	if err != nil {
		return nil, err
	}
	var sheet bytes.Buffer
	sheet.WriteString(`<worksheet xmlns="` + main + `"><sheetData>`)
	for i, row := range rows {
		fmt.Fprintf(&sheet, `<row r="%d">`, i+1)
		for j, value := range row {
			fmt.Fprintf(&sheet, `<c r="%s%d" t="inlineStr"><is><t xml:space="preserve">`, columnName(j), i+1)
			if err := xml.EscapeText(&sheet, []byte(value)); err != nil {
				return nil, err
			}
			sheet.WriteString("</t></is></c>")
		}
		sheet.WriteString("</row>")
	}
	sheet.WriteString("</sheetData></worksheet>")
	if _, err := sheet.WriteTo(file); err != nil {
		return nil, err
	}
	if err := book.Close(); err != nil {
		return nil, err
	}
	return output.Bytes(), nil
}
