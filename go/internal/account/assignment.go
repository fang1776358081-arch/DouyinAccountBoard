package account

import (
	"context"
	"database/sql"
	"reflect"
	"slices"

	"douyinboard/internal/inventory"
	"douyinboard/internal/recordwire"
	"douyinboard/internal/session"
)

func (set *accountChangeSet) captureAssignment(ctx context.Context, tx *sql.Tx, target, previous string) error {
	if target == "" && previous == "" {
		return nil
	}
	id := set.id
	if set.before == nil {
		id = ""
	}
	records, err := inventory.AssignmentRecords(ctx, tx, id, target)
	if err != nil {
		return err
	}
	set.assignment, set.assignmentTarget = records, &target
	for key, row := range records {
		if row == nil {
			set.checked[key] = nil
			continue
		}
		version, err := recordwire.Version(row)
		if err != nil {
			return err
		}
		set.checked[key] = version
	}
	return nil
}

func (set accountChangeSet) recordAssignment(ctx context.Context, tx *sql.Tx, actor session.User, group string) error {
	if set.assignmentTarget == nil {
		return nil
	}
	next, err := inventory.RefreshAssignmentRecords(ctx, tx, set.id, textField(set.before, "phone_device_id"), *set.assignmentTarget, set.assignment)
	if err != nil {
		return err
	}
	for key, record := range next {
		previous := set.assignment[key]
		if reflect.DeepEqual(previous, record) {
			continue
		}
		scope := pairedScope(previous, record)
		if len(scope) != 1 {
			scope = nil
			if err := enqueueScopedReload(ctx, tx, group); err != nil {
				return err
			}
		}
		if err := set.record(ctx, tx, actor, group, key, previous, record, scope); err != nil {
			return err
		}
	}
	return nil
}

func pairedScope(previous, next map[string]any) []string {
	all := []string{}
	for _, record := range []map[string]any{previous, next} {
		if record == nil {
			continue
		}
		offices, _ := record["office_ids"].([]string)
		if len(offices) == 0 {
			return nil
		}
		all = append(all, offices...)
	}
	slices.Sort(all)
	return slices.Compact(all)
}
