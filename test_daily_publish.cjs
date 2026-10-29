// Real frontend functions, synthetic DOM/fetch/storage only. No external I/O.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const assert=require('node:assert/strict'),crypto=require('node:crypto');
const source=name=>fs.readFileSync(path.join(__dirname,'static',name),'utf8');
const original=source('app.js'),controlled=original.replace(';openBoard();',';');
assert.notEqual(original,controlled,'control startup without replacing the real API/click functions');
const day='2026-10-29',otherDay='2026-10-28',id='synth-account-1';
const key=date=>'daily:'+id+':'+date;
const clone=value=>JSON.parse(JSON.stringify(value));
const response=(body,status=200)=>({ok:status>=200&&status<300,status,json:async()=>clone(body)});
const deferred=()=>{let resolve,reject;const promise=new Promise((ok,no)=>{resolve=ok;reject=no;});return {promise,resolve,reject};};

function harness(published=false){
 const elements=new Map(),storage=new Map(),listeners=new Map(),calls=[];
 const element=name=>{if(!elements.has(name))elements.set(name,{value:'',textContent:'',hidden:false,open:false,innerHTML:'',classList:{add(){},remove(){},toggle(){}},addEventListener(){}});return elements.get(name);};
 const buttons=Array.from({length:2},()=>({dataset:{publish:id},disabled:false,attrs:{},setAttribute(name,value){this.attrs[name]=value;}}));
 const board={cursor:10,date:day,today:day,user:{id:1,csrf:'synthetic-csrf',permissions:['publish']},
  accounts:[{id,name:'合成账号',status:'正常',published}],computers:[],statuses:['正常'],
  phone_cards:{cards:[]},phone_devices:{devices:[]},device_workflow:{enabled:true,units:[],access:[],placements:[],cases:[],events:[]},
  record_versions:{['account:'+id]:'account-v0',[key(day)]:'daily-v0'}};
 const server={flags:{[day]:published,[otherDay]:false},otherFlags:{},versions:clone(board.record_versions),records:new Map(),revision:0};
 const page={elements,storage,listeners,calls,buttons,server,updates:{cursor:10,changes:[]}};
 const context={URLSearchParams,console,crypto:crypto.webcrypto,currentUser:board.user,accountSearchFields:[],
  setTimeout:()=>0,clearTimeout(){},setInterval(){},location:{search:'?page=board',replace(){}},history:{replaceState(){}},
  document:{getElementById:element,addEventListener:(name,handler)=>listeners.set(name,handler),
   createElement:()=>({}),body:{appendChild(){}},querySelectorAll:selector=>selector==='[data-publish]'?buttons:[],dispatchEvent(){}},
  localStorage:{getItem:name=>storage.get(name)??null,setItem:(name,value)=>storage.set(name,value),removeItem:name=>storage.delete(name)},
  addEventListener(){},fixture:board,renders:0};
 context.window=context;
 page.save=body=>{
  const found=server.records.get(body.request_id),fingerprint=JSON.stringify(body);
  if(found)return found.fingerprint===fingerprint?response(found.result):response({error:'相同请求标识内容不同',code:'REQUEST_ID_REUSED'},409);
  if(body.id===id)server.flags[body.date]=body.published;else server.otherFlags['daily:'+body.id+':'+body.date]=body.published;
  const dailyKey='daily:'+body.id+':'+body.date;server.versions[dailyKey]='daily-v'+(++server.revision);
  const result={ok:true,daily:{id:body.id,date:body.date,published:body.published},_record_versions:{['account:'+body.id]:server.versions['account:'+body.id],[dailyKey]:server.versions[dailyKey]}};
  server.records.set(body.request_id,{fingerprint,result:clone(result)});return response(result);
 };
 page.snapshot=date=>clone({...board,date,accounts:board.accounts.map(row=>({...row,published:row.id===id?server.flags[date]??false:server.otherFlags['daily:'+row.id+':'+date]??false})),record_versions:server.versions});
 context.fetch=async(url,options={})=>{
  const body=options.body?JSON.parse(options.body):null;calls.push({url,body,options});
  if(url==='/api/daily')return page.onDaily?page.onDaily(body):page.save(body);
  if(url.startsWith('/api/idempotency/result?')){const request=new URLSearchParams(url.split('?')[1]).get('request_id');if(page.onResult)return page.onResult(request);const stored=server.records.get(request);return stored?response({...stored.result,code:'REQUEST_SUCCEEDED'}):response({error:'没有请求记录',code:'REQUEST_NOT_FOUND'},404);}
  if(url.startsWith('/api/record-versions?')){if(page.onVersions)return page.onVersions(url);const keys=new URLSearchParams(url.split('?')[1]).get('keys').split(',');return response({record_versions:Object.fromEntries(keys.map(name=>[name,server.versions[name]??null]))});}
  if(url.startsWith('/api/daily-flags?')){if(page.onFlags)return page.onFlags(url);const date=new URLSearchParams(url.split('?')[1]).get('date');return response({date,published:{[id]:server.flags[date]??false}});}
  if(url.startsWith('/api/board-updates?'))return response(page.updates);
  if(url.startsWith('/api/board?')){if(page.onBoard)return page.onBoard(url);const date=new URLSearchParams(url.split('?')[1]).get('date');return response(page.snapshot(date));}
  throw Error('Unexpected synthetic request: '+url);
 };
 vm.createContext(context);
 for(const name of ['edit-versions.js','app.js','board-updates.js'])vm.runInContext(name==='app.js'?controlled:source(name),context,{filename:name});
 const run=code=>vm.runInContext(code,context);
 run('data=fixture;globalThis.data=data;render=()=>{renders++;for(const button of document.querySelectorAll("[data-publish]"))button.disabled=false;syncDailyButtons();};');
 element('date').value=day;
 page.context=context;page.run=run;page.click=(index=0)=>listeners.get('click')({target:{closest:selector=>selector==='[data-publish]'?buttons[index]:null}});
 page.posts=()=>calls.filter(call=>call.url==='/api/daily');page.flag=()=>run('data.accounts[0].published');
 page.cached=()=>JSON.parse(storage.get('douyin-board-view')).board;
 page.message=()=>element('toast').textContent;
 return page;
}

