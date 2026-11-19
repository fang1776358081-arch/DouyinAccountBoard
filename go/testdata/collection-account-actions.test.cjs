// Actual account action/status code with a synthetic DOM, clock and API only.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const staticRoot=path.join(__dirname,'../internal/webassets/static');
const read=name=>fs.readFileSync(path.join(staticRoot,name),'utf8');
const app=read('app.js');
const actions=app.slice(app.indexOf('function renderBoardCollectionInfo()'),app.indexOf('{const boardUpdates=document.createElement'));
const apiSource=app.slice(app.indexOf('async function sendApi('),app.indexOf('// Preserve an uncertain write'));
const clone=value=>JSON.parse(JSON.stringify(value));
const reply=(body,status=200)=>({ok:status>=200&&status<300,status,json:async()=>clone(body)});
const defer=()=>{let resolve;const promise=new Promise(done=>{resolve=done;});return{promise,resolve};};
const flush=async()=>{for(let i=0;i<8;i++)await new Promise(setImmediate);};
function fixture({control=true,permission=true}={}){
 const fields=new Map(),events=new Map(),intervals=[],timers=[],calls=[];let now=10000;
 const attach=node=>{if(node.id)fields.set(node.id,node);for(const child of node.children||[])attach(child);};
 function node(id=''){
  return{id,children:[],hidden:false,disabled:false,textContent:'',title:'',dataset:{},parentElement:{},
   setAttribute(key,value){this[key]=value;},append(...children){this.children.push(...children);children.forEach(attach);},
   insertAdjacentElement(where,child){attach(child);},closest(){return{hidden:false};},querySelectorAll(){return[];}};
 }
 const field=id=>{if(!fields.has(id))fields.set(id,node(id));return fields.get(id);};
 const lookup=id=>fields.get(id)||(/^(accountCollection|boardCollection)/.test(id)?null:field(id));
 const document={visibilityState:'visible',activeElement:null,getElementById:lookup,createElement:()=>node(),
  querySelectorAll:selector=>selector==='[data-collect]'?[field('syntheticDetailCollect')]:[],querySelector:()=>null,
  addEventListener(type,handler){if(!events.has(type))events.set(type,[]);events.get(type).push(handler);}};
 const board={user:{id:'synthetic-owner',role:'owner',name:'合成用户',permissions:permission?['collection_manage']:[],csrf:'synthetic-csrf'},
  accounts:[{id:'synthetic-account',name:'真实资料占位不写入',note:'synthetic-note'}],cursor:99,record_versions:{'account:synthetic-account':'held-version'},
  collection:{enabled:true,worker_online:true,worker_status:'等待手动登录或验证',paused:true,waiting:0,failed:0}};
 const server={can_manage:permission,can_control:control,collection:clone(board.collection)};
 const context={document,URLSearchParams,AbortSignal,AbortController,console,Map,Set,
  Date:class extends Date{static now(){return now;}},
  setInterval(fn,ms){intervals.push({fn,ms});return intervals.length;},clearInterval(){},
  setTimeout(fn,ms){timers.push({fn,at:now+ms});return timers.length;},clearTimeout(){},
  addEventListener(type,handler){document.addEventListener(type,handler);},
  fetch:async(url,options={})=>{calls.push({url,options});if(context.respond)return context.respond(url,options);
   if(url==='/api/collection/status')return reply(server);
   assert.equal(url,'/api/collection/settings');assert.equal(options.method,'POST');
   assert.equal(options.headers['X-CSRF-Token'],'synthetic-csrf');assert.deepEqual(JSON.parse(options.body),{resume:true});
   server.collection.paused=false;return reply({ok:true});},
  clearBoardCache(){},location:{replace(){}},applyVersionDelta(){throw Error('collection must not replace account versions');},
  toast(){},newRequestId:()=> 'synthetic-request',filtered:()=>board.accounts};
 context.window=context;context.$=lookup;context.initial=board;context.draft={value:'held draft'};
 context.AccountInline={hasDraft:()=>context.hasDraft===true};
 vm.createContext(context);vm.runInContext('let data=initial,page="accounts",boardWrites=0,boardMutationEpoch=0;const selected=new Set(Array.from({length:173},(_,i)=>"synthetic-"+i));',context);
 vm.runInContext(read('auth-ui.js'),context);vm.runInContext(apiSource,context);vm.runInContext(actions,context);
 vm.runInContext(read('board-updates.js'),context);
 const run=source=>vm.runInContext(source,context);
 run('applyCollectionPermissions()');
 return{context,server,calls,field,run,document,intervals,timers,
  data:()=>clone(run('data')),snapshot:()=>clone(run('({accounts:data.accounts,cursor:data.cursor,versions:data.record_versions,selected:[...selected]})')),
  async read(options){await context.refreshBoardCollection(options);await flush();},
  async resume(){await context.resumeBoardCollection();await flush();},
  async tick(){for(const {fn} of intervals)fn();await flush();},
  async advance(ms){now+=ms;for(const timer of timers.splice(0).filter(timer=>timer.at<=now))timer.fn();await flush();}};
}
let checks=0;
async function test(name,fn){await fn();checks++;console.log('ok '+name);}
(async()=>{
 await test('paused reason is visible and authorized resume waits for worker without enqueueing 173 accounts',async()=>{
  const p=fixture();await p.read();
  assert.equal(p.field('collectSelected').disabled,true);assert.match(p.field('accountCollectionReason').textContent,/队列已暂停/);
  assert.equal(p.field('accountCollectionResume').hidden,false);assert.equal(p.field('accountCollectionQueue').href,'/collection.html');
  const before=p.snapshot();p.context.hasDraft=true;await p.resume();
  assert.equal(p.calls.filter(call=>call.options.method==='POST').length,1);
  assert.equal(p.field('collectSelected').disabled,true);assert.match(p.field('accountCollectionReason').textContent,/等待采集器/);
  p.server.collection.worker_status='待命';await p.advance(3100);
  assert.equal(p.field('collectSelected').disabled,false);assert.deepEqual(p.snapshot(),before);assert.equal(p.context.draft.value,'held draft');
  assert.equal(p.calls.some(call=>call.url==='/api/collection/enqueue'),false);
 });
 await test('can_control false and missing permission never send resume or infer access from owner role',async()=>{
  const p=fixture({control:false});await p.read();assert.equal(p.field('accountCollectionResume').hidden,true);await p.resume();
  assert.equal(p.calls.filter(call=>call.options.method==='POST').length,0);
  const q=fixture({permission:false});await q.read();await q.resume();assert.equal(q.calls.length,0);assert.equal(q.field('collectSelected').disabled,true);
 });
 await test('unknown control access is read before a write and resume clicks share one pending operation',async()=>{
  const p=fixture(),pending=defer();p.context.respond=(url,options)=>options.method==='POST'?pending.promise:Promise.resolve(reply(p.server));
  const first=p.context.resumeBoardCollection();await flush();const second=p.context.resumeBoardCollection();await flush();
  assert.equal(p.calls[0].url,'/api/collection/status');assert.equal(p.calls.filter(call=>call.options.method==='POST').length,1);
  pending.resolve(reply({ok:true}));await Promise.all([first,second]);
 });
 await test('eight-second status reads remain single flight during drafts and hidden pages perform no reads',async()=>{
  const p=fixture(),pending=defer(),before=p.snapshot();p.context.hasDraft=true;
  assert.deepEqual(p.intervals.map(item=>item.ms),[8000]);p.context.respond=()=>pending.promise;
  const first=p.context.refreshBoardCollection(),second=p.context.refreshBoardCollection();await p.tick();assert.equal(p.calls.length,1);
  pending.resolve(reply(p.server));await Promise.all([first,second]);p.context.respond=null;
  p.document.visibilityState='hidden';await p.tick();assert.equal(p.calls.length,1);
  p.document.visibilityState='visible';await p.tick();assert.equal(p.calls.length,2);assert.deepEqual(p.snapshot(),before);
 });
 await test('resume waits for old status JSON before POST so an older paused response cannot overwrite new state',async()=>{
  const p=fixture(),old=defer();let reads=0;
  p.context.respond=(url,options)=>{
   if(options.method==='POST'){p.server.collection.paused=false;return Promise.resolve(reply({ok:true}));}
   return Promise.resolve(++reads===1?{ok:true,status:200,json:()=>old.promise}:reply(p.server));};
  const reading=p.context.refreshBoardCollection();await flush();const resume=p.context.resumeBoardCollection();await flush();
  assert.equal(p.calls.length,1);old.resolve(clone(p.server));await Promise.all([reading,resume]);
  assert.deepEqual(p.calls.map(call=>call.options.method||'GET'),['GET','POST','GET']);assert.equal(p.data().collection.paused,false);
 });
 await test('failed reads retain known counts and show retry; retry recovers without rendering account data',async()=>{
  const p=fixture(),before=p.data().collection;p.context.respond=()=>Promise.reject(Error('synthetic network failure'));
  await p.read();assert.deepEqual(p.data().collection,before);assert.match(p.field('accountCollectionReason').textContent,/synthetic network failure/);
  p.context.respond=null;p.server.collection.paused=false;p.server.collection.worker_status='待命';
  p.field('accountCollectionRefresh').onclick();await flush();assert.equal(p.field('collectSelected').disabled,false);
 });
 await test('401 and 403 clear control access without zeroing records and cannot send a control write',async()=>{
  for(const status of [401,403]){const p=fixture(),before=p.data().collection;p.context.respond=()=>Promise.resolve(reply({},status));
   await p.read();await p.resume();assert.equal(p.field('collectSelected').disabled,true);assert.equal(p.field('accountCollectionResume').hidden,true);
   assert.equal(p.calls.some(call=>call.options.method==='POST'),false);assert.deepEqual(p.data().collection,before);
   p.context.respond=null;await p.read({retry:true});assert.equal(p.data().collection_access.can_control,true);}
 });
 await test('failed resume is not optimistically enabled and a later explicit retry can succeed',async()=>{
  const p=fixture();await p.read();p.context.respond=(url,options)=>Promise.resolve(options.method==='POST'?reply({error:'synthetic control failure'},503):reply(p.server));
  await p.resume();assert.equal(p.data().collection.paused,true);assert.equal(p.field('collectSelected').disabled,true);
  assert.match(p.field('accountCollectionReason').textContent,/synthetic control failure/);p.context.respond=null;await p.resume();
  p.server.collection.worker_status='待命';await p.advance(3100);assert.equal(p.field('collectSelected').disabled,false);
 });
 await test('a fresh pause or verification status ends the bounded wait and allows another explicit resume without changing drafts or selection',async()=>{
  for(const state of [{paused:true,worker_status:'待命'},{paused:true,worker_status:'等待手动登录或验证'},{paused:false,worker_status:'等待手动登录或验证'},{paused:false,worker_online:false,worker_status:'本机采集器离线'}]){
   const p=fixture();await p.read();const before=p.snapshot();p.context.hasDraft=true;await p.resume();
   Object.assign(p.server.collection,state);await p.advance(3100);
   assert.equal(p.run('collectionAwaitingWorker'),false);assert.equal(p.field('collectSelected').disabled,true);
   if(state.worker_online===false){
    assert.equal(p.field('accountCollectionResume').disabled,true);assert.match(p.field('accountCollectionReason').textContent,/离线|未在线/);
    p.server.collection.worker_online=true;p.server.collection.paused=true;await p.read();
   }
   assert.equal(p.field('accountCollectionResume').hidden,false);assert.equal(p.field('accountCollectionResume').disabled,false);
   await p.resume();assert.equal(p.calls.filter(call=>call.options.method==='POST').length,2);
   assert.equal(p.field('collectSelected').disabled,true);await p.advance(3100);
   if(state.worker_status.startsWith('等待'))assert.equal(p.field('collectSelected').disabled,true);
   p.server.collection.paused=false;p.server.collection.worker_status='待命';await p.read();
   assert.equal(p.field('collectSelected').disabled,false);assert.deepEqual(p.snapshot(),before);assert.equal(p.context.draft.value,'held draft');
   assert.equal(p.calls.some(call=>call.url==='/api/collection/enqueue'),false);
  }
 });
 await test('pure display checkbox is safe while true inline edits and real input still block board updates',async()=>{
  const p=fixture();p.document.activeElement={tagName:'INPUT',type:'checkbox',dataset:{sheetColumn:'name'},matches:()=>false};
  assert.equal(p.run('boardSyncBlocked()'),false);p.context.hasDraft=true;assert.equal(p.run('boardSyncBlocked()'),true);
  p.context.hasDraft=false;p.document.activeElement={tagName:'INPUT',dataset:{},matches:()=>false};assert.equal(p.run('boardSyncBlocked()'),true);
 });
 console.log(JSON.stringify({passed:true,checks,kind:'synthetic-account-collection-actions-no-business-writes'}));
})().catch(error=>{console.error(error);process.exitCode=1;});
