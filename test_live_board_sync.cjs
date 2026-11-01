// Real board scripts, shared scheduler, synthetic DOM/timers/fetch only.
// No browser profile, credentials, external network, database or business files.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const {harness:baseHarness,fixture,root}=require('./test_performance_ui.cjs');
const clone=value=>structuredClone(value),plain=value=>JSON.parse(JSON.stringify(value));
const response=(payload,status=200)=>({status,ok:status>=200&&status<300,json:async()=>clone(payload)});
const deferred=()=>{let resolve,reject;const promise=new Promise((ok,no)=>{resolve=ok;reject=no;});return {promise,resolve,reject};};
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function harness(count=125){
 const p=baseHarness(root),events=new Map(),docEvents=new Map(),timers=new Map();let now=100000,id=0;
 const add=(map,type,fn)=>{if(!map.has(type))map.set(type,new Set());map.get(type).add(fn);};
 p.context.Date=class extends Date{static now(){return now;}};
 p.context.setTimeout=(fn,delay)=>{timers.set(++id,{fn,at:now+delay});return id;};p.context.clearTimeout=key=>timers.delete(key);
 p.context.AbortController=AbortController;
 p.context.addEventListener=(type,fn)=>add(events,type,fn);p.context.removeEventListener=(type,fn)=>events.get(type)?.delete(fn);
 p.context.document.visibilityState='visible';p.context.document.activeElement=null;
 p.context.document.addEventListener=(type,fn)=>add(docEvents,type,fn);p.context.document.removeEventListener=(type,fn)=>docEvents.get(type)?.delete(fn);
 const originalQuery=p.context.document.querySelector;p.context.document.querySelector=selector=>selector==='dialog[open]'?[...p.elements.values()].find(item=>item.open)||null:originalQuery(selector);
 p.server=fixture(count);p.delta={cursor:10,changes:[]};p.context.redirect=null;p.context.location.replace=url=>{p.context.redirect=url;};
 p.context.fetch=async(url,options={})=>{p.calls.push({url,options});if(p.respond)return p.respond(url,options);if(url.startsWith('/api/board-updates?'))return response(p.delta);if(url.startsWith('/api/board?'))return response(p.server);if(options.method==='POST')return response({ok:true});throw Error('Unexpected synthetic request '+url);};
 p.reset(clone(p.server));p.show(clone(p.server));
 vm.runInContext(fs.readFileSync(path.join(root,'static/board-updates.js'),'utf8'),p.context,{filename:'board-updates.js'});
 p.fullLoads=()=>p.calls.filter(call=>/^\/api\/board(?:\?|$)/.test(call.url)).length;
 p.sync=()=>p.run('catchUpBoard()');p.state=()=>plain(p.run('data'));
 p.emit=async(type,doc=false)=>{for(const fn of (doc?docEvents:events).get(type)||[])await fn();await flush();};
 p.advance=async ms=>{now+=ms;let due=[...timers].filter(([,timer])=>timer.at<=now);for(const [key,timer]of due){if(!timers.has(key))continue;timers.delete(key);timer.fn();await flush();}};
 p.enable=async()=>{vm.runInContext(fs.readFileSync(path.join(root,'static/live-sync.js'),'utf8'),p.context,{filename:'live-sync.js'});p.run('startBoardLiveSync()');await flush();};
 p.stop=()=>p.run('window.boardLiveSync?.stop()');return p;
}
const noteChange=(note='remote')=>({entity:'account:account-0',after:{id:'account-0',note},record_version:'account-v1'});
let checks=0;async function test(name,fn){await fn();checks++;console.log('ok '+name);}
(async()=>{
 await test('two synthetic pages see a committed daily write through a bounded delta without a full read',async()=>{
  const a=harness(3),b=harness(3);a.context.fetch=async(url,options)=>{a.calls.push({url,options});assert.equal(url,'/api/daily');const body=JSON.parse(options.body);b.delta={cursor:11,changes:[{entity:'daily:'+body.id+':'+body.date,after:{published:body.published?1:0},record_version:'daily-v1'}]};return response({ok:true,daily:{id:body.id,date:body.date,published:body.published},_record_versions:{['daily:'+body.id+':'+body.date]:'daily-v1'}});};
  await a.run('toggleDaily("account-0")');await b.sync();assert.equal(b.state().accounts[0].published,false);assert.match(b.calls[0].url,/cursor=10&limit=200&wait=5$/);assert.equal(b.fullLoads(),0);
 });
 await test('steady visible page checks every five seconds and duplicate focus calls share one flight',async()=>{
  const p=harness(3);await p.enable();assert.equal(p.calls.length,1);p.delta={cursor:11,changes:[noteChange()]};await p.advance(5000);assert.equal(p.state().accounts[0].note,'remote');assert.equal(p.fullLoads(),0);
  const waiting=deferred();p.respond=()=>waiting.promise;const one=p.emit('focus'),two=p.emit('focus');await flush();assert.equal(p.calls.length,3);waiting.resolve(response({cursor:11,changes:[]}));await Promise.all([one,two]);p.stop();
 });
 await test('unchanged idle polls never render or rewrite the full view cache',async()=>{
  const p=harness(125);let writes=0;const set=p.context.localStorage.setItem;p.context.localStorage.setItem=(...args)=>{writes++;return set(...args);};p.run('globalThis.syntheticRenders=0;globalThis.syntheticRealRender=render;render=()=>{syntheticRenders++;syntheticRealRender();};');
  await p.enable();for(let i=0;i<3;i++)await p.advance(5000);assert.equal(writes,0);assert.equal(p.run('syntheticRenders'),0);assert.equal(p.fullLoads(),0);
  p.delta={cursor:11,changes:[]};await p.advance(5000);assert.equal(writes,1,'a changed invisible cursor may be cached once');assert.equal(p.run('syntheticRenders'),0);p.stop();
 });
 await test('legacy focus hook delegates to the shared listener and cannot bypass failure backoff',async()=>{
  const p=harness(3);p.respond=()=>response({},500);await p.enable();assert.equal(p.calls.length,1);assert.equal(p.run('boardLiveSync.getState().phase'),'error');
  for(let i=0;i<5;i++){await p.run('refreshFromFocus()');await p.emit('focus');}assert.equal(p.calls.length,1);await p.advance(10000);assert.equal(p.calls.length,2);p.stop();
 });
 await test('timeout releases an abort-ignoring delta or JSON flight and its late finally cannot clear a newer flight',async()=>{
  for(const parse of [false,true]){const p=harness(3),old=deferred(),fresh=deferred();let reads=0;p.respond=()=>++reads===1?(parse?{ok:true,status:200,json:()=>old.promise}:old.promise):fresh.promise;
   await p.enable();await p.advance(15000);assert.equal(p.run('boardLiveSync.getState().phase'),'error');await p.advance(10000);assert.equal(reads,2);
   old.resolve(parse?{cursor:99,changes:[noteChange('obsolete')]}:response({cursor:99,changes:[noteChange('obsolete')]}));await flush();assert.equal(p.state().cursor,10);
   const duplicate=p.sync();assert.equal(reads,2,'old finally must not clear the newer adapter flight');fresh.resolve(response({cursor:11,changes:[noteChange('fresh')]}));await duplicate;await flush();assert.equal(p.state().cursor,11);assert.equal(p.state().accounts[0].note,'fresh');p.stop();
  }
 });
 await test('timeout also releases background snapshot loading without letting an abort-ignoring snapshot overwrite fresh data',async()=>{
  const p=harness(3),old=deferred();let reads=0;p.delta={cursor:12,changes:[{entity:'unit:u',after:{id:'u'}}]};p.server.cursor=12;
  p.respond=url=>url.startsWith('/api/board-updates?')?response(p.delta):++reads===1?old.promise:response(p.server);
  await p.enable();assert.equal(p.run('dailyViewLoading')>0,true);await p.advance(15000);assert.equal(p.run('dailyViewLoading'),0);assert.equal(p.run('boardBackgroundReads'),0);await p.advance(10000);assert.equal(reads,2);assert.equal(p.state().cursor,12);old.resolve(response({...p.server,cursor:99}));await flush();assert.equal(p.state().cursor,12);p.stop();
 });
 await test('dialog and focused input defer the complete batch, preserve captured tokens and resume after close',async()=>{
  const p=harness(3);p.delta={cursor:11,changes:[noteChange()]};p.run('captureVersions("editor")');p.element('editor').open=true;const before=p.state();assert.equal((await p.sync()).status,'deferred');assert.equal(p.calls.length,0);assert.deepEqual(p.state(),before);
  p.element('editor').open=false;p.context.document.activeElement={tagName:'INPUT'};assert.equal((await p.sync()).status,'deferred');p.context.document.activeElement=null;await p.sync();assert.equal(p.state().accounts[0].note,'remote');assert.equal(p.run('capturedVersions.get("editor")["account:account-0"]'),'synthetic-account-v0');
 });
 await test('a focused computer filter or row checkbox keeps polling while preserving filter and selected rows',async()=>{
  for(const checkbox of [false,true]){const p=harness(12);p.element('computer').value='pc-0';p.run('selected.add("account-0");render()');p.context.document.activeElement=checkbox?{tagName:'INPUT',type:'checkbox',dataset:{select:'account-0'}}:{tagName:'SELECT',id:'computer'};
   await p.enable();p.delta={cursor:11,changes:[noteChange('while filtered')]};await p.advance(5000);assert.equal(p.state().accounts[0].note,'while filtered');assert.equal(p.element('computer').value,'pc-0');assert.equal(p.run('selected.has("account-0")'),true);assert.equal(p.fullLoads(),0);p.stop();
  }
 });
 await test('dialog opened during fetch or JSON parsing never applies data or advances its cursor',async()=>{
  for(const parse of [false,true]){const p=harness(3),wait=deferred();p.respond=()=>parse?{ok:true,status:200,json:()=>wait.promise}:wait.promise;const job=p.sync();await flush();p.element('editor').open=true;p.run('boardSyncInteraction()');wait.resolve(parse?{cursor:11,changes:[noteChange()]}:response({cursor:11,changes:[noteChange()]}));assert.equal((await job).status,'deferred');assert.equal(p.state().cursor,10);assert.equal(p.state().accounts[0].note,undefined);p.element('editor').open=false;p.respond=null;p.delta={cursor:11,changes:[noteChange()]};await p.sync();assert.equal(p.state().cursor,11);}
 });
 await test('an edit opened and closed while a read waits still invalidates that old response',async()=>{
  const p=harness(3),wait=deferred();p.respond=()=>wait.promise;const job=p.sync();p.element('editor').open=true;p.run('boardSyncInteraction()');p.element('editor').open=false;wait.resolve(response({cursor:11,changes:[noteChange()]}));assert.equal((await job).status,'waiting');assert.equal(p.state().cursor,10);
 });
 await test('a local POST completed before an old poll response still prevents stale state and token replacement',async()=>{
  const p=harness(3),wait=deferred();p.respond=(url,options)=>options.method==='POST'?response({ok:true,_record_versions:{'account:account-0':'local-v2'}}):wait.promise;
  const poll=p.sync();await p.run('api("/api/account/status",{id:"account-0",status:"正常"})');wait.resolve(response({cursor:11,changes:[noteChange('stale')]}));assert.equal((await poll).status,'waiting');assert.equal(p.state().cursor,10);assert.equal(p.state().record_versions['account:account-0'],'local-v2');
 });
 await test('ordinary saves block reads while pending and completion permits a fresh incremental read',async()=>{
  const p=harness(3),wait=deferred();p.respond=(url,options)=>options.method==='POST'?wait.promise:response({cursor:11,changes:[noteChange('after save')]});
  const saving=p.run('api("/api/account/status",{id:"account-0",status:"正常"})');assert.equal((await p.sync()).status,'deferred');assert.equal(p.calls.length,1);wait.resolve(response({ok:true}));await saving;await p.sync();assert.equal(p.state().accounts[0].note,'after save');
 });
 await test('running and unknown-result daily requests and uncertain UUID writes block synchronization',async()=>{
  for(const value of ['{running:Promise.resolve()}','{running:null}']){const p=harness(3);p.run('dailyRequests.set("synthetic",'+value+')');assert.equal((await p.sync()).status,'deferred');assert.equal(p.calls.length,0);}
  const p=harness(3);p.run('pendingRequestIds.set("synthetic","fixed-request")');assert.equal((await p.sync()).status,'deferred');assert.equal(p.calls.length,0);
 });
 await test('date switches, manual loads and controller lifecycle invalidate late delta responses',async()=>{
  for(const mode of ['date','load','lifecycle']){const p=harness(3),wait=deferred();p.respond=url=>url.startsWith('/api/board-updates?')?wait.promise:response({...p.server,cursor:20});const current={valid:true};p.context.syntheticCurrent=()=>current.valid;const job=p.run('catchUpBoard({isCurrent:syntheticCurrent})');if(mode==='date')p.run('data.date="2026-10-28";requestSerial++');else if(mode==='load')await p.run('load()');else current.valid=false;wait.resolve(response({cursor:11,changes:[noteChange('late')]}));assert.equal((await job).status,'waiting');assert.equal(p.state().accounts[0].note,undefined);assert.equal(p.state().cursor,mode==='load'?20:10);}
 });
 await test('complex mixed updates reload once, keep filters, selection and pagination, and carry cancellation signal',async()=>{
  const p=harness();p.element('accountsNext').onclick();p.run('selected.add("account-51")');p.element('query').value='';p.server.cursor=12;p.server.accounts[0].status='封禁';p.delta={cursor:12,changes:[noteChange('not partial'),{entity:'account:account-0',after:{id:'account-0',status:'封禁'}}]};const abort=new AbortController();p.context.syntheticSignal=abort.signal;assert.equal((await p.run('catchUpBoard({signal:syntheticSignal,isCurrent:()=>true})')).status,'updated');assert.equal(p.fullLoads(),1);assert.equal(p.state().cursor,12);assert.equal(p.state().accounts[0].status,'封禁');assert.equal(p.run('selected.has("account-51")'),true);assert.equal(p.run('accountPageIndex'),2);assert.ok(p.calls.every(call=>call.options.signal===abort.signal));
 });
 await test('shared scheduler accepts its own guarded snapshot read but rejects one spanning a completed write',async()=>{
  const p=harness(3);p.server.cursor=12;p.delta={cursor:12,changes:[{entity:'unit:u',after:{id:'u'}}]};await p.enable();assert.equal(p.state().cursor,12);assert.equal(p.fullLoads(),1);assert.equal(p.run('boardLiveSync.getState().phase'),'synced');p.stop();
  const q=harness(3),wait=deferred();q.delta=p.delta;q.respond=(url,options)=>options.method==='POST'?response({ok:true}):url.startsWith('/api/board-updates?')?response(q.delta):wait.promise;
  const reading=q.sync();await flush();await q.run('api("/api/account/status",{id:"account-0",status:"正常"})');wait.resolve(response({...q.server,cursor:12}));assert.equal((await reading).status,'waiting');assert.equal(q.state().cursor,10);
 });
 await test('editor opening while a complex snapshot loads blocks the late snapshot and retries from original cursor',async()=>{
  const p=harness(3),wait=deferred();p.delta={cursor:12,changes:[{entity:'unit:u',after:{id:'u'}}]};p.respond=url=>url.startsWith('/api/board-updates?')?response(p.delta):wait.promise;const job=p.sync();await flush();p.element('editor').open=true;p.run('boardSyncInteraction()');wait.resolve(response({...p.server,cursor:12}));assert.equal((await job).status,'deferred');assert.equal(p.state().cursor,10);p.element('editor').open=false;p.respond=null;p.server.cursor=12;await p.sync();assert.equal(p.state().cursor,12);assert.equal(p.fullLoads(),2);
 });
 await test('other-date changes advance cursor without rendering or attaching unrelated historical daily versions',async()=>{
  const p=harness(3);p.run('render=()=>{throw Error("must not redraw")};');p.delta={cursor:11,changes:[{entity:'daily:account-0:2026-10-28',after:{published:1},record_version:'past'}]};assert.equal((await p.sync()).status,'idle');assert.equal(p.state().cursor,11);assert.equal(p.state().record_versions['daily:account-0:2026-10-28'],undefined);
 });
 await test('404 old-server fallback is limited to one snapshot per minute and still protects edits',async()=>{
  const p=harness(3);p.respond=url=>url.startsWith('/api/board-updates?')?response({},404):response(p.server);await p.sync();await p.sync();assert.equal(p.fullLoads(),1);p.element('editor').open=true;await p.sync();assert.equal(p.fullLoads(),1);p.element('editor').open=false;await p.advance(60000);await p.sync();assert.equal(p.fullLoads(),2);
 });
 await test('401 clears only the view cache and redirects; forbidden/server/malformed responses never advance cursor',async()=>{
  for(const status of [401,403,500]){const p=harness(3);p.respond=()=>response({},status);await assert.rejects(p.sync(),error=>error.status===status);assert.equal(p.state().cursor,10);if(status===401){assert.equal(p.context.localStorage.getItem('douyin-board-view'),null);assert.equal(p.context.redirect,'/login.html');}}
  for(const payload of [null,{}, {cursor:9,changes:[]},{cursor:11,changes:'invalid'}]){const p=harness(3);p.respond=()=>response(payload);await assert.rejects(p.sync(),/格式/);assert.equal(p.state().cursor,10);}
 });
 await test('hidden pages pause and resume on visibility without a second full-board polling loop',async()=>{
  const p=harness(3);await p.enable();p.context.document.visibilityState='hidden';await p.emit('visibilitychange',true);await p.advance(30000);assert.equal(p.calls.length,1);p.delta={cursor:11,changes:[noteChange()]};p.context.document.visibilityState='visible';await p.emit('visibilitychange',true);assert.equal(p.state().accounts[0].note,'remote');assert.equal(p.fullLoads(),0);p.stop();
 });
 console.log(JSON.stringify({passed:true,checks,kind:'actual-board-and-scheduler-synthetic-only'}));
})().catch(error=>{console.error(error);process.exitCode=1;});
