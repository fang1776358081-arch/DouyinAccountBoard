"""Workspace open reads stay small; save-time versions match the full snapshot token."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from uuid import uuid4
import app
import operations
import test_auth_support
from test_isolation import checked_root


class WorkspaceSummaryTest(unittest.TestCase):
    def test_work_view_skips_full_versions_and_save_uses_targeted_tokens(self):
        folder = checked_root() / ('summary-' + uuid4().hex)
        folder.mkdir()
        with patch.object(app, 'DB', folder / 'board.db'):
            app.initialize()
            with app.connect() as db:
                db.execute("INSERT INTO computers(id,name,shadow_account,ip,operator,category) VALUES('office3:9','合成三号电脑','','','','')")
                db.execute("INSERT INTO publishing_layout(batch,computer_id,slot,account_id,label,office,source_cell,source_url,reason) VALUES('synthetic-batch','office3:9',1,'synth-account-1','合成格子','合成办公室','A1','','待核对')")
                db.execute("INSERT INTO nurture_records(account_id,started_on,workflow,active_since) VALUES('synth-account-1','2026-10-20','运行','2026-10-20')")
                db.execute("INSERT INTO nurture_images(id,account_id,cycle,day,mime,content,actor) VALUES('synthetic-image','synth-account-1',1,'2026-10-20','image/png',?,'合成人员')", (b'\x89PNG',))
                db.execute("UPDATE accounts SET account_type='大号',registered_on='not-a-date' WHERE id='synth-account-1'")
                layout_key = operations.row_key('publishing_layout', dict(batch='synthetic-batch', computer_id='office3:9', slot=1))
                self.assertEqual(layout_key, 'layout:synthetic-batch:office3:9:1')
                full = operations.versions(db)
                targeted = operations.versions_for(db, ['account:synth-account-1', 'nurture:synth-account-1', layout_key, 'daily:synth-account-1:1999-01-01'])
            self.assertEqual(targeted['account:synth-account-1'], full['account:synth-account-1'])
            self.assertEqual(targeted['nurture:synth-account-1'], full['nurture:synth-account-1'])
            self.assertEqual(targeted[layout_key], full[layout_key])
            self.assertIsNone(targeted['daily:synth-account-1:1999-01-01'])
            self.assertNotIn('daily:synth-account-1:1999-01-01', full)

            class QuietHandler(app.Handler):
                def log_message(self, *args):
                    pass
            server = app.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                base = 'http://127.0.0.1:' + str(server.server_port)
                client = test_auth_support.owner_client(app, base)
                def call(path, body=None):
                    request = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
                    try:
                        with client.open(request, timeout=10) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as error:
                        return error.code, json.load(error)
                def anonymous(path):
                    request = urllib.request.Request(base + path)
                    try:
                        with urllib.request.urlopen(request, timeout=10) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as error:
                        return error.code, json.load(error)

                image_flags = []
                import nurture
                original = nurture.overview
                def wrapped(db, today, allowed_accounts=None, images=True):
                    image_flags.append(images)
                    return original(db, today, allowed_accounts, images)
                with patch.object(nurture, 'overview', wrapped), patch.object(operations, 'versions', side_effect=AssertionError('opened the full version snapshot')):
                    status, work = call('/api/board?view=work')
                self.assertEqual(status, 200)
                self.assertEqual(image_flags, [False])
                self.assertEqual(work['workspace_view'], 'summary')
                self.assertEqual(work['record_versions'], {})
                self.assertNotIn('collection', work)
                account = work['accounts'][0]
                self.assertEqual(account['id'], 'synth-account-1')
                self.assertIn('lifecycle', account)
                self.assertEqual(account['douyin_id'], 'synthetic_001')
                self.assertNotIn('name', work['computers'][0])
                record = work['nurture']['records'][0]
                self.assertEqual(record['account_id'], 'synth-account-1')
                self.assertIn('workflow', record)
                self.assertNotIn('images', record)
                self.assertNotIn('day_dates', record)
                self.assertEqual(anonymous('/api/board?view=work')[0], 401)
                self.assertEqual(call('/api/board?view=other')[0], 400)
                self.assertEqual(call('/api/record-versions?keys=nope')[0], 400)

                image_flags.clear()
                with patch.object(nurture, 'overview', wrapped), patch.object(operations, 'versions', side_effect=AssertionError('opened the full version snapshot')):
                    status, detail = call('/api/board?view=detail')
                self.assertEqual(status, 200)
                self.assertEqual(image_flags, [True])
                self.assertEqual(detail['workspace_view'], 'detail')
                self.assertEqual(detail['record_versions'], {})
                self.assertEqual(detail['accounts'][0]['douyin_id'], 'synthetic_001')
                self.assertEqual(detail['nurture']['records'][0]['images'][0]['id'], 'synthetic-image')

                status, board = call('/api/board')
                self.assertEqual(status, 200)
                self.assertIn('collection', board)
                self.assertEqual(next(row for row in board['nurture']['records'] if row['account_id']=='synth-account-1')['images'][0]['id'], 'synthetic-image')
                self.assertEqual(board['record_versions']['account:synth-account-1'], full['account:synth-account-1'])
                loaded=next(row for row in board['accounts'] if row['id']=='synth-account-1')
                self.assertEqual(loaded['lifecycle'],'注册日期待补充')
                self.assertIsNone(loaded['days_remaining'])
                status, checked = call('/api/record-versions?keys=' + ','.join(['account:synth-account-1', layout_key, 'daily:synth-account-1:1999-01-01']))
                self.assertEqual(status, 200)
                self.assertEqual(checked['record_versions']['account:synth-account-1'], board['record_versions']['account:synth-account-1'])
                self.assertEqual(checked['record_versions'][layout_key], board['record_versions'][layout_key])
                self.assertIsNone(checked['record_versions']['daily:synth-account-1:1999-01-01'])
                saved, _ = call('/api/account/status', dict(id='synth-account-1', status='封禁', _expected_versions={'account:synth-account-1': checked['record_versions']['account:synth-account-1']}))
                self.assertEqual(saved, 200)
                self.assertEqual(call('/api/account/status', dict(id='synth-account-1', status='正常', _expected_versions={'account:synth-account-1': checked['record_versions']['account:synth-account-1']}))[0], 409)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

    def test_timeout_before_any_response_returns_json(self):
        class Boom(app.Handler):
            def do_GET(self):
                raise TimeoutError('stalled')
            def log_message(self, *args):
                pass
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), Boom)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            urllib.request.urlopen('http://127.0.0.1:%s/api/board' % server.server_port, timeout=5)
            self.fail('timed-out handler returned success')
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 500)
            self.assertEqual(json.load(error)['error'], '页面数据暂时读不出来，请稍后刷新')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
