package exchange

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"slices"
	"strconv"
	"strings"
	"unicode/utf8"

	"douyinboard/internal/accountwrite"
)

// ReadOfficeSources reads the two allowlisted source sheets for the v7
// synchronization flow. Identifiers are kept byte for byte, including spaces
// that a later identity validator may reject.
func ReadOfficeSources(body map[string]any) (accounts, progress []map[string]any, metadata map[string]any, err error) {
	rawRound, ok := body["source_round"].(string)
	round := strings.TrimSpace(rawRound)
	if !ok || round == "" || utf8.RuneCountInString(round) > 100 || strings.IndexByte(round, 0) >= 0 {
		err = accountwrite.Invalid("请填写100字以内的同轮来源标记")
		return
	}
	metadata = map[string]any{"round": round}
	result := [2][]map[string]any{}
	total := 0
	for i, name := range []string{"accounts", "progress"} {
		file, ok := body[name+"_file"].(map[string]any)
		if !ok || len(file) != 2 {
			err = accountwrite.Invalid("请同时上传同一轮的账号情况表与工作进度表")
			return
		}
		filename, encoded, fileErr := officeFile(file)
		if fileErr != nil {
			err = fileErr
			return
		}
		if len(encoded) > base64.StdEncoding.EncodedLen(maxFileSize) {
			err = accountwrite.Invalid("两份来源合计不能超过8MB")
			return
		}
		raw, decodeErr := base64.StdEncoding.Strict().DecodeString(encoded)
		if decodeErr != nil || strings.ContainsAny(encoded, "\r\n") {
			err = accountwrite.Invalid("来源文件内容无效")
			return
		}
		total += len(raw)
		if total > maxFileSize {
			err = accountwrite.Invalid("两份来源合计不能超过8MB")
			return
		}
		rows, readErr := readSheet(filename, encoded)
		if readErr != nil {
			err = accountwrite.Invalid(readErr.Error())
			return
		}
		result[i], err = readV7Rows(rows, i == 0)
		if err != nil {
			return
		}
		digest := sha256.Sum256(raw)
		metadata[name] = map[string]any{"sha256": hex.EncodeToString(digest[:]), "bytes": len(raw), "rows": len(result[i])}
	}
	return result[0], result[1], metadata, nil
}

func readV7Rows(rows [][]string, accounts bool) ([]map[string]any, error) {
	header := slices.Clone(rows[0])
	for i, value := range header {
		header[i] = strings.TrimSpace(value)
	}
	safe, columns, title := officeProgressHeaders, officeProgressColumns, "工作进度表"
	if accounts {
		safe, columns, title = officeAccountHeaders, officeAccountColumns, "账号情况表"
	}
	compact := len(header) >= len(safe) && slices.Equal(header[:len(safe)], safe)
	if !compact && !validOfficeRawHeader(header, accounts) {
		return nil, accountwrite.Invalid(title + "列结构不匹配，请使用安全模板或原始双表")
	}
	clean := []map[string]any{}
	for i, row := range rows[1:] {
		item := map[string]any{}
		hasValue := false
		for field, column := range columns {
			if compact {
				column = field + 1
			}
			key := safe[field+1]
			value := ""
			if column < len(row) {
				value = row[column]
			}
			hasValue = hasValue || value != ""
			if utf8.RuneCountInString(value) > 2000 || strings.HasPrefix(value, "=") ||
				strings.ContainsFunc(value, func(c rune) bool { return c < 32 && c != '\n' && c != '\t' }) {
				return nil, accountwrite.Invalid("允许导入的来源单元格过长或含不支持的公式/字符")
			}
			if key != "抖音号" {
				value = strings.TrimSpace(value)
			}
			item[key] = value
		}
		if !hasValue {
			continue
		}
		number := i + 2
		if compact && len(row) > 0 {
			if source := strings.TrimSpace(row[0]); source != "" {
				if !officeRowNumber.MatchString(source) {
					return nil, accountwrite.Invalid("来源行号格式不正确")
				}
				number, _ = strconv.Atoi(source)
			}
		}
		item["来源行"] = number
		clean = append(clean, item)
	}
	return clean, nil
}
