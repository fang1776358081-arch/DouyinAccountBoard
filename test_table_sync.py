"""Dual-sheet pilot services; synthetic local SQLite only, no customer files."""
import base64
import copy
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from test_isolation import checked_root

checked_root()

import app
import auth
import idempotency
import office_scope
import operations
import pg_schema
import table_sync as sync
import test_office_three_import as support
from sql_dialect import begin_write

DAY = support.DAY


class TableSyncTest(unittest.TestCase):
    insert_account = staticmethod(support.OfficeThreeFixture.insert_account)

    def setUp(self):
        support.OfficeThreeFixture.setUp(self)
        with app.connect() as db:
            sync.migrate(db)

    def send(self, action, body=None, *, request_id=None, actor=None):
        payload = dict(office_id='three', request_id=request_id or str(uuid4()), **(body or {}))
        with app.connect() as db:
            begin_write(db)
            return sync.post(db, actor or self.actor, '/api/table-sync/'+action, payload, DAY)

    def enable(self):
        with app.connect() as db:
            rev = sync._pilot(db, 'three')['revision']
        return self.send('pilot',dict(state='enabled',expected_revision=rev,confirmation='开启办公室三试点'))

    def source(self, *, mode='update', accounts=None, progress=None, source_round='合成轮次一'):
        return dict(mode=mode,source_round=source_round,
                    accounts_file=support.csv_file(support.ACCOUNT_HEADERS,accounts or [support.account_row('synthetic_reuse',name='合成正式名称')]),
                    progress_file=support.csv_file(support.PROGRESS_HEADERS,progress if progress is not None else [support.progress_row(labels=())]))

    def prepare(self, **kwargs):
        return self.send('preview',self.source(**kwargs))

    def apply(self, preview, *, selected=None, request_id=None):
        return self.send('rollback-apply' if preview['mode']=='rollback' else 'apply',
            dict(preview_id=preview['preview_id'],selected_rows=selected if selected is not None else [row['row_key'] for row in preview['rows'] if row['selected']],
                 confirmation=preview['confirmation']),request_id=request_id)

    def business(self):
        with app.connect() as db:
            return sync._snapshot(db)

    def read(self,path='/api/table-sync',query=None,actor=None):
        with app.connect() as db:
            return sync.get(db,actor or self.actor,path,query or {'office_id':['three']},DAY)

    def test_default_off_and_optional_schema_are_explicit_and_reads_do_not_seed(self):
        before = self.business()
        result=self.read()
        self.assertTrue(result['available'])
        self.assertEqual(result['pilot']['state'],'disabled')
        self.assertEqual(result['pilot']['revision'],0)
        self.assertEqual(self.business(),before)
        with self.assertRaises(ValueError):self.prepare()
        with app.connect() as db:
            for table in reversed(sync.TABLES):db.execute('DROP TABLE '+table)
        result=self.read()
        self.assertFalse(result['available'])
        self.assertFalse(result['can_manage'])
        self.assertIn('0007',result['note'])
        self.assertEqual(self.read('/api/table-sync/account-sources')['sources'],[])

    def test_pilot_owner_revision_confirmation_and_audit(self):
        enabled=self.enable()
        self.assertEqual(enabled['pilot']['revision'],1)
        with self.assertRaises(operations.ConflictError):
            self.send('pilot',dict(state='paused',expected_revision=0,confirmation='暂停办公室三试点'))
        with self.assertRaises(ValueError):
            self.send('pilot',dict(state='paused',expected_revision=1,confirmation='同意'))
        with app.connect() as db:
            employee=auth.public_user(db.execute('SELECT * FROM auth_users WHERE id=?',(self.employee_id,)).fetchone())
        with self.assertRaises(auth.AccessError):
            self.send('pilot',dict(state='paused',expected_revision=1,confirmation='暂停办公室三试点'),actor=employee)
        paused=self.send('pilot',dict(state='paused',expected_revision=1,confirmation='暂停办公室三试点'))
        self.assertEqual(paused['pilot']['state'],'paused')
        with app.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM table_sync_events').fetchone()[0],2)

    def test_preview_apply_only_allowlist_fields_and_durable_receipt(self):
        self.enable()
        before=self.business()
        preview=self.prepare()
        self.assertEqual(preview['summary']['safe_accounts'],1)
        self.assertEqual(self.business(),before)
        result=self.apply(preview)
        self.assertEqual(result['summary']['applied_accounts'],1)
        self.assertTrue(all(result['verification'].values()))
        after=self.business()
        self.assertEqual(after['protected'],before['protected'])
        self.assertEqual(after['scopes'],before['scopes'])
        old,new=before['accounts']['old-three'],after['accounts']['old-three']
        changed={key for key in old if old[key]!=new[key]}
        self.assertEqual(changed,{'name','registration_phone','registered_on'})
        self.assertEqual(new['note'],'原有备注必须保留')
        self.assertIsNone(new['parent_account_id'])
        source=after['sources']['old-three']
        self.assertEqual(source['responsible'],'合成负责人')
        self.assertEqual(source['business_type'],'大号')
        self.assertEqual(json.loads(source['source_data'])['source_phone_number'],'2')
        receipt=self.read('/api/table-sync/batch',{'office_id':['three'],'id':[result['batch_id']]})
        self.assertEqual(receipt['batch_id'],result['batch_id'])
        self.assertIn('sha256',receipt['source']['accounts'])
        self.assertEqual(receipt['changes'],result['receipt']['changes'])

    def test_same_request_replays_and_different_body_or_generation_cannot_reuse(self):
        self.enable()
        preview=self.prepare()
        request_id=str(uuid4())
        result=self.apply(preview,request_id=request_id)
        after=self.business()
        self.assertEqual(self.apply(preview,request_id=request_id),result)
        self.assertEqual(self.business(),after)
        queried=self.read('/api/table-sync/request',{'office_id':['three'],'request_id':[request_id]})
        self.assertEqual(queried,result)
        with self.assertRaises(idempotency.IdempotencyError):
            self.send('apply',dict(preview_id=preview['preview_id'],selected_rows=[],confirmation=preview['confirmation']),request_id=request_id)
        with app.connect() as db:db.execute('UPDATE table_sync_requests SET generation=? WHERE request_id=?',('old-generation',request_id))
        with self.assertRaises(idempotency.IdempotencyError):self.apply(preview,request_id=request_id)
        with self.assertRaises(sync.RequestNotFound):
            self.read('/api/table-sync/request',{'office_id':['three'],'request_id':[str(uuid4())]})

    def test_repeat_exact_import_is_noop_without_duplicate_or_new_quota_records(self):
        self.enable()
        self.apply(self.prepare())
        before=self.business()
        second=self.prepare()
        self.assertEqual(second['summary']['safe_accounts'],0)
        self.assertEqual([row['action'] for row in second['rows']],['same'])
        self.assertEqual(self.business(),before)
        receipt=self.apply(second)
        self.assertEqual(receipt['summary']['applied_accounts'],0)
        after=self.business()
        for key in ('accounts','sources','scopes','protected','history'):self.assertEqual(after[key],before[key])

    def test_blank_cells_never_clear_account_or_existing_source_fields(self):
        self.enable()
        self.apply(self.prepare())
        row=support.account_row('synthetic_reuse',name='')
        for index in (1,7,8,9,10,11,12,13):row[index]=''
        before=self.business()
        preview=self.prepare(accounts=[row])
        result=self.apply(preview)
        after=self.business()
        self.assertEqual(after['accounts'],before['accounts'])
        for key in sync.SOURCE_FIELDS:self.assertEqual(after['sources']['old-three'][key],before['sources']['old-three'][key])
        self.assertEqual(result['summary']['applied_accounts'],1)  # new safe source evidence only

    def test_raw_identity_keeps_leading_zero_case_and_rejects_whitespace(self):
        self.enable()
        with app.connect() as db:db.execute("UPDATE accounts SET douyin_id='000AbC' WHERE id='old-three'")
        preview=self.prepare(accounts=[support.account_row('000AbC',name='合成精确')])
        self.assertEqual(preview['rows'][0]['douyin_id'],'000AbC')
        self.assertEqual(preview['summary']['safe_accounts'],1)
        for handle in ('000abc',' 000AbC','000AbC '):
            with self.subTest(handle=handle):
                p=self.prepare(accounts=[support.account_row(handle)])
                self.assertEqual(p['summary']['safe_accounts'],0)
                self.assertEqual(p['rows'][0]['douyin_id'],handle)

    def test_duplicates_in_either_source_and_global_system_are_never_guessed(self):
        self.enable()
        duplicate=support.account_row('synthetic_reuse',row=12)
        preview=self.prepare(accounts=[support.account_row('synthetic_reuse'),duplicate])
        self.assertEqual(preview['summary']['conflict_accounts'],1)
        self.assertEqual(preview['summary']['safe_accounts'],0)
        preview=self.prepare(progress=[support.progress_row(labels=('抖音号:synthetic_reuse','抖音号:synthetic_reuse'))])
        self.assertEqual(preview['summary']['safe_accounts'],0)
        with app.connect() as db:self.insert_account(db,'duplicate','synthetic_reuse','另条合成','office3:1',3,'three')
        self.assertEqual(self.prepare()['summary']['safe_accounts'],0)

    def test_other_office_and_pending_ids_skip_without_revealing_stored_names(self):
        self.enable()
        with app.connect() as db:db.execute("UPDATE accounts SET douyin_id='synthetic_foreign' WHERE id='synth-account-1'")
        for handle in ('synthetic_foreign','synthetic_pending'):
            preview=self.prepare(accounts=[support.account_row(handle)])
            self.assertEqual(preview['summary']['safe_accounts'],0)
            self.assertEqual(preview['rows'][0]['current_name'],'')
            self.assertEqual(preview['rows'][0]['account_id'],'')

    def test_only_exact_explicit_progress_handles_participate_in_identity(self):
        self.enable()
        preview=self.prepare(progress=[support.progress_row(labels=('2 合成正式名称',))])
        self.assertEqual(preview['summary']['safe_accounts'],1)
        issue=next(row for row in preview['rows'] if row['row_key'].startswith('progress:'))
        self.assertEqual(issue['action'],'skip')
        self.assertEqual(issue['douyin_id'],'')
        preview=self.prepare(progress=[support.progress_row(computer='9',labels=('抖音号:synthetic_reuse',))])
        self.assertEqual(preview['summary']['safe_accounts'],0)

    def test_safe_add_only_account_and_source_no_phone_card_computer_layout_creation(self):
        self.enable()
        before=self.business()
        preview=self.prepare(mode='add',accounts=[support.account_row('000NewCase',name='合成新账号')])
        self.assertEqual(preview['summary']['new_accounts'],1)
        result=self.apply(preview)
        after=self.business()
        self.assertEqual(after['protected'],before['protected'])
        new_id=next(iter(set(after['accounts'])-set(before['accounts'])))
        account=after['accounts'][new_id]
        self.assertEqual(account['douyin_id'],'000NewCase')
        self.assertEqual(account['computer_id'],'office3:1')
        self.assertIsNone(account['phone_device_id'])
        self.assertIsNone(account['parent_account_id'])
        self.assertEqual(account['account_type'],'待确认')
        self.assertEqual(account['note'],'')
        self.assertEqual(after['sources'][new_id]['business_type'],'大号')
        self.assertEqual(result['receipt']['kind'],'add')
        self.assertEqual(self.prepare(mode='add',accounts=[support.account_row('000NewCase',name='合成新账号')])['summary']['new_accounts'],0)

    def test_add_requires_existing_exclusive_computer_and_free_slot(self):
        self.enable()
        for computer in ('','9','999'):
            preview=self.prepare(mode='add',accounts=[support.account_row('NewUnique',computer=computer)])
            self.assertEqual(preview['summary']['safe_accounts'],0)
        with app.connect() as db:
            for slot in range(2,101):self.insert_account(db,'full-'+str(slot),'full_handle_'+str(slot),'合成满位','office3:1',slot,'three')
        self.assertEqual(self.prepare(mode='add',accounts=[support.account_row('NewUnique')])['summary']['safe_accounts'],0)

    def test_registration_computer_can_move_without_actual_publication_change(self):
        self.enable()
        with app.connect() as db:
            db.execute("INSERT INTO computers VALUES('office3:2','合成第二台','','','','')")
            db.execute("INSERT INTO computer_offices VALUES('office3:2','three')")
        before=self.business()
        preview=self.prepare(accounts=[support.account_row('synthetic_reuse',computer='2')],progress=[support.progress_row(computer='2',labels=())])
        self.apply(preview)
        after=self.business()
        self.assertEqual(after['accounts']['old-three']['computer_id'],'office3:2')
        self.assertEqual(after['protected'],before['protected'])

    def test_unknown_status_invalid_phone_date_and_nurture_effect_are_conflicts(self):
        self.enable()
        examples=[]
        for index,value in ((10,'未有映射'),(1,'123'),(13,'2099-01-01'),(10,'养号7天'),(13,'2026-10-27')):
            row=support.account_row('synthetic_reuse');row[index]=value;examples.append(row)
        before=self.business()
        for row in examples:
            preview=self.prepare(accounts=[row])
            self.assertEqual(preview['summary']['safe_accounts'],0)
        self.assertEqual(self.business(),before)

    def test_related_change_after_preview_day_generation_and_pilot_revision_reject(self):
        self.enable()
        preview=self.prepare()
        with app.connect() as db:db.execute("UPDATE accounts SET note='后续人工备注' WHERE id='old-three'")
        before=self.business()
        with self.assertRaises(operations.ConflictError):self.apply(preview)
        self.assertEqual(self.business(),before)
        for key,value in (('day','2026-10-27'),('generation','old'),('pilot_revision',999)):
            preview=self.prepare()
            with app.connect() as db:
                row=db.execute('SELECT payload FROM operations_previews WHERE id=?',(preview['preview_id'],)).fetchone()
                payload=json.loads(row[0]);payload[key]=value
                db.execute('UPDATE operations_previews SET payload=? WHERE id=?',(operations.encoded(payload),preview['preview_id']))
            with self.assertRaises(operations.ConflictError):self.apply(preview)

    def test_expiry_actor_binding_selection_and_extra_plan_injection_reject(self):
        self.enable()
        preview=self.prepare()
        for selected in (['invalid'],[preview['rows'][0]['row_key']]*2):
            with self.assertRaises(ValueError):self.apply(preview,selected=selected)
        with self.assertRaises(ValueError):
            self.send('apply',dict(preview_id=preview['preview_id'],selected_rows=[preview['rows'][0]['row_key']],confirmation=preview['confirmation'],changes={'note':'不可注入'}))
        with app.connect() as db:db.execute('UPDATE operations_previews SET expires_at=0 WHERE id=?',(preview['preview_id'],))
        with self.assertRaises(ValueError):self.apply(preview)

    def test_all_conflict_batch_can_preserve_receipt_and_review_without_business_write(self):
        self.enable()
        preview=self.prepare(accounts=[support.account_row('synthetic_pending')])
        self.assertEqual(preview['summary']['safe_accounts'],0)
        before=self.business()
        result=self.apply(preview)
        self.assertEqual(result['summary']['applied_accounts'],0)
        self.assertEqual(len(result['receipt']['issues']),1)
        after=self.business()
        for key in ('accounts','sources','scopes','protected','history'):self.assertEqual(after[key],before[key])
        receipt=self.read('/api/table-sync/batch',{'office_id':['three'],'id':[result['batch_id']]})
        self.assertEqual(receipt['issues'][0]['state'],'open')

    def test_summary_counts_actual_source_rows_and_unchanged_fields(self):
        self.enable()
        self.apply(self.prepare())
        preview=self.prepare(progress=[support.progress_row(labels=('2 名字一','3 名字二'))])
        self.assertEqual(preview['summary']['source_rows'],2)
        self.assertEqual(preview['summary']['preview_rows'],3)
        self.assertGreater(preview['summary']['unchanged_fields'],0)

    def test_two_files_same_round_and_safety_limits_required(self):
        self.enable()
        for field in ('accounts_file','progress_file','source_round'):
            body=self.source();body.pop(field)
            with self.assertRaises(ValueError):self.send('preview',body)
        body=self.source();body['accounts_file']['content']='!bad'
        with self.assertRaises(ValueError):self.send('preview',body)
        body=self.source();body['accounts_file']=dict(filename='large.csv',content=base64.b64encode(b'x'*(8*1024*1024+1)).decode())
        with self.assertRaises(ValueError):self.send('preview',body)

    def test_disallowed_private_columns_and_raw_files_never_persist(self):
        self.enable()
        marker='SYNTHETIC_PRIVATE_MUST_DISAPPEAR'
        body=self.source()
        body['accounts_file']=support.csv_file([*support.ACCOUNT_HEADERS,'密码','远控'],[[*support.account_row('synthetic_reuse'),marker,marker]])
        preview=self.send('preview',body)
        result=self.apply(preview)
        with app.connect() as db:
            stored='\n'.join(str(tuple(row)) for table in (*sync.TABLES,'operations_previews','operations_history') for row in db.execute('SELECT * FROM '+table))
        self.assertNotIn(marker,stored)
        self.assertNotIn(body['accounts_file']['content'],stored)
        self.assertNotIn('filename',json.dumps(result))

    def test_late_unplanned_mutation_rolls_back_whole_batch_receipt_and_request(self):
        self.enable()
        preview=self.prepare()
        before=self.business()
        real=sync._write_account
        def corrupt(db,plan):
            real(db,plan)
            db.execute("UPDATE phone_devices SET owner='合成模拟错误' WHERE id='office3:1'")
        with patch.object(sync,'_write_account',side_effect=corrupt):
            with self.assertRaises(operations.ConflictError):self.apply(preview)
        self.assertEqual(self.business(),before)
        with app.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM table_sync_batches').fetchone()[0],0)

    def test_source_only_changes_have_light_marker_and_unchanged_account_version(self):
        self.enable()
        self.apply(self.prepare())
        before=self.business()
        row=support.account_row('synthetic_reuse',name='合成正式名称');row[7]='合成新负责人'
        result=self.apply(self.prepare(accounts=[row]))
        after=self.business()
        self.assertEqual(before['accounts'],after['accounts'])
        added=[item for key,item in after['history'].items() if key not in before['history']]
        self.assertEqual(len(added),1)
        self.assertEqual(added[0]['entity'],'account-source:old-three')
        marker=json.loads(added[0]['after_json'])
        self.assertEqual(set(marker),{'account_id','office_ids','revision'})
        self.assertNotIn('合成新负责人',added[0]['after_json'])
        self.assertTrue(result['ok'])

    def test_persistent_review_status_changes_only_review_with_audit(self):
        self.enable()
        result=self.apply(self.prepare(progress=[support.progress_row(labels=('2 合成文字',))]))
        receipt=result['receipt'];issue=receipt['issues'][0]
        before=self.business()
        self.send('review',dict(batch_id=result['batch_id'],issue_id=issue['id'],state='confirmed_no_action',note='合成人工核对无需处理'))
        after=self.business()
        for key in ('accounts','sources','scopes','protected','history'):self.assertEqual(after[key],before[key])
        fetched=self.read('/api/table-sync/batch',{'office_id':['three'],'id':[result['batch_id']]})
        self.assertEqual(fetched['issues'][0]['state'],'confirmed_no_action')
        with self.assertRaises(ValueError):self.send('review',dict(batch_id=result['batch_id'],issue_id=issue['id'],state='corrected',note=''))

    def test_reverse_restores_only_changed_fields_and_preserves_later_unrelated_note(self):
        self.enable()
        before=self.business()
        result=self.apply(self.prepare())
        with app.connect() as db:db.execute("UPDATE accounts SET note='后续备注要保留' WHERE id='old-three'")
        reverse=self.send('rollback-preview',dict(batch_id=result['batch_id']))
        self.assertEqual(reverse['summary']['safe_accounts'],1)
        self.apply(reverse)
        after=self.business()
        expected={**before['accounts']['old-three'],'note':'后续备注要保留'}
        self.assertEqual(after['accounts']['old-three'],expected)
        self.assertEqual(after['sources'],before['sources'])
        self.assertEqual(after['protected'],before['protected'])
        with self.assertRaises(operations.ConflictError):self.send('rollback-preview',dict(batch_id=result['batch_id']))

    def test_reverse_conflicts_with_later_changed_value_or_source_and_never_forces(self):
        self.enable()
        result=self.apply(self.prepare())
        with app.connect() as db:db.execute("UPDATE accounts SET name='后续人工真名' WHERE id='old-three'")
        before=self.business()
        preview=self.send('rollback-preview',dict(batch_id=result['batch_id']))
        self.assertEqual(preview['summary']['safe_accounts'],0)
        self.assertEqual(self.business(),before)
        self.assertIn('后续人工修改',preview['rows'][0]['reason'])

    def test_reverse_new_account_requires_no_dependencies_and_never_removes_history(self):
        self.enable()
        result=self.apply(self.prepare(mode='add',accounts=[support.account_row('new_reverse')]))
        ident=next(change['account_id'] for change in result['receipt']['changes'])
        with app.connect() as db:db.execute('INSERT INTO daily VALUES(?,?,?)',(ident,DAY,1))
        preview=self.send('rollback-preview',dict(batch_id=result['batch_id']))
        self.assertEqual(preview['summary']['safe_accounts'],0)
        with app.connect() as db:db.execute('DELETE FROM daily WHERE account_id=?',(ident,))
        before=self.business()
        reverse=self.send('rollback-preview',dict(batch_id=result['batch_id']))
        self.assertEqual(reverse['summary']['safe_accounts'],1)
        self.apply(reverse)
        after=self.business()
        self.assertNotIn(ident,after['accounts'])
        for key,value in before['history'].items():self.assertEqual(after['history'][key],value)

    def test_preview_mode_cannot_be_sent_to_wrong_apply_endpoint(self):
        self.enable()
        preview=self.prepare()
        with self.assertRaises(ValueError):
            self.send('rollback-apply',dict(preview_id=preview['preview_id'],selected_rows=[preview['rows'][0]['row_key']],confirmation=preview['confirmation']))

    def test_sources_scoped_and_readable_when_paused_receipts_remain_owner_only(self):
        self.enable()
        self.apply(self.prepare())
        with app.connect() as db:
            begin_write(db)
            office_scope.grant_user(db,self.actor,self.employee_id,['three'])
            employee=auth.public_user(db.execute('SELECT * FROM auth_users WHERE id=?',(self.employee_id,)).fetchone())
        result=self.read('/api/table-sync/account-sources',actor=employee)
        self.assertEqual([row['account_id'] for row in result['sources']],['old-three'])
        self.send('pilot',dict(state='paused',expected_revision=1,confirmation='暂停办公室三试点'))
        self.assertEqual(self.read('/api/table-sync/account-sources',actor=employee)['sources'],result['sources'])
        with self.assertRaises(auth.AccessError):self.read('/api/table-sync/batch',actor=employee)
        with app.connect() as db:
            begin_write(db)
            office_scope.grant_user(db,self.actor,self.employee_id,[])
        with self.assertRaises(auth.AccessError):self.read('/api/table-sync/account-sources',actor=employee)

    def test_v4_v5_v6_remain_compatible_and_pg_migrate_never_runs(self):
        versions=pg_schema.expected_versions()
        self.assertEqual(len(versions),7)
        for version in range(4,8):self.assertTrue(pg_schema.runtime_versions_supported(versions[:version]))
        self.assertFalse(pg_schema.runtime_versions_supported(versions[:3]))
        fake=type('Pg',(),{'dialect':'postgresql'})()
        with self.assertRaises(ValueError):sync.migrate(fake)

    def test_sources_query_count_is_constant_and_foreign_sources_never_escape(self):
        self.enable()
        self.apply(self.prepare())
        def measured():
            statements=[]
            with app.connect() as db:
                db.set_trace_callback(statements.append)
                result=sync.get(db,self.actor,'/api/table-sync/account-sources',{'office_id':['three']},DAY)
            queries=[sql for sql in statements if sql.lstrip().upper().startswith('SELECT')]
            return len(queries),result
        initial,_=measured()
        with app.connect() as db:
            for index in range(20):
                ident='bulk-source-'+str(index)
                self.insert_account(db,ident,'bulk_'+str(index),'合成资料','office3:1',index+10,'three')
                db.execute('INSERT INTO table_sync_account_sources(account_id,office_id,revision) VALUES(?,?,?)',(ident,'three',1))
            # Deliberately inconsistent metadata must not grant access to a
            # foreign account even when its source row says this office.
            db.execute("INSERT INTO table_sync_account_sources(account_id,office_id,revision) VALUES('synth-account-1','three',1)")
        expanded,result=measured()
        self.assertEqual(expanded,initial)
        self.assertEqual(len(result['sources']),21)
        self.assertNotIn('synth-account-1',{row['account_id'] for row in result['sources']})

    def test_preview_batch_guard_hashes_protected_state_constant_times_and_detects_trigger(self):
        self.enable()
        with app.connect() as db:
            for index in range(20):
                self.insert_account(db,'many-'+str(index),'many_'+str(index),'合成批量原名','office3:1',index+10,'three')
        counts=[]
        real=sync._snapshot
        def measured(db):
            counts.append(1)
            return real(db)
        with patch.object(sync,'_snapshot',side_effect=measured):
            preview=self.prepare(accounts=[support.account_row('many_'+str(index),row=index+2) for index in range(20)])
        self.assertEqual(preview['summary']['safe_accounts'],20)
        self.assertLessEqual(len(counts),4)
        with app.connect() as db:
            db.execute("CREATE TRIGGER synthetic_sync_changes_device AFTER UPDATE ON accounts BEGIN UPDATE phone_devices SET owner='合成意外副作用' WHERE id='office3:1'; END")
        before=self.business()
        preview=self.prepare()
        self.assertEqual(preview['summary']['safe_accounts'],0)
        self.assertEqual(preview['rows'][0]['issues'][0]['code'],'batch_validation_conflict')
        self.assertEqual(self.business(),before)

    def test_child_registration_phone_change_cannot_break_existing_parent_pair(self):
        self.enable()
        with app.connect() as db:
            self.insert_account(db,'paired-parent','synthetic_parent','合成关联大号','office3:1',90,'three')
            db.execute("UPDATE accounts SET account_type='大号',registration_phone='19900000101',auxiliary_state='已注册' WHERE id='paired-parent'")
            db.execute("UPDATE accounts SET account_type='小号',parent_account_id='paired-parent' WHERE id='old-three'")
        before=self.business()
        preview=self.prepare()
        self.assertEqual(preview['summary']['safe_accounts'],0)
        self.assertIn('注册手机号不一致',preview['rows'][0]['reason'])
        self.assertEqual(self.business(),before)

    def test_main_registration_phone_change_cannot_diverge_from_existing_child(self):
        self.enable()
        with app.connect() as db:
            self.insert_account(db,'paired-child','synthetic_child','合成关联小号','office3:1',90,'three')
            db.execute("UPDATE accounts SET account_type='大号',auxiliary_state='已注册' WHERE id='old-three'")
            db.execute("UPDATE accounts SET account_type='小号',registration_phone='19900000101',parent_account_id='old-three' WHERE id='paired-child'")
        before=self.business()
        preview=self.prepare()
        self.assertEqual(preview['summary']['safe_accounts'],0)
        self.assertIn('注册手机号不一致',preview['rows'][0]['reason'])
        self.assertEqual(self.business(),before)

    def test_phone_matching_cannot_implicitly_discover_and_save_a_parent(self):
        self.enable()
        with app.connect() as db:
            self.insert_account(db,'candidate-parent','synthetic_parent','合成可能大号','office3:1',90,'three')
            db.execute("UPDATE accounts SET account_type='大号',registration_phone='19900000102' WHERE id='candidate-parent'")
            db.execute("UPDATE accounts SET account_type='小号',parent_account_id=NULL WHERE id='old-three'")
        before=self.business()
        preview=self.prepare()
        self.assertEqual(preview['summary']['safe_accounts'],0)
        self.assertIn('隐式改变大小号关系',preview['rows'][0]['reason'])
        self.assertEqual(self.business(),before)

    def test_reverse_phone_validation_keeps_parent_relation_added_after_the_batch(self):
        self.enable()
        result=self.apply(self.prepare())
        with app.connect() as db:
            self.insert_account(db,'later-parent','synthetic_later_parent','合成后续大号','office3:1',90,'three')
            db.execute("UPDATE accounts SET account_type='大号',registration_phone='19900000102' WHERE id='later-parent'")
            db.execute("UPDATE accounts SET account_type='小号',parent_account_id='later-parent' WHERE id='old-three'")
        before=self.business()
        reverse=self.send('rollback-preview',dict(batch_id=result['batch_id']))
        self.assertEqual(reverse['summary']['safe_accounts'],0)
        self.assertIn('注册手机号不一致',reverse['rows'][0]['reason'])
        self.assertEqual(self.business(),before)


if __name__=='__main__':unittest.main()
