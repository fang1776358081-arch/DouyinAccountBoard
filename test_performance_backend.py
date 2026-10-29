"""Same-seed SQLite comparisons against an explicit frozen source baseline.

No production paths or PostgreSQL services. Timing measures local SQLite/Python,
not network latency. The baseline is optional outside this local investigation.
"""
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sqlite3
import statistics
import sys
import time
import types
import unittest
from contextlib import contextmanager
from datetime import date, timedelta
from unittest.mock import patch
from uuid import uuid4

from test_isolation import checked_database, checked_root, inside, PROJECT

checked_root()  # Must run before importing application code.

import app
import auth
import device_workflow
import idempotency
import nurture
import office_scope
import operations
import phone_cards
import phone_devices
import test_auth_support

DAY = '2026-10-29'
MISSING_DAY = '2026-08-01'
ACCOUNT = 'synth-account-1'
METRICS = {}


class CountCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        meter = self.connection.meter
        meter['queries'] += 1
        if sql.lstrip().upper().startswith('SELECT'):
            meter['selects'] += 1
            meter['sql'].append(sql)
        if sql.endswith(' FOR UPDATE'):
            sql = sql[:-len(' FOR UPDATE')]
        return super().execute(sql, parameters)

    def fetchone(self):
        value = super().fetchone()
        self.connection.meter['rows_returned'] += value is not None
        return value

    def fetchall(self):
        values = super().fetchall()
        self.connection.meter['rows_returned'] += len(values)
        return values

    def __next__(self):
        value = super().__next__()
        self.connection.meter['rows_returned'] += 1
        return value


class CountConnection(sqlite3.Connection):
    scope_enabled = True
    restore_generation = 'synthetic-performance-generation'

    def reset_meter(self):
        self.meter = dict(queries=0, selects=0, rows_returned=0, sql=[])

    def execute(self, sql, parameters=()):
        if not hasattr(self, 'meter'):
            self.reset_meter()
        return self.cursor(CountCursor).execute(sql, parameters)


