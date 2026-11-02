"""Reviewed subprocess harness, like pg_process_checks, for real backup binaries.

Keeps a separate audit policy: only the vendor pg_dump/pg_restore executable,
the fresh probe endpoint and files inside its marked root are admitted. Does
not disable or modify test_isolation's no-child-process policy for app tests.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
from uuid import uuid4

from test_isolation import checked_root, PROTECTED, inside
from pg_synthetic_migration import connect_synthetic
import pg_schema


def main():
    root = checked_root()
    binaries = Path(os.environ['DOUYIN_BOARD_PROBE_BIN']).resolve()
    dump, restore = binaries/'pg_dump.exe', binaries/'pg_restore.exe'
    os.environ['TMP'] = os.environ['TEMP'] = str(root)

    def audit(event, args):
        if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).resolve()
            if any(inside(path, protected) for protected in PROTECTED):
                raise PermissionError('Protected project data denied')
            if path.suffix.lower() in ('.db','.sqlite','.sqlite3') and not inside(path, root):
                raise PermissionError('Database file outside test root')
        elif event == 'socket.connect':
            address = args[1]
            if not isinstance(address,tuple) or address[:2] != ('127.0.0.1',int(os.environ['PGPORT'])):
                raise PermissionError('Only the fresh probe socket admitted')
        elif event == 'os.system':
            raise PermissionError('No shell admitted')
        elif event == 'subprocess.Popen':
            command, environment = args[1], args[3] or os.environ
            if isinstance(command,str) and os.name=='nt':
                # This harness deliberately uses only ASCII paths without spaces.
                if '"' in command or "'" in command:
                    raise PermissionError('Quoted Windows commands not admitted')
                command = command.split()
            if not isinstance(command,(list,tuple)) or Path(command[0]).resolve() not in (dump,restore):
                raise PermissionError('Only reviewed PostgreSQL backup binaries admitted')
            if list(command[1:]) == ['--version']:
                return
            if any(environment.get(k) != os.environ[k] for k in ('PGHOST','PGPORT','PGUSER','PGPASSWORD','PGDATABASE','PGSSLMODE')):
                raise PermissionError('Backup endpoint must match the marked fresh probe')
            if Path(command[0]).resolve() == dump:
                if len(command)!=4 or command[1:3] != ['--format=custom','--schema=board_candidate'] or not command[3].startswith('--file='):
                    raise PermissionError('Unreviewed dump arguments')
                path = Path(command[3][7:]).resolve()
            else:
                allowed = (len(command)==3 and command[1] in ('--list','--file='+os.devnull)
                           or len(command)==4 and command[1:3]==['--exit-on-error','--dbname=synthetic_upgrade_restore'])
                if not allowed:
                    raise PermissionError('Unreviewed restore arguments')
                path = Path(command[-1]).resolve()
            if not inside(path,root) or path.suffix!='.dump':
                raise PermissionError('Only this run archive admitted')

    sys.addaudithook(audit)
    source = Path(__file__).resolve().parent/'tools/upgrade_publish_review.py'
    spec = importlib.util.spec_from_file_location('review_upgrade_candidate', source)
    upgrade = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(upgrade)
    settings = dict(host='127.0.0.1', port=int(os.environ['PGPORT']), dbname='synthetic_probe',
                    user='synthetic_owner', password=os.environ['PGPASSWORD'], sslmode='disable')

    class Ops:
        commit = 'synthetic-source'
        timer_active = True
        def __init__(self):
            self.paused = False
            self.restarts = self.resumes = 0
        def pause_sync(self): self.paused = True
        def ensure_paused(self): assert self.paused
        def resume_sync(self): self.paused = False; self.resumes += 1
        def restart_and_verify(self): self.restarts += 1

    checks = []
    for key,value in (('MainPID','17'),('ControlPID','19'),('ActiveState','activating'),('LoadState','not-found')):
        ops=upgrade.ServerOps(12345,'synthetic-build')
        def state(unit):
            data=dict(LoadState='loaded',ActiveState='inactive',MainPID='0',ControlPID='0')
            if unit.endswith('.service'):data[key]=value
            return data
        ops.state=state
        try:
            ops.ensure_paused()
            raise AssertionError('Undrained or unknown sync unit accepted')
        except upgrade.UpgradeStop as error:assert str(error)=='SYNC_NOT_DRAINED'
    checks.append('sync-guard-checks-mainpid-controlpid-and-known-service-not-only-is-active')
    ops=upgrade.ServerOps(12345,'synthetic-build')
    ops.timer_active=False
    ops.state=lambda unit: dict(ActiveState='inactive')
    with patch.object(upgrade.subprocess,'run',side_effect=AssertionError('Inactive timer was started')):
        ops.resume_sync()
    timer=dict(LoadState='loaded',ActiveState='active',MainPID='0',ControlPID='0')
    ops=upgrade.ServerOps(12345,'synthetic-build')
    ops.state=lambda unit: dict(timer) if unit.endswith('.timer') else dict(LoadState='loaded',ActiveState='active',MainPID='23',ControlPID='0')
    def systemctl(command,**kwargs):
        assert command[:2] in (['systemctl','start'],['systemctl','stop'])
        timer['ActiveState']='active' if command[1]=='start' else 'inactive'
    with patch.object(upgrade.subprocess,'run',side_effect=systemctl), patch.object(upgrade.time,'monotonic',side_effect=[0,0,46]), patch.object(upgrade.time,'sleep'):
        try:
            ops.pause_sync()
            raise AssertionError('Busy sync command accepted')
        except upgrade.UpgradeStop as error:assert str(error)=='SYNC_SERVICE_STILL_RUNNING'
    assert timer['ActiveState']=='active'
    checks.append('original-inactive-timer-stays-off-and-busy-sync-timeout-restores-active-timer')
    def folder():
        value = root/('upgrade-'+uuid4().hex)
        value.mkdir()
        return value

    with connect_synthetic() as db:
        expected = pg_schema.expected_versions()
        with db.transaction():
            db.execute('CREATE SCHEMA board_candidate')
            db.execute('SET LOCAL search_path=board_candidate,pg_catalog')
            db.execute('CREATE TABLE schema_migrations(version bigint PRIMARY KEY,sha256 text NOT NULL)')
            for version, digest in expected[:5]:
                db.execute((pg_schema.MIGRATIONS/pg_schema.FILES[version-1]).read_text(encoding='utf-8'))
                db.execute('INSERT INTO schema_migrations VALUES(%s,%s)', (version,digest))
        db.execute('SET search_path=board_candidate,pg_catalog')
        db.execute("UPDATE runtime_control SET mode='open',writer_epoch=7,restore_generation='synthetic-upgrade-generation'")
        db.execute("INSERT INTO computers(id,name) VALUES('synthetic-pc','合成电脑')")
        db.execute("INSERT INTO accounts(id,computer_id,slot,name,douyin_id,status) VALUES('synthetic-account','synthetic-pc',1,'合成账号','000123456','正常')")
        db.execute("INSERT INTO auth_users(username,name,role,password_hash,created_at) VALUES('synthetic-admin','合成管理员','owner','synthetic-only-value-never-printed',1)")
        initial = upgrade.snapshot(db)
        original = upgrade.control(db)

        # A failed backup must never reach DDL; original mode/timer are recovered.
        ops, path = Ops(), folder()
        with patch.object(upgrade, 'backup', side_effect=upgrade.UpgradeStop('SYNTHETIC_BACKUP_FAILURE')):
            try:
                upgrade.upgrade(db,path,settings,dump,restore,ops,lambda: None)
                raise AssertionError('Backup fault accepted')
            except upgrade.UpgradeStop as error:
                assert str(error)=='SYNTHETIC_BACKUP_FAILURE'
        assert upgrade.control(db)==original and upgrade.versions(db)==expected[:5]
        assert upgrade.snapshot(db)==initial and not ops.paused and ops.restarts==0
        assert json.loads((path/'receipt.json').read_text(encoding='utf-8'))['status']=='recovered-v5'
        checks.append('backup-failure-keeps-v5-and-restores-original-mode-timer-without-ddl')

        # Exercise real pg_dump, full decode, and transactionally failed DDL.
        ops, path = Ops(), folder()
        def failed_ddl(connection):
            with connection.transaction():
                connection.execute('CREATE TABLE board_candidate.publish_review_samples(synthetic integer)')
                connection.execute('SELECT synthetic_intentionally_missing_function()')
        with patch.object(pg_schema, 'apply_migrations', side_effect=failed_ddl):
            try:
                upgrade.upgrade(db,path,settings,dump,restore,ops,lambda: None)
                raise AssertionError('DDL fault accepted')
            except Exception as error:
                if type(error).__name__ != 'UndefinedFunction':raise
        assert upgrade.control(db)==original and upgrade.versions(db)==expected[:5]
        assert upgrade.snapshot(db)==initial and not ops.paused and ops.restarts==0
        assert (path/'before-v6.dump').stat().st_size>0
        checks.append('real-backup-full-decode-and-ddl-rollback-preserve-core-rows-acl-sequences')

        # External data drift while paused is detected, never silently overwritten.
        ops, path, calls = Ops(), folder(), []
        def drift_guard():
            calls.append(1)
            if len(calls)==3:
                db.execute("UPDATE accounts SET name='合成外部改动' WHERE id='synthetic-account'")
        try:
            upgrade.upgrade(db,path,settings,dump,restore,ops,drift_guard)
            raise AssertionError('Concurrent drift accepted')
        except upgrade.UpgradeStop as error:
            assert str(error)=='OLD_STATE_CHANGED_DURING_BACKUP'
        assert upgrade.control(db)[0]=='maintenance' and ops.paused
        assert upgrade.versions(db)==expected[:5]
        db.execute("UPDATE accounts SET name='合成账号' WHERE id='synthetic-account'")
        db.execute("UPDATE runtime_control SET mode='open'")
        checks.append('external-row-drift-keeps-maintenance-and-original-data-is-not-guessed-or-overwritten')

        # Same schema and real client versions as production, all data synthetic.
        ops, path = Ops(), folder()
        result=upgrade.upgrade(db,path,settings,dump,restore,ops,lambda: None)
        assert result['status']=='complete' and upgrade.versions(db)==expected
        assert upgrade.control(db)==original and not ops.paused and ops.restarts==1
        assert upgrade.old_unchanged(initial,upgrade.snapshot(db))
        receipt=json.loads((path/'receipt.json').read_text(encoding='utf-8'))
        assert receipt['backup']['full_decode'] and receipt['backup']['all_old_relations_in_toc']
        assert receipt['sync_timer_was_active'] is True
        text=(path/'receipt.json').read_text(encoding='utf-8')
        assert settings['password'] not in text and 'synthetic-only-value-never-printed' not in text
        checks.append('real-v5-to-v6-four-empty-tables-old-records-grants-identities-control-preserved')

        # Restore this actual pre-upgrade archive to a second disposable database.
        db.execute('CREATE DATABASE synthetic_upgrade_restore TEMPLATE template0')
        subprocess.run([str(restore),'--exit-on-error','--dbname=synthetic_upgrade_restore',str(path/'before-v6.dump')],
                       capture_output=True,check=True,timeout=90)
        import psycopg
        with psycopg.connect(dbname='synthetic_upgrade_restore',autocommit=True) as restored:
            assert upgrade.versions(restored)==expected[:5]
            assert upgrade.snapshot(restored)==initial
            assert upgrade.control(restored)==('maintenance',*original[1:])
        checks.append('real-archive-restored-to-second-disposable-db-matches-all-old-row-acl-sequence-fingerprints')

        # Existing v6 data is retained and a repeat does not restart/collect/write.
        db.execute("INSERT INTO publish_review_requests VALUES(1,'synthetic-existing','synthetic-generation','synthetic-fingerprint','{}')")
        ops = Ops()
        repeated=upgrade.upgrade(db,folder(),settings,dump,restore,ops,lambda: None)
        assert repeated['status']=='already-v6' and ops.restarts==ops.resumes==0
        assert db.execute('SELECT count(*) FROM publish_review_requests').fetchone()[0]==1
        checks.append('already-v6-repeat-is-readonly-retains-existing-new-module-records')

    print(json.dumps(dict(passed=True,kind='real-loopback-postgresql-upgrade-rehearsal',checks=checks,
        production_connected=False,systemd_actions='simulated-only',posix_permissions='source-guard-not-Windows-host-test')))


if __name__=='__main__':main()
