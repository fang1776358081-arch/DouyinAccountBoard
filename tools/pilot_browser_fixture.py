"""Real loopback UI fixture; synthetic isolation precedes all application imports.

Never loads customer databases or sheets. The HTTP start route issues only a
new synthetic session; it never copies a production/browser credential.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import sys
from uuid import uuid4
from urllib.parse import parse_qs, urlparse

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from test_isolation import create_root, install


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--enabled', action='store_true', help='Seed only the disposable local pilot as enabled, avoiding native browser confirmation during interaction QA')
    args = parser.parse_args()
    root = create_root(args.root)
    install(root)
    import app
    import auth
    from test_office_three_import import OfficeThreeFixture, ACCOUNT_HEADERS, PROGRESS_HEADERS, csv_file, account_row, progress_row

    fixture = OfficeThreeFixture()
    fixture.setUp()
    app.today = lambda: '2026-11-04'
    with app.connect() as db:
        for index in range(2, 82):
            fixture.insert_account(db, 'synthetic-pilot-' + str(index), 'pilot_00' + str(index),
                '合成账号' + str(index), 'office3:1', index, 'three')
        owner_session = auth.new_session(db, fixture.actor['id'])
        db.execute('INSERT INTO user_office_grants VALUES(?,?)', (fixture.employee_id, 'three'))
        viewer_session = auth.new_session(db, fixture.employee_id)
    if args.enabled:
        import table_sync
        from sql_dialect import begin_write
        with app.connect() as db:
            begin_write(db)
            table_sync.post(db, fixture.actor, '/api/table-sync/pilot', dict(office_id='three',
                state='enabled', expected_revision=0, confirmation='开启办公室三试点', request_id=str(uuid4())), app.today())
    # Generated test sources only; no customer sheet, filename or credential.
    rows = [account_row('synthetic_reuse', name='合成核对后名称')]
    rows[0][1] = '19900000101'
    rows += [account_row('pilot_00'+str(i), row=i+20, name='合成来源账号'+str(i)) for i in range(2,62)]
    rows += [account_row('pilot_005', row=100, name='合成重复冲突'),
             account_row('synthetic_001', row=101, name='合成跨办冲突')]
    files = {'accounts.csv': csv_file(ACCOUNT_HEADERS, rows),
             'progress.csv': csv_file(PROGRESS_HEADERS, [progress_row(labels=('抖音号:synthetic_reuse',))]),
             'new-accounts.csv': csv_file(ACCOUNT_HEADERS, [account_row('000synthetic_new', row=150, name='合成安全新增')])}
    for name, item in files.items():
        (root / name).write_bytes(base64.b64decode(item['content']))

    class FixtureHandler(app.Handler):
        def log_message(self, *_args):
            pass

        def get_response(self):
            parsed = urlparse(self.path)
            if parsed.path == '/__synthetic_start__':
                query = parse_qs(parsed.query)
                session = viewer_session if query.get('role') == ['viewer'] else owner_session
                view = query.get('view', ['sync'])[0]
                destination = '/?page=accounts&office=three' if view == 'accounts' else '/workspace.html?tab=todo&office=three' if view == 'todo' else '/table-sync.html?office_id=three'
                self.send_response(303)
                self.send_header('Set-Cookie', auth.cookie('board_session', session))
                self.send_header('Location', destination)
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            return super().get_response()

        def do_POST(self):
            if not (self.path.startswith('/api/table-sync/') or self.path in ('/api/account/patch', '/api/daily')):
                self.close_connection = True
                return self.reply({'error': '此本机合成夹具不开放该写入接口'}, 405)
            return super().do_POST()

    server = app.ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
    base = 'http://127.0.0.1:' + str(server.server_address[1])
    receipt = {'kind': 'synthetic-only-real-loopback-http', 'base_url': base, 'pid': os.getpid(), 'pilot_enabled': args.enabled,
               'start_url': base + '/__synthetic_start__', 'accounts': 81,
               'source': str(PROJECT), 'database_root': str(root)}
    (root / 'preview-metadata.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print(json.dumps(receipt), flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        fixture.doCleanups()


if __name__ == '__main__':
    main()
