"""Office-three additive dual-sheet import: synthetic data and loopback only.

Run through run_isolated_tests.py. No customer spreadsheets or databases are read.
The SQLite fixture checks transaction/scope semantics, not PostgreSQL lock load.
"""
import base64
import csv
import io
import json
import sqlite3
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from test_isolation import checked_database, checked_root

checked_root()

import app
import auth
import office_three_import
import operations
import phone_cards
import phone_devices
import test_auth_support
from sql_dialect import begin_write


DAY = '2026-10-28'
PREVIEW_PATH = '/api/workspace/office-three-import-preview'
APPLY_PATH = '/api/workspace/office-three-import-apply'
COMPUTER_SCOPES_PATH = '/api/workspace/office-three-import-computer-scopes'
ACCOUNT_HEADERS = ['来源行', '注册手机号', '昵称', '登录手机序号', '抖音号', '小号说明', '门头',
                   '负责人', '登记电脑序号', '来源类型', '来源账号状态', '违规备注', '使用状态', '注册日期']
PROGRESS_HEADERS = ['来源行', '电脑序号', '负责人', '临时负责人', '类别'] + ['发布设备' + str(i) for i in range(1, 11)]


def csv_file(headers, rows, filename='synthetic.csv'):
    stream = io.StringIO(newline='')
    csv.writer(stream).writerows([headers, *rows])
    return {'filename': filename, 'content': base64.b64encode(stream.getvalue().encode('utf-8-sig')).decode()}


def account_row(handle='synthetic_new_2', *, row=11, device='2', name='合成新增二', computer='1', status='正常'):
    return [str(row), '19900000102', name, device, handle, '', '合成门头', '合成负责人',
            computer, '大号', status, '', '合成待核对', '2026-10-01']


def progress_row(computer='1', labels=('', '2 合成新增二'), row=3):
    return [str(row), computer, '合成负责人', '', '合成类别', *labels, *([''] * (10 - len(labels)))]


class SyntheticConnection(sqlite3.Connection):
    def execute(self, statement, parameters=()):
        # BEGIN IMMEDIATE provides this sequential fixture's write lock.
        if statement.endswith(' FOR UPDATE'):
            statement = statement[:-len(' FOR UPDATE')]
        return super().execute(statement, parameters)


