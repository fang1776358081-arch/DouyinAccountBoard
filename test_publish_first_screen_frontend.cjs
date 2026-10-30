// Actual first-screen scripts with a controlled synthetic DOM, storage and
// fetch. No browser, external network, database, business file or new dependency.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const {performance}=require('node:perf_hooks');
const {harness:baseHarness,fixture:baseFixture,root,scripts:baseScripts}=require('./test_performance_ui.cjs');
const extraScripts=['publishing-match.js','publishing-pending.js','workspace-shortcuts.js','auth-ui.js','board-updates.js'];
const scripts=[...baseScripts,...extraScripts];
const clone=value=>structuredClone(value),tick=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve,reject;const promise=new Promise((ok,no)=>{resolve=ok;reject=no;});return {promise,resolve,reject};};
function fixture(unmatched=0){
 const board=baseFixture(1600);board.user={...board.user,name:'合成测试人员',role:'owner',permissions:['publish','publishing_manage','account_manage','account_status','data_export']};
 for(let index=0;index<unmatched;index++)board.publishing_layout.cells.push({computer_id:'pc-'+index,slot:11,account_id:null,label:'合成待核对名称'+String(index).padStart(4,'0'),office:'one',source_cell:'synthetic-cell-'+index,reason:'合成待核对'});
 return board;
}
function harness(sourceRoot=root,{timing=false}={}){
 const page=baseHarness(sourceRoot),events=[],callbacks=[],requests=[],writes=[],metrics={};
 const measure=(phase,work)=>{const begin=performance.now();try{return work();}finally{(metrics[phase]??=[]).push(performance.now()-begin);}};
 const register=page.context.document.addEventListener;
 page.context.document.addEventListener=(type,listener)=>{callbacks.push({type,listener});register(type,listener);};
 page.context.CustomEvent=class {constructor(type){this.type=type;}};
 page.context.document.dispatchEvent=event=>measure(event.type==='board-ready'?'boardReady':'otherEvent',()=>{events.push(event.type);for(const item of callbacks)if(item.type===event.type)item.listener(event);});
 for(const name of extraScripts)vm.runInContext(fs.readFileSync(path.join(sourceRoot,'static',name),'utf8'),page.context,{filename:name});
 for(const {listener}of callbacks.filter(item=>item.type==='DOMContentLoaded'))listener();
 page.context.location.search='?page=board&office=one';page.run('page="board";activeOffice="one";');
 page.element('pendingReviewPanel').hidden=true;page.element('pendingReviewOffice').value='all';page.element('pendingReviewReason').value='all';
 const setItem=page.context.localStorage.setItem;
 page.context.localStorage.setItem=(key,value)=>measure('storageWrite',()=>{writes.push({key,bytes:Buffer.byteLength(value)});return setItem(key,value);});
 page.context.fetch=async(url,options={})=>{requests.push({url,method:options.method||'GET',body:options.body});if(!page.transport)throw Error('unhandled synthetic request: '+url);return page.transport(url,options);};
 page.response=(body,status=200)=>{const json=JSON.stringify(body);return {ok:status>=200&&status<300,status,bytes:Buffer.byteLength(json),json:async()=>measure('responseJsonParse',()=>JSON.parse(json))};};
 if(timing){
  // Instrument only timing boundaries in the in-memory function text. The
  // frozen/current files are not rewritten and the expression is unchanged.
  page.context.__measure=measure;
  const original=page.run('renderPageContent.toString()'),lines=original.split('\n');let replaced=0;
  const instrumented=lines.map(line=>{const marker="$('matrixBody').innerHTML=",at=line.indexOf(marker);if(at<0||!line.includes('hosts.map('))return line;const rhs=line.slice(at+marker.length).trim();assert.ok(rhs.endsWith(';'));replaced++;return line.slice(0,at)+marker+"__measure('matrixHTML',()=> ("+rhs.slice(0,-1)+'));';}).join('\n');
  assert.equal(replaced,1,'time the unchanged real matrix RHS exactly once');page.run('renderPageContent='+instrumented);
  page.context.__cacheStringify=(...args)=>measure('cacheStringify',()=>JSON.stringify(...args));page.context.__cacheParse=(...args)=>measure('cacheParse',()=>JSON.parse(...args));
  page.run('writeBoardCache='+page.run('writeBoardCache.toString()').replace('JSON.stringify(','__cacheStringify('));page.run('readBoardCache='+page.run('readBoardCache.toString()').replace('JSON.parse(','__cacheParse('));
  for(const [name,phase]of [['createRenderIndex','index'],['render','render'],['writeBoardCache','cacheWriteTotal']]){const originalFunction=page.run(name);page.context[name]=(...args)=>measure(phase,()=>originalFunction(...args));}
 }
 Object.assign(page,{events,callbacks,requests,writes,metrics,measure,
  clearMetrics(){for(const key of Object.keys(metrics))delete metrics[key];requests.length=0;writes.length=0;events.length=0;},
  cache(board,userId=board.user.id){page.context.localStorage.setItem('douyin-board-view',JSON.stringify({userId,board}));},
  cacheValue(){const raw=page.context.localStorage.getItem('douyin-board-view');return raw?JSON.parse(raw):null;},
  selectHost(board){page.reset(board,'board');page.run('renderComputerFilter()');page.element('computer').value='pc-0';},
 });
 return page;
}
async function main(){
 let checks=0;const test=async(name,work)=>{await work();checks++;console.log('ok '+name);};
 await test('cold startup uses authenticated board once, with user/roles/CSRF preserved',async()=>{const p=harness(),board=fixture();p.transport=async url=>{assert.match(url,/^\/api\/board\?version_scope=day/);return p.response(board);};await p.run('openBoard()');assert.equal(p.requests.length,1);assert.equal(p.run('currentUser.id'),board.user.id);assert.equal(p.run('data.user.csrf'),board.user.csrf);assert.equal(p.run('hasPermission("publish")'),true);assert.deepEqual(p.events,['board-ready']);});
 await test('auth UI itself does not issue a second me read',async()=>{const p=harness();assert.equal(p.requests.length,0);assert.ok(!fs.readFileSync(path.join(root,'static','auth-ui.js'),'utf8').includes('/api/auth/me'));});
 await test('corrupt or invalid cache is removed and never paints before a new board',async()=>{for(const cached of ['{invalid',JSON.stringify({userId:'other',board:{}})]){const p=harness(),wait=deferred();p.context.localStorage.setItem('douyin-board-view',cached);p.transport=()=>wait.promise;const opening=p.run('openBoard()');assert.equal(p.run('data'),null);assert.equal(p.cacheValue(),null);assert.equal(p.requests.length,1);assert.ok(p.requests[0].url.startsWith('/api/board?'));wait.resolve(p.response(fixture()));await opening;}});
 await test('cold 401 clears storage and does not paint or cache any board',async()=>{const p=harness();p.context.location.replace=url=>{p.redirect=url;};p.transport=async()=>p.response({error:'synthetic expired'},401);await p.run('openBoard()');assert.equal(p.cacheValue(),null);assert.equal(p.run('data'),null);assert.equal(p.events.length,0);assert.match(p.redirect,/login/);});
 await test('valid warm cache waits for current same-user auth before paint',async()=>{const p=harness(),wait=deferred(),board=fixture();p.cache(board);p.clearMetrics();p.transport=url=>url==='/api/auth/me'?wait.promise:Promise.resolve(p.response({cursor:10,changes:[]}));const opening=p.run('openBoard()');assert.equal(p.run('data'),null);assert.deepEqual(p.requests.map(row=>row.url),['/api/auth/me']);wait.resolve(p.response({user:{id:board.user.id}}));await opening;assert.equal(p.events.length,1);assert.equal(p.requests.length,2);assert.equal(p.writes.length,2,'showBoard and successful catch-up write; no third unconditional tail write');});
 await test('warm me 401 never paints last user cache',async()=>{const p=harness();p.cache(fixture());p.clearMetrics();p.context.location.replace=url=>{p.redirect=url;};p.transport=async()=>p.response({error:'synthetic expired'},401);await p.run('openBoard()');assert.equal(p.cacheValue(),null);assert.equal(p.run('data'),null);assert.equal(p.events.length,0);assert.equal(p.requests.length,1);assert.equal(p.writes.length,0);});
 await test('warm me null/different user clears old snapshot before authoritative load',async()=>{for(const user of [null,{id:'new-owner'}]){const p=harness(),wait=deferred();p.cache(fixture());p.clearMetrics();p.transport=url=>url==='/api/auth/me'?Promise.resolve(p.response({user})):wait.promise;const opening=p.run('openBoard()');await tick();assert.equal(p.cacheValue(),null);assert.equal(p.run('data'),null);assert.equal(p.events.length,0);const next=fixture();next.user.id='new-owner';wait.resolve(p.response(next));await opening;assert.equal(p.run('data.user.id'),'new-owner');assert.equal(p.events.length,1);}});
 await test('warm catch-up revoked response cannot resurrect cleared cache',async()=>{const p=harness(),board=fixture();p.cache(board);p.clearMetrics();p.context.location.replace=url=>{p.redirect=url;};p.transport=async url=>p.response(url==='/api/auth/me'?{user:{id:board.user.id}}:{error:'synthetic revoked'},url==='/api/auth/me'?200:401);await p.run('openBoard()');assert.equal(p.cacheValue(),null);assert.equal(p.writes.length,1,'only the already-authenticated cache paint precedes revoke');assert.match(p.redirect,/login/);});
 await test('cold board errors preserve visible load error instead of fake authenticated paint',async()=>{for(const status of [403,500]){const p=harness();p.transport=async()=>p.response({error:'synthetic load error'},status);await p.run('openBoard()');assert.equal(p.run('data'),null);assert.equal(p.events.length,0);assert.match(p.element('loadError').textContent,/synthetic load error/);}});
 await test('hidden pending listener counts all unmatched cells without invoking suggestions',async()=>{const p=harness(),board=fixture(150);p.run('globalThis.pendingCalls=0;globalThis.realPendingRows=pendingReviewRows;pendingReviewRows=()=>{pendingCalls++;return realPendingRows();};');p.show(board);assert.equal(p.run('pendingCalls'),0);assert.match(p.element('pendingReviewToggle').textContent,/150/);assert.equal(p.element('pendingReviewRows').innerHTML,'');});
 await test('opening/classifying/paging/filtering and visible board-ready remain unchanged',async()=>{const p=harness(),board=fixture(20);p.show(board);p.run('globalThis.pendingCalls=0;globalThis.realPendingRows=pendingReviewRows;pendingReviewRows=()=>{pendingCalls++;return realPendingRows();};');p.element('pendingReviewToggle').onclick();assert.equal(p.element('pendingReviewPanel').hidden,false);assert.equal(p.run('pendingCalls'),1);assert.match(p.element('pendingReviewSummary').textContent,/当前条件 20 格/);assert.match(p.element('pendingReviewRows').innerHTML,/synthetic-cell-14/);assert.doesNotMatch(p.element('pendingReviewRows').innerHTML,/synthetic-cell-15/);p.element('pendingReviewNext').onclick();assert.match(p.element('pendingReviewRows').innerHTML,/synthetic-cell-19/);p.element('pendingReviewCell').value='synthetic-cell-2';p.element('pendingReviewCell').oninput();assert.match(p.element('pendingReviewSummary').textContent,/当前条件 1 格/);const next=fixture(19);p.show(next);assert.ok(p.run('pendingCalls')>=4);assert.match(p.element('pendingReviewToggle').textContent,/19/);p.element('pendingReviewToggle').onclick();const before=p.run('pendingCalls');p.show(fixture(18));assert.equal(p.run('pendingCalls'),before);assert.match(p.element('pendingReviewToggle').textContent,/18/);});
 await test('single-host first screen still has all-field filters and daily account/version baseline',async()=>{const p=harness(),board=fixture(50);p.selectHost(board);p.show(board);assert.equal((p.element('matrixBody').innerHTML.match(/class="host"/g)||[]).length,1);assert.equal((p.element('matrixBody').innerHTML.match(/data-publish=/g)||[]).length,10);assert.equal(p.run('data.accounts.length'),1600);assert.equal(p.run('data.record_versions["account:account-0"]'),'synthetic-account-v0');assert.equal(p.run('data.record_versions["daily:account-0:2026-10-29"]'),'synthetic-daily-v0');assert.match(p.element('pendingReviewToggle').textContent,/50/);});
 console.log(JSON.stringify({passed:true,checks,kind:'real-first-screen-js-synthetic-dom-fetch-storage-events-only'}));
}
module.exports={harness,fixture,root,scripts,extraScripts};
if(require.main===module)main().catch(error=>{console.error(error);process.exitCode=1;});
