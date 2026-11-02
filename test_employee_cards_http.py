"""Authenticated loopback HTTP checks using only the isolated synthetic database."""
import http.cookiejar
import gzip
import json
from pathlib import Path
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from uuid import uuid4

from test_isolation import checked_root
import app
import auth
import idempotency


class QuietFixtureHandler(app.Handler):
    def log_message(self, *_args):
        pass


class EmployeeCardsHTTPTest(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(app, 'today', return_value='2026-11-02')
        self.clock.start()
        self.old_db = app.DB
        app.DB = checked_root() / ('employee-http-' + uuid4().hex + '.db')
        app.initialize()
        with app.connect() as db:
            token = auth.mint_setup(db)
            auth.setup_owner(db, {'username': 'synthetic-owner', 'name': '合成管理员',
                                 'password': 'Synthetic-test-only-2026'}, 'board_setup=' + token)
        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), QuietFixtureHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.owner = self.login('synthetic-owner')

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        app.DB = self.old_db
        self.clock.stop()

    def call(self, route, body=None, *, client=None, headers=None):
        request = urllib.request.Request(self.base + route,
            data=None if body is None else json.dumps(body).encode('utf-8'),
            headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            response = (client or urllib.request.build_opener()).open(request, timeout=8)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response), response.headers

    def login(self, name):
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        status, _, _ = self.call('/api/auth/login', {'username': name, 'password': 'Synthetic-test-only-2026'}, client=client)
        self.assertEqual(status, 200)
        status, state, _ = self.call('/api/auth/me', client=client)
        self.assertEqual(status, 200)
        client.addheaders = [('X-CSRF-Token', state['user']['csrf'])]
        return client

    def new_user(self, name, permissions=()):
        with app.connect() as db:
            owner = auth.public_user(db.execute("SELECT * FROM auth_users WHERE role='owner'").fetchone())
            return auth.save_user(db, owner, {'username': name, 'name': '合成员工', 'role': 'viewer',
                'permissions': list(permissions), 'password': 'Synthetic-test-only-2026', 'active': True})

    def preview_employee(self, number='001', auth_user_id=None):
        _, meta, _ = self.call('/api/employee-cards', client=self.owner)
        data = {'name': '合成员工', 'employee_no': number, 'office_id': '',
                'birth_date': '2000-01-01', 'employment_status': 'active',
                'consent_source': '仅供合成测试的员工授权', 'consent_on': '2026-11-02'}
        if auth_user_id is not None:
            data['auth_user_id'] = auth_user_id
        status, preview, _ = self.call('/api/employee-cards/preview',
            {'action': 'employee', 'data': data, 'expected_version': meta['version']}, client=self.owner)
        self.assertEqual(status, 200, preview)
        return preview

    def apply_preview(self, preview, request_id=None):
        return self.call('/api/employee-cards/apply', {'preview_id': preview['preview_id'],
            'request_id': request_id or str(uuid4()), 'confirmation': '确认保存'}, client=self.owner)

    def test_anonymous_and_csrf_are_required_and_no_store_is_retained(self):
        status, value, headers = self.call('/api/employee-cards')
        self.assertEqual(status, 401)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn('employees', value)
        status, _, _ = self.call('/api/employee-cards/preview', {'action': 'employee'}, client=self.owner,
                                  headers={'X-CSRF-Token': 'synthetic-invalid'})
        self.assertEqual(status, 403)

    def test_new_module_reads_only_its_own_records_and_keeps_six_named_brands(self):
        with patch.object(app.operations, 'snapshot', side_effect=AssertionError('full board snapshot forbidden')):
            status, meta, headers = self.call('/api/employee-cards', client=self.owner)
        self.assertEqual(status, 200)
        self.assertTrue(meta['available'])
        self.assertEqual({row['name'] for row in meta['operators']}, {'民生', '中兴', '长城', '联通', '电信', '移动'})
        self.assertEqual(meta['employees'], [])
        self.assertTrue(meta['can_configure_rules'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn('_record_versions', meta)

    def test_preview_is_not_a_business_write_and_apply_is_idempotent(self):
        with app.connect() as db:
            baseline = {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table)]
                        for table in ('accounts', 'phone_cards', 'daily', 'auth_users')}
        preview = self.preview_employee()
        _, before, _ = self.call('/api/employee-cards', client=self.owner)
        self.assertEqual(before['total'], 0)
        request_id = str(uuid4())
        status, receipt, _ = self.apply_preview(preview, request_id)
        self.assertEqual(status, 200, receipt)
        status, replay, _ = self.apply_preview(preview, request_id)
        self.assertEqual(status, 200)
        self.assertEqual(receipt, replay)
        _, listed, _ = self.call('/api/employee-cards?query=001', client=self.owner)
        self.assertEqual(listed['total'], 1)
        self.assertEqual(listed['employees'][0]['employee_no'], '001')
        self.assertNotIn('birth_date', listed['employees'][0])
        with app.connect() as db:
            for table, rows in baseline.items():
                self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM ' + table)], rows)

    def test_old_schema_returns_an_explicit_unavailable_state_without_creating_tables(self):
        with patch('employee_cards.available', return_value=False):
            status, meta, _ = self.call('/api/employee-cards', client=self.owner)
            self.assertEqual(status, 200)
            self.assertFalse(meta['available'])
            self.assertIn('结构', meta['message'])
            status, _, _ = self.call('/api/employee-cards/preview', {'action': 'employee'}, client=self.owner)
            self.assertNotEqual(status, 200)

    def test_unlinked_staff_never_falls_back_to_all_employees_and_cannot_write(self):
        self.apply_preview(self.preview_employee())
        self.new_user('synthetic-viewer')
        viewer = self.login('synthetic-viewer')
        status, meta, _ = self.call('/api/employee-cards', client=viewer)
        self.assertEqual(status, 200)
        self.assertFalse(meta['can_manage'])
        self.assertEqual(meta['employees'], [])
        self.assertEqual(meta['staff_candidates'], [])
        status, _, _ = self.call('/api/employee-cards/preview', {'action': 'employee'}, client=viewer)
        self.assertEqual(status, 403)

    def test_staff_self_view_is_bound_by_stable_user_id_and_other_detail_is_denied(self):
        own_id = self.new_user('synthetic-self')
        first = self.preview_employee('0001', own_id)
        _, receipt, _ = self.apply_preview(first)
        _, other, _ = self.apply_preview(self.preview_employee('0002'))
        own = self.login('synthetic-self')
        status, meta, _ = self.call('/api/employee-cards', client=own)
        self.assertEqual(status, 200)
        self.assertEqual([row['id'] for row in meta['employees']], [receipt['employee_id']])
        status, _, _ = self.call('/api/employee-cards?employee_id=' + other['employee_id'], client=own)
        self.assertEqual(status, 403)

    def test_obsolete_preview_never_overwrites_a_concurrent_employee_change(self):
        first = self.preview_employee('001')
        concurrent = self.preview_employee('002')
        self.assertEqual(self.apply_preview(concurrent)[0], 200)
        self.assertEqual(self.apply_preview(first)[0], 409)
        _, meta, _ = self.call('/api/employee-cards', client=self.owner)
        self.assertEqual(meta['total'], 1)

    def test_page_size_is_bounded(self):
        status, _, _ = self.call('/api/employee-cards?page_size=10000', client=self.owner)
        self.assertEqual(status, 400)

    def test_historical_request_generation_failure_is_a_private_conflict_not_500(self):
        error = idempotency.IdempotencyError('RESTORE_GENERATION_EXPIRED', '合成旧代次请求')
        with patch('employee_cards.get', side_effect=error):
            status, value, headers = self.call('/api/employee-cards?request_id=' + str(uuid4()), client=self.owner)
        self.assertEqual(status, 409)
        self.assertEqual(value['code'], 'RESTORE_GENERATION_EXPIRED')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn('employees', value)

    def test_employee_list_reuses_negotiated_compression_without_public_caching(self):
        _, _, identity = self.call('/api/employee-cards', client=self.owner)
        request = urllib.request.Request(self.base + '/api/employee-cards',
                                         headers={'Accept-Encoding': 'gzip, identity;q=0.5'})
        with self.owner.open(request, timeout=8) as response:
            body = response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers['Content-Encoding'], 'gzip')
            self.assertEqual(response.headers['Content-Length'], str(len(body)))
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertIn('Accept-Encoding', response.headers['Vary'])
            self.assertLess(len(body), int(identity['Content-Length']))
            value = json.loads(gzip.decompress(body))
            self.assertTrue(value['available'])
            self.assertEqual(len(value['operators']), 6)


if __name__ == '__main__':
    unittest.main()
