"""Real authenticated loopback routes over disposable scoped SQLite only."""
import gzip
import http.cookiejar
import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from uuid import uuid4

from test_isolation import checked_root
checked_root()
import app
import table_sync
import test_auth_support
from test_office_three_import import OfficeThreeFixture, ACCOUNT_HEADERS, PROGRESS_HEADERS, csv_file, account_row, progress_row


class QuietHandler(app.Handler):
    def log_message(self, *_args):
        pass


class TableSyncHTTPTest(OfficeThreeFixture):
    def setUp(self):
        super().setUp()
        clock = patch.object(app, 'today', return_value='2026-11-04')
        clock.start()
        self.addCleanup(clock.stop)
        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.owner_client = self.login('test_owner')

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def call(self, route, body=None, *, client=None, headers=None, raw=False):
        request = urllib.request.Request(self.base + route,
            data=None if body is None else json.dumps(body).encode('utf-8'),
            headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            response = (client or urllib.request.build_opener()).open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            content = response.read()
            if raw:
                return response.status, content, response.headers
            if response.headers.get('Content-Encoding') == 'gzip':
                content = gzip.decompress(content)
            return response.status, json.loads(content), response.headers

    def login(self, name):
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        status, _, _ = self.call('/api/auth/login', {'username': name,
            'password': test_auth_support.TEST_PASSWORD}, client=client)
        self.assertEqual(status, 200)
        _, result, _ = self.call('/api/auth/me', client=client)
        client.addheaders = [('X-CSRF-Token', result['user']['csrf'])]
        return client

    def write(self, path, body):
        return self.call('/api/table-sync/' + path,
            {'office_id': 'three', 'request_id': str(uuid4()), **body}, client=self.owner_client)

    def enable(self):
        status, result, _ = self.write('pilot', {'state': 'enabled', 'expected_revision': 0,
            'confirmation': '开启办公室三试点'})
        self.assertEqual(status, 200, result)

    def preview_body(self):
        return {'office_id': 'three', 'request_id': str(uuid4()), 'mode': 'update', 'source_round': 'synthetic-round',
            'accounts_file': csv_file(ACCOUNT_HEADERS, [account_row(handle='synthetic_reuse', name='合成已核对名称')]),
            'progress_file': csv_file(PROGRESS_HEADERS, [progress_row(labels=('抖音号:synthetic_reuse',))])}

    def test_status_is_authenticated_scoped_no_store_and_defaults_off(self):
        self.assertEqual(self.call('/api/table-sync?office_id=three')[0], 401)
        status, result, headers = self.call('/api/table-sync?office_id=three', client=self.owner_client)
        self.assertEqual(status, 200)
        self.assertTrue(result['available'])
        self.assertEqual(result['pilot']['state'], 'disabled')
        self.assertEqual(headers.get('Cache-Control'), 'no-store')
        self.assertNotIn('accounts', result)
        self.assertEqual(self.call('/api/table-sync?office_id=synthetic-other', client=self.owner_client)[0], 400)
        self.assertEqual(self.call('/api/table-sync/preview', self.preview_body(), client=self.owner_client)[0], 400)

    def test_fresh_owner_csrf_origin_and_unknown_request(self):
        employee = self.login('synthetic_import_employee')
        self.assertEqual(self.call('/api/table-sync?office_id=three', client=employee)[0], 403)
        self.assertEqual(self.call('/api/table-sync/preview', self.preview_body(), client=employee)[0], 403)
        self.assertEqual(self.call('/api/table-sync/preview', self.preview_body(), client=self.owner_client,
                                  headers={'X-CSRF-Token': 'synthetic-invalid'})[0], 403)
        self.assertEqual(self.call('/api/table-sync/preview', self.preview_body(), client=self.owner_client,
                                  headers={'Origin': 'http://external.invalid'})[0], 403)
        status, result, _ = self.call('/api/table-sync/request?office_id=three&request_id=' + str(uuid4()), client=self.owner_client)
        self.assertEqual((status, result['code']), (404, 'REQUEST_NOT_FOUND'))

    def test_preview_apply_replay_gzip_receipt_and_narrow_sources(self):
        self.enable()
        with app.connect() as db:
            protected = {name: [dict(row) for row in db.execute('SELECT * FROM ' + name)]
                         for name in ('phone_cards', 'phone_devices', 'daily', 'publishing_layout', 'phone_account_access')}
        status, preview, _ = self.call('/api/table-sync/preview', self.preview_body(), client=self.owner_client)
        self.assertEqual(status, 200, preview)
        safe = [row['row_key'] for row in preview['rows'] if row['selected']]
        self.assertEqual(len(safe), 1)
        request_id = str(uuid4())
        body = {'office_id': 'three', 'request_id': request_id, 'preview_id': preview['preview_id'],
                'selected_rows': safe, 'confirmation': preview['confirmation']}
        first = self.call('/api/table-sync/apply', body, client=self.owner_client)
        self.assertEqual(first[0], 200, first[1])
        second = self.call('/api/table-sync/apply', body, client=self.owner_client)
        self.assertEqual(first[1], second[1])
        status, stored, _ = self.call('/api/table-sync/request?office_id=three&request_id=' + request_id, client=self.owner_client)
        self.assertEqual((status, stored), (200, first[1]))
        self.assertEqual(self.call('/api/table-sync/apply', {**body, 'selected_rows': []}, client=self.owner_client)[0], 409)
        route = '/api/table-sync/batch?office_id=three&id=' + first[1]['batch_id']
        status, encoded, headers = self.call(route, client=self.owner_client, headers={'Accept-Encoding': 'gzip'}, raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get('Content-Encoding'), 'gzip')
        self.assertEqual(headers.get('Cache-Control'), 'no-store')
        self.assertEqual(int(headers['Content-Length']), len(encoded))
        receipt = json.loads(gzip.decompress(encoded))
        self.assertEqual(receipt['batch_id'], first[1]['batch_id'])
        status, sources, _ = self.call('/api/table-sync/account-sources?office_id=three', client=self.owner_client)
        self.assertEqual(status, 200)
        self.assertEqual([row['account_id'] for row in sources['sources']], ['old-three'])
        self.assertEqual(sources['sources'][0]['responsible'], '合成负责人')
        with app.connect() as db:
            row = dict(db.execute("SELECT * FROM accounts WHERE id='old-three'").fetchone())
            self.assertEqual(row['name'], '合成已核对名称')
            self.assertEqual(row['note'], '原有备注必须保留')
            for name, before in protected.items():
                self.assertEqual([dict(row) for row in db.execute('SELECT * FROM ' + name)], before)

    def test_missing_optional_schema_keeps_board_readable(self):
        with app.connect() as db:
            db.execute('DROP TABLE table_sync_requests')
        status, meta, _ = self.call('/api/table-sync?office_id=three', client=self.owner_client)
        self.assertEqual(status, 200)
        self.assertFalse(meta['available'])
        self.assertEqual(self.call('/api/table-sync/preview', self.preview_body(), client=self.owner_client)[0], 503)
        status, board, _ = self.call('/api/board?view=work', client=self.owner_client)
        self.assertEqual(status, 200)
        self.assertTrue(any(row['id'] == 'old-three' for row in board['accounts']))