class OfficeThreeFixture(unittest.TestCase):
    def setUp(self):
        self.assertIsNone(app._DATABASE_PROVIDER, 'External database providers are forbidden')
        self.folder = checked_root() / ('office-three-import-' + uuid4().hex)
        self.folder.mkdir()
        database_patch = patch.object(app, 'DB', self.folder / 'board.db')
        database_patch.start()
        self.addCleanup(database_patch.stop)
        app.initialize()
        with app.connect() as db:
            migration = Path(__file__).parent / 'migrations' / 'postgresql'
            db.executescript((migration / '0002_office_scope.sql').read_text(encoding='utf-8'))
            db.executescript((migration / '0003_idempotency.sql').read_text(encoding='utf-8'))

        @contextmanager
        def synthetic_connect():
            db = sqlite3.connect(checked_database(app.DB), factory=SyntheticConnection, timeout=10)
            db.row_factory = sqlite3.Row
            db.scope_enabled = True
            db.restore_generation = 'synthetic-office-three-generation'
            db.execute('PRAGMA foreign_keys=ON')
            try:
                with db:
                    yield db
            finally:
                db.close()

        connection_patch = patch.object(app, 'connect', synthetic_connect)
        connection_patch.start()
        self.addCleanup(connection_patch.stop)
        with app.connect() as db:
            token = auth.mint_setup(db)
            auth.setup_owner(db, {'username': 'test_owner', 'name': '合成总管理员',
                                 'password': test_auth_support.TEST_PASSWORD}, 'board_setup=' + token)
            self.actor = auth.public_user(db.execute("SELECT * FROM auth_users WHERE role='owner'").fetchone())
            self.employee_id = db.execute(
                'INSERT INTO auth_users(username,name,role,permissions,password_hash,created_at) '
                'VALUES(?,?,?,?,?,?) RETURNING id',
                ('synthetic_import_employee', '合成员工', 'employee', '["data_import"]',
                 db.execute('SELECT password_hash FROM auth_users WHERE id=?', (self.actor['id'],)).fetchone()[0],
                 int(time.time())),
            ).fetchone()[0]
            db.execute("INSERT INTO offices(id,name) VALUES('three','办公室三')")
            db.execute("INSERT INTO offices(id,name) VALUES('synthetic-other','合成其他办公室')")
            db.execute("INSERT INTO computer_offices VALUES('synth-pc-1','synthetic-other')")
            db.execute("INSERT INTO account_offices VALUES('synth-account-1','synthetic-other')")
            db.execute("INSERT INTO computers VALUES('office3:1','原有三办电脑','合成旧自动化账号','合成旧IP','原负责人','原类别')")
            db.execute("INSERT INTO computer_offices VALUES('office3:1','three')")
            db.execute("INSERT INTO computers VALUES('office3:9','前缀相同但属于别处','','','','')")
            db.execute("INSERT INTO computer_offices VALUES('office3:9','synthetic-other')")
            self.insert_account(db, 'old-three', 'synthetic_reuse', '原有账号原名', 'office3:1', 1, 'three')
            self.insert_account(db, 'old-pending', 'synthetic_pending', '原有待分配', 'synth-pc-1', 2)
            db.execute("UPDATE accounts SET note='原有备注必须保留',registered_on='2026-01-02',"
                       "work_count=73,last_collected='合成历史采集',registration_phone='19900000101' WHERE id='old-three'")
            phone_devices.save(db, {'id': 'office3:1', 'name': '原有设备原名', 'state': '在用',
                                    'owner': '原设备负责人', 'note': '原实物资料', 'office_ids': ['three']},
                               create=True, actor=self.actor)
            phone_devices.assign(db, 'old-three', 'office3:1')
            db.execute("UPDATE phone_devices SET state_source='manual' WHERE id='office3:1'")
            db.execute("UPDATE phone_account_access SET login_state='可操作',confirmed_at=123,revision=7 WHERE account_id='old-three'")
            phone_cards.save(db, {'phone': '19900000101', 'label': '原有合成卡', 'owner': '原卡主',
                                  'office_ids': ['three']}, actor=self.actor)
            phone_cards.save(db, {'phone': '19900000999', 'label': '其他办公室合成卡',
                                  'office_ids': ['synthetic-other']}, actor=self.actor)
            db.execute("INSERT INTO daily VALUES('old-three','2026-10-20',1)")
            db.execute("INSERT INTO publishing_layout_state VALUES(1,'synthetic-active')")
            db.execute("INSERT INTO publishing_layout VALUES('synthetic-active','office3:1',1,'old-three',"
                       "'原有手动位置','办公室三','H3','synthetic-source','原匹配原因')")
            db.execute("INSERT INTO publishing_layout_links VALUES('synthetic-active','office3:1',1,'原手动关联理由',?,123)",
                       (self.actor['id'],))
            db.execute("INSERT INTO publishing_layout VALUES('synthetic-active','synth-pc-1',1,'synth-account-1',"
                       "'其他办公室位置','合成其他办公室','H2','synthetic-other-source','')")
            db.execute("INSERT INTO publishing_layout VALUES('synthetic-history','office3:1',1,'old-three',"
                       "'历史批次不可改','办公室三','H3','synthetic-history-source','')")

    @staticmethod
    def insert_account(db, ident, handle, name, computer, slot, office=None):
        db.execute('INSERT INTO accounts(id,computer_id,slot,name,status,note,douyin_id,homepage,last_collected) '
                   'VALUES(?,?,?,?,?,?,?,?,?)', (ident, computer, slot, name, '正常', '', handle, '', ''))
        if office:
            db.execute('INSERT INTO account_offices VALUES(?,?)', (ident, office))

    def body(self, accounts=None, progress=None):
        return {'office_id': 'three',
                'accounts_file': csv_file(ACCOUNT_HEADERS, [account_row()] if accounts is None else accounts),
                'progress_file': csv_file(PROGRESS_HEADERS, [progress_row()] if progress is None else progress)}

    def preview(self, body=None, actor=None):
        with app.connect() as db:
            begin_write(db)
            return office_three_import.preview(db, actor or self.actor, self.body() if body is None else body, DAY)

    def apply(self, preview, **extra):
        body = {'office_id': 'three', 'preview_id': preview['preview_id'], 'confirmation': '导入办公室三', **extra}
        with app.connect() as db:
            begin_write(db)
            return office_three_import.apply(db, self.actor, body, DAY)

    def seed_pending_computers(self):
        with app.connect() as db:
            for number in ('3', '4', '5'):
                db.execute('INSERT INTO computers VALUES(?,?,?,?,?,?)',
                           ('office3:' + number, '合成待分配电脑' + number, '原自动化资料', '原IP资料', '原负责人', '原类别'))
            self.insert_account(db, 'old-scope-pending', 'synthetic_scope_pending',
                                '必须保持待分配的原账号', 'office3:3', 1)
            db.execute("INSERT INTO daily VALUES('old-scope-pending','2026-10-19',1)")
            db.execute("INSERT INTO publishing_layout VALUES('synthetic-active','office3:3',1,'old-scope-pending',"
                       "'原待分配账号位置','原办公室文字','H8','synthetic-pending-source','原有原因')")
            db.execute("INSERT INTO publishing_layout_links VALUES('synthetic-active','office3:3',1,'原待分配手工理由',?,123)",
                       (self.actor['id'],))

    def computer_scope_body(self):
        return self.body(accounts=[account_row(computer='3')], progress=[
            progress_row('3'), progress_row('4', labels=(), row=4),
            progress_row('9', labels=(), row=5), progress_row('2', labels=(), row=6),
            progress_row('1', labels=(), row=7),
        ])

    def assign_computer_scopes(self, preview, ids=None, **extra):
        body = {'office_id': 'three', 'preview_id': preview['preview_id'],
                'computer_ids': ['office3:3'] if ids is None else ids,
                'confirmation': '仅分配电脑到办公室三', **extra}
        with app.connect() as db:
            begin_write(db)
            return office_three_import.assign_computer_scopes(db, self.actor, body, DAY)

    def assert_only_computer_scopes_added(self, before, approved):
        after = self.snapshot()
        expected_scopes = before['computer_offices'] + [
            {'computer_id': ident, 'office_id': 'three'} for ident in sorted(approved)]
        self.assertEqual(sorted(after['computer_offices'], key=repr), sorted(expected_scopes, key=repr))
        for table, rows in before.items():
            if table != 'computer_offices':
                with self.subTest(table=table):
                    self.assertEqual(after[table], rows, 'Only explicitly requested computer scopes may change')

    def snapshot(self):
        ignored = {'operations_previews', 'operations_history', 'idempotency_records',
                   'auth_sessions', 'auth_limits', 'auth_audit'}
        with app.connect() as db:
            tables = [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            return {table: [dict(row) for row in db.execute('SELECT * FROM "' + table + '"')]
                    for table in tables if table not in ignored}

    def assert_old_rows_preserved(self, before):
        after = self.snapshot()
        for table, rows in before.items():
            with self.subTest(table=table):
                for row in rows:
                    self.assertIn(row, after[table], 'An existing row was removed or changed: ' + table)

    def assert_clean_database(self):
        with app.connect() as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())


