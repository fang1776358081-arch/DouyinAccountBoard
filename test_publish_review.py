"""Only invented data in the explicit isolated runner; no customer files/DB."""
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import patch
from uuid import uuid4
from test_isolation import checked_root
checked_root()
import app
import auth
import collection
import operations
import publish_review as review


class ScopedConnection(sqlite3.Connection):
    scope_enabled=True


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.folder=checked_root()/('publish-review-'+uuid4().hex);self.folder.mkdir()
        self.dbpatch=patch.object(app,'DB',self.folder/'board.db');self.dbpatch.start();self.addCleanup(self.dbpatch.stop)
        self.clock=patch.object(review,'now',return_value=datetime(2026,11,2,12,tzinfo=review.BEIJING));self.clock.start();self.addCleanup(self.clock.stop)
        app.initialize()
        self.db=sqlite3.connect(app.DB,factory=ScopedConnection);self.db.row_factory=sqlite3.Row;self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute("INSERT INTO accounts(id,computer_id,slot,name,status,note,douyin_id,homepage) VALUES('synth-account-2','synth-pc-1',2,'合成第二账号','正常','','synthetic_002','')")
        self.db.commit()
        self.db.executescript('''ALTER TABLE auth_users ADD COLUMN authorization_version INTEGER NOT NULL DEFAULT 0;
          CREATE TABLE offices(id TEXT PRIMARY KEY,name TEXT,active INTEGER DEFAULT 1);
          CREATE TABLE user_office_grants(user_id INTEGER,office_id TEXT,PRIMARY KEY(user_id,office_id));
          CREATE TABLE account_offices(account_id TEXT PRIMARY KEY,office_id TEXT);
          CREATE TABLE computer_offices(computer_id TEXT PRIMARY KEY,office_id TEXT);
          INSERT INTO offices VALUES('office-a','合成办公室甲',1),('office-b','合成办公室乙',1);
          INSERT INTO account_offices SELECT id,'office-a' FROM accounts;
          INSERT INTO computer_offices SELECT id,'office-a' FROM computers;
          INSERT INTO auth_users(username,name,role,password_hash,permissions,active,created_at) VALUES('synthetic-owner','合成管理员','owner','not-a-real-password-hash','[]',1,1);
          INSERT INTO auth_users(username,name,role,password_hash,permissions,active,created_at) VALUES('synthetic-employee','合成员工','employee','not-a-real-password-hash','["publishing_manage","collection_manage"]',1,1);
          INSERT INTO user_office_grants SELECT id,'office-a' FROM auth_users WHERE role='employee';''')
        self.owner=auth.public_user(self.db.execute("SELECT * FROM auth_users WHERE role='owner'").fetchone());self.owner['csrf']='synthetic-csrf'
        self.employee=auth.public_user(self.db.execute("SELECT * FROM auth_users WHERE role='employee'").fetchone());self.employee['csrf']='synthetic-csrf'
        self.db.commit();self.db.execute('BEGIN IMMEDIATE')
        self.addCleanup(self.db.close)

    def sample(self,day,total,at='08:00:00',ident='synth-account-1',failed=False,sample_id=None):
        return review.ingest(self.db,ident,sample_id or str(uuid4()),day+'T'+at+'+08:00',total,failed=failed)

    def task(self,target=5,ident='synth-account-1',day='2026-11-02',**extra):
        return review.save_task(self.db,self.owner,dict(account_id=ident,day=day,target=target,task_state='active',computer_id='synth-pc-1',reason='合成任务安排依据',expected_revision=0,**extra))

    def view(self,actor=None,**query):
        return review.overview(self.db,actor or self.owner,dict(date='2026-11-02',**query))

    def row(self,**query):
        return next(row for row in self.view(**query)['rows'] if row['account_id']=='synth-account-1')

    def test_achieved_and_over_target_progress_is_capped(self):
        self.task(5);self.sample('2026-11-01',100);self.sample('2026-11-02',109)
        row=self.row();self.assertEqual((row['net'],row['remaining'],row['progress'],row['state']),(9,0,100,'met'))

    def test_partial_zero_and_different_targets_are_not_hardcoded(self):
        self.task(17);self.sample('2026-11-01',100);self.sample('2026-11-02',104)
        self.assertEqual((self.row()['net'],self.row()['remaining']),(4,13))
        self.task(2,ident='synth-account-2');self.sample('2026-11-01',50,ident='synth-account-2');self.sample('2026-11-02',50,ident='synth-account-2')
        other=next(r for r in self.view()['rows'] if r['account_id']=='synth-account-2')
        self.assertEqual((other['net'],other['state'],other['remaining']),(0,'pending',2))

    def test_missing_yesterday_never_becomes_zero(self):
        self.task();self.sample('2026-11-02',20)
        row=self.row();self.assertIsNone(row['net']);self.assertEqual(row['state'],'review');self.assertIn('缺少昨日收口基线',row['reasons'])

    def test_negative_difference_is_retained(self):
        self.task();self.sample('2026-11-01',100);self.sample('2026-11-02',97)
        row=self.row();self.assertEqual((row['net'],row['state']),(-3,'review'));self.assertIsNone(row['progress'])

    def test_latest_failure_takes_priority_over_prior_success(self):
        self.task(1);self.sample('2026-11-01',10);self.sample('2026-11-02',15);self.sample('2026-11-02',None,at='09:00:00',failed=True)
        row=self.row();self.assertEqual((row['net'],row['state']),(5,'review'));self.assertIn('本日最近一次采样失败',row['reasons'])

    def test_retained_legacy_failure_after_success_is_not_reported_as_met(self):
        self.task(1);self.sample('2026-11-01',10);self.sample('2026-11-02',15)
        self.db.execute("UPDATE accounts SET collection_state='采集失败',last_attempted='2026-11-02T10:00:00+08:00' WHERE id='synth-account-1'")
        row=self.row();self.assertEqual(row['state'],'review');self.assertIn('本日最近一次采样失败',row['reasons'])

    def test_previous_day_samples_are_expired_not_current_success(self):
        self.task();self.sample('2026-11-01',20)
        row=self.row();self.assertEqual(row['state'],'review');self.assertTrue(any('过期' in r for r in row['reasons']))

    def test_failed_first_attempt_does_not_turn_yesterday_into_zero_today(self):
        self.task();self.sample('2026-11-01',20);self.sample('2026-11-02',None,failed=True)
        row=self.row();self.assertEqual(row['baseline_total'],20);self.assertEqual(row['state'],'review');self.assertIsNone(row['net'])

    def test_new_account_and_missing_target_are_explicit(self):
        self.db.execute("UPDATE accounts SET registered_on='2026-11-02' WHERE id='synth-account-1'")
        self.task(None);self.sample('2026-11-02',0)
        row=self.row();self.assertIn('已安排任务，但当日目标尚未设置',row['reasons']);self.assertIn('新账号缺少昨日基线',row['reasons'])

    def test_zero_baseline_is_valid_but_identity_changes_require_verification(self):
        self.task(0);self.sample('2026-11-01',0);self.sample('2026-11-02',0)
        self.assertEqual((self.row()['net'],self.row()['state']),(0,'met'))
        self.db.execute("UPDATE accounts SET douyin_id='synthetic-changed-identity' WHERE id='synth-account-1'")
        self.assertEqual(self.row()['state'],'review')
        self.sample('2026-11-02',5,at='09:00:00')
        self.assertEqual(self.row()['state'],'review');self.assertTrue(any('标识发生变化' in why for why in self.row()['reasons']))

    def test_unconfirmed_computer_can_be_recorded_without_guessing_a_binding(self):
        body=dict(account_id='synth-account-1',day='2026-11-02',target=5,task_state='none',computer_id='',reason='合成无任务',expected_revision=0)
        review.save_task(self.db,self.owner,body);self.assertEqual(self.row()['state'],'no_task')
        review.save_task(self.db,self.owner,{**body,'task_state':'active','expected_revision':1})
        self.assertEqual(self.row()['state'],'review');self.assertIn('当日任务电脑尚未确认',self.row()['reasons'])

    def test_unknown_tasks_are_not_silently_assumed_or_no_task(self):
        row=self.row();self.assertEqual(row['state'],'review');self.assertIsNone(row['task_state'])

    def test_paused_and_explicit_no_task_are_excluded(self):
        for ident,state in [('synth-account-1','paused'),('synth-account-2','none')]:
            body=dict(account_id=ident,day='2026-11-02',target=None,task_state=state,computer_id='synth-pc-1',reason='合成无需执行',expected_revision=0)
            review.save_task(self.db,self.owner,body)
        summary=self.view()['summary'];self.assertEqual((summary['pending'],summary['review'],summary['paused'],summary['no_task']),(0,0,1,1))

    def test_deadline_before_and_after_is_distinguished(self):
        self.task(5,deadline='13:00');self.sample('2026-11-01',10);self.sample('2026-11-02',11)
        self.assertEqual(self.row()['label'],'进行中')
        with patch.object(review,'now',return_value=datetime(2026,11,2,14,tzinfo=review.BEIJING)):
            self.assertEqual(self.row()['label'],'未达标')

    def test_baseline_first_seal_is_immutable_and_reads_are_read_only(self):
        self.task();self.sample('2026-11-01',100,at='10:00:00');self.sample('2026-11-01',103,at='23:00:00');self.sample('2026-11-02',105)
        original=dict(self.db.execute('SELECT * FROM publish_review_baselines').fetchone())
        self.sample('2026-11-01',104,at='23:50:00');self.sample('2026-11-02',109,at='10:00:00')
        before=self.db.total_changes
        for _ in range(3):self.view()
        self.assertEqual(self.db.total_changes,before);self.assertEqual(dict(self.db.execute('SELECT * FROM publish_review_baselines').fetchone()),original)
        self.assertEqual((self.row()['baseline_total'],self.row()['net']),(103,6))

    def test_duplicate_sample_is_not_added_and_conflict_is_rejected(self):
        self.sample('2026-11-01',10);self.sample('2026-11-02',13,sample_id='synthetic-fixed')
        self.assertTrue(self.sample('2026-11-02',13,sample_id='synthetic-fixed')['duplicate'])
        with self.assertRaises(operations.ConflictError):self.sample('2026-11-02',14,sample_id='synthetic-fixed')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM publish_review_samples').fetchone()[0],2)

    def test_actual_timezone_and_missing_timestamp_validation(self):
        review.ingest(self.db,'synth-account-1','utc-boundary','2026-11-01T16:01:00+00:00',7)
        self.assertEqual(self.db.execute('SELECT day FROM publish_review_samples').fetchone()[0],'2026-11-02')
        for value in ['2026-11-02T10:00:00','2026-11-03T10:00:00+08:00','invalid']:
            with self.assertRaises(ValueError):review.ingest(self.db,'synth-account-1',str(uuid4()),value,7)

    def test_history_freezes_target_computer_owner_and_excludes_new_unplanned_accounts(self):
        self.task(3);self.sample('2026-11-01',10);self.sample('2026-11-02',14)
        name=self.row()['computer_name'];owner=self.row()['operator']
        with patch.object(review,'now',return_value=datetime(2026,11,3,12,tzinfo=review.BEIJING)):
            self.db.execute("UPDATE computers SET name='合成改名',operator='合成新负责人'")
            historical=self.view();self.assertEqual(historical['total'],1)
            row=historical['rows'][0];self.assertEqual((row['computer_name'],row['operator'],row['target']),(name,owner,3))
            with self.assertRaisesRegex(ValueError,'历史'):self.task(20)
            self.task(75,day='2026-11-03')
            self.assertEqual(self.view()['rows'][0]['target'],3)

    def test_office_employee_aggregation_and_transfers_fail_closed(self):
        self.task(1);self.sample('2026-11-01',10);self.sample('2026-11-02',11)
        self.db.execute("UPDATE account_offices SET office_id='office-b' WHERE account_id='synth-account-2'")
        response=self.view(self.employee);self.assertEqual(response['total'],1);self.assertEqual(sum(response['summary'].values()),1)
        self.assertEqual(response['user']['csrf'],'synthetic-csrf')
        with self.assertRaises(auth.AccessError):review.authorize(self.db,self.employee,{'account_id':'synth-account-2'},'collection_manage')
        self.db.execute("UPDATE account_offices SET office_id='office-b' WHERE account_id='synth-account-1'")
        self.assertEqual(self.view(self.employee)['total'],0)
        self.db.execute("UPDATE user_office_grants SET office_id='office-b' WHERE user_id=?",(self.employee['id'],))
        self.assertNotIn('synth-account-1',[r['account_id'] for r in self.view(self.employee)['rows']])

    def test_foreign_computer_metadata_is_not_exposed_through_an_authorized_account(self):
        self.db.execute("UPDATE computer_offices SET office_id='office-b'")
        self.db.execute("UPDATE computers SET name='合成未授权电脑',operator='合成未授权负责人'")
        data=self.view(self.employee);row=next(r for r in data['rows'] if r['account_id']=='synth-account-1')
        self.assertEqual((row['computer_id'],row['operator']),('',''))
        self.assertNotIn('合成未授权',json.dumps(data,ensure_ascii=False))

    def test_guessed_revision_cannot_reassign_a_former_offices_plan(self):
        self.task(2)
        self.db.execute("UPDATE account_offices SET office_id='office-b' WHERE account_id='synth-account-1'")
        self.db.execute("UPDATE computer_offices SET office_id='office-b' WHERE computer_id='synth-pc-1'")
        self.db.execute("UPDATE user_office_grants SET office_id='office-b' WHERE user_id=?",(self.employee['id'],))
        body=dict(account_id='synth-account-1',day='2026-11-02',target=10,task_state='active',computer_id='synth-pc-1',reason='合成试图覆盖旧办公室',expected_revision=1,request_id=str(uuid4()))
        with self.assertRaises(auth.AccessError):review.post(self.db,self.employee,'/api/publish-review/task',body)
        self.assertEqual(self.db.execute('SELECT office_id,target FROM publish_review_tasks').fetchone()['office_id'],'office-a')

    def test_summary_computers_rows_and_state_filters_agree(self):
        self.task(1);self.sample('2026-11-01',10);self.sample('2026-11-02',11)
        data=self.view();self.assertEqual(sum(data['summary'].values()),data['total']);self.assertEqual(sum(c['accounts'] for c in data['computers']),data['total'])
        for state in ('pending','review'):
            filtered=self.view(state=state);self.assertEqual(filtered['total'],data['summary'][state]);self.assertTrue(all(r['state']==state for r in filtered['rows']))
        self.assertEqual(self.view(computer='synthetic-missing')['total'],0)

    def test_target_write_permissions_revision_and_idempotency(self):
        body=dict(account_id='synth-account-1',day='2026-11-02',target=3,task_state='active',computer_id='synth-pc-1',reason='合成目标',expected_revision=0,request_id=str(uuid4()))
        result=review.post(self.db,self.owner,'/api/publish-review/task',body)
        self.assertEqual(review.post(self.db,self.owner,'/api/publish-review/task',body),result)
        self.assertEqual(self.db.execute('SELECT revision FROM publish_review_tasks').fetchone()[0],1)
        with self.assertRaises(operations.ConflictError):review.post(self.db,self.owner,'/api/publish-review/task',{**body,'target':8})
        with self.assertRaises(operations.ConflictError):review.post(self.db,self.owner,'/api/publish-review/task',{**body,'request_id':str(uuid4())})
        self.db.execute("UPDATE auth_users SET permissions='[]' WHERE id=?",(self.employee['id'],))
        with self.assertRaises(auth.AccessError):review.post(self.db,self.employee,'/api/publish-review/task',{**body,'request_id':str(uuid4())})

    def test_legacy_latest_values_do_not_fabricate_baselines_or_history(self):
        self.db.execute("UPDATE accounts SET work_count=100,last_collected='2026-11-02T08:00:00+08:00'")
        self.assertEqual(self.row()['legacy_total'],100);self.assertIsNone(self.row()['net'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM publish_review_samples').fetchone()[0],0)

    def test_ordinary_daily_publish_permission_does_not_grant_target_management(self):
        self.db.execute("UPDATE auth_users SET permissions='[\"publish\"]' WHERE id=?",(self.employee['id'],))
        body=dict(account_id='synth-account-1',day='2026-11-02',target=1,task_state='active',computer_id='synth-pc-1',reason='合成普通员工修改目标',expected_revision=0,request_id=str(uuid4()))
        with self.assertRaises(auth.AccessError):review.post(self.db,self.employee,'/api/publish-review/task',body)
        self.assertFalse(self.view(self.employee)['can_manage'])

    def test_large_list_is_server_paginated_with_bounded_statement_count(self):
        self.db.executemany("INSERT INTO accounts(id,computer_id,slot,name,status) VALUES(?,'synth-pc-1',?,'合成大列表','正常')", [('synthetic-bulk-'+str(i),100+i) for i in range(2000)])
        self.db.execute("INSERT INTO account_offices SELECT id,'office-a' FROM accounts WHERE id LIKE 'synthetic-bulk-%'")
        queries=[];self.db.set_trace_callback(queries.append);started=time.perf_counter();result=self.view(page='500');elapsed=(time.perf_counter()-started)*1000;self.db.set_trace_callback(None)
        self.assertEqual((result['total'],result['page'],len(result['rows'])),(2002,41,2));self.assertLess(len(queries),15)
        first_statements=len(queries);first=self.view();first_bytes=len(json.dumps(first).encode())
        self.assertEqual(len(first['rows']),50);self.assertLess(first_bytes,100000)
        self.assertFalse(any('phone_cards' in q or 'nurture' in q or 'operations_history' in q for q in queries))
        print(json.dumps(dict(kind='sqlite-synthetic-only',accounts=2002,statements=first_statements,first_page_rows=50,first_page_bytes=first_bytes,last_page_bytes=len(json.dumps(result).encode()),elapsed_ms=round(elapsed,2))))

    def test_collection_time_hook_keeps_tokens_private_and_old_workers_compatible(self):
        job={'account_id':'synth-account-1','token':'synthetic-private-job-token'}
        collection.retain_sample(self.db,job,{},20,False)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM publish_review_samples').fetchone()[0],0)
        collection.retain_sample(self.db,job,{'sampled_at':'2026-11-02T08:00:00+08:00'},20,False)
        record=dict(self.db.execute('SELECT * FROM publish_review_samples').fetchone());self.assertNotIn(job['token'],json.dumps(record));self.assertEqual(record['total'],20)
