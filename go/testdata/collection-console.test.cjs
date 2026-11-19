// Exercise the real collection page against an in-memory, authenticated API fixture.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../internal/webassets/static/collection-console.js'),'utf8');
const settle=async()=>{for(let i=0;i<8;i++)await new Promise(setImmediate);};
async function page({online=true,verifying=false,paused=false,control=true,enabled=true}={}){
 const fields=new Map(),writes=[];
 const field=id=>{if(!fields.has(id))fields.set(id,{id,hidden:false,disabled:false,textContent:'',value:'',dataset:{},querySelectorAll:()=>[]});return fields.get(id);};
 const controls=['pause','resume','cancel_waiting'].map(command=>({...field(command),dataset:{collectionCommand:command}}));
 field('collectionControls').querySelectorAll=()=>controls;
 let collection={enabled,worker_online:online,browser_control:online,worker_status:verifying?'等待手动登录或验证':'待命',paused,waiting:0};
 const context={window:{},document:{getElementById:field,visibilityState:'visible'},URLSearchParams,setInterval:()=>1,clearInterval(){},location:{replace(){throw Error('unexpected login redirect');}},
  fetch:async(path,options={})=>{
   if(options.method==='POST'){
    assert.equal(path,'/api/collection/settings');assert.equal(options.headers['X-CSRF-Token'],'synthetic-csrf');
    writes.push(JSON.parse(options.body));collection={...collection,paused:false,worker_status:'待命'};
    return {ok:true,status:200,json:async()=>({ok:true})};
   }
   const result=path==='/api/auth/me'?{user:{permissions:['collection_manage'],csrf:'synthetic-csrf'}}:
    path==='/api/collection/status'?{can_manage:true,can_control:control,collection}:
    path.startsWith('/api/collection/jobs?')?{jobs:[],total:0,page:1,page_size:50}:null;
   assert.ok(result,'unexpected request');return {ok:true,status:200,json:async()=>result};
  }};
 vm.createContext(context);vm.runInContext(source,context);await settle();
 return {field,controls,writes,click:async command=>{
  const button=controls.find(b=>b.dataset.collectionCommand===command);
  field('collectionControls').onclick({target:{closest:()=>button}});await settle();
 }};
}
(async()=>{
 const waiting=await page({verifying:true});
 assert.equal(waiting.writes.length,0);assert.equal(waiting.field('collectionControls').open,true);
 const resume=waiting.controls.find(b=>b.dataset.collectionCommand==='resume');
 assert.equal(resume.disabled,false);assert.equal(resume.textContent,'验证完成，继续');
 await waiting.click('resume');assert.deepEqual(waiting.writes,[{resume:true}]);
 const idle=await page();assert.equal(idle.controls.find(b=>b.dataset.collectionCommand==='resume').disabled,true);
 assert.equal(idle.field('collectionOpenBrowser').hidden,false);assert.equal(idle.field('collectionOpenBrowser').disabled,false);
 idle.field('collectionOpenBrowser').onclick();await settle();assert.deepEqual(idle.writes,[{open_browser:true}]);
 const offline=await page({online:false,paused:true});assert.equal(offline.field('collectionOpenBrowser').disabled,true);
 offline.field('collectionOpenBrowser').onclick();await offline.click('resume');assert.equal(offline.writes.length,0);
 const restricted=await page({control:false});assert.equal(restricted.field('collectionOpenBrowser').hidden,true);
 restricted.field('collectionOpenBrowser').onclick();await settle();assert.equal(restricted.writes.length,0);
 const disabled=await page({enabled:false,online:false,control:false});assert.match(disabled.field('collectionNotice').textContent,/暂未启用/);
 assert.equal(disabled.writes.length,0);
 console.log(JSON.stringify({passed:true,cases:6,kind:'synthetic-collection-window-and-verification-controls'}));
})().catch(error=>{console.error(error);process.exitCode=1;});
