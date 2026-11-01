// Actual account inline/app/sync scripts with synthetic DOM, timers and HTTP only.
// No browser profiles, production services, business files or external network.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const {harness:baseHarness,fixture,root}=require('./test_performance_ui.cjs');
const clone=value=>structuredClone(value),plain=value=>JSON.parse(JSON.stringify(value));
const response=(body,status=200)=>({ok:status>=200&&status<300,status,json:async()=>clone(body)});
const deferred=()=>{let resolve,reject;const promise=new Promise((ok,no)=>{resolve=ok;reject=no;});return {promise,resolve,reject};};
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function server(){return {row:{id:'account-0',name:'当前昵称',status:'正常',note:'当前备注'},version:'server-v0',cursor:10,revision:0,records:new Map(),history:[]};}
function harness(shared=server(),permissions=['account_manage','account_status','publish']){
 const p=baseHarness(root),events=new Map(),windowEvents=new Map(),timers=new Map();let now=1000,timerId=0;
 p.server=shared;p.board=fixture(12);for(const row of p.board.accounts)row.note='旧备注';p.board.user.permissions=permissions;p.context.hasPermission=key=>permissions.includes(key);
 const register=p.context.document.addEventListener;p.context.document.addEventListener=(name,fn,options)=>{register(name,fn,options);if(!events.has(name))events.set(name,[]);events.get(name).push(fn);};
 p.context.addEventListener=(name,fn)=>{if(!windowEvents.has(name))windowEvents.set(name,new Set());windowEvents.get(name).add(fn);};p.context.removeEventListener=(name,fn)=>windowEvents.get(name)?.delete(fn);
 p.context.document.removeEventListener=(name,fn)=>{if(events.has(name))events.set(name,events.get(name).filter(item=>item!==fn));};p.context.document.visibilityState='visible';p.context.document.activeElement=null;
 const query=p.context.document.querySelector;p.context.document.querySelector=selector=>selector==='dialog[open]'?[...p.elements.values()].find(item=>item.open)||null:query(selector);
 p.context.Date=class extends Date{static now(){return now;}};p.context.AbortController=AbortController;
 p.context.setTimeout=(fn,delay)=>{timers.set(++timerId,{fn,at:now+delay});return timerId;};p.context.clearTimeout=id=>timers.delete(id);
 const body=p.element('accountsBody'),bodyHTML=Object.getOwnPropertyDescriptor(body,'innerHTML');Object.defineProperty(body,'innerHTML',{get:bodyHTML.get,set(value){if(p.context.document.activeElement?.id==='accountInlineInput')p.context.document.activeElement=null;bodyHTML.set.call(body,value);}});
 p.reads=0;p.posts=[];p.gets=[];p.requests=[];p.narrow=()=>({account:clone(shared.row),record_version:shared.version,cursor:shared.cursor});
 p.snapshot=()=>{const next=clone(p.board);Object.assign(next.accounts[0],shared.row);next.record_versions['account:account-0']=shared.version;next.cursor=shared.cursor;return next;};
 p.commit=body=>{
  const previous=shared.records.get(body.request_id);if(previous){assert.equal(JSON.stringify(body),previous.body,'UUID retries remain byte-identical');return clone(previous.result);}
  if(body._expected_versions['account:'+body.id]!==shared.version){const error=Error('资料已被其他人修改');error.status=409;throw error;}
  Object.assign(shared.row,body.changes);shared.version='server-v'+(++shared.revision);shared.cursor++;
  shared.history.push({entity:'account:'+body.id,after:clone(shared.row),record_version:shared.version,cursor:shared.cursor});
  const result={ok:true,id:body.id,...p.narrow(),_record_versions:{['account:'+body.id]:shared.version}};shared.records.set(body.request_id,{body:JSON.stringify(body),result:clone(result)});return result;
 };
 p.delta=()=>({cursor:shared.cursor,changes:clone(shared.history),wait_supported:true});
 p.context.fetch=async(url,options={})=>{
  p.requests.push({url,options});if(url.startsWith('/api/account/inline?')){p.reads++;return p.onRead?p.onRead(url):response(p.narrow());}
  if(url==='/api/account/patch'){const body=JSON.parse(options.body);p.posts.push({body,text:options.body,options});if(p.onPatch)return p.onPatch(body,options);try{return response(p.commit(body));}catch(error){return response({error:error.message},error.status||500);}}
  if(url.startsWith('/api/idempotency/result?')){p.gets.push(url);if(p.onResult)return p.onResult(url);const record=shared.records.get(new URLSearchParams(url.split('?')[1]).get('request_id'));return record?response({...clone(record.result),code:'REQUEST_SUCCEEDED'}):response({error:'未找到请求',code:'REQUEST_NOT_FOUND'},404);}
  if(url.startsWith('/api/board-updates?'))return p.onUpdates?p.onUpdates(url,options):response(p.delta());
  if(url.startsWith('/api/board?'))return response(p.snapshot());
  throw Error('Unexpected synthetic request '+url);
 };
 p.context.location.replace=url=>{p.redirect=url;};
 p.holder=(id='account-0',field='name')=>p.element('account-inline-'+field+'-'+encodeURIComponent(id));
 for(const field of ['name','status','note']){
  const node=p.holder('account-0',field),descriptor=Object.getOwnPropertyDescriptor(node,'innerHTML');node.dataset={inlineCell:'account-0',inlineField:field};
  Object.defineProperty(node,'innerHTML',{get:descriptor.get,set(value){descriptor.set.call(node,value);if(String(value).includes('id="accountInlineInput"')){const input=p.element('accountInlineInput');input.disabled=/id="accountInlineInput"[^>]*\bdisabled\b/.test(value);input.tagName=field==='note'?'TEXTAREA':field==='status'?'SELECT':'INPUT';input.focus=()=>{p.context.document.activeElement=input;};input.setSelectionRange=(a,b)=>{input.selectionStart=a;input.selectionEnd=b;};input.closest=selector=>selector==='[data-inline-cell]'?node:null;}}});
 }
 vm.runInContext(fs.readFileSync(path.join(root,'static/account-inline.js'),'utf8'),p.context,{filename:'account-inline.js'});
 p.reset(clone(p.board));p.show(clone(p.board));
 vm.runInContext(fs.readFileSync(path.join(root,'static/board-updates.js'),'utf8'),p.context,{filename:'board-updates.js'});
 p.component=p.context.AccountInline;p.draft=()=>plain(p.component.getState());p.begin=(field='name')=>p.component.begin('account-0',field);
 p.emit=(type,target=p.element('accountInlineInput'),extra={})=>{const event={target,defaultPrevented:false,preventDefault(){this.defaultPrevented=true;},stopImmediatePropagation(){this.stopped=true;},...extra};for(const fn of events.get(type)||[]){fn(event);if(event.stopped)break;}return event;};
 p.type=value=>{const input=p.element('accountInlineInput');assert.equal(input.disabled,false,'native disabled inputs cannot change');input.value=value;p.emit('input',input);};
 p.settle=async()=>{for(let i=0;i<12;i++)await flush();};
 p.advance=async ms=>{now+=ms;const due=[...timers].filter(([,timer])=>timer.at<=now);for(const [id,timer]of due){if(!timers.has(id))continue;timers.delete(id);timer.fn();await p.settle();}};
 p.enable=async()=>{vm.runInContext(fs.readFileSync(path.join(root,'static/live-sync.js'),'utf8'),p.context,{filename:'live-sync.js'});p.run('startBoardLiveSync()');await p.settle();};
 p.stop=()=>p.run('window.boardLiveSync?.stop()');return p;
}
let checks=0;async function test(name,fn){await fn();checks++;console.log('ok '+name);}
(async()=>{
 await test('only authorized name/status/note affordances appear and normal rendering sends no requests',async()=>{
  const p=harness(server(),[]);assert.ok(!p.element('accountsBody').innerHTML.includes('data-inline-cell'));assert.equal(p.requests.length,0);assert.equal(await p.begin(),false);
  const q=harness(server(),['account_status']);assert.ok(!q.component.cell(q.board.accounts[0],'name','nickname').includes('data-inline-cell'));assert.ok(q.component.cell(q.board.accounts[0],'status','state').includes('data-inline-cell'));assert.ok(q.element('accountsBody').innerHTML.includes('account-inline-note-preview'));
 });
 await test('real HTML hint and actual auth permissions show read-only or only granted inline fields, including revocation',async()=>{
  const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8'),initial=html.match(/<p\b[^>]*\bid="accountInlineHint"[^>]*>([\s\S]*?)<\/p>/)?.[1];assert.ok(initial,'exercise the real nonempty initial HTML hint');
  for(const permissions of [[],['publish'],['account_status'],['account_manage']]){
   const p=harness(server(),permissions);p.element('accountInlineHint').textContent=initial;vm.runInContext(fs.readFileSync(path.join(root,'static/auth-ui.js'),'utf8'),p.context,{filename:'auth-ui.js'});p.run('applyPermissions()');p.emit('board-ready');const text=p.element('accountInlineHint').textContent;
   if(permissions.includes('account_status')){assert.match(text,/账号状态支持点击编辑/);assert.ok(!text.includes('昵称')&&!text.includes('备注'));}
   else if(permissions.includes('account_manage')){assert.match(text,/昵称、备注支持点击编辑/);assert.ok(!text.includes('账号状态'));}
   else{assert.match(text,/当前账号为只读/);assert.ok(!text.includes('支持点击编辑'));}
   p.run('data.user.permissions=[];applyPermissions()');p.emit('board-ready');assert.match(p.element('accountInlineHint').textContent,/当前账号为只读/);assert.equal(await p.begin(),false);assert.equal(p.requests.length,0);
  }
 });
 await test('permission-hint refresh never overwrites active errors or saved-status synchronization messages',async()=>{
  const p=harness();vm.runInContext(fs.readFileSync(path.join(root,'static/auth-ui.js'),'utf8'),p.context,{filename:'auth-ui.js'});p.run('applyPermissions()');p.emit('board-ready');await p.begin();p.type(' ');await p.component.save();const error=p.element('accountInlineHint').textContent;p.emit('board-ready');assert.equal(p.element('accountInlineHint').textContent,error);p.component.cancel();
  await p.begin('status');p.type('封禁');await p.component.save();const pending=p.element('accountInlineHint').textContent;assert.match(pending,/正在更新筛选结果和统计/);p.emit('board-ready');assert.equal(p.element('accountInlineHint').textContent,pending);
 });
 await test('edit baseline is same-snapshot narrow GET and field-only save never advances old board/editor locks or cursor',async()=>{
  const p=harness();p.run('captureVersions("editor");captureVersions("batchEditor")');const versions=plain(p.run('data.record_versions')),cursor=p.run('data.cursor');await p.begin();assert.equal(p.draft().value,'当前昵称');assert.equal(p.run('data.accounts[0].name'),'合成账号00000');
  p.type('  新昵称 <安全>  ');assert.equal(await p.component.save(),true);assert.equal(p.posts.length,1);const body=p.posts[0].body;
  assert.deepEqual(body.changes,{name:'新昵称 <安全>'});assert.deepEqual(body._expected_versions,{'account:account-0':'server-v0'});assert.equal(body.return_account,true);assert.match(body.request_id,/^[0-9a-f-]{36}$/);assert.equal(p.posts[0].options.headers['X-CSRF-Token'],'synthetic-csrf');
  assert.equal(p.run('data.accounts[0].name'),'合成账号00000','receipt display stays separate from authoritative board');assert.ok(p.element('accountsBody').innerHTML.includes('新昵称 &lt;安全&gt;'));assert.deepEqual(plain(p.run('data.record_versions')),versions);assert.deepEqual(plain(p.run('capturedVersions.get("editor")')),versions);assert.deepEqual(plain(p.run('capturedVersions.get("batchEditor")')),versions);assert.equal(p.run('data.cursor'),cursor);
  await p.run('catchUpBoard()');assert.equal(p.run('data.accounts[0].name'),'新昵称 <安全>');assert.equal(p.run('data.cursor'),11);assert.equal(p.requests.filter(r=>/^\/api\/board\?/.test(r.url)).length,0);
 });
 await test('Enter plus blur shares a single save and unchanged blur writes nothing',async()=>{
  const p=harness();await p.begin();p.emit('focusout');await p.settle();assert.equal(p.posts.length,0);assert.equal(p.draft(),null);
  await p.begin();p.type('一次保存');p.emit('keydown',undefined,{key:'Enter'});p.emit('focusout');await p.settle();assert.equal(p.posts.length,1);assert.equal(p.draft(),null);
 });
 await test('Tab through editor actions saves once only after leaving the editor and Cancel never saves first',async()=>{
  const editor={},action=type=>({dataset:{inlineAction:type},closest:selector=>selector==='[data-inline-editor]'?editor:selector==='[data-inline-action]'?{dataset:{inlineAction:type}}:null});
  const p=harness();await p.begin();p.type('经过按钮后离开');const saveButton=action('save'),cancelButton=action('cancel');
  p.emit('focusout',undefined,{relatedTarget:saveButton});p.emit('focusout',saveButton,{relatedTarget:cancelButton});assert.equal(p.posts.length,0);assert.notEqual(p.draft(),null);
  p.emit('focusout',cancelButton,{relatedTarget:{closest:()=>null}});await p.settle();assert.equal(p.posts.length,1);assert.equal(p.draft(),null);
  const q=harness();await q.begin();q.type('应当取消的草稿');const cancel=action('cancel');q.emit('focusout',undefined,{relatedTarget:cancel});assert.equal(q.posts.length,0);q.emit('click',cancel);await q.settle();assert.equal(q.posts.length,0);assert.equal(q.draft(),null);
 });
 await test('notes preserve line breaks, Enter edits and Ctrl+Enter saves',async()=>{
  const p=harness();await p.begin('note');p.type('第一行\n第二行 <标签>');const enter=p.emit('keydown',undefined,{key:'Enter'});assert.equal(enter.defaultPrevented,false);assert.equal(p.posts.length,0);p.emit('keydown',undefined,{key:'Enter',ctrlKey:true});await p.settle();assert.deepEqual(p.posts[0].body.changes,{note:'第一行\n第二行 <标签>'});assert.equal(p.draft(),null);
 });
 await test('IME composition and key229 Enter never save or consume candidate confirmation',async()=>{
  const p=harness();await p.begin();p.type('中文输入');p.emit('compositionstart');assert.equal(p.emit('keydown',undefined,{key:'Enter'}).defaultPrevented,false);p.emit('focusout');p.emit('compositionend');assert.equal(p.emit('keydown',undefined,{key:'Enter',isComposing:true}).defaultPrevented,false);assert.equal(p.emit('keydown',undefined,{key:'Enter',keyCode:229}).defaultPrevented,false);assert.equal(p.posts.length,0);p.emit('keydown',undefined,{key:'Enter'});await p.settle();assert.equal(p.posts.length,1);
 });
 await test('native clicks on active text/select inputs keep focus selection and dropdown behavior',async()=>{
  for(const field of ['name','status']){const p=harness();await p.begin(field);assert.equal(p.emit('click').defaultPrevented,false);p.component.cancel();}
 });
 await test('status uses the same narrow patch but later authorized delta still reloads derived views',async()=>{
  const p=harness();await p.begin('status');p.type('封禁');await p.component.save();assert.deepEqual(p.posts[0].body.changes,{status:'封禁'});assert.equal(p.run('data.accounts[0].status'),'正常');await p.run('catchUpBoard()');assert.equal(p.run('data.accounts[0].status'),'封禁');assert.equal(p.requests.filter(r=>/^\/api\/board\?/.test(r.url)).length,1);
 });
 await test('saved status explicitly marks pending filter/statistic refresh until the authorized snapshot removes the row',async()=>{
  const p=harness();p.element('status').value='正常';p.run('render()');assert.equal(p.run('filtered().length'),12);await p.begin('status');p.type('封禁');await p.component.save();
  assert.equal(p.run('filtered().length'),12,'unrelated board fields and snapshot stay unchanged during confirmation');assert.ok(p.element('accountsBody').innerHTML.includes('account-inline-pending'));assert.match(p.element('accountInlineHint').textContent,/已保存账号状态，正在更新筛选结果和统计/);
  await p.run('catchUpBoard()');assert.equal(p.run('filtered().length'),11);assert.equal(p.rowIds().includes('account-0'),false);assert.equal(p.element('accountsBody').innerHTML.includes('account-inline-pending'),false);assert.match(p.element('accountInlineHint').textContent,/筛选结果和统计已更新/);
 });
 await test('invalid draft survives filters, pagination, render, refresh, date, daily and outside navigation until explicit cancel',async()=>{
  const p=harness();await p.begin();p.type('   ');await p.component.save();assert.equal(p.draft().phase,'error');const html=p.holder().innerHTML;p.element('query').value='no match';p.run('accountPageIndex=2;render()');assert.equal(p.holder().innerHTML,html);assert.equal(p.draft().value,'   ');
  const count=p.requests.length;await p.run('load()');p.element('date').value='2026-10-28';await p.run('loadPublished("2026-10-28")');await p.run('toggleDaily("account-1")');p.run('edit("account-1")');assert.equal(p.requests.length,count);assert.equal(p.element('date').value,p.run('data.date'));assert.equal(p.element('editor').open,false);
  const target={closest:selector=>selector==='a[href],button,input[type="submit"]'?{}:null};assert.equal(p.emit('click',target).defaultPrevented,true);assert.equal(p.component.cancel(),true);assert.equal(p.draft(),null);assert.equal(p.element('query').value,'no match');
 });
 await test('409 preserves input and frozen version, never retries with a fresh token',async()=>{
  const p=harness();await p.begin();p.server.row.note='另一人改了备注';p.server.version='peer-v1';p.type('我的昵称草稿');await p.component.save();assert.equal(p.draft().phase,'conflict');assert.equal(p.draft().value,'我的昵称草稿');const packet=p.posts[0].text;await p.component.save();assert.equal(p.posts.length,1);assert.equal(p.posts[0].text,packet);assert.equal(p.run('data.record_versions["account:account-0"]'),'synthetic-account-v0');p.component.cancel();
 });
 await test('unknown network outcome keeps UUID and payload, then lookup plus fresh GET confirms without a duplicate write',async()=>{
  const p=harness();p.onPatch=body=>{p.commit(body);throw Error('lost response');};p.onResult=()=>{throw Error('lookup offline');};await p.begin();p.type('已保存但回包丢失');await p.component.save();assert.equal(p.draft().phase,'unknown');assert.equal(p.component.cancel(),false);const packet=p.posts[0].text,id=p.draft().requestId;
  p.onResult=null;await p.component.save();assert.equal(p.posts.length,1);assert.equal(p.posts[0].text,packet);assert.equal(p.posts[0].body.request_id,id);assert.equal(p.draft(),null);assert.equal(p.run('data.cursor'),10);assert.equal(p.run('boardWrites'),0);
 });
 await test('HTTP 5xx and malformed 200 are uncertain, never accepted as saved or assigned fresh UUIDs',async()=>{
  for(const first of [response({},500),response({ok:true}),response({ok:true,id:'account-0',account:{id:'account-0',name:'bad',status:'正常',note:''},record_version:'x',cursor:11,_record_versions:{'account:account-0':'different'}})]){
   const p=harness();let initial=true;p.onPatch=body=>{if(initial){initial=false;return first;}return response(p.commit(body));};await p.begin();p.type('固定请求');await p.component.save();assert.equal(p.draft().phase,'unknown');assert.equal(p.reads,1,'invalid receipt cannot trigger a false successful current-value read');await p.component.save();assert.equal(p.posts.length,2);assert.equal(p.posts[0].text,p.posts[1].text);assert.equal(p.draft(),null);
  }
 });
 await test('historical success followed by another edit keeps draft and requires explicit adopt-current choice',async()=>{
  const p=harness();p.onPatch=body=>{const receipt=p.commit(body);p.server.row.name='其他人的新昵称';p.server.version='peer-after';return response(receipt);};await p.begin();p.type('我的旧提交');await p.component.save();assert.equal(p.draft().phase,'conflict');assert.equal(p.draft().value,'我的旧提交');assert.ok(p.holder().innerHTML.includes('采用当前内容'));assert.equal(p.server.row.name,'其他人的新昵称');await p.component.save();assert.equal(p.posts.length,1);assert.equal(p.component.cancel(),true);assert.equal(p.draft(),null);assert.ok(p.element('accountsBody').innerHTML.includes('其他人的新昵称'));
 });
 await test('hung POST is bounded without aborting business write and late response cannot occupy guards or damage a new draft',async()=>{
  const p=harness(),wait=deferred();let receipt;p.onPatch=body=>{receipt=p.commit(body);return wait.promise;};await p.begin();p.type('超时后核对');const saving=p.component.save();await p.advance(15000);await saving;assert.equal(p.draft().phase,'unknown');assert.equal(p.run('boardWrites'),0);await p.component.save();assert.equal(p.draft(),null);assert.equal(p.posts.length,1);assert.equal(p.run('boardSyncBlocked()'),false);
  await p.begin('note');p.type('新的备注草稿');wait.resolve(response(receipt));await p.settle();assert.equal(p.draft().field,'note');assert.equal(p.draft().value,'新的备注草稿');assert.equal(p.run('boardWrites'),0);p.component.cancel();await p.run('catchUpBoard()');assert.equal(p.run('data.accounts[0].name'),'超时后核对');
 });
 await test('hung result lookup times out and an obsolete attempt never clears a later retry',async()=>{
  const p=harness(),wait=deferred();let receipt;p.onPatch=body=>{receipt=p.commit(body);throw Error('lost response');};p.onResult=()=>wait.promise;await p.begin();p.type('查询超时');const first=p.component.save();await p.settle();await p.advance(15000);await first;assert.equal(p.draft().phase,'unknown');p.onResult=null;await p.component.save();assert.equal(p.draft(),null);wait.resolve(response({...receipt,code:'REQUEST_SUCCEEDED'}));await p.settle();assert.equal(p.draft(),null);assert.equal(p.run('boardWrites'),0);assert.equal(p.posts.length,1);
 });
 await test('Esc during loading discards only the read and its late result cannot reopen editing',async()=>{
  const p=harness(),wait=deferred();p.onRead=()=>wait.promise;const opening=p.begin();p.emit('keydown',undefined,{key:'Escape'});wait.resolve(response(p.narrow()));await opening;assert.equal(p.draft(),null);assert.equal(p.posts.length,0);
 });
 await test('temporary confirmed values never cross users and full snapshots clear the temporary display',async()=>{
  const p=harness();await p.begin();p.type('仅当前用户的确认值');await p.component.save();assert.ok(p.element('accountsBody').innerHTML.includes('仅当前用户的确认值'));p.run('data.user.id="different-user";render()');assert.ok(!p.element('accountsBody').innerHTML.includes('仅当前用户的确认值'));p.show(p.snapshot());assert.equal(p.run('data.accounts[0].name'),'仅当前用户的确认值');
 });
 await test('old long poll spanning edit/save cannot apply stale data and resumes within the 250ms continuation',async()=>{
  const p=harness(),old=deferred();let updates=0;p.onUpdates=()=>++updates===1?old.promise:response(p.delta());await p.enable();assert.equal(updates,1);await p.begin();p.type('长轮询协作昵称');p.onPatch=body=>{const receipt=p.commit(body);old.resolve(response(p.delta()));return response(receipt);};await p.component.save();await p.settle();await p.advance(250);assert.ok(updates>=2);assert.equal(p.run('data.accounts[0].name'),'长轮询协作昵称');assert.equal(p.run('data.cursor'),11);p.stop();
 });
 await test('two synthetic users see the saved account through the existing authorized incremental adapter',async()=>{
  const shared=server(),a=harness(shared),b=harness(shared);await a.begin();a.type('双人协作名称');await a.component.save();await b.run('catchUpBoard()');assert.equal(b.run('data.accounts[0].name'),'双人协作名称');assert.equal(shared.history.length,1);assert.equal(b.run('data.accounts.length'),12);
 });
 console.log(JSON.stringify({passed:true,checks,kind:'actual-inline-app-and-sync-with-synthetic-dom-timers-fetch-only'}));
})().catch(error=>{console.error(error);process.exitCode=1;});
