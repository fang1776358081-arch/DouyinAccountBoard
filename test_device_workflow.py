"""Device lifecycle checks using fresh synthetic data and loopback HTTP only."""
import json
import sqlite3
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from test_isolation import checked_root

# Fail closed before importing runtime modules when invoked without the runner.
checked_root()

import app
import auth
import operations
import test_auth_support


class SyntheticConnection(sqlite3.Connection):
    """Sequential HTTP fixture; does not simulate PostgreSQL lock concurrency."""

    def execute(self, statement, parameters=()):
        # SQLite BEGIN IMMEDIATE holds the write lock in this isolated fixture.
        # The production query uses a PostgreSQL row lock as well.
        if statement.endswith(' FOR UPDATE'):
            statement = statement[:-len(' FOR UPDATE')]
        return super().execute(statement, parameters)


class DeviceWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.folder = checked_root() / ('device-workflow-' + uuid4().hex)
        self.folder.mkdir()
        self.database_patch = patch.object(app, 'DB', self.folder / 'board.db')
        self.database_patch.start()
        self.addCleanup(self.database_patch.stop)
        app.initialize()

        class QuietHandler(app.Handler):
            def log_message(self, *args):
                pass

        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.owner = test_auth_support.owner_client(app, self.base)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())

    def call(self, path, body=None, client=None):
        client = self.owner if client is None else client
        if body is not None and not path.startswith('/api/auth/'):
            state = self.board(client)
            # Supply current versions for all records. Conflict tests pass an
            # explicit earlier snapshot so they exercise the HTTP guard.
            body = {**body}
            body.setdefault('_expected_versions', state['record_versions'])
            body.setdefault('request_id', str(uuid4()))
        request = urllib.request.Request(
            self.base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Content-Type': 'application/json'},
        )
        with client.open(request, timeout=10) as response:
            return json.load(response)

    def board(self, client=None):
        return self.call('/api/board', client=client)

    def action(self, action, **body):
        body.setdefault('request_id', str(uuid4()))
        return self.call('/api/device-workflow', {'action': action, **body})

    def reject(self, status, path, body, client=None, text=None):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.call(path, body, client)
        with caught.exception as response:
            result = json.load(response)
            self.assertEqual(response.code, status, result)
            if text:
                self.assertIn(text, result['error'])
            return result

    def reject_action(self, http_status, action, **body):
        body.setdefault('request_id', str(uuid4()))
        return self.reject(http_status, '/api/device-workflow', {'action': action, **body})

    def create_device(self, ident, state='待确认'):
        self.call('/api/phone-device/create', {'id': ident, 'state': state})
        return next(row for row in self.board()['phone_devices']['devices'] if row['id'] == ident)

    def create_card(self, phone, **fields):
        result = self.call('/api/phone-card', {'phone': phone, **fields})
        return next(row for row in self.board()['phone_cards']['cards'] if row['id'] == result['id'])

    def account(self, ident='synth-account-1'):
        return next(row for row in self.board()['accounts'] if row['id'] == ident)

    def device(self, ident):
        return next(row for row in self.board()['phone_devices']['devices'] if row['id'] == ident)

    def card(self, ident):
        return next(row for row in self.board()['phone_cards']['cards'] if row['id'] == ident)

    def case(self, ident):
        return next(row for row in self.board()['device_workflow']['cases'] if row['id'] == ident)

    def confirmed_account(self, device_id, account_id='synth-account-1'):
        self.call('/api/account/phone-device', {'id': account_id, 'phone_device_id': device_id})
        unit = self.device(device_id)['current_unit']
        self.assertIsNotNone(unit, '新增设备应直接登记独立实物')
        self.action('account_recovery', account_id=account_id, recovery_state='恢复成功',
                    target_unit_id=unit['id'], confirmed_login=True)
        return unit

    def enable_extended_database(self, *, scope=False, idempotency=False):
        migrations = Path(__file__).parent / 'migrations' / 'postgresql'
        with app.connect() as db:
            if scope:
                db.executescript((migrations / '0002_office_scope.sql').read_text(encoding='utf-8'))
            if idempotency:
                db.executescript((migrations / '0003_idempotency.sql').read_text(encoding='utf-8'))

        @contextmanager
        def synthetic_connect():
            connection = sqlite3.connect(app.DB, factory=SyntheticConnection, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.scope_enabled = scope
            if idempotency:
                connection.restore_generation = 'synthetic-device-generation'
            connection.execute('PRAGMA foreign_keys=ON')
            try:
                with connection:
                    yield connection
            finally:
                connection.close()

        connection_patch = patch.object(app, 'connect', synthetic_connect)
        connection_patch.start()
        self.addCleanup(connection_patch.stop)

    def test_fault_card_move_new_number_and_actual_login_are_separate(self):
        self.create_device('29', '在用')
        target = self.create_device('86', '可用闲置')['current_unit']
        old = self.confirmed_account('29')
        card = self.create_card('19900000029', location='29')
        self.action('place_card', card_id=card['id'], unit_id=old['id'], slot='1', confirmed_usable=True)
        baseline = self.account()
        result = self.action('open_case', unit_id=old['id'], reason='合成手机屏幕故障',
                             affected_account_ids=['synth-account-1'], owner='合成负责人')
        case_id = result['case_id']
        self.assertEqual(self.card(card['id'])['placement']['unit_id'], old['id'])
        self.assertEqual(self.account()['device_access']['last_unit_id'], old['id'])
        self.assertEqual(self.account()['device_access']['login_state'], '登录受阻')

        self.action('place_card', card_id=card['id'], unit_id=target['id'], slot='1', confirmed_usable=True)
        after_move = self.account()
        self.assertEqual(self.card(card['id'])['placement']['unit_id'], target['id'])
        self.assertEqual(after_move['device_access']['last_unit_id'], old['id'])
        self.assertEqual(after_move['phone_device_id'], '29')
        self.assertEqual(after_move['device_access']['login_state'], '登录受阻')
        for field in ('registration_phone', 'computer_id', 'slot', 'status', 'published'):
            self.assertEqual(after_move[field], baseline[field])

        self.action('unit_status', unit_id=old['id'], status='送修', note='合成送修记录')
        replacement = self.action('replace_unit', device_id='29', model='合成新机')
        new = self.device('29')['current_unit']
        self.assertNotEqual(new['id'], old['id'])
        self.assertEqual(new['id'], replacement['unit_id'])
        self.assertEqual(new['generation'], 2)
        self.assertEqual(new['status'], '待确认')
        self.assertEqual(self.account()['device_access']['last_unit_id'], old['id'])
        self.assertEqual(self.card(card['id'])['placement']['unit_id'], target['id'])
        self.assertEqual(len(self.device('29')['unit_history']), 2)
        self.assertEqual(self.device('29')['account_count'], 0)
        self.reject(400, '/api/account/phone-device', {'id': 'synth-account-1', 'phone_device_id': '86'})
        self.assertEqual(self.account()['device_access']['last_unit_id'], old['id'])
        self.action('unit_status', unit_id=old['id'], status='返修待验')
        self.reject_action(400, 'unit_status', unit_id=old['id'], status='可用闲置')
        self.assertEqual(self.device('29')['current_unit']['id'], new['id'])

        self.reject_action(400, 'account_recovery', account_id='synth-account-1',
                           recovery_state='恢复成功', target_unit_id=target['id'], case_id=case_id)
        self.assertEqual(self.account()['device_access']['last_unit_id'], old['id'])
        self.action('account_recovery', account_id='synth-account-1', recovery_state='等待协助',
                    target_unit_id=target['id'], case_id=case_id, reason='旧手机无法扫码',
                    owner='合成恢复负责人', followup_on='2026-11-01')
        self.assertEqual(self.account()['device_access']['last_unit_id'], old['id'])
        self.action('account_recovery', account_id='synth-account-1', recovery_state='恢复成功',
                    target_unit_id=target['id'], case_id=case_id, confirmed_login=True)
        restored = self.account()
        self.assertEqual(restored['phone_device_id'], '86')
        self.assertEqual(restored['device_access']['last_unit_id'], target['id'])
        self.assertEqual(restored['device_access']['login_state'], '可操作')
        self.assertGreater(restored['device_access']['confirmed_at'], 0)
        for field in ('registration_phone', 'computer_id', 'slot', 'status', 'published'):
            self.assertEqual(restored[field], baseline[field])

    def test_two_slots_confirmation_and_external_storage_preserve_card_identity(self):
        target = self.create_device('86', '可用闲置')['current_unit']
        first = self.create_card('19900000011', label='合成卡一')
        second = self.create_card('19900000012', label='合成卡二')
        self.reject_action(400, 'place_card', card_id=first['id'], unit_id=target['id'], slot='1')
        self.assertIsNone(self.card(first['id'])['placement'])
        self.action('place_card', card_id=first['id'], unit_id=target['id'], slot='1', confirmed_usable=True)
        self.reject_action(400, 'place_card', card_id=second['id'], unit_id=target['id'], slot='1', confirmed_usable=True)
        self.assertIsNone(self.card(second['id'])['placement'])
        self.action('place_card', card_id=second['id'], unit_id=target['id'], slot='2', confirmed_usable=True)
        self.assertEqual(self.card(first['id'])['placement']['slot'], '1')
        self.assertEqual(self.card(second['id'])['placement']['slot'], '2')
        self.reject_action(400, 'unit_status', unit_id=target['id'], status='可用闲置')
        self.action('place_card', card_id=first['id'], storage='合成卡盒 A')
        stored = self.card(first['id'])
        self.assertEqual((stored['id'], stored['phone'], stored['label']),
                         (first['id'], first['phone'], '合成卡一'))
        self.assertIsNone(stored['placement']['unit_id'])
        self.assertEqual(stored['placement']['storage'], '合成卡盒 A')
        self.assertEqual(self.card(second['id'])['placement']['unit_id'], target['id'])

    def test_partial_recovery_needs_explicit_outcome_before_case_closure(self):
        self.create_device('29', '在用')
        target = self.create_device('86', '可用闲置')['current_unit']
        old = self.confirmed_account('29')
        self.call('/api/account/create', {'name': '合成第二账号', 'computer_id': 'synth-pc-1',
                                         'slot': 2, 'status': '正常'})
        second = next(row['id'] for row in self.board()['accounts'] if row['name'] == '合成第二账号')
        self.confirmed_account('29', second)
        case_id = self.action('open_case', unit_id=old['id'], reason='合成主板故障',
                              affected_account_ids=['synth-account-1', second])['case_id']
        self.reject_action(400, 'update_case', case_id=case_id, status='暂挂')
        self.action('update_case', case_id=case_id, status='暂挂', owner='合成跟进人', followup_on='2026-11-02')
        self.assertEqual(self.case(case_id)['status'], '暂挂')
        todo = next(row for row in self.board()['device_workflow']['todos'] if row['id'] == case_id)
        self.assertEqual((todo['owner'], todo['followup_on']), ('合成跟进人', '2026-11-02'))
        self.action('account_recovery', account_id='synth-account-1', recovery_state='恢复成功',
                    target_unit_id=target['id'], case_id=case_id, confirmed_login=True)
        self.reject_action(400, 'update_case', case_id=case_id, status='已结案', closure_reason='只有一个账号已恢复')
        self.reject_action(400, 'account_recovery', account_id=second, recovery_state='无法恢复', case_id=case_id, reason='')
        self.action('account_recovery', account_id=second, recovery_state='无法恢复', case_id=case_id,
                    reason='合成恢复条件不足，人工确认无法恢复')
        self.action('update_case', case_id=case_id, status='已结案', closure_reason='一项恢复，一项确认无法恢复')
        self.assertEqual(self.case(case_id)['status'], '已结案')
        self.assertFalse(any(row.get('case_id') == case_id or row['id'] == case_id
                             for row in self.board()['device_workflow']['todos']))
        unavailable = self.account(second)
        self.assertEqual(unavailable['status'], '正常')
        self.assertEqual(unavailable['device_access']['last_unit_id'], old['id'])
        self.assertEqual(unavailable['device_access']['recovery_state'], '无法恢复')
        self.reject_action(400, 'update_case', case_id=case_id, status='处理中')

    def test_legacy_location_text_is_not_interpreted_as_confirmed_card_position(self):
        self.create_device('29', '在用')
        raw = '29 / 苹果13 / 合成机柜 A'
        card = self.create_card('19900000021', location=raw)
        self.assertEqual(self.card(card['id'])['location'], raw)
        self.assertIsNone(self.card(card['id'])['placement'])
        app.initialize()
        self.assertEqual(self.card(card['id'])['location'], raw)
        self.assertIsNone(self.card(card['id'])['placement'])

    def test_missing_permissions_and_stale_versions_do_not_mutate_workflow(self):
        unit = self.create_device('29', '在用')['current_unit']
        self.call('/api/auth/users', {'username': 'synthetic_device_viewer', 'name': '合成只读用户',
                                     'password': test_auth_support.TEST_PASSWORD, 'role': 'viewer', 'permissions': []})
        viewer = test_auth_support.client(self.base, 'synthetic_device_viewer')
        before = self.board()['device_workflow']
        self.reject(403, '/api/device-workflow', {'action': 'unit_status', 'unit_id': unit['id'],
                                                 'status': '故障', 'request_id': str(uuid4())}, viewer)
        self.assertEqual(self.board()['device_workflow'], before)
        stale = self.board()['record_versions']
        self.action('unit_status', unit_id=unit['id'], status='故障')
        self.reject_action(409, 'unit_status', unit_id=unit['id'], status='送修', _expected_versions=stale)
        self.assertEqual(self.device('29')['current_unit']['status'], '故障')
        with app.connect() as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())

    def test_same_request_replays_one_change_and_reused_id_is_rejected(self):
        unit = self.create_device('29', '在用')['current_unit']
        self.enable_extended_database(idempotency=True)
        request = {'action': 'open_case', 'unit_id': unit['id'], 'reason': '合成幂等故障',
                   'affected_account_ids': [], 'request_id': str(uuid4()),
                   '_expected_versions': self.board()['record_versions']}
        first = self.call('/api/device-workflow', request)
        after_first = self.board()['device_workflow']
        replayed = self.call('/api/device-workflow', request)
        self.assertEqual(replayed, first)
        self.assertEqual(self.board()['device_workflow'], after_first)
        self.assertEqual(len(after_first['cases']), 1)
        self.reject(409, '/api/device-workflow', {**request, 'reason': '合成不同内容'})
        self.assertEqual(self.board()['device_workflow'], after_first)

    def test_office_scope_checks_source_destination_and_related_accounts(self):
        self.create_device('29', '在用')
        own = self.create_device('86', '可用闲置')['current_unit']
        foreign = self.create_device('99', '可用闲置')['current_unit']
        last = self.confirmed_account('29')
        card = self.create_card('19900000031')
        self.action('place_card', card_id=card['id'], unit_id=foreign['id'], slot='1', confirmed_usable=True)
        self.enable_extended_database(scope=True)
        with app.connect() as db:
            db.execute("INSERT INTO offices(id,name) VALUES('synth-office-a','合成办公室甲')")
            db.execute("INSERT INTO offices(id,name) VALUES('synth-office-b','合成办公室乙')")
            db.execute("INSERT INTO computer_offices VALUES('synth-pc-1','synth-office-b')")
            db.execute("INSERT INTO account_offices VALUES('synth-account-1','synth-office-b')")
            for device, office in (('29', 'synth-office-a'), ('86', 'synth-office-a'), ('99', 'synth-office-b')):
                db.execute('INSERT INTO phone_device_offices VALUES(?,?)', (device, office))
            db.execute("INSERT INTO phone_card_offices VALUES(?,'synth-office-a')", (card['id'],))
            actor = auth.public_user(db.execute("SELECT * FROM auth_users WHERE role='owner'").fetchone())
            auth.save_user(db, actor, {'username': 'synthetic_scope_worker', 'name': '合成办公室甲员工',
                                      'role': 'employee', 'permissions': ['device_manage', 'card_manage'],
                                      'password': test_auth_support.TEST_PASSWORD, 'office_ids': ['synth-office-a']})
        employee = test_auth_support.client(self.base, 'synthetic_scope_worker')
        visible = self.board(employee)
        self.assertEqual({row['id'] for row in visible['phone_devices']['devices']}, {'29', '86'})
        self.assertFalse(visible['accounts'])
        self.assertFalse(any(row['id'] == foreign['id'] for row in visible['device_workflow']['units']))
        self.assertIsNone(next(row for row in visible['phone_cards']['cards'] if row['id'] == card['id'])['placement'])
        baseline = self.board()['device_workflow']
        requests = [
            {'action': 'unit_status', 'unit_id': foreign['id'], 'status': '故障'},
            # Moving an owned card out of a foreign phone still touches its source.
            {'action': 'place_card', 'card_id': card['id'], 'unit_id': own['id'], 'slot': '1', 'confirmed_usable': True},
            # The phone is visible, but its related account is outside the grant.
            {'action': 'open_case', 'unit_id': last['id'], 'reason': '合成越权故障'},
            {'action': 'account_recovery', 'account_id': 'synth-account-1',
             'recovery_state': '恢复成功', 'target_unit_id': own['id'], 'confirmed_login': True},
        ]
        for body in requests:
            self.reject(403, '/api/device-workflow', {**body, 'request_id': str(uuid4())}, employee)
            self.assertEqual(self.board()['device_workflow'], baseline)
        result = self.call('/api/device-workflow', {'action': 'unit_status', 'unit_id': own['id'],
                                                   'status': '待确认', 'request_id': str(uuid4())}, employee)
        self.assertTrue(result['ok'])
        self.assertEqual(self.device('86')['current_unit']['status'], '待确认')

    def test_repaired_old_phone_keeps_identity_when_given_a_new_number(self):
        self.create_device('29', '在用')
        self.create_device('86', '可用闲置')
        old = self.confirmed_account('29')
        card = self.create_card('19900000041', location='29 / 卡槽待核对')
        case_id = self.action('open_case', unit_id=old['id'], reason='合成返修流程',
                              affected_account_ids=['synth-account-1'])['case_id']
        # Inventory confirmation records the card already in a broken phone;
        # it does not assert that the phone is usable or log an account in.
        self.action('place_card', card_id=card['id'], unit_id=old['id'], slot='待确认', confirm_existing=True)
        self.assertEqual(self.device('29')['current_unit']['status'], '故障')
        self.assertEqual(self.card(card['id'])['placement']['slot'], '待确认')
        replacement = self.action('replace_unit', device_id='29', new_status='可用闲置')['unit_id']
        self.action('unit_status', unit_id=old['id'], status='返修待验')
        self.reject_action(400, 'reassign_unit', unit_id=old['id'], device_id='87')
        self.reject_action(400, 'reassign_unit', unit_id=old['id'], device_id='86', confirmed_usable=True)
        result = self.action('reassign_unit', unit_id=old['id'], device_id='87', confirmed_usable=True)
        self.assertEqual(result['unit_id'], old['id'])
        returned = self.device('87')['current_unit']
        self.assertEqual(returned['id'], old['id'])
        self.assertEqual(returned['original_device_id'], '29')
        self.assertEqual(self.device('29')['current_unit']['id'], replacement)
        self.assertEqual(self.device('29')['account_count'], 0)
        self.assertEqual(self.card(card['id'])['placement']['unit_id'], old['id'])
        account = self.account()
        self.assertEqual(account['phone_device_id'], '87')
        self.assertEqual(account['device_access']['last_unit_id'], old['id'])
        self.assertEqual(account['device_access']['login_state'], '登录受阻')
        self.assertEqual(account['device_access']['case_id'], case_id)
        self.assertEqual(self.case(case_id)['unit_id'], old['id'])
        self.assertTrue(any(row['id'] == old['id'] for row in self.device('29')['unit_history']))

    def test_export_real_version_contract_for_frontend_cross_check(self):
        self.create_device('29', '在用')
        target = self.create_device('86', '可用闲置')['current_unit']
        old = self.confirmed_account('29')
        card = self.create_card('19900000051')
        self.action('place_card', card_id=card['id'], unit_id=old['id'], slot='1', confirmed_usable=True)
        case_id = self.action('open_case', unit_id=old['id'], reason='合成版本契约故障',
                              affected_account_ids=['synth-account-1'])['case_id']
        new_id = self.action('replace_unit', device_id='29')['unit_id']
        contracts = []

        def capture(label, path, body, required=()):
            board = self.board()
            with app.connect() as db:
                keys = operations.targets(db, path, body)
            self.assertTrue(set(required) <= set(keys), (label, keys))
            # Export only synthetic relationship data, never the session/CSRF.
            state = {key: board[key] for key in ('accounts', 'phone_devices', 'phone_cards',
                                                'device_workflow', 'record_versions')}
            contracts.append({'label': label, 'path': path, 'body': body, 'state': state,
                              'required': sorted(set(keys))})

        for field, value in (('owner', '合成负责人'), ('location', '合成机柜')):
            capture('batch-devices-' + field, '/api/batch-edit',
                    {'kind': 'devices', 'ids': ['29', '86'], 'patch': {field: value}})
            self.assertEqual(contracts[-1]['required'], ['device:29', 'device:86'])
        capture('batch-devices-state', '/api/batch-edit',
                {'kind': 'devices', 'ids': ['29', '86'], 'patch': {'state': '待确认'}},
                ('unit:' + new_id, 'unit:' + target['id']))
        capture('legacy-old-physical-account-move', '/api/account/phone-device',
                {'id': 'synth-account-1', 'phone_device_id': '86'},
                ('access:synth-account-1', 'unit:' + old['id'], 'unit:' + new_id, 'unit:' + target['id']))
        capture('patch-old-physical-account-move', '/api/account/patch',
                {'id': 'synth-account-1', 'changes': {'phone_device_id': '86'}},
                ('access:synth-account-1', 'unit:' + old['id'], 'unit:' + new_id, 'unit:' + target['id']))
        capture('card-source-and-destination', '/api/device-workflow',
                {'action': 'place_card', 'card_id': card['id'], 'unit_id': target['id'], 'slot': '2',
                 'confirmed_usable': True},
                ('placement:' + str(card['id']), 'unit:' + old['id'], 'unit:' + target['id']))
        capture('old-unit-original-number', '/api/device-workflow',
                {'action': 'unit_status', 'unit_id': old['id'], 'status': '返修待验'},
                ('device:29', 'unit:' + old['id']))
        capture('old-unit-reassignment', '/api/device-workflow',
                {'action': 'reassign_unit', 'unit_id': old['id'], 'device_id': '87', 'confirmed_usable': True},
                ('device:29', 'device:87', 'unit:' + old['id']))
        capture('account-recovery-source-target-and-case', '/api/device-workflow',
                {'action': 'account_recovery', 'account_id': 'synth-account-1', 'case_id': case_id,
                 'target_unit_id': target['id'], 'recovery_state': '待处理'},
                ('case:' + case_id, 'unit:' + old['id'], 'unit:' + target['id']))
        capture('open-case-current-number', '/api/device-workflow',
                {'action': 'open_case', 'device_id': '29', 'reason': '合成另一故障',
                 'affected_account_ids': [], 'pause_nurture': True},
                ('device:29', 'unit:' + new_id))
        self.action('account_recovery', account_id='synth-account-1', case_id=case_id,
                    recovery_state='恢复成功', target_unit_id=target['id'], confirmed_login=True)
        capture('case-retains-account-moved-to-another-phone', '/api/device-workflow',
                {'action': 'update_case', 'case_id': case_id, 'status': '处理中'},
                ('case:' + case_id, 'account:synth-account-1', 'access:synth-account-1', 'unit:' + old['id']))
        contract_file = self.folder / 'version-key-contracts.json'
        contract_file.write_text(json.dumps(contracts, ensure_ascii=False, indent=2), encoding='utf-8')

    def test_requested_carrier_options_and_other_name_round_trip(self):
        options = self.board()['phone_cards']['carriers']
        for carrier in ('中国移动', '中国联通', '中国电信', '民生电信', '长城移动', '其他'):
            self.assertIn(carrier, options)
        for number, carrier in (('19900000001', '民生电信'), ('19900000002', '长城移动')):
            self.assertEqual(self.create_card(number, carrier=carrier)['carrier'], carrier)
        card = self.create_card('19900000003', carrier='其他', custom_carrier='合成自定义运营商')
        self.assertEqual(card['carrier'], '合成自定义运营商')
        self.call('/api/phone-card', {'id': card['id'], 'phone': card['phone'],
                                    'carrier': '其他', 'custom_carrier': '合成修改运营商'})
        saved = next(row for row in self.board()['phone_cards']['cards'] if row['id'] == card['id'])
        self.assertEqual(saved['carrier'], '合成修改运营商')

    def test_workspace_summary_preserves_workflow_without_full_version_snapshot(self):
        self.create_device('29', '在用')
        unit = self.confirmed_account('29')
        case_id = self.action('open_case', unit_id=unit['id'], reason='合成摘要集成故障',
                              affected_account_ids=['synth-account-1'])['case_id']
        card = self.create_card('19900000061')
        self.action('place_card', card_id=card['id'], unit_id=unit['id'], slot='待确认',
                    confirm_existing=True, confirmed_usable=True)
        full = self.board()
        with patch.object(operations, 'versions', side_effect=AssertionError('summary computed the full snapshot')):
            work = self.call('/api/board?view=work')
            detail = self.call('/api/board?view=detail')
        self.assertEqual(work['workspace_view'], 'summary')
        self.assertEqual(work['record_versions'], {})
        self.assertNotIn('events', work['device_workflow'])
        self.assertEqual({task['kind'] for task in work['device_workflow']['todos']},
                         {'device_case', 'account_recovery'})
        summary_unit = next(row for row in work['device_workflow']['units'] if row['id'] == unit['id'])
        self.assertEqual(set(summary_unit), {'id', 'device_id', 'label'})
        account = next(row for row in work['accounts'] if row['id'] == 'synth-account-1')
        self.assertEqual(account['device_access']['login_state'], '登录受阻')
        self.assertEqual(set(account['device_access']), {'login_state', 'recovery_state'})
        self.assertEqual(detail['record_versions'], {})
        self.assertTrue(any(row['id'] == case_id for row in detail['device_workflow']['cases']))
        self.assertTrue(detail['device_workflow']['events'])
        keys = ['unit:' + unit['id'], 'case:' + case_id, 'access:synth-account-1',
                'placement:' + str(card['id'])]
        found = self.call('/api/record-versions?keys=' + ','.join(keys))['record_versions']
        self.assertEqual(found, {key: full['record_versions'][key] for key in keys})
        self.assertEqual(self.call('/api/record-versions?keys=unit:missing')['record_versions'],
                         {'unit:missing': None})
        self.reject(400, '/api/record-versions?keys=unknown:bad', None)

    def test_legacy_device_editor_cannot_overwrite_newer_physical_record(self):
        device = self.create_device('86', '可用闲置')
        earlier = self.board()['record_versions']
        self.action('unit_status', unit_id=device['current_unit']['id'], status='可用闲置', note='合成复检已完成')
        self.reject(409, '/api/phone-device', {
            'id': '86', 'state': '故障', '_expected_versions': earlier,
        })
        latest = next(row for row in self.board()['phone_devices']['devices'] if row['id'] == '86')
        self.assertEqual(latest['current_unit']['status'], '可用闲置')
        self.assertEqual(latest['current_unit']['note'], '合成复检已完成')


if __name__ == '__main__':
    unittest.main()
