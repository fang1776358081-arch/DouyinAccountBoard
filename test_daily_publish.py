"""Daily publish flags: fresh synthetic scoped HTTP, never production data."""
import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4

from test_isolation import checked_root

checked_root()

import app
import auth
import idempotency
import test_auth_support
import test_device_workflow as fixtures


class DailyPublishTest(unittest.TestCase):
    stop_server = fixtures.DeviceWorkflowTest.stop_server
    enable_extended_database = fixtures.DeviceWorkflowTest.enable_extended_database

    def setUp(self):
        self.assertIsNone(app._DATABASE_PROVIDER)
        fixtures.DeviceWorkflowTest.setUp(self)
        self.enable_extended_database(scope=True, idempotency=True)
        with app.connect() as db:
            db.execute("INSERT INTO offices(id,name) VALUES('daily-one','合成发布办公室一')")
            db.execute("INSERT INTO offices(id,name) VALUES('daily-two','合成发布办公室二')")
            db.execute("INSERT INTO account_offices VALUES('synth-account-1','daily-one')")
            db.execute("INSERT INTO computer_offices VALUES('synth-pc-1','daily-one')")
            db.execute("INSERT INTO accounts(id,name,status) VALUES('synth-foreign-daily','合成异办账号','正常')")
            db.execute("INSERT INTO account_offices VALUES('synth-foreign-daily','daily-two')")
            owner = auth.public_user(db.execute("SELECT * FROM auth_users WHERE role='owner'").fetchone())
            self.employee_id = auth.save_user(db, owner, dict(
                username='synthetic_daily_employee', name='合成发布员工', role='employee',
                permissions=['publish'], password=test_auth_support.TEST_PASSWORD, office_ids=['daily-one']))
        self.employee = test_auth_support.client(self.base, 'synthetic_daily_employee')

    def call(self, path, body=None, client=None):
        return fixtures.DeviceWorkflowTest.call(self, path, body, client or self.employee)

    def board(self, client=None):
        return self.call('/api/board', client=client)

    def reject(self, status, path, body, client=None, text=None):
        return fixtures.DeviceWorkflowTest.reject(self, status, path, body, client or self.employee, text)

    def body(self, published, day='2026-10-29', ident='synth-account-1'):
        return dict(id=ident, date=day, published=published, request_id=str(uuid4()),
                    _expected_versions=self.board()['record_versions'])

    def flags(self, day='2026-10-29'):
        return self.call('/api/daily-flags?date=' + day)['published']

    def stored(self):
        with app.connect() as db:
            return [tuple(row) for row in db.execute('SELECT account_id,day,published FROM daily ORDER BY account_id,day')]

    def history_count(self):
        with app.connect() as db:
            return db.execute("SELECT count(DISTINCT group_id) FROM operations_history WHERE action='/api/daily'").fetchone()[0]

    def test_employee_can_mark_and_unmark_with_consistent_daily_and_versions(self):
        with app.connect() as db:
            accounts_before = [tuple(row) for row in db.execute('SELECT * FROM accounts ORDER BY id')]
        for value in (True, False):
            result = self.call('/api/daily', self.body(value))
            self.assertTrue(result['ok'])
            self.assertEqual(result['daily'], dict(id='synth-account-1', date='2026-10-29', published=value))
            key = 'daily:synth-account-1:2026-10-29'
            board = self.board()
            self.assertEqual(result['_record_versions'][key], board['record_versions'][key])
            exact = self.call('/api/record-versions?keys=' + key)['record_versions']
            self.assertEqual(exact[key], result['_record_versions'][key])
            self.assertEqual(self.flags()['synth-account-1'], value)
            self.assertNotIn('synth-foreign-daily', self.flags())
        self.assertEqual(self.stored(), [('synth-account-1', '2026-10-29', 0)])
        self.assertEqual(self.history_count(), 2)
        with app.connect() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM accounts ORDER BY id')], accounts_before)

    def test_replay_and_result_lookup_keep_daily_and_do_not_write_twice(self):
        body = self.body(True)
        first = self.call('/api/daily', body)
        before = self.stored(), self.history_count()
        self.assertEqual(self.call('/api/daily', body), first)
        looked_up = self.call('/api/idempotency/result?request_id=' + body['request_id'])
        self.assertEqual(looked_up['daily'], first['daily'])
        self.assertEqual(looked_up['_record_versions'], first['_record_versions'])
        self.assertEqual(looked_up['code'], 'REQUEST_SUCCEEDED')
        self.assertEqual((self.stored(), self.history_count()), before)
        self.reject(409, '/api/daily', {**body, 'published': False})
        self.assertEqual((self.stored(), self.history_count()), before)

    def test_committed_pending_reservation_can_resume_the_same_frozen_request(self):
        body=self.body(True)
        # Simulate a process interruption immediately after reservation commit,
        # before the request re-enters admission to perform its business write.
        with app.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            actor=auth.public_user(db.execute('SELECT * FROM auth_users WHERE id=?',(self.employee_id,)).fetchone())
            self.assertEqual(idempotency.claim(db,actor,'/api/daily',body)[0],'reserved')
            db.commit()
        self.assertEqual(self.stored(),[])
        pending=self.reject(409,'/api/idempotency/result?request_id='+body['request_id'],None)
        self.assertEqual(pending['code'],'REQUEST_PENDING')
        result=self.call('/api/daily',body)
        self.assertEqual(result['daily'],dict(id='synth-account-1',date='2026-10-29',published=True))
        self.assertEqual(self.call('/api/daily',body),result)
        self.assertEqual(self.history_count(),1)
        self.assertEqual(self.stored(),[('synth-account-1','2026-10-29',1)])

    def test_non_idempotent_compatibility_path_has_the_same_daily_shape(self):
        original = app.connect
        @contextmanager
        def without_generation():
            with original() as db:
                del db.restore_generation
                yield db
        with patch.object(app, 'connect', without_generation):
            marked = self.call('/api/daily', self.body(True))
            unmarked = self.call('/api/daily', self.body(False))
        self.assertEqual(marked['daily'], dict(id='synth-account-1', date='2026-10-29', published=True))
        self.assertEqual(unmarked['daily'], dict(id='synth-account-1', date='2026-10-29', published=False))
        self.assertFalse(self.flags()['synth-account-1'])

    def test_stale_versions_reject_without_reverting_a_newer_flag(self):
        stale = self.body(True)
        self.call('/api/daily', stale)
        before = self.stored(), self.history_count()
        self.reject(409, '/api/daily', {**stale, 'published': False, 'request_id': str(uuid4())})
        self.assertEqual((self.stored(), self.history_count()), before)
        self.assertTrue(self.flags()['synth-account-1'])

    def test_other_dates_and_other_accounts_are_not_overwritten(self):
        self.call('/api/daily', self.body(True, '2026-10-28'))
        previous = self.board()['record_versions']['daily:synth-account-1:2026-10-28']
        self.call('/api/daily', self.body(False, '2026-10-29'))
        self.assertTrue(self.flags('2026-10-28')['synth-account-1'])
        self.assertFalse(self.flags('2026-10-29')['synth-account-1'])
        self.assertEqual(self.board()['record_versions']['daily:synth-account-1:2026-10-28'], previous)
        self.assertEqual(self.stored(), [('synth-account-1', '2026-10-28', 1), ('synth-account-1', '2026-10-29', 0)])

    def test_scope_permission_and_missing_request_guards_prevent_any_daily_write(self):
        before = self.stored(), self.history_count()
        self.reject(403, '/api/daily', self.body(True, ident='synth-foreign-daily'))
        request = self.body(True)
        request.pop('request_id')
        # Fixture auto-adds request IDs, so call the real HTTP opener directly.
        import urllib.request
        import urllib.error
        raw = urllib.request.Request(self.base + '/api/daily', data=json.dumps(request).encode(),
                                     headers={'Content-Type': 'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.employee.open(raw, timeout=10)
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()
        with app.connect() as db:
            db.execute("UPDATE auth_users SET permissions='[]' WHERE id=?", (self.employee_id,))
        self.reject(403, '/api/daily', self.body(True))
        self.assertEqual((self.stored(), self.history_count()), before)

    def test_csrf_origin_and_invalid_values_do_not_mutate_flags(self):
        import urllib.request
        import urllib.error
        before = self.stored(), self.history_count()
        for headers in ({'X-CSRF-Token': 'invalid-synthetic-csrf'}, {'Origin': 'https://invalid.example'}):
            request = urllib.request.Request(self.base + '/api/daily', data=json.dumps(self.body(True)).encode(),
                                            headers={'Content-Type': 'application/json', **headers})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.employee.open(request, timeout=10)
            self.assertEqual(caught.exception.code, 403)
            caught.exception.close()
        self.reject(400, '/api/daily', self.body(1))
        self.reject(400, '/api/daily', self.body(True, '2026-99-99'))
        self.assertEqual((self.stored(), self.history_count()), before)


if __name__ == '__main__':
    unittest.main()
