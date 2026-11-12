package account

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf16"
	"unicode/utf8"

	"douyinboard/internal/session"
)

var boardTimezone = time.FixedZone("Asia/Shanghai", 8*60*60)
var isoWeekDate = regexp.MustCompile(`^(?:([0-9]{4})-W([0-9]{2})(?:-([1-7]))?|([0-9]{4})W([0-9]{2})([1-7])?)$`)

func boardToday() time.Time {
	now := time.Now().In(boardTimezone)
	return time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, time.UTC)
}

func isoDate(value string) (time.Time, error) {
	var date time.Time
	var err error
	if len(value) == 8 {
		date, err = time.Parse("20060102", value)
	} else {
		date, err = time.Parse(time.DateOnly, value)
	}
	if err == nil && date.Year() > 0 {
		return date, nil
	}
	if match := isoWeekDate.FindStringSubmatch(value); match != nil {
		fields := match[1:4]
		if fields[0] == "" {
			fields = match[4:7]
		}
		year, _ := strconv.Atoi(fields[0])
		week, _ := strconv.Atoi(fields[1])
		weekday, _ := strconv.Atoi(fields[2])
		if year > 0 && week > 0 && week <= 53 {
			jan4 := time.Date(year, time.January, 4, 0, 0, 0, 0, time.UTC)
			monday := jan4.AddDate(0, 0, -(int(jan4.Weekday())+6)%7)
			date = monday.AddDate(0, 0, (week-1)*7+max(weekday, 1)-1)
			if y, w := date.ISOWeek(); y == year && w == week && date.Year() > 0 && date.Year() <= 9999 {
				return date, nil
			}
		}
	}
	return time.Time{}, errors.New("日期格式不正确")
}

func textField(row map[string]any, field string) string {
	value, _ := row[field].(string)
	return value
}

func readNurture(ctx context.Context, tx *sql.Tx, id string, offices []string) (map[string]any, error) {
	var raw []byte
	err := tx.QueryRowContext(ctx, `SELECT to_jsonb(n)::text FROM board_candidate.nurture_records n WHERE account_id=$1`, id).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	row, err := decodeRow(raw)
	if err != nil {
		return nil, err
	}
	row["office_ids"] = offices
	return row, nil
}

const (
	nurtureNoChange = iota
	nurtureCreate
	nurtureUpgrade
	nurturePause
)

func nurtureEffect(account, record map[string]any, day time.Time) (int, string) {
	started := textField(account, "nurture_started")
	if started == "" {
		started = textField(account, "registered_on")
	}
	when, dateErr := isoDate(started)
	status := textField(account, "status")
	if record == nil {
		if status == "养号7天" || dateErr == nil && !when.After(day) && when.After(day.AddDate(0, 0, -7)) {
			return nurtureCreate, started
		}
		return nurtureNoChange, ""
	}
	if textField(record, "completed_on") != "" {
		return nurtureNoChange, ""
	}
	workflow := textField(record, "workflow")
	if workflow == "待开始" && textField(record, "started_on") != "" {
		return nurtureUpgrade, ""
	}
	if workflow == "运行" && status != "正常" && status != "待确认" && status != "养号7天" {
		return nurturePause, ""
	}
	return nurtureNoChange, ""
}

// NicknameChangesNurture reports whether saving a different nickname would
// also change this authorized account's nurture data in the same transaction.
func NicknameChangesNurture(ctx context.Context, tx *sql.Tx, actor session.User, id string) (bool, error) {
	account, err := requireAccount(ctx, tx, actor, id)
	if err != nil {
		return false, err
	}
	offices, _ := account["office_ids"].([]string)
	record, err := readNurture(ctx, tx, id, offices)
	if err != nil {
		return false, err
	}
	action, _ := nurtureEffect(account, record, boardToday())
	return action != nurtureNoChange, nil
}