class OfficeThreeSourceSafetyTest(OfficeThreeFixture):
    def test_preview_does_not_mutate_business_or_persist_credentials(self):
        before = self.snapshot()
        payload = self.body()
        # Original-layout columns and all credential values are wholly synthetic.
        account_headers = [''] * 22
        for index, title in ((2, '注册抖音用的手机号'), (5, '抖音名称'), (6, '抖音在哪个手机上'),
                             (7, '抖音号'), (11, '对应电脑')):
            account_headers[index] = title
        account_headers[13] = '密码'
        raw_account = [''] * 22
        for index, value in zip((2, 5, 6, 7, 8, 9, 10, 11, 12, 15, 16, 17, 18), account_row()[1:]):
            raw_account[index] = value
        raw_account[13] = 'SYNTHETIC_SECRET_ACCOUNT'
        matrix_headers = [''] * 24
        for slot, index in enumerate((7, 8, 9, 10, 11, 12, 13, 14, 15, 17), 1):
            matrix_headers[index] = '发布设备' + str(slot)
        matrix_headers[1] = '远程登录密码 SYNTHETIC_SECRET_HEADER'
        raw_matrix = [''] * 24
        for index, value in zip((0, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 17), progress_row()[1:]):
            raw_matrix[index] = value
        raw_matrix[0] = '1号电脑'
        raw_matrix[1] = 'SYNTHETIC_SECRET_REMOTE'
        raw_matrix[2] = 'SYNTHETIC_SECRET_AUTOMATION'
        raw_matrix[3] = 'SYNTHETIC_SECRET_FIXED_IP'
        payload['accounts_file'] = csv_file(account_headers, [raw_account])
        payload['progress_file'] = csv_file(matrix_headers, [raw_matrix])
        result = self.preview(payload)
        self.assertEqual(self.snapshot(), before)
        with app.connect() as db:
            stored = '\n'.join(row[0] for row in db.execute('SELECT payload FROM operations_previews'))
        rendered = json.dumps(result, ensure_ascii=False)
        for secret in ('SYNTHETIC_SECRET_ACCOUNT', 'SYNTHETIC_SECRET_REMOTE',
                       'SYNTHETIC_SECRET_AUTOMATION', 'SYNTHETIC_SECRET_FIXED_IP', 'SYNTHETIC_SECRET_HEADER'):
            self.assertNotIn(secret, stored)
            self.assertNotIn(secret, rendered)


