"""Disposable real local PostgreSQL verification; never a production entry point."""
from datetime import datetime
import json
import time
from unittest.mock import patch
from uuid import uuid4
from test_isolation import checked_root, install
from pg_schema import MIGRATIONS, FILES, expected_versions, apply_migrations, runtime_versions_supported, validate_extensions
from pg_synthetic_migration import connect_synthetic
from pg_service_adapter import ServiceConnection


def main():
    install(checked_root())
    import auth
    import employee_cards
    import operations
    import publish_review as review
    checks=[]
    clock=patch.object(review,'now',return_value=datetime(2026,11,2,12,tzinfo=review.BEIJING))
    with clock, connect_synthetic() as connection:
        expected=expected_versions()
        with connection.transaction():
            connection.execute('CREATE SCHEMA board_candidate')
            connection.execute('SET LOCAL search_path=board_candidate,pg_catalog')
            connection.execute('CREATE TABLE schema_migrations(version bigint PRIMARY KEY,sha256 text NOT NULL)')
            for version,digest in expected[:4]:
                connection.execute((MIGRATIONS/FILES[version-1]).read_text(encoding='utf-8'))
                connection.execute('INSERT INTO schema_migrations VALUES(%s,%s)',(version,digest))
        connection.execute('SET search_path=board_candidate,pg_catalog')
        db=ServiceConnection(connection,schema='board_candidate',scope_enabled=True)
        assert runtime_versions_supported(expected[:4])
        assert not review.available(db) and not employee_cards.available(db)
        validate_extensions(db,expected[:4])
        checks.append('v4-readable-both-unapplied-extensions-disabled')
        with connection.transaction():
            setup=auth.mint_setup(db)
            token=auth.setup_owner(db,dict(username='synthetic-owner',name='合成管理员',password='Synthetic-only-test-2026'),'board_setup='+setup)
            owner=auth.session(db,'board_session='+token)
            db.execute("INSERT INTO offices VALUES('a','合成办公室甲',1),('b','合成办公室乙',1)")
            db.execute("INSERT INTO computers(id,name,operator) VALUES('pc-a','合成电脑甲','合成人员甲'),('pc-b','合成电脑乙','合成人员乙')")
            db.execute("INSERT INTO computer_offices VALUES('pc-a','a'),('pc-b','b')")
            for ident,pc,office in [('account-a','pc-a','a'),('account-b','pc-b','b')]:
                db.execute("INSERT INTO accounts(id,computer_id,slot,name,douyin_id,status) VALUES(?,?,1,?,?,'正常')",(ident,pc,'合成'+ident,'handle-'+ident))
                db.execute('INSERT INTO account_offices VALUES(?,?)',(ident,office))
            employee_id=db.execute("INSERT INTO auth_users(username,name,role,password_hash,permissions,created_at) VALUES('synthetic-employee','合成员工','employee','synthetic-only',?,1) RETURNING id", (json.dumps(['publishing_manage','collection_manage']),)).fetchone()[0]
            db.execute("INSERT INTO user_office_grants VALUES(?,'a')",(employee_id,))
        protected=('accounts','computers','auth_users','user_office_grants','account_offices','computer_offices','daily','runtime_control')
        snapshot={table:connection.execute('SELECT * FROM '+table+' ORDER BY 1').fetchall() for table in protected}
        with connection.transaction():
            version,digest=expected[4]
            connection.execute((MIGRATIONS/FILES[4]).read_text(encoding='utf-8'))
            connection.execute('INSERT INTO schema_migrations VALUES(%s,%s)',(version,digest))
        validate_extensions(db,expected[:5])
        assert employee_cards.available(db) and not review.available(db)
        checks.append('v5-employee-cards-still-enabled-review-disabled')
        # Force a DDL conflict in this disposable cluster to prove atomic failure.
        connection.execute('CREATE VIEW publish_review_samples AS SELECT 1 AS synthetic')
        try:
            apply_migrations(connection)
            raise AssertionError('DDL fault was not rejected')
        except Exception as error:
            if isinstance(error,AssertionError):raise
        assert connection.execute('SELECT count(*) FROM schema_migrations').fetchone()[0]==5
        assert connection.execute("SELECT to_regclass('publish_review_tasks')").fetchone()[0] is None
        connection.execute('DROP VIEW publish_review_samples')
        assert apply_migrations(connection)=='applied'
        assert apply_migrations(connection)=='already-applied'
        assert employee_cards.available(db) and review.available(db)
        validate_extensions(db,expected)
        for table,rows in snapshot.items():assert connection.execute('SELECT * FROM '+table+' ORDER BY 1').fetchall()==rows,table
        checks.append('v6-atomic-fault-rollback-retry-idempotent-core-grants-control-unchanged')
        control=connection.execute("UPDATE runtime_control SET mode='open' WHERE id=1 RETURNING writer_epoch,restore_generation").fetchone()
        db=ServiceConnection(connection,schema='board_candidate',scope_enabled=True,writer_epoch=control[0],restore_generation=control[1])
        employee=auth.public_user(db.execute('SELECT * FROM auth_users WHERE id=?',(employee_id,)).fetchone())
        def write(fn,*args,**kwargs):
            db.begin_write()
            try:
                result=fn(*args,**kwargs);db.commit();return result
            except BaseException:db.rollback();raise
        def sample(ident,day,total,at='08:00:00',failed=False):
            return write(review.ingest,db,ident,str(uuid4()),day+'T'+at+'+08:00',total,failed=failed)
        def task(ident,target):
            body=dict(account_id=ident,day='2026-11-02',target=target,task_state='active',computer_id='pc-a' if ident=='account-a' else 'pc-b',reason='合成目标依据',expected_revision=0,request_id=str(uuid4()))
            result=write(review.post,db,owner,'/api/publish-review/task',body)
            assert write(review.post,db,owner,'/api/publish-review/task',body)==result
            return body
        body=task('account-a',5);task('account-b',10)
        sample('account-a','2026-11-01',100);sample('account-a','2026-11-02',109)
        sample('account-b','2026-11-01',100);sample('account-b','2026-11-02',97)
        def view(actor=owner,**query):return review.overview(db,actor,dict(date='2026-11-02',**query))
        rows={row['account_id']:row for row in view()['rows']}
        assert (rows['account-a']['net'],rows['account-a']['progress'],rows['account-a']['state'])==(9,100,'met')
        assert (rows['account-b']['net'],rows['account-b']['state'])==(-3,'review')
        assert view(employee)['total']==1 and view(employee)['rows'][0]['account_id']=='account-a'
        foreign=view(employee,office='b')
        assert foreign['total']==0 and sum(foreign['summary'].values())==0 and not foreign['rows']
        try:review.authorize(db,employee,dict(account_id='account-b'),'collection_manage');raise AssertionError('Foreign write accepted')
        except auth.AccessError:pass
        checks.append('actual-pg-cte-net-negative-summary-and-employee-office-scope')
        sample('account-a','2026-11-01',108,at='23:00:00')
        assert view()['rows'][0]['baseline_total']==100
        sample('account-a','2026-11-02',None,at='09:00:00',failed=True)
        assert next(row for row in view()['rows'] if row['account_id']=='account-a')['state']=='review'
        changed={**body,'request_id':str(uuid4()),'target':8}
        try:write(review.post,db,owner,'/api/publish-review/task',changed);raise AssertionError('Stale target accepted')
        except operations.ConflictError:pass
        assert db.execute('SELECT count(*) FROM daily').fetchone()[0]==0
        checks.append('baseline-immutable-latest-failure-revision-replay-no-manual-publish-change')
        # Previous date keeps dated assignment and target after live data moves.
        with patch.object(review,'now',return_value=datetime(2026,11,3,12,tzinfo=review.BEIJING)):
            historical=review.overview(db,owner,dict(date='2026-11-02'))
            historical_row=next(row for row in historical['rows'] if row['account_id']=='account-a')
            assert historical['historical'] and historical_row['target']==5
            try:write(review.save_task,db,owner,{**body,'expected_revision':1});raise AssertionError('Historical target overwritten')
            except ValueError:pass
        # Large fixture: no payload growth proportional to account detail count.
        with connection.transaction():
            connection.execute("INSERT INTO accounts(id,computer_id,slot,name,douyin_id,status) SELECT 'bulk-'||n,'pc-a',n+10,'合成批量'||n,'synthetic-'||n,'正常' FROM generate_series(1,2000) n")
            connection.execute("INSERT INTO account_offices SELECT id,'a' FROM accounts WHERE id LIKE 'bulk-%'")
        started=time.perf_counter();large=view();elapsed=(time.perf_counter()-started)*1000
        assert large['total']==2002 and len(large['rows'])==50
        raw=json.dumps(large,ensure_ascii=False).encode();assert len(raw)<100000
        assert view(state='pending')['total']==0
        assert view(computer='pc-b')['total']==1
        checks.append('dated-history-and-2002-accounts-scoped-pagination-summary-filters')
        # Ledger must not claim a usable extension with missing tables or a bad hash.
        connection.execute('ALTER TABLE publish_review_tasks RENAME TO synthetic_hidden_tasks')
        try:validate_extensions(db,expected);raise AssertionError('Missing extension accepted')
        except ValueError:pass
        connection.execute('ALTER TABLE synthetic_hidden_tasks RENAME TO publish_review_tasks')
        assert not runtime_versions_supported(expected[:5]+[(6,'drifted')])
        generation='synthetic-restored-generation'
        connection.execute('UPDATE runtime_control SET restore_generation=%s WHERE id=1',(generation,))
        restored=ServiceConnection(connection,schema='board_candidate',scope_enabled=True,writer_epoch=control[0],restore_generation=generation)
        restored.begin_write()
        try:review.post(restored,owner,'/api/publish-review/task',body);raise AssertionError('Restored old request accepted')
        except operations.ConflictError:restored.rollback()
        checks.append('missing-table-hash-drift-and-old-generation-replay-rejected')
    print(json.dumps(dict(passed=True,kind='real-loopback-postgresql-synthetic-only',checks=checks,production_connected=False,
        accounts=2002,first_page_rows=50,first_page_bytes=len(raw),read_ms=round(elapsed,2)),ensure_ascii=True))


if __name__=='__main__':main()
