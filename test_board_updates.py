import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from uuid import uuid4
from http.server import ThreadingHTTPServer
import app
import auth
import backup
import board_updates
import operations
import test_auth_support
import test_isolation


def _verification_seed():
    # Synthetic stand-in for the counts and ids the existing board tests already check.
    computers=[dict(id=ident,name='合成电脑'+ident,shadow_account='',ip='',operator='',category='') for ident in [str(i) for i in range(1,10)]+['11']]
    placed={(1,1):'43',(2,1):'39',(3,1):'50'}
    accounts=[]
    for computer in range(1,10):
        for slot in range(1,4):
            ident=placed.get((computer,slot),'s%d-%d'%(computer,slot))
            accounts.append(dict(id=ident,computer_id=str(computer),slot=slot,name='合成账号'+ident,status='正常',note='',douyin_id='synthetic_'+ident,homepage='',work_count=None,last_collected=None))
    return dict(computers=computers,accounts=accounts)


_VERIFY_ROOT=test_isolation.create_root(test_isolation.PROJECT/'.synthetic-tests'/('board-updates-verify-'+uuid4().hex))
os.environ['DOUYIN_BOARD_TEST_ROOT']=str(_VERIFY_ROOT)
tempfile.tempdir=str(_VERIFY_ROOT)
test_isolation.synthetic_seed=_verification_seed


