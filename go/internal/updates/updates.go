// Package updates serves the workbench's durable, office-scoped change feed.
package updates

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"slices"
	"strconv"
	"strings"
	"time"

	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

type Service struct {
	DB       *sql.DB
	Fallback http.Handler
}

type query struct {
	cursor int64
	limit  int
	wait   time.Duration
}

type response struct {
	Cursor        int64     `json:"cursor"`
	Changes       *[]change `json:"changes,omitempty"`
	Reload        bool      `json:"reload,omitempty"`
	WaitSupported bool      `json:"wait_supported,omitempty"`
}

type change struct {
	Cursor        int64          `json:"cursor"`
	Entity        string         `json:"entity"`
	Kind          string         `json:"kind"`
	After         map[string]any `json:"after"`
	RecordVersion *string        `json:"record_version"`
	Offices       []string       `json:"offices"`
}

type badQuery string

func (err badQuery) Error() string { return string(err) }

var errScopeRefresh = errors.New("related offices require a scoped snapshot")

func firstValue(values url.Values, key string) string {
	for _, value := range values[key] {
		if value != "" {
			return value
		}
	}
	return ""
}

func parseQuery(values url.Values) (query, error) {
	// The HTTP contract uses PostgreSQL's nonnegative BIGINT cursor.
	cursor, err := strconv.ParseInt(strings.TrimSpace(firstValue(values, "cursor")), 10, 64)
	if err != nil || cursor < 0 {
		return query{}, badQuery("同步记号不正确")
	}
	result := query{cursor: cursor}
	if raw := firstValue(values, "limit"); raw != "" {
		result.limit, err = strconv.Atoi(strings.TrimSpace(raw))
		if err != nil || result.limit < 1 || result.limit > 500 {
			return query{}, badQuery("同步读取上限必须是1至500的整数")
		}
	}
	if raw := firstValue(values, "wait"); raw != "" {
		wait, err := strconv.Atoi(strings.TrimSpace(raw))
		if err != nil || wait < 0 || wait > 5 {
			return query{}, badQuery("同步等待时间必须是0至5的整数")
		}
		result.wait = time.Duration(wait) * time.Second
	}
	return result, nil
}

func (s Service) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		reply(w, http.StatusMethodNotAllowed, map[string]string{"error": "不支持的请求方法"})
		return
	}
	values := r.URL.Query()
	if firstValue(values, "cursor") == "" {
		if s.Fallback != nil {
			s.Fallback.ServeHTTP(w, r)
			return
		}
		s.version(w, r)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()
	input, inputErr := parseQuery(values)
	found, err := s.read(ctx, r, input, inputErr)
	if err == nil && input.wait > 0 {
		found, err = s.wait(ctx, r, input, found)
		found.WaitSupported = true
	}
	var invalid badQuery
	switch {
	case errors.Is(err, session.ErrUnauthorized):
		reply(w, http.StatusUnauthorized, map[string]string{"error": session.ErrUnauthorized.Error()})
	case errors.As(err, &invalid):
		reply(w, http.StatusBadRequest, map[string]string{"error": invalid.Error()})
	case err != nil:
		reply(w, http.StatusServiceUnavailable, map[string]string{"error": "暂时无法读取同步更新"})
	default:
		reply(w, http.StatusOK, found)
	}
}

func (s Service) version(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	tx, err := s.DB.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
	if err == nil {
		defer tx.Rollback()
		_, err = session.AuthenticateWith(ctx, tx, r)
	}
	var cursor int64
	if err == nil {
		err = tx.QueryRowContext(ctx, "SELECT COALESCE(MAX(id),0) FROM board_candidate.operations_history").Scan(&cursor)
	}
	switch {
	case errors.Is(err, session.ErrUnauthorized):
		reply(w, http.StatusUnauthorized, map[string]string{"error": session.ErrUnauthorized.Error()})
	case err != nil:
		reply(w, http.StatusServiceUnavailable, map[string]string{"error": "暂时无法读取同步更新"})
	default:
		reply(w, http.StatusOK, map[string]any{"version": "go:" + strconv.FormatInt(cursor, 10), "reload": true})
	}
}

