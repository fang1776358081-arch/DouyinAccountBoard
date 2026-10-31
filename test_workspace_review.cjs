// Actual workspace functions with controlled DOM and fake Fetch only.
// No browser, server, business database, source sheet or file output.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const assert=require('node:assert/strict');
const read=name=>fs.readFileSync(path.join(__dirname,name),'utf8');
const navigationHarness=read('test_workspace_navigation.cjs');
const boundary=navigationHarness.indexOf('const flush = () =>');
assert.ok(boundary>0,'reuse only the reviewed harness declarations');
const fixture={require,console,URL,URLSearchParams,__dirname};
const harnessPrefix=navigationHarness.slice(0,boundary),exposedHarness=harnessPrefix.replace('return {context, field, requests, renders, run, navigate, respond, respondRaw, reject,','return {listeners, context, field, requests, renders, run, navigate, respond, respondRaw, reject,');
assert.notEqual(exposedHarness,harnessPrefix,'expose only the fixture listener collection');
vm.createContext(fixture);vm.runInContext(exposedHarness,fixture);
let passed=0;
const test=async(name,job)=>{await job();passed++;console.log('PASS '+name);};
function issue(index,overrides={}){return {key:'synthetic-issue-'+index,kind:'missing_name',kind_label:'昵称待补充',office_ids:['one'],account_id:'synthetic-account-'+index,current_name:'合成账号'+index,douyin_id:'synthetic-handle-'+index,source_name:'',source_row:'',source_label:'当前资料',reason:'合成核对原因',...overrides};}
function board(count=0,{offices=[{id:'one',name:'合成办公室一'}],role='staff',scope='assigned',permissions=[],issues=[]}={}){
 const value=fixture.board('summary');value.user={id:7,name:'合成人员',role,scope_mode:scope,office_ids:offices.map(o=>o.id),permissions};value.offices=offices;
 value.computers=[{id:'synthetic-computer',name:'合成电脑'}];
 value.accounts=Array.from({length:count},(_,index)=>({id:'synthetic-account-'+index,name:'合成账号'+index,office_ids:['one'],phone_device_id:String(index+1),computer_id:'synthetic-computer',status:'正常',account_type:'大号',douyin_id:'synthetic-handle-'+index}));
 value.review_issues=issues;return value;
}
function harness(search='?tab=review'){
 const page=fixture.harness(search),downloads=[];
 page.context.AccountReview={fromBoard:value=>value.review_issues||[],importRows:value=>value.import_issues||[],identityComplete:value=>value.identity_complete!==false,toCSV:rows=>{page.csvRows=structuredClone(rows);return '\ufeff合成CSV';}};
 page.run(read('static/field-search.js'));page.run(read('static/identity-labels.js'));
 page.context.URL={createObjectURL(blob){downloads.push(blob);return 'blob:synthetic';},revokeObjectURL(){}};
 page.context.Blob=class{constructor(parts,options){this.parts=parts;this.options=options;}};
 page.context.setTimeout=fn=>fn();
 page.context.document.createElement=tag=>({tagName:tag,click(){page.downloadClicked=true;},before(){},append(){},remove(){}});
 return Object.assign(page,{downloads});
}
async function start(page,value){const job=page.start();page.respond(0,value);await job;return page;}
const content=page=>page.field('workspaceBody').innerHTML;
const reviewHTML=page=>page.field('reviewRows').innerHTML;
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function clickOverview(page,id){const listener=page.listeners.find(item=>item.type==='click'&&!item.capture&&item.callback.toString().includes('[data-work-tab]'));return listener.callback({target:{closest:selector=>selector==='[data-overview]'?{dataset:{overview:id}}:null}});}
(async()=>{
 await test('account overview clamps an old page after a smaller refresh',async()=>{
  const page=harness('?tab=accounts'),initial=board(151);initial.workspace_view='detail';await start(page,initial);
  for(let i=0;i<3;i++)page.field('accountNext').onclick();assert.equal(page.run('listPage'),4);
  const refresh=page.field('reloadWorkspace').onclick(),smaller=board(20);smaller.workspace_view='detail';page.respond(1,smaller);await refresh;
  assert.equal(page.run('listPage'),1);assert.equal((page.field('accountListRows').innerHTML.match(/data-overview=/g)||[]).length,20);
  assert.match(page.field('accountListRows').innerHTML,/1 \/ 1 页，共 20 个账号/);
  const empty=page.field('reloadWorkspace').onclick(),none=board(0);none.workspace_view='detail';page.respond(2,none);await empty;
  assert.equal(page.run('listPage'),1);assert.match(page.field('accountListRows').innerHTML,/1 \/ 1 页，共 0 个账号/);
 });
 await test('review uses the work summary, authorized offices and no new requests',async()=>{
  const page=harness(),value=board(2,{issues:[issue(0),issue(1,{office_ids:['two'],current_name:'不可见合成名称'}),issue(2,{office_ids:[],current_name:'待分配合成名称'}),issue(3,{office_ids:['one','two'],current_name:'共享合成名称'})]});
  const original=JSON.stringify(value);await start(page,value);
  assert.match(page.requests[0].url,/view=work/);assert.equal(page.requests.length,1);
  assert.match(reviewHTML(page),/合成账号0/);assert.match(reviewHTML(page),/共享合成名称/);assert.doesNotMatch(reviewHTML(page),/不可见合成名称|待分配合成名称/);
  assert.doesNotMatch(content(page),/<option value="two">|<option value="pending">/);assert.equal(JSON.stringify(value),original);
  assert.equal(page.field('workspaceTitle').textContent,'资料待核对');assert.equal(page.context.document.title,'资料待核对 · 账号工作台');
 });
 await test('pending follows the existing server all-office scope instead of redefining roles',async()=>{
  const rows=[issue(0,{office_ids:[],current_name:'合成待分配'})],owner=harness();await start(owner,board(0,{role:'owner',scope:'all',issues:rows}));
  assert.match(content(owner),/<option value="pending">待分配/);assert.match(reviewHTML(owner),/合成待分配/);
  const admin=harness();await start(admin,board(0,{role:'admin',scope:'all',issues:rows}));
  assert.match(content(admin),/<option value="pending">待分配/);assert.match(reviewHTML(admin),/合成待分配/);
  const assigned=harness();await start(assigned,board(0,{role:'owner',scope:'assigned',issues:rows}));
  assert.doesNotMatch(content(assigned),/<option value="pending">/);assert.doesNotMatch(reviewHTML(assigned),/合成待分配/);
 });
 await test('filters combine office, type and text; reset restores the result',async()=>{
  const page=harness();await start(page,board(3,{offices:[{id:'one',name:'合成办公室一'},{id:'two',name:'合成办公室二'}],issues:[issue(0,{kind:'missing_id',kind_label:'抖音号待补充'}),issue(1,{current_name:'合成查找样例'}),issue(2,{office_ids:['two'],current_name:'另一合成样例'})]}));
  page.field('reviewOffice').value='one';page.field('reviewOffice').onchange();page.field('reviewKind').value='missing_name';page.field('reviewKind').onchange();page.field('reviewQuery').value='合成查找';page.field('reviewQuery').oninput();
  assert.match(page.field('reviewSummary').textContent,/当前 1 项/);assert.match(reviewHTML(page),/合成查找样例/);assert.doesNotMatch(reviewHTML(page),/另一合成样例|合成账号0/);
  page.field('resetReviewFilters').onclick();assert.match(page.field('reviewSummary').textContent,/当前 3 项/);assert.equal(page.field('reviewQuery').value,'');
 });
 await test('review pagination clamps after refresh and renders only fifty rows',async()=>{
  const page=harness();await start(page,board(0,{issues:Array.from({length:121},(_,i)=>issue(i))}));
  assert.equal((reviewHTML(page).match(/review-kind/g)||[]).length,50);page.field('reviewNext').onclick();page.field('reviewNext').onclick();assert.equal(page.run('reviewPage'),3);
  const refresh=page.field('reloadWorkspace').onclick();page.respond(1,board(0,{issues:[issue(0),issue(1)]}));await refresh;
  assert.equal(page.run('reviewPage'),1);assert.match(reviewHTML(page),/第 1 \/ 1 页，共 2 项/);
 });
 await test('review export rechecks permissions and exports only the current filter',async()=>{
  const denied=harness();await start(denied,board(0,{issues:[issue(0)]}));assert.equal(denied.field('exportReview').disabled,true);denied.field('exportReview').onclick();
  assert.equal(denied.downloads.length,0);assert.match(denied.field('reviewError').textContent,/没有导出资料权限/);assert.equal(denied.requests.length,1);
  const allowed=harness();await start(allowed,board(2,{permissions:['data_export'],issues:[issue(0),issue(1)]}));
  allowed.field('reviewQuery').value='synthetic-handle-1';allowed.field('reviewQuery').oninput();allowed.field('exportReview').onclick();
  assert.equal(allowed.csvRows.length,1);assert.equal(allowed.csvRows[0].douyin_id,'synthetic-handle-1');assert.equal(allowed.downloads.length,1);assert.equal(allowed.downloadClicked,true);assert.equal(allowed.requests.length,1);
 });
 await test('only known account identities get a location link and text is escaped',async()=>{
  const page=harness();await start(page,board(1,{issues:[issue(0,{current_name:'<合成名称>',reason:'<合成原因>'}),issue(1,{account_id:'synthetic-unknown',source_row:'=合成来源',source_label:'<来源>'})]}));
  const html=reviewHTML(page);assert.match(html,/data-overview="synthetic-account-0"/);assert.doesNotMatch(html,/data-overview="synthetic-unknown"|<合成名称>|<合成原因>|<来源>/);assert.match(html,/&lt;合成名称&gt;/);assert.match(html,/按来源核对，不猜测关联/);
 });
 await test('an older server cannot silently report identity checks complete',async()=>{
  const page=harness(),value=board();value.identity_complete=false;await start(page,value);assert.match(content(page),/抖音号检查尚未完成/);assert.equal(page.requests.length,1);
 });
 await test('real model integrates current issues and temporary import rows without extra reads',async()=>{
  const page=harness(),value=board(2,{role:'owner',scope:'all',offices:[{id:'one',name:'合成办公室一'},{id:'three',name:'合成办公室三'}]});
  value.accounts[0].name='昵称待补充';page.run(read('static/account-review.js'));await start(page,value);
  assert.match(page.field('reviewSummary').textContent,/当前 1 项/);
  const preview={mode:'fill_missing_nicknames',office_id:'three',preview_id:'synthetic-preview',rows:[
   {action:'unmatched',row:11,douyin_id:'000syntheticA',source_name:'合成未匹配',reason:'来源没有唯一匹配'},
   {action:'skip',row:12,douyin_id:'syntheticB',source_name:'',current_name:'昵称待补充',reason:'来源名称为空'},
   {action:'conflict',row:13,douyin_id:'',source_name:'合成待核对',reason:'来源缺少标识'},
  ]};
  page.context.syntheticPreview=preview;assert.equal(page.run('AccountReview.captureImport(syntheticPreview,data)'),true);page.run('renderReview()');
  assert.match(page.field('reviewSummary').textContent,/当前 4 项/);assert.match(reviewHTML(page),/000syntheticA|合成未匹配|来源行 11/);
  page.field('reviewOffice').value='three';page.field('reviewOffice').onchange();assert.match(page.field('reviewSummary').textContent,/当前 3 项/);assert.doesNotMatch(reviewHTML(page),/data-overview=/);
  page.run("data.user.role='staff';data.user.scope_mode='assigned';data.user.office_ids=['one'];renderReview()");
  assert.equal(page.field('reviewOffice').value,'all');assert.match(page.field('reviewSummary').textContent,/当前 1 项/);assert.doesNotMatch(content(page),/<option value="three">/);assert.doesNotMatch(reviewHTML(page),/合成未匹配|来源行 11/);assert.equal(page.requests.length,1);
 });
 await test('review account details load on demand and use the refreshed authorized row',async()=>{
  const page=harness(),value=board(1,{issues:[issue(0)]});await start(page,value);
  page.field('overviewDialog').showModal=()=>{page.field('overviewDialog').open=true;};
  clickOverview(page,'synthetic-account-0');assert.equal(page.requests.length,2);assert.match(page.requests[1].url,/view=detail/);assert.equal(page.field('overviewDialog').open,false);
  const detail=board(1);detail.workspace_view='detail';detail.accounts[0].name='合成刷新后名称';page.respond(1,detail);await flush();
  assert.equal(page.field('overviewDialog').open,true);assert.match(page.field('overviewTitle').textContent,/^合成刷新后名称/);assert.match(page.field('overviewBody').innerHTML,/synthetic-handle-0/);
  clickOverview(page,'synthetic-account-0');await flush();assert.equal(page.requests.length,2,'already detailed rows must not reload');
  const removed=harness();await start(removed,value);removed.field('overviewDialog').showModal=()=>{removed.field('overviewDialog').open=true;};
  clickOverview(removed,'synthetic-account-0');const noLongerVisible=board(0);noLongerVisible.workspace_view='detail';removed.respond(1,noLongerVisible);await flush();
  assert.equal(removed.field('overviewDialog').open,false);assert.equal(removed.field('overviewBody').innerHTML,'');
  const expired=harness();await start(expired,value);clickOverview(expired,'synthetic-account-0');expired.respondRaw(1,{ok:false,status:401});await flush();
  assert.equal(expired.context.redirect,'/login.html');assert.equal(expired.field('overviewDialog').open,false);
 });
 await test('generic import failures stay in their own module and source changes clear them',async()=>{
  const page=harness('?tab=exchange');page.context.ScopeUI={mount(){},payload(){return {};}};let dualOptions;
  page.context.OfficeThreeImport={mount(host,options){dualOptions=options;}};page.field('importFile').parentElement={before(){}};page.field('importFile').files=[];page.field('workspaceBody').prepend=()=>{};
  const value=board(0,{permissions:['data_import']});value.workspace_view='detail';await start(page,value);
  await page.field('previewImport').onclick();assert.equal(page.field('dataImportError').textContent,'请选择表格文件');assert.equal(page.field('workspaceError').textContent,'');assert.equal(page.requests.length,1);
  page.field('importFile').onchange();assert.equal(page.field('dataImportError').textContent,'');page.field('dataImportError').textContent='合成旧错误';page.field('exchangeKind').onchange();assert.equal(page.field('dataImportError').textContent,'');
  assert.equal(dualOptions.canExport(),false);assert.match(content(page),/id="dataImportError"/);
 });
 const html=read('static/workspace.html');assert.match(html,/data-work-tab="review"/);assert.ok(html.indexOf('/account-review.js')<html.indexOf('/workspace.js'));assert.match(html,/id="workspaceTitle"/);
 console.log('PASS workspace review and pagination: '+passed+' groups');
})().catch(error=>{console.error(error);process.exitCode=1;});
