"""One authenticated board read uses one consistent connection; synthetic only."""
import importlib.util
import json
import statistics
import time
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from test_isolation import checked_root
checked_root()

import app
import test_device_workflow as support
import test_daily_publish as daily_support

METRICS = {}


class BoardConnectionPerformanceTest(unittest.TestCase):
    setUp = support.DeviceWorkflowTest.setUp
    stop_server = support.DeviceWorkflowTest.stop_server

    def get(self, path, authenticated=True):
        opener = self.owner if authenticated else urllib.request.build_opener()
        try:
            with opener.open(self.base + path, timeout=15) as response:
                payload = response.read()
                return response.status, json.loads(payload), len(payload)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error), 0

    def test_one_connection_and_authentication_precedes_query_validation(self):
        real = app.connect
        calls = []
        @contextmanager
        def counted():
            calls.append(1)
            with real() as db:
                yield db
        with patch.object(app, 'connect', counted):
            self.assertEqual(self.get('/api/board')[0], 200)
        self.assertEqual(len(calls), 1)
        for suffix in ('', '?view=other', '?date=invalid'):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.get('/api/board' + suffix, False)[0], 401)
        self.assertEqual(self.get('/api/board?view=other')[0], 400)
        self.assertEqual(self.get('/api/board?date=invalid')[0], 400)

    def test_frozen_board_handler_has_identical_payload_with_fewer_connections(self):
        baseline = Path(__file__).parent / '.synthetic-tests/performance-source-baseline-20261029/app.py'
        if not baseline.is_file():
            self.skipTest('Optional frozen pre-optimization source is not present')
        spec = importlib.util.spec_from_file_location('performance_baseline_app', baseline)
        old = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(old)
        real = app.connect
        current_handler = app.Handler.get_response
        values = {}
        samples = {'before': [], 'after': []}
        expected = self.get('/api/board')[1]
        # Both handlers share this same reviewed fixture and unchanged service
        # dependencies. This isolates connection/auth duplication, not all work.
        methods = {'before': old.Handler.get_response, 'after': current_handler}
        for iteration in range(9):
            order = ('before', 'after') if iteration % 2 == 0 else ('after', 'before')
            for label in order:
                count = []
                @contextmanager
                def counted():
                    count.append(1)
                    with real() as db:
                        yield db
                with patch.object(app, 'connect', counted), patch.object(old, 'connect', counted), patch.object(app.Handler, 'get_response', methods[label]):
                    start = time.perf_counter()
                    status, payload, size = self.get('/api/board')
                    duration = (time.perf_counter() - start) * 1000
                self.assertEqual(status, 200)
                self.assertEqual(payload, expected)
                samples[label].append((len(count), duration, size))
        for label, rows in samples.items():
            connections, durations, sizes = zip(*rows)
            self.assertEqual(len(set(connections)), 1)
            values[label] = dict(connections_per_read=connections[0], samples=len(rows), order='alternating-AB-BA',
                p50_ms=round(statistics.median(durations), 3), p95_ms=round(max(durations), 3), response_bytes=size)
        self.assertEqual(values['before']['connections_per_read'], 2)
        self.assertEqual(values['after']['connections_per_read'], 1)
        METRICS.update(kind='same-loopback-sqlite-fixture-connection-only-comparison',
                       production_latency=False, **values)


class DayVersionContractTest(unittest.TestCase):
    setUp = daily_support.DailyPublishTest.setUp
    stop_server = support.DeviceWorkflowTest.stop_server
    enable_extended_database = support.DeviceWorkflowTest.enable_extended_database
    call = daily_support.DailyPublishTest.call
    board = daily_support.DailyPublishTest.board
    reject = daily_support.DailyPublishTest.reject

    def test_compact_is_opt_in_and_only_removes_other_date_versions(self):
        with app.connect() as db:
            db.executemany('INSERT INTO daily VALUES(?,?,?)', [
                ('synth-account-1','2026-10-27',1), ('synth-account-1','2026-10-28',0),
                ('synth-account-1','2026-10-29',1), ('synth-foreign-daily','2026-10-29',1)])
        full = self.call('/api/board?date=2026-10-29')
        compact = self.call('/api/board?date=2026-10-29&version_scope=day')
        expected = {**full, 'record_versions': {key: value for key, value in full['record_versions'].items()
                    if not key.startswith('daily:') or key.endswith(':2026-10-29')}}
        self.assertEqual(compact, expected)
        self.assertIn('daily:synth-account-1:2026-10-27', full['record_versions'])
        self.assertNotIn('daily:synth-foreign-daily:2026-10-29', compact['record_versions'])
        self.assertEqual(self.call('/api/board?version_scope=day&view=work')['record_versions'], {})
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM daily').fetchone()[0], 4)

    def test_flags_return_coherent_scoped_versions_only_when_requested(self):
        import operations
        with app.connect() as db:
            db.execute("INSERT INTO daily VALUES('synth-account-1','2026-10-28',1)")
            db.execute("INSERT INTO daily VALUES('synth-foreign-daily','2026-10-28',1)")
            context = app.office_scope.read_context(db, app.auth.public_user(db.execute(
                'SELECT * FROM auth_users WHERE id=?', (self.employee_id,)).fetchone()))
            full = operations.versions(db, context)
        legacy = self.call('/api/daily-flags?date=2026-10-28')
        self.assertEqual(set(legacy), {'date','published'})
        state = self.call('/api/daily-flags?date=2026-10-28&versions=1')
        key = 'daily:synth-account-1:2026-10-28'
        self.assertEqual(state['record_versions'], {key: full[key]})
        self.assertEqual(state['published'], {'synth-account-1': True})
        absent = self.call('/api/daily-flags?date=2026-10-29&versions=1')
        self.assertEqual(absent['record_versions'], {'daily:synth-account-1:2026-10-29': None})
        self.assertEqual(absent['published'], {'synth-account-1': False})
        body = dict(id='synth-account-1', date='2026-10-28', published=False,
                    _expected_versions={**self.board()['record_versions'], **state['record_versions']})
        saved = self.call('/api/daily', body)
        self.assertFalse(saved['daily']['published'])
        self.assertFalse(self.call('/api/daily-flags?date=2026-10-28&versions=1')['published']['synth-account-1'])

    def test_new_options_do_not_bypass_login_or_validation(self):
        self.reject(400, '/api/board?version_scope=invalid', None)
        self.reject(400, '/api/daily-flags?versions=invalid', None)
        for path in ('/api/board?version_scope=day', '/api/daily-flags?versions=1'):
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(self.base + path, timeout=10)
            self.assertEqual(error.exception.code, 401)
            error.exception.close()