class BoardUpdatesTest(unittest.TestCase):
    def setUp(self):
        self.root=test_isolation.create_root(test_isolation.PROJECT/'.synthetic-tests'/('board-updates-'+uuid4().hex))
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.old_root=os.environ.get('DOUYIN_BOARD_TEST_ROOT')
        os.environ['DOUYIN_BOARD_TEST_ROOT']=str(self.root)
        self.addCleanup(self._restore_root)
        self.old_db=app.DB;self.old_worker=app.WORKER_TOKEN
        app.DB=self.root/'board.db';app.WORKER_TOKEN='test-local-worker'
        self.addCleanup(self._restore_app)
        app.initialize()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)

    def _restore_app(self):
        app.DB=self.old_db;app.WORKER_TOKEN=self.old_worker

    def _restore_root(self):
        if self.old_root is None:os.environ.pop('DOUYIN_BOARD_TEST_ROOT',None)
        else:os.environ['DOUYIN_BOARD_TEST_ROOT']=self.old_root

    def test_updates_require_login(self):
        request=urllib.request.Request(self.base+'/api/board-updates')
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(error.exception.code,401)
        raw=error.exception.read()
        payload=json.loads(raw.decode())
        for key in ('accounts','computers','nurture'):
            self.assertNotIn(key,payload)
        text=raw.decode()
        for word in ('账号','电脑','养号'):
            self.assertNotIn(word,text)
        client=test_auth_support.owner_client(app,self.base)
        with client.open(self.base+'/api/board',timeout=5) as response:
            self.assertEqual(response.status,200)
            body=json.load(response)
        self.assertIn('accounts',body)
        self.assertIn('computers',body)
        self.assertIn('nurture',body)

    def test_unchanged_wait_does_not_open_database(self):
        client=test_auth_support.owner_client(app,self.base)
        with client.open(self.base+'/api/board',timeout=5) as response:
            board=json.load(response)
        version=operations.token(board['record_versions'])
        jar=next(handler.cookiejar for handler in client.handlers if hasattr(handler,'cookiejar'))
        cookie='; '.join('%s=%s'%(item.name,item.value) for item in jar)
        finished=[];connects=[]
        original=app.connect
        def counting_connect():
            connects.append(time.monotonic())
            return original()
        app.connect=counting_connect
        self.addCleanup(lambda: setattr(app,'connect',original))
        def wait_once():
            request=urllib.request.Request(self.base+'/api/board-updates?version='+version,headers={'Cookie':cookie})
            try:
                with urllib.request.urlopen(request,timeout=3) as response:
                    finished.append((response.status,json.load(response)))
            except urllib.error.HTTPError as error:
                finished.append((error.code,json.loads(error.read().decode())))
                error.close()
        threads=[threading.Thread(target=wait_once,daemon=True) for _ in range(50)]
        started=time.monotonic()
        for thread in threads:thread.start()
        while time.monotonic()-started<0.2 and not finished:time.sleep(0.02)
        self.assertEqual(finished,[])
        seen=len(connects)
        time.sleep(0.3)
        self.assertEqual(len(connects),seen)
        self.assertEqual(finished,[])
        for thread in threads:thread.join(3)
        self.assertEqual(len(finished),50)
        for status,payload in finished:
            self.assertEqual(status,200)
            self.assertNotIn('accounts',payload)

    def test_save_delivers_only_changed_account(self):
        client=test_auth_support.owner_client(app,self.base)
        with client.open(self.base+'/api/board',timeout=5) as response:
            board=json.load(response)
        account=next(row for row in board['accounts'] if row['id']=='43')
        version=operations.token(board['record_versions'])
        jar=next(handler.cookiejar for handler in client.handlers if hasattr(handler,'cookiejar'))
        cookie='; '.join('%s=%s'%(item.name,item.value) for item in jar)
        seen=[]
        def spy(actor,update):
            seen.append((actor,update))
            return True
        previous=getattr(board_updates,'recipients',None)
        board_updates.recipients=spy
        self.addCleanup(lambda: setattr(board_updates,'recipients',previous))
        found=[]
        def wait_once():
            request=urllib.request.Request(self.base+'/api/board-updates?version='+version,headers={'Cookie':cookie})
            with urllib.request.urlopen(request,timeout=3) as response:
                found.append((response.status,json.load(response)))
        thread=threading.Thread(target=wait_once,daemon=True)
        thread.start()
        time.sleep(0.2)
        note='按条更新备注'
        request=urllib.request.Request(self.base+'/api/account',data=json.dumps({**account,'note':note}).encode(),headers={'Content-Type':'application/json'})
        with client.open(request,timeout=5) as response:
            self.assertEqual(response.status,200)
        thread.join(3)
        self.assertEqual(len(found),1)
        status,payload=found[0]
        self.assertEqual(status,200)
        self.assertEqual(payload['account']['id'],'43')
        self.assertEqual(payload['account']['note'],note)
        self.assertNotEqual(payload['version'],version)
        self.assertEqual(payload['record_version'],json.load(client.open(self.base+'/api/board',timeout=5))['record_versions']['account:43'])
        for key in ('computers','phone_cards','nurture','accounts'):
            self.assertNotIn(key,payload)
        self.assertTrue(seen)
        self.assertEqual(seen[0][1]['account']['id'],'43')

    def test_other_office_is_omitted(self):
        update={'account':{'id':'only-b','office_ids':['B']},'version':'v2','record_version':'rv'}
        office_a={'role':'employee','office_ids':['A']}
        office_b={'role':'employee','office_ids':['B']}
        owner={'role':'owner'}
        self.assertFalse(board_updates.recipients(office_a,update))
        self.assertTrue(board_updates.recipients(office_b,update))
        self.assertTrue(board_updates.recipients(owner,update))

    def test_office_move_notifies_remove_and_add(self):
        event=getattr(board_updates,'notify_office_move',None)
        self.assertTrue(callable(event))
        calls=[]
        def spy(*args,**kwargs):
            calls.append((args,kwargs))
            return event(*args,**kwargs)
        board_updates.notify_office_move=spy
        self.addCleanup(lambda: setattr(board_updates,'notify_office_move',event))
        client=test_auth_support.owner_client(app,self.base)
        with client.open(self.base+'/api/board',timeout=5) as response:
            board=json.load(response)
        account=next(row for row in board['accounts'] if row['id']=='43')
        version=operations.token(board['record_versions'])
        jar=next(handler.cookiejar for handler in client.handlers if hasattr(handler,'cookiejar'))
        cookie='; '.join('%s=%s'%(item.name,item.value) for item in jar)
        found=[]
        def wait_once():
            request=urllib.request.Request(self.base+'/api/board-updates?version='+version,headers={'Cookie':cookie})
            with urllib.request.urlopen(request,timeout=3) as response:
                found.append((response.status,json.load(response)))
        thread=threading.Thread(target=wait_once,daemon=True)
        thread.start()
        time.sleep(0.2)
        note='无办公室备注'
        request=urllib.request.Request(self.base+'/api/account',data=json.dumps({**account,'note':note}).encode(),headers={'Content-Type':'application/json'})
        with client.open(request,timeout=5) as response:
            self.assertEqual(response.status,200)
        thread.join(3)
        self.assertEqual(len(calls),1)
        self.assertEqual(len(found),1)
        status,payload=found[0]
        self.assertEqual(status,200)
        self.assertEqual(payload['account']['id'],'43')
        self.assertEqual(payload['account']['note'],note)
        self.assertNotIn('removed',payload)
        self.assertNotIn('accounts',payload)
        with app.connect() as db:
            known=operations.token(operations.versions(db))
            db.execute('UPDATE accounts SET note=? WHERE id=?',('办公室迁移','43'))
            board_updates.notify_office_move(db,'43',previous_offices=['A'],next_offices=['B'])
        office_a={'role':'employee','office_ids':['A']}
        office_b={'role':'employee','office_ids':['B']}
        got_a=board_updates.wait(known,office_a,seconds=1)
        got_b=board_updates.wait(known,office_b,seconds=1)
        self.assertEqual(got_a.get('removed'),'43')
        self.assertNotIn('account',got_a)
        self.assertEqual(got_b['account']['id'],'43')
        self.assertEqual(got_b['account']['office_ids'],['B'])
        self.assertNotIn('removed',got_b)
        self.assertEqual(got_a['version'],got_b['version'])
        self.assertNotEqual(got_a['version'],known)

    def test_save_response_includes_changed_record(self):
        client=test_auth_support.owner_client(app,self.base)
        with client.open(self.base+'/api/board',timeout=5) as response:
            board=json.load(response)
        account=next(row for row in board['accounts'] if row['id']=='43')
        stale={'account:43':board['record_versions']['account:43']}
        note='响应里的新备注'
        request=urllib.request.Request(self.base+'/api/account',data=json.dumps({**account,'note':note}).encode(),headers={'Content-Type':'application/json'})
        with client.open(request,timeout=5) as response:
            self.assertEqual(response.status,200)
            saved=json.load(response)
        self.assertIn('account',saved)
        self.assertEqual(saved['account']['id'],'43')
        self.assertEqual(saved['account']['note'],note)
        for key in ('computers','phone_cards','nurture','accounts'):
            self.assertNotIn(key,saved)
        with client.open(self.base+'/api/board',timeout=5) as response:
            current=json.load(response)
        self.assertEqual(saved['account'],next(row for row in current['accounts'] if row['id']=='43'))
        day=current['date']
        request=urllib.request.Request(self.base+'/api/daily',data=json.dumps({'id':'43','date':day,'published':True}).encode(),headers={'Content-Type':'application/json'})
        with client.open(request,timeout=5) as response:
            self.assertEqual(response.status,200)
            published=json.load(response)
        self.assertEqual(published.get('daily'),{'id':'43','date':day,'published':True})
        for key in ('computers','phone_cards','nurture','accounts'):
            self.assertNotIn(key,published)
        conflict=urllib.request.Request(self.base+'/api/account',data=json.dumps({**account,'note':'后一次不写入','_expected_versions':stale}).encode(),headers={'Content-Type':'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as error:
            client.open(conflict,timeout=5)
        self.assertEqual(error.exception.code,409)
        error.exception.close()
        with client.open(self.base+'/api/board',timeout=5) as response:
            after=json.load(response)
        self.assertEqual(next(row for row in after['accounts'] if row['id']=='43')['note'],note)

    def test_restore_tells_waiters_to_reload(self):
        client=test_auth_support.owner_client(app,self.base)
        folder=app.DB.parent.parent/'backups'
        folder.mkdir(parents=True,exist_ok=True)
        target=folder/'manual-20261019.db'
        backup.snapshot(app.DB,target)
        self.addCleanup(lambda: shutil.rmtree(folder,ignore_errors=True))
        request=urllib.request.Request(self.base+'/api/auth/users',data=json.dumps(dict(username='keep_employee',name='keep_employee',role='employee',permissions=auth.DEFAULTS['employee'],password=test_auth_support.TEST_PASSWORD,active=True)).encode(),headers={'Content-Type':'application/json'})
        with client.open(request,timeout=5) as response:
            self.assertEqual(response.status,200)
        request=urllib.request.Request(self.base+'/api/account/status',data=json.dumps(dict(id='43',status='停用')).encode(),headers={'Content-Type':'application/json'})
        with client.open(request,timeout=5) as response:
            self.assertEqual(response.status,200)
        with client.open(self.base+'/api/board',timeout=5) as response:
            version=operations.token(json.load(response)['record_versions'])
        jar=next(handler.cookiejar for handler in client.handlers if hasattr(handler,'cookiejar'))
        cookie='; '.join('%s=%s'%(item.name,item.value) for item in jar)
        found=[]
        def wait_once():
            request=urllib.request.Request(self.base+'/api/board-updates?version='+version,headers={'Cookie':cookie})
            with urllib.request.urlopen(request,timeout=5) as response:
                found.append((response.status,json.load(response)))
        thread=threading.Thread(target=wait_once,daemon=True)
        thread.start()
        time.sleep(0.2)
        request=urllib.request.Request(self.base+'/api/workspace/restore-preview',data=json.dumps(dict(backup_id=target.name)).encode(),headers={'Content-Type':'application/json'})
        with client.open(request,timeout=5) as response:
            preview=json.load(response)
        request=urllib.request.Request(self.base+'/api/workspace/restore-apply',data=json.dumps(dict(preview_id=preview['preview_id'],confirmation='恢复业务数据',password=test_auth_support.TEST_PASSWORD)).encode(),headers={'Content-Type':'application/json'})
        with client.open(request,timeout=5) as response:
            restored=json.load(response)
        thread.join(5)
        self.assertIn('保持当前',restored['note'])
        self.assertEqual(len(found),1)
        status,payload=found[0]
        self.assertEqual(status,200)
        self.assertTrue(payload.get('reload'))
        for key in ('account','accounts','computers','phone_cards','nurture'):
            self.assertNotIn(key,payload)
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM auth_users').fetchone()[0],2)
            self.assertEqual(db.execute("SELECT username FROM auth_users WHERE username='keep_employee'").fetchone()[0],'keep_employee')
            self.assertNotEqual(db.execute("SELECT status FROM accounts WHERE id='43'").fetchone()[0],'停用')
        request=urllib.request.Request(self.base+'/api/workspace/restore-preview',data=json.dumps(dict(backup_id=target.name)).encode(),headers={'Content-Type':'application/json'})
        with client.open(request,timeout=5) as response:
            preview=json.load(response)
        request=urllib.request.Request(self.base+'/api/account/status',data=json.dumps(dict(id='43',status='停用')).encode(),headers={'Content-Type':'application/json'})
        with client.open(request,timeout=5) as response:
            self.assertEqual(response.status,200)
        request=urllib.request.Request(self.base+'/api/workspace/restore-apply',data=json.dumps(dict(preview_id=preview['preview_id'],confirmation='恢复业务数据',password=test_auth_support.TEST_PASSWORD)).encode(),headers={'Content-Type':'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as error:
            client.open(request,timeout=5)
        self.assertEqual(error.exception.code,409)
        error.exception.close()

    def test_import_apply_tells_waiters_to_reload(self):
        import base64
        import csv
        import io
        client=test_auth_support.owner_client(app,self.base)
        with client.open(self.base+'/api/board',timeout=5) as response:
            before=json.load(response)
        original=next(row for row in before['accounts'] if row['id']=='43')['name']
        buffer=io.StringIO()
        csv.writer(buffer).writerows([['内部编号','昵称','抖音号','注册手机号','实际发布位置'],['43','更正昵称','','','不要导入'],['','新账号','new_unique_handle','13800000000','不要导入']])
        content=base64.b64encode(buffer.getvalue().encode('utf-8-sig')).decode()
        request=urllib.request.Request(self.base+'/api/workspace/import-preview',data=json.dumps(dict(kind='accounts',filename='accounts.csv',content=content)).encode(),headers={'Content-Type':'application/json'})
        with client.open(request,timeout=5) as response:
            preview=json.load(response)
        self.assertFalse(preview['rows'][0]['selected'])
        self.assertTrue(preview['rows'][1]['selected'])
        self.assertIn('实际发布位置',preview['ignored_columns'])
        with client.open(self.base+'/api/board',timeout=5) as response:
            board=json.load(response)
        self.assertEqual(next(row for row in board['accounts'] if row['id']=='43')['name'],original)
        version=operations.token(board['record_versions'])
        jar=next(handler.cookiejar for handler in client.handlers if hasattr(handler,'cookiejar'))
        cookie='; '.join('%s=%s'%(item.name,item.value) for item in jar)
        found=[]
        def wait_once():
            request=urllib.request.Request(self.base+'/api/board-updates?version='+version,headers={'Cookie':cookie})
            with urllib.request.urlopen(request,timeout=5) as response:
                found.append((response.status,json.load(response)))
        thread=threading.Thread(target=wait_once,daemon=True)
        thread.start()
        time.sleep(0.2)
        request=urllib.request.Request(self.base+'/api/workspace/import-apply',data=json.dumps(dict(preview_id=preview['preview_id'],rows=[3])).encode(),headers={'Content-Type':'application/json'})
        with client.open(request,timeout=5) as response:
            applied=json.load(response)
        thread.join(5)
        self.assertEqual(applied['imported'],1)
        self.assertEqual(len(found),1)
        status,payload=found[0]
        self.assertEqual(status,200)
        self.assertTrue(payload.get('reload'))
        for key in ('account','accounts','computers','phone_cards','nurture'):
            self.assertNotIn(key,payload)
        with client.open(self.base+'/api/board',timeout=5) as response:
            self.assertEqual(len(json.load(response)['accounts']),28)
        request=urllib.request.Request(self.base+'/api/auth/users',data=json.dumps(dict(username='ordinary_staff',name='ordinary_staff',role='employee',permissions=auth.DEFAULTS['employee'],password=test_auth_support.TEST_PASSWORD,active=True)).encode(),headers={'Content-Type':'application/json'})
        with client.open(request,timeout=5) as response:
            self.assertEqual(response.status,200)
        staff=test_auth_support.client(self.base,'ordinary_staff')
        request=urllib.request.Request(self.base+'/api/workspace/import-preview',data=json.dumps(dict(kind='accounts',filename='x.csv',content=content)).encode(),headers={'Content-Type':'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as error:
            staff.open(request,timeout=5)
        self.assertEqual(error.exception.code,403)
        error.exception.close()
        for path in ('/api/workspace/history','/api/workspace/backups'):
            with self.assertRaises(urllib.error.HTTPError) as error:
                staff.open(self.base+path,timeout=5)
            self.assertEqual(error.exception.code,403)
            error.exception.close()

    def test_export_waits_while_heavy_slot_held(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('heavy_work'))
        import heavy_work
        client=test_auth_support.owner_client(app,self.base)
        body=dict(kind='accounts',ids=['43'],format='csv')
        request=urllib.request.Request(self.base+'/api/workspace/export',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
        with client.open(request,timeout=5) as response:
            idle=json.load(response)
        self.assertEqual(idle['count'],1)
        self.assertIn('43',__import__('base64').b64decode(idle['content']).decode('utf-8-sig'))
        heavy_work.acquire()
        held={'value':True}
        finished=[]
        def cleanup():
            if held['value']:
                heavy_work.release()
                held['value']=False
            if thread.is_alive():thread.join(5)
        thread=threading.Thread(target=lambda: finished.append(json.load(client.open(urllib.request.Request(self.base+'/api/workspace/export',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'}),timeout=5))),daemon=True)
        self.addCleanup(cleanup)
        thread.start()
        deadline=time.monotonic()+2
        while heavy_work.waiting()<1 and time.monotonic()<deadline:time.sleep(0.02)
        self.assertGreaterEqual(heavy_work.waiting(),1)
        self.assertEqual(finished,[])
        heavy_work.release()
        held['value']=False
        thread.join(5)
        self.assertEqual(len(finished),1)
        self.assertEqual(finished[0]['count'],idle['count'])
        self.assertEqual(finished[0]['content'],idle['content'])
        self.assertEqual(finished[0]['filename'],idle['filename'])
        self.assertEqual(finished[0]['mime'],idle['mime'])

    def test_import_and_screenshot_share_export_slot(self):
        import base64
        import csv
        import io
        import heavy_work
        client=test_auth_support.owner_client(app,self.base)
        started=app.today()
        for path,payload in (('/api/workspace/nurture/join',dict(id='43')),('/api/workspace/nurture/verification',dict(id='43',verification_state='无需核对，可使用')),('/api/workspace/nurture/start',dict(id='43',started=started))):
            request=urllib.request.Request(self.base+path,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
            with client.open(request,timeout=5) as response:
                self.assertEqual(response.status,200)
        buffer=io.StringIO()
        csv.writer(buffer).writerows([['内部编号','昵称','抖音号','注册手机号'],['','排队账号','slot_unique_handle','13800000001']])
        content=base64.b64encode(buffer.getvalue().encode('utf-8-sig')).decode()
        request=urllib.request.Request(self.base+'/api/workspace/import-preview',data=json.dumps(dict(kind='accounts',filename='accounts.csv',content=content)).encode(),headers={'Content-Type':'application/json'})
        with client.open(request,timeout=5) as response:
            preview=json.load(response)
        self.assertTrue(preview['rows'][0]['selected'])
        with client.open(self.base+'/api/board',timeout=5) as response:
            board=json.load(response)
        versions=board['record_versions']
        jar=next(handler.cookiejar for handler in client.handlers if hasattr(handler,'cookiejar'))
        cookie='; '.join('%s=%s'%(item.name,item.value) for item in jar)
        csrf=next(value for key,value in client.addheaders if key=='X-CSRF-Token')
        headers={'Content-Type':'application/json','Cookie':cookie,'X-CSRF-Token':csrf}
        image='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='
        jobs=(
            ('/api/workspace/import-apply',dict(preview_id=preview['preview_id'],rows=[2],_expected_versions={})),
            ('/api/workspace/nurture/upload',dict(id='43',day=started,content=image,_expected_versions={'account:43':versions['account:43'],'nurture:43':versions['nurture:43']})),
        )
        heavy_work.acquire()
        held={'value':True}
        finished=[]
        threads=[]
        def cleanup():
            if held['value']:
                heavy_work.release()
                held['value']=False
            for item in threads:
                if item.is_alive():item.join(5)
        self.addCleanup(cleanup)
        def post(path,payload):
            request=urllib.request.Request(self.base+path,data=json.dumps(payload).encode(),headers=headers)
            with urllib.request.urlopen(request,timeout=5) as response:
                finished.append(json.load(response))
        for path,payload in jobs:
            thread=threading.Thread(target=post,args=(path,payload),daemon=True)
            threads.append(thread)
            thread.start()
        deadline=time.monotonic()+2
        while heavy_work.waiting()<2 and time.monotonic()<deadline:time.sleep(0.02)
        self.assertGreaterEqual(heavy_work.waiting(),2)
        self.assertEqual(finished,[])
        account=next(row for row in board['accounts'] if row['id']=='39')
        account['note']='名额外保存'
        request=urllib.request.Request(self.base+'/api/account',data=json.dumps(account).encode(),headers={'Content-Type':'application/json'})
        with client.open(request,timeout=5) as response:
            saved=json.load(response)
        self.assertEqual(response.status,200)
        self.assertEqual(saved['account']['note'],'名额外保存')
        self.assertEqual(finished,[])
        heavy_work.release()
        held['value']=False
        for thread in threads:thread.join(5)
        self.assertEqual(len(finished),2)
        self.assertEqual(sum(1 for item in finished if item.get('imported')==1),1)
        self.assertEqual(sum(1 for item in finished if item.get('ok') is True),1)

    def test_interval_reload_is_only_the_fallback(self):
        import subprocess
        harness=r'''
const fs=require('fs');
const path=require('path');
const vm=require('vm');
const appSource=fs.readFileSync(path.join(process.cwd(),'static','app.js'),'utf8');
const updatesPath=path.join(process.cwd(),'static','board-updates.js');
const updatesSource=fs.existsSync(updatesPath)?fs.readFileSync(updatesPath,'utf8'):'';
function run(status){
  const context={console,setImmediate};
  context.globalThis=context;
  context.window=context;
  context.__updatesSource=updatesSource;
  vm.createContext(context);
  vm.runInContext(`
    var __intervals=[];
    var __boardFetches=[];
    var __elements={};
    function __element(){
      return {value:'',hidden:false,textContent:'',innerHTML:'',open:false,checked:false,indeterminate:false,disabled:false,title:'',className:'',classList:{add(){},remove(){},toggle(){return false;}},style:{},dataset:{},addEventListener(){},removeEventListener(){},dispatchEvent(){},showModal(){this.open=true;},close(){this.open=false;},focus(){},querySelector(){return __element();},querySelectorAll(){return [];},appendChild(){},setAttribute(){},removeAttribute(){}};
    }
    document={visibilityState:'visible',body:{appendChild(node){if(node&&String(node.src||'').includes('board-updates.js')){if(!__updatesSource)throw new Error('missing board-updates.js');(0,eval)(__updatesSource);}if(node&&typeof node.onload==='function')node.onload();}},addEventListener(){},dispatchEvent(){},createElement(){return {src:'',onload:null};},getElementById(id){if(!__elements[id])__elements[id]=__element();return __elements[id];},querySelector(){return __element();},querySelectorAll(){return [];}};
    location={search:'',href:'',replace(){}};
    CustomEvent=class CustomEvent{constructor(type){this.type=type;}};
    crypto={randomUUID:()=>'test-id',getRandomValues(bytes){return bytes;}};
    accountSearchFields=[];
    clearAccountSearch=()=>{};
    activeLifecyclePreset='';
    fetch=async(url)=>{const target=String(url);const updates=target.includes('/api/board-updates');const code=updates?__STATUS__:200;if(target.includes('/api/board')&&!updates)__boardFetches.push(target);return {status:code,ok:code>=200&&code<300,json:async()=>updates?{version:'known'}:{date:'2026-10-26',today:'2026-10-26',computers:[],statuses:[],accounts:[],collection:{auto_enabled:false,paused:false,worker_status:'',worker_online:false,pause_reason:'',linked:0,waiting:0,done:0,failed:0},user:{},backup:{}}};};
    setInterval=(fn,ms)=>{__intervals.push({fn,ms});return __intervals.length;};
    clearInterval=()=>{};
    setTimeout=(fn)=>{if(typeof fn==='function')fn();return 0;};
    clearTimeout=()=>{};
    confirm=()=>false;
    window.addEventListener=()=>{};
    window.removeEventListener=()=>{};
  `.replace('__STATUS__',String(status)),context);
  vm.runInContext(appSource+'\n__ready=new Promise(resolve=>setImmediate(()=>setImmediate(resolve)));',context);
  return context.__ready.then(()=>context);
}
(async()=>{
  const missing=await run(404);
  const ready=await run(200);
  const errors=[];
  const timers=missing.__intervals.filter(item=>item.ms===5000);
  if(timers.length!==1)errors.push('404 should register one 5s load timer, got '+timers.length);
  else{
    const before=missing.__boardFetches.length;
    await timers[0].fn();
    if(missing.__boardFetches.length!==before+1)errors.push('404 timer should call load');
    missing.__elements.editor.open=true;
    await timers[0].fn();
    if(missing.__boardFetches.length!==before+1)errors.push('open editor should not call load');
  }
  if(ready.__intervals.some(item=>item.ms===5000))errors.push('available board-updates should not register the 5s timer');
  if(errors.length){console.error(errors.join('\n'));process.exit(1);}
})().catch(error=>{console.error(error&&error.stack||error);process.exit(1);});
'''
        completed=subprocess.run(['node','-e',harness],cwd=os.path.dirname(os.path.abspath(__file__)),capture_output=True,text=True,timeout=20)
        self.assertEqual(completed.returncode,0,(completed.stderr or completed.stdout).strip())

    def test_editor_and_daily_do_not_full_reload(self):
        import subprocess
        harness=r'''
const fs=require('fs');
const path=require('path');
const vm=require('vm');
const appSource=fs.readFileSync(path.join(process.cwd(),'static','app.js'),'utf8');
const updatesPath=path.join(process.cwd(),'static','board-updates.js');
const updatesSource=fs.existsSync(updatesPath)?fs.readFileSync(updatesPath,'utf8'):'';
const context={console,setImmediate};
context.globalThis=context;
context.window=context;
context.__updatesSource=updatesSource;
vm.createContext(context);
vm.runInContext(`
  var __boardFetches=[];
  var __calls=[];
  var __elements={};
  var __clicks=[];
  function __element(){return {value:'',hidden:false,textContent:'',innerHTML:'',open:false,checked:false,indeterminate:false,disabled:false,title:'',className:'',classList:{add(){},remove(){},toggle(){return false;}},style:{},dataset:{},addEventListener(){},removeEventListener(){},dispatchEvent(){},showModal(){this.open=true;},close(){this.open=false;},focus(){},querySelector(){return __element();},querySelectorAll(){return [];},appendChild(){},setAttribute(){},removeAttribute(){}};}
  document={visibilityState:'visible',body:{appendChild(node){if(node&&String(node.src||'').includes('board-updates.js')){if(!__updatesSource)throw new Error('missing board-updates.js');(0,eval)(__updatesSource);}if(node&&typeof node.onload==='function')node.onload();}},addEventListener(type,fn){if(type==='click')__clicks.push(fn);},dispatchEvent(){},createElement(){return {src:'',onload:null};},getElementById(id){if(!__elements[id])__elements[id]=__element();return __elements[id];},querySelector(){return __element();},querySelectorAll(){return [];}};
  location={search:'',href:'',replace(){}};
  CustomEvent=class CustomEvent{constructor(type){this.type=type;}};
  crypto={randomUUID:()=>'test-id',getRandomValues(bytes){return bytes;}};
  currentUser=null;
  accountSearchFields=[];
  clearAccountSearch=()=>{};
  activeLifecyclePreset='';
  lifecycleEdit=()=>{};
  readinessEdit=()=>{};
  phoneDeviceEdit=()=>{};
  lifecyclePayload=()=>({});
  readinessPayload=()=>({});
  captureVersions=()=>{};
  applyAccountPermissions=()=>{};
  applyVersionDelta=()=>{};
  versionPayload=(path,body)=>body;
  activeEditVersions=()=>null;
  renderVerificationFilter=()=>{};
  ScopeUI={mount(){},payload(){return {};}};
  fetch=async(url)=>{
    const target=String(url);
    __calls.push(target);
    if(target.includes('/api/board-updates'))return {status:200,ok:true,json:async()=>({version:'known'})};
    if(target.includes('/api/board')){__boardFetches.push(target);return {status:200,ok:true,json:async()=>({date:'2026-10-26',today:'2026-10-26',computers:[{id:'1',name:'电脑1'}],statuses:['正常'],accounts:[{id:'43',name:'jia',computer_id:'1',slot:1,status:'正常',douyin_id:'',homepage:'',note:'OLD-NOTE',published:false,phone_device_name:'OLD-DEVICE'}],collection:{auto_enabled:false,paused:false,worker_status:'',worker_online:false,pause_reason:'',linked:0,waiting:0,done:0,failed:0},user:{},backup:{},record_versions:{'account:43':'v1'}})};}
    if(target.includes('/api/account/patch'))return {status:200,ok:true,json:async()=>({ok:true,_record_versions:{}})};
    if(target.includes('/api/account'))return {status:200,ok:true,json:async()=>({ok:true,account:{id:'43',name:'jia',computer_id:'1',slot:1,status:'正常',douyin_id:'',homepage:'',note:'RESPONSE-NOTE',published:false,phone_device_name:'RESPONSE-DEVICE'},_record_versions:{'account:43':'v2'}})};
    if(target.includes('/api/daily'))return {status:200,ok:true,json:async()=>({ok:true,daily:{id:'43',date:'2026-10-26',published:true},_record_versions:{}})};
    return {status:404,ok:false,json:async()=>({error:'missing'})};
  };
  setInterval=()=>0;
  clearInterval=()=>{};
  setTimeout=(fn)=>{if(typeof fn==='function')fn();return 0;};
  clearTimeout=()=>{};
  confirm=()=>false;
  window.addEventListener=()=>{};
  window.removeEventListener=()=>{};
`,context);
const scenario=`
render=()=>{};
__ready=(async()=>{
  await new Promise(resolve=>setImmediate(()=>setImmediate(resolve)));
  if(!data||!data.accounts||!data.accounts.length)throw new Error('board did not load');
  const before=()=>__boardFetches.length;
  const savedBefore=before();
  edit('43');
  __elements.editNote.value='FORM-NOTE';
  await __elements.editForm.onsubmit({preventDefault(){},submitter:{disabled:false}});
  if(before()!==savedBefore)throw new Error('editor save called load');
  const saved=data.accounts.find(account=>account.id==='43');
  if(!saved||saved.note!=='RESPONSE-NOTE'||saved.phone_device_name!=='RESPONSE-DEVICE')throw new Error('editor did not apply the response account');
  const dailyBefore=before();
  const button={dataset:{publish:'43'},disabled:false,closest(selector){return selector==='[data-publish]'?this:null;}};
  if(!__clicks.length)throw new Error('missing publish handler');
  await __clicks[0]({target:button});
  if(before()!==dailyBefore)throw new Error('daily save called load');
  const marked=data.accounts.find(account=>account.id==='43');
  if(!marked||marked.published!==true)throw new Error('daily did not apply the response');
})();
`;
vm.runInContext(appSource+'\n'+scenario,context);
context.__ready.then(()=>process.exit(0),error=>{console.error(error&&error.stack||error);process.exit(1);});
'''
        completed=subprocess.run(['node','-e',harness],cwd=os.path.dirname(os.path.abspath(__file__)),capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20)
        self.assertEqual(completed.returncode,0,(completed.stderr or completed.stdout).strip())

    def test_remote_update_leaves_editor_version(self):
        import subprocess
        harness=r'''
const fs=require('fs');
const path=require('path');
const vm=require('vm');
const root=process.cwd();
const versions=fs.readFileSync(path.join(root,'static','edit-versions.js'),'utf8');
const updates=fs.readFileSync(path.join(root,'static','board-updates.js'),'utf8');
const context={console};
vm.createContext(context);
const script=[
"let data={accounts:[{id:'43',note:'OLD',phone_device_name:'OLD-DEVICE'}],record_versions:{'account:43':'opened'}};",
"const $=id=>({open:id==='editor'});",
versions,
updates,
"if(typeof applyRemoteBoardUpdate!=='function')throw new Error('remote update is not applied separately from editor versions');",
"let deltaCalls=0;",
"const realApply=applyVersionDelta;",
"applyVersionDelta=function(){deltaCalls++;return realApply.apply(this,arguments);};",
"captureVersions('editor');",
"applyRemoteBoardUpdate({account:{id:'43',note:'REMOTE',phone_device_name:'REMOTE-DEVICE'},version:'v2',record_version:'remote-v'});",
"if(deltaCalls!==0)throw new Error('remote update called applyVersionDelta');",
"const snapshot=capturedVersions.get('editor');",
"if(!snapshot||snapshot['account:43']!=='opened')throw new Error('remote update changed capturedVersions');",
"const row=data.accounts.find(account=>account.id==='43');",
"if(!row||row.note!=='REMOTE'||row.phone_device_name!=='REMOTE-DEVICE')throw new Error('remote update did not apply the account');"
].join('\n');
try{vm.runInContext(script,context);}
catch(error){console.error(error&&error.stack||error);process.exit(1);}
'''
        completed=subprocess.run(['node','-e',harness],cwd=os.path.dirname(os.path.abspath(__file__)),capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20)
        self.assertEqual(completed.returncode,0,(completed.stderr or completed.stdout).strip())

    def test_collection_poll_stops_while_disabled(self):
        import subprocess
        harness=r'''
const fs=require('fs');
const path=require('path');
const vm=require('vm');
const source=fs.readFileSync(path.join(process.cwd(),'static','collection-panel.js'),'utf8');
function run(enabled){
  const context={console,setImmediate};
  context.window=context;
  vm.createContext(context);
  vm.runInContext(`
    var __intervals=[];
    var __fetches=[];
    var __elements={};
    function __element(){return {textContent:'',disabled:false,hidden:false,onclick:null};}
    document={visibilityState:'visible',getElementById(id){if(!__elements[id])__elements[id]=__element();return __elements[id];}};
    fetch=async()=>{__fetches.push('/api/collection/status');return {ok:true,status:200,json:async()=>({collection:{enabled:__ENABLED__,worker_online:false,worker_status:'idle',waiting:0,done:0,failed:0,paused:false,pause_reason:''},can_manage:false})};};
    setInterval=(fn,ms)=>{__intervals.push({fn,ms});return __intervals.length;};
    clearInterval=()=>{};
  `.replace('__ENABLED__',enabled?'true':'false'),context);
  vm.runInContext(source+'\n__ready=new Promise(resolve=>setImmediate(()=>setImmediate(resolve)));',context);
  return context.__ready.then(()=>context);
}
(async()=>{
  const disabled=await run(false);
  const enabled=await run(true);
  const errors=[];
  if(disabled.__fetches.length!==1)errors.push('disabled panel should read status once, got '+disabled.__fetches.length);
  if(disabled.__intervals.some(item=>item.ms===5000))errors.push('disabled collection still registered the 5s poll');
  const timers=enabled.__intervals.filter(item=>item.ms===5000);
  if(timers.length!==1)errors.push('enabled collection should register one 5s poll, got '+timers.length);
  else{
    const before=enabled.__fetches.length;
    await timers[0].fn();
    if(enabled.__fetches.length!==before+1)errors.push('enabled poll should request collection status');
  }
  if(errors.length){console.error(errors.join('\n'));process.exit(1);}
})().catch(error=>{console.error(error&&error.stack||error);process.exit(1);});
'''
        completed=subprocess.run(['node','-e',harness],cwd=os.path.dirname(os.path.abspath(__file__)),capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20)
        self.assertEqual(completed.returncode,0,(completed.stderr or completed.stdout).strip())


if __name__=='__main__':
    unittest.main()