func syncNurture(ctx context.Context, tx *sql.Tx, id string, account, record map[string]any, day time.Time) error {
	action, started := nurtureEffect(account, record, day)
	switch action {
	case nurtureCreate:
		_, err := tx.ExecContext(ctx, `INSERT INTO board_candidate.nurture_records(account_id,legacy_started_on) VALUES($1,$2)`, id, started)
		return err
	case nurtureUpgrade:
		_, err := tx.ExecContext(ctx, `UPDATE board_candidate.nurture_records
SET legacy_started_on=started_on,started_on='',revision=revision+1 WHERE account_id=$1`, id)
		return err
	case nurturePause:
		if err := pauseNurture(ctx, tx, id, record, day); err != nil {
			return err
		}
		_, err := tx.ExecContext(ctx, `UPDATE board_candidate.nurture_records
SET verification_state='待检查',verification_updated_on=$1 WHERE account_id=$2`, day.Format(time.DateOnly), id)
		return err
	default:
		return nil
	}
}

func pauseNurture(ctx context.Context, tx *sql.Tx, id string, record map[string]any, day time.Time) error {
	active := textField(record, "active_since")
	today := day.Format(time.DateOnly)
	periods, _ := appendNurturePeriod(textField(record, "periods"), active, today, true)
	_, err := tx.ExecContext(ctx, `UPDATE board_candidate.nurture_records
SET banked_days=$1,periods=$2,active_since='',paused_on=$3,workflow='暂停',revision=revision+1 WHERE account_id=$4`, elapsedNurture(record, day), periods, today, id)
	return err
}

func elapsedNurture(record map[string]any, day time.Time) int64 {
	var banked int64
	if number, ok := record["banked_days"].(json.Number); ok {
		banked, _ = number.Int64()
	}
	active := textField(record, "active_since")
	if when, err := isoDate(active); err == nil && day.After(when) {
		banked += (day.Unix() - when.Unix()) / 86400
	}
	return banked
}

// Python stores periods as JSON text, including the default spaces and ASCII
// escapes. Preserve key order and formatting because the text is versioned.
func appendNurturePeriod(raw, start, end string, onlyObjects bool) (string, error) {
	var entries []json.RawMessage
	if err := json.Unmarshal([]byte(raw), &entries); err != nil || entries == nil {
		if !onlyObjects {
			return "", errors.New("invalid nurture periods")
		}
		entries = nil
	}
	parts := make([]string, 0, len(entries)+1)
	for _, entry := range entries {
		if !onlyObjects || entry[0] == '{' {
			parts = append(parts, pythonPeriodJSON(entry))
		}
	}
	parts = append(parts, fmt.Sprintf(`{"start": %s, "end": %s}`, pythonPeriodString(start), pythonPeriodString(end)))
	return "[" + strings.Join(parts, ", ") + "]", nil
}

func pythonPeriodJSON(raw []byte) string {
	var compact bytes.Buffer
	_ = json.Compact(&compact, raw)
	value := compact.Bytes()
	var result strings.Builder
	for i := 0; i < len(value); i++ {
		switch value[i] {
		case '"':
			start := i
			for i++; i < len(value) && value[i] != '"'; i++ {
				if value[i] == '\\' {
					i++
				}
			}
			var text string
			_ = json.Unmarshal(value[start:i+1], &text)
			result.WriteString(pythonPeriodString(text))
		case ',', ':':
			result.WriteByte(value[i])
			result.WriteByte(' ')
		default:
			result.WriteByte(value[i])
		}
	}
	return result.String()
}

func pythonPeriodString(value string) string {
	var encoded bytes.Buffer
	encoder := json.NewEncoder(&encoded)
	encoder.SetEscapeHTML(false)
	_ = encoder.Encode(value)
	var result strings.Builder
	for _, char := range strings.TrimSuffix(encoded.String(), "\n") {
		if char < utf8.RuneSelf-1 {
			result.WriteRune(char)
		} else if char <= 0xffff {
			fmt.Fprintf(&result, `\u%04x`, char)
		} else {
			first, second := utf16.EncodeRune(char)
			fmt.Fprintf(&result, `\u%04x\u%04x`, first, second)
		}
	}
	return result.String()
}
