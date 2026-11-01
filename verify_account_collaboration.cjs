// Reviewed outer composition runner. Real project JS + loopback HTTP only.
// The Python child installs the unchanged synthetic isolation before imports.
// Synthetic DOM is reused, not a browser. Session material remains in memory.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),http=require('node:http');
const assert=require('node:assert/strict'),{spawn}=require('node:child_process');
const {harness:baseHarness,root}=require('./test_performance_ui.cjs');
const args=process.argv.slice(2);
assert.equal(args.length,4,'Use --root <new synthetic root> --python <absolute local python.exe>');
assert.equal(args[0],'--root');assert.equal(args[2],'--python');
const runRoot=path.resolve(root,args[1]),syntheticRoot=path.join(root,'.synthetic-tests')+path.sep;
assert.ok(runRoot.startsWith(syntheticRoot)&&runRoot!==syntheticRoot.slice(0,-1));
assert.ok(path.isAbsolute(args[3])&&path.basename(args[3]).toLowerCase()==='python.exe');
const child=spawn(args[3],['-B',path.join(root,'verify_account_collaboration_fixture.py'),'--root',runRoot],
 {cwd:root,stdio:['pipe','pipe','pipe'],windowsHide:true,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
let buffer='',dead=false,exitCode=null;const messages=[],waiters=[],requests=[],pages=[];
let fixtureError=null;
const deliver=value=>{messages.push(value);for(const wake of [...waiters])wake();};
child.stdout.setEncoding('utf8');child.stdout.on('data',chunk=>{buffer+=chunk;for(;;){const at=buffer.indexOf('\n');if(at<0)break;const line=buffer.slice(0,at);buffer=buffer.slice(at+1);try{const value=JSON.parse(line);if(value.kind==='fixture_error')fixtureError=Error('Synthetic fixture failed ('+value.type+')');deliver(value);}catch{fixtureError=Error('Invalid private fixture protocol');deliver({kind:'protocol_error'});}}});
// Discard arbitrary child diagnostics; they must never become credential logs.
child.stderr.on('data',()=>{});
child.on('error',()=>{fixtureError=Error('Unable to start reviewed local fixture');dead=true;deliver({kind:'child_error'});});
child.on('exit',code=>{dead=true;exitCode=code;deliver({kind:'child_exit'});});
function receive(kind,after=()=>true,timeout=15000){return new Promise((resolve,reject)=>{
 let timer;const inspect=()=>{if(fixtureError)return done(fixtureError);const value=messages.find(item=>item.kind===kind&&after(item));if(value)return done(null,value);if(dead)return done(Error('Synthetic fixture exited before verification'));};
 const done=(error,value)=>{clearTimeout(timer);const at=waiters.indexOf(inspect);if(at>=0)waiters.splice(at,1);error?reject(error):resolve(value);};
 waiters.push(inspect);timer=setTimeout(()=>done(Error('Isolated verification coordination timed out')),timeout);inspect();
});}
const command=value=>child.stdin.write(JSON.stringify(value)+'\n');
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(check,timeout=8000){const end=Date.now()+timeout;while(!check()){if(Date.now()>end)throw Error('Frontend did not settle within verification deadline');await sleep(10);}}
function transport(base,session){
 const origin=new URL(base);assert.equal(origin.protocol,'http:');assert.equal(origin.hostname,'127.0.0.1');assert.ok(Number(origin.port)>0);
 return (url,options={})=>new Promise((resolve,reject)=>{
  if(typeof url!=='string'||!url.startsWith('/api/')||url.startsWith('//'))return reject(Error('Only reviewed loopback API paths are allowed'));
  const target=new URL(url,origin);if(target.origin!==origin.origin)return reject(Error('External network blocked'));
  const method=options.method||'GET',headers={...options.headers,Cookie:session.cookie};
  if(method==='POST'){headers.Origin=origin.origin;headers['Content-Length']=Buffer.byteLength(options.body||'');}
  const req=http.request({hostname:'127.0.0.1',port:Number(origin.port),path:target.pathname+target.search,method,headers,agent:false},res=>{
   const chunks=[];res.on('data',chunk=>chunks.push(chunk));res.on('end',()=>{
    const body=Buffer.concat(chunks);requests.push({path:target.pathname,method,status:res.statusCode,bytes:body.length});
    resolve({ok:res.statusCode>=200&&res.statusCode<300,status:res.statusCode,headers:{get:key=>res.headers[key.toLowerCase()]},json:async()=>JSON.parse(body.toString('utf8'))});
   });res.on('error',()=>reject(Error('Loopback response failed')));
  });
  const abort=()=>req.destroy(Object.assign(Error('Synthetic read cancelled'),{name:'AbortError'}));
  if(options.signal?.aborted){abort();return;}
  options.signal?.addEventListener('abort',abort,{once:true});
  req.on('close',()=>options.signal?.removeEventListener('abort',abort));
  req.on('error',error=>reject(error.name==='AbortError'?error:Error('Loopback request failed')));
  req.setTimeout(12000,()=>req.destroy(Error('Loopback request timeout')));req.end(options.body);
 });
}
async function makePage(config,key){
 const p=baseHarness(root),events=new Map();p.context.AbortController=AbortController;
 p.context.setTimeout=setTimeout;p.context.clearTimeout=clearTimeout;
 const register=p.context.document.addEventListener;p.context.document.addEventListener=(type,fn,options)=>{register(type,fn,options);if(!events.has(type))events.set(type,[]);events.get(type).push(fn);};
 p.context.document.activeElement=null;p.context.document.visibilityState='visible';
 const query=p.context.document.querySelector;p.context.document.querySelector=selector=>selector==='dialog[open]'?[...p.elements.values()].find(item=>item.open)||null:query(selector);
 p.context.hasPermission=permission=>p.run('data?.user?.permissions||[]').includes(permission);
 p.context.fetch=transport(config.base,config.sessions[key]);
 p.context.location.replace=()=>{throw Error('Unexpected login redirect during isolated verification');};
 const body=p.element('accountsBody'),descriptor=Object.getOwnPropertyDescriptor(body,'innerHTML');
 Object.defineProperty(body,'innerHTML',{get:descriptor.get,set(value){if(p.context.document.activeElement?.id==='accountInlineInput')p.context.document.activeElement=null;descriptor.set.call(body,value);}});
 for(const field of ['name','status','note']){
  const holder=p.element('account-inline-'+field+'-'+encodeURIComponent(config.accountId)),cell=Object.getOwnPropertyDescriptor(holder,'innerHTML');
  holder.dataset={inlineCell:config.accountId,inlineField:field};
  Object.defineProperty(holder,'innerHTML',{get:cell.get,set(value){cell.set.call(holder,value);if(String(value).includes('id="accountInlineInput"')){
   const input=p.element('accountInlineInput');input.disabled=/id="accountInlineInput"[^>]*\bdisabled\b/.test(value);input.tagName=field==='note'?'TEXTAREA':field==='status'?'SELECT':'INPUT';
   input.focus=()=>{p.context.document.activeElement=input;};input.setSelectionRange=()=>{};input.closest=selector=>selector==='[data-inline-cell]'?holder:null;
  }}});
 }
 for(const name of ['account-inline.js','board-updates.js'])vm.runInContext(fs.readFileSync(path.join(root,'static',name),'utf8'),p.context,{filename:name});
 const response=await p.context.fetch('/api/board?version_scope=day&compact=accounts&date='+config.date);assert.equal(response.status,200);const board=await response.json();
 p.reset(board);p.run("activeOffice='three'");p.show(board);
 p.component=p.context.AccountInline;p.begin=field=>p.component.begin(config.accountId,field);
 p.emit=(type,extra={})=>{const event={target:p.element('accountInlineInput'),preventDefault(){},stopImmediatePropagation(){},...extra};for(const fn of events.get(type)||[])fn(event);};
 p.type=value=>{p.element('accountInlineInput').value=value;p.emit('input');};
 p.sync=()=>p.run('catchUpBoard()');p.row=()=>JSON.parse(p.run('JSON.stringify(data.accounts.find(item=>item.id==="old-three"))'));
 pages.push(p);return p;
}
let checks=0,currentCase='initialization';
async function check(name,job){currentCase=name;await job();checks++;console.log('PASS '+name);}
(async()=>{
 let evidence;
 try{
  const config=await receive('ready');const a=await makePage(config,'a'),b=await makePage(config,'b'),foreign=await makePage(config,'foreign');
  assert.notEqual(a.run('data.user.id'),b.run('data.user.id'),'independent authenticated users');
  assert.notEqual(a.run('data.user.id'),foreign.run('data.user.id'),'independent out-of-office user');
  let observedWait=0;
  await check('real name Enter autosave wakes the other HTTP session and updates its actual board',async()=>{
   await a.begin('name');a.type('合成联调昵称');const waiting=receive('waiting',value=>value.count>observedWait);const peer=b.sync();observedWait=(await waiting).count;
   a.emit('keydown',{key:'Enter'});await until(()=>!a.component.hasDraft());assert.equal((await peer).status,'updated');assert.equal(b.row().name,'合成联调昵称');assert.ok(b.element('accountsBody').innerHTML.includes('合成联调昵称'));await a.sync();assert.equal(a.row().name,'合成联调昵称');
  });
  await check('real note Ctrl+Enter and status blur autosaves preserve canonical values in both sessions',async()=>{
   await a.begin('note');a.type('合成联调备注\n第二行');a.emit('keydown',{key:'Enter',ctrlKey:true});await until(()=>!a.component.hasDraft());await b.sync();assert.equal(b.row().note,'合成联调备注\n第二行');assert.ok(b.element('accountsBody').innerHTML.includes('合成联调备注'));await a.sync();
   await a.begin('status');a.type('封禁');a.emit('focusout',{relatedTarget:null});await until(()=>!a.component.hasDraft());await b.sync();assert.equal(b.row().status,'封禁');assert.ok(b.element('accountsBody').innerHTML.includes('封禁'));await a.sync();assert.equal(a.row().status,'封禁');
  });
  await check('real stale concurrent save returns 409 and retains the typed draft until explicit cancel',async()=>{
   await b.begin('name');b.type('保留我的并发草稿');await a.begin('note');a.type('合成另一人修改');assert.equal(await a.component.save(),true);
   const before=requests.length;assert.equal((await b.sync()).status,'deferred');assert.equal(requests.length,before);assert.equal(await b.component.save(),false);
   assert.equal(b.component.getState().phase,'conflict');assert.equal(b.component.getState().value,'保留我的并发草稿');assert.equal(b.element('accountInlineInput').value,'保留我的并发草稿');assert.ok(requests.some(item=>item.method==='POST'&&item.path==='/api/account/patch'&&item.status===409));
   assert.equal(b.component.cancel(),true);await b.sync();assert.equal(b.row().name,'合成联调昵称');assert.equal(b.row().note,'合成另一人修改');await a.sync();
  });
  await check('an unsaved draft defers remote changes and catches up after cancel and after a later save',async()=>{
   await b.begin('note');b.type('我未保存的备注草稿');await a.begin('name');a.type('合成联调最终昵称');assert.equal(await a.component.save(),true);
   const before=requests.length;assert.equal((await b.sync()).status,'deferred');assert.equal(requests.length,before);assert.equal(b.component.getState().value,'我未保存的备注草稿');assert.equal(b.row().name,'合成联调昵称');
   assert.equal(b.component.cancel(),true);await b.sync();assert.equal(b.row().name,'合成联调最终昵称');await a.sync();
   await b.begin('note');b.type('合成保存后追赶备注');assert.equal(await b.component.save(),true);await b.sync();await a.sync();assert.equal(a.row().note,'合成保存后追赶备注');assert.equal(b.row().note,'合成保存后追赶备注');
  });
  await check('real out-of-office read and patch remain forbidden without exposing account data',async()=>{
   assert.equal(foreign.run('data.accounts.some(row=>row.id==="old-three")'),false);
   await assert.rejects(foreign.run('api("/api/account/inline?id=old-three",undefined,{applyVersions:false})'),error=>error.status===403);
   foreign.context.probe={id:config.accountId,changes:{note:'越界修改不得保存'},request_id:require('node:crypto').randomUUID(),return_account:true,_expected_versions:{}};
   await assert.rejects(foreign.run('api("/api/account/patch",probe,{fixedPayload:true,applyVersions:false})'),error=>error.status===403);
  });
  command({command:'verify'});const audit=await receive('audit');
  evidence={passed:true,checks,kind:'actual-frontend-VM-to-real-loopback-HTTP-synthetic-SQLite-PG-history-projection',production:false,real_browser:false,
   successful_account_changes:audit.successful_account_changes,unchanged_other_tables:audit.unchanged_other_tables,waits_observed:audit.waits_observed,
   response_status_counts:requests.reduce((counts,item)=>(counts[item.status]=(counts[item.status]||0)+1,counts),{}),requests:requests.length};
  fs.writeFileSync(path.join(runRoot,'composition-evidence.json'),JSON.stringify(evidence,null,2));
  console.log(JSON.stringify(evidence));
 }finally{
  for(const p of pages)p.run('window.boardLiveSync?.stop()');
  if(!dead){command({command:'stop'});child.stdin.end();await Promise.race([new Promise(resolve=>child.once('exit',resolve)),sleep(7000)]);if(!dead)child.kill();}
 }
 if(exitCode!==0)throw Error('Synthetic fixture cleanup did not complete successfully');
})().catch(error=>{console.error('FAIL isolated JS/HTTP composition: '+currentCase+' ['+error.name+']');process.exitCode=1;});
