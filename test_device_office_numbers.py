"""Device office/number contracts, using only fresh isolated synthetic fixtures."""
import json
import sqlite3
import threading
import unittest
import urllib.error
from uuid import uuid4

from test_isolation import checked_root

checked_root()  # Fail closed before importing application/runtime helpers.

import app
import auth
import device_workflow
import office_scope
import phone_devices
import test_auth_support
import test_device_workflow as fixtures


class DeviceOfficeNumbersTest(unittest.TestCase):
    # Reuse the already reviewed isolated HTTP setup, not its behavioral tests.
    stop_server = fixtures.DeviceWorkflowTest.stop_server
    call = fixtures.DeviceWorkflowTest.call
    board = fixtures.DeviceWorkflowTest.board
    action = fixtures.DeviceWorkflowTest.action
    reject = fixtures.DeviceWorkflowTest.reject
    reject_action = fixtures.DeviceWorkflowTest.reject_action
    device = fixtures.DeviceWorkflowTest.device
    card = fixtures.DeviceWorkflowTest.card
    account = fixtures.DeviceWorkflowTest.account
    create_card = fixtures.DeviceWorkflowTest.create_card
    enable_extended_database = fixtures.DeviceWorkflowTest.enable_extended_database

    def setUp(self):
        fixtures.DeviceWorkflowTest.setUp(self)
        self.enable_extended_database(scope=True, idempotency=True)
        with app.connect() as db:
            for ident, name in [('one', '合成办公室一'), ('two', '合成办公室二'), ('three', '合成办公室三')]:
                db.execute('INSERT INTO offices(id,name) VALUES(?,?)', (ident, name))
            db.execute("INSERT INTO account_offices VALUES('synth-account-1','one')")
            db.execute("INSERT INTO computer_offices VALUES('synth-pc-1','one')")

    def create(self, number, office='one', **fields):
        result = self.call('/api/phone-device/create', dict(number=number, office_ids=[office], **fields))
        return self.device(result['id'])

    def legacy(self, ident, offices, state='在用'):
        with app.connect() as db:
            db.execute("INSERT INTO phone_devices(id,name,state,state_source) VALUES(?,?,?,'manual')",
                       (ident, '合成旧设备 ' + ident, state))
            for office in offices:
                db.execute('INSERT INTO phone_device_offices VALUES(?,?)', (ident, office))
            device_workflow.ensure_unit(db, ident)
        return self.device(ident)

    def business_snapshot(self):
        with app.connect() as db:
            return {table: [dict(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY 1')]
                    for table in ('phone_devices', 'phone_device_offices', 'phone_units', 'accounts',
                                  'phone_cards', 'phone_card_placements', 'phone_account_access',
                                  'device_cases', 'device_workflow_events')}

    def test_generated_id_and_explicit_offices_allow_equal_numbers(self):
        one = self.create('000029', 'one')
        two = self.create('29', 'two')
        self.assertNotEqual(one['id'], two['id'])
        self.assertRegex(one['id'], r'^phone:[a-f0-9]{32}:29$')
        self.assertEqual((one['number'], one['office_id'], one['office_ids']), ('29', 'one', ['one']))
        self.assertEqual((two['number'], two['office_id']), ('29', 'two'))
        self.assertIn('合成办公室一', one['name'])
        self.assertEqual(phone_devices.number_from_id('office3:000029'), '29')
        self.assertEqual(phone_devices.number_from_id(one['id']), '29')
        self.assertIsNone(phone_devices.number_from_id('苹果29'))
        self.assertIsNone(phone_devices.number_from_id('phone:not-a-uuid:29'))

    def test_same_office_rejects_normalized_numbers_and_aliases(self):
        self.create('29')
        before = self.business_snapshot()
        for body in [dict(number='000029', office_ids=['one']),
                     dict(id='29', office_ids=['one']),
                     dict(id='office3:029', office_ids=['one']),
                     dict(id=phone_devices.new_identity('29'), number='29', office_ids=['one'])]:
            self.reject(400, '/api/phone-device/create', body, text='已经有29号手机')
            self.assertEqual(self.business_snapshot(), before)

    def test_legacy_assigned_and_shared_devices_reserve_their_existing_offices(self):
        old = self.legacy('office3:29', ['one', 'three'])
        before = self.business_snapshot()
        for office in ('one', 'three'):
            self.reject(400, '/api/phone-device/create', dict(number='29', office_ids=[office]))
        self.assertEqual(self.business_snapshot(), before)
        created = self.create('29', 'two')
        self.assertNotEqual(created['id'], old['id'])
        self.assertEqual(self.device(old['id'])['office_ids'], ['one', 'three'])
        self.assertEqual(self.device(old['id'])['identity_state'], '归属或序号待核对')

    def test_unassigned_legacy_records_are_not_guessed_or_rewritten(self):
        old = self.legacy('29', [])
        current = self.create('29', 'one')
        self.assertNotEqual(current['id'], old['id'])
        self.assertEqual(self.device('29')['office_ids'], [])
        self.call('/api/phone-device', dict(id='29', owner='合成待核对负责人'))
        self.assertEqual(self.device('29')['office_ids'], [])
        self.reject(400, '/api/phone-device', dict(id='29', office_ids=['one']), text='已经有29号手机')
        self.assertEqual(self.device('29')['owner'], '合成待核对负责人')

    def test_new_devices_require_exactly_one_valid_office_even_for_owner(self):
        before = self.business_snapshot()
        for fields in ({}, {'office_ids': []}, {'office_ids': ['one', 'two']},
                       {'office_ids': ['missing']}, {'office_ids': ['one', 'one']}):
            self.reject(400, '/api/phone-device/create', dict(number='29', **fields))
        self.assertEqual(self.business_snapshot(), before)

    def test_scope_reassignment_checks_duplicates_before_editing_fields_or_mapping(self):
        one = self.create('29', 'one')
        two = self.create('29', 'two')
        before = self.business_snapshot()
        self.reject(400, '/api/phone-device', dict(id=one['id'], office_ids=['two'], owner='不能留下的改动'))
        self.assertEqual(self.business_snapshot(), before)
        self.reject(400, '/api/auth/entity-scope', dict(kind='devices', id=one['id'], office_ids=['two'],
                    request_id=str(uuid4()), expected_scope=office_scope.scope_version('devices', one['id'], ['one'])),
                    text='已经有29号手机')
        self.assertEqual(self.business_snapshot(), before)
        self.call('/api/auth/entity-scope', dict(kind='devices', id=one['id'], office_ids=['three'],
                  request_id=str(uuid4()), expected_scope=office_scope.scope_version('devices', one['id'], ['one'])))
        self.assertEqual(self.device(one['id'])['office_ids'], ['three'])
        self.assertEqual(self.device(two['id'])['office_ids'], ['two'])
        # Moving an opaque identity does not reserve the source office forever.
        self.assertNotEqual(self.create('29', 'one')['id'], one['id'])

    def test_explicit_scope_cannot_clear_or_share_an_assigned_device(self):
        current = self.create('29')
        before = self.business_snapshot()
        for wanted in ([], ['one', 'two']):
            self.reject(400, '/api/phone-device', dict(id=current['id'], office_ids=wanted))
        self.assertEqual(self.business_snapshot(), before)
        shared = self.legacy('office3:88', ['one', 'two'])
        self.call('/api/phone-device', dict(id=shared['id'], office_ids=['two', 'one'], owner='保留旧共享待核对'))
        self.assertEqual(self.device(shared['id'])['office_ids'], ['one', 'two'])
        self.assertEqual(self.device(shared['id'])['owner'], '保留旧共享待核对')

    def test_old_identity_creation_is_compatible_but_edit_cannot_renumber_it(self):
        result = self.call('/api/phone-device/create', dict(id='office3:000029', office_ids=['one']))
        self.assertEqual(result['id'], 'office3:29')
        before = self.business_snapshot()
        self.reject(400, '/api/phone-device', dict(id='office3:29', number='30', owner='不能保存'))
        self.assertEqual(self.business_snapshot(), before)
        self.call('/api/phone-device', dict(id='office3:29', number='000029', owner='可保存'))
        self.assertEqual(self.device('office3:29')['owner'], '可保存')
        self.reject(400, '/api/phone-device/create', dict(id='office3:31', number='32', office_ids=['one']))

    def test_employee_cannot_create_or_move_a_device_outside_their_office(self):
        with app.connect() as db:
            actor = auth.public_user(db.execute("SELECT * FROM auth_users WHERE role='owner'").fetchone())
            auth.save_user(db, actor, dict(username='synthetic_device_worker', name='合成手机员工', role='employee',
                           permissions=['device_create', 'device_manage'], password=test_auth_support.TEST_PASSWORD,
                           office_ids=['one']))
        worker = test_auth_support.client(self.base, 'synthetic_device_worker')
        before = self.business_snapshot()
        self.reject(403, '/api/phone-device/create', dict(number='29', office_ids=['two']), worker)
        self.assertEqual(self.business_snapshot(), before)
        result = self.call('/api/phone-device/create', dict(number='29', office_ids=['one']), worker)
        current = self.device(result['id'])
        before = self.business_snapshot()
        self.reject(403, '/api/phone-device', dict(id=current['id'], office_ids=['two']), worker)
        self.assertEqual(self.business_snapshot(), before)

    def test_idempotent_create_replays_one_identity_and_rejects_changed_content(self):
        body = dict(number='29', office_ids=['one'], request_id=str(uuid4()),
                    _expected_versions=self.board()['record_versions'])
        first = self.call('/api/phone-device/create', body)
        before = self.business_snapshot()
        again = self.call('/api/phone-device/create', body)
        self.assertEqual(first, again)
        self.assertEqual(self.business_snapshot(), before)
        self.reject(409, '/api/phone-device/create', {**body, 'number': '30'})
        self.assertEqual(self.business_snapshot(), before)

    def test_two_serialized_connections_cannot_create_the_same_office_number(self):
        # This exercises SQLite's real BEGIN IMMEDIATE admission. Production PG
        # uses its already-existing runtime_control row lock for the same gate.
        start = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()
        def create_one():
            start.wait(timeout=5)
            try:
                with app.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    actor = auth.public_user(db.execute("SELECT * FROM auth_users WHERE role='owner'").fetchone())
                    ident = phone_devices.save(db, dict(number='29', office_ids=['one']), create=True, actor=actor)
                    result = ('created', ident)
            except ValueError as error:
                result = ('rejected', str(error))
            with lock:outcomes.append(result)
        workers = [threading.Thread(target=create_one) for _ in range(2)]
        for worker in workers:worker.start()
        for worker in workers:worker.join(timeout=10)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(sorted(row[0] for row in outcomes), ['created', 'rejected'])
        with app.connect() as db:
            self.assertEqual(len(phone_devices.office_number_devices(db, 'one', '000029')), 1)

    def test_repaired_unit_inherits_office_but_preserves_all_physical_relationships(self):
        old_device = self.create('29', 'one', state='在用')
        foreign = self.create('87', 'two', state='可用闲置')
        old = old_device['current_unit']
        self.call('/api/account/phone-device', dict(id='synth-account-1', phone_device_id=old_device['id']))
        card = self.create_card('19900000087', office_ids=['one'])
        self.action('place_card', card_id=card['id'], unit_id=old['id'], slot='待确认', confirm_existing=True)
        self.action('unit_status', unit_id=old['id'], status='故障')
        replacement = self.action('replace_unit', device_id=old_device['id'])['unit_id']
        self.action('unit_status', unit_id=old['id'], status='返修待验')
        with app.connect() as db:
            keys = device_workflow.version_keys(db, dict(action='reassign_unit', unit_id=old['id'], number='87'))
        self.assertNotIn('device:87', keys)
        self.assertIn('device:' + old_device['id'], keys)
        result = self.action('reassign_unit', unit_id=old['id'], number='87', confirmed_usable=True)
        new = self.device(result['device_id'])
        self.assertEqual(new['number'], '87')
        self.assertEqual(new['office_ids'], ['one'])
        self.assertEqual(new['current_unit']['id'], old['id'])
        self.assertEqual(new['current_unit']['original_device_id'], old_device['id'])
        self.assertEqual(self.device(old_device['id'])['current_unit']['id'], replacement)
        self.assertEqual(self.card(card['id'])['placement']['unit_id'], old['id'])
        self.assertEqual(self.account()['phone_device_id'], new['id'])
        self.assertEqual(self.account()['device_access']['last_unit_id'], old['id'])
        self.assertEqual(self.device(foreign['id']), foreign)
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM phone_units WHERE device_id=?', (new['id'],)).fetchone()[0], 1)
            self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())

    def test_repaired_unit_rejects_same_office_duplicate_and_unconfirmed_source_office(self):
        original = self.create('29', 'one', state='故障')
        self.create('87', 'one')
        self.action('replace_unit', device_id=original['id'])
        before = self.business_snapshot()
        self.reject_action(400, 'reassign_unit', unit_id=original['current_unit']['id'], number='000087', confirmed_usable=True)
        self.assertEqual(self.business_snapshot(), before)
        shared = self.legacy('office3:88', ['one', 'two'], state='故障')
        self.action('replace_unit', device_id=shared['id'])
        before = self.business_snapshot()
        self.reject_action(400, 'reassign_unit', unit_id=shared['current_unit']['id'], number='89', confirmed_usable=True)
        self.assertEqual(self.business_snapshot(), before)

    def test_uuid_phone_account_publishing_uses_real_offices_not_legacy_batch(self):
        three = self.create('29', 'three')
        one = self.create('29', 'one')
        self.call('/api/account/phone-device', dict(id='synth-account-1', phone_device_id=three['id']))
        with app.connect() as db:
            db.execute("UPDATE account_offices SET office_id='three' WHERE account_id='synth-account-1'")
            db.execute("UPDATE accounts SET office_group='' WHERE id='synth-account-1'")
            db.execute("INSERT INTO accounts(id,computer_id,slot,name,status,phone_device_id,office_group) VALUES('synth-other-office','synth-pc-1',2,'合成异办同号账号','正常',?,'')", (one['id'],))
            db.execute("INSERT INTO account_offices VALUES('synth-other-office','one')")
            db.execute("INSERT INTO computers(id,name) VALUES('office3:synthetic-number-contract','合成三办发布电脑')")
            db.execute("INSERT INTO computer_offices VALUES('office3:synthetic-number-contract','three')")
            db.execute("INSERT INTO publishing_layout_state(id,batch) VALUES(1,'synthetic-number-contract') ON CONFLICT(id) DO UPDATE SET batch=excluded.batch")
            for slot in (1, 2):
                db.execute("INSERT INTO publishing_layout VALUES('synthetic-number-contract','office3:synthetic-number-contract',?,NULL,'合成待匹配','显示文字不作权限来源',?,'synthetic-only','合成待核对')", (slot, 'A' + str(slot)))
        common = dict(batch='synthetic-number-contract', computer_id='office3:synthetic-number-contract',
                      expected_account_id=None, action='link')
        self.call('/api/publishing/link', dict(**common, slot=1, account_id='synth-account-1'))
        self.reject(400, '/api/publishing/link', dict(**common, slot=2, account_id='synth-other-office'))
        with app.connect() as db:
            self.assertEqual(db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-number-contract' AND slot=1").fetchone()[0], 'synth-account-1')
            self.assertIsNone(db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-number-contract' AND slot=2").fetchone()[0])
            self.assertEqual(db.execute("SELECT office_group FROM accounts WHERE id='synth-account-1'").fetchone()[0], '')


if __name__ == '__main__':
    unittest.main()
