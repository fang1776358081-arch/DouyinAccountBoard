"""Checks only on a disposable, attested local PostgreSQL synthetic cluster."""
import base64
import csv
import io
import json
from unittest.mock import patch
from uuid import uuid4

import psycopg
from test_isolation import checked_root, install
from pg_synthetic_migration import connect_synthetic
from pg_service_adapter import ServiceConnection
import pg_schema


def _file(headers,rows):
    stream=io.StringIO(newline='')
    csv.writer(stream).writerows([headers,*rows])
    return dict(filename='synthetic.csv',content=base64.b64encode(stream.getvalue().encode('utf-8-sig')).decode())


def main():
    install(checked_root())
    import auth
    import operations
    import office_three_import as source
    import table_sync as sync
    day,checks='2026-11-04',[]
    with connect_synthetic() as connection:
        versions=pg_schema.expected_versions()
        with patch.object(pg_schema,'expected_versions',return_value=versions[:6]):
            pg_schema.apply_migrations(connection)
        connection.execute('SET search_path=board_candidate,pg_catalog')
        installed=connection.execute('SELECT version,sha256 FROM schema_migrations ORDER BY version').fetchall()
        assert pg_schema.runtime_versions_supported(installed) and installed==versions[:6]
        control=connection.execute('SELECT writer_epoch,restore_generation FROM runtime_control WHERE id=1').fetchone()
        db=ServiceConnection(connection,schema='board_candidate',scope_enabled=True,writer_epoch=control[0],restore_generation=control[1])
        with connection.transaction():
            token=auth.mint_setup(db)
            session=auth.setup_owner(db,dict(username='synthetic-sync-owner',name='合成PG同步管理员',
                  password='Synthetic-only-test-2026'),'board_setup='+token)
            actor=auth.session(db,'board_session='+session)
            db.execute("INSERT INTO offices(id,name) VALUES('three','办公室三'),('other','合成其他办公室')")
            db.execute("INSERT INTO computers VALUES('office3:1','合成三办电脑','','','',''),('other-pc','合成其他电脑','','','','')")
            db.execute("INSERT INTO computer_offices VALUES('office3:1','three'),('other-pc','other')")
            db.execute("INSERT INTO accounts(id,computer_id,slot,name,status,note,douyin_id,homepage,last_collected,registered_on,registration_phone) VALUES('pg-three','office3:1',1,'合成旧名','正常','合成手工备注','000PGCase','','','2026-01-01','13800001111'),('pg-other','other-pc',1,'合成外办名称','正常','','pg_foreign','','','','')")
            db.execute("INSERT INTO account_offices VALUES('pg-three','three'),('pg-other','other')")
            db.execute("INSERT INTO publishing_layout_state VALUES(1,'synthetic-pg-batch')")
            db.execute("INSERT INTO publishing_layout VALUES('synthetic-pg-batch','office3:1',1,'pg-three','合成旧发布位置','办公室三','H3','synthetic','')")
        unavailable=sync.get(db,actor,'/api/table-sync',{'office_id':['three']},day)
        assert not unavailable['available'] and not unavailable['can_manage']
        checks.append('real-pg-v6-remains-compatible-optional-v7-does-not-write-or-block-existing-site')
        pg_schema.apply_migrations(connection)
        assert connection.execute('SELECT version,sha256 FROM schema_migrations WHERE version<=6 ORDER BY version').fetchall()==installed
        assert sync.available(db)
        connection.execute("UPDATE runtime_control SET mode='open' WHERE id=1")
        assert sync.get(db,actor,'/api/table-sync',{'office_id':['three']},day)['pilot']['state']=='disabled'
        checks.append('real-pg-v7-additive-hashes-preserved-default-disabled')

        def send(action,body=None,request_id=None):
            payload=dict(office_id='three',request_id=request_id or str(uuid4()),**(body or {}))
            db.begin_write()
            try:
                result=sync.post(db,actor,'/api/table-sync/'+action,payload,day)
                db.commit()
                return result
            except BaseException:
                db.rollback()
                raise

        def row(handle='000PGCase',name='合成PG正式名称'):
            return ['2','13800002222',name,'29',handle,'','合成门头','合成负责人','1','大号','正常','','合成使用情况','2026-09-01']

        def preview(handle='000PGCase',name='合成PG正式名称',mode='update'):
            return send('preview',dict(mode=mode,source_round='合成PG同轮',accounts_file=_file(source.ACCOUNTS_HEADERS,[row(handle,name)]),
                progress_file=_file(source.PROGRESS_HEADERS,[['3','1','合成负责人','','',*['']*10]])))

        def apply(value,request_id=None):
            return send('rollback-apply' if value['mode']=='rollback' else 'apply',
                dict(preview_id=value['preview_id'],selected_rows=[item['row_key'] for item in value['rows'] if item['selected']],
                     confirmation=value['confirmation']),request_id)

        try:
            preview()
            raise AssertionError('Disabled pilot unexpectedly accepted preview')
        except ValueError:
            pass
        send('pilot',dict(state='enabled',expected_revision=0,confirmation='开启办公室三试点'))
        before=sync._snapshot(db)
        prepared=preview()
        assert prepared['summary']['safe_accounts']==1 and sync._snapshot(db)==before
        receipt=apply(prepared)
        after=sync._snapshot(db)
        assert after['protected']==before['protected'] and after['scopes']==before['scopes']
        assert after['accounts']['pg-three']['name']=='合成PG正式名称'
        assert after['accounts']['pg-three']['note']=='合成手工备注'
        assert after['accounts']['pg-three']['phone_device_id'] is None
        assert sync.get(db,actor,'/api/table-sync/batch',{'office_id':['three'],'id':[receipt['batch_id']]},day)['changes']==receipt['receipt']['changes']
        checks.append('real-pg-preview-apply-safe-fields-durable-receipt-and-layout-preserved')

        repeated=preview()
        assert repeated['summary']['safe_accounts']==0
        assert apply(repeated)['summary']['applied_accounts']==0
        foreign=preview('pg_foreign')
        assert foreign['summary']['safe_accounts']==0
        assert foreign['rows'][0]['account_id']=='' and foreign['rows'][0]['current_name']==''
        assert apply(foreign)['receipt']['issues']
        checks.append('real-pg-repeat-no-duplicate-and-foreign-conflict-only-receipt')

        fresh=preview(name='合成PG再次修改')
        request_id=str(uuid4())
        # A real database failure after preview must roll back the entire action
        # and must not reserve a success receipt for this UUID.
        connection.execute("CREATE FUNCTION synthetic_sync_reject() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic-only-rejection'; END $$")
        connection.execute('CREATE TRIGGER synthetic_sync_reject BEFORE UPDATE ON accounts FOR EACH ROW EXECUTE FUNCTION synthetic_sync_reject()')
        stable=sync._snapshot(db)
        try:
            apply(fresh,request_id)
            raise AssertionError('Synthetic PG trigger failure unexpectedly accepted')
        except psycopg.errors.RaiseException:
            pass
        assert sync._snapshot(db)==stable
        assert db.execute('SELECT 1 FROM table_sync_requests WHERE request_id=?',(request_id,)).fetchone() is None
        connection.execute('DROP TRIGGER synthetic_sync_reject ON accounts')
        connection.execute('DROP FUNCTION synthetic_sync_reject()')
        saved=apply(fresh,request_id)
        stable=sync._snapshot(db)
        assert apply(fresh,request_id)==saved and sync._snapshot(db)==stable
        assert sync.get(db,actor,'/api/table-sync/request',{'office_id':['three'],'request_id':[request_id]},day)==saved
        checks.append('real-pg-late-failure-full-rollback-original-uuid-retry-and-replay-once')

        query_log=[]
        real_execute=db.execute
        def traced(query,parameters=None):
            query_log.append(query)
            return real_execute(query,parameters)
        with patch.object(db,'execute',side_effect=traced):
            source_result=sync.get(db,actor,'/api/table-sync/account-sources',{'office_id':['three']},day)
        assert len(source_result['sources'])==1
        assert source_result['sources'][0]['responsible']=='合成负责人'
        assert sum('table_sync_account_sources s JOIN accounts' in query for query in query_log)==1
        assert not any('SELECT * FROM phone_cards' in query or 'SELECT * FROM publishing_layout' in query for query in query_log)
        checks.append('real-pg-source-metadata-narrow-scoped-query-no-full-board')

        connection.execute("UPDATE accounts SET name='合成后续人工名称' WHERE id='pg-three'")
        reverse=send('rollback-preview',dict(batch_id=saved['batch_id']))
        assert reverse['summary']['safe_accounts']==0 and reverse['rows'][0]['action']=='conflict'
        assert db.execute("SELECT name FROM accounts WHERE id='pg-three'").fetchone()[0]=='合成后续人工名称'
        checks.append('real-pg-reverse-preview-preserves-later-manual-edit')

        new=apply(preview('000PGNew','合成PG新增',mode='add'))
        new_id=new['receipt']['changes'][0]['account_id']
        assert db.execute('SELECT phone_device_id,parent_account_id FROM accounts WHERE id=?',(new_id,)).fetchone().values==(None,None)
        reverse=send('rollback-preview',dict(batch_id=new['batch_id']))
        assert reverse['summary']['safe_accounts']==1
        apply(reverse)
        assert db.execute('SELECT 1 FROM accounts WHERE id=?',(new_id,)).fetchone() is None
        assert sync._snapshot(db)['protected']==before['protected']
        checks.append('real-pg-new-account-no-device-or-publish-link-and-guarded-reversal')
        with connection.transaction():
            db.execute("INSERT INTO accounts(id,computer_id,slot,name,status,note,douyin_id,homepage,account_type,registration_phone) VALUES('pg-parent','office3:1',99,'合成PG关联大号','正常','','pg_parent','','大号','13800003333')")
            db.execute("INSERT INTO account_offices VALUES('pg-parent','three')")
            db.execute("UPDATE accounts SET account_type='小号',parent_account_id='pg-parent',registration_phone='13800003333' WHERE id='pg-three'")
        paired=sync._snapshot(db)
        conflict=preview()
        assert conflict['summary']['safe_accounts']==0
        assert '注册手机号不一致' in conflict['rows'][0]['reason']
        assert sync._snapshot(db)==paired
        checks.append('real-pg-lifecycle-validator-blocks-registration-phone-parent-pair-divergence')
    print(json.dumps(dict(passed=True,kind='real-loopback-postgresql-synthetic-table-sync-only',
                          checks=checks,production_connected=False),ensure_ascii=True))


if __name__=='__main__':main()
