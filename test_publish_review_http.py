"""Authenticated loopback review routes; synthetic fixtures only."""
import gzip
import json
import threading
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch
from uuid import uuid4
from test_isolation import checked_root
checked_root()
import app
import auth
import publish_review
import test_auth_support


class ReviewHTTPTest(unittest.TestCase):
    def setUp(self):
        folder=checked_root()/('review-http-'+uuid4().hex);folder.mkdir()
        self.dbpatch=patch.object(app,'DB',folder/'board.db');self.dbpatch.start();self.addCleanup(self.dbpatch.stop)
        app.initialize()
        class Quiet(app.Handler):
            def log_message(self,*args):pass
        self.server=app.ThreadingHTTPServer(('127.0.0.1',0),Quiet)
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start();self.addCleanup(self.stop)
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.owner=test_auth_support.owner_client(app,self.base)

    def stop(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)

    def call(self,path,body=None,client=None,headers=None):
        request=urllib.request.Request(self.base+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json',**(headers or {})})
        with (client or self.owner).open(request,timeout=10) as response:
            raw=response.read()
            if response.headers.get('Content-Encoding')=='gzip':raw=gzip.decompress(raw)
            return response.status,dict(response.headers),json.loads(raw)

    def test_authentication_csrf_and_read_only_permissions(self):
        with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(self.base+'/api/publish-review')
        self.assertEqual(error.exception.code,401)
        with app.connect() as db:
            owner=auth.public_user(db.execute("SELECT * FROM auth_users WHERE role='owner'").fetchone())
            auth.save_user(db,owner,dict(username='synthetic_viewer',name='合成只读',role='viewer',password=test_auth_support.TEST_PASSWORD,permissions=[]))
        viewer=test_auth_support.client(self.base,'synthetic_viewer')
        data=self.call('/api/publish-review',client=viewer)[2];self.assertFalse(data['can_manage'])
        with self.assertRaises(urllib.error.HTTPError) as error:self.call('/api/publish-review/task',{'account_id':'synth-account-1'},client=viewer)
        self.assertEqual(error.exception.code,403)
        with self.assertRaises(urllib.error.HTTPError) as error:self.call('/api/publish-review/task',{'account_id':'synth-account-1'},headers={'X-CSRF-Token':'synthetic-invalid'})
        self.assertEqual(error.exception.code,403)

    def test_gzip_no_store_minimal_payload_and_no_board_versions(self):
        status,headers,data=self.call('/api/publish-review',headers={'Accept-Encoding':'gzip'})
        self.assertEqual((status,headers['Content-Encoding'],headers['Cache-Control']),(200,'gzip','no-store'))
        self.assertIn('Accept-Encoding',headers['Vary']);self.assertEqual(data['total'],1)
        self.assertNotIn('phone_cards',data);self.assertNotIn('nurture',data);self.assertNotIn('record_versions',data)
        self.assertEqual(data['rows'][0]['state'],'review')

    def test_target_save_replay_conflict_and_daily_flags_independence(self):
        day=app.today();body=dict(day=day,account_id='synth-account-1',target=9,task_state='active',computer_id='synth-pc-1',reason='合成按日目标',expected_revision=0,request_id=str(uuid4()))
        first=self.call('/api/publish-review/task',body)[2];second=self.call('/api/publish-review/task',body)[2]
        self.assertEqual(first,second);self.assertEqual(first['revision'],1)
        with self.assertRaises(urllib.error.HTTPError) as error:self.call('/api/publish-review/task',{**body,'request_id':str(uuid4()),'target':11})
        self.assertEqual(error.exception.code,409)
        with app.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM daily').fetchone()[0],0)

    def test_external_samples_require_actual_time_and_never_trigger_collection(self):
        with app.connect() as db:identity=db.execute("SELECT douyin_id FROM accounts WHERE id='synth-account-1'").fetchone()[0]
        body=dict(account_id='synth-account-1',matched_douyin_id=identity,total=10,sampled_at=publish_review.stamp(),request_id=str(uuid4()))
        with patch.object(app.collection,'enqueue',side_effect=AssertionError('must not collect')):
            first=self.call('/api/publish-review/sample',body)[2];second=self.call('/api/publish-review/sample',body)[2]
        self.assertEqual(first,second)
        with app.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM publish_review_samples').fetchone()[0],1)
        with self.assertRaises(urllib.error.HTTPError) as error:self.call('/api/publish-review/sample',{**body,'request_id':str(uuid4()),'sampled_at':'2026-11-02T10:00:00'})
        self.assertEqual(error.exception.code,400)

    def test_invalid_dates_and_unmigrated_pg_are_not_empty_results(self):
        with self.assertRaises(urllib.error.HTTPError) as error:self.call('/api/publish-review?date=invalid')
        self.assertEqual(error.exception.code,400)
        with patch.object(app.publish_review,'available',return_value=False):
            with self.assertRaises(urllib.error.HTTPError) as error:self.call('/api/publish-review')
        self.assertEqual(error.exception.code,503)
        self.assertEqual(json.load(error.exception)['code'],'PUBLISH_REVIEW_SCHEMA_REQUIRED')
