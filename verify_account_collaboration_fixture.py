"""Reviewed loopback fixture child for the JS/HTTP composition verifier.

Install the existing fail-closed isolation before importing any application.
Protocol stdout is consumed privately by the parent; session material is never
written to an artifact or forwarded to user-visible output. No subprocesses.
"""
import argparse
import json
import os
import sys
import threading
from unittest.mock import patch

from test_isolation import create_root, install


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    root = create_root(args.root)
    install(root)
    import app
    import board_updates
    import test_live_sync_http

    lock = threading.Lock()

    def message(value):
        with lock:
            sys.stdout.write(json.dumps(value, ensure_ascii=False) + '\n')
            sys.stdout.flush()

    fixture = test_live_sync_http.LiveSyncHttpTest('test_limit_validation_remains_after_authentication')
    try:
        fixture.setUp()
        os.environ['DOUYIN_BOARD_PUBLIC_ORIGIN'] = fixture.base
        before = fixture.snapshot()
        original_wait = board_updates.wait_committed
        waits = 0

        def wait(known, seconds):
            nonlocal waits
            waits += 1
            message({'kind': 'waiting', 'count': waits})
            return original_wait(known, seconds)

        waiting_patch = patch.object(board_updates, 'wait_committed', wait)
        waiting_patch.start()
        fixture.addCleanup(waiting_patch.stop)

        def session(client):
            jar = next(handler.cookiejar for handler in client.handlers if hasattr(handler, 'cookiejar'))
            cookies = [cookie for cookie in jar if cookie.name == 'board_session']
            if len(cookies) != 1:
                raise ValueError('Synthetic session initialization failed')
            csrf = next(value for key, value in client.addheaders if key.lower() == 'x-csrf-token')
            return {'cookie': 'board_session=' + cookies[0].value, 'csrf': csrf}

        # This line goes only into the parent's private pipe, never its log.
        message({'kind': 'ready', 'base': fixture.base, 'date': test_live_sync_http.support.DAY,
                 'accountId': 'old-three', 'sessions': {key: session(client) for key, client in
                     [('a', fixture.a), ('b', fixture.b), ('foreign', fixture.foreign)]}})
        for line in sys.stdin:
            command = json.loads(line)
            if command == {'command': 'verify'}:
                after = fixture.snapshot()
                for table in before:
                    if table != 'accounts':
                        fixture.assertEqual(after[table], before[table], 'Unrelated business table changed')
                expected = {'name': '合成联调最终昵称', 'status': '封禁', 'note': '合成保存后追赶备注'}
                fixture.assertEqual(len(after['accounts']), len(before['accounts']))
                for old, new in zip(before['accounts'], after['accounts']):
                    fixture.assertEqual(new, {**old, **expected} if old['id'] == 'old-three' else old)
                with app.connect() as db:
                    count = db.execute("SELECT COUNT(*) FROM operations_history WHERE action=? AND entity=?",
                        ('/api/account/patch', 'account:old-three')).fetchone()[0]
                fixture.assertEqual(count, 6)
                message({'kind': 'audit', 'unchanged_other_tables': len(before) - 1,
                         'successful_account_changes': count, 'waits_observed': waits})
            elif command == {'command': 'stop'}:
                break
            else:
                raise ValueError('Unknown isolated verifier command')
        return 0
    except BaseException as error:
        # Never stringify arbitrary fixture errors: they may contain a request.
        message({'kind': 'fixture_error', 'type': type(error).__name__})
        return 1
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    raise SystemExit(main())
