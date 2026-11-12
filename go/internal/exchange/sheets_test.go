package exchange

import (
	"archive/zip"
	"bytes"
	"encoding/base64"
	"reflect"
	"strings"
	"testing"

	"golang.org/x/text/encoding/simplifiedchinese"
)

func TestWorkbookRoundTripPreservesText(t *testing.T) {
	rows := [][]string{{"抖音号", "备注"}, {"000042", "表格 <>&\n新行"}, {"0089", "=作为文字导出"}}
	book, err := writeXLSX(rows)
	if err != nil {
		t.Fatal(err)
	}
	got, err := readSheet("sample.xlsx", base64.StdEncoding.EncodeToString(book))
	if err != nil || !reflect.DeepEqual(rows, got) {
		t.Fatalf("text and leading zeros did not round-trip: %v %v", got, err)
	}
}

func TestWorkbookRejectsFormulas(t *testing.T) {
	var output bytes.Buffer
	book := zip.NewWriter(&output)
	sheet, err := book.Create("xl/worksheets/sheet1.xml")
	if err != nil {
		t.Fatal(err)
	}
	_, _ = sheet.Write([]byte(`<worksheet><sheetData><row><c r="A1"><f>1+1</f><v>2</v></c></row></sheetData></worksheet>`))
	if err := book.Close(); err != nil {
		t.Fatal(err)
	}
	_, err = readSheet("sample.xlsx", base64.StdEncoding.EncodeToString(output.Bytes()))
	if err == nil || !strings.Contains(err.Error(), "公式") {
		t.Fatalf("workbook formula accepted: %v", err)
	}
}

func TestCSVRecognizesGB18030(t *testing.T) {
	raw, err := simplifiedchinese.GB18030.NewEncoder().Bytes([]byte("抖音号,昵称\r\n00042,测试\r\n"))
	if err != nil {
		t.Fatal(err)
	}
	rows, err := readSheet("sample.csv", base64.StdEncoding.EncodeToString(raw))
	if err != nil || rows[1][0] != "00042" || rows[1][1] != "测试" {
		t.Fatalf("legacy CSV text lost: %v %v", rows, err)
	}
}

func TestCSVRetainsBlankSourceRows(t *testing.T) {
	raw := []byte("抖音号,备注\r\n\r\n00042,\"第一行\n\n第四行\"\n\n0100,完成\n")
	rows, err := readSheet("sample.csv", base64.StdEncoding.EncodeToString(raw))
	want := [][]string{{"抖音号", "备注"}, {}, {"00042", "第一行\n\n第四行"}, {}, {"0100", "完成"}}
	if err != nil || !reflect.DeepEqual(rows, want) {
		t.Fatalf("source row positions were not preserved: %v %v", rows, err)
	}
}

func TestV7SourceAllowlistAndLiteralHandle(t *testing.T) {
	a := strings.Join(officeAccountHeaders, ",") + ",私有来源列\n2,,合成昵称,, 0042 ,,,,,,,,,,excluded-cell\n"
	p := strings.Join(officeProgressHeaders, ",") + "\n"
	file := func(value string) map[string]any {
		return map[string]any{"filename": "sample.csv", "content": base64.StdEncoding.EncodeToString([]byte(value))}
	}
	rows, progress, metadata, err := ReadOfficeSources(map[string]any{"source_round": "合成批次",
		"accounts_file": file(a), "progress_file": file(p)})
	if err != nil || len(rows) != 1 || len(progress) != 0 || rows[0]["抖音号"] != " 0042 " ||
		len(rows[0]) != len(officeAccountHeaders) || metadata["round"] != "合成批次" {
		t.Fatalf("source allowlist or literal identity mismatch: %v %v", rows, err)
	}
	if _, found := rows[0]["私有来源列"]; found {
		t.Fatal("unexpected private source column")
	}
	for _, value := range rows[0] {
		if value == "excluded-cell" {
			t.Fatal("private source cell leaked")
		}
	}
}
