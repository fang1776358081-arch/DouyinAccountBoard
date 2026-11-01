"""Real loopback HTTP collaboration on reviewed synthetic SQLite only.

History scope projection is the existing isolated PG-column fixture, not a real
PostgreSQL connection. Timed waits test coordination, not production latency.
"""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4

from test_isolation import checked_root

checked_root()

import account_service
import app
import board_updates
import office_scope
import operations
import test_live_sync_http as support
import test_auth_support
from sql_dialect import begin_write

METRICS = {}


class AccountCollaborationHttpTest(unittest.TestCase):
    setUp = support.LiveSyncHttpTest.setUp
    snapshot = support.LiveSyncHttpTest.snapshot
    insert_account = staticmethod(support.LiveSyncHttpTest.insert_account)
    stop_server = support.LiveSyncHttpTest.stop_server
    call = support.LiveSyncHttpTest.call
    board = support.LiveSyncHttpTest.board
    updates = support.LiveSyncHttpTest.updates
    rename = support.LiveSyncHttpTest.rename

    def inline(self, client=None, ident='old-three'):
        return self.call(client or self.b, '/api/account/inline?id=' + ident)

    def payload(self, state, changes, **options):
        return dict(id='old-three', changes=changes, request_id=str(uuid4()),
            _expected_versions={'account:old-three': state['record_version']}, **options)

    def waiting_path(self, cursor, wait=1):
        return '/api/board-updates?cursor=' + str(cursor) + '&limit=200&wait=' + str(wait)

    def test_narrow_read_and_opt_in_receipt_match_final_transaction_versions(self):
        before = self.snapshot()
        with patch.object(operations, 'versions', side_effect=AssertionError('broad versions')), \
             patch.object(operations, 'snapshot', side_effect=AssertionError('broad snapshot')), \
             patch.object(account_service, 'board_account', side_effect=AssertionError('broad account projection')):
            status, opened, size = self.inline()
        self.assertEqual(status, 200)
        self.assertEqual(set(opened), {'account', 'record_version', 'cursor'})
        self.assertEqual(set(opened['account']), {'id', 'name', 'status', 'note'})
        self.assertLess(size, 600)
        self.assertEqual(self.snapshot(), before)
        body = self.payload(opened, {'name': '  合成行内名称  '}, return_account=True)
        status, saved, _ = self.call(self.a, '/api/account/patch', body)
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved['account']['name'], '合成行内名称')
        self.assertEqual(saved['record_version'], saved['_record_versions']['account:old-three'])
        self.assertEqual(self.inline()[1], {key: saved[key] for key in ('account', 'record_version', 'cursor')})
        current = self.board(self.b)
        self.assertEqual(current['record_versions']['account:old-three'], saved['record_version'])
        after = self.snapshot()
        for table in before:
            if table != 'accounts':self.assertEqual(after[table], before[table], table)
        old = next(row for row in before['accounts'] if row['id'] == 'old-three')
        new = next(row for row in after['accounts'] if row['id'] == 'old-three')
        self.assertEqual({**old, 'name': '合成行内名称'}, new)
        # Status side effects and finish()/nurture.sync must precede the receipt.
        status, changed, _ = self.call(self.a, '/api/account/patch',
            self.payload(saved, {'status': '养号7天'}, return_account=True))
        self.assertEqual(status, 200, changed)
        self.assertEqual(changed['record_version'], changed['_record_versions']['account:old-three'])
        self.assertEqual(self.inline()[1]['record_version'], changed['record_version'])
        METRICS.update(kind='synthetic-loopback-SQLite-PG-history-projection',
            production_latency=False, inline_read_max_bytes=size, full_snapshot_calls_from_inline_read=0)

    def test_read_scope_authentication_and_opt_in_validation_keep_old_semantics(self):
        before = self.snapshot()
        self.assertEqual(self.call(None, '/api/account/inline?id=old-three')[0], 401)
        self.assertEqual(self.inline(self.foreign)[0], 403)
        self.assertEqual(self.inline(self.b, 'missing-synthetic-account')[0], 403)
        self.assertEqual(self.call(self.b, '/api/account/inline')[0], 400)
        state = self.inline()[1]
        for value in ('true', 1, None):
            body = self.payload(state, {'name': '无效回执选项'}, return_account=value)
            self.assertEqual(self.call(self.a, '/api/account/patch', body)[0], 400)
        self.assertEqual(self.call(self.a, '/api/account/patch',
            self.payload(state, {'douyin_id': 'synthetic_forbidden_inline'}, return_account=True))[0], 400)
        self.assertEqual(self.snapshot(), before)
        status, saved, _ = self.call(self.a, '/api/account/patch', self.payload(state, {'note': '合成旧接口兼容'}))
        self.assertEqual(status, 200)
        self.assertEqual(set(saved), {'ok', 'id', '_record_versions'})

    def test_replay_stays_historical_and_fresh_read_resolves_current_without_rebasing(self):
        opened = self.inline()[1]
        body = self.payload(opened, {'name': '合成第一次保存'}, return_account=True)
        status, first, _ = self.call(self.a, '/api/account/patch', body)
        self.assertEqual(status, 200)
        status, second, _ = self.call(self.b, '/api/account/patch',
            self.payload(first, {'name': '合成另一人新名称'}, return_account=True))
        self.assertEqual(status, 200)
        before = self.snapshot()
        status, replay, _ = self.call(self.a, '/api/account/patch', body)
        self.assertEqual(status, 200)
        self.assertEqual(replay, first)
        self.assertEqual(self.snapshot(), before)
        fresh = self.inline()[1]
        self.assertEqual(fresh['account']['name'], second['account']['name'])
        self.assertEqual(fresh['record_version'], second['record_version'])
        self.assertEqual(self.call(self.a, '/api/account/patch',
            self.payload(opened, {'note': '旧草稿不可覆盖'}, return_account=True))[0], 409)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.call(self.a, '/api/account/patch', {**body, 'return_account': False})[0], 409)

    def test_wait_releases_database_and_wakes_only_after_successful_commit(self):
        opened = self.inline()[1]
        waiting, staged, release = threading.Event(), threading.Event(), threading.Event()
        active = threading.local()
        original_connect, original_wait = app.connect, board_updates.wait_committed
        original_finish, original_notify = operations.finish, board_updates.notify_committed
        observed = []

        @contextmanager
        def tracked_connect():
            active.depth = getattr(active, 'depth', 0) + 1
            try:
                with original_connect() as db:yield db
            finally:active.depth -= 1

        def wait(known, seconds):
            self.assertEqual(getattr(active, 'depth', 0), 0)
            waiting.set()
            return original_wait(known, seconds)

        def finish(*args):
            original_finish(*args)
            staged.set()
            self.assertTrue(release.wait(4))

        def notify():
            self.assertEqual(getattr(active, 'depth', 0), 0)
            with original_connect() as db:
                observed.append(db.execute('SELECT name FROM accounts WHERE id=?', ('old-three',)).fetchone()[0])
            original_notify()

        with patch.object(app, 'connect', tracked_connect), patch.object(board_updates, 'wait_committed', wait), \
             patch.object(board_updates, 'notify_committed', notify), patch.object(operations, 'finish', finish), \
             ThreadPoolExecutor(max_workers=2) as pool:
            poll = pool.submit(self.call, self.b, self.waiting_path(opened['cursor'], 5))
            self.assertTrue(waiting.wait(2))
            saving = pool.submit(self.call, self.a, '/api/account/patch',
                self.payload(opened, {'name': '合成提交后可见'}, return_account=True))
            try:
                self.assertTrue(staged.wait(2))
                self.assertFalse(poll.done())
                self.assertEqual(observed, [])
            finally:release.set()
            self.assertEqual(saving.result(timeout=4)[0], 200)
            status, updates, _ = poll.result(timeout=4)
        self.assertEqual(status, 200)
        self.assertTrue(updates['wait_supported'])
        self.assertEqual(observed, ['合成提交后可见'])
        self.assertEqual(next(row['after']['name'] for row in updates['changes'] if row['entity']=='account:old-three'), '合成提交后可见')

    def test_stored_inline_text_cannot_be_read_after_office_grant_is_removed(self):
        opened = self.inline(self.a)[1]
        body = self.payload(opened, {'name': '合成旧办公室名称'}, return_account=True)
        self.assertEqual(self.call(self.a, '/api/account/patch', body)[0], 200)
        with app.connect() as db:
            begin_write(db)
            office_scope.grant_user(db, self.actor, self.employee_id, ['synthetic-other'])
        current = test_auth_support.client(self.base, 'synthetic_import_employee')
        self.assertEqual(self.call(current, '/api/account/inline?id=old-three')[0], 403)
        self.assertEqual(self.call(current, '/api/idempotency/result?request_id=' + body['request_id'])[0], 403)
        self.assertEqual(self.call(current, '/api/account/patch', body)[0], 403)

    def test_stored_inline_text_cannot_be_read_after_account_moves_out_of_scope(self):
        opened = self.inline(self.a)[1]
        body = self.payload(opened, {'note': '合成移出办公室备注'}, return_account=True)
        self.assertEqual(self.call(self.a, '/api/account/patch', body)[0], 200)
        with app.connect() as db:
            begin_write(db)
            office_scope.save_assignment(db, self.actor, 'accounts', 'old-three', {'office_id': 'synthetic-other'})
        self.assertEqual(self.call(self.a, '/api/account/inline?id=old-three')[0], 403)
        self.assertEqual(self.call(self.a, '/api/idempotency/result?request_id=' + body['request_id'])[0], 403)
        self.assertEqual(self.call(self.a, '/api/account/patch', body)[0], 403)

    def test_rolled_back_partial_patch_never_notifies_or_reaches_the_waiter(self):
        opened, before = self.inline()[1], self.snapshot()
        waiting = threading.Event()
        original_wait, original_patch = board_updates.wait_committed, account_service.patch

        def wait(known, seconds):
            waiting.set()
            return original_wait(known, seconds)

        def fail_after_change(*args):
            original_patch(*args)
            raise ValueError('合成回滚演练')

        notice = board_updates.committed_notice()
        with patch.object(board_updates, 'wait_committed', wait), ThreadPoolExecutor(max_workers=1) as pool:
            poll = pool.submit(self.call, self.b, self.waiting_path(opened['cursor']))
            self.assertTrue(waiting.wait(2))
            with patch.object(account_service, 'patch', fail_after_change):
                self.assertEqual(self.call(self.a, '/api/account/patch',
                    self.payload(opened, {'name': '合成不得泄漏的临时值'}, return_account=True))[0], 400)
            self.assertEqual(board_updates.committed_notice(), notice)
            status, value, _ = poll.result(timeout=3)
        self.assertEqual(status, 200)
        self.assertEqual(value, {'changes': [], 'cursor': opened['cursor'], 'wait_supported': True})
        self.assertEqual(self.snapshot(), before)

    def test_wake_reauthenticates_and_revoked_session_never_reads_changes(self):
        opened = self.inline()[1]
        waiting, original_wait = threading.Event(), board_updates.wait_committed

        def wait(known, seconds):
            waiting.set()
            return original_wait(known, seconds)

        with patch.object(board_updates, 'wait_committed', wait), ThreadPoolExecutor(max_workers=1) as pool:
            poll = pool.submit(self.call, self.b, self.waiting_path(opened['cursor'], 5))
            self.assertTrue(waiting.wait(2))
            with app.connect() as db:
                begin_write(db)
                office_scope.grant_user(db, self.actor, self.second_id, [])
            board_updates.notify_committed()
            status, result, _ = poll.result(timeout=3)
        self.assertEqual(status, 401)
        self.assertNotIn('changes', result)

    def test_timeout_finds_committed_history_even_without_process_local_wake(self):
        opened = self.inline()[1]
        waiting, original_wait = threading.Event(), board_updates.wait_committed

        def wait(known, seconds):
            waiting.set()
            return original_wait(known, seconds)

        with patch.object(board_updates, 'wait_committed', wait), ThreadPoolExecutor(max_workers=1) as pool:
            poll = pool.submit(self.call, self.b, self.waiting_path(opened['cursor']))
            self.assertTrue(waiting.wait(2))
            with patch.object(board_updates, 'notify_committed'):
                self.assertEqual(self.call(self.a, '/api/account/patch',
                    self.payload(opened, {'note': '合成另一进程提交'}, return_account=True))[0], 200)
            status, result, _ = poll.result(timeout=3)
        self.assertEqual(status, 200)
        self.assertEqual(next(row['after']['note'] for row in result['changes'] if row['entity']=='account:old-three'), '合成另一进程提交')

    def test_wait_bounds_are_authenticated_and_default_body_is_unchanged(self):
        state = self.inline()[1]
        self.assertEqual(self.updates(self.b, state['cursor'])[1], {'changes': [], 'cursor': state['cursor']})
        for value in (-1, 6, 'text', '1.5'):
            self.assertEqual(self.call(self.b, self.waiting_path(state['cursor'], value))[0], 400)
        self.assertEqual(self.call(None, self.waiting_path(state['cursor'], 6))[0], 401)
        self.assertEqual(self.call(self.b, self.waiting_path(state['cursor'], 0))[1], {'changes': [], 'cursor': state['cursor']})
