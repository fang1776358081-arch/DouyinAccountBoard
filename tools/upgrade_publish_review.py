"""Explicit v5 -> v6 maintenance upgrade. Never imported by website startup.

Run on the existing Linux server as root, with its existing service credentials.
No connection arguments, credential output, owner setup, collection or old-data writes.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCHEMA = 'board_candidate'
LOCK = 811032001
NEW_TABLES = ('publish_review_samples', 'publish_review_baselines',
              'publish_review_tasks', 'publish_review_requests')
NEW_INDEXES = ('publish_review_samples_account_time', 'publish_review_samples_account_day',
               'publish_review_tasks_day_scope')
SQL6_SHA = '5fb8227b5e280f7c651934eb626881a870d3264348b2385c3e4e4f797d77939d'


class UpgradeStop(Exception):
    pass


def check(value, code):
    if not value:
        raise UpgradeStop(code)


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def save(folder, value):
    target = Path(folder) / 'receipt.json'
    temporary = Path(folder) / 'receipt.tmp'
    with temporary.open('w', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
    temporary.chmod(0o600)
    temporary.replace(target)


def versions(db):
    return db.execute('SELECT version,sha256 FROM board_candidate.schema_migrations ORDER BY version').fetchall()


def control(db):
    return db.execute('SELECT mode,writer_epoch,restore_generation FROM board_candidate.runtime_control WHERE id=1').fetchone()


def set_mode(db, before, after):
    with db.transaction():
        db.execute("SET LOCAL lock_timeout='10s'")
        result = db.execute('UPDATE board_candidate.runtime_control SET mode=%s WHERE id=1 AND mode=%s AND writer_epoch=%s AND restore_generation=%s',
                            (after, *before))
        check(result.rowcount == 1, 'CONTROL_CHANGED')


def relations(db):
    return db.execute("SELECT c.relname,c.relkind,c.relowner,c.relacl::text FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relkind IN ('r','p','S') ORDER BY c.relname", (SCHEMA,)).fetchall()


def fingerprint(db):
    """Only hashes leave SQL; no business values, password hashes or sessions exported."""
    from psycopg import sql
    result = dict(relations=relations(db), tables={}, sequences={},
                  schema=db.execute('SELECT nspowner,nspacl::text FROM pg_namespace WHERE nspname=%s', (SCHEMA,)).fetchone())
    for name, kind, _, _ in result['relations']:
        if kind == 'S':
            result['sequences'][name] = db.execute(sql.SQL('SELECT last_value,is_called FROM {}').format(sql.Identifier(SCHEMA, name))).fetchone()
        elif name not in ('schema_migrations', 'runtime_control'):
            digest, count = hashlib.sha256(), 0
            query = sql.SQL('SELECT md5(row_to_json(t)::text) FROM {} t ORDER BY 1').format(sql.Identifier(SCHEMA, name))
            with db.cursor(name='upgrade_fingerprint') as cursor:
                cursor.execute(query)
                for row in cursor:
                    digest.update(row[0].encode('ascii'))
                    count += 1
            result['tables'][name] = (count, digest.hexdigest())
    return result


def snapshot(db):
    with db.transaction():
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        return fingerprint(db)


def old_unchanged(before, after):
    return (before['schema'] == after['schema']
            and before['sequences'] == after['sequences']
            and all(after['tables'].get(key) == value for key, value in before['tables'].items())
            and all(row in after['relations'] for row in before['relations']))


def resolve_preflight(db, pending):
    """Archive only an empty, pre-maintenance attempt, after proving live v5/open.

    This never clears a receipt-bearing attempt, changes mode, restarts services,
    deletes a backup, or rolls a committed schema backward.
    """
    import pg_schema
    import stat
    from pg_service_adapter import ServiceConnection
    pending = Path(pending)
    check(pending.name == 'douyin-board-publish-review-v6.pending'
          and pending.is_file() and not pending.is_symlink(), 'INVALID_PENDING_MARKER')
    marker = json.loads(pending.read_text(encoding='utf-8'))
    check(isinstance(marker,dict) and set(marker) == {'receipt'}, 'UNKNOWN_PENDING_FORMAT')
    receipt = Path(marker['receipt'])
    folder = receipt.parent
    check(receipt.name == 'receipt.json' and folder.parent == pending.parent
          and re.fullmatch(r'douyin-board-v6-[A-Za-z0-9_-]{6,64}', folder.name)
          and folder.is_dir() and not folder.is_symlink() and folder.resolve() == folder,
          'UNSAFE_PENDING_PATH')
    check(not any(folder.iterdir()), 'PENDING_ATTEMPT_HAS_RECEIPT_OR_FILES')
    if os.name == 'posix':
        file_info, dir_info = pending.stat(), folder.stat()
        check(stat.S_ISREG(file_info.st_mode) and file_info.st_uid == dir_info.st_uid == 0
              and file_info.st_mode & 0o777 == 0o600 and dir_info.st_mode & 0o777 == 0o700,
              'PENDING_NOT_PRIVATE')
    db.execute('SET search_path=board_candidate,pg_catalog')
    expected = pg_schema.expected_versions()
    check(len(expected) == 6, 'UNEXPECTED_MIGRATION_LIST')
    check(db.execute('SELECT pg_try_advisory_lock(%s)', (LOCK,)).fetchone()[0], 'OTHER_MIGRATION_RUNNING')
    try:
        with db.transaction():
            db.execute('SET TRANSACTION READ ONLY')
            check(versions(db) == expected[:5] and control(db)[0] == 'open', 'PENDING_DATABASE_NOT_UNCHANGED_V5_OPEN')
            pg_schema.validate_extensions(ServiceConnection(db, schema=SCHEMA, scope_enabled=True), expected[:5])
            check(not any(db.execute('SELECT to_regclass(%s)', (SCHEMA+'.'+name,)).fetchone()[0]
                          for name in NEW_TABLES+NEW_INDEXES), 'PENDING_HAS_PARTIAL_V6')
        destination = folder/'preflight-marker-resolved.json'
        check(not destination.exists(), 'PENDING_ARCHIVE_EXISTS')
        pending.rename(destination)
        with (folder/'preflight-resolution.json').open('x', encoding='utf-8') as output:
            json.dump(dict(status='preflight-never-started', schema_version=5, mode='open',
                           database_changed=False, marker_archived=str(destination),
                           resolved_utc=datetime.now(timezone.utc).isoformat()), output, indent=2)
        (folder/'preflight-resolution.json').chmod(0o600)
        print(json.dumps(dict(phase='preflight-marker-resolved', evidence=str(destination))), flush=True)
    finally:
        db.execute('SELECT pg_advisory_unlock(%s)', (LOCK,))


def backup(db, settings, folder, dump_bin, restore_bin, before):
    path = Path(folder) / 'before-v6.dump'
    check(not path.exists(), 'BACKUP_ALREADY_EXISTS')
    server_major = int(db.execute('SHOW server_version_num').fetchone()[0]) // 10000
    for binary in (dump_bin, restore_bin):
        output = subprocess.check_output([str(binary), '--version'], stderr=subprocess.PIPE, text=True, timeout=10)
        match = re.search(r'PostgreSQL\)\s+(\d+)', output)
        check(match and int(match[1]) == server_major, 'BACKUP_TOOL_VERSION_MISMATCH')
    environment = {key: os.environ[key] for key in ('PATH','Path','SystemRoot','WINDIR','TEMP','TMP','LANG')
                   if key in os.environ}
    environment.update(PGHOST=settings['host'], PGPORT=str(settings['port']),
                       PGDATABASE=settings['dbname'], PGUSER=settings['user'], PGPASSWORD=settings['password'],
                       PGSSLMODE=settings['sslmode'], PGPASSFILE=str(Path(folder) / 'unused-passfile'),
                       PGCONNECT_TIMEOUT='10', PGAPPNAME='douyin-publish-review-backup', PGCLIENTENCODING='UTF8')
    if settings.get('sslrootcert'):
        environment['PGSSLROOTCERT'] = settings['sslrootcert']
    # Writes are already drained and disabled; pg_dump makes its own consistent snapshot.
    subprocess.run([str(dump_bin), '--format=custom', '--schema='+SCHEMA, '--file='+str(path)],
                   env=environment, capture_output=True, check=True, timeout=180)
    path.chmod(0o600)
    check(path.is_file() and not path.is_symlink() and path.stat().st_size > 0, 'BACKUP_INVALID')
    if os.name == 'posix':
        check(path.stat().st_mode & 0o777 == 0o600, 'BACKUP_NOT_PRIVATE')
    listing = subprocess.check_output([str(restore_bin), '--list', str(path)], env=environment, stderr=subprocess.PIPE, timeout=60).decode('utf-8')
    for name, kind, _, _ in before['relations']:
        descriptor = ' SEQUENCE ' if kind == 'S' else ' TABLE '
        check(descriptor+SCHEMA+' '+name+' ' in listing, 'BACKUP_TOC_INCOMPLETE')
        if kind != 'S':
            check(' TABLE DATA '+SCHEMA+' '+name+' ' in listing, 'BACKUP_DATA_TOC_INCOMPLETE')
    subprocess.run([str(restore_bin), '--file='+os.devnull, str(path)], env=environment,
                   capture_output=True, check=True, timeout=180)
    return dict(path=str(path), bytes=path.stat().st_size, sha256=file_sha(path), full_decode=True,
                restore_rehearsed=False, all_old_relations_in_toc=True)


def upgrade(db, folder, settings, dump_bin, restore_bin, ops, guard):
    import pg_schema
    from pg_service_adapter import ServiceConnection
    # A raw connection starts at public; the adapter's schema parameter only
    # configures writer admission and does not change PostgreSQL search_path.
    db.execute('SET search_path=board_candidate,pg_catalog')
    expected = pg_schema.expected_versions()
    check(len(expected) == 6, 'UNEXPECTED_MIGRATION_LIST')
    guard()
    current, original = versions(db), control(db)
    check(original and original[0] == 'open', 'DATABASE_NOT_OPEN')
    check(current in (expected[:5], expected), 'SCHEMA_NOT_REVIEWED_V5_OR_V6')
    pg_schema.validate_extensions(ServiceConnection(db, schema=SCHEMA, scope_enabled=True), current)
    if current == expected:
        save(folder, dict(status='already-v6', changed=False, schema_after=6))
        return dict(status='already-v6', schema_version=6, changed=False)
    check(not any(db.execute('SELECT to_regclass(%s)', (SCHEMA+'.'+name,)).fetchone()[0]
                  for name in NEW_TABLES+NEW_INDEXES), 'PARTIAL_V6_RELATIONS_EXIST')
    check(db.execute('SELECT pg_try_advisory_lock(%s)', (LOCK,)).fetchone()[0], 'OTHER_MIGRATION_RUNNING')
    receipt = dict(status='started', schema_before=5, original_control=original,
                   commit=getattr(ops, 'commit', None),
                   started_utc=datetime.now(timezone.utc).isoformat(), production_backup=str(folder))
    maintained, before, paused = False, None, False
    try:
        save(folder, receipt)
        ops.pause_sync()
        paused = True
        receipt['sync_timer_was_active'] = ops.timer_active
        save(folder, receipt)
        guard()
        check(versions(db) == expected[:5] and control(db) == original, 'PREFLIGHT_CHANGED')
        set_mode(db, original, 'maintenance')  # Waits for every admitted application writer.
        maintained = True
        before = snapshot(db)
        receipt.update(status='maintenance', old_state=before)
        save(folder, receipt)
        receipt['backup'] = backup(db, settings, folder, dump_bin, restore_bin, before)
        receipt['status'] = 'backup-verified'
        save(folder, receipt)
        print(json.dumps(dict(phase='backup-verified', bytes=receipt['backup']['bytes'], receipt=str(Path(folder)/'receipt.json'))), flush=True)
        guard()
        ops.ensure_paused()
        check(versions(db) == expected[:5] and control(db) == ('maintenance', *original[1:]), 'CONTROL_OR_VERSION_CHANGED')
        check(snapshot(db) == before, 'OLD_STATE_CHANGED_DURING_BACKUP')
        check(file_sha(receipt['backup']['path']) == receipt['backup']['sha256'], 'BACKUP_HASH_CHANGED')
        db.execute('SET search_path=board_candidate,pg_catalog')
        receipt['status'] = 'applying-v6'
        save(folder, receipt)
        pg_schema.apply_migrations(db)
        check(versions(db) == expected, 'V6_LEDGER_MISMATCH')
        pg_schema.validate_extensions(ServiceConnection(db, schema=SCHEMA, scope_enabled=True), expected)
        after = snapshot(db)
        check(old_unchanged(before, after), 'OLD_DATA_OR_PERMISSIONS_CHANGED')
        check(set(after['tables']) - set(before['tables']) == set(NEW_TABLES), 'UNEXPECTED_NEW_TABLES')
        check(all(after['tables'][name][0] == 0 for name in NEW_TABLES), 'NEW_TABLES_NOT_EMPTY')
        check(all(db.execute('SELECT to_regclass(%s)', (SCHEMA+'.'+name,)).fetchone()[0]
                  for name in NEW_INDEXES), 'V6_INDEXES_MISSING')
        check(control(db) == ('maintenance', *original[1:]), 'CONTROL_IDENTITY_CHANGED')
        guard()
        receipt.update(status='v6-verified', old_data_unchanged=True, old_acl_sequences_unchanged=True)
        save(folder, receipt)
        set_mode(db, ('maintenance', *original[1:]), 'open')
        maintained = False
        ops.restart_and_verify()
        check(control(db) == original and versions(db) == expected, 'POST_RESTART_CONTROL_CHANGED')
        guard()
        ops.resume_sync()
        paused = False
        receipt.update(status='complete', schema_after=6, mode='open', health_ready=True,
                       finished_utc=datetime.now(timezone.utc).isoformat())
        save(folder, receipt)
        return dict(status='complete', schema_version=6, old_data_unchanged=True,
                    backup=str(Path(folder)/'before-v6.dump'), receipt=str(Path(folder)/'receipt.json'))
    except BaseException as error:
        db.rollback()
        recovered = False
        # Only reopen a provably unchanged v5. A committed v6 failure needs receipt-based review.
        if maintained and before is not None and versions(db) == expected[:5]:
            if control(db) == ('maintenance', *original[1:]) and snapshot(db) == before:
                if not any(db.execute('SELECT to_regclass(%s)', (SCHEMA+'.'+name,)).fetchone()[0] for name in NEW_TABLES+NEW_INDEXES):
                    set_mode(db, ('maintenance', *original[1:]), 'open')
                    maintained, recovered = False, True
        if paused and not maintained and control(db) == original and versions(db) == expected[:5]:
            ops.resume_sync()
            recovered = True
        receipt.update(status='recovered-v5' if recovered else 'stopped-needs-review',
                       failure=type(error).__name__, code=str(error) if isinstance(error, UpgradeStop) else 'CHECK_RECEIPT',
                       mode_restored=recovered, sync_timer_was_active=getattr(ops, 'timer_active', None))
        save(folder, receipt)
        raise
    finally:
        db.execute('SELECT pg_advisory_unlock(%s)', (LOCK,))


class ServerOps:
    def __init__(self, port, build):
        self.port, self.build, self.timer_active = port, build, None

    def state(self, unit):
        result = subprocess.check_output(['systemctl', 'show', unit, '-p', 'LoadState', '-p', 'ActiveState', '-p', 'SubState', '-p', 'MainPID', '-p', 'ControlPID'], text=True, stderr=subprocess.PIPE, timeout=15)
        return dict(line.split('=', 1) for line in result.splitlines() if '=' in line)

    def pause_sync(self):
        state = self.state('douyin-board-sync.timer')
        check(state.get('LoadState') == 'loaded' and state.get('ActiveState') in ('active','inactive'), 'SYNC_TIMER_STATE_UNEXPECTED')
        self.timer_active = state['ActiveState'] == 'active'
        try:
            subprocess.run(['systemctl','stop','douyin-board-sync.timer'], capture_output=True, check=True, timeout=20)
            until = time.monotonic()+45
            while time.monotonic() < until:
                active = self.state('douyin-board-sync.service')
                if active.get('MainPID','0') == '0' and active.get('ControlPID','0') == '0' and active.get('ActiveState') not in ('activating','deactivating','reloading'):
                    return self.ensure_paused()
                time.sleep(1)
            raise UpgradeStop('SYNC_SERVICE_STILL_RUNNING')
        except BaseException:
            self.resume_sync()
            raise

    def ensure_paused(self):
        state, service = self.state('douyin-board-sync.timer'), self.state('douyin-board-sync.service')
        check(state.get('ActiveState') == 'inactive' and service.get('LoadState') == 'loaded'
              and service.get('MainPID') == '0' and service.get('ControlPID') == '0'
              and service.get('ActiveState') not in ('activating','deactivating','reloading'), 'SYNC_NOT_DRAINED')

    def resume_sync(self):
        if self.timer_active:
            subprocess.run(['systemctl','start','douyin-board-sync.timer'], capture_output=True, check=True, timeout=20)
        check(self.state('douyin-board-sync.timer').get('ActiveState') == ('active' if self.timer_active else 'inactive'), 'SYNC_RESTORE_FAILED')

    def restart_and_verify(self):
        import urllib.request
        subprocess.run(['systemctl','restart','douyin-board.service'], capture_output=True, check=True, timeout=45)
        for _ in range(20):
            try:
                for endpoint in ('/api/health','/api/ready'):
                    with urllib.request.urlopen('http://127.0.0.1:'+str(self.port)+endpoint, timeout=3) as response:
                        check(response.status == 200 and response.headers.get('X-Board-Build') == self.build, 'HTTP_BUILD_OR_READINESS_MISMATCH')
                        response.read()
                return
            except Exception:
                time.sleep(1)
        raise UpgradeStop('WEBSITE_RESTART_NEEDS_REVIEW')


def main():
    import fcntl
    import pwd
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expect-commit', required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--resolve-preflight', action='store_true',
                        help='Archive a provably empty v5/open preflight marker before the upgrade')
    args = parser.parse_args()
    check(sys.platform == 'linux' and os.geteuid() == 0 and ROOT == Path('/opt/douyin-board'), 'RUN_ONLY_ON_REVIEWED_SERVER_AS_ROOT')
    check(re.fullmatch(r'[0-9a-f]{40}', args.expect_commit), 'EXACT_COMMIT_REQUIRED')
    pending = Path('/root/douyin-board-publish-review-v6.pending')
    check(not args.resolve_preflight or args.execute, 'PREFLIGHT_RESOLUTION_REQUIRES_EXECUTE')
    check(not pending.exists() or args.resolve_preflight, 'UNRESOLVED_UPGRADE_RECEIPT_EXISTS')
    owner = pwd.getpwuid(ROOT.stat().st_uid).pw_name

    def guard():
        git = ['runuser','-u',owner,'--','git','-C',str(ROOT)]
        check(subprocess.check_output(git+['rev-parse','HEAD'], text=True, stderr=subprocess.PIPE).strip() == args.expect_commit, 'CODE_CHANGED')
        check(not subprocess.check_output(git+['status','--porcelain'], text=True, stderr=subprocess.PIPE).strip(), 'WORKTREE_NOT_CLEAN')
        check(file_sha(ROOT/'migrations/postgresql/0006_publish_review.sql') == SQL6_SHA, 'V6_FILE_NOT_REVIEWED')
        for name in ('pg_schema.py','cloudbase_runtime.py','employee_cards.py','publish_review.py','tools/upgrade_publish_review.py'):
            check(subprocess.check_output(git+['show','HEAD:'+name]) == (ROOT/name).read_bytes(), 'DEPLOYED_SOURCE_DIFFERS')

    guard()
    pid = subprocess.check_output(['systemctl','show','douyin-board.service','-p','MainPID','--value'], text=True).strip()
    check(pid.isdigit() and int(pid)>0, 'WEBSITE_NOT_RUNNING')
    environment = dict(item.decode().split('=',1) for item in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in item)
    from cloudbase_runtime import connect_database, settings
    from board_performance import BUILD_ID
    with connect_database(environment) as db:
        if not args.execute:
            print(json.dumps(dict(status='check-only', schema_version=len(versions(db)), mode=control(db)[0], commit=args.expect_commit)))
            return
        os.umask(0o077)
        descriptor = os.open('/run/lock/douyin-publish-review-v6.lock', os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'a') as lock:
            import stat
            metadata = os.fstat(lock.fileno())
            check(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == 0
                  and metadata.st_mode & 0o777 == 0o600, 'PROCESS_LOCK_NOT_PRIVATE')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if pending.exists():
                check(args.resolve_preflight, 'UNRESOLVED_UPGRADE_RECEIPT_EXISTS')
                resolve_preflight(db, pending)
            folder = Path(tempfile.mkdtemp(prefix='douyin-board-v6-', dir='/root'))
            with pending.open('x', encoding='utf-8') as output:
                json.dump(dict(receipt=str(folder/'receipt.json')), output)
            print(json.dumps(dict(phase='starting', receipt=str(folder/'receipt.json'))), flush=True)
            try:
                ops = ServerOps(int(environment['PORT']), BUILD_ID)
                ops.commit = args.expect_commit
                result = upgrade(db, folder, settings(environment), '/usr/bin/pg_dump', '/usr/bin/pg_restore', ops, guard)
                print(json.dumps(result), flush=True)
            finally:
                receipt = json.loads((folder/'receipt.json').read_text(encoding='utf-8')) if (folder/'receipt.json').exists() else {}
                if receipt.get('status') in ('complete','recovered-v5') or ('result' in locals() and result.get('status') == 'already-v6'):
                    pending.rename(folder/'resolved-marker.json')
                else:
                    print(json.dumps(dict(status='stopped', receipt=str(folder/'receipt.json'), pending=str(pending))), flush=True)


if __name__ == '__main__':
    try:
        main()
    except BaseException as error:
        if isinstance(error, SystemExit) and error.code in (None,0):
            raise
        print(json.dumps(dict(status='STOP', error=str(error) if isinstance(error, UpgradeStop) else type(error).__name__)), flush=True)
        raise SystemExit(1)
