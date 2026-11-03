"""Batch-link checks exclusively on the disposable, attested local PG cluster."""
import json
from uuid import uuid4
from unittest.mock import patch

import psycopg
from test_isolation import checked_root, install
from pg_synthetic_migration import connect_synthetic
from pg_service_adapter import ServiceConnection
from pg_schema import apply_migrations


def main():
    install(checked_root())
    import auth
    import employee_cards
    import operations
    day, checks = '2026-11-03', []
    with connect_synthetic() as connection:
        apply_migrations(connection)
        connection.execute('SET search_path=board_candidate,pg_catalog')
        control = connection.execute("UPDATE runtime_control SET mode='open' WHERE id=1 RETURNING writer_epoch,restore_generation").fetchone()
        db = ServiceConnection(connection, schema='board_candidate', scope_enabled=True,
                               writer_epoch=control[0], restore_generation=control[1])
        with connection.transaction():
            setup = auth.mint_setup(db)
            session = auth.setup_owner(db, dict(username='synthetic-batch-owner', name='合成PG批量管理员',
                password='Synthetic-only-test-2026'), 'board_setup=' + setup)
            actor = auth.session(db, 'board_session=' + session)
            cards = [db.execute('INSERT INTO phone_cards(phone,carrier,status,note) VALUES(?,?,?,?) RETURNING id',
                                ('1380000990' + str(index), '其他', '停用', '合成原资料必须保留')).fetchone()[0]
                     for index in (1, 2)]

        def preview(action, data, employee_id=''):
            version = employee_cards.get(db, actor, {'employee_id': [employee_id]} if employee_id else {}, day)['version']
            db.begin_write()
            try:
                value = employee_cards.preview(db, actor, dict(action=action, employee_id=employee_id,
                    data=data, expected_version=version), day)
                db.commit()
                return value
            except BaseException:
                db.rollback()
                raise

        def apply(preview_value, request_id=None):
            body = dict(preview_id=preview_value['preview_id'], request_id=request_id or str(uuid4()), confirmation='确认保存')
            db.begin_write()
            try:
                value = employee_cards.apply(db, actor, body, day)
                db.commit()
                return value
            except BaseException:
                db.rollback()
                raise

        def legacy():
            return {name: connection.execute('SELECT * FROM ' + name + ' ORDER BY 1').fetchall()
                    for name in ('phone_cards', 'accounts', 'auth_users', 'phone_card_offices', 'user_office_grants')}

        def counts():
            return tuple(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                         for table in ('employee_card_units', 'employee_card_links', 'employee_card_events'))

        people = [apply(preview('employee', dict(name='合成PG员工' + str(index), employee_no='PG00' + str(index),
                                               office_id='', employment_status='active')))['employee_id']
                  for index in (1, 2)]
        before_legacy, before_counts = legacy(), counts()
        data = dict(cards=[dict(card_id=card, operator_id=operator, activated_on='')
                           for card, operator in zip(cards, ('minsheng', 'changcheng'))], source='合成本人实名确认')
        batch = preview('card_link_batch', data, people[0])
        assert batch['counts'] == dict(total=2, new=2, existing=0, conflict=0)
        assert batch['can_apply'] and not batch['has_conflicts']
        assert legacy() == before_legacy and counts() == before_counts
        checks.append('real-pg-selected-card-preview-does-not-write-legacy-or-ledger')

        # Force a genuine PG unique violation in the second link insert, after
        # the unit group has been written. No partial batch may survive.
        request_id = str(uuid4())
        with patch.object(employee_cards, '_id', return_value=str(uuid4())):
            try:
                apply(batch, request_id)
                raise AssertionError('Synthetic duplicate link IDs unexpectedly accepted')
            except psycopg.errors.UniqueViolation:
                pass
        assert legacy() == before_legacy and counts() == before_counts
        checks.append('real-pg-executemany-constraint-failure-rolls-back-entire-batch')
        receipt = apply(batch, request_id)
        assert receipt['counts']['new'] == 2 and 'rows' not in receipt
        assert apply(batch, request_id) == receipt
        assert counts()[0] == before_counts[0] + 2 and counts()[1] == before_counts[1] + 2
        assert legacy() == before_legacy
        assert all(row[0] == '' for row in connection.execute('SELECT activated_on FROM employee_card_units'))
        checks.append('real-pg-confirm-retry-same-uuid-once-and-unknown-date-preserved')
        duplicate = preview('card_link_batch', data, people[0])
        assert duplicate['counts'] == dict(total=2, new=0, existing=2, conflict=0)
        current = counts()
        apply(duplicate)
        assert counts() == current
        checks.append('same-employee-repeat-preserves-all-original-associations')
        blocked = preview('card_link_batch', data, people[1])
        assert blocked['has_conflicts'] and not blocked['can_apply'] and blocked['counts']['conflict'] == 2
        try:
            apply(blocked)
            raise AssertionError('Conflicting batch unexpectedly accepted')
        except operations.ConflictError:
            pass
        assert counts() == current and legacy() == before_legacy
        checks.append('other-employee-conflict-blocks-whole-batch-without-legacy-mutation')

        # Exact-name views are derived on reads, including when the real
        # operator is unknown. No additional unit or historical link is made.
        auto_card = db.execute('INSERT INTO phone_cards(phone,owner,carrier,status) VALUES(?,?,?,?) RETURNING id',
                              ('13800009881', '合成PG员工1', '待确认', '已注销')).fetchone()[0]
        db.commit()
        initial_counts, initial_legacy = counts(), legacy()
        detail = employee_cards.get(db, actor, {'employee_id': [people[0]]}, day)
        assert detail['card_ledger']['registered'] == 3
        assert detail['card_ledger']['name_matched_count'] == 1
        derived = next(row for row in detail['linked_cards'] if row['card_id'] == auto_card)
        assert derived['association_source'] == 'name' and derived['operator_id'] == ''
        assert derived['ledger_state'] == 'closed' and derived['activated_on'] == ''
        assert detail['summary']['estimated_available'] is None
        assert counts() == initial_counts and legacy() == initial_legacy
        checks.append('real-pg-name-association-read-only-with-unknown-operator')
        previous_version = detail['version']
        db.execute('UPDATE phone_cards SET owner=? WHERE id=?', ('合成无匹配姓名', auto_card))
        db.commit()
        changed = employee_cards.get(db, actor, {'employee_id': [people[0]]}, day)
        assert changed['card_ledger']['registered'] == 2 and changed['version'] != previous_version
        assert counts() == initial_counts
        checks.append('real-pg-name-change-refreshes-view-and-version-without-duplicate-units')
    print(json.dumps(dict(passed=True, kind='real-loopback-postgresql-synthetic-batch-only',
                          checks=checks, production_connected=False), ensure_ascii=True))


if __name__ == '__main__':
    main()
