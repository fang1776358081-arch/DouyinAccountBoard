"""Real local PostgreSQL extension rehearsal; invoked only by the disposable probe."""
import json
from uuid import uuid4

from test_isolation import checked_root, install
from pg_schema import MIGRATIONS, FILES, apply_migrations, expected_versions, runtime_versions_supported
from pg_synthetic_migration import connect_synthetic
from pg_service_adapter import ServiceConnection


def main():
    install(checked_root())
    import auth
    import employee_cards
    import idempotency
    checks = []
    requests = []
    today = '2026-11-02'
    with connect_synthetic() as connection:
        expected = expected_versions()
        with connection.transaction():
            connection.execute('CREATE SCHEMA board_candidate')
            connection.execute('SET LOCAL search_path=board_candidate,pg_catalog')
            connection.execute('CREATE TABLE schema_migrations(version bigint PRIMARY KEY,sha256 text NOT NULL)')
            for version, digest in expected[:4]:
                connection.execute((MIGRATIONS / FILES[version - 1]).read_text(encoding='utf-8'))
                connection.execute('INSERT INTO schema_migrations VALUES(%s,%s)', (version, digest))
        connection.execute('SET search_path=board_candidate,pg_catalog')
        db = ServiceConnection(connection, schema='board_candidate', scope_enabled=True)
        assert runtime_versions_supported(connection.execute('SELECT version,sha256 FROM schema_migrations ORDER BY version').fetchall())
        assert not employee_cards.available(db)
        assert connection.execute('SELECT 1').fetchone() == (1,)
        checks.append('reviewed-v4-remains-readable-and-new-module-disabled-without-aborted-transaction')
        with connection.transaction():
            setup = auth.mint_setup(db)
            session = auth.setup_owner(db, dict(username='synthetic-owner', name='合成管理员',
                password='Synthetic-only-test-2026'), 'board_setup=' + setup)
            actor = auth.session(db, 'board_session=' + session)
            first_card = db.execute("INSERT INTO phone_cards(phone,carrier,status,owner,keeper) VALUES(?,?,?,?,?) RETURNING id",
                ('13800000101', '民生电信', '正常', '合成实物使用人', '合成保管人')).fetchone()[0]
            second_card = db.execute("INSERT INTO phone_cards(phone,carrier,status) VALUES(?,?,?) RETURNING id",
                ('13800000102', '民生电信', '停用')).fetchone()[0]
        baseline = {name: connection.execute('SELECT * FROM ' + name + ' ORDER BY id').fetchall()
                    for name in ('auth_users', 'phone_cards', 'accounts')}
        assert apply_migrations(connection) == 'applied'
        assert employee_cards.available(db)
        assert apply_migrations(connection) == 'already-applied'
        for name, rows in baseline.items():
            assert connection.execute('SELECT * FROM ' + name + ' ORDER BY id').fetchall() == rows
        checks.append('v4-to-v5-additive-atomic-migration-repeat-is-noop-and-legacy-rows-unchanged')
        assert db.execute('SELECT count(*) FROM employee_card_operators').fetchone()[0] == 6
        assert db.execute('SELECT count(*) FROM employee_card_rules').fetchone()[0] == 8
        assert db.execute('SELECT count(*) FROM employee_card_employees').fetchone()[0] == 0
        checks.append('configuration-only-seed-no-real-or-demo-staff-or-card-links')
        control = connection.execute('UPDATE runtime_control SET mode=\'open\' WHERE id=1 RETURNING writer_epoch,restore_generation').fetchone()
        db = ServiceConnection(connection, schema='board_candidate', scope_enabled=True,
                               writer_epoch=control[0], restore_generation=control[1])

        def save(action, data, employee_id=''):
            query = {'employee_id': [employee_id]} if employee_id else {}
            meta = employee_cards.get(db, actor, query, today)
            body = dict(action=action, data=data, employee_id=employee_id, expected_version=meta['version'])
            db.begin_write()
            try:
                preview = employee_cards.preview(db, actor, body, today)
                db.commit()
                apply_body = dict(preview_id=preview['preview_id'], request_id=str(uuid4()), confirmation='确认保存')
                db.begin_write()
                result = employee_cards.apply(db, actor, apply_body, today)
                db.commit()
                db.begin_write()
                assert employee_cards.apply(db, actor, apply_body, today) == result
                db.commit()
                requests.append(apply_body)
                return result
            except BaseException:
                db.rollback()
                raise

        employee = save('employee', dict(name='合成PG员工', employee_no='0001', office_id='',
            birth_date='2000-01-01', employment_status='active', consent_source='合成授权', consent_on=today))['employee_id']
        save('application', dict(operator_id='minsheng', requested_on=today, quantity=2, result='pending', source='合成申请'), employee)
        detail = employee_cards.get(db, actor, {'employee_id': [employee]}, today)
        assert detail['summary']['pending_count'] == 2
        unit_id = detail['units'][0]['id']
        save('card_link', dict(card_id=first_card, unit_id=unit_id, operator_id='minsheng',
                              activated_on=today, source='合成实名依据'), employee)
        save('card_link', dict(card_id=first_card, source='重复合成导入'), employee)
        save('replacement', dict(card_id=second_card, unit_id=unit_id, source='合成补换卡依据'), employee)
        detail = employee_cards.get(db, actor, {'employee_id': [employee]}, today)
        assert detail['summary']['company_lifetime'] == 1
        assert detail['summary']['company_current'] == 1
        assert detail['summary']['pending_count'] == 1
        assert len(detail['units']) == 2
        checks.append('real-pg-preview-apply-idempotency-pending-activation-repeat-and-replacement-one-unit')
        save('cancel_request', dict(unit_id=unit_id, requested_on=today, source='合成注销申请'), employee)
        assert employee_cards.get(db, actor, {'employee_id': [employee]}, today)['summary']['company_current'] == 1
        for name, rows in baseline.items():
            assert connection.execute('SELECT * FROM ' + name + ' ORDER BY id').fetchall() == rows
        checks.append('cancel-request-keeps-occupancy-and-module-does-not-change-legacy-cards-or-users')
        receipt_count = db.execute('SELECT count(*) FROM employee_card_requests').fetchone()[0]
        assert receipt_count == 6
        assert db.execute('SELECT count(*) FROM employee_card_links').fetchone()[0] == 2
        meta = employee_cards.get(db, actor, {}, today)
        db.begin_write()
        unfinished = employee_cards.preview(db, actor, dict(action='employee',
            expected_version=meta['version'], data=dict(name='合成未确认员工', employee_no='0002', office_id='')), today)
        db.commit()
        generation = 'synthetic-restored-generation'
        connection.execute('UPDATE runtime_control SET restore_generation=%s WHERE id=1', (generation,))
        restored = ServiceConnection(connection, schema='board_candidate', scope_enabled=True,
            writer_epoch=control[0], restore_generation=generation)
        rejected = [requests[-1], dict(preview_id=unfinished['preview_id'],
                                      request_id=str(uuid4()), confirmation='确认保存')]
        for body in rejected:
            restored.begin_write()
            try:
                employee_cards.apply(restored, actor, body, today)
                raise AssertionError('Old-generation action was accepted')
            except idempotency.IdempotencyError as error:
                assert error.code == 'RESTORE_GENERATION_EXPIRED'
                restored.rollback()
        try:
            employee_cards.get(restored, actor, {'request_id': [requests[-1]['request_id']]}, today)
            raise AssertionError('Old-generation receipt was returned')
        except idempotency.IdempotencyError as error:
            assert error.code == 'RESTORE_GENERATION_EXPIRED'
        assert restored.execute('SELECT count(*) FROM employee_card_requests').fetchone()[0] == receipt_count
        assert restored.execute('SELECT count(*) FROM employee_card_employees').fetchone()[0] == 1
        checks.append('restore-generation-rejects-stale-preview-uuid-replay-and-receipt-without-business-write')
    print(json.dumps(dict(passed=True, kind='real-loopback-postgresql-synthetic-only', checks=checks,
                          production_connected=False, persisted_requests=receipt_count), ensure_ascii=True))


if __name__ == '__main__':
    main()