def frozen_module(name):
    folder = Path(os.environ.get('DOUYIN_BOARD_PERFORMANCE_BASELINE',
        PROJECT / '.synthetic-tests' / 'performance-source-baseline-20261029')).absolute()
    if folder != folder.resolve() or not inside(folder, PROJECT / '.synthetic-tests'):
        raise ValueError('Performance baseline must be explicit synthetic source, without links')
    if any(path.is_symlink() or path.is_junction() for path in (folder, *folder.parents)):
        raise ValueError('Linked performance baseline prohibited')
    path = folder / (name + '.py')
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location('frozen_performance_' + name, path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    METRICS.setdefault('source_sha256', {})[name] = dict(
        baseline=hashlib.sha256(path.read_bytes()).hexdigest(),
        current=hashlib.sha256((PROJECT / (name + '.py')).read_bytes()).hexdigest())
    return module


def summary(samples):
    result = {}
    for field in ('milliseconds', 'queries', 'selects', 'rows_returned', 'response_bytes', 'connect_calls'):
        if not all(field in sample for sample in samples):
            continue
        values = sorted(sample[field] for sample in samples)
        result[field] = dict(p50=statistics.median(values), p95=values[math.ceil(.95 * len(values)) - 1])
    return result


class BackendPerformanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if app._DATABASE_PROVIDER is not None:
            raise ValueError('Synthetic SQLite fixture cannot use an external provider')
        cls.folder = checked_root() / ('backend-performance-' + uuid4().hex)
        cls.folder.mkdir()
        cls.seed = checked_database(cls.folder / 'seed.db')
        with patch.object(app, 'DB', cls.seed):
            app.initialize()
        with sqlite3.connect(cls.seed) as db:
            db.execute('PRAGMA foreign_keys=ON')
            migrations = PROJECT / 'migrations' / 'postgresql'
            for filename in ('0002_office_scope.sql', '0003_idempotency.sql'):
                db.executescript((migrations / filename).read_text(encoding='utf-8'))
            db.executemany('INSERT INTO offices(id,name) VALUES(?,?)', [('one', '合成一办'), ('two', '合成二办')])
            encoded = auth.password_hash(test_auth_support.TEST_PASSWORD)
            db.executemany('INSERT INTO auth_users(id,username,name,role,permissions,password_hash,created_at) VALUES(?,?,?,?,?,?,0)',
                [(1, 'synth_perf_owner', '合成总管理员', 'owner', '[]', encoded),
                 (2, 'synth_perf_employee', '合成员工', 'employee', '["publish"]', encoded)])
            db.execute("INSERT INTO user_office_grants VALUES(2,'one')")
            db.execute("INSERT INTO computer_offices VALUES('synth-pc-1','one')")
            cls.ids = [ACCOUNT] + ['synth-perf-' + str(i) for i in range(1, 1600)]
            cls.ids[200] = 'synth:colon-account'
            cls.devices = [str(i + 1) if i < 400 else 'office3:' + str(i - 399) for i in range(800)]
            db.executemany('INSERT INTO phone_devices(id,name,state,state_source) VALUES(?,?,?,?)',
                [(ident, '合成手机' + str(i), '在用' if i % 2 else '可用闲置', 'manual' if i % 3 else 'auto') for i, ident in enumerate(cls.devices)])
            db.executemany('INSERT INTO phone_device_offices VALUES(?,?)',
                [(ident, 'one' if i < 400 else 'two') for i, ident in enumerate(cls.devices) if i < 700] + [(cls.devices[450], 'one')])
            db.executemany('INSERT INTO accounts(id,name,status,note,phone_device_id) VALUES(?,?,?,?,?)',
                [(ident, '合成账号' + str(i), ['正常', '封禁', '停用', '待确认'][i % 4], '合成旧备注', cls.devices[i % 800] if i % 13 else None)
                 for i, ident in enumerate(cls.ids) if i])
            db.execute('UPDATE accounts SET phone_device_id=? WHERE id=?', (cls.devices[0], ACCOUNT))
            db.execute("UPDATE accounts SET status='养号7天' WHERE id=?", (cls.ids[11],))
            db.execute("UPDATE accounts SET registered_on='2026-10-28' WHERE id=?", (cls.ids[17],))
            db.executemany('INSERT INTO account_offices VALUES(?,?)',
                [(ident, 'one' if i < 800 else 'two') for i, ident in enumerate(cls.ids) if i < 1400])
            days = [(date.fromisoformat(DAY) - timedelta(days=i)).isoformat() for i in range(30)]
            db.executemany('INSERT INTO daily VALUES(?,?,?)',
                [(ident, day, (i + offset) % 2) for offset, day in enumerate(days) for i, ident in enumerate(cls.ids)])
            db.executemany('INSERT INTO phone_units(id,device_id,original_device_id,generation,status,created_at) VALUES(?,?,?,1,?,1)',
                [('unit-' + str(i), ident, ident, '在用') for i, ident in enumerate(cls.devices) if i != 799])
            db.execute("UPDATE phone_units SET active=0,status='送修' WHERE id='unit-28'")
            db.execute("INSERT INTO phone_units(id,device_id,original_device_id,generation,status,created_at) VALUES('replacement-29','29','29',2,'可用闲置',2)")
            db.execute("INSERT INTO phone_units(id,device_id,original_device_id,generation,active,status,created_at) VALUES('renumbered-old',?,'29',2,0,'返修待验',3)", (cls.devices[85],))
            db.executemany('INSERT INTO phone_account_access(account_id,last_unit_id) VALUES(?,?)',
                [(ident, 'unit-' + str(i % 799)) for i, ident in enumerate(cls.ids) if i % 11])
            db.executemany('INSERT INTO phone_cards(id,phone,location) VALUES(?,?,?)',
                [(i + 1, str(19900000000 + i), '合成待核对位置') for i in range(800)])
            db.executemany('INSERT INTO phone_card_offices VALUES(?,?)',
                [(i + 1, 'one' if i < 400 else 'two') for i in range(700)] + [(451, 'one')])
            db.executemany("INSERT INTO phone_card_placements(card_id,unit_id,slot,updated_at) VALUES(?,?,'待确认',1)",
                [(i + 1, 'unit-' + str(i)) for i in range(20)])
            db.execute("INSERT INTO device_cases(id,unit_id,status,reason,created_at,updated_at,account_ids) VALUES('case-one','unit-28','处理中','合成故障',1,2,?)", (json.dumps([cls.ids[28]]),))
            db.execute("UPDATE phone_account_access SET login_state='登录受阻',case_id='case-one',reason='合成故障' WHERE account_id=?", (cls.ids[28],))
            db.execute("INSERT INTO device_workflow_events(id,unit_id,actor_name,at,action,detail) VALUES('event-one','unit-28','合成人员',2,'open_case',?)", (json.dumps(dict(affected_account_ids=[cls.ids[28]])),))
            db.execute("INSERT INTO nurture_records(account_id,workflow,started_on) VALUES(?,'待开始','2026-10-20')", (cls.ids[10],))
            db.execute("INSERT INTO nurture_records(account_id,workflow,active_since) VALUES(?,'运行','2026-10-20')", (cls.ids[2],))
        cls.baseline = {name: frozen_module(name) for name in ('operations', 'phone_devices', 'device_workflow')}
        METRICS.update(kind='isolated-synthetic-SQLite-not-production-PostgreSQL', accounts=1600, devices=800, daily_rows=48000,
                       samples=5, rows_definition='rows returned to Python, not SQL engine rows examined')

    def setUp(self):
        self.path = checked_database(self.folder / ('case-' + uuid4().hex + '.db'))
        with sqlite3.connect(self.seed) as source, sqlite3.connect(self.path) as target:
            source.backup(target)
        self.db = sqlite3.connect(self.path, factory=CountConnection)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.addCleanup(self.db.close)
        self.owner = auth.public_user(self.db.execute('SELECT * FROM auth_users WHERE id=1').fetchone())
        self.employee = auth.public_user(self.db.execute('SELECT * FROM auth_users WHERE id=2').fetchone())

    def expected(self, ident=ACCOUNT, day=DAY):
        return operations.versions_for(self.db, ['account:' + ident, 'daily:' + ident + ':' + day])

    def daily_run(self, module, day=DAY, published=True, ident=ACCOUNT):
        body = dict(id=ident, date=day, published=published, _expected_versions=self.expected(ident, day))
        self.db.execute('SAVEPOINT compare_daily')
        handler = types.SimpleNamespace()
        self.db.reset_meter()
        started = time.perf_counter()
        with patch.object(module.time, 'time', return_value=1900000000), patch.object(module.uuid, 'uuid4', return_value=types.SimpleNamespace(hex='synthetic-comparison-group')):
            state = module.before(self.db, '/api/daily', body)
            result = app.Handler._save_daily(None, self.db, body)
            module.finish(handler, self.db, self.employee, '/api/daily', state)
        response = {**result, '_record_versions': handler._version_delta}
        meter = {key: value for key, value in self.db.meter.items() if key != 'sql'}
        meter.update(milliseconds=round((time.perf_counter() - started) * 1000, 3), response_bytes=len(json.dumps(response, ensure_ascii=False).encode()))
        history = [dict(row) for row in self.db.execute('SELECT * FROM operations_history ORDER BY id')]
        self.db.execute('ROLLBACK TO compare_daily')
        self.db.execute('RELEASE compare_daily')
        return response, history, meter

    def require_baseline(self, name):
        module = self.baseline[name]
        if module is None:
            self.skipTest('Explicit frozen source baseline not supplied')
        return module

    def test_daily_history_tokens_and_unchanged_records_match_frozen_baseline(self):
        baseline = self.require_baseline('operations')
        before = operations.token(operations.snapshot(self.db))
        for ident, day, published in ((ACCOUNT, DAY, True), (ACCOUNT, DAY, False), (ACCOUNT, MISSING_DAY, True), (self.ids[1499], DAY, False), (self.ids[200], MISSING_DAY, True)):
            old = self.daily_run(baseline, day, published, ident)
            new = self.daily_run(operations, day, published, ident)
            self.assertEqual(new[:2], old[:2])
            self.assertEqual(set(new[0]['_record_versions']), {'account:' + ident, 'daily:' + ident + ':' + day})
            if new[1]:
                self.assertEqual(json.loads(new[1][0]['scope_offices'] or 'null'), None if ident == self.ids[1499] else ['one'])
        self.assertEqual(operations.token(operations.snapshot(self.db)), before)

    def test_scoped_daily_does_not_scan_other_tables_or_sync_nurture(self):
        expected = self.expected()
        self.db.reset_meter()
        with patch.object(operations, 'snapshot', side_effect=AssertionError('full snapshot on daily')), patch.object(nurture, 'sync', side_effect=AssertionError('nurture sync on daily')):
            state = operations.before(self.db, '/api/daily', dict(id=ACCOUNT, date=DAY, _expected_versions=expected))
            self.assertEqual(len(state), 2)
            app.Handler._save_daily(None, self.db, dict(id=ACCOUNT, date=DAY, published=True))
            operations.finish(types.SimpleNamespace(), self.db, self.employee, '/api/daily', state)
        self.assertEqual(self.db.meter['selects'], 7)  # Six snapshot reads plus account existence check.
        self.assertLessEqual(self.db.meter['rows_returned'], 7)

    def test_other_writes_and_unscoped_daily_keep_the_full_snapshot_path(self):
        for scoped, path in ((True, '/api/account/status'), (False, '/api/daily')):
            self.db.scope_enabled = scoped
            with patch.object(operations, 'snapshot', wraps=operations.snapshot) as snapshot:
                with self.assertRaises(operations.ConflictError):
                    operations.before(self.db, path, dict(id=ACCOUNT, date=DAY, _expected_versions={}))
            self.assertEqual(snapshot.call_count, 1)
        self.db.scope_enabled = True

    def test_day_scoped_versions_only_omit_other_dates_without_changing_history(self):
        before = self.db.execute('SELECT count(*) FROM daily').fetchone()[0]
        for actor in (self.owner, self.employee):
            context = office_scope.read_context(self.db, actor)
            full = operations.versions(self.db, context)
            self.db.reset_meter()
            selected = operations.versions(self.db, context, daily_day=DAY)
            expected = {key: value for key, value in full.items() if not key.startswith('daily:') or key.rsplit(':', 1)[-1] == DAY}
            self.assertEqual(selected, expected)
            self.assertIn('SELECT * FROM daily WHERE day=?', self.db.meter['sql'])
            self.assertNotIn('SELECT * FROM daily', self.db.meter['sql'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM daily').fetchone()[0], before)
        with patch.object(operations, 'snapshot', return_value={}) as snapshot:
            self.assertEqual(operations.versions(self.db), {})
            snapshot.assert_called_once_with(self.db)

    def test_daily_versions_are_one_batch_include_missing_null_and_preserve_scope_tokens(self):
        self.db.execute('DELETE FROM daily WHERE day=? AND account_id IN (?,?)', (DAY, ACCOUNT, self.ids[200]))
        for actor in (self.owner, self.employee):
            context = office_scope.read_context(self.db, actor)
            full = operations.versions(self.db, context)
            self.db.reset_meter()
            selected = operations.daily_versions(self.db, DAY, context)
            self.assertEqual(self.db.meter['selects'], 1)
            self.assertEqual(set(selected), {'daily:' + ident + ':' + DAY for ident in context['ids']['accounts']})
            for key, value in selected.items():
                self.assertEqual(value, full.get(key))
            self.assertIsNone(selected['daily:' + ACCOUNT + ':' + DAY])
            self.assertIsNone(selected['daily:' + self.ids[200] + ':' + DAY])
            self.assertIsNotNone(selected['daily:' + self.ids[2] + ':' + DAY])
            if actor['role'] == 'employee':
                self.assertNotIn('daily:' + self.ids[900] + ':' + DAY, selected)
                self.assertNotIn('daily:' + self.ids[1499] + ':' + DAY, selected)
        # Exercise the production adapter's mapping/positional row interface,
        # without a PostgreSQL process or connection.
        from pg_service_adapter import ServiceRow
        self.db.row_factory = lambda cursor, values: ServiceRow(tuple(column[0] for column in cursor.description), values)
        try:
            self.db.reset_meter()
            selected = operations.daily_versions(self.db, DAY)
            self.assertEqual(len(selected), 1600)
            self.assertEqual(self.db.meter['selects'], 3)
            self.assertIsNone(selected['daily:' + ACCOUNT + ':' + DAY])
        finally:
            self.db.row_factory = sqlite3.Row

    def test_employee_scope_conflicts_and_idempotent_replay_match_baseline(self):
        baseline = self.require_baseline('operations')
        observations = []
        for module in (baseline, operations):
            self.db.execute('SAVEPOINT protocol_compare')
            body = dict(id=ACCOUNT, date=DAY, published=True, request_id='00000000-0000-4000-8000-000000000001', _expected_versions=self.expected())
            auth.authorize_route(self.db, self.employee, '/api/daily', body)
            with self.assertRaises(auth.AccessError):
                auth.authorize_route(self.db, self.employee, '/api/daily', {**body, 'id': self.ids[900]})
            with self.assertRaises(auth.AccessError):
                auth.authorize_route(self.db, self.employee, '/api/daily', {**body, 'id': self.ids[1499]})
            with self.assertRaises(module.ConflictError):
                module.before(self.db, '/api/daily', {**body, '_expected_versions': {}})
            self.assertEqual(idempotency.claim(self.db, self.employee, '/api/daily', body)[0], 'reserved')
            state = module.before(self.db, '/api/daily', body)
            result = app.Handler._save_daily(None, self.db, body)
            handler = types.SimpleNamespace()
            module.finish(handler, self.db, self.employee, '/api/daily', state)
            result['_record_versions'] = handler._version_delta
            idempotency.complete(self.db, self.employee, body, 'succeeded', 200, result)
            self.assertEqual(idempotency.claim(self.db, self.employee, '/api/daily', body)[0], 'replay')
            with self.assertRaises(idempotency.IdempotencyError):
                idempotency.claim(self.db, self.employee, '/api/daily', {**body, 'published': False})
            observations.append((result, self.db.execute('SELECT count(*) FROM operations_history').fetchone()[0]))
            self.db.execute('ROLLBACK TO protocol_compare')
            self.db.execute('RELEASE protocol_compare')
        self.assertEqual(observations[0], observations[1])

    def overview_run(self, phones, workflow, actor):
        context = office_scope.read_context(self.db, actor) if self.db.scope_enabled else None
        accounts = [dict(row) for row in self.db.execute('SELECT * FROM accounts ORDER BY id')]
        if context:
            _, accounts = office_scope.filter_board_entities(context, [], accounts)
        cards = phone_cards.overview(self.db, context['ids']['cards'] if context else None, accounts if context else None)
        self.db.reset_meter()
        started = time.perf_counter()
        devices = phones.overview(self.db, accounts, context['ids']['devices'] if context else None)
        workflow = workflow.overview(self.db, accounts, devices['devices'], cards['cards'], actor=actor)
        result = dict(accounts=accounts, devices=devices, cards=cards, workflow=workflow)
        meter = {key: value for key, value in self.db.meter.items() if key != 'sql'}
        meter.update(milliseconds=round((time.perf_counter() - started) * 1000, 3), response_bytes=len(json.dumps(result, ensure_ascii=False).encode()))
        return result, meter

    def test_device_overviews_match_every_field_order_and_scope(self):
        old_phones, old_workflow = self.require_baseline('phone_devices'), self.require_baseline('device_workflow')
        for actor in (self.owner, self.employee):
            old = self.overview_run(old_phones, old_workflow, actor)
            new = self.overview_run(phone_devices, device_workflow, actor)
            self.assertEqual(new[0], old[0])
            self.assertEqual(new[1]['response_bytes'], old[1]['response_bytes'])
            self.assertEqual(new[1]['queries'], old[1]['queries'])
            self.assertEqual(new[1]['rows_returned'], old[1]['rows_returned'])
            units = new[0]['workflow']['units']
            if actor['role'] == 'owner':
                self.assertIn('renumbered-old', [unit['id'] for unit in units])
                device = next(row for row in new[0]['devices']['devices'] if row['id'] == '29')
                self.assertEqual(device['current_unit']['id'], 'replacement-29')
                self.assertEqual([unit['id'] for unit in device['unit_history']], ['unit-28', 'replacement-29', 'renumbered-old'])
            else:
                self.assertNotIn('unit-799', [unit['id'] for unit in units])
        self.db.scope_enabled = False
        old = self.overview_run(old_phones, old_workflow, self.owner)
        new = self.overview_run(phone_devices, device_workflow, self.owner)
        self.assertEqual(new[0], old[0])

    def test_same_seed_daily_and_overview_performance_comparison(self):
        old_operations = self.require_baseline('operations')
        old_phones, old_workflow = self.require_baseline('phone_devices'), self.require_baseline('device_workflow')
        daily_old, daily_new, device_old, device_new = [], [], [], []
        # Warm each path once, then alternate AB/BA to reduce ordering bias.
        self.daily_run(old_operations);self.daily_run(operations)
        self.overview_run(old_phones, old_workflow, self.owner);self.overview_run(phone_devices, device_workflow, self.owner)
        for iteration in range(5):
            if iteration % 2:
                after, before = self.daily_run(operations), self.daily_run(old_operations)
            else:
                before, after = self.daily_run(old_operations), self.daily_run(operations)
            self.assertEqual(before[:2], after[:2])
            daily_old.append(before[2]);daily_new.append(after[2])
            if iteration % 2:
                after, before = self.overview_run(phone_devices, device_workflow, self.owner), self.overview_run(old_phones, old_workflow, self.owner)
            else:
                before, after = self.overview_run(old_phones, old_workflow, self.owner), self.overview_run(phone_devices, device_workflow, self.owner)
            self.assertEqual(before[0], after[0])
            device_old.append(before[1]);device_new.append(after[1])
        METRICS['daily'] = dict(before=summary(daily_old), after=summary(daily_new))
        METRICS['device_overview'] = dict(before=summary(device_old), after=summary(device_new))
        self.assertLess(daily_new[0]['queries'], daily_old[0]['queries'] / 3)
        self.assertLess(daily_new[0]['rows_returned'], daily_old[0]['rows_returned'] / 100)

    def test_complete_board_handler_same_seed_payload_and_remaining_cost(self):
        baseline_app = frozen_module('app')
        if baseline_app is None:
            self.skipTest('Explicit frozen app source baseline not supplied')
        for name in ('operations', 'phone_devices', 'device_workflow'):
            self.require_baseline(name)
        token = 'synthetic-performance-session'
        self.db.execute('INSERT INTO auth_sessions VALUES(?,?,?,?)',
                        (auth.digest(token), 1, 'synthetic-performance-csrf', 2000000000))
        self.db.commit()

        def measure(module, dependencies, query=''):
            captured, connections = {}, []

            @contextmanager
            def synthetic_connection():
                connections.append(True)
                try:
                    yield self.db
                finally:
                    self.db.rollback()

            handler = object.__new__(module.Handler)
            handler.path = '/api/board?date=' + DAY + query
            handler.headers = {'Cookie': 'board_session=' + token}

            def reply(value, status=200, headers=None):
                captured.update(body=value, status=status,
                                encoded=json.dumps(value, ensure_ascii=False).encode())

            handler.reply = reply
            self.db.reset_meter()
            started = time.perf_counter()
            with patch.object(module, 'connect', synthetic_connection), patch.object(module, 'today', return_value=DAY), \
                    patch.object(module, 'operations', dependencies['operations']), \
                    patch.object(module, 'phone_devices', dependencies['phone_devices']), \
                    patch.object(module, 'device_workflow', dependencies['device_workflow']):
                handler.get_response()
            meter = {key: value for key, value in self.db.meter.items() if key != 'sql'}
            meter.update(milliseconds=round((time.perf_counter() - started) * 1000, 3),
                         response_bytes=len(captured['encoded']), connect_calls=len(connections))
            self.assertEqual(captured['status'], 200)
            return captured['body'], meter

        current = dict(operations=operations, phone_devices=phone_devices, device_workflow=device_workflow)
        first_old, first_new = measure(baseline_app, self.baseline), measure(app, current)
        self.assertEqual(first_old[0], first_new[0])
        old_samples, new_samples = [], []
        for iteration in range(5):
            if iteration % 2:
                after, before = measure(app, current), measure(baseline_app, self.baseline)
            else:
                before, after = measure(baseline_app, self.baseline), measure(app, current)
            self.assertEqual(before[0], after[0])
            old_samples.append(before[1]);new_samples.append(after[1])
        METRICS['complete_board_handler'] = dict(before=summary(old_samples), after=summary(new_samples),
            first_call_before=first_old[1], first_call_after=first_new[1],
            visible_accounts=len(first_new[0]['accounts']), version_keys=len(first_new[0]['record_versions']),
            daily_version_keys=sum(key.startswith('daily:') for key in first_new[0]['record_versions']),
            timing_scope='real Handler GET + JSON encoding; injected local SQLite connection, no HTTP/TLS/PG connection setup')
        compact_first = measure(app, current, '&version_scope=day')
        expected = {**first_new[0], 'record_versions': {key: value for key, value in first_new[0]['record_versions'].items()
                    if not key.startswith('daily:') or key.rsplit(':', 1)[-1] == DAY}}
        self.assertEqual(compact_first[0], expected)
        full_samples, compact_samples = [], []
        for iteration in range(5):
            if iteration % 2:
                compact, full = measure(app, current, '&version_scope=day'), measure(app, current)
            else:
                full, compact = measure(app, current), measure(app, current, '&version_scope=day')
            self.assertEqual(compact[0], {**full[0], 'record_versions': expected['record_versions']})
            full_samples.append(full[1]);compact_samples.append(compact[1])
        METRICS['day_version_board_handler'] = dict(full=summary(full_samples), compact=summary(compact_samples),
            first_compact=compact_first[1], version_keys=len(compact_first[0]['record_versions']),
            daily_version_keys=sum(key.startswith('daily:') for key in compact_first[0]['record_versions']),
            contract='opt-in removes other-date technical daily tokens only; all business payload unchanged')
        self.assertEqual(self.db.execute('SELECT count(*) FROM daily').fetchone()[0], 48000)


if __name__ == '__main__':
    unittest.main()
