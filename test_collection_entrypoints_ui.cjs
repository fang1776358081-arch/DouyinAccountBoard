// Actual presentation/permission code; synthetic DOM/fetch only, no collector.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const {harness,fixture}=require('./test_performance_ui.cjs');
const read=name=>fs.readFileSync(__dirname+'/static/'+name,'utf8');
let checks=0;
async function main(){
 for(const [enabled,permissions]of [[false,['collection_manage','publish']],[true,['publish']],[true,['collection_manage','publish']]]){
  const fields=new Map(),one={disabled:false,title:''},field=id=>{if(!fields.has(id))fields.set(id,{hidden:false,disabled:false,textContent:''});return fields.get(id);};
  const context={data:{user:{id:'synthetic-user',name:'合成用户',role:'employee',permissions},collection:{enabled}},$:field,document:{addEventListener(){},querySelectorAll:selector=>selector==='[data-collect]'?[one]:[]}};
  vm.createContext(context);vm.runInContext(read('auth-ui.js'),context);context.applyPermissions();
  const blocked=!enabled||!permissions.includes('collection_manage');
  for(const id of ['collectVisible','collectSelected']){assert.equal(field(id).disabled,blocked);assert.equal(field(id).hidden,false);assert.match(field(id).title,enabled&&!blocked?/^$/:enabled?/权限/:/暂未启用/);}
  assert.equal(one.disabled,blocked);checks++;
 }
 const p=harness(),board=fixture(2),summary={innerHTML:''};board.collection.enabled=false;p.element('accountSupplement').querySelector=selector=>selector==='summary'?summary:null;p.run(read('account-workspace.js'));p.show(board);
 assert.equal(p.element('accountSupplement').hidden,false);assert.match(summary.innerHTML,/作品采集与注册进度/);assert.match(summary.innerHTML,/采集暂未启用/);assert.equal(p.calls.length,0);checks++;
 for(const [enabled,canManage]of [[false,true],[true,false],[true,true]]){
  const fields=new Map(),actions={hidden:true},field=id=>{if(!fields.has(id))fields.set(id,{disabled:false,hidden:false,textContent:'',closest:()=>actions});return fields.get(id);};
  let writes=0;const context={document:{getElementById:field,visibilityState:'visible'},setInterval(){},fetch:async()=>({ok:true,json:async()=>({can_manage:canManage,collection:{enabled,worker_online:false,worker_status:'合成状态',waiting:0,done:0,failed:0,paused:true}})}),send:async()=>{writes++;}};
  vm.createContext(context);vm.runInContext(read('collection-panel.js'),context);await context.refreshCollectionPanel();
  assert.equal(actions.hidden,false);for(const id of ['workCollectionPause','workCollectionResume'])assert.equal(field(id).disabled,!enabled||!canManage);assert.equal(writes,0);checks++;
 }
 console.log(JSON.stringify({passed:true,checks,kind:'collection-entry-policy-permission-synthetic-only'}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
