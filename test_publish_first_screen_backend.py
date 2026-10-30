"""Same-snapshot scope reuse, against the explicit pre-change source freeze.

Only run through run_isolated_tests.py. Reuses the reviewed 1600-account,
800-device, 48000-daily synthetic fixture. No customer files or PG services.
Timing is local SQLite/Python/JSON, not production or network latency.
"""
from collections import defaultdict
from contextlib import ExitStack, contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

from test_isolation import checked_root, inside, PROJECT

checked_root()  # Must precede application and fixture imports.

import app
import auth
import office_scope
import operations
import test_performance_backend as support

DAY = support.DAY
METRICS = {}
BASELINE = PROJECT / '.synthetic-tests' / 'publish-firstscreen-before-20261029-r1'


def frozen(name):
    """Fail closed: this comparison must use the reviewed current-task freeze."""
    if (BASELINE != BASELINE.resolve() or not inside(BASELINE, PROJECT / '.synthetic-tests')
            or any(path.is_symlink() or path.is_junction() for path in (BASELINE, *BASELINE.parents))):
        raise ValueError('First-screen baseline must be explicit synthetic source without links')
    manifest = json.loads((BASELINE / 'SOURCE-MANIFEST.json').read_text(encoding='utf-8'))
    if manifest.get('kind') != 'publish-firstscreen-immutable-source-before' or manifest.get('production_data') is not False:
        raise ValueError('First-screen source baseline marker mismatch')
    expected = {row['Path']: row['SHA256'].lower() for row in manifest['files']}
    source = BASELINE / (name + '.py')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if digest != expected.get(source.name):
        raise ValueError('Frozen first-screen source hash mismatch')
    spec = importlib.util.spec_from_file_location('first_screen_baseline_' + name, source)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    METRICS.setdefault('source_sha256', {})[name] = dict(before=digest,
        after=hashlib.sha256((PROJECT / source.name).read_bytes()).hexdigest())
    return module


class PublishFirstScreenBackendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Reuse fixture construction, not the old TestCase's test methods.
        with patch.object(support, 'frozen_module', return_value=None):
            support.BackendPerformanceTest.setUpClass.__func__(cls)
        cls.before_scope, cls.before_operations, cls.before_app = (frozen(name) for name in ('office_scope', 'operations', 'app'))
        METRICS.update(kind='synthetic-SQLite-same-snapshot-scope-reuse', production_latency=False,
            accounts=1600, devices=800, daily_rows=48000, samples=5, order='warm-once-then-alternating-AB-BA',
            rows_definition='rows returned to Python, not SQL rows examined', business_payload='unchanged')

    def setUp(self):
        support.BackendPerformanceTest.setUp(self)
        # Separate case-only scope anomalies; the source seed is not modified.
        self.db.execute("INSERT INTO offices(id,name,active) VALUES('synthetic-inactive','合成停用办公室',0)")
        encoded = self.db.execute('SELECT password_hash FROM auth_users WHERE id=1').fetchone()[0]
        self.db.executemany('INSERT INTO auth_users(id,username,name,role,permissions,password_hash,created_at) VALUES(?,?,?,?,?,?,0)', [
            (3, 'synthetic_no_grants', '合成未授权员工', 'employee', '["publish"]', encoded),
            (4, 'synthetic_inactive_only', '合成仅停用办员工', 'employee', '["publish"]', encoded),
            (5, 'synthetic_admin', '合成管理员', 'admin', '[]', encoded)])
        self.db.execute("INSERT INTO user_office_grants VALUES(4,'synthetic-inactive')")
        self.db.execute("UPDATE account_offices SET office_id='synthetic-inactive' WHERE account_id=?", (self.ids[1399],))
        self.db.execute("INSERT INTO phone_card_offices VALUES(800,'synthetic-inactive')")
        self.db.execute("INSERT INTO phone_device_offices VALUES(?,'synthetic-inactive')", (self.devices[799],))
        self.actors = {name: auth.public_user(self.db.execute('SELECT * FROM auth_users WHERE id=?', (ident,)).fetchone())
                       for name, ident in [('owner', 1), ('assigned', 2), ('no_grants', 3), ('inactive_only', 4), ('admin', 5)]}

    def context(self, scope, actor):
        with patch.dict(sys.modules, {'office_scope': scope}):
            return scope.read_context(self.db, actor)

    def test_context_scope_values_match_all_roles_and_refresh_actor_only_once(self):
        for name, actor in self.actors.items():
            with self.subTest(role=name):
                self.db.reset_meter()
                old = self.context(self.before_scope, actor)
                before = self.db.meter.copy()
                self.db.reset_meter()
                with patch.object(office_scope, 'current_user', wraps=office_scope.current_user) as refreshed:
                    new = self.context(office_scope, actor)
                self.assertEqual(refreshed.call_count, 1)
                self.assertEqual({key: value for key, value in new.items() if key != 'actor_id'}, old)
                self.assertEqual(new['actor_id'], actor['id'])
                self.assertEqual(self.db.meter['selects'], before['selects'] - 5)
                self.assertEqual(self.db.meter['rows_returned'], before['rows_returned'] - 5)
                if name in ('no_grants', 'inactive_only'):
                    self.assertFalse(new['grants'])
                    self.assertTrue(all(not ids for ids in new['ids'].values()))
        assigned = self.context(office_scope, self.employee)
        self.assertIn(self.devices[450], assigned['ids']['devices'])  # Legacy multi-office read stays visible.
        self.assertIn(451, assigned['ids']['cards'])
        self.assertNotIn(self.ids[1399], assigned['ids']['accounts'])

    def test_new_reads_and_public_write_checks_never_reuse_stale_permissions(self):
        stale_owner = dict(self.owner)
        self.db.execute("UPDATE auth_users SET role='employee' WHERE id=1")
        before = self.context(self.before_scope, stale_owner)
        current = self.context(office_scope, stale_owner)
        self.assertFalse(current['global_access'])
        self.assertEqual({key: value for key, value in current.items() if key != 'actor_id'}, before)
        self.assertEqual(office_scope.visible_ids(self.db, stale_owner, 'accounts'), set())
        self.assertEqual(office_scope.granted_offices(self.db, stale_owner), set())
        with self.assertRaises(auth.AccessError):
            office_scope.require_entity(self.db, stale_owner, 'accounts', support.ACCOUNT, write=True)
        self.db.execute('UPDATE auth_users SET active=0 WHERE id=1')
        for function in (lambda: office_scope.read_context(self.db, stale_owner),
                         lambda: office_scope.visible_ids(self.db, stale_owner, 'accounts'),
                         lambda: office_scope.granted_offices(self.db, stale_owner),
                         lambda: office_scope.list_offices(self.db, stale_owner)):
            with self.assertRaises(auth.AccessError):
                function()

    def test_versions_reuse_existing_maps_and_match_raw_row_tokens_for_dates_and_roles(self):
        for name, actor in self.actors.items():
            old_context, context = self.context(self.before_scope, actor), self.context(office_scope, actor)
            for day in (None, DAY, '2026-10-20', support.MISSING_DAY):
                with self.subTest(role=name, day=day):
                    options = {} if day is None else dict(daily_day=day)
                    self.db.reset_meter()
                    with patch.dict(sys.modules, {'office_scope': self.before_scope}):
                        expected = self.before_operations.versions(self.db, old_context, **options)
                    before = self.db.meter.copy()
                    self.db.reset_meter()
                    with patch.object(office_scope, 'scope_maps', side_effect=AssertionError('re-reading a same-transaction scope map')):
                        actual = operations.versions(self.db, context, **options)
                    self.assertEqual(actual, expected)
                    self.assertEqual(self.db.meter['selects'], before['selects'] - 7)
                    self.assertLess(self.db.meter['rows_returned'], before['rows_returned'])
                    if name in ('no_grants', 'inactive_only'):
                        self.assertEqual(actual, {})
        context = self.context(office_scope, self.owner)
        raw = dict(self.db.execute('SELECT * FROM accounts WHERE id=?', (support.ACCOUNT,)).fetchone())
        raw['office_ids'] = context['maps']['accounts'][support.ACCOUNT]
        self.assertEqual(operations.versions(self.db, context, daily_day=DAY)['account:' + support.ACCOUNT], operations.token(raw))

    def test_list_offices_default_is_unchanged_and_explicit_context_is_actor_bound(self):
        for name, actor in self.actors.items():
            with self.subTest(role=name):
                expected = self.before_scope.list_offices(self.db, actor)
                self.assertEqual(office_scope.list_offices(self.db, actor), expected)
                context = self.context(office_scope, actor)
                self.db.reset_meter()
                with patch.object(office_scope, 'current_user', side_effect=AssertionError('duplicate actor lookup')):
                    self.assertEqual(office_scope.list_offices(self.db, actor, context), expected)
                self.assertEqual(self.db.meter['selects'], 1)
        context = self.context(office_scope, self.owner)
        with self.assertRaises(auth.AccessError):
            office_scope.list_offices(self.db, self.employee, context)
        with self.assertRaises(auth.AccessError):
            office_scope.list_offices(self.db, None, context)

    def test_snapshot_and_versions_defaults_keep_legacy_call_shape_and_values(self):
        with patch.dict(sys.modules, {'office_scope': self.before_scope}):
            expected = self.before_operations.snapshot(self.db)
        self.assertEqual(operations.snapshot(self.db), expected)
        with patch.object(operations, 'snapshot', return_value={}) as snapshot:
            self.assertEqual(operations.versions(self.db), {})
            snapshot.assert_called_once_with(self.db)
        with patch.object(operations, 'snapshot', return_value={}) as snapshot:
            self.assertEqual(operations.versions(self.db, daily_day=DAY), {})
            snapshot.assert_called_once_with(self.db, daily_day=DAY)
        self.db.scope_enabled = False
        self.assertEqual(operations.snapshot(self.db), self.before_operations.snapshot(self.db))
        self.assertEqual(operations.versions(self.db), self.before_operations.versions(self.db))

    def seed_active_layout(self):
        """Named extension, identical on both paths; original fixture unchanged."""
        computers = ['synth-pc-1'] + ['synthetic-screen-pc-' + str(number) for number in range(1, 160)]
        self.db.executemany('INSERT INTO computers VALUES(?,?,?,?,?,?)',
            [(ident, '合成发布电脑' + str(index), '', '', '', '') for index, ident in enumerate(computers) if index])
        self.db.executemany('INSERT INTO computer_offices VALUES(?,?)',
            [(ident, 'one' if index < 80 else 'two') for index, ident in enumerate(computers) if index])
        self.db.execute("INSERT INTO publishing_layout_state VALUES(1,'synthetic-first-screen-active')")
        self.db.executemany('INSERT INTO publishing_layout VALUES(?,?,?,?,?,?,?,?,?)',
            [('synthetic-first-screen-active', computers[index // 10], index % 10 + 1, ident,
              '合成位置' + str(index), '合成办公室', 'H' + str(index + 2), 'synthetic-only', '')
             for index, ident in enumerate(self.ids)])

    def measure_board(self, module, scope, versions, actor, day=DAY):
        token = 'synthetic-first-screen-session-' + str(actor['id'])
        self.db.execute('INSERT OR REPLACE INTO auth_sessions VALUES(?,?,?,?)',
                        (auth.digest(token), actor['id'], 'synthetic-first-screen-csrf', 2000000000))
        self.db.commit()
        captured, connections, stages = {}, [], defaultdict(list)

        @contextmanager
        def synthetic_connection():
            connections.append(True)
            try:
                yield self.db
            finally:
                self.db.rollback()

        def observed(name, function):
            def wrapped(*args, **kwargs):
                meter = {key: self.db.meter[key] for key in ('queries', 'selects', 'rows_returned')}
                started = time.perf_counter()
                try:
                    return function(*args, **kwargs)
                finally:
                    stages[name].append(dict(milliseconds=(time.perf_counter() - started) * 1000,
                        **{key: self.db.meter[key] - value for key, value in meter.items()}))
            return wrapped

        handler = object.__new__(module.Handler)
        handler.path = '/api/board?date=' + day + '&version_scope=day'
        handler.headers = {'Cookie': 'board_session=' + token}

        def reply(value, status=200, headers=None):
            started = time.perf_counter()
            encoded = json.dumps(value, ensure_ascii=False).encode()
            stages['json'].append(dict(milliseconds=(time.perf_counter() - started) * 1000,
                                       queries=0, selects=0, rows_returned=0))
            captured.update(body=value, status=status, response_bytes=len(encoded))

        handler.reply = reply
        self.db.reset_meter()
        started = time.perf_counter()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {'office_scope': scope}))
            stack.enter_context(patch.dict(os.environ, {'DOUYIN_BOARD_TIMING': '0'}))
            stack.enter_context(patch.object(module, 'connect', synthetic_connection))
            stack.enter_context(patch.object(module, 'today', return_value=DAY))
            stack.enter_context(patch.object(module, 'office_scope', scope))
            stack.enter_context(patch.object(module, 'operations', versions))
            functions = [(scope, 'read_context', 'context'), (scope, 'scope_maps', 'scope_maps'),
                         (scope, 'list_offices', 'offices'), (versions, 'versions', 'versions'),
                         (versions, 'snapshot', 'snapshot'), (module.account_lifecycle, 'enrich', 'lifecycle'),
                         (module.auth, 'session', 'auth'), (module.collection, 'overview', 'collection'),
                         (module.phone_cards, 'overview', 'cards'), (module.phone_devices, 'overview', 'devices'),
                         (module.device_workflow, 'overview', 'workflow'), (module.publishing_layout, 'overview', 'layout'),
                         (module.nurture, 'overview', 'nurture')]
            for owner, attribute, stage in functions:
                stack.enter_context(patch.object(owner, attribute, observed(stage, getattr(owner, attribute))))
            handler.get_response()
        total = {key: value for key, value in self.db.meter.items() if key != 'sql'}
        total.update(milliseconds=(time.perf_counter() - started) * 1000, connect_calls=len(connections),
                     response_bytes=captured['response_bytes'])
        self.assertEqual(captured['status'], 200)
        stages = {name: dict(calls=len(rows), **{key: sum(row[key] for row in rows)
                  for key in ('milliseconds', 'queries', 'selects', 'rows_returned')}) for name, rows in stages.items()}
        return captured['body'], total, stages

    def test_complete_board_payload_tokens_and_staged_cost_match_current_freeze(self):
        self.seed_active_layout()
        role_metrics = {}
        for role in ('owner', 'assigned', 'no_grants'):
            actor = self.actors[role]
            before = lambda: self.measure_board(self.before_app, self.before_scope, self.before_operations, actor)
            after = lambda: self.measure_board(app, office_scope, operations, actor)
            self.assertEqual(before()[0], after()[0])  # Warm both once.
            rows = {'before': [], 'after': []}
            stage_rows = {'before': defaultdict(list), 'after': defaultdict(list)}
            for iteration in range(5):
                order = ('before', 'after') if iteration % 2 == 0 else ('after', 'before')
                pair = {}
                for label in order:
                    body, meter, stages = before() if label == 'before' else after()
                    pair[label] = body
                    rows[label].append(meter)
                    for stage, values in stages.items():
                        stage_rows[label][stage].append(values)
                    self.assertEqual(meter['connect_calls'], 1)
                self.assertEqual(pair['before'], pair['after'])
            self.assertLess(rows['after'][0]['selects'], rows['before'][0]['selects'])
            self.assertLess(rows['after'][0]['rows_returned'], rows['before'][0]['rows_returned'])
            role_metrics[role] = {label: dict(total=support.summary(rows[label]), stages={
                stage: {**support.summary(values), 'calls': values[0]['calls']} for stage, values in stage_rows[label].items()})
                for label in rows}
        METRICS['board'] = dict(scenario='existing-1600-800-48000-plus-active-layout-and-inactive-office',
            computers=160, active_layout_cells=1600, roles=role_metrics,
            timing_scope='detached real Handler GET and JSON; injected SQLite connection; no HTTP, PG, TLS or connection setup',
            stage_definition='inclusive timings/returned-row deltas; nested scope_maps not additive')
        self.assertEqual(self.db.execute('SELECT count(*) FROM daily').fetchone()[0], 48000)


if __name__ == '__main__':
    unittest.main()