func (s Service) read(ctx context.Context, r *http.Request, input query, inputErr error) (response, error) {
	tx, err := s.DB.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
	if err != nil {
		return response{}, err
	}
	defer tx.Rollback()
	actor, err := session.AuthenticateWith(ctx, tx, r)
	if err != nil {
		return response{}, err
	}
	if inputErr != nil {
		return response{}, inputErr
	}
	return load(ctx, tx, input, actor)
}

func (s Service) wait(ctx context.Context, r *http.Request, input query, found response) (response, error) {
	if found.Reload || found.Cursor != input.cursor {
		return found, nil
	}
	// Changes may be committed by the Python or Go service. Re-read briefly
	// between idle intervals without reserving a database connection.
	deadline := time.Now().Add(input.wait)
	for {
		delay := min(time.Second, time.Until(deadline))
		timer := time.NewTimer(delay)
		select {
		case <-ctx.Done():
			timer.Stop()
			return response{}, ctx.Err()
		case <-timer.C:
		}
		var err error
		found, err = s.read(ctx, r, input, nil)
		if err != nil || found.Reload || found.Cursor != input.cursor || !time.Now().Before(deadline) {
			return found, err
		}
	}
}

func load(ctx context.Context, tx *sql.Tx, input query, actor session.User) (response, error) {
	const snapshot = `
		SELECT COALESCE(MAX(id), 0),
		       ($1 = 0 OR EXISTS(SELECT 1 FROM board_candidate.operations_history WHERE id=$1))
		FROM board_candidate.operations_history`
	var latest int64
	var known bool
	if err := tx.QueryRowContext(ctx, snapshot, input.cursor).Scan(&latest, &known); err != nil {
		return response{}, err
	}
	if !known {
		// Existing clients require a nondecreasing update cursor before they
		// apply reload. The following full board read supplies the restored cursor.
		return response{Cursor: max(latest, input.cursor), Reload: true}, nil
	}
	changes := []change{}
	found := response{Cursor: input.cursor, Changes: &changes}
	if latest == input.cursor {
		return found, nil
	}
	statement := `SELECT id, entity, after_json, scope_offices
		FROM board_candidate.operations_history WHERE id>$1 ORDER BY id`
	args := []any{input.cursor}
	if input.limit != 0 {
		statement += " LIMIT $2"
		args = append(args, input.limit+1)
	}
	rows, err := tx.QueryContext(ctx, statement, args...)
	if err != nil {
		return response{}, err
	}
	defer rows.Close()
	for count := 1; rows.Next(); count++ {
		// The cap counts even changes that the current actor cannot see.
		if input.limit != 0 && count > input.limit {
			return response{Cursor: latest, Reload: true}, nil
		}
		if found.Reload {
			continue
		}
		var cursor int64
		var entity string
		var after, scope sql.NullString
		if err := rows.Scan(&cursor, &entity, &after, &scope); err != nil {
			return response{}, err
		}
		found.Cursor = cursor
		if entity == "sync:reload" {
			found = response{Cursor: cursor, Reload: true}
			if input.limit == 0 {
				return found, nil
			}
			continue
		}
		item, err := projectChange(actor, cursor, entity, after.String, scope.String)
		if errors.Is(err, errScopeRefresh) {
			return response{Cursor: latest, Reload: true}, nil
		}
		if err != nil {
			return response{}, err
		}
		if item != nil {
			changes = append(changes, *item)
		}
	}
	if err := rows.Err(); err != nil {
		return response{}, err
	}
	rows.Close()
	if !actor.Global() && !found.Reload {
		for i := range changes {
			if changes[i].After == nil {
				continue
			}
			safe, err := currentAccess(ctx, tx, actor, &changes[i])
			if err != nil {
				return response{}, err
			}
			if !safe {
				return response{Cursor: latest, Reload: true}, nil
			}
		}
	}
	return found, nil
}

