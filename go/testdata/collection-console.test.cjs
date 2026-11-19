// Exercise the actual page against an authenticated, in-memory API and DOM fixture.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../internal/webassets/static/collection-console.js'),'utf8');
const html=fs.readFileSync(path.join(__dirname,'../internal/webassets/static/collection.html'),'utf8');
const settle=async()=>{for(let i=0;i<35;i++)await new Promise(setImmediate);};
const reply=(status,result)=>({ok:status>=200&&status<300,status,json:async()=>result});
async function page(options={}){
 const fields=new Map(),requests=[],writes=[],redirects=[],intervals=new Map();let sequence=0,clock=0;
 const field=id=>{if(!fields.has(id))fields.set(id,{id,hidden:false,disabled:false,textContent:'',innerHTML:'',value:'',dataset:{},querySelectorAll:()=>[],showModal(){this.open=true;}});return fields.get(id);};
 const controls=[field('collectionOpenBrowser'),field('cancelWaiting')];controls[0].dataset.collectionCommand='open_browser';controls[1].dataset.collectionCommand='cancel_waiting';
 field('collectionControls').querySelectorAll=()=>controls;
 const state={user:{id:'synthetic-owner',permissions:['collection_manage'],csrf:'synthetic-csrf'},
  ids:options.ids??['scoped-east','scoped-west'],control:options.control??true,manage:options.manage??true,
  collection:{enabled:true,worker_online:true,browser_control:true,worker_status:'待命',paused:false,waiting:0,done:0,failed:0,...options.collection},
  resumePolls:0,jobs:options.jobs??[],total:options.total??0};
 if(options.permissions)state.user.permissions=options.permissions;
 const context={window:{},document:{getElementById:field,visibilityState:'visible',querySelector:()=>field('collectionCancelDialog').open?{}:null},
  URLSearchParams,crypto:{randomUUID:()=>`synthetic-request-${++sequence}`},
  Date:class extends Date{static now(){return clock;}},
  setTimeout:(fn,ms)=>{clock+=ms;return setImmediate(fn);},
  setInterval:(fn,ms)=>{intervals.set(ms,fn);return ms;},clearInterval:id=>intervals.delete(id),location:{replace:url=>redirects.push(url)},
  fetch:async(url,settings={})=>{
   const method=settings.method||'GET',body=settings.body?JSON.parse(settings.body):null;
   const request={url,method,body,workerStatus:state.collection.worker_status};requests.push(request);
   if(method==='POST'){
    assert.equal(settings.headers['X-CSRF-Token'],state.user.csrf);writes.push(request);
   }
   if(options.intercept){const result=await options.intercept(request,state);if(result)return result;}
   if(url==='/api/auth/me')return reply(200,{user:state.user});
   if(url==='/api/board?view=work&version_scope=day')return reply(200,{workspace_view:'summary',accounts:state.ids.map(id=>({id}))});
   if(url==='/api/collection/status'){
    if(state.resumePolls>0&&--state.resumePolls===0)state.collection.worker_status='待命';
    return reply(200,{can_manage:state.manage,can_control:state.control,collection:{...state.collection}});
   }
   if(url.startsWith('/api/collection/jobs?')){
    const query=new URLSearchParams(url.split('?')[1]);
    return reply(200,{user:state.user,jobs:state.jobs,total:state.total,page:Number(query.get('page')),page_size:50});
   }
   if(url==='/api/collection/settings'){
    if(body.resume){state.collection.paused=false;state.collection.pause_reason='';state.collection.worker_status=options.stuck?'等待手动登录或验证':'待命';state.resumePolls=options.resumePolls||0;if(state.resumePolls)state.collection.worker_status='等待手动登录或验证';}
    if(body.pause)state.collection.paused=true;
    if(body.cancel_waiting)state.collection.waiting=0;
    return reply(200,{ok:true,cancelled:2});
   }
   if(url==='/api/collection/enqueue'){
    assert.match(state.collection.worker_status,/^(待命|正在采集)/,'must wait for a real ready response before enqueue');
    assert.equal(state.collection.paused,false);state.collection.waiting+=body.ids.length;
    return reply(200,{queued:body.ids.length,duplicate:0,invalid:0,missing:0});
   }
   if(url.startsWith('/api/idempotency/result?'))return reply(404,{error:'尚未查到回执'});
   throw Error('unexpected request: '+url);
  }};
 vm.createContext(context);vm.runInContext(source,context);await settle();
 return {field,state,controls,writes,requests,redirects,
  click:async id=>{field(id).onclick();await settle();},
  poll:async()=>{intervals.get(8000)?.();await settle();},
  retry:async id=>{field('collectionRows').onclick({target:{closest:()=>({dataset:{retry:id}})}});await settle();}};
}
const posts=(page,url)=>page.writes.filter(request=>request.url===url);
const cases=[];
async function test(name,run){await run();cases.push(name);}
(async()=>{
 await test('visible primary controls and honest historical counters',async()=>{
  assert.match(html,/id="collectionStart"[^>]*>开始采集/);assert.match(html,/id="collectionPause"[^>]*>暂停采集/);
  assert.match(html,/授权范围最近结果 · 待处理包含采集中/);assert.doesNotMatch(html,/collectionRefresh|继续队列|队列设置/);
  assert.ok(html.indexOf('collectionStart')<html.indexOf('collectionSheet'));
  const p=await page({collection:{done:17,waiting:0,failed:3}});
  assert.equal(String(p.field('collectionDone').textContent),'17');assert.equal(String(p.field('collectionFailed').textContent),'3');
  assert.match(p.field('collectionScope').textContent,/全部授权账号 2 个/);assert.equal(p.writes.length,0);
  await p.click('collectionStart');assert.equal(posts(p,'/api/collection/enqueue').length,1);
  assert.deepEqual(posts(p,'/api/collection/enqueue')[0].body.ids,['scoped-east','scoped-west']);
  assert.equal(String(p.field('collectionDone').textContent),'17','old successes are never reset or called this batch progress');
  assert.equal(String(p.field('collectionWaiting').textContent),'2');
 });
 await test('paused start resumes then waits for worker readiness before enqueue',async()=>{
  const p=await page({collection:{paused:true,worker_status:'等待手动登录或验证'},resumePolls:2});
  await p.click('collectionStart');
  assert.deepEqual(posts(p,'/api/collection/settings').map(x=>x.body),[{resume:true}]);
  assert.equal(posts(p,'/api/collection/enqueue').length,1);assert.equal(p.field('collectionError').hidden,true);
 });
 await test('existing queued or running work only resumes and never enqueues again',async()=>{
  const p=await page({collection:{waiting:4,paused:true}});
  assert.match(p.field('collectionScope').textContent,/现有共享队列.*4/);
  await p.click('collectionStart');assert.equal(posts(p,'/api/collection/enqueue').length,0);
  assert.deepEqual(posts(p,'/api/collection/settings').map(x=>x.body),[{resume:true}]);
  assert.equal(p.field('collectionStart').disabled,true,'an active queue must not get another start submission');
  await p.click('collectionPause');assert.equal(p.state.collection.paused,true);
  assert.equal(String(p.field('collectionWaiting').textContent),'4');assert.match(p.field('collectionNotice').textContent,/当前任务完成后停止/);
  await p.click('collectionStart');assert.equal(posts(p,'/api/collection/enqueue').length,0);
 });
 await test('double click cannot enqueue twice',async()=>{
  let release,called=false;const gate=new Promise(resolve=>{release=resolve;});
  const p=await page({intercept:async request=>{if(request.url==='/api/collection/enqueue'&&!called){called=true;await gate;}}});
  p.field('collectionStart').onclick();p.field('collectionStart').onclick();await settle();
  assert.equal(posts(p,'/api/collection/enqueue').length,1);assert.equal(p.field('collectionStart').disabled,true);
  release();await settle();assert.equal(posts(p,'/api/collection/enqueue').length,1);
 });
 await test('bounded verification wait fails visibly and polling never later enqueues',async()=>{
  const p=await page({collection:{paused:true,worker_status:'等待手动登录或验证'},stuck:true});
  await p.click('collectionStart');assert.equal(posts(p,'/api/collection/enqueue').length,0);
  assert.match(p.field('collectionError').textContent,/尚未确认就绪/);assert.equal(p.field('collectionStart').disabled,false);
  p.state.collection.worker_status='待命';await p.poll();assert.equal(posts(p,'/api/collection/enqueue').length,0);
  await p.click('collectionStart');assert.equal(posts(p,'/api/collection/enqueue').length,1);
 });
 await test('offline and re-paused workers do not create tasks',async()=>{
  const offline=await page({collection:{worker_online:false,browser_control:false}});await offline.click('collectionStart');
  assert.equal(offline.writes.length,0);assert.match(offline.field('collectionError').textContent,/离线/);assert.equal(offline.field('collectionStart').disabled,false);
  const paused=await page({collection:{paused:true},intercept:request=>request.body?.resume?reply(200,{ok:true}):null});
  await paused.click('collectionStart');assert.equal(posts(paused,'/api/collection/enqueue').length,0);assert.match(paused.field('collectionError').textContent,/仍已暂停/);
 });
 await test('permissions and disabled collection preserve read only access',async()=>{
  const restricted=await page({control:false,collection:{paused:true}});assert.equal(restricted.field('collectionStart').disabled,true);
  await restricted.click('collectionStart');restricted.field('collectionOpenBrowser').onclick();await settle();assert.equal(restricted.writes.length,0);
  assert.equal(restricted.field('collectionOpenBrowser').hidden,true);assert.match(restricted.field('collectionStatus').textContent,/管理员继续/);
  const disabled=await page({collection:{enabled:false}});await disabled.click('collectionStart');assert.equal(disabled.writes.length,0);assert.match(disabled.field('collectionStatus').textContent,/暂未启用/);
  const denied=await page({permissions:[]});assert.equal(denied.requests.length,1);assert.equal(denied.writes.length,0);
 });
 await test('ready employees enqueue only their authorized targets without control writes',async()=>{
  const employee=await page({control:false,ids:['office-east-only']});
  assert.equal(employee.field('collectionStart').disabled,false);assert.equal(employee.field('collectionPause').disabled,true);
  await employee.click('collectionStart');assert.deepEqual(posts(employee,'/api/collection/enqueue')[0].body.ids,['office-east-only']);
  assert.equal(posts(employee,'/api/collection/settings').length,0);
  const active=await page({control:false,collection:{waiting:1}});assert.equal(active.field('collectionStart').disabled,true);
  await active.click('collectionStart');assert.equal(active.writes.length,0);
 });
 await test('startup wait is not falsely labelled an upstream security challenge',async()=>{
  const startup=await page({collection:{worker_status:'等待手动登录或验证'}});
  assert.match(startup.field('collectionStatus').textContent,/待开始/);assert.doesNotMatch(startup.field('collectionStatus').textContent,/完成验证|安全验证|重新登录/);
  await startup.click('collectionStart');assert.deepEqual(posts(startup,'/api/collection/settings').map(x=>x.body),[{resume:true}]);
  assert.equal(posts(startup,'/api/collection/enqueue').length,1);
  const challenge=await page({collection:{paused:true,pause_reason:'抖音搜索需要安全验证，请手动完成'}});
  assert.match(challenge.field('collectionStatus').textContent,/安全验证.*采集帮助打开窗口/);assert.equal(challenge.writes.length,0);
 });
 await test('401 and 403 never bypass authentication or hide the error',async()=>{
  const unauth=await page({intercept:request=>request.url==='/api/auth/me'?reply(401,{error:'请重新登录'}):null});
  assert.deepEqual(unauth.redirects,['/login.html?next=%2Fcollection.html']);assert.equal(unauth.writes.length,0);
  const forbidden=await page({intercept:request=>request.url==='/api/collection/status'?reply(403,{error:'没有权限'}):null});
  await forbidden.click('collectionStart');assert.equal(forbidden.writes.length,0);assert.match(forbidden.field('collectionError').textContent,/没有权限/);
 });
 await test('late successful reads cannot restore scope or controls after an authorization denial',async()=>{
  let releaseStatus;const statusGate=new Promise(resolve=>{releaseStatus=resolve;});
  const scopeDenied=await page({intercept:request=>request.url==='/api/collection/status'?statusGate:
   request.url.startsWith('/api/board?')?reply(403,{error:'范围权限已撤销'}):null});
  releaseStatus(reply(200,{can_manage:true,can_control:true,collection:{...scopeDenied.state.collection}}));await settle();
  assert.equal(String(scopeDenied.field('collectionDone').textContent),'—');assert.equal(scopeDenied.field('collectionSheet').hidden,true);
  assert.equal(scopeDenied.field('collectionOpenBrowser').hidden,true);assert.equal(scopeDenied.writes.length,0);
  let releaseScope;const scopeGate=new Promise(resolve=>{releaseScope=resolve;});
  const statusDenied=await page({intercept:request=>request.url==='/api/collection/status'?reply(403,{error:'没有权限'}):
   request.url.startsWith('/api/board?')?scopeGate:null});
  releaseScope(reply(200,{workspace_view:'summary',accounts:[{id:'old-user-only'}]}));await settle();
  assert.doesNotMatch(statusDenied.field('collectionScope').textContent,/全部授权账号 1/);
  assert.equal(String(statusDenied.field('collectionDone').textContent),'—');assert.equal(statusDenied.writes.length,0);
 });
 await test('CSRF rejection is visible and can be retried with fresh reads',async()=>{
  let rejected=false;const p=await page({collection:{paused:true},intercept:request=>{
   if(request.body?.resume&&!rejected){rejected=true;return reply(403,{error:'页面已过期，请重试'});}
  }});
  await p.click('collectionStart');assert.match(p.field('collectionError').textContent,/页面已过期/);assert.equal(posts(p,'/api/collection/enqueue').length,0);
  assert.equal(p.field('collectionStart').disabled,false);await p.click('collectionStart');
  assert.match(p.field('collectionScope').textContent,/全部授权账号 2/);await p.click('collectionStart');assert.equal(posts(p,'/api/collection/enqueue').length,1);
 });
 await test('uncertain enqueue reuses original idempotency request rather than another batch',async()=>{
  let failed=false;const p=await page({intercept:request=>{if(request.url==='/api/collection/enqueue'&&!failed){failed=true;throw Error('synthetic network failure');}}});
  await p.click('collectionStart');assert.match(p.field('collectionError').textContent,/同一次请求/);
  await p.click('collectionStart');const requests=posts(p,'/api/collection/enqueue');assert.equal(requests.length,2);
  assert.equal(requests[0].body.request_id,requests[1].body.request_id);assert.deepEqual(requests[0].body.ids,requests[1].body.ids);
 });
 await test('targets retry is explicit and never comes from only one jobs page',async()=>{
  let failed=false;const p=await page({intercept:request=>{if(request.url.startsWith('/api/board?')&&!failed){failed=true;return reply(503,{error:'范围暂不可读'});}}});
  assert.match(p.field('collectionError').textContent,/范围暂不可读/);await p.click('collectionStart');assert.equal(p.writes.length,0);
  assert.match(p.field('collectionScope').textContent,/全部授权账号 2/);await p.click('collectionStart');assert.equal(posts(p,'/api/collection/enqueue').length,1);
  await p.poll();assert.equal(p.requests.filter(x=>x.url.startsWith('/api/board?')).length,2,'poll must never reread the board');
  for(const ids of [[],Array.from({length:2001},(_,i)=>'scoped-'+i)]){
   const limited=await page({ids});await limited.click('collectionStart');assert.equal(limited.writes.length,0);assert.equal(limited.field('collectionError').hidden,false);
  }
 });
 await test('pagination filtering account links and zero/failed quantities remain truthful',async()=>{
  const jobs=[{account_id:'scoped-east',name:'<nickname>',douyin_id:'123',office_name:'东办',state:'采集成功',count:0,current:true,job_state:'done'},
   {account_id:'scoped-west',state:'采集失败',count:1961,error:'采集失败，旧数量保留',current:true,job_state:'done'}];
  const p=await page({jobs,total:101,collection:{done:0,failed:1}});
  assert.match(p.field('collectionRows').innerHTML,/<td>0<\/td>/);assert.match(p.field('collectionRows').innerHTML,/<td>1961<\/td>/);
  assert.match(p.field('collectionRows').innerHTML,/&lt;nickname&gt;/);assert.match(p.field('collectionRows').innerHTML,/account_id=scoped-east/);
  assert.equal(String(p.field('collectionDone').textContent),'0');assert.equal(String(p.field('collectionFailed').textContent),'1');
  await p.click('collectionNext');assert.ok(p.requests.some(x=>x.url.includes('jobs?page=2')));
  p.field('collectionState').value='采集失败';p.field('collectionState').onchange();await settle();
  const last=p.requests.filter(x=>x.url.startsWith('/api/collection/jobs?')).at(-1);assert.equal(new URLSearchParams(last.url.split('?')[1]).get('page'),'1');
  assert.equal(new URLSearchParams(last.url.split('?')[1]).get('state'),'采集失败');
  await p.retry('scoped-west');assert.deepEqual(posts(p,'/api/collection/enqueue')[0].body.ids,['scoped-west']);
 });
 await test('help controls are explicit and do not run from page initialization',async()=>{
  const p=await page({collection:{waiting:2}});assert.equal(p.writes.length,0);
  await p.click('collectionOpenBrowser');assert.deepEqual(posts(p,'/api/collection/settings').map(x=>x.body),[{open_browser:true}]);
  p.field('collectionControls').onclick({target:{closest:()=>p.controls[1]}});assert.equal(p.field('collectionCancelDialog').open,true);
  p.field('collectionCancelDialog').returnValue='cancel';p.field('collectionCancelDialog').onclose();await settle();
  assert.equal(posts(p,'/api/collection/settings').at(-1).body.cancel_waiting,true);
 });
 console.log(JSON.stringify({passed:true,cases:cases.length,kind:'synthetic-collection-primary-start-pause-scope-and-truthful-counts',scenarios:cases}));
})().catch(error=>{console.error(error);process.exitCode=1;});
