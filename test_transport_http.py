"""Actual loopback HTTP content-coding tests; synthetic SQLite only.

Identity, gzip and opt-in accounts transport read the same 1600-account fixture
through the same current handler. No production files, credentials, proxy, PG
or browser measurements.
Run explicitly with run_isolated_tests.py; never discover historical tests.
"""
import gzip
from http.client import HTTPConnection
import io
import json
import math
import sqlite3
import statistics
import time
import types
import unittest

from test_isolation import checked_root, checked_database

checked_root()  # Before application or fixture imports.

import app
import account_transport
import test_publish_first_screen_http as fixture_http
import test_performance_backend as seed

DAY = fixture_http.DAY
METRICS = {}


def statistics_rows(rows):
    return {name: dict(p50=round(statistics.median([row[name] for row in rows]), 3),
                       minimum=round(min(row[name] for row in rows), 3),
                       maximum=round(max(row[name] for row in rows), 3))
            for name in rows[0]}


class TransportHttpTest(unittest.TestCase):
    # Reuse fixture setup/helpers, not the existing TestCase's test methods.
    @classmethod
    def setUpClass(cls):
        fixture_http.PublishFirstScreenHttpTest.setUpClass.__func__(cls)
        METRICS.update(kind='same-current-handler-identity-vs-gzip-loopback-SQLite',
            production_latency=False, accounts=1600, devices=800, daily_rows=48000,
            computers=160, active_layout_cells=1600,
            network='127.0.0.1 HTTP; no TLS, proxy, public bandwidth or browser',
            rows='rows consumed by Python, not SQL engine rows scanned')

    setUp = fixture_http.PublishFirstScreenHttpTest.setUp
    stop_server = fixture_http.PublishFirstScreenHttpTest.stop_server
    counts = fixture_http.PublishFirstScreenHttpTest.counts
    synthetic_connection = fixture_http.PublishFirstScreenHttpTest.synthetic_connection
    application = fixture_http.PublishFirstScreenHttpTest.application

    def request(self, path, encoding='identity', role='owner', decode=True):
        headers = {'Cookie': self.cookies[role]} if role else {}
        if encoding is not None:
            headers['Accept-Encoding'] = encoding
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=30)
        started = time.perf_counter()
        try:
            connection.request('GET', path, headers=headers)
            response = connection.getresponse()
            head = time.perf_counter()
            encoded = response.read()
            received = time.perf_counter()
            coding = response.getheader('Content-Encoding')
            decoded = gzip.decompress(encoded) if coding == 'gzip' else encoded
            decompressed = time.perf_counter()
            value = json.loads(decoded) if decode and decoded else None
            parsed = time.perf_counter()
            result = dict(status=response.status, headers=dict(response.getheaders()),
                encoded=encoded, decoded=decoded, value=value,
                client=dict(ttfb_ms=(head - started) * 1000,
                    body_receive_ms=(received - head) * 1000,
                    content_decode_ms=(decompressed - received) * 1000,
                    json_parse_ms=(parsed - decompressed) * 1000,
                    encoded_body_bytes=len(encoded), decoded_body_bytes=len(decoded)))
        finally:
            connection.close()
        result['server'] = self.completed.get(timeout=30)
        return result

    def assert_headers(self, result, encoding):
        self.assertEqual(result['headers'].get('Content-Encoding'), encoding)
        self.assertEqual(int(result['headers']['Content-Length']), len(result['encoded']))
        self.assertEqual(result['headers']['Cache-Control'], 'no-store')
        self.assertEqual(result['headers']['Content-Type'], 'application/json; charset=utf-8')
        vary = {item.strip().lower() for item in result['headers'].get('Vary', '').split(',')}
        self.assertIn('accept-encoding', vary)

    def test_board_gzip_preserves_scope_dates_every_field_and_versions(self):
        for role in ('owner', 'assigned', 'no_grants', 'inactive_only'):
            for day in (DAY, '2026-10-20', seed.MISSING_DAY):
                with self.subTest(role=role, day=day):
                    path = '/api/board?date=' + day + '&version_scope=day'
                    with self.application():
                        identity = self.request(path, 'identity', role)
                        compressed = self.request(path, 'gzip, deflate', role)
                    self.assertEqual(identity['status'], 200)
                    self.assertEqual(compressed['status'], 200)
                    self.assert_headers(identity, None)
                    self.assert_headers(compressed, 'gzip')
                    self.assertTrue(identity['decoded'] == compressed['decoded'], 'Coding changed business JSON bytes')
                    self.assertEqual(identity['value'], compressed['value'])
                    self.assertEqual(identity['server']['counts'], compressed['server']['counts'])
                    self.assertEqual(compressed['server']['connections'], 1)
                    self.assertEqual(compressed['value']['date'], day)
                    if role == 'assigned':
                        self.assertTrue(all(row['office_ids'] == ['one'] for row in compressed['value']['accounts']))
                    if role in ('no_grants', 'inactive_only'):
                        self.assertFalse(compressed['value']['accounts'])
                        self.assertFalse(compressed['value']['computers'])
                        self.assertEqual(compressed['value']['record_versions'], {})

    def test_auth_errors_and_other_routes_do_not_start_compressing(self):
        cases = [('/api/board?version_scope=day', None, 401),
                 ('/api/board?date=invalid', 'owner', 400),
                 ('/api/board?view=invalid', 'owner', 400),
                 ('/api/auth/me', 'owner', 200),
                 ('/api/health', None, 200)]
        with self.application(timing=None):
            for path, role, status in cases:
                with self.subTest(path=path, role=role):
                    result = self.request(path, 'gzip, deflate', role)
                    self.assertEqual(result['status'], status)
                    self.assertNotIn('Content-Encoding', result['headers'])
                    self.assertEqual(result['headers']['Cache-Control'], 'no-store')
                    self.assertEqual(int(result['headers']['Content-Length']), len(result['encoded']))
                    self.assertNotIn('Server-Timing', result['headers'])

    def test_accounts_transport_preserves_projection_scope_dates_and_versions(self):
        for role in ('owner', 'assigned', 'no_grants', 'inactive_only'):
            for day in (DAY, '2026-10-20', seed.MISSING_DAY):
                with self.subTest(role=role, day=day):
                    path = '/api/board?date=' + day + '&version_scope=day'
                    with self.application():
                        full = self.request(path, 'identity', role)
                        compact = self.request(path + '&compact=accounts', 'identity', role)
                        compressed = self.request(path + '&compact=accounts', 'gzip, deflate', role)
                    self.assertEqual(full['status'], 200)
                    self.assertEqual(compact['status'], 200)
                    self.assertEqual(compressed['status'], 200)
                    self.assert_headers(full, None)
                    self.assert_headers(compact, None)
                    self.assert_headers(compressed, 'gzip')
                    self.assertEqual(compact['value'], account_transport.compact(full['value']))
                    self.assertTrue(compact['decoded'] == compressed['decoded'], 'Compact coding changed JSON bytes')
                    self.assertEqual(set(compact['value']), set(full['value']))
                    for key in ('accounts', 'computers', 'phone_cards', 'record_versions', 'offices',
                                'user', 'publishing_layout', 'nurture', 'collection', 'backup',
                                'date', 'today', 'cursor', 'statuses'):
                        self.assertEqual(compact['value'][key], full['value'][key], key)
                    self.assertEqual(full['server']['counts'], compact['server']['counts'])
                    self.assertEqual(compact['server']['counts'], compressed['server']['counts'])
                    self.assertEqual(compact['server']['connections'], 1)
                    self.assertEqual(compact['value']['date'], day)
                    if role == 'assigned':
                        self.assertTrue(all(row['office_ids'] == ['one'] for row in compact['value']['accounts']))
                    if role in ('no_grants', 'inactive_only'):
                        self.assertFalse(compact['value']['accounts'])
                        self.assertFalse(compact['value']['computers'])
                        self.assertFalse(compact['value']['offices'])
                        self.assertEqual(compact['value']['record_versions'], {})

    def test_accounts_transport_validation_follows_auth_and_empty_is_compatible(self):
        path = '/api/board?date=' + DAY + '&version_scope=day'
        invalid = ('&compact=invalid', '&compact=Accounts', '&compact=accounts&view=work',
                   '&compact=accounts&view=detail', '&compact=accounts&view=invalid')
        with self.application():
            full = self.request(path, 'identity')
            empty = self.request(path + '&compact=', 'identity')
            self.assertEqual(empty['status'], 200)
            self.assertTrue(empty['decoded'] == full['decoded'], 'Empty compact changed default response')
            self.assert_headers(empty, None)
            for suffix in invalid:
                for role, status in (('owner', 400), (None, 401)):
                    with self.subTest(suffix=suffix, role=role):
                        result = self.request(path + suffix, 'gzip, deflate', role)
                        self.assertEqual(result['status'], status)
                        self.assertNotIn('Content-Encoding', result['headers'])
                        self.assertEqual(result['headers']['Cache-Control'], 'no-store')
                        self.assertEqual(int(result['headers']['Content-Length']), len(result['encoded']))

    def test_http_negotiation_respects_rejection_and_identity_fallback(self):
        path = '/api/board?date=' + DAY + '&version_scope=day'
        with self.application():
            baseline = self.request(path, 'identity')
            for header in ('gzip;q=0', 'gzip;q=0, *;q=1', 'br, deflate', 'identity', None):
                with self.subTest(header=header):
                    result = self.request(path, header)
                    self.assertEqual(result['status'], 200)
                    self.assert_headers(result, None)
                    self.assertTrue(result['encoded'] == baseline['encoded'], 'Identity fallback changed bytes')
            for header in ('gzip, identity;q=0', 'GZIP;Q=1, deflate;q=0.2', 'gzip, *;q=0'):
                with self.subTest(header=header):
                    result = self.request(path, header)
                    self.assertEqual(result['status'], 200)
                    self.assert_headers(result, 'gzip')
                    self.assertTrue(result['decoded'] == baseline['decoded'], 'Accepted coding changed bytes')
            for header in ('gzip;q=0, identity;q=0', 'br, deflate, identity;q=0', '*;q=0'):
                with self.subTest(header=header):
                    result = self.request(path, header, decode=False)
                    self.assertEqual(result['status'], 406)
                    self.assert_headers(result, None)
                    self.assertEqual(result['encoded'], b'')

    def test_existing_content_encoding_is_not_encoded_twice(self):
        statuses, headers = [], []
        handler = types.SimpleNamespace(path='/api/board?version_scope=day', command='GET',
            headers={'Accept-Encoding': 'gzip, deflate'}, wfile=io.BytesIO(),
            send_response=statuses.append, send_header=lambda name, value: headers.append((name, value)),
            end_headers=lambda: None)
        body = {'kind': 'synthetic-only', 'note': '仅合成内容' * 500}
        app.Handler.reply(handler, body, headers=[('Content-Encoding', 'identity')])
        expected = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.assertEqual(statuses, [200])
        self.assertEqual([value for name, value in headers if name.lower() == 'content-encoding'], ['identity'])
        self.assertEqual(handler.wfile.getvalue(), expected)
        self.assertEqual(dict(headers)['Content-Length'], str(len(expected)))
        self.assertEqual(dict(headers)['Cache-Control'], 'no-store')

    def test_compression_does_not_reuse_permissions_after_revocation(self):
        path = '/api/board?date=' + DAY + '&version_scope=day'
        with self.application():
            before = self.request(path, 'gzip', 'assigned')
            compact_before = self.request(path + '&compact=accounts', 'gzip', 'assigned')
        self.assertTrue(before['value']['accounts'])
        self.assertEqual(compact_before['value'], account_transport.compact(before['value']))
        with sqlite3.connect(checked_database(self.path)) as db:
            db.execute('DELETE FROM user_office_grants WHERE user_id=2')
        with self.application():
            after = self.request(path, 'gzip', 'assigned')
            compact_after = self.request(path + '&compact=accounts', 'gzip', 'assigned')
        self.assertEqual(after['status'], 200)
        self.assertFalse(after['value']['accounts'])
        self.assertFalse(after['value']['computers'])
        self.assertFalse(after['value']['offices'])
        self.assertEqual(after['value']['record_versions'], {})
        self.assertEqual(compact_after['status'], 200)
        self.assertEqual(compact_after['value'], account_transport.compact(after['value']))
        self.assertFalse(compact_after['value']['accounts'])
        self.assertFalse(compact_after['value']['computers'])
        self.assertFalse(compact_after['value']['offices'])
        self.assertEqual(compact_after['value']['record_versions'], {})

    def test_same_seed_wire_bytes_and_local_timing_abba(self):
        path = '/api/board?date=' + DAY + '&version_scope=day'
        roles = {}
        modes = ('identity', 'gzip', 'accounts_gzip')
        for role in ('owner', 'assigned'):
            with self.application():
                for mode in modes:
                    self.request(path + ('&compact=accounts' if mode == 'accounts_gzip' else ''),
                        'identity' if mode == 'identity' else 'gzip, deflate', role)
                samples = {mode: [] for mode in modes}
                stages = {mode: [] for mode in modes}
                field_bytes = {}
                for trial in range(5):
                    pair = {}
                    order = modes if trial % 2 == 0 else tuple(reversed(modes))
                    for mode in order:
                        result = self.request(path + ('&compact=accounts' if mode == 'accounts_gzip' else ''),
                            'identity' if mode == 'identity' else 'gzip, deflate', role)
                        self.assertEqual(result['status'], 200)
                        self.assert_headers(result, None if mode == 'identity' else 'gzip')
                        self.assertEqual(result['server']['connections'], 1)
                        pair[mode] = result
                        samples[mode].append({**result['client'], **result['server']['counts'],
                            'handler_ms': result['server']['handler_ms']})
                        stages[mode].append(result['server']['timer'])
                        field_bytes[mode] = {key: len(json.dumps(value, ensure_ascii=False).encode('utf-8'))
                            for key, value in result['value'].items()}
                    self.assertTrue(pair['identity']['decoded'] == pair['gzip']['decoded'], 'Measured coding changed bytes')
                    self.assertEqual(pair['identity']['server']['counts'], pair['gzip']['server']['counts'])
                    self.assertLess(len(pair['gzip']['encoded']), len(pair['identity']['encoded']))
                    self.assertEqual(pair['accounts_gzip']['value'], account_transport.compact(pair['identity']['value']))
                    self.assertEqual(pair['identity']['server']['counts'], pair['accounts_gzip']['server']['counts'])
                    self.assertLess(len(pair['accounts_gzip']['decoded']), len(pair['identity']['decoded']))
                    self.assertLess(len(pair['accounts_gzip']['encoded']), len(pair['gzip']['encoded']))
            roles[role] = {}
            for mode in modes:
                numeric_stages = stages[mode]
                self.assertTrue(all(type(value) in (float, int) and math.isfinite(value) and value >= 0
                    for row in numeric_stages for value in row.values()))
                stage_names = sorted(set.intersection(*(set(row) for row in numeric_stages)))
                roles[role][mode] = dict(summary=statistics_rows(samples[mode]),
                    samples=[{key: round(value, 3) for key, value in row.items()} for row in samples[mode]],
                    stages=statistics_rows([{name: row[name] for name in stage_names} for row in numeric_stages]),
                    top_level_value_bytes=field_bytes[mode])
        METRICS['wire_comparison'] = dict(order='one warmup each then ABC/CBA alternating, five samples each; identity/gzip retain AB/BA order',
            roles=roles, phases='server fixed stage durations are inclusive; not additive',
            timing_scope='loopback request headers/body/decompress/Python JSON only; not production or page usable time')


if __name__ == '__main__':
    unittest.main()