class OfficeThreeComputerScopeTest(OfficeThreeFixture):
    def setUp(self):
        super().setUp()
        self.seed_pending_computers()

    def test_candidates_are_only_existing_unassigned_source_computers(self):
        before = self.snapshot()
        result = self.preview(self.computer_scope_body())
        self.assertEqual(result['scope_candidates'], [{'id': 'office3:3'}, {'id': 'office3:4'}])
        self.assertEqual(self.snapshot(), before)
        # office3:5 is unassigned but absent from source; 2 is new; 9 is foreign;
        # 1 is already scoped. None can be silently included as a candidate.
        self.assertFalse({'office3:5', 'office3:2', 'office3:9', 'office3:1'} &
                         {row['id'] for row in result['scope_candidates']})

    def test_only_approved_computer_scope_changes_and_fresh_preview_can_import(self):
        payload = self.computer_scope_body()
        result = self.preview(payload)
        before = self.snapshot()
        assigned = self.assign_computer_scopes(result)
        self.assertEqual(assigned['assigned_computer_ids'], ['office3:3'])
        for key in ('business_fields_preserved', 'only_requested_computers_assigned', 'other_entity_scopes_preserved'):
            self.assertTrue(assigned['verification'][key])
        self.assert_only_computer_scopes_added(before, ['office3:3'])
        with app.connect() as db:
            self.assertIsNone(db.execute('SELECT id FROM operations_previews WHERE id=?', (result['preview_id'],)).fetchone())
            self.assertIsNone(db.execute("SELECT office_id FROM account_offices WHERE account_id='old-scope-pending'").fetchone())
        with self.assertRaises(ValueError):
            self.apply(result)
        with self.assertRaises(ValueError):
            self.assign_computer_scopes(result)
        scoped_state = self.snapshot()
        fresh = self.preview(payload)
        self.assertEqual(fresh['scope_candidates'], [{'id': 'office3:4'}])
        self.assertEqual(fresh['summary']['new_accounts'], 1)
        self.apply(fresh)
        self.assert_old_rows_preserved(scoped_state)
        with app.connect() as db:
            account = db.execute("SELECT * FROM accounts WHERE douyin_id='synthetic_new_2'").fetchone()
            self.assertEqual(account['computer_id'], 'office3:3')
            self.assertEqual(db.execute('SELECT office_id FROM account_offices WHERE account_id=?', (account['id'],)).fetchone()[0], 'three')
            self.assertIsNone(db.execute("SELECT office_id FROM account_offices WHERE account_id='old-scope-pending'").fetchone())
            self.assertIsNone(db.execute("SELECT office_id FROM computer_offices WHERE computer_id='office3:4'").fetchone())
        self.assert_clean_database()

    def test_unrequested_duplicate_and_invalid_ids_or_confirmation_are_rejected(self):
        result = self.preview(self.computer_scope_body())
        before = self.snapshot()
        invalid_ids = [[], ['office3:5'], ['office3:9'], ['office3:2'], ['office3:1'],
                       ['office3:3', 'office3:3'], ['office3:3', 'office3:5'], [True],
                       'office3:3', [None], ['office3:3'] * 101]
        for ids in invalid_ids:
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.assign_computer_scopes(result, ids)
        for extra in ({'confirmation': '导入办公室三'}, {'confirmation': ''}, {'office_id': 'synthetic-other'}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.assign_computer_scopes(result, **extra)
        self.assertEqual(self.snapshot(), before)
        with app.connect() as db:
            self.assertIsNotNone(db.execute('SELECT id FROM operations_previews WHERE id=?', (result['preview_id'],)).fetchone())

    def test_changed_guard_and_target_scope_race_are_rejected(self):
        result = self.preview(self.computer_scope_body())
        with app.connect() as db:
            db.execute("INSERT INTO computer_offices VALUES('office3:3','synthetic-other')")
        before = self.snapshot()
        with self.assertRaises(operations.ConflictError):
            self.assign_computer_scopes(result)
        self.assertEqual(self.snapshot(), before)
        with app.connect() as db:
            self.assertEqual(db.execute("SELECT office_id FROM computer_offices WHERE computer_id='office3:3'").fetchone()[0], 'synthetic-other')

    def test_mid_write_failure_rolls_back_all_assignments_and_keeps_preview(self):
        result = self.preview(self.computer_scope_body())
        before = self.snapshot()
        with app.connect() as db:
            db.execute("CREATE TRIGGER synthetic_reject_second_scope BEFORE INSERT ON computer_offices "
                       "WHEN NEW.computer_id='office3:4' BEGIN SELECT RAISE(ABORT,'synthetic scope constraint'); END")
        with self.assertRaises((ValueError, sqlite3.IntegrityError)):
            self.assign_computer_scopes(result, ['office3:3', 'office3:4'])
        self.assertEqual(self.snapshot(), before)
        with app.connect() as db:
            self.assertIsNotNone(db.execute('SELECT id FROM operations_previews WHERE id=?', (result['preview_id'],)).fetchone())

    def test_unexpected_business_mutation_during_scope_assignment_is_rolled_back(self):
        result = self.preview(self.computer_scope_body())
        before = self.snapshot()
        with app.connect() as db:
            db.execute("CREATE TRIGGER synthetic_bad_scope_side_effect AFTER INSERT ON computer_offices "
                       "WHEN NEW.computer_id='office3:3' BEGIN UPDATE accounts SET note='合成模拟意外改动' "
                       "WHERE id='old-scope-pending'; END")
        with self.assertRaises(operations.ConflictError):
            self.assign_computer_scopes(result)
        self.assertEqual(self.snapshot(), before)

    def test_fresh_owner_and_admitted_transaction_are_required_for_scope_assignment(self):
        result = self.preview(self.computer_scope_body())
        body = {'office_id': 'three', 'preview_id': result['preview_id'], 'computer_ids': ['office3:3'],
                'confirmation': '仅分配电脑到办公室三'}
        with app.connect() as db:
            self.assertFalse(db.in_transaction)
            with self.assertRaises(ValueError):
                office_three_import.assign_computer_scopes(db, self.actor, body, DAY)
        with app.connect() as db:
            db.execute("UPDATE auth_users SET role='viewer' WHERE id=?", (self.actor['id'],))
        before = self.snapshot()
        with self.assertRaises(auth.AccessError):
            self.assign_computer_scopes(result)
        self.assertEqual(self.snapshot(), before)

    def test_scope_assignment_preview_is_actor_bound_and_expires(self):
        result = self.preview(self.computer_scope_body())
        before = self.snapshot()
        with app.connect() as db:
            db.execute('UPDATE operations_previews SET actor_id=? WHERE id=?', (self.employee_id, result['preview_id']))
        with self.assertRaises(ValueError):
            self.assign_computer_scopes(result)
        with app.connect() as db:
            db.execute('UPDATE operations_previews SET actor_id=?,expires_at=1 WHERE id=?',
                       (self.actor['id'], result['preview_id']))
        with self.assertRaises(ValueError):
            self.assign_computer_scopes(result)
        self.assertEqual(self.snapshot(), before)


class OfficeThreeImportTest(OfficeThreeFixture):
    def test_safe_template_trailing_columns_are_never_persisted(self):
        payload = self.body()
        payload['accounts_file'] = csv_file(ACCOUNT_HEADERS + ['密码'], [account_row() + ['SYNTHETIC_COMPACT_SECRET']])
        payload['progress_file'] = csv_file(PROGRESS_HEADERS + ['远控备注'], [progress_row() + ['SYNTHETIC_COMPACT_REMOTE']])
        result = self.preview(payload)
        self.assertEqual(result['summary']['new_accounts'], 1)
        self.apply(result)
        with app.connect() as db:
            stored = '\n'.join(row[0] for row in db.execute('SELECT payload FROM operations_previews'))
            stored += '\n' + json.dumps(self.snapshot(), ensure_ascii=False)
        rendered = json.dumps(result, ensure_ascii=False)
        for secret in ('SYNTHETIC_COMPACT_SECRET', 'SYNTHETIC_COMPACT_REMOTE'):
            self.assertNotIn(secret, stored)
            self.assertNotIn(secret, rendered)

    def test_additive_import_reuses_identity_without_changing_any_original_row(self):
        old = account_row('synthetic_reuse', row=12, device='1', name='来源新名字不得覆盖', status='封禁')
        before = self.snapshot()
        result = self.preview(self.body(accounts=[old, account_row()]))
        self.assertEqual(result['summary']['new_accounts'], 1)
        self.assertEqual(result['summary']['reused_accounts'], 1)
        applied = self.apply(result)
        for key in ('original_records_preserved', 'other_offices_unchanged', 'active_batch_unchanged'):
            self.assertTrue(applied['verification'][key])
        self.assert_old_rows_preserved(before)
        after = self.snapshot()
        self.assertEqual(after['daily'], before['daily'], 'Progress quantities are not daily publication history')
        self.assertEqual(after['phone_cards'], before['phone_cards'], 'Registration phones do not prove physical SIM cards')
        with app.connect() as db:
            added = db.execute("SELECT * FROM accounts WHERE douyin_id='synthetic_new_2'").fetchone()
            self.assertIsNotNone(added)
            self.assertEqual(db.execute('SELECT office_id FROM account_offices WHERE account_id=?', (added['id'],)).fetchone()[0], 'three')
            self.assertEqual(db.execute('SELECT batch FROM publishing_layout_state WHERE id=1').fetchone()[0], 'synthetic-active')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM accounts WHERE douyin_id='synthetic_reuse'").fetchone()[0], 1)
            placed = db.execute("SELECT * FROM publishing_layout WHERE batch='synthetic-active' "
                                "AND computer_id='office3:1' AND slot=2").fetchone()
            self.assertIsNotNone(placed, 'A missing safe coordinate should be inserted')
            self.assertEqual(placed['account_id'], added['id'], 'Device and nickname uniquely identify the synthetic account')
            self.assertEqual(db.execute("SELECT office_id FROM phone_device_offices WHERE device_id='office3:2'").fetchone()[0], 'three')
            access = db.execute('SELECT * FROM phone_account_access WHERE account_id=?', (added['id'],)).fetchone()
            if access:
                self.assertEqual(access['login_state'], '待确认')
                self.assertEqual(access['confirmed_at'], 0)
        self.assert_clean_database()

    def test_new_computer_scope_is_three_and_only_missing_coordinates_are_added(self):
        before = self.snapshot()
        result = self.preview(self.body(accounts=[account_row(computer='2')], progress=[progress_row('2')]))
        self.assertEqual(result['summary']['new_computers'], 1)
        self.apply(result)
        self.assert_old_rows_preserved(before)
        with app.connect() as db:
            computer = db.execute("SELECT * FROM computers WHERE id='office3:2'").fetchone()
            self.assertIsNotNone(computer)
            self.assertEqual(computer['shadow_account'], '')
            self.assertEqual(computer['ip'], '')
            self.assertEqual(db.execute("SELECT office_id FROM computer_offices WHERE computer_id='office3:2'").fetchone()[0], 'three')
            self.assertTrue(db.execute("SELECT * FROM publishing_layout WHERE computer_id='office3:2' AND batch='synthetic-active'").fetchall())

    def test_foreign_and_pending_handles_are_conflicts_not_reassigned_or_duplicated(self):
        before = self.snapshot()
        result = self.preview(self.body(accounts=[account_row('synthetic_001'), account_row('synthetic_pending', row=12)]))
        self.assertEqual(result['summary']['new_accounts'], 0)
        self.assertGreaterEqual(result['summary']['conflicts'], 2)
        self.apply(result)
        self.assert_old_rows_preserved(before)
        with app.connect() as db:
            for handle in ('synthetic_001', 'synthetic_pending'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM accounts WHERE douyin_id=?', (handle,)).fetchone()[0], 1)
            self.assertIsNone(db.execute("SELECT * FROM account_offices WHERE account_id='old-pending'").fetchone())

    def test_duplicate_and_missing_handles_cannot_create_accounts(self):
        rows = [account_row(), account_row(row=12), account_row('', row=13)]
        result = self.preview(self.body(accounts=rows))
        self.assertEqual(result['summary']['new_accounts'], 0)
        self.assertGreaterEqual(result['summary']['conflicts'], 3)
        self.apply(result)
        with app.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM accounts WHERE douyin_id='synthetic_new_2'").fetchone()[0], 0)

    def test_existing_duplicate_handle_is_not_guessed_even_inside_office_three(self):
        with app.connect() as db:
            self.insert_account(db, 'old-duplicate', 'synthetic_reuse', '合成重复旧账号', 'office3:1', 2, 'three')
        before = self.snapshot()
        result = self.preview(self.body(accounts=[account_row('synthetic_reuse')]))
        self.assertEqual(result['summary']['new_accounts'], 0)
        self.assertEqual(result['summary']['reused_accounts'], 0)
        self.assertGreater(result['summary']['conflicts'], 0)
        self.apply(result)
        self.assert_old_rows_preserved(before)

    def test_auxiliary_and_multi_open_labels_cannot_collapse_to_main_account(self):
        for label in ('2-合成新增二（小号）', '2小号-合成新增二', '2多开-合成新增二'):
            with self.subTest(label=label):
                before = self.snapshot()
                result = self.preview(self.body(progress=[progress_row(labels=('', label))]))
                self.assertEqual(result['summary']['new_layouts'], 0)
                self.assertGreater(result['summary']['conflicts'], 0)
                self.apply(result)
                self.assert_old_rows_preserved(before)
                with app.connect() as db:
                    self.assertIsNone(db.execute("SELECT * FROM publishing_layout WHERE batch='synthetic-active' "
                                                 "AND computer_id='office3:1' AND slot=2").fetchone())

    def test_suffixed_source_device_cannot_match_plain_main_account_label(self):
        for device in ('2小号', '2多开'):
            with self.subTest(device=device):
                before = self.snapshot()
                result = self.preview(self.body(accounts=[account_row(device=device)],
                                                progress=[progress_row(labels=('', '2-合成新增二'))]))
                self.assertEqual(result['summary']['new_layouts'], 0)
                self.assertGreater(result['summary']['conflicts'], 0)
                self.apply(result)
                self.assert_old_rows_preserved(before)
                with app.connect() as db:
                    account = db.execute("SELECT * FROM accounts WHERE douyin_id='synthetic_new_2'").fetchone()
                    self.assertIsNotNone(account, 'A documentary account can still be retained without guessing publishing identity')
                    self.assertIsNone(db.execute("SELECT * FROM publishing_layout WHERE batch='synthetic-active' "
                                                 "AND computer_id='office3:1' AND slot=2").fetchone())

    def test_complete_parenthesized_nickname_matches_only_when_the_whole_name_matches(self):
        name = '合成新增二（分店）'
        before = self.snapshot()
        result = self.preview(self.body(accounts=[account_row(name=name)],
                                        progress=[progress_row(labels=('', '2-' + name))]))
        self.assertEqual(result['summary']['new_layouts'], 1)
        self.apply(result)
        self.assert_old_rows_preserved(before)
        with app.connect() as db:
            account = db.execute("SELECT id FROM accounts WHERE douyin_id='synthetic_new_2'").fetchone()
            cell = db.execute("SELECT * FROM publishing_layout WHERE batch='synthetic-active' "
                              "AND computer_id='office3:1' AND slot=2").fetchone()
            self.assertEqual(cell['account_id'], account['id'])
            self.assertEqual(cell['label'], '2-' + name)

    def test_explicit_handle_can_identify_new_source_with_auxiliary_phone_suffix(self):
        result = self.preview(self.body(accounts=[account_row(device='2小号')],
                                        progress=[progress_row(labels=('', '抖音号:synthetic_new_2'))]))
        self.assertEqual(result['summary']['new_layouts'], 1)
        self.apply(result)
        with app.connect() as db:
            account = db.execute("SELECT id FROM accounts WHERE douyin_id='synthetic_new_2'").fetchone()
            cell = db.execute("SELECT * FROM publishing_layout WHERE batch='synthetic-active' "
                              "AND computer_id='office3:1' AND slot=2").fetchone()
            self.assertEqual(cell['account_id'], account['id'])

    def test_existing_device_and_physical_unit_are_unchanged_by_new_documentary_reference(self):
        before = self.snapshot()
        result = self.preview(self.body(accounts=[account_row(device='1')], progress=[progress_row(labels=())]))
        self.apply(result)
        self.assert_old_rows_preserved(before)
        with app.connect() as db:
            account = db.execute("SELECT * FROM accounts WHERE douyin_id='synthetic_new_2'").fetchone()
            self.assertIsNotNone(account)
            access = db.execute('SELECT * FROM phone_account_access WHERE account_id=?', (account['id'],)).fetchone()
            if access:
                self.assertIsNone(access['last_unit_id'], 'Source device number is not proof of the physical logged-in unit')
                self.assertEqual(access['login_state'], '待确认')

    def test_prefix_does_not_override_existing_computer_scope(self):
        before = self.snapshot()
        result = self.preview(self.body(accounts=[account_row(computer='9')], progress=[progress_row('9')]))
        self.assertGreater(result['summary']['conflicts'], 0)
        self.apply(result)
        self.assert_old_rows_preserved(before)
        with app.connect() as db:
            self.assertEqual(db.execute("SELECT office_id FROM computer_offices WHERE computer_id='office3:9'").fetchone()[0], 'synthetic-other')
            self.assertFalse(db.execute("SELECT * FROM publishing_layout WHERE batch='synthetic-active' AND computer_id='office3:9'").fetchall())

    def test_active_layout_and_manual_link_are_not_overwritten(self):
        before = self.snapshot()
        result = self.preview(self.body(progress=[progress_row(labels=('2 合成新增二',))]))
        self.assertGreater(result['summary']['conflicts'], 0)
        self.apply(result)
        self.assert_old_rows_preserved(before)
        with app.connect() as db:
            self.assertEqual(db.execute("SELECT batch FROM publishing_layout_state WHERE id=1").fetchone()[0], 'synthetic-active')

    def test_repeat_apply_is_rejected_and_preview_is_consumed(self):
        result = self.preview()
        self.apply(result)
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.apply(result)
        self.assertEqual(self.snapshot(), before)

    def test_expired_and_actor_bound_previews_cannot_apply(self):
        result = self.preview()
        with app.connect() as db:
            # Even the owner cannot apply a preview stored for a different actor.
            db.execute('UPDATE operations_previews SET actor_id=? WHERE id=?', (self.employee_id, result['preview_id']))
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.apply(result)
        with app.connect() as db:
            db.execute('UPDATE operations_previews SET actor_id=?,expires_at=1 WHERE id=?', (self.actor['id'], result['preview_id']))
        with self.assertRaises(ValueError):
            self.apply(result)
        self.assertEqual(self.snapshot(), before)

    def test_database_change_after_preview_requires_new_preview(self):
        result = self.preview()
        with app.connect() as db:
            db.execute("UPDATE accounts SET note='合成并发更新' WHERE id='old-three'")
        before = self.snapshot()
        with self.assertRaises(operations.ConflictError):
            self.apply(result)
        self.assertEqual(self.snapshot(), before)

    def test_manual_link_and_active_batch_change_invalidate_preview(self):
        for statement in ("UPDATE publishing_layout_links SET previous_reason='合成并发关联变更'",
                          "UPDATE publishing_layout_state SET batch='synthetic-history'"):
            with self.subTest(statement=statement):
                result = self.preview()
                with app.connect() as db:
                    db.execute(statement)
                before = self.snapshot()
                with self.assertRaises(operations.ConflictError):
                    self.apply(result)
                self.assertEqual(self.snapshot(), before)

    def test_unexpected_existing_row_mutation_is_detected_and_rolled_back(self):
        result = self.preview()
        before = self.snapshot()
        real_save = phone_devices.save

        def accidental_mutation(db, *args, **kwargs):
            saved = real_save(db, *args, **kwargs)
            db.execute("UPDATE phone_devices SET owner='合成模拟回归错误' WHERE id='office3:1'")
            return saved

        with patch.object(phone_devices, 'save', side_effect=accidental_mutation):
            with self.assertRaises(operations.ConflictError):
                self.apply(result)
        self.assertEqual(self.snapshot(), before)

    def test_source_does_not_initialize_or_switch_a_missing_active_batch(self):
        with app.connect() as db:
            db.execute('DELETE FROM publishing_layout_state')
        before = self.snapshot()
        result = self.preview()
        self.assertEqual(result['summary']['new_layouts'], 0)
        self.assertGreater(result['summary']['conflicts'], 0)
        self.apply(result)
        self.assert_old_rows_preserved(before)
        with app.connect() as db:
            self.assertIsNone(db.execute('SELECT * FROM publishing_layout_state').fetchone())

    def test_apply_failure_rolls_back_all_new_rows_and_keeps_preview(self):
        result = self.preview()
        before = self.snapshot()
        with app.connect() as db:
            # A test-only constraint fails late, after account/device writes.
            db.execute("CREATE TRIGGER synthetic_reject_layout BEFORE INSERT ON publishing_layout "
                       "BEGIN SELECT RAISE(ABORT,'synthetic layout constraint'); END")
        with self.assertRaises((ValueError, sqlite3.IntegrityError)):
            self.apply(result)
        self.assertEqual(self.snapshot(), before)
        with app.connect() as db:
            self.assertIsNotNone(db.execute('SELECT id FROM operations_previews WHERE id=?', (result['preview_id'],)).fetchone())

    def test_target_office_confirmation_and_fresh_owner_are_required(self):
        for value in ('synthetic-other', 'pending', 'all', '', None):
            with self.subTest(office=value), self.assertRaises(ValueError):
                self.preview({**self.body(), 'office_id': value})
        result = self.preview()
        with self.assertRaises(ValueError):
            self.apply(result, confirmation='同意')
        with self.assertRaises(ValueError):
            self.apply(result, office_id='synthetic-other')
        with app.connect() as db:
            db.execute("UPDATE auth_users SET role='viewer' WHERE id=?", (self.actor['id'],))
        with self.assertRaises(auth.AccessError):
            self.apply(result)

    def test_services_refuse_a_non_admitted_connection(self):
        with app.connect() as db:
            self.assertFalse(db.in_transaction)
            with self.assertRaises(ValueError):
                office_three_import.preview(db, self.actor, self.body(), DAY)
        result = self.preview()
        with app.connect() as db:
            self.assertFalse(db.in_transaction)
            with self.assertRaises(ValueError):
                office_three_import.apply(db, self.actor, {'office_id': 'three', 'preview_id': result['preview_id'],
                                                          'confirmation': '导入办公室三'}, DAY)

    def test_malformed_oversized_wrong_header_and_formula_files_are_rejected(self):
        invalid = [
            {'filename': 'synthetic.csv', 'content': '!invalid-base64!'},
            {'filename': 'synthetic.csv', 'content': base64.b64encode(b'x' * (8 * 1024 * 1024 + 1)).decode()},
            csv_file(['unrecognized'], [['synthetic']]),
            {'filename': 'synthetic.xls', 'content': base64.b64encode(b'synthetic').decode()},
        ]
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('xl/worksheets/sheet1.xml',
                             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                             '<sheetData><row r="1"><c r="A1"><f>1+1</f><v>2</v></c></row></sheetData></worksheet>')
        invalid.append({'filename': 'synthetic.xlsx', 'content': base64.b64encode(stream.getvalue()).decode()})
        before = self.snapshot()
        for source in invalid:
            with self.subTest(filename=source['filename']), self.assertRaises(ValueError):
                self.preview({**self.body(), 'accounts_file': source})
        self.assertEqual(self.snapshot(), before)


class OfficeThreeImportHTTPTest(OfficeThreeFixture):
    def setUp(self):
        super().setUp()

        class QuietHandler(app.Handler):
            def log_message(self, *args):
                pass

        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.owner = test_auth_support.client(self.base)
        self.employee = test_auth_support.client(self.base, 'synthetic_import_employee')

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())

    def call(self, path, body, client=None, headers=None):
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                         headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            response = (client or self.owner).open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, json.load(response)

    def test_routes_require_owner_and_session_not_only_import_permission(self):
        before = self.snapshot()
        for client, expected in ((urllib.request.build_opener(), 401), (self.employee, 403)):
            for route, body in ((PREVIEW_PATH, self.body()), (APPLY_PATH, {'office_id': 'three', 'request_id': str(uuid4())})):
                with self.subTest(route=route, expected=expected):
                    self.assertEqual(self.call(route, body, client)[0], expected)
        self.assertEqual(self.snapshot(), before)

    def test_csrf_and_origin_rejection_prevent_preview_and_apply(self):
        status, result = self.call(PREVIEW_PATH, self.body())
        self.assertEqual(status, 200, result)
        apply_body = {'office_id': 'three', 'preview_id': result['preview_id'],
                      'confirmation': '导入办公室三', 'request_id': str(uuid4())}
        before = self.snapshot()
        headers = self.owner.addheaders
        self.owner.addheaders = []
        for path, body in ((PREVIEW_PATH, self.body()), (APPLY_PATH, apply_body)):
            self.assertEqual(self.call(path, body)[0], 403)
        self.owner.addheaders = headers
        for path, body in ((PREVIEW_PATH, self.body()), (APPLY_PATH, apply_body)):
            self.assertEqual(self.call(path, body, headers={'Origin': 'http://synthetic.invalid'})[0], 403)
        self.assertEqual(self.snapshot(), before)

    def test_request_uuid_required_and_success_replays_once(self):
        status, result = self.call(PREVIEW_PATH, self.body())
        self.assertEqual(status, 200, result)
        apply_body = {'office_id': 'three', 'preview_id': result['preview_id'], 'confirmation': '导入办公室三',
                      '_expected_versions': {}}
        self.assertEqual(self.call(APPLY_PATH, apply_body)[0], 400)
        self.assertEqual(self.call(APPLY_PATH, {**apply_body, 'request_id': 'synthetic-invalid'})[0], 400)
        apply_body['request_id'] = str(uuid4())
        before = self.snapshot()
        first = self.call(APPLY_PATH, apply_body)
        self.assertEqual(first[0], 200, first)
        after = self.snapshot()
        self.assert_old_rows_preserved(before)
        second = self.call(APPLY_PATH, apply_body)
        self.assertEqual(second, first)
        self.assertEqual(self.snapshot(), after)
        reused = self.call(APPLY_PATH, {**apply_body, 'confirmation': '其他内容'})
        self.assertEqual(reused[0], 409, reused)
        self.assertEqual(self.snapshot(), after)
        self.assert_clean_database()

    def test_computer_scope_route_enforces_owner_csrf_origin_and_uuid(self):
        self.seed_pending_computers()
        status, result = self.call(PREVIEW_PATH, self.computer_scope_body())
        self.assertEqual(status, 200, result)
        body = {'office_id': 'three', 'preview_id': result['preview_id'], 'computer_ids': ['office3:3'],
                'confirmation': '仅分配电脑到办公室三', '_expected_versions': {}}
        before = self.snapshot()
        for client, expected in ((urllib.request.build_opener(), 401), (self.employee, 403)):
            self.assertEqual(self.call(COMPUTER_SCOPES_PATH, {**body, 'request_id': str(uuid4())}, client)[0], expected)
        headers = self.owner.addheaders
        self.owner.addheaders = []
        try:
            self.assertEqual(self.call(COMPUTER_SCOPES_PATH, {**body, 'request_id': str(uuid4())})[0], 403)
        finally:
            self.owner.addheaders = headers
        self.assertEqual(self.call(COMPUTER_SCOPES_PATH, {**body, 'request_id': str(uuid4())},
                                   headers={'Origin': 'http://synthetic.invalid'})[0], 403)
        self.assertEqual(self.call(COMPUTER_SCOPES_PATH, body)[0], 400)
        self.assertEqual(self.call(COMPUTER_SCOPES_PATH, {**body, 'request_id': 'synthetic-invalid'})[0], 400)
        self.assertEqual(self.snapshot(), before)

    def test_computer_scope_http_replay_writes_once_and_preserves_every_business_field(self):
        self.seed_pending_computers()
        status, result = self.call(PREVIEW_PATH, self.computer_scope_body())
        self.assertEqual(status, 200, result)
        body = {'office_id': 'three', 'preview_id': result['preview_id'], 'computer_ids': ['office3:3'],
                'confirmation': '仅分配电脑到办公室三', 'request_id': str(uuid4()), '_expected_versions': {}}
        before = self.snapshot()
        first = self.call(COMPUTER_SCOPES_PATH, body)
        self.assertEqual(first[0], 200, first)
        self.assertEqual(first[1]['assigned_computer_ids'], ['office3:3'])
        self.assertTrue(first[1]['requires_new_preview'])
        self.assert_only_computer_scopes_added(before, ['office3:3'])
        after = self.snapshot()
        self.assertEqual(self.call(COMPUTER_SCOPES_PATH, body), first)
        self.assertEqual(self.snapshot(), after)
        self.assertEqual(self.call(COMPUTER_SCOPES_PATH, {**body, 'computer_ids': ['office3:4']})[0], 409)
        self.assertEqual(self.snapshot(), after)
        self.assert_clean_database()


if __name__ == '__main__':
    unittest.main()
