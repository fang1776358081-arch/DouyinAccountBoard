"""Fresh synthetic SQLite service checks; never imports the application."""
from datetime import date
import json
from pathlib import Path
import sqlite3
import unittest
import uuid

import auth
import employee_cards as service
import idempotency
import operations
import phone_cards
from test_isolation import checked_root


class ScopedConnection(sqlite3.Connection):
    scope_enabled = True


class EmployeeCardsTest(unittest.TestCase):
    def setUp(self):
        self.today = '2026-11-02'
        self.db = sqlite3.connect(checked_root() / ('employee-cards-' + uuid.uuid4().hex + '.db'), factory=ScopedConnection)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''CREATE TABLE accounts(id TEXT PRIMARY KEY,registration_phone TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT '',account_type TEXT NOT NULL DEFAULT '');
          CREATE TABLE offices(id TEXT PRIMARY KEY,name TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
          CREATE TABLE user_office_grants(user_id INTEGER,office_id TEXT,PRIMARY KEY(user_id,office_id));
          CREATE TABLE account_offices(account_id TEXT,office_id TEXT,PRIMARY KEY(account_id,office_id));
          CREATE TABLE phone_card_offices(card_id INTEGER,office_id TEXT,PRIMARY KEY(card_id,office_id));''')
        auth.migrate(self.db)
        self.db.execute('ALTER TABLE auth_users ADD COLUMN authorization_version INTEGER NOT NULL DEFAULT 0')
        phone_cards.migrate(self.db)
        operations.migrate(self.db)
        self.db.executemany('INSERT INTO offices(id,name) VALUES(?,?)', [('one', '合成办公室一'), ('two', '合成办公室二')])
        for ident, role, permissions in [(1, 'owner', []), (2, 'employee', ['card_manage']), (3, 'employee', []), (4, 'viewer', [])]:
            self.db.execute('INSERT INTO auth_users(id,username,name,role,permissions,password_hash,created_at) VALUES(?,?,?,?,?,?,?)',
                            (ident, 'synthetic-' + str(ident), '合成员工' + str(ident), role, json.dumps(permissions), 'synthetic-not-a-real-hash', 1))
        self.db.executemany('INSERT INTO user_office_grants VALUES(?,?)', [(2, 'one'), (3, 'one')])
        for ident, phone, office in [(1, '13800000001', 'one'), (2, '13800000002', 'one'), (3, '13800000003', 'two')]:
            self.db.execute('INSERT INTO phone_cards(id,phone,owner,keeper,status,carrier) VALUES(?,?,?,?,?,?)',
                            (ident, phone, '合成文字卡主不能自动关联', '合成保管人', '停用', '中国联通'))
            self.db.execute('INSERT INTO phone_card_offices VALUES(?,?)', (ident, office))
        self.legacy = self.legacy_snapshot()
        service.migrate(self.db)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def actor(self, ident=1):
        return dict(auth.public_user(self.db.execute('SELECT * FROM auth_users WHERE id=?', (ident,)).fetchone()), csrf='synthetic-only-csrf')

    def legacy_snapshot(self):
        return {name: [tuple(row) for row in self.db.execute('SELECT * FROM ' + name + ' ORDER BY 1')]
                for name in ('phone_cards', 'auth_users', 'user_office_grants', 'phone_card_offices')}

    def meta(self, actor=1, **query):
        return service.get(self.db, self.actor(actor), {key: [str(value)] for key, value in query.items()}, self.today)

    def prepare(self, action, data, employee_id='', actor=1):
        current = self.meta(actor, **({'employee_id': employee_id} if employee_id else {}))['version']
        self.db.execute('BEGIN IMMEDIATE')
        try:
            result = service.preview(self.db, self.actor(actor), dict(action=action, employee_id=employee_id, data=data, expected_version=current), self.today)
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def commit_preview(self, preview, actor=1, request_id=None):
        body = dict(preview_id=preview['preview_id'], confirmation='确认保存', request_id=request_id or str(uuid.uuid4()))
        self.db.execute('BEGIN IMMEDIATE')
        try:
            result = service.apply(self.db, self.actor(actor), body, self.today)
            self.db.commit()
            return result, body
        except BaseException:
            self.db.rollback()
            raise

    def save(self, action, data, employee_id='', actor=1):
        return self.commit_preview(self.prepare(action, data, employee_id, actor), actor)[0]

    def person(self, no='001', office='one', user_id=None, actor=1):
        data = dict(employee_no=no, name='合成实名员工' + no, employment_status='active', office_id=office,
                    birth_date='2005-11-03', consent_source='合成员工自行授权', consent_on=self.today)
        if user_id is not None:
            data['auth_user_id'] = user_id
        return self.save('employee', data, actor=actor)['employee_id']

    def operator(self, **changes):
        op = next(row for row in self.meta()['operators'] if row['id'] == 'minsheng')
        op.pop('revision', None)
        op.update(changes)
        return self.save('operator', op)

    def test_migration_seeds_config_only_and_is_repeatable(self):
        self.assertEqual(self.meta()['total'], 0)
        self.assertEqual([r[0] for r in service.OPERATOR_NAMES], [r['id'] for r in sorted(self.meta()['operators'], key=lambda r: [x[0] for x in service.OPERATOR_NAMES].index(r['id']))])
        self.assertEqual(len(self.meta()['rules']), 8)
        service.migrate(self.db)
        self.db.commit()
        self.assertEqual(len(self.meta()['operators']), 6)
        self.assertEqual(self.legacy_snapshot(), self.legacy)

    def test_preview_does_not_persist_profile_and_replay_is_idempotent(self):
        preview = self.prepare('employee', dict(employee_no='0007', name='合成同名人员', office_id='one', employment_status='unknown'))
        self.assertEqual(self.meta()['total'], 0)
        result, body = self.commit_preview(preview)
        self.db.execute('BEGIN IMMEDIATE')
        replay = service.apply(self.db, self.actor(), body, self.today)
        self.db.commit()
        self.assertEqual(replay, result)
        self.assertEqual(self.meta()['total'], 1)
        self.assertEqual(self.meta()['employees'][0]['employee_no'], '0007')
        stored = self.db.execute('SELECT result FROM employee_card_requests').fetchone()[0]
        self.assertNotIn('birth_date', stored)
        self.assertNotIn('csrf', stored)
        self.assertEqual(self.legacy_snapshot(), self.legacy)

    def test_birth_requires_consent_and_is_not_in_list_or_history(self):
        with self.assertRaisesRegex(ValueError, '授权'):
            self.prepare('employee', dict(employee_no='A', name='合成授权测试', birth_date='2000-01-01', office_id='one'))
        employee = self.person()
        meta = self.meta()
        self.assertNotIn('birth_date', meta['employees'][0])
        self.assertEqual(meta['employees'][0]['summary']['age_years'], 20)
        detail = self.meta(employee_id=employee)
        self.assertEqual(detail['employee']['birth_date'], '2005-11-03')
        self.assertNotIn('2005-11-03', json.dumps(detail['history']))
        self.assertEqual(detail['summary']['other_current'], None)

    def test_employee_identity_is_unique_and_never_inferred_from_owner_text(self):
        first = self.person(user_id=3)
        second = self.person('002', 'two')
        self.assertNotEqual(first, second)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM employee_card_units').fetchone()[0], 0)
        with self.assertRaisesRegex(ValueError, '已有关联'):
            self.person('003', user_id=3)
        with self.assertRaisesRegex(ValueError, '已有关联'):
            self.person()

    def test_scoped_manager_and_personal_view_recheck_identity(self):
        one, two = self.person(user_id=3), self.person('002', 'two')
        self.assertEqual([e['id'] for e in self.meta(2)['employees']], [one])
        self.assertEqual([e['id'] for e in self.meta(3)['employees']], [one])
        self.assertEqual(self.meta(4)['employees'], [])
        with self.assertRaises(auth.AccessError):
            self.meta(2, employee_id=two)
        with self.assertRaises(auth.AccessError):
            self.prepare('employee', dict(name='合成更名'), one, actor=3)
        with self.assertRaises(auth.AccessError):
            self.prepare('employee', dict(auth_user_id=4), one, actor=2)

    def test_duplicate_import_and_replacement_preserve_single_occupancy(self):
        person = self.person()
        data = dict(card_id=1, operator_id='minsheng', activated_on='2026-10-01', source='合成实名登记')
        result = self.save('card_link', data, person)
        duplicate = self.save('card_link', data, person)
        self.assertEqual(result['id'], duplicate['id'])
        self.save('replacement', dict(unit_id=result['id'], card_id=2, source='合成补换卡确认'), person)
        detail = self.meta(employee_id=person)
        self.assertEqual(detail['summary']['company_lifetime'], 1)
        self.assertEqual(detail['summary']['company_current'], 1)
        self.assertEqual(len(detail['units']), 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM employee_card_links').fetchone()[0], 2)
        self.assertEqual(self.legacy_snapshot(), self.legacy)

    def test_pending_units_are_promoted_not_duplicated_and_failures_remain_recorded(self):
        person = self.person()
        app = self.save('application', dict(operator_id='minsheng', requested_on='2026-11-01', quantity=2, result='pending'), person)
        detail = self.meta(employee_id=person)
        self.assertEqual(detail['summary']['pending_count'], 2)
        unit = detail['units'][0]['id']
        self.save('card_link', dict(unit_id=unit, card_id=1, operator_id='minsheng', activated_on='2026-11-01', source='合成激活确认'), person)
        detail = self.meta(employee_id=person)
        self.assertEqual(detail['summary']['company_current'], 1)
        self.assertEqual(detail['summary']['pending_count'], 1)
        self.save('application', dict(id=app['id'], result='failed', failure_reason='原因待核实'), person)
        detail = self.meta(employee_id=person)
        self.assertEqual(detail['summary']['pending_count'], 0)
        self.assertEqual(detail['summary']['company_current'], 1)
        self.assertEqual(len(detail['units']), 2)
        self.assertEqual(detail['applications'][0]['result'], 'failed')

    def test_cancellation_is_not_release_and_confirmed_release_is_once_only(self):
        person = self.person()
        unit = self.save('card_link', dict(card_id=1, operator_id='minsheng', activated_on='2026-10-01', source='合成实名来源'), person)['id']
        self.save('cancel_request', dict(unit_id=unit, requested_on='2026-11-01', source='仅提交注销申请'), person)
        self.assertEqual(self.meta(employee_id=person)['summary']['company_current'], 1)
        with self.assertRaisesRegex(ValueError, '未确认'):
            self.prepare('release', dict(unit_id=unit, released_on=self.today, source='合成运营商确认'), person)
        self.operator(release_mode='confirmed')
        release = dict(unit_id=unit, released_on=self.today, source='合成运营商确认')
        self.save('release', release, person)
        self.save('release', release, person)
        summary = self.meta(employee_id=person)['summary']
        self.assertEqual(summary['company_lifetime'], 1)
        self.assertEqual(summary['company_current'], 0)
        self.operator(release_mode='unknown', release_delay_days=None)
        self.assertEqual(self.meta(employee_id=person)['summary']['company_current'], 0)
        self.assertEqual(self.legacy_snapshot(), self.legacy)

    def test_total_verification_never_adds_company_twice_and_external_is_separate(self):
        person = self.person()
        self.save('card_link', dict(card_id=1, operator_id='minsheng', activated_on='2026-10-01', source='合成实名来源'), person)
        data = dict(scope='all', scope_id='', period='stock', period_start='2026-01-01', period_end='2026-12-31',
                    count=4, coverage='total', verified_on=self.today, valid_until=self.today, source='合成全运营商名下核验', pending_unit_ids=[], company_inventory_complete=True)
        self.save('verification', data, person)
        summary = self.meta(employee_id=person)['summary']
        self.assertEqual(summary['verified_total'], 4)
        self.assertEqual(summary['other_current'], 3)
        self.save('verification', dict(data, count=3, coverage='external'), person)
        summary = self.meta(employee_id=person)['summary']
        self.assertEqual(summary['verified_total'], 4)
        self.assertEqual(summary['other_current'], 3)
        checks = self.meta(employee_id=person)['verifications']
        external = next(c for c in checks if c['coverage'] == 'external')
        self.assertEqual(external['unit_ids'], [])

    def test_preview_expiration_version_and_replay_permissions_are_rechecked(self):
        person = self.person()
        preview = self.prepare('employee', dict(name='合成改名'), person)
        self.save('employee', dict(name='另一合成修改'), person)
        with self.assertRaises(operations.ConflictError):
            self.commit_preview(preview)
        preview = self.prepare('employee', dict(name='合成到期'), person)
        self.db.execute('UPDATE operations_previews SET expires_at=0 WHERE id=?', (preview['preview_id'],))
        self.db.commit()
        with self.assertRaisesRegex(ValueError, '过期'):
            self.commit_preview(preview)
        preview = self.prepare('employee', dict(name='合成经理修改'), person, actor=2)
        _, body = self.commit_preview(preview, actor=2)
        self.db.execute("UPDATE auth_users SET permissions='[]' WHERE id=2")
        self.db.commit()
        with self.assertRaises(auth.AccessError):
            service.apply(self.db, self.actor(2), body, self.today)

    def test_cross_office_association_redacts_counts_and_blocks_detail_history(self):
        person = self.person(user_id=3)
        self.save('card_link', dict(card_id=3, operator_id='minsheng', activated_on='2026-10-01', source='合成跨办来源不应泄露'), person)
        self.save('verification', dict(scope='operator', scope_id='minsheng', period='stock', period_start='2026-01-01',
                                      period_end='2026-12-31', count=4321, coverage='total', verified_on=self.today,
                                      valid_until=self.today, source='合成跨办核验来源不应泄露', pending_unit_ids=[],
                                      company_inventory_complete=True), person)
        for actor in (2, 3):
            meta = self.meta(actor)
            employee = meta['employees'][0]
            self.assertIsNone(employee['summary']['company_current'])
            self.assertIsNone(employee['summary']['verified_total'])
            self.assertTrue(all(op['status'] == 'needs_verification' for op in employee['summary']['operators']))
            self.assertNotIn('13800000003', json.dumps(meta))
            self.assertNotIn('合成跨办来源不应泄露', json.dumps(meta, ensure_ascii=False))
            self.assertNotIn('4321', json.dumps(meta))
            self.assertTrue(all('count_evidence' not in op for op in employee['summary']['operators']))
            with self.assertRaises(auth.AccessError):
                self.meta(actor, employee_id=person)
        self.assertEqual(self.meta(employee_id=person)['summary']['company_current'], 1)

    def test_card_changed_after_preview_is_a_conflict_and_failed_preview_rolls_back(self):
        person = self.person()
        preview = self.prepare('card_link', dict(card_id=1, operator_id='minsheng', activated_on=self.today, source='合成来源'), person)
        self.db.execute("UPDATE phone_cards SET status='已注销' WHERE id=1")
        self.db.commit()
        with self.assertRaises(operations.ConflictError):
            self.commit_preview(preview)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM employee_card_units').fetchone()[0], 0)
        with self.assertRaises(ValueError):
            self.prepare('employee', dict(employee_no='A', name='合成无授权', birth_date='2000-01-01'), actor=2)
        self.assertEqual(self.meta()['total'], 1)

    def test_list_reuses_same_transaction_bundle_and_scopes_sql_before_rows(self):
        self.person()
        for number in ('002', '003', '004'):
            self.person(number, 'two')
        statements = []
        self.db.set_trace_callback(statements.append)
        meta = self.meta()
        self.db.set_trace_callback(None)
        self.assertEqual(meta['total'], 4)
        reads = [s for s in statements if s.startswith('SELECT * FROM employee_card_employees ORDER BY')]
        self.assertEqual(len(reads), 1, 'list versions must not reread the module once per employee')
        statements.clear()
        self.db.set_trace_callback(statements.append)
        self.assertEqual(self.meta(2)['total'], 1)
        self.db.set_trace_callback(None)
        reads = [s for s in statements if s.startswith('SELECT * FROM employee_card_employees')]
        self.assertEqual(len(reads), 1)
        self.assertIn("WHERE office_id IN ('one')", reads[0])

    def test_pending_verification_cannot_claim_another_operator_and_receipt_reuse_conflicts(self):
        person = self.person()
        self.save('application', dict(operator_id='zhongxing', requested_on=self.today, quantity=1, result='pending'), person)
        pending = self.meta(employee_id=person)['units'][0]['id']
        with self.assertRaisesRegex(ValueError, '在途核验关联'):
            self.prepare('verification', dict(scope='operator', scope_id='minsheng', period='day', period_start=self.today,
                                              period_end=self.today, count=1, coverage='total', verified_on=self.today,
                                              valid_until=self.today, source='合成核验来源', pending_unit_ids=[pending]), person)
        preview = self.prepare('employee', dict(name='合成改名'), person)
        _, body = self.commit_preview(preview)
        with self.assertRaisesRegex(ValueError, '同一请求标识'):
            service.apply(self.db, self.actor(), dict(body, confirmation='不同内容'), self.today)

    def test_activation_network_is_frozen_across_current_operator_configuration_changes(self):
        person = self.person()
        self.operator(network='unicom', channel_id='synthetic-old-channel', effective_from='2026-10-01', source='合成历史联通网络依据')
        other = next(row for row in self.meta()['operators'] if row['id'] == 'zhongxing')
        other.pop('revision', None)
        other.update(network='unicom', effective_from='2026-10-01', source='合成另一联通品牌依据')
        self.save('operator', other)
        unit = self.save('card_link', dict(card_id=1, operator_id='minsheng', activated_on='2026-11-01', source='合成明确实名关联'), person)['id']
        self.operator(network='mobile', channel_id='synthetic-new-channel', effective_from=self.today, source='合成仅当前移动网络依据')
        self.save('verification', dict(scope='network', scope_id='unicom', period='month', period_start='2026-11-01',
                                      period_end='2026-11-30', count=0, coverage='external', verified_on=self.today,
                                      valid_until=self.today, source='合成联通当月其他渠道0张已核验', pending_unit_ids=[], company_inventory_complete=True), person)
        detail = self.meta(employee_id=person)
        frozen = detail['units'][0]['network_policy']
        self.assertEqual(frozen['network'], 'unicom')
        self.assertEqual(frozen['channel_id'], 'synthetic-old-channel')
        self.assertEqual(frozen['source'], '合成历史联通网络依据')
        sibling = next(op for op in detail['summary']['operators'] if op['operator_id'] == 'zhongxing')
        monthly = next(limit for limit in sibling['limits'] if limit['rule_id'] == 'unicom-month')
        self.assertEqual(monthly['used'], 1, 'an older activation still counts against its actual historical network')
        self.save('replacement', dict(unit_id=unit, card_id=2, source='合成补换卡'), person)
        self.assertEqual(self.meta(employee_id=person)['units'][0]['network_policy'], frozen)
        self.assertEqual(len(self.meta(employee_id=person)['card_links']), 2)

    def test_unknown_historical_network_is_not_backfilled_from_current_brand(self):
        person = self.person()
        self.save('card_link', dict(card_id=1, operator_id='minsheng', activated_on='2026-10-01', source='合成历史卡来源'), person)
        self.operator(network='unicom', effective_from='2026-09-01', source='合成当前配置不能倒推旧卡')
        detail = self.meta(employee_id=person)
        self.assertEqual(detail['units'][0]['network_policy']['network'], 'unknown')
        with self.assertRaisesRegex(ValueError, '未开放'):
            self.prepare('card_link', dict(card_id=2, operator_id='minsheng', activated_on=self.today,
                                          source='合成来源', network_policy={'network': 'mobile'}), person)

    def test_incomplete_company_inventory_is_not_inferred_from_full_permission_scope(self):
        person = self.person()
        self.save('card_link', dict(card_id=1, operator_id='minsheng', activated_on='2026-10-01', source='合成仅一张已登记公司卡'), person)
        data = dict(scope='all', scope_id='', period='stock', period_start='2026-01-01', period_end='2026-12-31',
                    count=4, coverage='total', verified_on=self.today, valid_until=self.today,
                    source='合成全运营商官方总数，公司旧卡尚未核全', pending_unit_ids=[])
        self.save('verification', data, person)
        detail = self.meta(employee_id=person)
        self.assertIs(detail['verifications'][0]['company_inventory_complete'], False)
        self.assertEqual(detail['summary']['verified_total'], 4)
        self.assertIsNone(detail['summary']['other_current'], 'the other three may be unregistered company cards')
        self.save('verification', dict(data, coverage='external', count=3), person)
        summary = self.meta(employee_id=person)['summary']
        self.assertEqual(summary['other_current'], 3)
        self.assertIsNone(summary['verified_total'], 'one known company card is a lower bound, not the full ledger')
        self.assertIsNone(summary['estimated_available'])
        with self.assertRaisesRegex(ValueError, '完整性'):
            self.prepare('verification', dict(data, company_inventory_complete='true'), person)

    def test_restore_generation_invalidates_unused_preview_without_business_writes(self):
        self.db.restore_generation = 'synthetic-generation-one'
        person = self.person()
        old_version = self.meta(employee_id=person)['version']
        preview = self.prepare('employee', dict(name='合成恢复前草稿'), person)
        before = self.db.execute('SELECT name,revision FROM employee_card_employees WHERE id=?', (person,)).fetchone()
        self.db.restore_generation = 'synthetic-generation-two'
        self.assertNotEqual(old_version, self.meta(employee_id=person)['version'])
        with self.assertRaises(idempotency.IdempotencyError) as error:
            self.commit_preview(preview)
        self.assertEqual(error.exception.code, 'RESTORE_GENERATION_EXPIRED')
        after = self.db.execute('SELECT name,revision FROM employee_card_employees WHERE id=?', (person,)).fetchone()
        self.assertEqual(tuple(before), tuple(after))

    def test_restore_generation_rejects_old_uuid_replay_and_result_lookup(self):
        self.db.restore_generation = 'synthetic-generation-one'
        person = self.person()
        preview = self.prepare('employee', dict(name='合成已保存旧代次'), person)
        result, body = self.commit_preview(preview)
        stored = self.db.execute('SELECT restore_generation FROM employee_card_requests WHERE request_id=?', (body['request_id'],)).fetchone()[0]
        self.assertEqual(stored, 'synthetic-generation-one')
        self.assertEqual(service.get(self.db, self.actor(), {'request_id': [body['request_id']]}, self.today)['result'], result)
        self.db.restore_generation = 'synthetic-generation-two'
        for changed in (body, dict(body, confirmation='其他新内容')):
            with self.assertRaises(idempotency.IdempotencyError) as error:
                service.apply(self.db, self.actor(), changed, self.today)
            self.assertEqual(error.exception.code, 'RESTORE_GENERATION_EXPIRED')
        with self.assertRaises(idempotency.IdempotencyError) as error:
            service.get(self.db, self.actor(), {'request_id': [body['request_id']]}, self.today)
        self.assertEqual(error.exception.code, 'RESTORE_GENERATION_EXPIRED')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM employee_card_requests WHERE request_id=?', (body['request_id'],)).fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
