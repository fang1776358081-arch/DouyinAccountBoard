// Real workspace/nurture/version/batch scripts, synthetic DOM and controlled fetch.
// No browser, production connection, database, credentials or persisted data.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const read=name=>fs.readFileSync(path.join(__dirname,name),'utf8');
const fixture={require,console,URL,URLSearchParams,__dirname};
const navigation=read('test_workspace_navigation.cjs'),boundary=navigation.indexOf('const flush = () =>');
assert.ok(boundary>0);
let prefix=navigation.slice(0,boundary)
 .replace('return {context, field, requests, renders, run, navigate, respond, respondRaw, reject,','return {listeners, context, field, requests, renders, run, navigate, respond, respondRaw, reject,')
 .replace("value: id === 'workDate'", "id, value: id === 'workDate'")
 .replace('fetch(url) {','fetch(url, options) {')
 .replace('requests.push({url, resolve, reject, settled: false})','requests.push({url, options, resolve, reject, settled: false})')
 .replace("textContent: '', hidden: false, open: false, innerHTML: '',", "textContent: '', hidden: false, open: false, innerHTML: '', tagName: 'DIV', showModal(){this.open=true;}, close(){this.open=false;},")
 .replace('content = value; renders.push(value);',`content = value; renders.push(value);
        for(const match of String(value).matchAll(/id="([^"]+)"/g)){
          const control=field(match[1]);control.value=match[1].endsWith('Office')?'all':match[1]==='nurtureStage'?'active':match[1]==='historyType'?'account':'';
        }`)
 .replace('context.window = context;',`context.window = context;
  context.scrollX=4;context.scrollY=220;context.scrollNodes=[{scrollLeft:73,scrollTop:19}];context.detailNodes=[{id:'syntheticDetails',open:true}];
  context.scrollTo=(x,y)=>{context.lastScroll=[x,y];};
  context.crypto={randomUUID:()=> 'synthetic-request-id'};
  context.document.activeElement=null;
  context.document.querySelectorAll=selector=>selector==='.account-scroll'?context.scrollNodes:selector==='#workspaceBody details[id]'?context.detailNodes:selector==='dialog[open]'?[...fields.values()].filter(item=>item.open&&/Dialog$|Editor$/.test(item.id)):[];
  context.document.querySelector=selector=>selector==='dialog[open]'?[...fields.values()].find(item=>item.open&&/Dialog$|Editor$/.test(item.id)):null;
  context.document.createElement=tag=>({tagName:tag.toUpperCase(),innerHTML:'',open:false,showModal(){this.open=true;},close(){this.open=false;},querySelectorAll(){return [];}});
  context.document.body={append(){}};
  context.LiveSync={create(options){context.syncOptions=options;return {start(){context.started=true;},poke(){context.poked=(context.poked||0)+1;},stop(){}};},status(id,state){context.syncStatus={id,state};}};`)
 .replace("['nurture.js', read('nurture.js')],", "['nurture.js', read('nurture.js')],['edit-versions.js',read('edit-versions.js')],['batch-edit.js',read('batch-edit.js')],");
