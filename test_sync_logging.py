"""Isolated sync logging checks; production main and real commands never run."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import errno
import importlib
import io
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.request


def deny_external(*args, **kwargs):
    raise AssertionError('External commands and network are forbidden in this test')


def external_guards():
    stack = ExitStack()
    blocked = []
    for target in ('subprocess.Popen', 'subprocess.run', 'socket.socket',
                   'socket.create_connection', 'urllib.request.urlopen',
                   'urllib.request.OpenerDirector.open', 'os.system'):
        blocked.append(stack.enter_context(patch(target, side_effect=deny_external)))

    def assert_unused():
        if any(endpoint.called for endpoint in blocked):
            raise AssertionError('The test attempted a forbidden command or network entry point')

    stack.callback(assert_unused)
    return stack


# Import itself must not run commands, probes, production setup or main.
with external_guards():
    sync = importlib.import_module('ops.douyin_board_sync')


PROJECT = Path(__file__).resolve().parent
SYNTHETIC_ROOT = PROJECT / '.synthetic-tests'
OLD_SHA = '1' * 40
NEW_SHA = '2' * 40
GIT = ['git', '-C', str(sync.REPO)]
FETCH = GIT + ['fetch', 'origin', 'main']
HEAD = GIT + ['rev-parse', 'HEAD']
REMOTE = GIT + ['rev-parse', 'origin/main']
STATUS = GIT + ['status', '--porcelain=v1']
MERGE = GIT + ['merge', '--ff-only', 'origin/main']
RESTART = ['systemctl', 'restart', 'douyin-board.service']
SHOW_STATE = ['systemctl', 'show', 'douyin-board.service',
              '--property=ActiveState', '--property=SubState']
SECRETS = (
    'SYNTHETIC_TOKEN_NEVER_LOG_4297',
    'https://synthetic-user:SYNTHETIC_PASSWORD_NEVER_LOG@example.invalid/private/repo',
    'Authorization: Bearer SYNTHETIC_AUTHORIZATION_NEVER_LOG',
    '-----BEGIN PRIVATE KEY-----',
    'SYNTHETIC_PRIVATE_KEY_NEVER_LOG',
    'synthetic-business-accounts-private.csv',
)
SECRET_BYTES = ('\n'.join(SECRETS) + '\n-----END PRIVATE KEY-----').encode()


def result(returncode=0, stdout=b'', stderr=b'', timed_out=False):
    return sync.CommandResult(returncode=returncode, stdout=stdout,
                              stderr=stderr, timed_out=timed_out)


def log_event():
    return {'run_id': 'a' * 32, 'timestamp': '2026-10-27T10:00:00.000+08:00',
            'event': 'step_end', 'step': 'fetch', 'result': 'ok', 'elapsed_ms': 1, 'exit_code': 0}


class ScriptedExecutor:
    def __init__(self, case, script):
        self.case = case
        self.script = list(script)
        self.calls = []

    def run(self, args, timeout):
        self.calls.append((list(args), timeout))
        self.case.assertTrue(self.script, 'Unexpected extra command')
        expected, expected_timeout, response = self.script.pop(0)
        self.case.assertEqual(list(args), expected)
        self.case.assertEqual(timeout, expected_timeout)
        if isinstance(response, Exception):
            raise response
        return response


class CollectingLogger:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(json.loads(json.dumps(event)))


class TickClock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        self.value += 0.01
        return self.value


class SyncLoggingTest(unittest.TestCase):
    def setUp(self):
        self.guards = external_guards()
        self.addCleanup(self.guards.close)

    def prefix(self, dirty=b''):
        return [(FETCH, 20, result()), (HEAD, 5, result(stdout=OLD_SHA.encode())),
                (REMOTE, 5, result(stdout=NEW_SHA.encode())),
                (STATUS, 5, result(stdout=dirty))]

    def run_sync(self, script, *, ready=True, exists=True, probe=None, logger=None):
        executor = ScriptedExecutor(self, script)
        logger = logger or CollectingLogger()
        probes = []

        def ready_probe(timeout):
            probes.append(timeout)
            self.assertEqual(timeout, 15)
            return probe or sync.ProbeResult(ready=ready)

        runner = sync.SyncRunner(
            executor=executor, logger=logger, ready_probe=ready_probe,
            repo_exists=lambda: exists, monotonic=TickClock(),
            now=lambda: datetime(2026, 10, 27, 4, 30, tzinfo=timezone.utc))
        code = runner.run()
        self.assertFalse(executor.script, 'Planned commands were not completed')
        self.assert_contract(logger.events, code)
        self.assert_no_secrets(json.dumps(logger.events))
        self.assertFalse(any('stop' in args for args, _ in executor.calls),
                         'Failure handling must never stop the existing web service')
        return code, executor.calls, logger.events, probes

    def assert_contract(self, events, code):
        self.assertTrue(events)
        self.assertEqual(events[0]['event'], 'run_begin')
        self.assertEqual(events[-1]['event'], 'run_finish')
        self.assertEqual(sum(event['event'] == 'run_finish' for event in events), 1)
        run_ids = {event['run_id'] for event in events}
        self.assertEqual(len(run_ids), 1)
        self.assertRegex(next(iter(run_ids)), r'^[0-9a-f]{32}$')
        active = []
        ends = []
        for event in events:
            sync.safe_event(event)
            instant = datetime.fromisoformat(event['timestamp'])
            self.assertEqual(instant.utcoffset(), timedelta(hours=8))
            self.assertGreaterEqual(event['elapsed_ms'], 0)
            self.assertIsInstance(event['elapsed_ms'], (int, float))
            self.assertIn('exit_code', event)
            if event['exit_code'] is not None:
                self.assertIsInstance(event['exit_code'], int)
            for key in ('old_sha', 'new_sha'):
                if event.get(key) is not None:
                    self.assertRegex(event[key], r'^[0-9a-f]{40}$')
            if event['event'] == 'step_begin':
                active.append(event['step'])
            elif event['event'] == 'step_end':
                self.assertTrue(active)
                self.assertEqual(active.pop(), event['step'])
                ends.append(event)
        self.assertFalse(active, 'Every started step must finish')
        self.assertTrue(ends)
        self.assertEqual(events[-1]['exit_code'], code)
        self.assertEqual(ends[-1]['result'], 'ok' if events[-1]['result'] == 'updated'
                         else events[-1]['result'])
        if events[-1].get('error_category') == 'log_failed':
            self.assertEqual(code, 1)
        else:
            self.assertEqual(events[-1].get('error_category'), ends[-1].get('error_category'))
        self.assertGreater(events[-1]['elapsed_ms'], 0)

    def assert_no_secrets(self, text):
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def test_unchanged_never_merges_or_restarts(self):
        script = [(FETCH, 20, result(stdout=SECRET_BYTES)),
                  (HEAD, 5, result(stdout=OLD_SHA.encode())),
                  (REMOTE, 5, result(stdout=OLD_SHA.encode()))]
        code, calls, events, probes = self.run_sync(script)
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]['result'], 'no_change')
        self.assertEqual(len(calls), 3)
        self.assertFalse(probes)

    def test_dirty_repository_skips_without_logging_business_filenames(self):
        code, calls, events, probes = self.run_sync(self.prefix(dirty=b' M ' + SECRET_BYTES))
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]['result'], 'skipped')
        self.assertEqual(events[-1]['error_category'], 'dirty')
        self.assertFalse(any(args == MERGE or args == RESTART for args, _ in calls))
        self.assertFalse(probes)

    def test_missing_repository_is_a_finished_skip(self):
        code, calls, events, probes = self.run_sync([], exists=False)
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]['error_category'], 'repo_missing')
        self.assertFalse(calls)
        self.assertFalse(probes)

    def test_fetch_failures_are_classified_without_raw_output(self):
        failures = [
            ('timeout', result(124, stdout=SECRET_BYTES, timed_out=True)),
            ('dns', result(128, stdout=SECRET_BYTES, stderr=b'fatal: Could not resolve host: example.invalid')),
            ('connection', result(128, stdout=SECRET_BYTES, stderr=b'Failed to connect to example.invalid port 443: Could not connect to server')),
            ('tls', result(128, stdout=SECRET_BYTES, stderr=b'SSL certificate problem: certificate verify failed')),
            ('auth', result(128, stdout=SECRET_BYTES, stderr=b'fatal: Authentication failed for https://example.invalid/repo')),
            ('http', result(128, stdout=SECRET_BYTES, stderr=b'The requested URL returned error: 503')),
        ]
        for category, failure in failures:
            with self.subTest(category=category):
                code, calls, events, probes = self.run_sync([(FETCH, 20, failure)])
                self.assertNotEqual(code, 0)
                self.assertEqual(events[-1]['error_category'], category)
                self.assertEqual(len(calls), 1)
                self.assertFalse(probes)

    def test_raw_credentials_in_failure_stderr_do_not_enter_any_event(self):
        code, calls, events, probes = self.run_sync([(FETCH, 20, result(128, stderr=SECRET_BYTES))])
        self.assertNotEqual(code, 0)
        self.assertFalse(probes)

    def test_invalid_sha_is_not_logged_and_prevents_merge(self):
        script = self.prefix()[:2] + [(REMOTE, 5, result(stdout=SECRET_BYTES))]
        code, calls, events, probes = self.run_sync(script)
        self.assertNotEqual(code, 0)
        self.assertEqual(events[-1]['error_category'], 'invalid_sha')
        self.assertFalse(probes)

    def test_non_fast_forward_never_restarts(self):
        script = self.prefix() + [(MERGE, 20, result(128, stdout=SECRET_BYTES,
            stderr=b'fatal: Not possible to fast-forward, aborting.'))]
        code, calls, events, probes = self.run_sync(script)
        self.assertNotEqual(code, 0)
        self.assertEqual(events[-1]['error_category'], 'non_fast_forward')
        self.assertFalse(probes)
        self.assertFalse(any(args == RESTART for args, _ in calls))

    def test_restart_failure_is_nonzero_and_does_not_probe_ready(self):
        script = self.prefix() + [(MERGE, 20, result(stdout=SECRET_BYTES)),
                                  (RESTART, 95, result(1, stderr=SECRET_BYTES))]
        code, calls, events, probes = self.run_sync(script)
        self.assertNotEqual(code, 0)
        self.assertEqual(events[-1]['error_category'], 'restart_failed')
        self.assertFalse(probes)

    def test_updated_run_records_both_shas_elapsed_time_and_readiness(self):
        script = self.prefix() + [(MERGE, 20, result(stdout=SECRET_BYTES)), (RESTART, 95, result())]
        code, calls, events, probes = self.run_sync(script)
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]['result'], 'updated')
        self.assertEqual(events[-1]['old_sha'], OLD_SHA)
        self.assertEqual(events[-1]['new_sha'], NEW_SHA)
        self.assertEqual(probes, [15])
        self.assertEqual([event['step'] for event in events if event['event'] == 'step_end'],
                         ['preflight', 'fetch', 'compare', 'dirty', 'merge', 'restart', 'ready'])

    def test_readiness_failure_has_only_numeric_http_diagnostics(self):
        script = self.prefix() + [(MERGE, 20, result()), (RESTART, 95, result())]
        code, calls, events, probes = self.run_sync(script,
            probe=sync.ProbeResult(ready=False, category='ready_failed', http_status=503))
        self.assertNotEqual(code, 0)
        self.assertEqual(events[-1]['error_category'], 'ready_failed')
        diagnosed = [event for event in events if event.get('diagnostic')]
        self.assertTrue(diagnosed)
        for event in diagnosed:
            self.assertEqual(event['diagnostic'], {'http_status': 503})

    def test_executor_exception_text_is_not_logged(self):
        code, calls, events, probes = self.run_sync([(FETCH, 20, OSError(SECRET_BYTES.decode()))])
        self.assertNotEqual(code, 0)
        self.assertFalse(probes)

    def test_restart_timeout_only_inspects_safe_state_and_remains_unknown(self):
        states = b'ActiveState=active\nSubState=running\nUnreviewed=' + SECRET_BYTES
        script = self.prefix() + [(MERGE, 20, result()),
            (RESTART, 95, result(124, stderr=SECRET_BYTES, timed_out=True)),
            (SHOW_STATE, 5, result(stdout=states))]
        code, calls, events, probes = self.run_sync(script)
        self.assertNotEqual(code, 0)
        self.assertEqual(events[-1]['error_category'], 'restart_state_unknown')
        self.assertEqual(events[-1]['diagnostic'], {'active_state': 'active', 'sub_state': 'running'})
        self.assertEqual(sum(args == RESTART for args, _ in calls), 1)
        self.assertFalse(probes, 'Unknown restart state must not trigger a second restart or readiness claim')

    def test_restart_timeout_with_failed_state_query_never_retries_or_logs_raw_error(self):
        script = self.prefix() + [(MERGE, 20, result()),
            (RESTART, 95, result(124, timed_out=True)),
            (SHOW_STATE, 5, result(1, stdout=SECRET_BYTES, stderr=SECRET_BYTES))]
        code, calls, events, probes = self.run_sync(script)
        self.assertNotEqual(code, 0)
        self.assertEqual(events[-1]['error_category'], 'restart_state_unknown')
        self.assertEqual(sum(args == RESTART for args, _ in calls), 1)
        self.assertFalse(probes)

    def test_safe_event_rejects_untrusted_fields_and_oversized_values_on_all_platforms(self):
        self.assertEqual(json.loads(sync.safe_event(log_event())), log_event())
        for event in ({**log_event(), 'stderr': SECRET_BYTES.decode()},
                      {**log_event(), 'run_id': 'a' * 10000},
                      {**log_event(), 'diagnostic': {'path': SECRETS[-1]}},
                      {**log_event(), 'old_sha': SECRET_BYTES.decode()}):
            with self.subTest(fields=sorted(event)):
                with self.assertRaises(sync.SafeLogError) as raised:
                    sync.safe_event(event)
                self.assert_no_secrets(str(raised.exception))

    def test_log_configuration_rejects_unbounded_size_before_opening_files(self):
        with patch.object(Path, 'mkdir', side_effect=AssertionError('Invalid bounds must not touch the filesystem')) as mkdir:
            for limits in ({'max_bytes': 2 * 1024 * 1024 + 1}, {'backups': 6}, {'backups': 0}):
                with self.subTest(limits=limits):
                    with self.assertRaises(sync.SafeLogError):
                        sync.SecureEventLog(directory=SYNTHETIC_ROOT / 'unused-sync-log', stdout=io.StringIO(), **limits)
                    mkdir.assert_not_called()

    def test_merge_completion_log_failure_still_restarts_once_and_checks_ready(self):
        class FailingLogger(CollectingLogger):
            failed = False

            def emit(self, event):
                if event['event'] == 'step_end' and event['step'] == 'merge' and not self.failed:
                    self.failed = True
                    raise sync.SafeLogError()
                super().emit(event)

        logger = FailingLogger()

        def journal(event, *args, **kwargs):
            logger.events.append(json.loads(sync.safe_event(event)))

        script = self.prefix() + [(MERGE, 20, result()), (RESTART, 95, result())]
        with patch.object(sync, 'journal_event', side_effect=journal):
            code, calls, events, probes = self.run_sync(script, logger=logger)
        self.assertTrue(logger.failed)
        self.assertEqual(sum(args == RESTART for args, _ in calls), 1)
        self.assertEqual(probes, [15])
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]['result'], 'updated')
        self.assertEqual(events[-1]['error_category'], 'log_failed')
        self.assertTrue(any(event.get('error_category') == 'log_failed'
                            and event['event'] != 'run_finish' for event in events))

    def test_initial_log_failure_stops_before_any_repository_or_command_access(self):
        logger = CollectingLogger()
        executor = ScriptedExecutor(self, [])
        touched = []

        def fail_log(event):
            raise sync.SafeLogError()

        def journal(event, *args, **kwargs):
            logger.events.append(json.loads(sync.safe_event(event)))

        def forbidden_probe(*args):
            touched.append('probe')
            raise AssertionError('Initial log failure must stop before readiness')

        def repo_exists():
            touched.append('repository')
            return True

        logger.emit = fail_log
        runner = sync.SyncRunner(executor=executor, logger=logger, ready_probe=forbidden_probe,
            repo_exists=repo_exists, monotonic=TickClock(),
            now=lambda: datetime(2026, 10, 27, 4, 30, tzinfo=timezone.utc))
        with patch.object(sync, 'journal_event', side_effect=journal):
            code = runner.run()
        self.assertEqual(code, 1)
        self.assertFalse(executor.calls)
        self.assertFalse(touched)
        self.assertEqual([event['event'] for event in logger.events], ['run_begin', 'run_finish'])
        self.assertEqual(logger.events[-1]['exit_code'], code)
        self.assertEqual(logger.events[-1]['error_category'], 'log_failed')

    def test_no_change_finish_log_failure_returns_nonzero_and_finish_stays_last(self):
        class FailingFinishLogger(CollectingLogger):
            def emit(self, event):
                if event['event'] == 'run_finish':
                    raise sync.SafeLogError()
                super().emit(event)

        logger = FailingFinishLogger()

        def journal(event, *args, **kwargs):
            logger.events.append(json.loads(sync.safe_event(event)))

        script = [(FETCH, 20, result()), (HEAD, 5, result(stdout=OLD_SHA.encode())),
                  (REMOTE, 5, result(stdout=OLD_SHA.encode()))]
        with patch.object(sync, 'journal_event', side_effect=journal):
            code, calls, events, probes = self.run_sync(script, logger=logger)
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]['result'], 'no_change')
        self.assertEqual(events[-1]['error_category'], 'log_failed')
        self.assertFalse(probes)


@unittest.skipUnless(os.name == 'posix', 'SecureEventLog uses Linux directory fds/O_NOFOLLOW; validate permissions and rotation on Linux')
class SecureEventLogTest(unittest.TestCase):
    def setUp(self):
        self.guards = external_guards()
        self.addCleanup(self.guards.close)
        SYNTHETIC_ROOT.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix='sync-log-', dir=SYNTHETIC_ROOT)
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'private-log'
        self.stream = io.StringIO()

    def event(self):
        return log_event()

    def logger(self, **kwargs):
        logger = sync.SecureEventLog(directory=self.path, expected_uid=os.geteuid(),
                                    stdout=self.stream, **kwargs)
        if hasattr(logger, 'close'):
            self.addCleanup(logger.close)
        return logger

    def test_permissions_and_journal_file_events_match(self):
        logger = self.logger()
        event = self.event()
        logger.emit(event)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o700)
        target = self.path / 'events.jsonl'
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(json.loads(target.read_text()), event)
        self.assertEqual(json.loads(self.stream.getvalue()), event)

    def test_rotation_is_bounded_and_preserves_complete_json_lines(self):
        logger = self.logger(max_bytes=1024, backups=2)
        for _ in range(80):
            logger.emit(self.event())
        files = sorted(self.path.glob('events.jsonl*'))
        self.assertEqual({file.name for file in files}, {'events.jsonl', 'events.jsonl.1', 'events.jsonl.2'})
        self.assertLessEqual(sum(file.stat().st_size for file in files), 3 * 1024)
        for file in files:
            self.assertLessEqual(file.stat().st_size, 1024)
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
            for line in file.read_text().splitlines():
                self.assertEqual(json.loads(line), self.event())
        self.assertEqual(len(self.stream.getvalue().splitlines()), 80)

    def test_unknown_or_oversized_fields_are_rejected_without_leaking(self):
        logger = self.logger()
        for event in ({**self.event(), 'stderr': SECRET_BYTES.decode()},
                      {**self.event(), 'run_id': 'a' * 10000}):
            with self.subTest(fields=sorted(event)):
                with self.assertRaises(sync.SafeLogError) as raised:
                    logger.emit(event)
                for secret in SECRETS:
                    self.assertNotIn(secret, str(raised.exception))
                    self.assertNotIn(secret, self.stream.getvalue())
        self.assertEqual(self.stream.getvalue(), '')

    def test_partial_file_write_is_truncated_to_last_complete_event(self):
        logger = self.logger()
        logger.emit(self.event())
        target = self.path / 'events.jsonl'
        before = target.read_bytes()
        real_write = os.write
        writes = []

        def fail_after_partial_write(fd, value):
            writes.append(len(value))
            if len(writes) == 1:
                return real_write(fd, value[:10])
            raise OSError(errno.ENOSPC, 'synthetic full log filesystem')

        with patch.object(sync.os, 'write', side_effect=fail_after_partial_write):
            with self.assertRaises(sync.SafeLogError):
                logger.emit(self.event())
        self.assertEqual(len(writes), 2)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(json.loads(target.read_text()), self.event())
        self.assertEqual(len(self.stream.getvalue().splitlines()), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
