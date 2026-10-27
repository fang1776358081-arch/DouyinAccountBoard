"""Fixed Douyin Board updater with bounded, credential-free structured logs.

Production has no arguments or configurable targets. Tests inject dependencies
into SyncRunner; importing this module never executes commands or opens files.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
import uuid

REPO = Path('/opt/douyin-board')
USER = 'douyin'
SERVICE = 'douyin-board.service'
LOG_DIRECTORY = Path('/var/log/douyin-board-sync')
CST = timezone(timedelta(hours=8))
SAFE_ENV = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
            'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'GIT_TERMINAL_PROMPT': '0'}
CATEGORIES = frozenset(('timeout', 'dns', 'connection', 'tls', 'auth', 'http',
    'repo_permission', 'dirty', 'non_fast_forward', 'git_failed', 'invalid_sha',
    'restart_failed', 'restart_state_unknown', 'ready_failed', 'log_failed', 'repo_missing',
    'internal_error', 'platform_rejected', 'already_running'))
EVENTS = frozenset(('run_begin', 'step_begin', 'step_end', 'run_finish', 'log_warning'))
STEPS = frozenset(('run', 'preflight', 'fetch', 'compare', 'dirty', 'merge', 'restart', 'ready'))
RESULTS = frozenset(('begin', 'ok', 'no_change', 'skipped', 'failed', 'timeout', 'updated'))
ACTIVE_STATES = frozenset(('active', 'inactive', 'activating', 'deactivating',
                          'failed', 'reloading', 'maintenance', 'refreshing', 'unknown'))
SUB_STATES = frozenset(('running', 'dead', 'failed', 'start', 'start-pre', 'start-post',
    'stop', 'stop-sigterm', 'stop-sigkill', 'stop-post', 'auto-restart', 'auto-restart-queued',
    'reload', 'exited', 'condition', 'final-sigterm', 'final-sigkill', 'cleaning', 'unknown'))


@dataclass
class CommandResult:
    returncode: int = 0
    stdout: bytes = b''
    stderr: bytes = b''
    timed_out: bool = False


@dataclass
class ProbeResult:
    ready: bool
    category: str = 'ready_failed'
    http_status: int | None = None


class SyncFailure(Exception):
    def __init__(self, category, exit_code=1, diagnostic=None):
        self.category = category if category in CATEGORIES else 'internal_error'
        self.exit_code = exit_code if type(exit_code) is int and -255 <= exit_code <= 255 and exit_code else 1
        self.diagnostic = diagnostic or {}


class SyncSkip(Exception):
    def __init__(self, result, category=None):
        self.result, self.category = result, category


class SafeLogError(Exception):
    def __init__(self, *, journal_emitted=False):
        self.journal_emitted = journal_emitted


def safe_event(event):
    """Reject unreviewed fields rather than serializing arbitrary diagnostics."""
    required = {'run_id', 'timestamp', 'event', 'step', 'result', 'elapsed_ms', 'exit_code'}
    allowed = required | {'old_sha', 'new_sha', 'error_category', 'diagnostic'}
    if not isinstance(event, dict) or not required <= event.keys() or not event.keys() <= allowed:
        raise SafeLogError()
    if not re.fullmatch('[0-9a-f]{32}', event['run_id'] if isinstance(event['run_id'], str) else ''):
        raise SafeLogError()
    timestamp = event['timestamp'] if isinstance(event['timestamp'], str) else ''
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+08:00', timestamp):
        raise SafeLogError()
    if any(not isinstance(event[key], str) for key in ('event', 'step', 'result')) or event['event'] not in EVENTS or event['step'] not in STEPS or event['result'] not in RESULTS:
        raise SafeLogError()
    if type(event['elapsed_ms']) is not int or not 0 <= event['elapsed_ms'] <= 2147483647:
        raise SafeLogError()
    if event['exit_code'] is not None and (type(event['exit_code']) is not int or not -255 <= event['exit_code'] <= 255):
        raise SafeLogError()
    for key in ('old_sha', 'new_sha'):
        if key in event and not re.fullmatch('[0-9a-f]{40}', event[key] if isinstance(event[key], str) else ''):
            raise SafeLogError()
    if 'error_category' in event and (not isinstance(event['error_category'], str) or event['error_category'] not in CATEGORIES):
        raise SafeLogError()
    diagnostic = event.get('diagnostic', {})
    if not isinstance(diagnostic, dict) or not diagnostic.keys() <= {'curl_code', 'http_status', 'active_state', 'sub_state'}:
        raise SafeLogError()
    for key, value in diagnostic.items():
        if key in ('active_state', 'sub_state'):
            if not isinstance(value, str) or value not in (ACTIVE_STATES if key == 'active_state' else SUB_STATES):
                raise SafeLogError()
            continue
        if type(value) is not int or not (100 <= value <= 599 if key == 'http_status' else 0 <= value <= 999):
            raise SafeLogError()
    line = (json.dumps(event, ensure_ascii=True, separators=(',', ':')) + '\n').encode('ascii')
    if len(line) > 4096:
        raise SafeLogError()
    return line


def journal_event(event, stream=None):
    stream = sys.stdout if stream is None else stream
    stream.write(safe_event(event).decode('ascii'))
    stream.flush()


class SecureEventLog:
    """Private, finite JSONL rotation. Existing unsafe files are never changed."""
    def __init__(self, *, directory=LOG_DIRECTORY, max_bytes=2 * 1024 * 1024,
                 backups=5, expected_uid=0, stdout=None):
        self.directory, self.max_bytes, self.backups = Path(directory), max_bytes, backups
        self.expected_uid, self.stdout = expected_uid, sys.stdout if stdout is None else stdout
        self._directory_fd = self._lock_fd = None
        try:
            if type(max_bytes) is not int or not 256 <= max_bytes <= 2 * 1024 * 1024 or type(backups) is not int or not 1 <= backups <= 5:
                raise SafeLogError()
            try:
                self.directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = self.directory.lstat()
            self._check(info, directory=True)
            self._directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self._check(os.fstat(self._directory_fd), directory=True)
            self._validate_files()
        except BaseException:
            self.close()
            raise SafeLogError() from None

    def _check(self, info, *, directory=False):
        if info.st_uid != self.expected_uid or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600):
            raise SafeLogError()
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1):
            raise SafeLogError()

    def _stat(self, name):
        try:
            info = os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        self._check(info)
        return info

    def _validate_files(self):
        for name in ('events.jsonl',) + tuple('events.jsonl.' + str(i) for i in range(1, self.backups + 1)):
            info = self._stat(name)
            if info is not None and info.st_size > self.max_bytes:
                raise SafeLogError()

    def acquire_run_lock(self):
        # Linux-only dependency is deliberately imported only in production use.
        import fcntl
        try:
            self._stat('run.lock')
            self._lock_fd = os.open('run.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                                    0o600, dir_fd=self._directory_fd)
            self._check(os.fstat(self._lock_fd))
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(self._lock_fd)
                self._lock_fd = None
                return False
            return True
        except BaseException:
            raise SafeLogError() from None

    def _append(self, line):
        self._validate_files()
        if len(line) > self.max_bytes:
            raise SafeLogError()
        current = self._stat('events.jsonl')
        if current is not None and current.st_size + len(line) > self.max_bytes:
            oldest = 'events.jsonl.' + str(self.backups)
            if self._stat(oldest) is not None:
                os.unlink(oldest, dir_fd=self._directory_fd)
            for index in range(self.backups - 1, 0, -1):
                source, target = 'events.jsonl.' + str(index), 'events.jsonl.' + str(index + 1)
                if self._stat(source) is not None:
                    os.replace(source, target, src_dir_fd=self._directory_fd, dst_dir_fd=self._directory_fd)
            os.replace('events.jsonl', 'events.jsonl.1', src_dir_fd=self._directory_fd, dst_dir_fd=self._directory_fd)
        fd = os.open('events.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                     0o600, dir_fd=self._directory_fd)
        try:
            self._check(os.fstat(fd))
            original_size = os.fstat(fd).st_size
            try:
                written = 0
                while written < len(line):
                    count = os.write(fd, line[written:])
                    if not count:
                        raise SafeLogError()
                    written += count
                os.fsync(fd)
            except BaseException:
                # Under the production run lock, restore the previous complete
                # JSONL boundary if a short/partial write subsequently fails.
                try:
                    os.ftruncate(fd, original_size)
                    os.fsync(fd)
                except BaseException:
                    pass
                raise
        finally:
            os.close(fd)

    def emit(self, event):
        line = safe_event(event)
        emitted = False
        try:
            # Persist first: if append fails, the runner can report the correct
            # exit code in the one journal finish event (not a premature zero).
            self._append(line)
            self.stdout.write(line.decode('ascii'))
            self.stdout.flush()
            emitted = True
        except BaseException:
            raise SafeLogError(journal_emitted=emitted) from None

    def close(self):
        for key in ('_lock_fd', '_directory_fd'):
            value = getattr(self, key, None)
            if value is not None:
                try:
                    os.close(value)
                except OSError:
                    pass
                setattr(self, key, None)


class ProcessExecutor:
    def run(self, args, timeout):
        options = dict(cwd=REPO, env=SAFE_ENV, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, start_new_session=True)
        if args[:3] == ['git', '-C', str(REPO)]:
            # Direct POSIX credential switching avoids runuser/PAM creating an
            # additional session outside the process group we own and terminate.
            import pwd
            target = pwd.getpwnam(USER)
            if target.pw_uid <= 0 or REPO.stat().st_uid != target.pw_uid:
                raise PermissionError()
            options.update(user=target.pw_uid, group=target.pw_gid,
                extra_groups=os.getgrouplist(USER, target.pw_gid),
                env=dict(SAFE_ENV, HOME=target.pw_dir, USER=USER, LOGNAME=USER))
        process = subprocess.Popen(args, **options)
        try:
            out, err = process.communicate(timeout=timeout)
            return CommandResult(process.returncode, out, err)
        except BaseException as error:
            # Git itself is the session/process-group leader (no runuser layer).
            # Kill the group, not just Git, to terminate its ordinary helpers.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = process.communicate(timeout=5)
            if isinstance(error, subprocess.TimeoutExpired):
                return CommandResult(124, out, err, True)
            raise


def classify_git(result):
    # Raw output lives in memory only and is never returned as diagnostics.
    text = (result.stderr + b'\n' + result.stdout).decode('utf-8', errors='replace').lower()
    diagnostic = {}
    match = re.search(r'\bcurl\s+(\d{1,3})\b', text)
    if match:
        diagnostic['curl_code'] = int(match.group(1))
    match = re.search(r'(?:returned error:\s*|http/\S+\s+|http code[=:]\s*)([1-5]\d{2})\b', text)
    if match:
        diagnostic['http_status'] = int(match.group(1))
    if result.timed_out:
        category = 'timeout'
    elif re.search(r'could not resolve|name or service not known|temporary failure in name resolution', text):
        category = 'dns'
    elif re.search(r'authentication failed|could not read username|terminal prompts disabled|permission denied \(publickey\)|http basic: access denied', text):
        category = 'auth'
    elif re.search(r'certificate|\btls\b|\bssl\b|gnutls|schannel', text):
        category = 'tls'
    elif re.search(r'failed to connect|connection refused|network is unreachable|connection reset|could not connect', text):
        category = 'connection'
    elif re.search(r'dubious ownership|permission denied|insufficient permission|unable to create.*lock', text):
        category = 'repo_permission'
    elif re.search(r'would be overwritten|local changes', text):
        category = 'dirty'
    elif re.search(r'not possible to fast-forward|non-fast-forward|divergent|unrelated histories', text):
        category = 'non_fast_forward'
    elif diagnostic.get('http_status') in (401, 403):
        category = 'auth'
    elif 'http_status' in diagnostic:
        category = 'http'
    else:
        category = {6: 'dns', 7: 'connection', 22: 'http', 28: 'timeout',
                    35: 'tls', 51: 'tls', 60: 'tls'}.get(diagnostic.get('curl_code'), 'git_failed')
    return category, diagnostic


def probe_ready(timeout):
    end = time.monotonic() + timeout
    opener = build_opener(ProxyHandler({}))
    category, status = 'ready_failed', None
    while time.monotonic() < end:
        try:
            request = Request('http://127.0.0.1:80/api/ready', headers={'Cache-Control': 'no-cache'})
            with opener.open(request, timeout=max(0.1, min(2, end - time.monotonic()))) as response:
                status = response.status
                payload = response.read(4097)
                if status == 200 and len(payload) <= 4096 and json.loads(payload).get('ready') is True:
                    return ProbeResult(True, http_status=status)
                category = 'ready_failed'
        except HTTPError as error:
            status, category = error.code, 'http'
        except (TimeoutError,):
            category = 'timeout'
        except URLError:
            category = 'connection'
        except Exception:
            category = 'ready_failed'
        left = end - time.monotonic()
        if left > 0:
            time.sleep(min(0.5, left))
    return ProbeResult(False, category, status)


class SyncRunner:
    def __init__(self, *, executor, logger, ready_probe, repo_exists,
                 monotonic=time.monotonic, now=lambda: datetime.now(CST)):
        self.executor, self.logger, self.ready_probe = executor, logger, ready_probe
        self.repo_exists, self.monotonic, self.now = repo_exists, monotonic, now
        self.run_id, self.old_sha, self.new_sha = uuid.uuid4().hex, None, None
        self.started = self.monotonic()
        self.logging_started = self.log_degraded = False

    def _event(self, event, step, result, started, exit_code=None, category=None, diagnostic=None):
        value = dict(run_id=self.run_id, timestamp=self.now().astimezone(CST).isoformat(timespec='milliseconds'),
            event=event, step=step, result=result,
            elapsed_ms=max(0, min(2147483647, int((self.monotonic() - started) * 1000))), exit_code=exit_code)
        if self.old_sha:
            value['old_sha'] = self.old_sha
        if self.new_sha:
            value['new_sha'] = self.new_sha
        if category:
            value['error_category'] = category
        if diagnostic:
            value['diagnostic'] = diagnostic
        return value

    def _emit(self, event):
        if self.log_degraded:
            self._journal(event)
            return
        try:
            self.logger.emit(event)
        except BaseException as error:
            emitted = isinstance(error, SafeLogError) and error.journal_emitted
            if not self.logging_started:
                if not emitted:
                    self._journal(event)
                raise SafeLogError(journal_emitted=True) from None
            # After an initially writable log, never interrupt an in-progress
            # merge/restart sequence merely because append/rotation now fails.
            self.log_degraded = True
            if event['event'] == 'run_finish' and event['exit_code'] == 0:
                event = dict(event, exit_code=1, error_category='log_failed')
            warning = self._event('log_warning', event['step'], 'failed', self.started, 1, 'log_failed')
            if event['event'] == 'run_finish':
                # Keep the corrected, unique finish as the last journal event.
                self._journal(warning)
            if not emitted:
                self._journal(event)
            if event['event'] != 'run_finish':
                self._journal(warning)

    @staticmethod
    def _journal(event):
        # A broken journal stream must not strand a successfully merged checkout
        # before its required restart/readiness actions either.
        try:
            journal_event(event)
        except BaseException:
            pass

    @contextmanager
    def _step(self, name):
        started, outcome = self.monotonic(), {}
        try:
            self._emit(self._event('step_begin', name, 'begin', started))
            yield outcome
        except (SyncFailure, SafeLogError) as error:
            category = error.category if isinstance(error, SyncFailure) else 'log_failed'
            code = error.exit_code if isinstance(error, SyncFailure) else 1
            diagnostic = error.diagnostic if isinstance(error, SyncFailure) else None
            try:
                self._emit(self._event('step_end', name, 'timeout' if category == 'timeout' else 'failed',
                    started, code, category, diagnostic))
            except SafeLogError:
                pass
            raise
        except BaseException:
            try:
                self._emit(self._event('step_end', name, 'failed', started, 1, 'internal_error'))
            except SafeLogError:
                pass
            raise SyncFailure('internal_error') from None
        else:
            self._emit(self._event('step_end', name, outcome.get('result', 'ok'), started, 0,
                                  outcome.get('category'), outcome.get('diagnostic')))

    def _command(self, args, timeout, *, restart=False):
        try:
            result = self.executor.run(args, timeout)
        except PermissionError:
            raise SyncFailure('restart_failed' if restart else 'repo_permission') from None
        except BaseException:
            raise SyncFailure('restart_failed' if restart else 'internal_error') from None
        if result.timed_out or result.returncode != 0:
            if restart and result.timed_out:
                raise SyncFailure('restart_state_unknown', 124, self._restart_state())
            category, diagnostic = classify_git(result)
            if restart and not result.timed_out:
                category, diagnostic = 'restart_failed', {}
            raise SyncFailure(category, 124 if result.timed_out else result.returncode, diagnostic)
        return result.stdout

    def _restart_state(self):
        # A systemctl client timeout does not cancel systemd's restart job.
        # Observe, but never retry or stop/roll back the website here.
        state = {'active_state': 'unknown', 'sub_state': 'unknown'}
        try:
            result = self.executor.run(['systemctl', 'show', SERVICE,
                '--property=ActiveState', '--property=SubState'], 5)
            if result.returncode == 0 and not result.timed_out:
                for line in result.stdout.decode('ascii', errors='ignore').splitlines():
                    key, separator, value = line.partition('=')
                    if separator and key == 'ActiveState' and value in ACTIVE_STATES:
                        state['active_state'] = value
                    elif separator and key == 'SubState' and value in SUB_STATES:
                        state['sub_state'] = value
        except BaseException:
            pass
        return state

    def _git(self, *args, timeout=5):
        return self._command(['git', '-C', str(REPO), *args], timeout)

    @staticmethod
    def _sha(output):
        if not isinstance(output, bytes) or not re.fullmatch(b'[0-9a-f]{40}\n?', output):
            raise SyncFailure('invalid_sha')
        return output.decode('ascii').strip()

    def run(self):
        result, code, category, diagnostic = 'failed', 1, None, None
        try:
            self._emit(self._event('run_begin', 'run', 'begin', self.started))
            self.logging_started = True
            with self._step('preflight') as outcome:
                if not self.repo_exists():
                    outcome.update(result='skipped', category='repo_missing')
            if outcome.get('result') == 'skipped':
                raise SyncSkip('skipped', 'repo_missing')
            with self._step('fetch'):
                self._git('fetch', 'origin', 'main', timeout=20)
            with self._step('compare') as outcome:
                self.old_sha = self._sha(self._git('rev-parse', 'HEAD'))
                self.new_sha = self._sha(self._git('rev-parse', 'origin/main'))
                if self.old_sha == self.new_sha:
                    outcome['result'] = 'no_change'
            if outcome.get('result') == 'no_change':
                raise SyncSkip('no_change')
            with self._step('dirty') as outcome:
                if self._git('status', '--porcelain=v1'):
                    outcome.update(result='skipped', category='dirty')
            if outcome.get('result') == 'skipped':
                raise SyncSkip('skipped', 'dirty')
            with self._step('merge'):
                self._git('merge', '--ff-only', 'origin/main', timeout=20)
            with self._step('restart'):
                self._command(['systemctl', 'restart', SERVICE], 95, restart=True)
            with self._step('ready') as outcome:
                probe = self.ready_probe(15)
                diagnostic = {'http_status': probe.http_status} if type(probe.http_status) is int and 100 <= probe.http_status <= 599 else {}
                if not probe.ready:
                    raise SyncFailure(probe.category if probe.category in CATEGORIES else 'ready_failed', 1, diagnostic)
                outcome['diagnostic'] = diagnostic
            result, code = 'updated', 0
        except SyncSkip as skipped:
            result, code, category = skipped.result, 0, skipped.category
        except SyncFailure as error:
            category, code, diagnostic = error.category, error.exit_code, error.diagnostic
            result = 'timeout' if category == 'timeout' else 'failed'
        except SafeLogError:
            category, code, result = 'log_failed', 1, 'failed'
        except BaseException:
            category, code, result = 'internal_error', 1, 'failed'
        finally:
            if self.log_degraded and code == 0:
                code, category = 1, 'log_failed'
            finish = self._event('run_finish', 'run', result, self.started, code, category, diagnostic)
            try:
                self._emit(finish)
                if self.log_degraded and code == 0:
                    code = 1
            except SafeLogError as error:
                code = 1
                if not error.journal_emitted:
                    try:
                        journal_event(self._event('run_finish', 'run', 'failed', self.started, 1, 'log_failed'))
                    except BaseException:
                        pass
        return code


def _bootstrap_events(category, *, skipped=False):
    identity, timestamp = uuid.uuid4().hex, datetime.now(CST).isoformat(timespec='milliseconds')
    base = dict(run_id=identity, timestamp=timestamp, step='run', elapsed_ms=0)
    journal_event(dict(base, event='run_begin', result='begin', exit_code=None))
    journal_event(dict(base, event='run_finish', result='skipped' if skipped else 'failed',
                       exit_code=0 if skipped else 1, error_category=category))


def _production_repo_exists():
    if not (REPO / '.git').is_dir():
        return False
    try:
        import pwd
        target = pwd.getpwnam(USER)
        if target.pw_uid <= 0 or REPO.stat().st_uid != target.pw_uid:
            raise SyncFailure('repo_permission')
    except SyncFailure:
        raise
    except BaseException:
        raise SyncFailure('repo_permission') from None
    return True


def main():
    # No testing flags, target paths, command arguments or environment overrides.
    if len(sys.argv) != 1 or not sys.platform.startswith('linux') or os.geteuid() != 0:
        _bootstrap_events('platform_rejected')
        return 1
    os.umask(0o077)
    logger = None
    try:
        logger = SecureEventLog()
        if not logger.acquire_run_lock():
            _bootstrap_events('already_running', skipped=True)
            return 0
        return SyncRunner(executor=ProcessExecutor(), logger=logger, ready_probe=probe_ready,
                          repo_exists=_production_repo_exists).run()
    except BaseException:
        try:
            _bootstrap_events('log_failed')
        except BaseException:
            pass
        return 1
    finally:
        if logger is not None:
            logger.close()


if __name__ == '__main__':
    raise SystemExit(main())