let checks=0;
async function test(name,body){await body();checks++;console.log('ok '+name);}
(async()=>{
 await test('mark and unmark update flags, versions and durable view cache',async()=>{
  const p=harness();await p.click();assert.equal(p.flag(),true);assert.equal(p.cached().accounts[0].published,true);
  await p.click(1);assert.equal(p.flag(),false);assert.equal(p.cached().accounts[0].published,false);
  const [first,second]=p.posts().map(call=>call.body);assert.notEqual(first.request_id,second.request_id);
  assert.equal(second._expected_versions[key(day)],'daily-v1');assert.match(p.message(),/已取消/);
 });
 await test('all duplicate account buttons share one in-flight request',async()=>{
  const p=harness(),wait=deferred();p.onDaily=()=>wait.promise;
  const first=p.click(),second=p.click(1);assert.equal(p.posts().length,1);assert.ok(p.buttons.every(button=>button.disabled));
  assert.ok(p.buttons.every(button=>button.textContent==='保存中…'&&button.attrs['aria-busy']==='true'));
  const result=p.save(p.posts()[0].body);wait.resolve(result);await Promise.all([first,second]);
  assert.equal(p.flag(),true);assert.ok(p.buttons.every(button=>!button.disabled&&button.textContent==='✓ 已发'));assert.equal(p.posts().length,1);
 });
 await test('legacy only-ok response is verified by readback before marking',async()=>{
  const p=harness();p.onDaily=body=>{p.save(body);return response({ok:true});};
  await p.click();assert.equal(p.flag(),true);assert.ok(p.calls.some(call=>call.url.startsWith('/api/board?')));
  assert.equal(p.cached().record_versions[key(day)],'daily-v1');assert.doesNotMatch(p.message(),/undefined|TypeError/);
  await p.click();assert.equal(p.flag(),false);assert.match(p.message(),/已取消/);
 });
 await test('legacy success does not optimistically trust the requested flag',async()=>{
  const p=harness();p.onDaily=()=>response({ok:true});await p.click();assert.equal(p.flag(),false);
  assert.match(p.message(),/核对.*未发/);assert.equal(p.posts().length,1);
 });
 await test('failed legacy verification keeps an uncertain fixed request',async()=>{
  const p=harness();p.onDaily=body=>{p.save(body);return response({ok:true});};p.onBoard=()=>{throw Error('synthetic offline');};
  await p.click();assert.equal(p.flag(),false);assert.match(p.message(),/待确认/);assert.equal(p.run('dailyRequests.size'),1);
  assert.ok(p.buttons.every(button=>button.textContent==='待确认 · 重试'&&button.title.includes('原操作')&&!button.disabled));
  p.onBoard=null;await p.click();assert.equal(p.flag(),true);assert.equal(p.posts().length,1);assert.equal(p.run('dailyRequests.size'),0);
 });
 await test('a stored legacy only-ok result lookup is read back, not inferred',async()=>{
  const p=harness();p.onDaily=body=>{p.save(body);p.server.records.get(body.request_id).result={ok:true};
   p.server.flags[day]=false;p.server.versions[key(day)]='other-employee-false';throw Error('synthetic lost response');};
  await p.click();assert.equal(p.posts().length,1);assert.ok(p.calls.some(call=>call.url.startsWith('/api/idempotency/result?')));
  assert.equal(p.flag(),false);assert.equal(p.cached().record_versions[key(day)],'other-employee-false');assert.match(p.message(),/核对.*未发/);
 });
 await test('a complete stored success result is also rechecked against newer server state',async()=>{
  const p=harness();p.onDaily=body=>{p.save(body);p.server.flags[day]=false;p.server.versions[key(day)]='remote-false-after-success';throw Error('synthetic lost response');};
  await p.click();assert.equal(p.flag(),false);assert.equal(p.cached().record_versions[key(day)],'remote-false-after-success');
  assert.ok(p.calls.some(call=>call.url.startsWith('/api/board?')));
 });
 await test('a response for a different date is never applied to this date',async()=>{
  const p=harness();p.onDaily=()=>response({ok:true,daily:{id,date:otherDay,published:true},_record_versions:{[key(otherDay)]:'foreign-date'}});
  await p.click();assert.equal(p.flag(),false);assert.equal(p.cached().date,day);assert.match(p.message(),/核对.*未发/);
 });
 await test('changing dates during a slow save preserves the selected date',async()=>{
  const p=harness(),wait=deferred();p.onDaily=body=>{const saved=p.save(body);wait.saved=saved;return wait.promise;};
  const pending=p.click();p.context.targetDay=otherDay;await p.run('loadPublished(targetDay)');
  wait.resolve(wait.saved);await pending;assert.equal(p.flag(),false);assert.equal(p.cached().date,otherDay);assert.match(p.message(),/2026-10-29/);
 });
 await test('conflict refreshes flags and versions and allows a fresh retry',async()=>{
  const p=harness();let first=true;p.onDaily=body=>{if(first){first=false;p.server.flags[day]=true;p.server.versions[key(day)]='other-v1';return response({error:'资料已被其他人修改，请核对后重试'},409);}return p.save(body);};
  await p.click();assert.equal(p.flag(),true);assert.match(p.message(),/其他人修改/);assert.equal(p.run('dailyRequests.size'),0);
  await p.click();assert.equal(p.flag(),false);assert.notEqual(p.posts()[0].body.request_id,p.posts()[1].body.request_id);assert.equal(p.posts()[1].body._expected_versions[key(day)],'other-v1');
 });
 await test('unknown network outcome resolves the old request before reversing',async()=>{
  const p=harness();let failed=true;p.onDaily=body=>{const saved=p.save(body);if(failed){failed=false;throw Error('synthetic lost response');}return saved;};p.onResult=()=>{throw Error('synthetic lookup offline');};
  await p.click();assert.match(p.message(),/待确认/);const original=p.posts()[0].body;
  p.context.change={entity:key(day),after:{published:1},record_version:'daily-v1'};p.run('applyCaughtChange(change)');assert.equal(p.flag(),true);
  p.onResult=null;await p.click();assert.equal(p.posts().length,1);assert.equal(p.flag(),true);assert.equal(p.run('dailyRequests.size'),0);
  await p.click();const reverse=p.posts()[1].body;assert.equal(reverse.published,false);assert.notEqual(reverse.request_id,original.request_id);assert.equal(p.flag(),false);
 });
 await test('a retry uses byte-identical frozen versions and request body',async()=>{
  const p=harness();let attempts=0;p.onDaily=body=>{if(attempts++===0)throw Error('synthetic request not delivered');return p.save(body);};
  await p.click();const original=p.posts()[0].body;
  p.run('data.record_versions["account:synth-account-1"]="newer-account";');p.server.versions['account:'+id]='newer-account';
  await p.click();assert.deepEqual(p.posts()[1].body,original);assert.equal(p.flag(),true);
  assert.equal(p.context.data.record_versions['account:'+id],'newer-account');
 });
 await test('late v1 POST cannot roll back caught-up v2 state or cursor',async()=>{
  const p=harness(),wait=deferred();p.onDaily=body=>{wait.saved=p.save(body);return wait.promise;};const pending=p.click();
  p.server.flags[day]=false;p.server.versions[key(day)]='daily-v2';p.updates={cursor:12,changes:[
   {entity:key(day),after:{published:1},record_version:'daily-v1'},
   {entity:key(day),after:{published:0},record_version:'daily-v2'}]};
  await p.run('catchUpBoard()');assert.equal(p.flag(),false);assert.equal(p.context.data.cursor,12);
  wait.resolve(wait.saved);await pending;assert.equal(p.flag(),false);assert.equal(p.context.data.record_versions[key(day)],'daily-v2');
  assert.equal(p.cached().cursor,12);assert.equal(p.cached().accounts[0].published,false);
 });
 await test('a false-true-false content-token ABA still rejects the old true response',async()=>{
  const p=harness(),wait=deferred();p.onDaily=body=>{wait.saved=p.save(body);return wait.promise;};const pending=p.click();
  // Real content hashes return to the original token when the complete daily
  // row returns to false. Only the advanced sync cursor exposes this history.
  p.server.flags[day]=false;p.server.versions[key(day)]='daily-v0';p.updates={cursor:12,changes:[
   {entity:key(day),after:{published:1},record_version:'daily-v1'},
   {entity:key(day),after:{published:0},record_version:'daily-v0'}]};
  await p.run('catchUpBoard()');wait.resolve(wait.saved);await pending;
  assert.equal(p.flag(),false);assert.equal(p.context.data.record_versions[key(day)],'daily-v0');assert.equal(p.cached().cursor,12);
  assert.ok(p.calls.some(call=>call.url.startsWith('/api/board?')));
 });
 await test('atomic readback retries if a newer caught-up ABA wins while it is delayed',async()=>{
  const p=harness(),wait=deferred();p.onDaily=()=>response({ok:true});let reads=0;
  p.onBoard=url=>++reads===1?wait.promise:response(p.snapshot(new URLSearchParams(url.split('?')[1]).get('date')));
  const pending=p.click();await new Promise(resolve=>setImmediate(resolve));
  assert.ok(p.calls.some(call=>call.url.startsWith('/api/board?')));
  p.updates={cursor:12,changes:[{entity:key(day),after:{published:1},record_version:'daily-v1'},
   {entity:key(day),after:{published:0},record_version:'daily-v0'}]};
  await p.run('catchUpBoard()');const stale=p.snapshot(day);stale.accounts[0].published=true;stale.record_versions[key(day)]='daily-v1';wait.resolve(response(stale));await pending;
  assert.equal(p.flag(),false);assert.equal(p.cached().record_versions[key(day)],'daily-v0');assert.equal(p.cached().cursor,12);
  assert.equal(reads,2);
 });
 await test('atomic readback cannot overwrite a newer real full-page load',async()=>{
  const p=harness(),wait=deferred();p.onDaily=()=>response({ok:true});let reads=0;
  p.onBoard=url=>++reads===1?wait.promise:response(p.snapshot(new URLSearchParams(url.split('?')[1]).get('date')));
  p.run('showBoard=next=>{data=next;globalThis.data=data;$("date").value=data.date;render();writeBoardCache();};');
  const pending=p.click();await new Promise(resolve=>setImmediate(resolve));
  await p.run('load()');const stale=p.snapshot(day);stale.accounts[0].published=true;stale.record_versions[key(day)]='stale-true';
  wait.resolve(response(stale));await pending;
  assert.equal(p.flag(),false);assert.equal(p.cached().accounts[0].published,false);assert.equal(reads,2);
  assert.equal(p.run('dailyRequests.size'),1);assert.match(p.message(),/待确认/);
 });
 await test('old recovery cannot invalidate a newer user full-page load still awaiting its response',async()=>{
  const p=harness(),recovery=deferred(),refresh=deferred();p.context.data.accounts[0].note='old-note';
  p.onDaily=body=>{p.save(body);return response({ok:true});};let reads=0;
  p.onBoard=()=>++reads===1?recovery.promise:refresh.promise;
  p.run('showBoard=next=>{data=next;globalThis.data=data;$("date").value=data.date;render();writeBoardCache();};captureVersions("editor");captureVersions("batchEditor");');
  const save=p.click();await new Promise(resolve=>setImmediate(resolve));
  const load=p.run('load()'),stale=p.snapshot(day);stale.accounts[0].note='old-note';
  recovery.resolve(response(stale));await save;assert.equal(reads,2);assert.equal(p.run('dailyRequests.size'),1);
  const fresh=p.snapshot(day);fresh.accounts[0].note='fresh-note';fresh.record_versions['account:'+id]='fresh-account';
  refresh.resolve(response(fresh));await load;
  assert.equal(p.context.data.accounts[0].note,'fresh-note');assert.equal(p.context.data.record_versions['account:'+id],'fresh-account');assert.equal(p.flag(),true);
  assert.equal(p.run('capturedVersions.get("editor")["account:synth-account-1"]'),'account-v0');
  assert.equal(p.run('capturedVersions.get("batchEditor")["daily:synth-account-1:2026-10-29"]'),'daily-v0');
  p.onBoard=null;await p.click();assert.equal(p.run('dailyRequests.size'),0);assert.equal(p.posts().length,1);assert.equal(p.flag(),true);
 });
 await test('late POST cannot start recovery that cancels an already pending newer full-page load',async()=>{
  const p=harness(),post=deferred(),refresh=deferred();p.context.data.accounts[0].note='old-note';
  p.onDaily=body=>{post.saved=p.save(body);return post.promise;};let reads=0;
  p.onBoard=()=>{reads++;return refresh.promise;};
  p.run('showBoard=next=>{data=next;globalThis.data=data;$("date").value=data.date;render();writeBoardCache();};captureVersions("editor");');
  const save=p.click(),load=p.run('load()');post.resolve(post.saved);await save;
  assert.equal(reads,1);assert.equal(p.run('dailyRequests.size'),1);assert.match(p.message(),/待确认/);
  const fresh=p.snapshot(day);fresh.accounts[0].note='fresh-note';fresh.record_versions['account:'+id]='fresh-account';
  refresh.resolve(response(fresh));await load;
  assert.equal(p.context.data.accounts[0].note,'fresh-note');assert.equal(p.context.data.record_versions['account:'+id],'fresh-account');
  assert.equal(p.run('capturedVersions.get("editor")["account:synth-account-1"]'),'account-v0');
  p.onBoard=null;await p.click();assert.equal(p.run('dailyRequests.size'),0);assert.equal(p.posts().length,1);assert.equal(p.flag(),true);
 });
 await test('a new click after full-page refresh starts waits without cancelling the fresh view',async()=>{
  const p=harness(),refresh=deferred();p.context.data.accounts[0].note='old-note';p.onBoard=()=>refresh.promise;
  p.run('showBoard=next=>{data=next;globalThis.data=data;$("date").value=data.date;render();writeBoardCache();};');
  const load=p.run('load()');await p.click();assert.equal(p.posts().length,0);assert.match(p.message(),/页面正在刷新/);
  const fresh=p.snapshot(day);fresh.accounts[0].note='fresh-note';refresh.resolve(response(fresh));await load;
  assert.equal(p.context.data.accounts[0].note,'fresh-note');assert.equal(p.run('dailyViewLoading'),0);
  p.onBoard=null;await p.click();assert.equal(p.flag(),true);assert.equal(p.posts().length,1);
 });
 await test('pending retries wait for a view load and failed loads release the guard',async()=>{
  const p=harness();p.onDaily=body=>{p.save(body);return response({ok:true});};p.onBoard=()=>{throw Error('synthetic offline');};
  await p.click();assert.equal(p.run('dailyRequests.size'),1);const refresh=deferred();p.onBoard=()=>refresh.promise;
  const load=p.run('load()');await p.click();assert.equal(p.posts().length,1);assert.equal(p.run('dailyRequests.size'),1);
  refresh.reject(Error('synthetic failed page load'));await load;assert.equal(p.run('dailyViewLoading'),0);
  p.onBoard=null;await p.click();assert.equal(p.flag(),true);assert.equal(p.posts().length,1);assert.equal(p.run('dailyRequests.size'),0);
 });
 await test('overlapping view loads clear only their own pending generation',async()=>{
  const p=harness(),older=deferred(),newer=deferred();let reads=0;p.onBoard=()=>++reads===1?older.promise:newer.promise;
  p.run('showBoard=next=>{data=next;globalThis.data=data;$("date").value=data.date;render();writeBoardCache();};');
  const first=p.run('load()'),second=p.run('load()');older.resolve(response(p.snapshot(day)));await first;
  assert.notEqual(p.run('dailyViewLoading'),0);await p.click();assert.equal(p.posts().length,0);
  newer.resolve(response(p.snapshot(day)));await second;assert.equal(p.run('dailyViewLoading'),0);
  await p.click();assert.equal(p.posts().length,1);
 });
 await test('other accounts can finish while a legacy readback is delayed without losing any flag',async()=>{
  const p=harness(),wait=deferred();
  for(const account of ['b','c']){p.context.data.accounts.push({id:account,name:'合成账号'+account,published:false});p.context.data.record_versions['account:'+account]='account-'+account;p.server.versions['account:'+account]='account-'+account;}
  p.onDaily=body=>{const saved=p.save(body);return body.id===id?response({ok:true}):saved;};
  let snapshot;p.onBoard=url=>{snapshot=p.snapshot(new URLSearchParams(url.split('?')[1]).get('date'));return wait.promise;};
  const first=p.click();await new Promise(resolve=>setImmediate(resolve));
  await p.run('toggleDaily("b")');await p.run('toggleDaily("c")');
  assert.deepEqual(p.context.data.accounts.map(row=>row.published),[false,true,true]);
  wait.resolve(response(snapshot));await first;
  assert.deepEqual(p.context.data.accounts.map(row=>row.published),[true,true,true]);
  assert.equal(p.run('dailyRequests.size'),0);assert.deepEqual(p.cached().accounts.map(row=>row.published),[true,true,true]);
  assert.equal(p.cached().cursor,10);assert.equal(p.posts().length,3);
 });
 await test('continued concurrent changes retain a retryable pending entry instead of dropping a flag',async()=>{
  const p=harness();p.onDaily=body=>{p.save(body);return response({ok:true});};let reads=0;
  p.onBoard=url=>{reads++;const snapshot=p.snapshot(new URLSearchParams(url.split('?')[1]).get('date'));p.context.data.cursor++;return response(snapshot);};
  await p.click();assert.equal(reads,2);assert.equal(p.flag(),false);assert.equal(p.run('dailyRequests.size'),1);assert.match(p.message(),/待确认/);
  p.onBoard=null;await p.click();assert.equal(p.flag(),true);assert.equal(p.run('dailyRequests.size'),0);assert.equal(p.posts().length,1);
 });
 await test('late daily delta does not overwrite unrelated newer account version',async()=>{
  const p=harness(),wait=deferred();p.onDaily=body=>{wait.saved=p.save(body);return wait.promise;};const pending=p.click();
  p.context.change={entity:'account:'+id,after:{name:'合成新昵称'},record_version:'account-v2'};p.run('applyCaughtChange(change)');
  wait.resolve(wait.saved);await pending;assert.equal(p.flag(),true);assert.equal(p.context.data.accounts[0].name,'合成新昵称');assert.equal(p.context.data.record_versions['account:'+id],'account-v2');
 });
 await test('recovery cannot attach foreign account tokens to stale fields or open editor snapshots',async()=>{
  const p=harness();p.context.data.accounts[0].note='local-old-note';p.run('captureVersions("editor");captureVersions("batchEditor");');
  p.onDaily=body=>{p.save(body);p.server.versions['account:'+id]='foreign-account-v2';return response({ok:true});};
  p.onBoard=url=>{const snapshot=p.snapshot(new URLSearchParams(url.split('?')[1]).get('date'));snapshot.accounts[0].note='remote-new-note';snapshot.record_versions['account:unrelated']='unrelated-token';return response(snapshot);};
  await p.click();assert.equal(p.flag(),true);assert.equal(p.context.data.accounts[0].note,'local-old-note');
  assert.equal(p.context.data.record_versions['account:'+id],'account-v0');assert.equal(p.context.data.record_versions[key(day)],'daily-v1');
  assert.equal(p.context.data.record_versions['account:unrelated'],undefined);assert.equal(p.context.data.cursor,10);
  for(const editor of ['editor','batchEditor']){p.context.editorName=editor;assert.equal(p.run('capturedVersions.get(editorName)["account:synth-account-1"]'),'account-v0');assert.equal(p.run('capturedVersions.get(editorName)["daily:synth-account-1:2026-10-29"]'),'daily-v0');}
  p.run('$("editor").open=true;');
  assert.equal(p.run('versionPayload("/api/account",{id:"synth-account-1",note:"local-old-note"},data,activeEditVersions())._expected_versions["account:synth-account-1"]'),'account-v0');
 });
 await test('a normal daily success updates live versions without advancing an open batch snapshot',async()=>{
  const p=harness();p.run('captureVersions("batchEditor");$("batchEditor").open=true;');
  await p.click();assert.equal(p.context.data.record_versions[key(day)],'daily-v1');
  assert.equal(p.run('capturedVersions.get("batchEditor")["daily:synth-account-1:2026-10-29"]'),'daily-v0');
 });
 await test('a missing account baseline requires a real refresh and never guesses a token',async()=>{
  const p=harness();delete p.context.data.record_versions['account:'+id];await p.click();
  assert.equal(p.posts().length,0);assert.equal(p.flag(),false);assert.match(p.message(),/刷新页面后重试/);
  assert.equal(p.context.data.record_versions['account:'+id],undefined);assert.equal(p.run('dailyRequests.size'),0);
 });
 await test('daily button feedback never clears an existing permission-disabled state',async()=>{
  const p=harness();for(const button of p.buttons)button.disabled=true;
  p.run('syncDailyButtons()');assert.ok(p.buttons.every(button=>button.disabled&&button.textContent==='○ 未发'));
 });
 await test('a legitimately absent date uses expected null without an extra board load',async()=>{
  const p=harness();delete p.context.data.record_versions[key(day)];delete p.server.versions[key(day)];
  await p.click();assert.equal(p.posts()[0].body._expected_versions[key(day)],null);assert.equal(p.flag(),true);
  assert.equal(p.calls.filter(call=>call.url.startsWith('/api/board?')).length,0);
 });
 await test('an unknown existing date row conflicts then reads a coherent flag and version',async()=>{
  const p=harness();delete p.context.data.record_versions[key(day)];p.server.flags[day]=true;p.server.versions[key(day)]='remote-v2';
  let first=true;p.onDaily=body=>{if(first){first=false;assert.equal(body._expected_versions[key(day)],null);return response({error:'资料已变化，请核对后重试'},409);}return p.save(body);};
  await p.click();assert.equal(p.posts().length,1);assert.equal(p.flag(),true);assert.match(p.message(),/变化/);
  await p.click();assert.equal(p.posts()[1].body.published,false);assert.equal(p.posts()[1].body._expected_versions[key(day)],'remote-v2');
 });
 await test('permission failure and a date transition leave controls retryable',async()=>{
  const p=harness();p.onDaily=()=>response({error:'没有发布权限'},403);await p.click();assert.equal(p.flag(),false);assert.match(p.message(),/发布权限/);assert.ok(p.buttons.every(button=>!button.disabled));
  p.elements.get('date').value=otherDay;await p.click();assert.equal(p.posts().length,1);assert.match(p.message(),/日期切换/);
 });
 await test('an explicit retry continues a durable pending record with the identical payload',async()=>{
  const p=harness();let attempt=0;p.onDaily=body=>{if(attempt++===0)throw Error('synthetic worker died after reservation');return p.save(body);};p.onResult=()=>response({error:'结果待确认',code:'REQUEST_PENDING'},409);
  await p.click();assert.equal(p.posts().length,1);assert.equal(p.run('dailyRequests.size'),1);
  await p.click();assert.equal(p.posts().length,2);assert.deepEqual(p.posts()[1].body,p.posts()[0].body);
  assert.equal(p.flag(),true);assert.equal(p.run('dailyRequests.size'),0);assert.equal(p.server.records.size,1);
 });
 console.log(JSON.stringify({passed:true,checks,kind:'real-app-js-daily-api-click-and-catchup-with-synthetic-dom-fetch-only'}));
})().catch(error=>{console.error(error);process.exitCode=1;});
