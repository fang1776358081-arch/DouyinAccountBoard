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
        board_updates._pending=None
        board_updates._version=None
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

    def test_office_move_and_revoked_grant_follow_the_cursor(self):
        owner={'role':'owner','office_ids':[]}
        office_a={'role':'employee','office_ids':['A']}
        office_b={'role':'employee','office_ids':['B']}
        office_c={'role':'employee','office_ids':['C']}
        with app.connect() as db:
            start=db.execute('SELECT COALESCE(MAX(id),0) FROM operations_history').fetchone()[0]
            board_updates.note_grant_change(db,['A'],['A'])
            self.assertEqual(db.execute('SELECT COALESCE(MAX(id),0) FROM operations_history').fetchone()[0],start)
            board_updates.notify_office_move(db,'43',previous_offices=['A'],next_offices=['B'])
            board_updates.notify_office_move(db,'39',previous_offices=['A'],next_offices=[])
            moved=board_updates.since(db,start,office_a)
            gained=board_updates.since(db,start,office_b)
            other=board_updates.since(db,start,office_c)
            seen=board_updates.since(db,start,owner)
            cursor=db.execute('SELECT COALESCE(MAX(id),0) FROM operations_history').fetchone()[0]
            board_updates.note_grant_change(db,['A','B'],['B'])
            revoked=board_updates.since(db,cursor,office_a)
        self.assertFalse(moved.get('reload'))
        self.assertEqual([(item['entity'],item['kind']) for item in moved['changes']],[('account:43','remove'),('account:39','remove')])
        self.assertEqual([(item['entity'],item['kind']) for item in gained['changes']],[('account:43','change')])
        self.assertEqual(gained['changes'][0]['after']['id'],'43')
        self.assertNotIn('account:39',[item['entity'] for item in gained['changes']])
        self.assertEqual(other['changes'],[])
        self.assertEqual([item['entity'] for item in seen['changes']],['account:43','account:39'])
        self.assertTrue(all(item['kind']=='change' for item in seen['changes']))
        self.assertTrue(revoked.get('reload'))
        self.assertNotIn('changes',revoked)
        import subprocess
        harness=r'''
const fs=require('fs');const vm=require('vm');
const source=fs.readFileSync('static/board-updates.js','utf8');
function run(mode){
  const context={console};context.globalThis=context;vm.createContext(context);
  const script=`
    var data={accounts:[{id:'43',name:'jia',note:'OLD',published:false},{id:'39',name:'yi',note:'KEEP',published:false}],cursor:4,date:'2026-10-26',record_versions:{'account:43':'opened'}};
    globalThis.data=data;var requestSerial=0;var loads=0;
    document={getElementById(){return {open:false};}};
    function render(){}
    async function load(){loads++;data={accounts:[{id:'39',name:'yi',note:'KEEP',published:false}],cursor:9,date:'2026-10-26',computers:[],statuses:[],user:{id:1},record_versions:{}};globalThis.data=data;}
    fetch=async()=>({status:200,ok:true,json:async()=>__MODE__==='reload'?{reload:true,cursor:9}:{cursor:6,changes:[{entity:'account:43',kind:'remove',after:null},{entity:'account:40',kind:'change',after:{id:'40',name:'xin',note:'MOVED',published:false},record_version:'moved'}]}});
    ${source}
    __ready=catchUpBoard().then(()=>({ids:data.accounts.map(account=>account.id),loads,moved:(data.accounts.find(account=>account.id==='40')||{}).note,kept:(data.accounts.find(account=>account.id==='39')||{}).note}));
  `.replace('__MODE__',JSON.stringify(mode));
  vm.runInContext(script,context);
  return context.__ready;
}
(async()=>{
  const moved=await run('move');
  if(moved.loads!==0)throw new Error('office move reloaded the whole board');
  if(moved.ids.includes('43')||moved.ids.join(',')!=='39,40')throw new Error('old office kept the row or new office missed it: '+moved.ids.join(','));
  if(moved.moved!=='MOVED'||moved.kept!=='KEEP')throw new Error('office move changed the wrong row');
  const revoked=await run('reload');
  if(revoked.loads!==1||revoked.ids.join(',')!=='39')throw new Error('revoked grant did not replace the cached board');
})().catch(error=>{console.error(error&&error.stack||error);process.exit(1);});
'''
        completed=subprocess.run(['node','-e',harness],cwd=os.path.dirname(os.path.abspath(__file__)),capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20)
        self.assertEqual(completed.returncode,0,(completed.stderr or completed.stdout).strip())

    def test_end_to_end_six_checks(self):
        client=test_auth_support.owner_client(app,self.base)
        owner={'role':'owner','office_ids':[]}
        with client.open(self.base+'/api/board',timeout=5) as response:
            board=json.load(response)
        self.assertIsInstance(board['cursor'],int)
        self.assertGreater(len(board['accounts']),1)
        start=board['cursor']
        def save(ident,note):
            with client.open(self.base+'/api/board',timeout=5) as response:
                current=json.load(response)
            account=next(row for row in current['accounts'] if row['id']==ident)
            request=urllib.request.Request(self.base+'/api/account',data=json.dumps({**account,'note':note}).encode(),headers={'Content-Type':'application/json'})
            with client.open(request,timeout=5) as response:
                self.assertEqual(response.status,200)
        save('43','端到端甲')
        save('39','端到端乙')
        with app.connect() as db:
            caught=board_updates.since(db,start,owner)
        accounts=[item for item in caught['changes'] if item['entity'].startswith('account:')]
        self.assertEqual([item['entity'] for item in accounts],['account:43','account:39'])
        self.assertEqual([item['after']['note'] for item in accounts],['端到端甲','端到端乙'])
        self.assertFalse(caught.get('reload'))
        with app.connect() as db:
            first=db.execute("SELECT MIN(id) FROM operations_history WHERE id>? AND entity='account:43'",(start,)).fetchone()[0]
            db.execute('DELETE FROM operations_history WHERE id=?',(first,))
            truncated=board_updates.since(db,first,owner)
        self.assertTrue(truncated.get('reload'))
        self.assertNotIn('changes',truncated)
        with app.connect() as db:
            cursor=db.execute('SELECT COALESCE(MAX(id),0) FROM operations_history').fetchone()[0]
            board_updates.notify_office_move(db,'43',previous_offices=['A'],next_offices=['B'])
            hidden=board_updates.since(db,cursor,{'role':'employee','office_ids':['C']})
            lost=board_updates.since(db,cursor,{'role':'employee','office_ids':['A']})
            board_updates.note_grant_change(db,['A'],[])
            revoked=board_updates.since(db,lost['cursor'],{'role':'employee','office_ids':['A']})
        self.assertEqual(hidden['changes'],[])
        self.assertEqual(lost['changes'][0]['kind'],'remove')
        self.assertEqual(lost['changes'][0]['entity'],'account:43')
        self.assertTrue(revoked.get('reload'))
        self.assertNotIn('changes',revoked)
        stale=urllib.request.Request(self.base+'/api/account',data=json.dumps({'id':'43','note':'不会写上','_expected_versions':{'account:43':'stale-token'}}).encode(),headers={'Content-Type':'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            client.open(stale,timeout=5)
        self.assertEqual(rejected.exception.code,409)
        with app.connect() as db:
            self.assertEqual(db.execute("SELECT note FROM accounts WHERE id='43'").fetchone()[0],'端到端甲')
        import subprocess
        harness=r'''
const fs=require('fs');const path=require('path');const vm=require('vm');
const root=process.cwd();
const versions=fs.readFileSync(path.join(root,'static','edit-versions.js'),'utf8');
const updates=fs.readFileSync(path.join(root,'static','board-updates.js'),'utf8');
const context={console};context.globalThis=context;vm.createContext(context);
vm.runInContext(versions+'\n'+updates+`
let data={accounts:[{id:'43',name:'jia',note:'OLD',published:false},{id:'39',name:'yi',note:'OLD',published:false}],cursor:4,date:'2026-10-26',record_versions:{'account:43':'opened','account:39':'opened'}};
const $=id=>({open:id==='editor',value:'FORM-NOTE'});
document={getElementById(){return {open:false};}};
function render(){}
let loads=0;
async function load(){loads++;}
const before=data.accounts.map(account=>account.note);
captureVersions('editor');
fetch=async()=>({status:200,ok:true,json:async()=>({cursor:6,changes:[
 {entity:'account:43',kind:'change',after:{id:'43',note:'端到端甲'},record_version:'remote-a'},
 {entity:'account:39',kind:'change',after:{id:'39',note:'端到端乙'},record_version:'remote-b'}
]})});
__ready=catchUpBoard().then(()=>{
 if(before.join(',')!=='OLD,OLD')throw new Error('reopen did not start from the previous board');
 if(loads!==0)throw new Error('reopen reloaded the whole board');
 const notes=data.accounts.map(account=>account.note).join(',');
 if(notes!=='端到端甲,端到端乙')throw new Error('reopen did not apply only the missed rows');
 if($('editNote').value!=='FORM-NOTE')throw new Error('remote update changed the editor text');
 if(capturedVersions.get('editor')['account:43']!=='opened')throw new Error('remote update changed the editor version');
});
`,context);
context.__ready.then(()=>process.exit(0),error=>{console.error(error&&error.stack||error);process.exit(1);});
'''
        completed=subprocess.run(['node','-e',harness],cwd=os.path.dirname(os.path.abspath(__file__)),capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20)
        self.assertEqual(completed.returncode,0,(completed.stderr or completed.stdout).strip())

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
"if(!row||row.note!=='REMOTE'||row.phone_device_name!=='REMOTE-DEVICE')throw new Error('remote update did not apply the account');",
"data.accounts.push({id:'39',note:'KEEP'});",
"applyRemoteBoardUpdate({removed:'39'});",
"if(data.accounts.some(account=>account.id==='39'))throw new Error('old office row was not removed');",
"if(!data.accounts.some(account=>account.id==='43'))throw new Error('removal deleted a different account');",
"if(capturedVersions.get('editor')['account:43']!=='opened')throw new Error('removal changed the editor version');",
"applyRemoteBoardUpdate({account:{id:'40',note:'ADDED',phone_device_name:'NEW-DEVICE'},record_version:'added-v'});",
"const added=data.accounts.find(account=>account.id==='40');",
"if(!added||added.note!=='ADDED')throw new Error('new office did not receive the account');",
"if(capturedVersions.get('editor')['account:43']!=='opened')throw new Error('added account changed the editor version');"
].join('\n');
try{vm.runInContext(script,context);}
catch(error){console.error(error&&error.stack||error);process.exit(1);}
'''
        completed=subprocess.run(['node','-e',harness],cwd=os.path.dirname(os.path.abspath(__file__)),capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20)
        self.assertEqual(completed.returncode,0,(completed.stderr or completed.stdout).strip())

    def test_focus_applies_one_change_without_full_board(self):
        client=test_auth_support.owner_client(app,self.base)
        with client.open(self.base+'/api/board',timeout=5) as response:
            board=json.load(response)
        with app.connect() as db:
            self.assertEqual(board['cursor'],db.execute('SELECT COALESCE(MAX(id),0) FROM operations_history').fetchone()[0])
        import subprocess
        harness=r'''
const fs=require('fs');
const path=require('path');
const vm=require('vm');
const root=process.cwd();
const versions=fs.readFileSync(path.join(root,'static','edit-versions.js'),'utf8');
const appSource=fs.readFileSync(path.join(root,'static','app.js'),'utf8');
const updatesSource=fs.readFileSync(path.join(root,'static','board-updates.js'),'utf8');
const context={console,setImmediate};
context.globalThis=context;
context.window=context;
context.__updatesSource=updatesSource;
vm.createContext(context);
const prelude=`
  var __boardFetches=[];
  var __calls=[];
  var __elements={};
  var __focus=[];
  function __element(){return {value:'',hidden:false,textContent:'',innerHTML:'',open:false,checked:false,indeterminate:false,disabled:false,title:'',className:'',classList:{add(){},remove(){},toggle(){return false;}},style:{},dataset:{},addEventListener(){},removeEventListener(){},dispatchEvent(){},showModal(){this.open=true;},close(){this.open=false;},focus(){},querySelector(){return __element();},querySelectorAll(){return [];},appendChild(){},setAttribute(){},removeAttribute(){}};}
  document={visibilityState:'visible',body:{appendChild(node){if(node&&String(node.src||'').includes('board-updates.js')){if(!__updatesSource)throw new Error('missing board-updates.js');(0,eval)(__updatesSource);}if(node&&typeof node.onload==='function')node.onload();}},addEventListener(){},dispatchEvent(){},createElement(){return {src:'',onload:null};},getElementById(id){if(!__elements[id])__elements[id]=__element();return __elements[id];},querySelector(){return __element();},querySelectorAll(){return [];}};
  location={search:'',href:'',replace(){}};
  CustomEvent=class CustomEvent{constructor(type){this.type=type;}};
  crypto={randomUUID:()=>'test-id',getRandomValues(bytes){return bytes;}};
  accountSearchFields=[];
  clearAccountSearch=()=>{};
  activeLifecyclePreset='';
  lifecycleEdit=()=>{};
  readinessEdit=()=>{};
  phoneDeviceEdit=()=>{};
  lifecyclePayload=()=>({});
  readinessPayload=()=>({});
  applyAccountPermissions=()=>{};
  renderVerificationFilter=()=>{};
  ScopeUI={mount(){},payload(){return {};}};
  fetch=async(url)=>{
    const target=String(url);
    __calls.push(target);
    if(target.includes('/api/board-updates?cursor=')){
      const cursor=Number(String(target).split('cursor=')[1]);
      if(cursor===4)return {status:200,ok:true,json:async()=>({cursor:5,changes:[{entity:'account:43',kind:'change',after:{id:'43',note:'REMOTE-NOTE'},record_version:'remote-v'}]})};
      if(cursor===5)return {status:200,ok:true,json:async()=>({cursor:6,changes:[{entity:'daily:43:2026-10-26',kind:'change',after:{account_id:'43',day:'2026-10-26',published:1},record_version:'daily-v'}]})};
      if(cursor===6)return {status:200,ok:true,json:async()=>({reload:true,cursor:9})};
      return {status:200,ok:true,json:async()=>({cursor,changes:[]})};
    }
    if(target.includes('/api/board-updates'))return {status:200,ok:true,json:async()=>({version:'known'})};
    if(target.includes('/api/board')){__boardFetches.push(target);return {status:200,ok:true,json:async()=>({date:'2026-10-26',today:'2026-10-26',cursor:4,computers:[{id:'1',name:'电脑1'}],statuses:['正常'],accounts:[{id:'43',name:'jia',computer_id:'1',slot:1,status:'正常',note:'OLD-NOTE',published:false,phone_device_name:'OLD-DEVICE'},{id:'39',name:'yi',computer_id:'1',slot:2,status:'正常',note:'KEEP',published:false}],collection:{auto_enabled:false,paused:false,worker_status:'',worker_online:false,pause_reason:'',linked:0,waiting:0,done:0,failed:0},user:{},backup:{},record_versions:{'account:43':'opened','account:39':'other'},phone_cards:{cards:[]},phone_devices:{devices:[]},publishing_layout:{enabled:false,cells:[],batch:''},nurture:{records:[]},offices:[]})};}
    return {status:404,ok:false,json:async()=>({error:'missing'})};
  };
  setInterval=(fn,ms)=>{return 0;};
  clearInterval=()=>{};
  setTimeout=(fn)=>{if(typeof fn==='function')fn();return 0;};
  clearTimeout=()=>{};
  confirm=()=>false;
  window.addEventListener=(type,fn)=>{if(type==='focus')__focus.push(fn);};
  window.removeEventListener=()=>{};
`;
const scenario=`
render=()=>{};
__ready=(async()=>{
  await new Promise(resolve=>setImmediate(()=>setImmediate(resolve)));
  if(!data||data.cursor!==4)throw new Error('board did not keep the sync cursor');
  if(!__focus.length)throw new Error('missing focus handler');
  edit('43');
  __elements.editNote.value='FORM-NOTE';
  const opened=capturedVersions.get('editor')['account:43'];
  const boardsBefore=__boardFetches.length;
  await __focus[0]();
  if(__boardFetches.length!==boardsBefore)throw new Error('valid cursor requested the full board');
  if(!__calls.some(item=>item.includes('cursor=4')))throw new Error('focus did not catch up from the cursor');
  const row=data.accounts.find(account=>account.id==='43');
  const other=data.accounts.find(account=>account.id==='39');
  if(!row||row.note!=='REMOTE-NOTE'||row.name!=='jia'||row.phone_device_name!=='OLD-DEVICE')throw new Error('account catch-up did not update only that row');
  if(!other||other.note!=='KEEP')throw new Error('catch-up changed a different account');
  if(__elements.editNote.value!=='FORM-NOTE')throw new Error('remote update changed the editor text');
  if(capturedVersions.get('editor')['account:43']!==opened)throw new Error('remote update changed the editor version');
  if(data.record_versions['account:43']!=='remote-v')throw new Error('board version was not updated');
  await __focus[0]();
  if(row.published!==true||row.note!=='REMOTE-NOTE'||other.note!=='KEEP')throw new Error('daily catch-up did not update only the publish flag');
  if(__elements.editNote.value!=='FORM-NOTE'||capturedVersions.get('editor')['account:43']!==opened)throw new Error('daily catch-up changed the editor');
  if(__boardFetches.length!==boardsBefore)throw new Error('daily catch-up requested the full board');
  await __focus[0]();
  if(__boardFetches.length!==boardsBefore)throw new Error('reload while editing requested the full board');
  __elements.editor.open=false;
  await __focus[0]();
  if(__boardFetches.length!==boardsBefore+1)throw new Error('expired cursor should reload the full board once');
})();
`;
vm.runInContext(prelude+'\n'+versions+'\n'+appSource+'\n'+scenario,context);
context.__ready.then(()=>process.exit(0),error=>{console.error(error&&error.stack||error);process.exit(1);});
'''
        completed=subprocess.run(['node','-e',harness],cwd=os.path.dirname(os.path.abspath(__file__)),capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20)
        self.assertEqual(completed.returncode,0,(completed.stderr or completed.stdout).strip())

    def test_reopen_uses_cached_board_then_cursor(self):
        import subprocess
        harness=r'''
const fs=require('fs');
const path=require('path');
const vm=require('vm');
const root=process.cwd();
const versions=fs.readFileSync(path.join(root,'static','edit-versions.js'),'utf8');
const appSource=fs.readFileSync(path.join(root,'static','app.js'),'utf8');
const authSource=fs.readFileSync(path.join(root,'static','auth-ui.js'),'utf8');
const updatesSource=fs.readFileSync(path.join(root,'static','board-updates.js'),'utf8');
const memory=new Map();
const localStorage={getItem(key){return memory.has(key)?memory.get(key):null;},setItem(key,value){memory.set(String(key),String(value));},removeItem(key){memory.delete(key);}};
function run(userId,note){
  const context={console,setImmediate,localStorage};
  context.globalThis=context;
  context.window=context;
  context.__updatesSource=updatesSource;
  context.__userId=userId;
  context.__note=note;
  vm.createContext(context);
  const prelude=`
    var __boardFetches=[];
    var __calls=[];
    var __elements={};
    var __dom=[];
    function __element(){return {value:'',hidden:false,textContent:'',innerHTML:'',open:false,checked:false,indeterminate:false,disabled:false,title:'',className:'',classList:{add(){},remove(){},toggle(){return false;}},style:{},dataset:{},addEventListener(){},removeEventListener(){},dispatchEvent(){},showModal(){this.open=true;},close(){this.open=false;},focus(){},querySelector(){return __element();},querySelectorAll(){return [];},appendChild(){},setAttribute(){},removeAttribute(){},reset(){}};}
    document={visibilityState:'visible',body:{appendChild(node){if(node&&String(node.src||'').includes('board-updates.js')){(0,eval)(__updatesSource);}if(node&&typeof node.onload==='function')node.onload();}},addEventListener(type,fn){if(type==='DOMContentLoaded')__dom.push(fn);},dispatchEvent(){},createElement(){return {src:'',onload:null};},getElementById(id){if(!__elements[id])__elements[id]=__element();return __elements[id];},querySelector(){return __element();},querySelectorAll(){return [];}};
    location={search:'',href:'',replace(){}};
    CustomEvent=class CustomEvent{constructor(type){this.type=type;}};
    crypto={randomUUID:()=>'test-id',getRandomValues(bytes){return bytes;}};
    accountSearchFields=[];
    clearAccountSearch=()=>{};
    activeLifecyclePreset='';
    renderVerificationFilter=()=>{};
    fetch=async(url)=>{
      const target=String(url);
      __calls.push(target);
      if(target.includes('/api/auth/me'))return {status:200,ok:true,json:async()=>({user:{id:__userId,name:'user'+__userId}})};
      if(target.includes('/api/auth/logout'))return {status:200,ok:true,json:async()=>({ok:true})};
      if(target.includes('/api/board-updates?cursor=')){
        const cursor=Number(String(target).split('cursor=')[1]);
        if(cursor===4)return {status:200,ok:true,json:async()=>({cursor:5,changes:[{entity:'account:43',kind:'change',after:{id:'43',note:'REMOTE-NOTE'},record_version:'remote-v'}]})};
        return {status:200,ok:true,json:async()=>({cursor,changes:[]})};
      }
      if(target.includes('/api/board-updates'))return {status:200,ok:true,json:async()=>({version:'known'})};
      if(target.includes('/api/board')){
        __boardFetches.push(target);
        return {status:200,ok:true,json:async()=>({date:'2026-10-26',today:'2026-10-26',cursor:4,computers:[{id:'1',name:'电脑1'}],statuses:['正常'],accounts:[{id:'43',name:'jia',computer_id:'1',slot:1,status:'正常',note:__note,published:false}],collection:{auto_enabled:false,paused:false,worker_status:'',worker_online:false,pause_reason:'',linked:0,waiting:0,done:0,failed:0},user:{id:__userId,name:'user'+__userId},backup:{},record_versions:{'account:43':'v1'},phone_cards:{cards:[]},phone_devices:{devices:[]},publishing_layout:{enabled:false,cells:[]},nurture:{records:[]},offices:[]})};
      }
      return {status:404,ok:false,json:async()=>({error:'missing'})};
    };
    setInterval=()=>0;
    clearInterval=()=>{};
    setTimeout=(fn)=>{if(typeof fn==='function')fn();return 0;};
    clearTimeout=()=>{};
    confirm=()=>false;
    window.addEventListener=()=>{};
    window.removeEventListener=()=>{};
  `;
  const scenario=`
render=()=>{};
__ready=(async()=>{
  await new Promise(resolve=>setImmediate(()=>setImmediate(resolve)));
  __result={calls:__calls.slice(),boards:__boardFetches.slice(),note:data&&data.accounts&&data.accounts[0]&&data.accounts[0].note,user:data&&data.user&&data.user.id};
})();
`;
  vm.runInContext(prelude+'\n'+versions+'\n'+appSource+'\n'+authSource+'\n'+scenario,context);
  return context.__ready.then(()=>context.__result);
}
(async()=>{
  const first=await run(7,'CACHED-NOTE');
  if(first.boards.length!==1||first.note!=='CACHED-NOTE')throw new Error('first open should show the full board');
  const second=await run(7,'SHOULD-NOT-LOAD');
  if(second.boards.length!==0)throw new Error('second open requested the full board: '+second.calls.join(' | '));
  if(!second.calls.some(item=>item.includes('cursor=4')))throw new Error('second open did not catch up from the saved cursor');
  if(second.note!=='REMOTE-NOTE')throw new Error('second open did not apply only the missed row, got '+second.note);
  const switched=await run(8,'OTHER-USER');
  if(switched.boards.length!==1||switched.note!=='OTHER-USER'||switched.user!==8)throw new Error('user switch still showed the previous board');
  if(String(localStorage.getItem('douyin-board-view')||'').includes('CACHED-NOTE'))throw new Error('previous user remained in the cache');
  const context={console,setImmediate,localStorage};
  context.globalThis=context;context.window=context;context.__updatesSource=updatesSource;context.__userId=8;context.__note='AFTER-LOGOUT';
  vm.createContext(context);
  const prelude=`
    var __boardFetches=[];var __calls=[];var __elements={};var __dom=[];
    function __element(){return {value:'',hidden:false,textContent:'',innerHTML:'',open:false,checked:false,disabled:false,title:'',className:'',classList:{add(){},remove(){},toggle(){return false;}},style:{},dataset:{},onclick:null,addEventListener(){},removeEventListener(){},dispatchEvent(){},showModal(){this.open=true;},close(){this.open=false;},focus(){},querySelector(){return __element();},querySelectorAll(){return [];},appendChild(){},setAttribute(){},removeAttribute(){},reset(){}};}
    document={visibilityState:'visible',body:{appendChild(node){if(node&&String(node.src||'').includes('board-updates.js')){(0,eval)(__updatesSource);}if(node&&typeof node.onload==='function')node.onload();}},addEventListener(type,fn){if(type==='DOMContentLoaded')__dom.push(fn);},dispatchEvent(){},createElement(){return {src:'',onload:null};},getElementById(id){if(!__elements[id])__elements[id]=__element();return __elements[id];},querySelector(){return __element();},querySelectorAll(){return [];}};
    location={search:'',href:'',replace(){}};
    CustomEvent=class CustomEvent{constructor(type){this.type=type;}};
    crypto={randomUUID:()=>'test-id',getRandomValues(bytes){return bytes;}};
    accountSearchFields=[];clearAccountSearch=()=>{};activeLifecyclePreset='';renderVerificationFilter=()=>{};
    fetch=async(url)=>{const target=String(url);__calls.push(target);if(target.includes('/api/auth/logout'))return {status:200,ok:true,json:async()=>({ok:true})};if(target.includes('/api/auth/me'))return {status:200,ok:true,json:async()=>({user:{id:8,name:'user8'}})};if(target.includes('/api/board-updates'))return {status:200,ok:true,json:async()=>({version:'known',cursor:4,changes:[]})};return {status:404,ok:false,json:async()=>({error:'missing'})};};
    setInterval=()=>0;clearInterval=()=>{};setTimeout=(fn)=>{if(typeof fn==='function')fn();return 0;};clearTimeout=()=>{};confirm=()=>false;window.addEventListener=()=>{};window.removeEventListener=()=>{};
  `;
  vm.runInContext(prelude+'\n'+versions+'\n'+appSource+'\n'+authSource+'\nrender=()=>{};__ready=(async()=>{await new Promise(resolve=>setImmediate(()=>setImmediate(resolve)));for(const fn of __dom)fn();await __elements.logout.onclick();})();',context);
  await context.__ready;
  if(localStorage.getItem('douyin-board-view')!==null)throw new Error('logout kept the previous board');
  const again=await run(8,'AFTER-LOGOUT');
  if(again.boards.length!==1||again.note!=='AFTER-LOGOUT')throw new Error('opening after logout did not load a fresh board');
})().catch(error=>{console.error(error&&error.stack||error);process.exit(1);});
'''
        completed=subprocess.run(['node','-e',harness],cwd=os.path.dirname(os.path.abspath(__file__)),capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20)
        self.assertEqual(completed.returncode,0,(completed.stderr or completed.stdout).strip())

    def test_date_switch_fetches_flags_only(self):
        client=test_auth_support.owner_client(app,self.base)
        with client.open(self.base+'/api/board',timeout=5) as response:
            board=json.load(response)
        day=board['today']
        account=next(row for row in board['accounts'] if row['id']=='43')
        request=urllib.request.Request(self.base+'/api/daily',data=json.dumps({**{'id':'43','date':day,'published':True}}).encode(),headers={'Content-Type':'application/json'})
        with client.open(request,timeout=5) as response:
            self.assertEqual(response.status,200)
        with client.open(self.base+'/api/daily-flags?date='+day,timeout=5) as response:
            flags=json.load(response)
        self.assertEqual(sorted(flags),['date','published'])
        self.assertEqual(flags['date'],day)
        self.assertTrue(flags['published']['43'])
        self.assertFalse(flags['published']['39'])
        self.assertNotIn(account['name'],json.dumps(flags))
        bad=urllib.request.Request(self.base+'/api/daily-flags?date=nope')
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            client.open(bad,timeout=5)
        self.assertEqual(rejected.exception.code,400)
        anonymous=urllib.request.Request(self.base+'/api/daily-flags?date='+day)
        with self.assertRaises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(anonymous,timeout=5)
        self.assertEqual(missing.exception.code,401)
        import subprocess
        harness=r'''
const fs=require('fs');const path=require('path');const vm=require('vm');
const appSource=fs.readFileSync(path.join(process.cwd(),'static','app.js'),'utf8');
const updatesSource=fs.readFileSync(path.join(process.cwd(),'static','board-updates.js'),'utf8');
const context={console,setImmediate};context.globalThis=context;context.window=context;context.__updatesSource=updatesSource;
vm.createContext(context);
const prelude=`
  var __boardFetches=[];var __calls=[];var __elements={};
  function __element(){return {value:'',hidden:false,textContent:'',innerHTML:'',open:false,checked:false,indeterminate:false,disabled:false,title:'',className:'',classList:{add(){},remove(){},toggle(){return false;}},style:{},dataset:{},addEventListener(){},removeEventListener(){},dispatchEvent(){},showModal(){this.open=true;},close(){this.open=false;},focus(){},querySelector(){return __element();},querySelectorAll(){return [];},appendChild(){},setAttribute(){},removeAttribute(){}};}
  document={visibilityState:'visible',body:{appendChild(node){if(node&&String(node.src||'').includes('board-updates.js')){(0,eval)(__updatesSource);}if(node&&typeof node.onload==='function')node.onload();}},addEventListener(){},dispatchEvent(){},createElement(){return {src:'',onload:null};},getElementById(id){if(!__elements[id])__elements[id]=__element();return __elements[id];},querySelector(){return __element();},querySelectorAll(){return [];}};
  location={search:'',href:'',replace(){}};
  CustomEvent=class CustomEvent{constructor(type){this.type=type;}};
  crypto={randomUUID:()=>'test-id',getRandomValues(bytes){return bytes;}};
  accountSearchFields=[];clearAccountSearch=()=>{};activeLifecyclePreset='';renderVerificationFilter=()=>{};
  applyVersionDelta=()=>{};versionPayload=(path,body)=>body;activeEditVersions=()=>null;
  fetch=async(url)=>{
    const target=String(url);__calls.push(target);
    if(target.includes('/api/daily-flags'))return {status:200,ok:true,json:async()=>({date:'2026-10-25',published:{'43':true,'39':false}})};
    if(target.includes('/api/board-updates'))return {status:200,ok:true,json:async()=>({version:'known'})};
    if(target.includes('/api/board')){__boardFetches.push(target);return {status:200,ok:true,json:async()=>({date:'2026-10-26',today:'2026-10-26',cursor:1,computers:[{id:'1',name:'电脑1'}],statuses:['正常'],accounts:[{id:'43',name:'jia',computer_id:'1',slot:1,status:'正常',note:'KEEP',published:false},{id:'39',name:'yi',computer_id:'1',slot:2,status:'正常',note:'OTHER',published:true}],collection:{auto_enabled:false,paused:false,worker_status:'',worker_online:false,pause_reason:'',linked:0,waiting:0,done:0,failed:0},user:{id:7,name:'user7'},backup:{},record_versions:{},phone_cards:{cards:[]},phone_devices:{devices:[]},publishing_layout:{enabled:false,cells:[]},nurture:{records:[]},offices:[]})};}
    return {status:404,ok:false,json:async()=>({error:'missing'})};
  };
  setInterval=()=>0;clearInterval=()=>{};setTimeout=(fn)=>{if(typeof fn==='function')fn();return 0;};clearTimeout=()=>{};confirm=()=>false;window.addEventListener=()=>{};window.removeEventListener=()=>{};
`;
const scenario=`
render=()=>{};
__ready=(async()=>{
  await new Promise(resolve=>setImmediate(()=>setImmediate(resolve)));
  const boards=__boardFetches.length;
  __elements.date.value='2026-10-25';
  await __elements.date.onchange();
  if(__boardFetches.length!==boards)throw new Error('date switch requested the full board');
  if(!__calls.some(item=>item.includes('/api/daily-flags?date=2026-10-25')))throw new Error('date switch did not request that day');
  const row=data.accounts.find(account=>account.id==='43');
  const other=data.accounts.find(account=>account.id==='39');
  if(!row||row.published!==true||row.name!=='jia'||row.note!=='KEEP')throw new Error('date switch replaced the account');
  if(!other||other.published!==false||other.name!=='yi')throw new Error('date switch changed another account body');
  if(data.accounts.length!==2)throw new Error('date switch changed the account list');
})();
`;
vm.runInContext(prelude+'\n'+appSource+'\n'+scenario,context);
context.__ready.then(()=>process.exit(0),error=>{console.error(error&&error.stack||error);process.exit(1);});
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


    def test_two_saves_remain_readable_after_restart(self):
        client=test_auth_support.owner_client(app,self.base)
        with app.connect() as db:
            start=db.execute('SELECT COALESCE(MAX(id),0) FROM operations_history').fetchone()[0]
        def save(ident,note):
            with client.open(self.base+'/api/board',timeout=5) as response:
                board=json.load(response)
            account=next(row for row in board['accounts'] if row['id']==ident)
            request=urllib.request.Request(self.base+'/api/account',data=json.dumps({**account,'note':note}).encode(),headers={'Content-Type':'application/json'})
            with client.open(request,timeout=5) as response:
                self.assertEqual(response.status,200)
        save('43','甲的备注')
        save('39','乙的备注')
        with app.connect() as db:
            before_reject=db.execute('SELECT COALESCE(MAX(id),0) FROM operations_history').fetchone()[0]
        stale=urllib.request.Request(self.base+'/api/account',data=json.dumps({'id':'43','note':'不会写上','_expected_versions':{'account:43':'stale-token'}}).encode(),headers={'Content-Type':'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            client.open(stale,timeout=5)
        self.assertEqual(rejected.exception.code,409)
        board_updates._pending=None
        board_updates._version=None
        owner={'role':'owner','office_ids':[]}
        staff={'role':'employee','office_ids':['office-a']}
        with app.connect() as db:
            self.assertEqual(db.execute("SELECT note FROM accounts WHERE id='43'").fetchone()[0],'甲的备注')
            self.assertEqual(db.execute("SELECT note FROM accounts WHERE id='39'").fetchone()[0],'乙的备注')
            self.assertEqual(db.execute('SELECT COALESCE(MAX(id),0) FROM operations_history').fetchone()[0],before_reject)
            found=board_updates.since(db,start,owner)
            hidden=board_updates.since(db,start,staff)
        accounts=[item['entity'] for item in found['changes'] if item['entity'].startswith('account:')]
        self.assertEqual(accounts,['account:43','account:39'])
        self.assertEqual(hidden['changes'],[])
        first,second=found['changes'][0],found['changes'][1]
        self.assertLess(first['cursor'],second['cursor'])
        self.assertEqual(first['kind'],'change')
        self.assertEqual(first['after']['note'],'甲的备注')
        self.assertEqual(second['after']['note'],'乙的备注')
        self.assertEqual(first['record_version'],operations.token(first['after']))
        self.assertEqual(first['offices'],[])
        with app.connect() as db:
            tail=board_updates.since(db,first['cursor'],owner)
        self.assertEqual([item['entity'] for item in tail['changes']],['account:39'])
        self.assertTrue(board_updates.change_visible({'role':'employee','office_ids':['office-a']},['office-a']))
        self.assertFalse(board_updates.change_visible({'role':'employee','office_ids':['office-a']},['office-b']))
        self.assertFalse(board_updates.change_visible({'role':'employee','office_ids':['office-a']},[]))
        self.assertTrue(board_updates.change_visible({'role':'owner','office_ids':[]},[]))

    def test_following_cursor_is_a_delta_and_a_missing_cursor_reloads(self):
        client=test_auth_support.owner_client(app,self.base)
        def current_cursor():
            with app.connect() as db:
                return db.execute('SELECT COALESCE(MAX(id),0) FROM operations_history').fetchone()[0]
        def save(ident,note):
            with client.open(self.base+'/api/board',timeout=5) as response:
                account=next(row for row in json.load(response)['accounts'] if row['id']==ident)
            request=urllib.request.Request(self.base+'/api/account',data=json.dumps({**account,'note':note}).encode(),headers={'Content-Type':'application/json'})
            with client.open(request,timeout=5) as response:
                self.assertEqual(response.status,200)
        def fetch(cursor):
            with client.open(self.base+'/api/board-updates?cursor='+str(cursor),timeout=5) as response:
                self.assertEqual(response.status,200)
                return json.load(response)
        save('43','先改甲')
        middle=current_cursor()
        save('39','再改乙')
        delta=fetch(middle)
        self.assertFalse(delta.get('reload'))
        self.assertEqual([item['entity'] for item in delta['changes']],['account:39'])
        self.assertNotIn('account:43',json.dumps(delta))
        with app.connect() as db:
            db.execute('DELETE FROM operations_history WHERE id=?',(middle,))
        missing=fetch(middle)
        self.assertTrue(missing.get('reload'))
        self.assertNotIn('changes',missing)
        kept=current_cursor()
        with app.connect() as db:
            board_updates.notify_reload(db)
        forced=fetch(kept)
        self.assertTrue(forced.get('reload'))
        self.assertNotIn('changes',forced)
        request=urllib.request.Request(self.base+'/api/board-updates?cursor=nope')
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request,timeout=5)
        self.assertEqual(error.exception.code,401)
        bad=urllib.request.Request(self.base+'/api/board-updates?cursor=nope')
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            client.open(bad,timeout=5)
        self.assertEqual(rejected.exception.code,400)

    def test_each_save_kind_is_visible_to_later_catch_up(self):
        client=test_auth_support.owner_client(app,self.base)
        def current_cursor():
            with app.connect() as db:
                return db.execute('SELECT COALESCE(MAX(id),0) FROM operations_history').fetchone()[0]
        def post(path,body):
            request=urllib.request.Request(self.base+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
            try:
                with client.open(request,timeout=5) as response:
                    self.assertEqual(response.status,200,path)
                    return json.load(response)
            except urllib.error.HTTPError as error:
                self.fail(path+' '+error.read().decode())
        start=current_cursor()
        with client.open(self.base+'/api/board',timeout=5) as response:
            day=json.load(response)['today']
        with app.connect() as db:
            db.execute("INSERT INTO publishing_layout_state(id,batch) VALUES(1,'synthetic-batch')")
            db.execute("INSERT INTO publishing_layout(batch,computer_id,slot,account_id,label,office,source_cell,source_url,reason) VALUES('synthetic-batch','1',1,NULL,'合成格子','合成办公室','A1','','待匹配')")
        post('/api/daily',dict(id='43',date=day,published=True))
        post('/api/bulk-status',dict(ids=['50'],status='待确认'))
        post('/api/phone-card',dict(phone='13800001111'))
        post('/api/phone-device/create',dict(id='8',state='在用'))
        post('/api/workspace/nurture/join',dict(id='43'))
        post('/api/publishing/link',dict(batch='synthetic-batch',computer_id='1',slot=1,account_id='43',expected_account_id=None))
        with client.open(self.base+'/api/board-updates?cursor='+str(start),timeout=5) as response:
            payload=json.load(response)
        self.assertFalse(payload.get('reload'))
        entities=[item['entity'] for item in payload['changes']]
        self.assertTrue(any(entity.startswith('daily:43:') for entity in entities),entities)
        self.assertIn('account:50',entities)
        self.assertTrue(any(entity.startswith('card:') for entity in entities),entities)
        self.assertIn('device:8',entities)
        self.assertIn('nurture:43',entities)
        self.assertTrue(any(entity.startswith('layout:synthetic-batch:1:1') for entity in entities),entities)


if __name__=='__main__':
    unittest.main()
