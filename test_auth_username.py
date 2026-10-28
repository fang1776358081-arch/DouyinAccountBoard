"""Owner self-rename checks: synthetic SQLite and loopback HTTP only.

Run through run_isolated_tests.py. This module deliberately refuses to import
the runtime until the dedicated synthetic root has been validated.
"""
import json
import sqlite3
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from test_isolation import checked_database, checked_root

checked_root()

import app
import auth
import phone_cards
import phone_devices
import test_auth_support


class SyntheticConnection(sqlite3.Connection):
    """Adds only the scope capability flag used by the production adapter."""


class UsernameChangeTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNone(app._DATABASE_PROVIDER, 'External providers are forbidden in this fixture')
        self.folder = checked_root() / ('auth-username-' + uuid4().hex)
        self.folder.mkdir()
        self.database_patch = patch.object(app, 'DB', self.folder / 'board.db')
        self.database_patch.start()
        self.addCleanup(self.database_patch.stop)
        app.initialize()
        with app.connect() as db:
            migration = Path(__file__).parent / 'migrations' / 'postgresql' / '0002_office_scope.sql'
            db.executescript(migration.read_text(encoding='utf-8'))

        @contextmanager
        def synthetic_connect():
            database = checked_database(app.DB)
            db = sqlite3.connect(database, factory=SyntheticConnection, timeout=10)
            db.row_factory = sqlite3.Row
            db.scope_enabled = True
            db.execute('PRAGMA foreign_keys=ON')
            try:
                with db:
                    yield db
            finally:
                db.close()

        connection_patch = patch.object(app, 'connect', synthetic_connect)
        connection_patch.start()
        self.addCleanup(connection_patch.stop)

        class QuietHandler(app.Handler):
            def log_message(self, *args):
                pass

        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.owner = test_auth_support.owner_client(app, self.base)
        with app.connect() as db:
            owner = db.execute("SELECT * FROM auth_users WHERE role='owner'").fetchone()
            self.owner_id = owner['id']
            # Reuse the generated, test-only hash, never customer-derived fixtures.
            self.other_id = db.execute(
                'INSERT INTO auth_users(username,name,role,permissions,password_hash,created_at) '
                'VALUES(?,?,?,?,?,?) RETURNING id',
                ('synthetic_employee', '合成核验员工', 'employee', '["publish"]',
                 owner['password_hash'], int(time.time())),
            ).fetchone()[0]
            db.execute("INSERT INTO offices(id,name) VALUES('synth-office-rename','合成改名办公室')")
            db.execute("INSERT INTO user_office_grants VALUES(?,'synth-office-rename')", (self.owner_id,))
            db.execute("INSERT INTO user_office_grants VALUES(?,'synth-office-rename')", (self.other_id,))
            db.execute("INSERT INTO computer_offices VALUES('synth-pc-1','synth-office-rename')")
            db.execute("INSERT INTO account_offices VALUES('synth-account-1','synth-office-rename')")
            actor = auth.public_user(owner)
            # Normal fixture writes remain within this newly created synthetic DB.
            card = phone_cards.save(db, {
                'phone': '19900000901', 'label': '合成改名核验卡', 'owner': '合成卡主',
                'carrier': '民生电信', 'office_ids': ['synth-office-rename'],
            }, actor=actor)
            phone_devices.save(db, {
                'id': '9901', 'name': '合成改名核验手机', 'state': '在用',
                'office_ids': ['synth-office-rename'],
            }, create=True, actor=actor)
            phone_devices.assign(db, 'synth-account-1', '9901')
            db.execute("UPDATE accounts SET registration_phone='19900000901' WHERE id='synth-account-1'")
            db.execute("INSERT INTO daily(account_id,day,published) VALUES('synth-account-1','2026-10-21',1)")
            self.assertIsInstance(card, int)
        self.other = test_auth_support.client(self.base, 'synthetic_employee')

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())

    def call(self, path, body=None, client=None, headers=None):
        client = self.owner if client is None else client
        request = urllib.request.Request(
            self.base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Content-Type': 'application/json', **(headers or {})},
        )
        try:
            response = client.open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, json.load(response), dict(response.headers)

    def rename(self, username='synthetic_new_owner', password=test_auth_support.TEST_PASSWORD, **extra):
        return self.call('/api/auth/username', {
            'username': username, 'current_password': password, **extra,
        })

    def login(self, username, password=test_auth_support.TEST_PASSWORD):
        return self.call('/api/auth/login', {'username': username, 'password': password},
                         client=urllib.request.build_opener())

    def business_snapshot(self):
        with app.connect() as db:
            names = [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            mutable_auth = {'auth_users', 'auth_sessions', 'auth_limits', 'auth_audit'}
            return {name: sorted([tuple(row) for row in db.execute(
                'SELECT * FROM "' + name.replace('"', '""') + '"')], key=repr)
                for name in names if name not in mutable_auth}

    def safe_users(self):
        with app.connect() as db:
            return {row['id']: {key: row[key] for key in row.keys() if key != 'password_hash'}
                    for row in db.execute('SELECT * FROM auth_users')}

    def limit(self):
        with app.connect() as db:
            row = db.execute('SELECT failures,expires_at FROM auth_limits WHERE key=?',
                             (auth.digest('username-change:' + str(self.owner_id)),)).fetchone()
            return tuple(row) if row else None

    def assert_owner_still_logged_in(self):
        status, body, _ = self.call('/api/auth/me')
        self.assertEqual(status, 200)
        self.assertIsNotNone(body['user'])
        self.assertEqual(body['user']['username'], 'test_owner')

    def test_success_keeps_identity_grants_password_and_every_business_table(self):
        second_owner = test_auth_support.client(self.base)
        before_business = self.business_snapshot()
        before_users = self.safe_users()
        with app.connect() as db:
            previous_hash = db.execute('SELECT password_hash FROM auth_users WHERE id=?', (self.owner_id,)).fetchone()[0]
            other_sessions = [tuple(row) for row in db.execute(
                'SELECT * FROM auth_sessions WHERE user_id=? ORDER BY token_hash', (self.other_id,))]
        status, body, headers = self.call('/api/auth/username', {
            'username': 'synthetic_new_owner', 'current_password': test_auth_support.TEST_PASSWORD,
            'id': self.other_id, 'role': 'viewer', 'permissions': [], 'name': '不应采用的姓名',
            'password': 'not-a-permitted-password-change',
            '_expected_versions': {'account:synth-account-1': 'ignored'}, 'request_id': str(uuid4()),
        })
        self.assertEqual(status, 200, body)
        self.assertTrue(body['ok'])
        self.assertIn('board_session=;', headers.get('Set-Cookie', ''))
        self.assertIn('Max-Age=0', headers.get('Set-Cookie', ''))
        self.assertEqual(self.business_snapshot(), before_business)
        expected_users = {key: dict(value) for key, value in before_users.items()}
        expected_users[self.owner_id]['username'] = 'synthetic_new_owner'
        self.assertEqual(self.safe_users(), expected_users)
        with app.connect() as db:
            current_hash = db.execute('SELECT password_hash FROM auth_users WHERE id=?', (self.owner_id,)).fetchone()[0]
            self.assertTrue(auth.hmac.compare_digest(previous_hash, current_hash), 'Password hash must not change')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auth_sessions WHERE user_id=?', (self.owner_id,)).fetchone()[0], 0)
            self.assertTrue(other_sessions == [tuple(row) for row in db.execute(
                'SELECT * FROM auth_sessions WHERE user_id=? ORDER BY token_hash', (self.other_id,))],
                'Other users sessions must not change')
            audits = [dict(row) for row in db.execute('SELECT action,target FROM auth_audit ORDER BY id')]
            self.assertTrue(any('登录名' in row['action'] for row in audits))
            self.assertFalse(any(test_auth_support.TEST_PASSWORD in str(row) for row in audits))
        for old_client in (self.owner, second_owner):
            self.assertIsNone(self.call('/api/auth/me', client=old_client)[1]['user'])
            self.assertEqual(self.call('/api/board', client=old_client)[0], 401)
        self.assertEqual(self.call('/api/auth/me', client=self.other)[1]['user']['id'], self.other_id)
        self.assertEqual(self.login('test_owner')[0], 401)
        self.assertEqual(self.login('synthetic_new_owner')[0], 200)
        renamed = test_auth_support.client(self.base, 'synthetic_new_owner')
        current = self.call('/api/auth/me', client=renamed)[1]['user']
        self.assertEqual(current['id'], self.owner_id)
        self.assertEqual(current['role'], 'owner')
        self.assertEqual(current['office_ids'], ['synth-office-rename'])
        self.assertEqual(self.business_snapshot(), before_business)

    def test_unauthenticated_request_cannot_rename(self):
        users = self.safe_users()
        status, _, _ = self.call('/api/auth/username', {
            'username': 'synthetic_new_owner', 'current_password': test_auth_support.TEST_PASSWORD,
        }, client=urllib.request.build_opener())
        self.assertEqual(status, 401)
        self.assertEqual(self.safe_users(), users)

    def test_non_owner_roles_cannot_rename_themselves_or_owner(self):
        users = self.safe_users()
        for role in ('employee', 'admin', 'viewer'):
            with self.subTest(role=role):
                with app.connect() as db:
                    db.execute('UPDATE auth_users SET role=? WHERE id=?', (role, self.other_id))
                status, _, _ = self.call('/api/auth/username', {
                    'username': 'synthetic_attempt', 'current_password': test_auth_support.TEST_PASSWORD,
                    'id': self.owner_id,
                }, client=self.other)
                self.assertEqual(status, 403)
                self.assertEqual(self.safe_users()[self.owner_id], users[self.owner_id])
                self.assertEqual(self.safe_users()[self.other_id]['username'], 'synthetic_employee')

    def test_csrf_is_required_even_with_current_password(self):
        users = self.safe_users()
        original = self.owner.addheaders
        for headers in ([], [('X-CSRF-Token', 'synthetic-invalid-csrf')]):
            with self.subTest(missing=not headers):
                self.owner.addheaders = headers
                self.assertEqual(self.rename()[0], 403)
                self.assertEqual(self.safe_users(), users)
        self.owner.addheaders = original
        self.assert_owner_still_logged_in()

    def test_cross_origin_is_rejected_before_any_mutation(self):
        before = self.business_snapshot()
        status, _, _ = self.call('/api/auth/username', {
            'username': 'synthetic_new_owner', 'current_password': test_auth_support.TEST_PASSWORD,
        }, headers={'Origin': 'http://synthetic-cross-origin.invalid'})
        self.assertEqual(status, 403)
        self.assertEqual(self.business_snapshot(), before)
        self.assert_owner_still_logged_in()
        self.assertIsNone(self.limit())

    def test_wrong_password_is_persisted_without_any_other_changes(self):
        users = self.safe_users()
        before = self.business_snapshot()
        self.assertEqual(self.rename(password='synthetic-wrong-password')[0], 400)
        self.assertEqual(self.limit()[0], 1)
        self.assertEqual(self.safe_users(), users)
        self.assertEqual(self.business_snapshot(), before)
        self.assert_owner_still_logged_in()

    def test_noop_duplicate_and_invalid_names_leave_identity_unchanged(self):
        users = self.safe_users()
        invalid = ['', 'ab', 'x' * 41, '含中文', 'has space', 'has/slash', 'has@symbol', 'line\nbreak',
                   'test_owner', 'synthetic_employee', None, 123, True, [], {}]
        for value in invalid:
            with self.subTest(kind=type(value).__name__, value=value):
                status, _, _ = self.rename(value)
                self.assertEqual(status, 400)
                self.assertEqual(self.safe_users(), users)
        self.assert_owner_still_logged_in()

    def test_missing_current_password_is_rejected(self):
        status, _, _ = self.call('/api/auth/username', {'username': 'synthetic_new_owner'})
        self.assertEqual(status, 400)
        self.assert_owner_still_logged_in()

    def test_valid_boundaries_and_trim_preserve_existing_username_rules(self):
        for value in (' Ab_ ', 'X' * 40, 'synth.name-with_dash'):
            with self.subTest(length=len(value.strip())):
                status, body, _ = self.rename(value)
                self.assertEqual(status, 200, body)
                self.owner = test_auth_support.client(self.base, value.strip())
                self.assertEqual(self.call('/api/auth/me')[1]['user']['username'], value.strip())

    def test_case_sensitive_username_semantics_are_preserved(self):
        self.assertEqual(self.rename('TEST_OWNER')[0], 200)
        self.assertEqual(self.login('test_owner')[0], 401)
        self.assertEqual(self.login('TEST_OWNER')[0], 200)

    def test_five_failures_then_correct_password_is_throttled_across_sessions(self):
        first_expiry = None
        for attempt in range(1, 6):
            self.assertEqual(self.rename(password='synthetic-wrong-password')[0], 400)
            failures, expiry = self.limit()
            self.assertEqual(failures, attempt)
            first_expiry = expiry if first_expiry is None else first_expiry
            self.assertEqual(expiry, first_expiry, 'Attempts must not extend the fixed cooldown')
        self.owner = test_auth_support.client(self.base)
        with patch.object(auth, 'password_matches', side_effect=AssertionError('Throttled attempts must not hash passwords')):
            self.assertEqual(self.rename()[0], 429)
        self.assertEqual(self.limit(), (5, first_expiry))
        self.assert_owner_still_logged_in()

    def test_cooldown_expiry_allows_success_and_clears_own_limit_only(self):
        now = int(time.time())
        with patch.object(auth.time, 'time', return_value=now):
            for _ in range(5):
                self.assertEqual(self.rename(password='synthetic-wrong-password')[0], 400)
        unrelated = auth.digest('synthetic-unrelated-limit')
        with app.connect() as db:
            db.execute('INSERT INTO auth_limits VALUES(?,3,?)', (unrelated, now + 1800))
        with patch.object(auth.time, 'time', return_value=now + 901):
            self.assertEqual(self.rename()[0], 200)
        self.assertIsNone(self.limit())
        with app.connect() as db:
            self.assertEqual(tuple(db.execute('SELECT failures,expires_at FROM auth_limits WHERE key=?',
                                           (unrelated,)).fetchone()), (3, now + 1800))

    def test_expired_limit_restarts_at_one_failure(self):
        now = int(time.time())
        with patch.object(auth.time, 'time', return_value=now):
            self.assertEqual(self.rename(password='synthetic-wrong-password')[0], 400)
        with patch.object(auth.time, 'time', return_value=now + 901):
            self.assertEqual(self.rename(password='synthetic-wrong-password')[0], 400)
        self.assertEqual(self.limit(), (1, now + 1801))

    def test_success_before_threshold_clears_persisted_failure_counter(self):
        self.assertEqual(self.rename(password='synthetic-wrong-password')[0], 400)
        self.assertIsNotNone(self.limit())
        self.assertEqual(self.rename()[0], 200)
        self.assertIsNone(self.limit())

    def test_deactivated_owner_cannot_use_an_existing_session_to_rename(self):
        with app.connect() as db:
            db.execute('UPDATE auth_users SET active=0 WHERE id=?', (self.owner_id,))
        users = self.safe_users()
        self.assertEqual(self.rename()[0], 401)
        self.assertEqual(self.safe_users(), users)


if __name__ == '__main__':
    unittest.main()
