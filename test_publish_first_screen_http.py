"""Reviewed loopback HTTP measurements, never a production latency claim.

The old/current handlers read the same case-only synthetic SQLite database.
Old scope/operations/app come only from this task's immutable source manifest.
Only run explicitly through run_isolated_tests.py; no automatic discovery.
"""
from collections import defaultdict
from contextlib import ExitStack, contextmanager
from html.parser import HTMLParser
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import logging
import math
import os
from queue import Queue
import re
import sqlite3
import sys
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

from test_isolation import checked_root, checked_database, PROJECT

checked_root()  # Must precede imports that initialize application paths.

import app
import auth
import board_performance
import office_scope
import operations
import test_performance_backend as support
import test_publish_first_screen_backend as fixture

DAY = fixture.DAY
METRICS = {}
COUNTS = ('queries', 'selects', 'rows_returned')


def percentile(values, fraction):
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 3)


def summarize(rows):
    return {key: {'p50': percentile([row[key] for row in rows], .5),
                  'p95': percentile([row[key] for row in rows], .95)} for key in rows[0]}


class MemoryLog(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class PageAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        path = attributes.get('src') if tag == 'script' else attributes.get('href') if tag == 'link' and attributes.get('rel') == 'stylesheet' else None
        if path:
            if not path.startswith('/') or path.startswith('//'):
                raise ValueError('Only current local declared assets may be measured')
            self.paths.append(path)


class PublishFirstScreenHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Reuse only fixture construction, never an old behavioral TestCase.
        with patch.object(support, 'frozen_module', return_value=None):
            support.BackendPerformanceTest.setUpClass.__func__(cls)
        cls.before_scope, cls.before_operations, cls.before_app = (
            fixture.frozen(name) for name in ('office_scope', 'operations', 'app'))
        METRICS.update(kind='synthetic-loopback-HTTP-SQLite-first-screen', production_latency=False,
            accounts=1600, devices=800, daily_rows=48000, computers=160, active_layout_cells=1600,
            source_sha256={name: dict(values) for name, values in fixture.METRICS['source_sha256'].items()},
            percentile_method='nearest rank; five-sample p95 is the maximum, not a production percentile',
            rows_definition='rows consumed by Python, not SQL engine rows examined',
            network_definition='127.0.0.1 HTTP only; no TLS, proxy, public network, PG or browser rendering',
            before_instrumentation='frozen GET plus current identical JSON reply and local function timing wrappers; test-instrumented',
            stage_definition='inclusive fixed-phase time/query/returned-row deltas; old unwrapped entities/cursor are residual, not zero')

    def setUp(self):
        fixture.PublishFirstScreenBackendTest.setUp(self)
        fixture.PublishFirstScreenBackendTest.seed_active_layout(self)
        self.cookies = {}
        for role, actor in self.actors.items():
            token = 'synthetic-loopback-first-screen-' + str(actor['id'])
            self.cookies[role] = 'board_session=' + token
            self.db.execute('INSERT INTO auth_sessions VALUES(?,?,?,?)',
                (auth.digest(token), actor['id'], 'synthetic-loopback-only-csrf', 2000000000))
        self.db.commit()
        self.db.close()  # Request threads open their own isolated SQLite connections.
        self.local = threading.local()
        self.completed = Queue()
        self.mode = 'after'
        owner = self

        class Handler(app.Handler):
            def log_message(self, *args):
                pass  # Do not print cookies, paths, headers or request details.

            def get_response(self):
                if owner.mode == 'before':
                    self._board_timing = board_performance.BoardTimings.from_environment()
                    owner.local.timer = self._board_timing
                    return owner.before_app.Handler.get_response(self)
                return app.Handler.get_response(self)

            def do_GET(self):
                owner.local.current = None
                owner.local.finished = []
                owner.local.phases = defaultdict(list)
                owner.local.timer = None
                started = time.perf_counter()
                try:
                    return super().do_GET()
                finally:
                    timer = getattr(self, '_board_timing', None)
                    owner.completed.put(dict(counts=owner.counts(), connections=len(owner.local.finished),
                        phases=dict(owner.local.phases), timer=timer.values() if timer else {},
                        handler_ms=(time.perf_counter() - started) * 1000))

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def counts(self):
        result = {key: sum(row[key] for row in getattr(self.local, 'finished', [])) for key in COUNTS}
        current = getattr(self.local, 'current', None)
        if current is not None:
            for key in COUNTS:
                result[key] += current.meter[key]
        return result

    @contextmanager
    def synthetic_connection(self):
        timer = getattr(self.local, 'timer', None)
        with ExitStack() as stack:
            if self.mode == 'before' and timer is not None:
                stack.enter_context(timer.phase('connect'))
            db = sqlite3.connect(checked_database(self.path), factory=support.CountConnection)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.reset_meter()  # Equal setup policy on both paths; exclude PRAGMA.
            self.local.current = db
        try:
            yield db
        finally:
            with ExitStack() as stack:
                if self.mode == 'before' and timer is not None:
                    stack.enter_context(timer.phase('transaction_close'))
                db.rollback()
                self.local.finished.append({key: db.meter[key] for key in COUNTS})
                self.local.current = None
                db.close()

    @contextmanager
    def application(self, mode='after', timing=True):
        """No request may overlap a different mode's module patches."""
        self.mode = mode
        scope = self.before_scope if mode == 'before' else office_scope
        versions = self.before_operations if mode == 'before' else operations
        module = self.before_app if mode == 'before' else app
        phase = board_performance.BoardTimings.phase
        owner = self

        @contextmanager
        def measured_phase(timer, name):
            before = owner.counts()
            started = time.perf_counter()
            with phase(timer, name):
                try:
                    yield
                finally:
                    after = owner.counts()
                    owner.local.phases[name].append(dict(milliseconds=(time.perf_counter() - started) * 1000,
                        **{key: after[key] - before[key] for key in COUNTS}))

        def observed(name, function):
            def wrapped(*args, **kwargs):
                timer = getattr(owner.local, 'timer', None)
                if timer is None:
                    return function(*args, **kwargs)
                with timer.phase(name):
                    return function(*args, **kwargs)
            return wrapped

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ))
            if timing is None:
                os.environ.pop('DOUYIN_BOARD_TIMING', None)
            else:
                os.environ['DOUYIN_BOARD_TIMING'] = '1' if timing else '0'
            stack.enter_context(patch.dict(sys.modules, {'office_scope': scope, 'operations': versions}))
            stack.enter_context(patch.object(module, 'connect', self.synthetic_connection))
            stack.enter_context(patch.object(module, 'today', return_value=DAY))
            stack.enter_context(patch.object(module, 'office_scope', scope))
            stack.enter_context(patch.object(module, 'operations', versions))
            stack.enter_context(patch.object(board_performance.BoardTimings, 'phase', measured_phase))
            if mode == 'before':
                functions = [(module, 'begin_read', 'read_begin'), (module.auth, 'session', 'auth'),
                    (scope, 'read_context', 'scope'), (scope, 'list_offices', 'offices'),
                    (versions, 'versions', 'versions'), (module.collection, 'overview', 'collection'),
                    (module.account_lifecycle, 'enrich', 'lifecycle'), (module.phone_cards, 'overview', 'cards'),
                    (module.phone_devices, 'overview', 'devices'), (module.device_workflow, 'overview', 'workflow'),
                    (module.publishing_layout, 'overview', 'layout'), (module.nurture, 'overview', 'nurture'),
                    (module.backup, 'status', 'backup')]
                for target, attribute, name in functions:
                    stack.enter_context(patch.object(target, attribute, observed(name, getattr(target, attribute))))
            yield

    def get(self, path, role='owner', decode=True):
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=30)
        headers = {'Cookie': self.cookies[role]} if role else {}
        started = time.perf_counter()
        try:
            connection.request('GET', path, headers=headers)
            response = connection.getresponse()
            head = time.perf_counter()
            body = response.read()
            received = time.perf_counter()
            value = json.loads(body) if decode else None
            parsed = time.perf_counter()
            result = dict(status=response.status, headers=dict(response.getheaders()), body=body, value=value,
                client=dict(ttfb_ms=(head - started) * 1000, body_receive_ms=(received - head) * 1000,
                    json_parse_ms=(parsed - received) * 1000, response_bytes=len(body)))
        finally:
            connection.close()
        result['server'] = self.completed.get(timeout=30)  # Includes close/log after body read.
        return result

    def pair(self, path, role='owner'):
        with self.application('before'):
            before = self.get(path, role)
        with self.application('after'):
            after = self.get(path, role)
        self.assertEqual(after['status'], before['status'])
        self.assertTrue(after['body'] == before['body'], 'Frozen/current raw HTTP payload differs')
        return before, after

    def stage_summary(self, samples):
        stages = defaultdict(list)
        for sample in samples:
            observed = sample['server']['phases']
            attributed = {key: 0 for key in COUNTS}
            for name, calls in observed.items():
                values = {key: sum(call[key] for call in calls) for key in ('milliseconds', *COUNTS)}
                stages[name].append(values)
                for key in COUNTS:
                    attributed[key] += values[key]
            # Connection timing is production-native but does not execute query work.
            for name in ('connect', 'transaction_close'):
                if name not in observed and name in sample['server']['timer']:
                    stages[name].append(dict(milliseconds=sample['server']['timer'][name], **{key: 0 for key in COUNTS}))
            residual = {key: sample['server']['counts'][key] - attributed[key] for key in COUNTS}
            self.assertTrue(all(value >= 0 for value in residual.values()))
            # Old entities + cursor and tiny dispatch overhead are deliberately unattributed.
            residual['milliseconds'] = max(0, sample['server']['handler_ms'] - sum(
                sum(call['milliseconds'] for call in calls) for calls in observed.values()))
            stages['unattributed_residual'].append(residual)
        return {name: summarize(rows) for name, rows in stages.items()}

    def test_same_seed_real_http_abba_exact_payload_and_stage_measurements(self):
        path = '/api/board?date=' + DAY + '&version_scope=day'
        results = {}
        for role in ('owner', 'assigned'):
            self.pair(path, role)  # Warm each complete path once.
            samples = {'before': [], 'after': []}
            for iteration in range(5):
                pair = {}
                for mode in (('before', 'after') if iteration % 2 == 0 else ('after', 'before')):
                    with self.application(mode):
                        pair[mode] = self.get(path, role)
                    samples[mode].append(pair[mode])
                    self.assertEqual(pair[mode]['status'], 200)
                    self.assertEqual(pair[mode]['server']['connections'], 1)
                self.assertTrue(pair['before']['body'] == pair['after']['body'], 'Same-seed raw HTTP payload differs')
            old, new = samples['before'][0], samples['after'][0]
            self.assertEqual(new['server']['counts']['queries'], old['server']['counts']['queries'] - 15)
            self.assertLess(new['server']['counts']['rows_returned'], old['server']['counts']['rows_returned'])
            results[role] = {mode: dict(client=summarize([sample['client'] for sample in values]),
                server=summarize([{**sample['server']['counts'], 'handler_ms': sample['server']['handler_ms']}
                                  for sample in values]), stages=self.stage_summary(values)) for mode, values in samples.items()}
            # Aggregate synthetic field sizes only; never retain response records.
            results[role]['payload_fields'] = {name: dict(
                value_bytes=len(json.dumps(value, ensure_ascii=False).encode()),
                items=len(value) if isinstance(value, (list, dict)) else None)
                for name, value in new['value'].items()}
        METRICS['board'] = dict(order='one warm pair then alternating AB/BA, five samples each', roles=results)

    def test_roles_dates_default_contract_and_fresh_permissions(self):
        cases = [('owner', '?date=' + DAY), ('assigned', '?date=2026-10-20&version_scope=day'),
            ('no_grants', '?date=' + DAY + '&version_scope=day'), ('inactive_only', '?date=' + DAY + '&version_scope=day'),
            ('admin', '?date=' + support.MISSING_DAY + '&version_scope=day'),
            ('assigned', '?view=work&date=' + DAY), ('assigned', '?view=detail&date=' + DAY)]
        for role, query in cases:
            with self.subTest(role=role, query=query):
                _, current = self.pair('/api/board' + query, role)
                self.assertEqual(current['status'], 200)
                if role in ('no_grants', 'inactive_only'):
                    self.assertFalse(current['value']['accounts'])
                    self.assertFalse(current['value']['computers'])
                    self.assertEqual(current['value']['record_versions'], {})
                if role == 'assigned':
                    self.assertTrue(all(row['office_ids'] == ['one'] for row in current['value']['accounts']))
        for query, status in [('?date=not-a-date', 400), ('?version_scope=invalid', 400), ('?view=invalid', 400)]:
            self.assertEqual(self.pair('/api/board' + query)[1]['status'], status)
        self.assertEqual(self.pair('/api/board', None)[1]['status'], 401)
        with sqlite3.connect(checked_database(self.path)) as db:
            db.execute('DELETE FROM user_office_grants WHERE user_id=2')
        current = self.pair('/api/board?date=' + DAY + '&version_scope=day', 'assigned')[1]
        self.assertFalse(current['value']['accounts'])
        self.assertFalse(current['value']['offices'])

    def test_timing_opt_in_is_numeric_only_and_default_silent(self):
        logger = logging.getLogger('douyin_board.performance')
        sink = MemoryLog()
        old_handlers, old_level, old_propagate = logger.handlers[:], logger.level, logger.propagate
        logger.handlers, logger.propagate = [sink], False
        logger.setLevel(logging.INFO)
        try:
            path = '/api/board?date=' + DAY + '&version_scope=day'
            with self.application(timing=None):
                disabled = self.get(path)
            self.assertNotIn('Server-Timing', disabled['headers'])
            self.assertFalse(sink.messages)
            with self.application(timing=True):
                enabled = self.get(path)
                me = self.get('/api/auth/me')
            self.assertTrue(disabled['body'] == enabled['body'], 'Timing changed the HTTP business body')
            self.assertEqual(len(sink.messages), 2)
            for message in sink.messages:
                values = json.loads(message)
                self.assertTrue(set(values) <= board_performance.PHASES | {'total'})
                self.assertIn('response_write', values)
                self.assertTrue(all(type(value) in (float, int) and math.isfinite(value) and value >= 0 for value in values.values()))
            for result in (enabled, me):
                header = result['headers']['Server-Timing']
                parts = header.split(', ')
                self.assertTrue(all(re.fullmatch(r'[a-z_]+;dur=\d+\.\d{3}', part) for part in parts))
                names = {part.split(';')[0] for part in parts}
                self.assertTrue(names <= board_performance.PHASES | {'total'})
                self.assertNotIn('response_write', names)  # Headers precede response write.
                self.assertIn('json_encode', names)
            with self.assertRaises(ValueError):
                with board_performance.BoardTimings(True).phase('arbitrary-request-text'):
                    pass
            METRICS['timing_contract'] = dict(default_headers=False, default_logs=False,
                enabled_keys='fixed phase whitelist plus total; finite numeric milliseconds only',
                header_excludes_response_write=True, log_includes_response_write=True)
        finally:
            logger.handlers, logger.level, logger.propagate = old_handlers, old_level, old_propagate

    def test_fixed_backend_build_header_preserves_readiness_and_auth_contract(self):
        # Case-only readiness table; no production schema or data is accessed.
        with sqlite3.connect(checked_database(self.path)) as db:
            db.execute('CREATE TABLE runtime_control(id INTEGER PRIMARY KEY,mode TEXT NOT NULL)')
        for state in ('open', 'maintenance', None):
            with self.subTest(state=state):
                with sqlite3.connect(checked_database(self.path)) as db:
                    db.execute('DELETE FROM runtime_control')
                    if state is not None:
                        db.execute('INSERT INTO runtime_control VALUES(1,?)', (state,))
                results = {}
                for mode in ('before', 'after'):
                    with self.application(mode, timing=None):
                        results[mode] = self.get('/api/ready', role=None)
                before, after = results['before'], results['after']
                self.assertEqual(after['status'], 200 if state == 'open' else 503)
                self.assertEqual(after['body'], before['body'])
                self.assertEqual(after['value'], {'ready': True} if state == 'open' else {'ready': False, 'code': 'NOT_READY'})
                self.assertEqual(after['server']['counts'], before['server']['counts'])
                self.assertEqual(after['server']['connections'], 1)
                self.assertEqual(after['server']['counts']['queries'], 2)
                self.assertEqual(after['headers']['X-Board-Build'], board_performance.BUILD_ID)
                self.assertNotIn('X-Board-Build', before['headers'])
                self.assertNotIn('Server-Timing', after['headers'])
        with sqlite3.connect(checked_database(self.path)) as db:
            db.execute("INSERT INTO runtime_control VALUES(1,'open')")
        health_results = {}
        for mode in ('before', 'after'):
            with self.application(mode, timing=None):
                health_results[mode] = self.get('/api/health?build=ignored-synthetic-input', role=None)
        before, health = health_results['before'], health_results['after']
        self.assertEqual(health['status'], 200)
        self.assertEqual(health['body'], before['body'])
        self.assertEqual(health['headers']['X-Board-Build'], board_performance.BUILD_ID)
        self.assertEqual(health['server']['connections'], 0)
        self.assertEqual(health['server']['counts'], dict(queries=0, selects=0, rows_returned=0))
        self.assertNotIn('Server-Timing', health['headers'])
        with self.application(timing=None):
            anonymous = self.get('/api/board?version_scope=day', role=None)
        self.assertEqual(anonymous['status'], 401)
        self.assertNotIn('X-Board-Build', anonymous['headers'])
        self.assertNotIn('Server-Timing', anonymous['headers'])
        METRICS['build_contract'] = dict(marker=board_performance.BUILD_ID,
            endpoints=['health', 'ready'], body_and_status_unchanged=True,
            ready_queries=2, health_connections=0, anonymous_board_status=401)

    def test_current_declared_static_assets_and_auth_me_loopback_measurement(self):
        with self.application():
            page = self.get('/', decode=False)
            self.assertEqual(page['status'], 200)
            parser = PageAssets()
            parser.feed(page['body'].decode('utf-8'))
            assets = []
            for path in dict.fromkeys(parser.paths):
                result = self.get(path, decode=False)
                self.assertEqual(result['status'], 200)
                source = PROJECT / 'static' / urlparse(path).path.lstrip('/')
                self.assertTrue(source.resolve().is_relative_to(PROJECT / 'static'))
                self.assertEqual(result['body'], source.read_bytes())
                self.assertEqual(result['server']['connections'], 0)
                assets.append(dict(kind='javascript' if source.suffix == '.js' else 'stylesheet', **result['client']))
            me = self.get('/api/auth/me')
        self.assertTrue(assets)
        self.assertEqual(me['value']['user']['id'], 1)
        METRICS['static_and_identity'] = dict(scenario='sequential HTTP requests for current HTML-declared resources, not browser waterfall',
            html=page['client'], asset_count=len(assets), total_asset_bytes=sum(row['response_bytes'] for row in assets),
            javascript_bytes=sum(row['response_bytes'] for row in assets if row['kind'] == 'javascript'),
            stylesheet_bytes=sum(row['response_bytes'] for row in assets if row['kind'] == 'stylesheet'),
            asset_ttfb_ms_sum=round(sum(row['ttfb_ms'] for row in assets), 3),
            asset_receive_ms_sum=round(sum(row['body_receive_ms'] for row in assets), 3),
            auth_me=dict(client=me['client'], counts=me['server']['counts'], phases=me['server']['timer']))


if __name__ == '__main__':
    unittest.main()
