"""Two real loopback clients and bounded history reads on synthetic SQLite.

Only the history-reader projection emulates PostgreSQL's scope_offices column.
This does not exercise a PostgreSQL server, browser rendering or public latency.
Run explicitly through run_isolated_tests.py; no customer data or discovery.
"""
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
import auth
import board_updates
import office_scope
import operations
import test_auth_support
import test_office_three_import as support
from sql_dialect import begin_write

METRICS = {}


class LiveSyncHttpTest(unittest.TestCase):
    snapshot = support.OfficeThreeFixture.snapshot
    insert_account = staticmethod(support.OfficeThreeFixture.insert_account)
    stop_server = support.OfficeThreeImportHTTPTest.stop_server

    def setUp(self):
        support.OfficeThreeFixture.setUp(self)
        with app.connect() as db:
            begin_write(db)
            password_hash = db.execute('SELECT password_hash FROM auth_users WHERE id=?', (self.actor['id'],)).fetchone()[0]
            permissions = json.dumps(['account_manage', 'account_status', 'publish'])
            db.execute('UPDATE auth_users SET permissions=? WHERE id=?', (permissions, self.employee_id))
            self.second_id = db.execute('INSERT INTO auth_users(username,name,role,permissions,password_hash,created_at) '
                'VALUES(?,?,?,?,?,123) RETURNING id',
                ('synthetic_sync_b', '合成协作人员乙', 'employee', permissions, password_hash)).fetchone()[0]
            self.foreign_id = db.execute('INSERT INTO auth_users(username,name,role,permissions,password_hash,created_at) '
                'VALUES(?,?,?,?,?,123) RETURNING id',
                ('synthetic_sync_foreign', '合成异办协作人员', 'employee', permissions, password_hash)).fetchone()[0]
            office_scope.grant_user(db, self.actor, self.employee_id, ['three'])
            office_scope.grant_user(db, self.actor, self.second_id, ['three'])
            office_scope.grant_user(db, self.actor, self.foreign_id, ['synthetic-other'])

        class QuietHandler(app.Handler):
            def log_message(self, *args):
                pass

        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.a = test_auth_support.client(self.base, 'synthetic_import_employee')
        self.b = test_auth_support.client(self.base, 'synthetic_sync_b')
        self.foreign = test_auth_support.client(self.base, 'synthetic_sync_foreign')
        self.owner = test_auth_support.client(self.base)
        self.history_queries = []
        original_since = board_updates.since
        statements = self.history_queries

        class HistoryProjection:
            # The synthetic fixture already has the real scope_offices schema.
            # Emulate only since()'s PostgreSQL SELECT projection, not SQL/locks.
            dialect = 'postgresql'

            def __init__(self, db):
                self.db = db

            def execute(self, query, parameters=()):
                statements.append(query)
                return self.db.execute(query, parameters)

        def projected_since(db, cursor, actor, limit=None):
            return original_since(HistoryProjection(db), cursor, actor, limit=limit)

        projection_patch = patch.object(board_updates, 'since', projected_since)
        projection_patch.start()
        self.addCleanup(projection_patch.stop)

    def call(self, client, path, body=None):
        request = urllib.request.Request(self.base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Content-Type': 'application/json'})
        try:
            opener = client.open if client else urllib.request.urlopen
            with opener(request, timeout=10) as response:
                raw = response.read()
                self.assertEqual(response.headers.get('Cache-Control'), 'no-store')
                return response.status, json.loads(raw), len(raw)
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.load(error), 0
            finally:
                error.close()

    def board(self, client, suffix=''):
        status, value, _ = self.call(client, '/api/board?date=' + support.DAY + suffix)
        self.assertEqual(status, 200)
        return value

    def updates(self, client, cursor, limit=200):
        suffix = '' if limit is None else '&limit=' + str(limit)
        return self.call(client, '/api/board-updates?cursor=' + str(cursor) + suffix)

    def rename(self, client, state, name):
        return self.call(client, '/api/account/patch', dict(id='old-three', changes={'name': name},
            request_id=str(uuid4()), _expected_versions={'account:old-three': state['record_versions']['account:old-three']}))

    def test_two_sessions_observe_name_and_daily_changes_and_stale_save_cannot_overwrite(self):
        a, b = self.board(self.a), self.board(self.b)
        status, saved, _ = self.rename(self.a, a, '合成协作甲改名')
        self.assertEqual(status, 200, saved)
        status, changes, _ = self.updates(self.b, b['cursor'])
        self.assertEqual(status, 200)
        account = next(row for row in changes['changes'] if row['entity'] == 'account:old-three')
        self.assertEqual(account['after']['name'], '合成协作甲改名')
        self.assertEqual(account['offices'], ['three'])
        self.assertEqual(account['record_version'], saved['_record_versions']['account:old-three'])
        before_refusal = self.snapshot()
        self.assertEqual(self.rename(self.b, b, '合成旧草稿不得覆盖')[0], 409)
        self.assertEqual(self.snapshot(), before_refusal)
        current = self.board(self.a)
        daily_key = 'daily:old-three:' + support.DAY
        body = dict(id='old-three', date=support.DAY, published=True, request_id=str(uuid4()),
            _expected_versions={key: current['record_versions'].get(key) for key in ('account:old-three', daily_key)})
        status, result, _ = self.call(self.a, '/api/daily', body)
        self.assertEqual(status, 200, result)
        updates = self.updates(self.b, changes['cursor'])[1]
        daily = next(row for row in updates['changes'] if row['entity'] == daily_key)
        self.assertEqual(daily['after']['day'], support.DAY)
        self.assertEqual(daily['after']['published'], 1)
        self.assertEqual(daily['record_version'], result['_record_versions'][daily_key])
        historical = self.call(self.b, '/api/daily-flags?date=2026-10-20&versions=1')[1]
        self.assertTrue(historical['published']['old-three'])

    def test_cross_office_history_is_filtered_and_scope_revocation_invalidates_session(self):
        old = self.board(self.foreign)
        a = self.board(self.a)
        self.assertEqual(self.rename(self.a, a, '合成仅三办可见')[0], 200)
        status, unseen, _ = self.updates(self.foreign, old['cursor'])
        self.assertEqual(status, 200)
        self.assertEqual(unseen['changes'], [])
        self.assertGreater(unseen['cursor'], old['cursor'])
        self.assertNotIn('合成仅三办可见', json.dumps(unseen, ensure_ascii=False))
        with app.connect() as db:
            begin_write(db)
            office_scope.grant_user(db, self.actor, self.second_id, [])
        status, revoked, _ = self.updates(self.b, old['cursor'])
        self.assertEqual(status, 401)
        self.assertNotIn('changes', revoked)

    def test_work_and_detail_share_transaction_cursor_without_business_writes(self):
        before = self.snapshot()
        current = self.board(self.b)
        for view in ('work', 'detail'):
            with patch.object(operations, 'versions', side_effect=AssertionError('workspace opened full versions')):
                value = self.board(self.b, '&view=' + view)
            self.assertEqual(value['cursor'], current['cursor'])
            self.assertEqual([row['id'] for row in value['accounts']], ['old-three'])
            self.assertEqual(value['record_versions'], {})
        self.assertEqual(self.snapshot(), before)

    def test_idle_bounded_polls_are_small_and_never_build_full_snapshots_or_versions(self):
        state = self.board(self.b)
        before = self.snapshot()
        self.history_queries.clear()
        sizes = []
        with patch.object(operations, 'versions', side_effect=AssertionError('poll opened full versions')), \
             patch.object(operations, 'snapshot', side_effect=AssertionError('poll opened business snapshot')):
            for _ in range(10):
                status, value, size = self.updates(self.b, state['cursor'])
                self.assertEqual(status, 200)
                self.assertEqual(value, {'changes': [], 'cursor': state['cursor']})
                sizes.append(size)
        self.assertEqual(len(self.history_queries), 20)
        self.assertTrue(all(query.startswith('SELECT ') and 'operations_history' in query for query in self.history_queries))
        self.assertEqual(sum('LIMIT ?' in query for query in self.history_queries), 10)
        self.assertLess(max(sizes), 128)
        self.assertEqual(self.snapshot(), before)
        METRICS.update(kind='synthetic-loopback-SQLite-with-PG-history-projection', production_latency=False,
            idle_requests=10, history_queries_per_idle_poll=2, idle_max_json_bytes=max(sizes), business_writes_from_polls=0)

    def test_limit_overflow_requires_reload_and_legacy_unbounded_reads_still_deliver_every_row(self):
        cursor = self.board(self.b)['cursor']
        with app.connect() as db:
            for i in range(4):
                after = operations.encoded(dict(id='synthetic-history-' + str(i), name='合成历史变更'))
                db.execute('INSERT INTO operations_history(group_id,at,actor_id,actor_name,action,entity,before_json,after_json,scope_offices) '
                    'VALUES(?,123,NULL,?,?,?,?,?,?)', (uuid4().hex, '合成操作人', 'synthetic-change', 'account:synthetic-history-' + str(i), None, after, '["three"]'))
            latest = db.execute('SELECT MAX(id) FROM operations_history').fetchone()[0]
        status, overflow, _ = self.updates(self.b, cursor, 2)
        self.assertEqual(status, 200)
        self.assertEqual(overflow, {'reload': True, 'cursor': latest})
        for limit in (4, None):
            status, complete, _ = self.updates(self.b, cursor, limit)
            self.assertEqual(status, 200)
            self.assertEqual(len(complete['changes']), 4)
            self.assertEqual(complete['cursor'], latest)
        status, invalid_cursor, _ = self.updates(self.b, latest + 100)
        self.assertEqual(status, 200)
        self.assertEqual(invalid_cursor, {'reload': True, 'cursor': latest})

    def test_limit_validation_remains_after_authentication(self):
        for limit in (0, -1, 501, 'text', '1.5'):
            status, value, _ = self.updates(self.b, 0, limit)
            self.assertEqual(status, 400)
            self.assertEqual(value['error'], '同步读取上限必须是1至500的整数')
        self.assertEqual(self.updates(None, 0, 0)[0], 401)
        self.assertEqual(self.updates(self.b, -1)[0], 400)
        for value in (True, False, 0, 501, 1.5, '2'):
            with app.connect() as db:
                with self.assertRaises(ValueError):
                    board_updates.since(db, 0, self.actor, limit=value)
