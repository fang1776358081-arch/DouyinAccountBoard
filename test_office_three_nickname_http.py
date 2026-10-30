"""Names-only routes through real loopback HTTP; entirely synthetic SQLite."""
import copy
import gzip
import json
import threading
import unittest
import urllib.request
from unittest.mock import patch
from uuid import uuid4

from test_isolation import checked_root

checked_root()

import app
import auth
import idempotency
import office_three_nickname as fill
import operations
import test_office_three_import as support
from sql_dialect import begin_write


class NicknameHttpTest(unittest.TestCase):
    snapshot = support.OfficeThreeFixture.snapshot
    body = support.OfficeThreeFixture.body
    insert_account = staticmethod(support.OfficeThreeFixture.insert_account)
    stop_server = support.OfficeThreeImportHTTPTest.stop_server
    call = support.OfficeThreeImportHTTPTest.call

    def setUp(self):
        support.OfficeThreeFixture.setUp(self)

        class QuietHandler(app.Handler):
            def log_message(self, *args):
                pass

        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.owner = support.test_auth_support.client(self.base)
        self.employee = support.test_auth_support.client(self.base, 'synthetic_import_employee')
        clock = patch.object(app, 'today', return_value=support.DAY)
        clock.start()
        self.addCleanup(clock.stop)
        with app.connect() as db:
            db.execute("UPDATE accounts SET name='昵称待补充（手机7）' WHERE id='old-three'")

    def payload(self):
        return dict(mode=fill.MODE, office_id='three', accounts_file=support.csv_file(
            support.ACCOUNT_HEADERS, [support.account_row('synthetic_reuse', name='合成接口正式名')]))

    def prepare(self):
        status, preview = self.call(support.PREVIEW_PATH, self.payload())
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview['mode'], fill.MODE)
        return dict(mode=fill.MODE, office_id='three', preview_id=preview['preview_id'],
                    confirmation=fill.CONFIRMATION, request_id=str(uuid4()), _expected_versions={})

    def get(self, path, coding=None):
        request = urllib.request.Request(self.base + path,
            headers={'Accept-Encoding': coding} if coding else {})
        with self.owner.open(request, timeout=10) as response:
            wire, headers = response.read(), dict(response.headers)
            decoded = gzip.decompress(wire) if headers.get('Content-Encoding') == 'gzip' else wire
            return json.loads(decoded), headers, wire

    def bookkeeping(self):
        with app.connect() as db:
            return {name: [dict(row) for row in db.execute('SELECT * FROM ' + name + ' ORDER BY 1')]
                    for name in ('operations_history', 'auth_audit', 'idempotency_records')}

    def test_apply_replay_only_changes_name_and_delivers_incremental_history(self):
        before = self.snapshot()
        with app.connect() as db:
            cursor = db.execute('SELECT COALESCE(MAX(id),0) FROM operations_history').fetchone()[0]
        body = self.prepare()
        first = self.call(support.APPLY_PATH, body)
        self.assertEqual(first[0], 200, first)
        self.assertEqual(first[1]['updated_accounts'], 1)
        self.assertNotIn('_record_versions', first[1])
        expected = copy.deepcopy(before)
        next(row for row in expected['accounts'] if row['id'] == 'old-three')['name'] = '合成接口正式名'
        self.assertEqual(self.snapshot(), expected)
        after = self.bookkeeping()
        self.assertEqual(self.call(support.APPLY_PATH, body), first)
        self.assertEqual(self.bookkeeping(), after)
        self.assertEqual(self.call(support.APPLY_PATH, {**body, 'confirmation': '合成另一个确认'})[0], 409)
        changes, _, _ = self.get('/api/board-updates?cursor=' + str(cursor))
        self.assertNotIn('reload', changes)
        self.assertEqual(len(changes['changes']), 1)
        change = changes['changes'][0]
        self.assertEqual(change['entity'], 'account:old-three')
        self.assertEqual(change['after']['name'], '合成接口正式名')
        self.assertEqual(change['record_version'], first[1]['changes'][0]['after_version'])

    def test_full_and_compact_gzip_board_agree_with_persisted_name_and_associations(self):
        body = self.prepare()
        self.assertEqual(self.call(support.APPLY_PATH, body)[0], 200)
        full, _, _ = self.get('/api/board?date=' + support.DAY + '&version_scope=day')
        compact, headers, wire = self.get('/api/board?date=' + support.DAY + '&version_scope=day&compact=accounts', 'gzip, deflate')
        self.assertEqual(full['accounts'], compact['accounts'])
        current = next(row for row in compact['accounts'] if row['id'] == 'old-three')
        with app.connect() as db:
            stored = dict(db.execute("SELECT * FROM accounts WHERE id='old-three'").fetchone())
            version = operations.token(operations.snapshot(db)['account:old-three'])
        for key in ('name', 'douyin_id', 'computer_id', 'slot', 'phone_device_id'):
            self.assertEqual(current[key], stored[key])
        self.assertEqual(current['name'], '合成接口正式名')
        self.assertEqual(current['office_ids'], ['three'])
        self.assertEqual(compact['record_versions']['account:old-three'], version)
        self.assertEqual(headers['Content-Encoding'], 'gzip')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(int(headers['Content-Length']), len(wire))
        status, preview = self.call(support.PREVIEW_PATH, self.payload())
        self.assertEqual(status, 200)
        self.assertEqual(preview['summary']['updated_accounts'], 0)
        self.assertEqual(preview['summary']['unchanged'], 1)

    def test_owner_csrf_origin_uuid_and_office_guards_are_preserved(self):
        body = self.prepare()
        before = self.snapshot()
        for client, code in ((urllib.request.build_opener(), 401), (self.employee, 403)):
            self.assertEqual(self.call(support.PREVIEW_PATH, self.payload(), client)[0], code)
            self.assertEqual(self.call(support.APPLY_PATH, body, client)[0], code)
        for route, value in ((support.PREVIEW_PATH, self.payload()), (support.APPLY_PATH, body)):
            self.assertEqual(self.call(route, value, headers={'Origin': 'http://synthetic.invalid'})[0], 403)
        saved = self.owner.addheaders
        self.owner.addheaders = []
        try:
            self.assertEqual(self.call(support.APPLY_PATH, body)[0], 403)
        finally:
            self.owner.addheaders = saved
        for value in ({key: value for key, value in body.items() if key != 'request_id'},
                      {**body, 'request_id': 'invalid'}, {**body, 'request_id': str(uuid4()), 'office_id': 'synthetic-other'},
                      {**body, 'request_id': str(uuid4()), 'changes': {'name': '合成伪造计划'}}):
            self.assertEqual(self.call(support.APPLY_PATH, value)[0], 400)
        self.assertEqual(self.snapshot(), before)

    def test_pending_reservation_continues_same_request_and_result_lookup_is_a_receipt(self):
        body = self.prepare()
        with app.connect() as db:
            begin_write(db)
            outcome = idempotency.claim(db, self.actor, support.APPLY_PATH, body)
            self.assertEqual(outcome[0], 'reserved')
        status, applied = self.call(support.APPLY_PATH, body)
        self.assertEqual(status, 200, applied)
        receipt, _, _ = self.get('/api/idempotency/result?request_id=' + body['request_id'])
        self.assertEqual(receipt['code'], 'REQUEST_SUCCEEDED')
        self.assertEqual(receipt['changes'], applied['changes'])
        self.assertEqual(receipt['updated_accounts'], 1)
        self.assertNotIn('_record_versions', receipt)

    def test_stale_handler_version_delta_cannot_be_attached_to_names_receipt(self):
        body = self.prepare()
        with patch.object(app.Handler, '_version_delta', {'account:synthetic-stale': 'synthetic-old'}, create=True):
            status, result = self.call(support.APPLY_PATH, body)
            receipt, _, _ = self.get('/api/idempotency/result?request_id=' + body['request_id'])
            self.assertEqual(receipt['code'], 'REQUEST_SUCCEEDED')
            self.assertNotIn('_record_versions', receipt)
            self.assertEqual(self.call(support.APPLY_PATH, body)[1], result)
        self.assertEqual(status, 200, result)
        self.assertNotIn('_record_versions', result)


if __name__ == '__main__':
    unittest.main()
