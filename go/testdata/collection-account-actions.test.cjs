// Actual start/pause scripts; synthetic DOM, clock and API only.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.join(__dirname,'../internal/webassets/static'),read=name=>fs.readFileSync(path.join(root,name),'utf8');
const app=read('app.js'),actions=app.slice(app.indexOf('function collectionScopeSnapshot('),app.indexOf('{const boardUpdates=document.createElement'));
const apiSource=app.slice(app.indexOf('async function sendApi('),app.indexOf('// Preserve an uncertain write'));
const clone=value=>JSON.parse(JSON.stringify(value)),reply=(body,status=200)=>({ok:status>=200&&status<300,status,json:async()=>clone(body)});
const deferred=()=>{let resolve;const promise=new Promise(done=>{resolve=done;});return{promise,resolve};};
const flush=async()=>{for(let i=0;i<8;i++)await new Promise(setImmediate);};
function fixture({control=true,permission=true,paused=true,waiting=0,worker='待命',count=173,selected=true,page='accounts'}={}){
 const fields=new Map(),events=new Map(),intervals=[],timers=new Map(),calls=[],receipts=new Map();let now=10000,timerId=0,requestId=0;
 const field=id=>{if(!fields.has(id))fields.set(id,{id,hidden:false,disabled:false,textContent:'',innerHTML:'',title:'',dataset:{},closest:()=>({hidden:false})});return fields.get(id);};
 const document={visibilityState:'visible',activeElement:null,getElementById:field,querySelectorAll:selector=>selector==='[data-collect]'?[field('syntheticSingle')]:[],querySelector:()=>null,
  addEventListener(type,fn){if(!events.has(type))events.set(type,[]);events.get(type).push(fn);}};
 const board={user:{id:'synthetic-owner',role:'owner',name:'合成用户',permissions:permission?['collection_manage']:[],csrf:'synthetic-csrf'},
  accounts:Array.from({length:count},(_,i)=>({id:'synthetic-'+i,name:'合成账号'+i,note:'held',computer_id:i===0?'unassigned':'pc-0'})),
  cursor:99,record_versions:{'account:synthetic-0':'held-version'},collection:{enabled:true,worker_online:true,worker_status:worker,paused,waiting,done:8,failed:2}};
 const server={can_manage:permission,can_control:control,collection:clone(board.collection)};
 const context={document,URLSearchParams,AbortSignal,AbortController,console,Map,Set,Date:class extends Date{static now(){return now;}},
  setInterval(fn,ms){intervals.push({fn,ms});return intervals.length;},clearInterval(){},
  setTimeout(fn,ms){timers.set(++timerId,{fn,at:now+ms});return timerId;},clearTimeout:id=>timers.delete(id),
  addEventListener(type,fn){document.addEventListener(type,fn);},
  fetch:async(url,options={})=>{
   calls.push({url,options});if(context.respond)return context.respond(url,options);
   if(url==='/api/collection/status')return reply(server);
   if(url.startsWith('/api/idempotency/result?'))return reply({},404);
   assert.equal(options.method,'POST');assert.equal(options.headers['X-CSRF-Token'],'synthetic-csrf');const body=JSON.parse(options.body);
   if(url==='/api/collection/settings'){assert.ok(body.resume===true||body.pause===true);assert.equal(Object.keys(body).length,1);server.collection.paused=!!body.pause;return reply({ok:true});}
   assert.equal(url,'/api/collection/enqueue');assert.ok(body.request_id);assert.ok(body.ids.length);
   if(receipts.has(body.request_id)){assert.deepEqual(body,receipts.get(body.request_id).body);return reply(receipts.get(body.request_id).result);}
   const result=context.enqueueResult||{queued:body.ids.length,missing:0,invalid:0,duplicate:0};receipts.set(body.request_id,{body,result});server.collection.waiting+=result.queued;return reply(result);
  },clearBoardCache(){},location:{replace(){}},applyVersionDelta(){throw Error('must not replace draft/account versions');},
  render(){throw Error('must not redraw account records');},toast(){},newRequestId:()=> 'synthetic-request-'+(++requestId),
  filtered:()=>board.accounts.filter(row=>!context.filterIds||context.filterIds.includes(row.id))};
 context.window=context;context.$=field;context.initial=board;context.draft={value:'held draft'};context.AccountInline={hasDraft:()=>context.hasDraft===true};
 vm.createContext(context);vm.runInContext('let data=initial,page='+JSON.stringify(page)+',boardWrites=0,boardMutationEpoch=0;const selected=new Set('+(selected?'initial.accounts.map(row=>row.id)':'[]')+');',context);
 vm.runInContext(read('auth-ui.js'),context);vm.runInContext(apiSource,context);vm.runInContext(actions,context);vm.runInContext(read('board-updates.js'),context);
 const run=code=>vm.runInContext(code,context);run('applyCollectionPermissions()');
 return{context,server,calls,field,fields,run,document,intervals,data:()=>clone(run('data')),snapshot:()=>clone(run('({accounts:data.accounts,cursor:data.cursor,versions:data.record_versions,selected:[...selected]})')),
  async read(options){await context.refreshBoardCollection(options);await flush();},start(ids,trigger){return context.startBoardCollection(ids,trigger);},pause(){return context.pauseBoardCollection();},
  async tick(){for(const {fn}of intervals)fn();await flush();},
  async advance(ms){now+=ms;for(const [id,timer]of [...timers])if(timer.at<=now){timers.delete(id);timer.fn();await flush();}}};
}
const posts=p=>p.calls.filter(call=>call.options.method==='POST').map(call=>({url:call.url,body:JSON.parse(call.options.body)}));
let checks=0;async function test(name,fn){await fn();checks++;console.log('ok '+name);}
(async()=>{
 await test('one start resumes, confirms worker then enqueues frozen 173 once; pause preserves pending jobs',async()=>{
  const p=fixture();await p.read();const before=p.snapshot();p.context.hasDraft=true;const job=p.start();await flush();
  assert.deepEqual(posts(p),[{url:'/api/collection/settings',body:{resume:true}}]);assert.equal(p.field('collectionStart').disabled,true);
  await p.advance(3100);await job;await flush();const enqueued=posts(p).filter(row=>row.url.endsWith('/enqueue'));
  assert.equal(enqueued.length,1);assert.equal(enqueued[0].body.ids.length,173);assert.equal(p.field('collectionStart').disabled,true);await p.pause();await flush();
  assert.deepEqual(posts(p).at(-1),{url:'/api/collection/settings',body:{pause:true}});assert.equal(p.server.collection.waiting,173);
  assert.match(p.field('collectionMessage').textContent,/当前.*账号完成后停止/);assert.match(p.field('collectionMessage').textContent,/不取消/);
  assert.deepEqual(p.snapshot(),before);assert.equal(p.context.draft.value,'held draft');
 });
 await test('quick double start shares one operation, one resume and one idempotent enqueue',async()=>{
  const p=fixture();const one=p.start(),two=p.start();await flush();assert.equal(one,two);await p.advance(3100);await Promise.all([one,two]);
  assert.equal(posts(p).filter(row=>row.body.resume).length,1);assert.equal(posts(p).filter(row=>row.url.endsWith('/enqueue')).length,1);
 });
 await test('existing paused queue resumes only; an active queue never appends changed selection or a single account',async()=>{
  const p=fixture({waiting:7});const job=p.start(['synthetic-0']);await flush();assert.match(p.field('collectionScope').textContent,/7.*未完成/);
  await p.advance(3100);await job;assert.equal(posts(p).some(row=>row.url.endsWith('/enqueue')),false);assert.match(p.field('collectionMessage').textContent,/本次选择未新增/);
  await p.start(['synthetic-1']);assert.equal(posts(p).length,1);assert.equal(p.field('collectionStart').disabled,true);
 });
 await test('selection and filter changes while waiting cannot expand the original click scope',async()=>{
  const p=fixture();p.context.filterIds=['synthetic-0','synthetic-1'];const job=p.start();await flush();p.context.filterIds=['synthetic-2'];p.run('selected.clear();selected.add("synthetic-2")');
  await p.advance(3100);await job;assert.deepEqual(posts(p).find(row=>row.url.endsWith('/enqueue')).body.ids,['synthetic-0','synthetic-1']);
  const q=fixture({paused:false,selected:false,page:'board',count:3});await q.start();assert.deepEqual(posts(q).find(row=>row.url.endsWith('/enqueue')).body.ids,['synthetic-1','synthetic-2']);
  const r=fixture({paused:false});await r.start(['outside-scope']);assert.equal(posts(r).length,0);
  const s=fixture({paused:false,count:3});s.context.filterIds=['synthetic-0'];await s.start(['synthetic-1']);
  assert.deepEqual(posts(s).find(row=>row.url.endsWith('/enqueue')).body.ids,['synthetic-1']);
 });
 await test('no global control permission cannot resume or pause but can enqueue an already-ready authorized scope',async()=>{
  const p=fixture({control:false});await p.start();await p.pause();assert.equal(posts(p).length,0);assert.match(p.field('collectionMessage').textContent,/管理员/);
  p.server.collection.paused=false;await p.start(['synthetic-1']);assert.equal(posts(p).length,1);assert.match(posts(p)[0].url,/enqueue$/);
  const q=fixture({permission:false});await q.start();await q.pause();assert.equal(q.calls.length,0);
 });
 await test('verification wait is bounded, never enqueues after timeout and permits a later explicit start',async()=>{
  const p=fixture({worker:'等待手动登录或验证'});p.server.collection.pause_reason='抖音搜索需要安全验证，请在采集电脑手动完成';const job=p.start();await flush();await p.advance(3100);await p.advance(3100);await job;
  assert.equal(posts(p).filter(row=>row.url.endsWith('/enqueue')).length,0);assert.equal(p.field('collectionStart').disabled,false);assert.match(p.field('collectionMessage').textContent,/安全验证/);
  p.server.collection.worker_status='待命';await p.start();assert.equal(posts(p).filter(row=>row.url.endsWith('/enqueue')).length,1);
 });
 await test('generic startup waiting asks only for start; only explicit server challenge asks for verification',async()=>{
  const p=fixture({worker:'等待手动登录或验证'});await p.read();
  assert.equal(p.field('collectionMessage').hidden,true);assert.equal(p.field('collectionMessage').textContent,'');assert.doesNotMatch(p.field('collectionMessage').textContent,/完成.*验证/);
  assert.doesNotMatch(p.field('collectionStart').title,/登录或验证/);
  p.server.collection.pause_reason='抖音登录已失效，请在采集电脑重新登录';await p.read();
  assert.match(p.field('collectionMessage').textContent,/登录已失效/);assert.match(p.field('collectionMessage').textContent,/打开验证窗口/);
  p.server.collection.pause_reason='已手动暂停，当前采集完成后停止';await p.read();assert.equal(p.field('collectionMessage').hidden,true);
 });
 await test('partial results stay explicit; three counters identify authorized latest results rather than batch progress',async()=>{
  const p=fixture({paused:false,count:4});p.context.enqueueResult={queued:1,missing:1,invalid:1,duplicate:1};await p.start();
  const text=p.field('collectionMessage').textContent;assert.match(text,/已提交 1/);assert.match(text,/1 个缺少/);assert.match(text,/1 个主页/);assert.match(text,/1 个已在队列/);
  const counts=p.field('collectionCounts').innerHTML;assert.match(counts,/已采集/);assert.match(counts,/待处理/);assert.equal((counts.match(/class="collection-metric"/g)||[]).length,3);assert.doesNotMatch(counts,/<small/);
  assert.match(p.field('collectionInfo').textContent,/授权范围最近结果/);assert.match(p.field('collectionInfo').textContent,/包含采集中/);
  assert.equal(p.field('collectSelected').hidden,true);assert.equal(p.field('collectVisible').hidden,true);assert.equal(p.fields.has('accountCollectionActions'),false);
 });
 await test('eight-second metadata reads are single-flight; hidden pages do not read and drafts stay unchanged',async()=>{
  const p=fixture(),pending=deferred(),before=p.snapshot();p.context.hasDraft=true;p.context.respond=()=>pending.promise;
  const one=p.context.refreshBoardCollection(),two=p.context.refreshBoardCollection();await p.tick();assert.equal(p.calls.length,1);assert.deepEqual(p.intervals.map(item=>item.ms),[8000]);
  pending.resolve(reply(p.server));await Promise.all([one,two]);p.context.respond=null;p.document.visibilityState='hidden';await p.tick();assert.equal(p.calls.length,1);
  p.document.visibilityState='visible';await p.tick();assert.equal(p.calls.length,2);assert.deepEqual(p.snapshot(),before);
 });
 await test('an older status JSON completes before the resume write and cannot overwrite its later state',async()=>{
  const p=fixture(),old=deferred();let reads=0;p.context.respond=(url,options)=>{if(options.method==='POST'){p.server.collection.paused=false;return Promise.resolve(reply({ok:true,queued:173}));}
   return Promise.resolve(++reads===1?{ok:true,status:200,json:()=>old.promise}:reply(p.server));};
  const reading=p.context.refreshBoardCollection();await flush();const start=p.start();await flush();assert.equal(posts(p).length,0);
  old.resolve(clone(p.server));await reading;await flush();assert.equal(posts(p)[0].body.resume,true);await p.advance(3100);await start;
  assert.equal(p.data().collection.paused,false);assert.equal(posts(p).filter(row=>row.url.endsWith('/enqueue')).length,1);
 });
 await test('failed reads and control writes never optimistically enqueue; start retries without another button',async()=>{
  const p=fixture({paused:false}),before=p.data().collection;p.context.respond=()=>Promise.reject(Error('synthetic network failure'));await p.start();
  assert.deepEqual(p.data().collection,before);assert.equal(posts(p).length,0);assert.match(p.field('collectionMessage').textContent,/synthetic network failure/);
  p.context.respond=null;await p.start();assert.equal(posts(p).filter(row=>row.url.endsWith('/enqueue')).length,1);
  const q=fixture();q.context.respond=(url,options)=>Promise.resolve(options.method==='POST'?reply({error:'synthetic control failure'},503):reply(q.server));await q.start();
  assert.equal(q.data().collection.paused,true);assert.equal(posts(q).some(row=>row.url.endsWith('/enqueue')),false);assert.match(q.field('collectionMessage').textContent,/synthetic control failure/);assert.equal(q.field('collectionMessage').dataset.tone,'error');
 });
 await test('401 and 403 revoke controls without replacing records or sending resume, enqueue or pause writes',async()=>{
  for(const status of [401,403]){const p=fixture(),before=p.data().collection;p.context.respond=()=>Promise.resolve(reply({},status));await p.start();await p.pause();
   assert.equal(posts(p).length,0);assert.equal(p.field('collectionStart').disabled,true);assert.deepEqual(p.data().collection,before);}
 });
 await test('an unconfirmed enqueue keeps its request ID for an explicit retry and never schedules another batch',async()=>{
  const p=fixture({paused:false,count:2});p.context.respond=(url,options)=>Promise.resolve(url==='/api/collection/status'?reply(p.server):url.startsWith('/api/idempotency/')?reply({},404):Promise.reject(Error('synthetic write connection loss')));
  await p.start();const first=posts(p).find(row=>row.url.endsWith('/enqueue'));assert.ok(first);assert.match(p.field('collectionMessage').textContent,/结果待确认/);
  await p.advance(10000);await p.tick();assert.equal(posts(p).filter(row=>row.url.endsWith('/enqueue')).length,1);
  p.context.respond=null;await p.start();const second=posts(p).filter(row=>row.url.endsWith('/enqueue'))[1];assert.equal(second.body.request_id,first.body.request_id);assert.deepEqual(second.body.ids,first.body.ids);
 });
 await test('display checkbox stays safe while true inline drafts and real input block board synchronization',async()=>{
  const p=fixture();p.document.activeElement={tagName:'INPUT',type:'checkbox',dataset:{sheetColumn:'name'},matches:()=>false};assert.equal(p.run('boardSyncBlocked()'),false);
  p.context.hasDraft=true;assert.equal(p.run('boardSyncBlocked()'),true);p.context.hasDraft=false;p.document.activeElement={tagName:'INPUT',dataset:{},matches:()=>false};assert.equal(p.run('boardSyncBlocked()'),true);
 });
 console.log(JSON.stringify({passed:true,checks,kind:'synthetic-simple-start-pause-no-real-collection'}));
})().catch(error=>{console.error(error);process.exitCode=1;});