vm.createContext(fixture);vm.runInContext(prefix,fixture);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
let passed=0;
const test=async(name,job)=>{await job();passed++;console.log('PASS '+name);};
function board(count=3,view='detail',cursor=10){
 const value=fixture.board(view);value.cursor=cursor;
 value.user={id:8,name:'合成员工',role:'staff',scope_mode:'assigned',office_ids:['one'],permissions:['nurture_manage','publishing_manage','history_view']};
 value.offices=[{id:'one',name:'合成办公室一'}];value.computers=[{id:'synthetic-computer',name:'合成电脑',office:'one'}];
 value.accounts=Array.from({length:count},(_,i)=>({id:'synthetic-'+i,name:'合成昵称'+i,douyin_id:'synthetic-handle-'+i,computer_id:'synthetic-computer',office_ids:['one'],office:'one',status:'正常',account_type:'大号',phone_device_id:String(i+1),published:false}));
 value.nurture.verification_states=['待检查','核对处理中'];value.publishing_layout.batch='synthetic-batch';
 return value;
}
function harness(tab='accounts'){
 const page=fixture.harness('?tab='+tab);page.run(read('static/field-search.js'));page.run(read('static/identity-labels.js'));
 page.context.AccountReview={fromBoard:value=>value.review_issues||[],importRows:()=>[],identityComplete:()=>true};
 page.dispatch=(selector,dataset)=>{
  let stopped=false;const event={target:{closest:query=>query===selector?{dataset}:null},stopPropagation(){stopped=true;}};
  for(const listener of page.listeners.filter(row=>row.type==='click'&&row.capture))listener.callback(event);
  if(!stopped)for(const listener of page.listeners.filter(row=>row.type==='click'&&!row.capture))listener.callback(event);
 };
 return page;
}
async function start(page,value){const job=page.start();page.respond(0,value);await flush();if(page.requests[1]?.url.startsWith('/api/workspace/history'))page.respond(1,{entries:[],next_before:0});await job;return page;}
const invoke=page=>page.run('syncWorkspace({signal:syntheticSignal,isCurrent:()=>syntheticCurrent})');
function sync(page){page.context.syntheticSignal={synthetic:true};page.context.syntheticCurrent=true;return invoke(page);}
const rows=page=>page.field('accountListRows').innerHTML;
function record(id='synthetic-0') {return {account_id:id,stage:'待检查',verification_state:'待检查',verification_owner:'原负责人',verification_note:'原备注',verification_updated_on:'',workflow:'待开始',day_dates:[],images:[],elapsed:0,remaining_days:7,image_count:0};}
(async()=>{
 await test('controller contract starts once; idle and invisible changes never render or read a board',async()=>{
  const page=await start(harness(),board());assert.equal(page.context.started,true);assert.equal(page.context.syncOptions.intervalMs,5000);
  const renders=page.renders.length,job=sync(page);assert.match(page.requests[1].url,/cursor=10&limit=200/);assert.equal(page.requests[1].options.signal,page.context.syntheticSignal);
  page.respond(1,{cursor:13,changes:[]});assert.equal((await job).status,'idle');assert.equal(page.run('data.cursor'),13);assert.equal(page.renders.length,renders);assert.equal(page.requests.length,2);
  page.run('startWorkspaceLiveSync()');assert.equal(page.requests.length,2);
 });
 await test('two open views catch daily changes and preserve filters, page, table and page scroll',async()=>{
  const pages=await Promise.all([start(harness(),board(130)),start(harness(),board(130))]);
  for(const page of pages){page.field('accountListName').value='合成昵称';page.field('accountListName').oninput();page.field('accountNext').onclick();assert.equal(page.run('listPage'),2);}
  for(const page of pages){const job=sync(page);page.respond(1,{cursor:11,changes:[{entity:'daily:synthetic-51:2026-10-27',after:{published:1},record_version:'synthetic-day-v2'}]});assert.equal((await job).status,'updated');assert.equal(page.run('data.accounts[51].published'),true);assert.equal(page.run('listPage'),2);assert.equal(page.field('accountListName').value,'合成昵称');assert.deepEqual(Array.from(page.context.lastScroll),[4,220]);assert.equal(page.context.scrollNodes[0].scrollLeft,73);assert.equal(page.requests.length,2);assert.match(rows(page),/合成昵称51/);}
 });
 await test('another date advances only the cursor without changing selected-day publication',async()=>{
  const page=await start(harness(),board()),renders=page.renders.length,job=sync(page);page.respond(1,{cursor:12,changes:[{entity:'daily:synthetic-0:2026-10-26',after:{published:true}}]});assert.equal((await job).status,'idle');assert.equal(page.run('data.accounts[0].published'),false);assert.equal(page.renders.length,renders);assert.equal(page.run('data.cursor'),12);
 });
 await test('business changes refresh only the active detail, and apply current authorized metadata',async()=>{
  const page=await start(harness(),board(120));page.field('accountListOffice').value='one';page.field('accountListName').value='合成昵称';page.field('accountListName').oninput();page.field('accountNext').onclick();
  const job=sync(page);page.respond(1,{cursor:11,changes:[{entity:'account:synthetic-51',after:{name:'合成昵称已更新'}}]});await flush();assert.equal(page.requests.length,3);assert.match(page.requests[2].url,/view=detail/);assert.equal(page.requests[2].options.signal,page.context.syntheticSignal);
  const updated=board(120,'detail',12);updated.accounts[51].name='合成昵称已更新';updated.user.permissions=['history_view'];page.respond(2,updated);assert.equal((await job).status,'updated');assert.match(rows(page),/合成昵称已更新/);assert.equal(page.run('listPage'),2);assert.equal(page.field('accountListOffice').value,'one');assert.equal(page.field('accountListName').value,'合成昵称');assert.equal(page.run('can("publishing_manage")'),false);assert.equal(page.run('data.cursor'),12);
 });
 await test('work summary refresh stays light; the review filter and page remain selected',async()=>{
  const page=harness('review'),value=board(2,'summary');value.review_issues=Array.from({length:110},(_,i)=>({kind:'missing_name',kind_label:'昵称待补充',office_ids:['one'],key:'synthetic-issue-'+i,account_id:'synthetic-0',current_name:'合成待核对'+i,reason:'合成原因'}));await start(page,value);page.field('reviewQuery').value='合成待核对';page.field('reviewQuery').oninput();page.field('reviewNext').onclick();
  const job=sync(page);page.respond(1,{cursor:11,reload:true});await flush();assert.match(page.requests[2].url,/view=work/);const updated=structuredClone(value);updated.cursor=12;page.respond(2,updated);assert.equal((await job).status,'updated');assert.equal(page.run('reviewPage'),2);assert.equal(page.field('reviewQuery').value,'合成待核对');assert.match(page.field('reviewRows').innerHTML,/合成待核对50/);
 });
 await test('every dialog, focused input, write, unknown save and import/restore view blocks reads',async()=>{
  const page=await start(harness(),board());
  for(const id of ['nurtureDialog','positionDialog','overviewDialog','batchEditor','deviceWorkflowDialog','customDialog']){page.field(id).open=true;assert.equal((await sync(page)).status,'deferred');page.field(id).open=false;}
  page.context.document.activeElement={tagName:'INPUT',value:'正在输入'};assert.equal((await sync(page)).status,'deferred');page.context.document.activeElement=null;
  for(const state of ['workspaceWrites=1','workspaceEditLoading=1','pendingRequestIds.set("synthetic","pending")','tab="exchange"','tab="backups"']){page.run(state);assert.equal((await sync(page)).status,'deferred');page.run('workspaceWrites=0;workspaceEditLoading=0;pendingRequestIds.clear();tab="accounts"');}
  assert.equal(page.requests.length,1);
 });
 await test('editing begins during a cursor read: text, snapshot, cursor and rendering stay intact',async()=>{
  const page=await start(harness(),board()),renders=page.renders.length,job=sync(page);page.field('nurtureDialog').open=true;page.field('verificationNote').value='本地草稿';page.run('workspaceMutationEpoch++');page.respond(1,{cursor:11,changes:[{entity:'account:synthetic-0',after:{name:'远端名称'}}]});assert.equal((await job).status,'deferred');assert.equal(page.run('data.cursor'),10);assert.equal(page.run('data.accounts[0].name'),'合成昵称0');assert.equal(page.field('verificationNote').value,'本地草稿');assert.equal(page.renders.length,renders);
 });
 await test('an opened then closed editor still invalidates the old response; retry catches up',async()=>{
  const page=await start(harness(),board()),job=sync(page);page.run('workspaceMutationEpoch+=2');page.respond(1,{cursor:11,changes:[]});assert.equal((await job).status,'deferred');assert.equal(page.run('data.cursor'),10);
  const retry=sync(page);page.respond(2,{cursor:11,changes:[]});assert.equal((await retry).status,'idle');assert.equal(page.run('data.cursor'),11);
 });
 await test('a detail body arriving after input starts is discarded without cursor progress',async()=>{
  const page=await start(harness(),board()),job=sync(page);page.respond(1,{cursor:11,reload:true});await flush();let resolveJSON;page.respondRaw(2,{ok:true,status:200,json:()=>new Promise(resolve=>resolveJSON=resolve)});await flush();page.field('accountListName').value='仍在编辑';page.run('workspaceMutationEpoch++');resolveJSON(board(1,'detail',12));assert.equal((await job).status,'deferred');assert.equal(page.run('data.cursor'),10);assert.equal(page.run('data.accounts.length'),3);assert.equal(page.field('accountListName').value,'仍在编辑');
 });
 await test('date, navigation, manual reload and scheduler lease discard stale cursor results',async()=>{
  for(const mutation of ['$("workDate").value="2026-10-26"','tab="todo"','boardRequestSerial++','syntheticCurrent=false']){const page=await start(harness(),board()),job=sync(page);page.run(mutation);page.respond(1,{cursor:12,changes:[]});assert.equal((await job).status,'waiting');assert.equal(page.run('data.cursor'),10);}
 });
 await test('old server support is explicit and never turns into repeated full reads',async()=>{
  const page=await start(harness(),board());page.run('delete data.cursor');assert.equal((await sync(page)).status,'unsupported');assert.equal(page.requests.length,1);page.run('data.cursor=10');const job=sync(page);page.respondRaw(1,{ok:false,status:404});assert.equal((await job).status,'unsupported');assert.equal(page.requests.length,2);
 });
 await test('invalid 200 replies do not advance cursors; authentication and network failures surface',async()=>{
  for(const invalid of [null,{cursor:9,changes:[]},{cursor:12,changes:{}},{cursor:12,changes:[null]}]){const page=await start(harness(),board()),job=sync(page);page.respond(1,invalid===null?{invalid:true}:invalid);await assert.rejects(job,/格式/);assert.equal(page.run('data.cursor'),10);}
  const auth=await start(harness(),board()),job=sync(auth);auth.respondRaw(1,{ok:false,status:401});await assert.rejects(job,error=>error.status===401);assert.equal(auth.context.redirect,'/login.html');
  const failed=await start(harness(),board()),pending=sync(failed);failed.reject(1,'synthetic offline');await assert.rejects(pending,/offline/);assert.equal(failed.run('data.cursor'),10);
 });
 await test('nurture opens from one fresh data/version snapshot and conflict never refreshes its draft token',async()=>{
  const page=harness('nurture'),value=board();value.nurture.records=[record()];await start(page,value);page.dispatch('[data-nurture]',{nurture:'synthetic-0'});assert.equal(page.requests.length,2);assert.match(page.requests[1].url,/version_scope=day/);assert.doesNotMatch(page.requests[1].url,/view=/);
  const updated=board(3,'detail',12);updated.nurture.records=[record()];updated.accounts[0].name='刷新后昵称';updated.record_versions={'account:synthetic-0':'synthetic-account-v1','nurture:synthetic-0':'synthetic-nurture-v1'};page.respond(1,updated);await flush();assert.equal(page.field('nurtureDialog').open,true);assert.match(page.field('nurtureDialog').innerHTML,/刷新后昵称/);assert.equal(page.run('capturedVersions.get("nurtureDialog")["nurture:synthetic-0"]'),'synthetic-nurture-v1');
  page.field('verificationNote').value='保留草稿';page.run('data.record_versions["nurture:synthetic-0"]="synthetic-newer-version"');
  const save=page.run('nurtureAction("/verification",{id:"synthetic-0",verification_note:$("verificationNote").value,request_id:claimRequestId("nurture-verification:synthetic-0")})');await flush();assert.equal(page.requests.length,3);assert.match(page.requests[2].url,/nurture\/verification/);const sent=JSON.parse(page.requests[2].options.body);assert.equal(sent._expected_versions['nurture:synthetic-0'],'synthetic-nurture-v1');page.respondRaw(2,{ok:false,status:409,json:async()=>({error:'资料已被其他人修改'})});await save;assert.equal(page.field('nurtureDialog').open,true);assert.equal(page.field('verificationNote').value,'保留草稿');assert.match(page.field('nurtureError').textContent,/其他人/);assert.equal((await sync(page)).status,'deferred');
 });
 await test('new nurture record keeps missing frozen versions null, never fetching a new save token',async()=>{
  const page=await start(harness(),board());page.run('captureVersions("nurtureDialog");freezeWorkspaceVersions("nurtureDialog")');const job=page.run('send("/api/workspace/nurture/join",{id:"synthetic-0"},capturedVersions.get("nurtureDialog"))');await flush();assert.equal(page.requests.length,2);assert.match(page.requests[1].url,/nurture\/join/);assert.equal(JSON.parse(page.requests[1].options.body)._expected_versions['nurture:synthetic-0'],null);page.respond(1,{});await job;
 });
 await test('position editor relocates a reordered cell by batch, computer, slot and account',async()=>{
  const page=harness('positions'),value=board();value.publishing_layout.cells=[{computer_id:'synthetic-computer',slot:1,account_id:'synthetic-0',label:'合成安排'}];await start(page,value);page.dispatch('[data-position]',{position:'0'});assert.equal(page.requests.length,2);
  const updated=board(3,'detail',12);updated.publishing_layout.cells=[{computer_id:'synthetic-computer',slot:2,account_id:null,label:''},{...value.publishing_layout.cells[0],label:'刷新后的安排'}];updated.record_versions={'layout:synthetic-batch:synthetic-computer:1':'synthetic-layout-v1'};page.respond(1,updated);await flush();assert.equal(page.run('positionCell.slot'),1);assert.match(page.field('positionSource').textContent,/刷新后的安排/);assert.equal(page.field('positionDialog').open,true);assert.equal(page.run('capturedVersions.get("positionDialog")["layout:synthetic-batch:synthetic-computer:1"]'),'synthetic-layout-v1');
 });
 await test('batch editor preserves the chosen identities and captures refreshed versions before preview',async()=>{
  const page=harness('nurture'),value=board();value.nurture.records=[record()];await start(page,value);page.run('batchSelection("verification").add("synthetic-0")');page.dispatch('[data-batch-open]',{batchOpen:'verification',batchSource:'verification'});assert.equal(page.requests.length,2);
  const updated=board(3,'detail',12);updated.nurture.records=[record()];updated.record_versions={'account:synthetic-0':'synthetic-account-v2','nurture:synthetic-0':'synthetic-nurture-v2'};page.respond(1,updated);await flush();assert.equal(page.field('batchEditor').open,true);assert.equal(page.run('batchSelection("verification").has("synthetic-0")'),true);assert.equal(page.run('capturedVersions.get("batchEditor")["nurture:synthetic-0"]'),'synthetic-nurture-v2');assert.equal((await sync(page)).status,'deferred');
 });
 await test('historical queries keep selected entity and older-page offset after a remote change',async()=>{
  const page=await start(harness('history'),board());page.field('historyEntity').value='synthetic-0';page.run('historyBefore=91;historyPageBefore=191');
  const job=sync(page);page.respond(2,{cursor:11,changes:[{entity:'daily:synthetic-0:2026-10-26',after:{published:true}}]});await flush();page.respond(3,board(3,'detail',12));await flush();assert.match(page.requests[4].url,/entity=account%3Asynthetic-0&before=191/);page.respond(4,{entries:[],next_before:91});assert.equal((await job).status,'updated');assert.equal(page.field('historyEntity').value,'synthetic-0');assert.equal(page.run('historyPageBefore'),191);
 });
 await test('an ignored abort cannot keep the adapter flight stuck or clear a newer flight',async()=>{
  const page=await start(harness(),board()),old=sync(page),oldSignal=page.context.syntheticSignal;let resolveJSON;
  page.respondRaw(1,{ok:true,status:200,json:()=>new Promise(resolve=>resolveJSON=resolve)});await flush();oldSignal.aborted=true;
  const newer=sync(page);assert.equal(page.requests.length,3);resolveJSON({cursor:11,changes:[]});assert.equal((await old).status,'waiting');assert.equal(page.run('data.cursor'),10);
  const joined=invoke(page);assert.equal(page.requests.length,3);page.respond(2,{cursor:12,changes:[]});assert.equal((await newer).status,'idle');assert.equal((await joined).status,'idle');assert.equal(page.run('data.cursor'),12);
 });
 await test('completed filter selection keeps syncing, and focused text restores focus and caret after typing settles',async()=>{
  const page=await start(harness(),board()),filter=page.field('accountListName');filter.tagName='INPUT';filter.value='合成昵称';filter.selectionStart=2;filter.selectionEnd=4;filter.selectionDirection='forward';filter.focus=()=>{page.context.document.activeElement=filter;page.focusRestored=true;};filter.setSelectionRange=(...args)=>page.selectionRestored=args;page.context.document.activeElement=filter;
  const input=page.listeners.find(row=>row.type==='input'&&row.capture);input.callback({target:filter});assert.equal((await sync(page)).status,'deferred');page.run('workspaceTypingUntil=0');
  const job=sync(page);page.respond(1,{cursor:11,changes:[{entity:'daily:synthetic-0:2026-10-27',after:{published:true}}]});assert.equal((await job).status,'updated');assert.equal(filter.value,'合成昵称');assert.equal(page.focusRestored,true);assert.deepEqual(page.selectionRestored,[2,4,'forward']);
  const office=page.field('accountListOffice');office.tagName='SELECT';page.context.document.activeElement=office;const next=sync(page);page.respond(2,{cursor:12,changes:[]});assert.equal((await next).status,'idle');
 });
 await test('device workflow waits for one fresh snapshot and abandons it if another edit begins',async()=>{
  const page=await start(harness('todo'),board(3,'summary'));page.context.DeviceWorkflow={isOpen:()=>false,openAccount:id=>{page.workflowOpened=id;}};
  page.dispatch('[data-workflow-device],[data-workflow-card],[data-workflow-account],[data-workflow-case]',{workflowAccount:'synthetic-0'});assert.equal(page.requests.length,2);assert.equal(page.context.syncOptions.ready(),false);assert.equal(page.context.syncOptions.blocked(),true);
  page.run('workspaceMutationEpoch++');page.respond(1,board(3,'detail',12));await flush();assert.equal(page.workflowOpened,undefined);assert.equal(page.run('data.cursor'),10);
  page.dispatch('[data-workflow-device],[data-workflow-card],[data-workflow-account],[data-workflow-case]',{workflowAccount:'synthetic-0'});page.respond(2,board(3,'detail',13));await flush();assert.equal(page.workflowOpened,'synthetic-0');assert.equal(page.requests.length,3);
 });
 await test('nurture join recomputes available identities using the fresh snapshot',async()=>{
  const page=harness('nurture'),value=board();value.nurture.records=[record()];await start(page,value);page.field('nurtureJoin').onclick();assert.equal(page.requests.length,2);
  const updated=board(3,'detail',12);updated.nurture.records=[record(),record('synthetic-1')];page.respond(1,updated);await flush();assert.match(page.field('joinChoice').innerHTML,/synthetic-2/);assert.doesNotMatch(page.field('joinChoice').innerHTML,/value="synthetic-[01]"/);assert.equal(page.field('nurtureDialog').open,true);
 });
 await test('an older history render cannot replace a reopened history view',async()=>{
  const page=await start(harness('history'),board()),old=page.run('renderHistory()'),fresh=page.run('renderHistory()');page.respond(3,{entries:[],next_before:9});await fresh;const html=page.field('historyRows').innerHTML;
  page.respond(2,{entries:[{entity:'account:synthetic-0',at:1,actor_name:'旧响应',action:'/api/account',changes:[]}],next_before:91});await old;assert.equal(page.field('historyRows').innerHTML,html);assert.equal(page.run('historyBefore'),9);
 });
 await test('batch checkbox focus and chosen identities survive a safe background refresh',async()=>{
  const page=harness('nurture'),value=board();value.nurture.records=[record()];await start(page,value);page.run('batchSelection("verification").add("synthetic-0");renderNurture()');
  const checkbox={tagName:'INPUT',type:'checkbox',value:'synthetic-0',dataset:{batchPick:'verification'},focus(){page.checkboxFocused=true;}};page.context.document.activeElement=checkbox;const lookup=page.context.document.querySelectorAll;page.context.document.querySelectorAll=selector=>selector==='[data-batch-pick]'?[checkbox]:lookup(selector);
  const job=sync(page);page.respond(1,{cursor:11,changes:[{entity:'nurture:synthetic-0',after:{verification_owner:'更新负责人'}}]});await flush();const updated=board(3,'detail',12);updated.nurture.records=[{...record(),verification_owner:'更新负责人'}];page.respond(2,updated);assert.equal((await job).status,'updated');assert.equal(page.run('batchSelection("verification").has("synthetic-0")'),true);assert.match(page.field('nurtureRows').innerHTML,/value="synthetic-0"[^>]*checked/);assert.equal(page.checkboxFocused,true);
  page.checkboxFocused=false;const again=sync(page);page.respond(3,{cursor:13,changes:[{entity:'daily:synthetic-0:2026-10-27',after:{published:true}}]});assert.equal((await again).status,'updated');assert.equal(page.run('batchSelection("verification").has("synthetic-0")'),true);assert.match(page.field('nurtureRows').innerHTML,/value="synthetic-0"[^>]*checked/);assert.equal(page.checkboxFocused,true);assert.equal(page.run('data.cursor'),13);
 });
 console.log('PASS workspace live sync: '+passed+' groups (synthetic DOM/fetch, actual scripts)');
})().catch(error=>{console.error(error);process.exitCode=1;});