// Legacy history entries contain the original unredacted record. If a related
// record moved since the entry was written, a scoped board read can still
// show the permissible data without exposing that older relationship.
func currentAccess(ctx context.Context, tx *sql.Tx, actor session.User, item *change) (bool, error) {
	type relation struct{ field, mapping, key string }
	account := relation{"account_id", "account_offices", "account_id"}
	computer := relation{"computer_id", "computer_offices", "computer_id"}
	device := relation{"phone_device_id", "phone_device_offices", "device_id"}
	parent := relation{"parent_account_id", "account_offices", "account_id"}
	kind, _, _ := strings.Cut(item.Entity, ":")
	var checks []relation
	switch kind {
	case "account":
		checks = []relation{{"id", account.mapping, account.key}, computer, device, parent}
	case "daily", "nurture", "account-source":
		checks = []relation{account}
	case "layout":
		checks = []relation{computer, account}
	case "card":
		checks = []relation{{"id", "phone_card_offices", "card_id"}}
	case "device":
		checks = []relation{{"id", device.mapping, device.key}}
	case "computer":
		checks = []relation{{"id", computer.mapping, computer.key}}
	}
	for _, ref := range checks {
		id := accountID(item.After[ref.field])
		if id == "" || ref.field == "computer_id" && kind == "account" && id == "unassigned" {
			continue
		}
		// The SQL identifiers come exclusively from the fixed cases above.
		query := "SELECT EXISTS(SELECT 1 FROM " + ref.mapping + " WHERE " + ref.key +
			"::text=$1 AND office_id=ANY($2::text[]))"
		var allowed bool
		if err := tx.QueryRowContext(ctx, query, id, actor.OfficeIDs).Scan(&allowed); err != nil {
			return false, err
		}
		if !allowed {
			return false, nil
		}
	}
	return true, nil
}

func projectChange(actor session.User, cursor int64, entity, after, scope string) (*change, error) {
	if entity == "sync:office" {
		return projectOfficeMove(actor, cursor, after)
	}
	offices := historyScope(scope)
	if !actor.Global() && !intersects(actor.OfficeIDs, offices) {
		return nil, nil
	}
	if !actor.Global() && slices.ContainsFunc(offices, func(office string) bool {
		return !slices.Contains(actor.OfficeIDs, office)
	}) {
		// A history row can carry relation fields from several offices. The
		// snapshot redacts them using current grants; a raw history row cannot.
		return nil, errScopeRefresh
	}
	value, err := decode(after)
	if err != nil {
		return nil, err
	}
	item := change{Cursor: cursor, Entity: entity, Kind: "change", Offices: offices}
	if value == nil {
		item.Kind = "remove"
	}
	item.After, _ = value.(map[string]any)
	return withVersion(item)
}

func projectOfficeMove(actor session.User, cursor int64, raw string) (*change, error) {
	value, err := decode(raw)
	if err != nil {
		return nil, err
	}
	move, _ := value.(map[string]any)
	account, _ := move["account"].(map[string]any)
	id := accountID(move["id"])
	if id == "" {
		id = accountID(account["id"])
	}
	if id == "" {
		return nil, nil
	}
	previous, next := officeSet(move["previous"]), officeSet(move["next"])
	item := change{Cursor: cursor, Entity: "account:" + id}
	if actor.Global() || intersects(actor.OfficeIDs, next) {
		if len(account) == 0 {
			return nil, nil
		}
		item.Kind, item.After, item.Offices = "change", account, next
		return withVersion(item)
	}
	if intersects(actor.OfficeIDs, previous) {
		item.Kind, item.Offices = "remove", previous
		return &item, nil
	}
	return nil, nil
}

func decode(raw string) (any, error) {
	if raw == "" {
		return nil, nil
	}
	var value any
	decoder := json.NewDecoder(strings.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return nil, errors.New("invalid history JSON")
	}
	return value, nil
}

func accountID(value any) string {
	switch id := value.(type) {
	case string:
		return id
	case json.Number:
		if id.String() != "0" {
			return id.String()
		}
	}
	return ""
}

func officeSet(value any) []string {
	list, _ := value.([]any)
	offices := []string{}
	for _, entry := range list {
		if office, ok := entry.(string); ok {
			offices = append(offices, office)
		}
	}
	slices.Sort(offices)
	return slices.Compact(offices)
}

func historyScope(raw string) []string {
	var list []*string
	if raw != "" && json.Unmarshal([]byte(raw), &list) == nil {
		offices := make([]string, 0, len(list))
		for _, office := range list {
			if office == nil {
				return []string{}
			}
			offices = append(offices, *office)
		}
		return offices
	}
	return []string{}
}

func intersects(left, right []string) bool {
	return slices.ContainsFunc(left, func(office string) bool { return slices.Contains(right, office) })
}

func withVersion(item change) (*change, error) {
	if item.After != nil {
		version, err := recordwire.Version(item.After)
		if err != nil {
			return nil, err
		}
		item.RecordVersion = &version
	}
	return &item, nil
}

func reply(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
