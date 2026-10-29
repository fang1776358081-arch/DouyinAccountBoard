// Runs real identity/workflow scripts with synthetic DOM and API stubs only.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const read=name=>fs.readFileSync(path.join(__dirname,'static',name),'utf8');
function fixture(scoped=true){
 const first='phone:synthetic-office-a:29',second='phone:synthetic-office-b:29';
 const units=[
  {id:'physical-a-old',device_id:first,original_device_id:first,generation:1,active:0,status:'返修待验',label:'缓存短名称29'},
  {id:'physical-a-new',device_id:first,original_device_id:first,generation:2,active:1,status:'可用闲置',label:'缓存短名称29'},
  {id:'physical-b-current',device_id:second,original_device_id:second,generation:1,active:1,status:'在用',label:'缓存短名称29'},
 ];
 const access={account_id:'a',last_unit_id:units[0].id,last_unit_label:'过期的登录短名称',target_unit_id:units[2].id,login_state:'登录受阻',recovery_state:'等待协助',case_id:'case-a',reason:'等待旧机验证'};
 const placement={card_id:1,unit_id:units[0].id,slot:'1',unit_label:'过期的卡位置短名称'};
 return {
  user:{id:1,name:'合成管理员',...(scoped?{scope_mode:'all'}:{}),permissions:['device_manage','device_create','card_manage']},
  offices:[{id:'office-a',name:'甲办公室'},{id:'office-b',name:'乙办公室'}],
  phone_devices:{devices:[
   {id:first,number:'29',name:'备用<img src=x onerror=alert(1)>',office_ids:['office-a']},
   {id:second,number:'29',name:'备用手机',office_ids:['office-b']},
  ]},
  accounts:[{id:'a',name:'合成账号',status:'正常',phone_device_id:first,device_access:access}],
  phone_cards:{cards:[{id:1,phone:'13800000000',location:'原文字29',placement}]},record_versions:{},
  device_workflow:{enabled:true,units,access:[access],placements:[placement],cases:[{id:'case-a',unit_id:units[0].id,unit_label:'过期的案例短名称',status:'处理中',reason:'合成故障',account_ids:['a'],created_at:10}],events:[
   {id:'event-move',at:20,unit_id:units[2].id,card_id:1,action:'移动手机卡',details:{before:{unit_id:units[0].id},after:{unit_id:units[2].id},device_id:second,original_device_id:first}},
  ]},
 };
}
function harness(board,withLabels=true){
 const elements=new Map(),listeners=new Map(),writes=[];
 const element=id=>{if(!elements.has(id))elements.set(id,{id,innerHTML:'',textContent:'',open:false,hidden:false,value:'',
  showModal(){this.open=true;},close(){this.open=false;},addEventListener(){},querySelector(){return null;},querySelectorAll(){return [];},insertAdjacentHTML(_position,html){this.innerHTML+=html;}});return elements.get(id);};
 class SyntheticFormData{constructor(form){this.entries=form.entries;}*[Symbol.iterator](){yield* this.entries;}get(name){return this.entries.find(([key])=>key===name)?.[1]??null;}getAll(name){return this.entries.filter(([key])=>key===name).map(([,value])=>value);}has(name){return this.entries.some(([key])=>key===name);}}
 const context={console,data:board,FormData:SyntheticFormData,document:{getElementById:element,addEventListener(type,handler){listeners.set(type,handler);}},api:async(url,payload)=>{assert.equal(url,'/api/device-workflow');writes.push(structuredClone(payload));return {ok:true};}};
 context.window=context;vm.createContext(context);if(withLabels)vm.runInContext(read('identity-labels.js'),context);vm.runInContext(read('device-workflow.js'),context);
 const click=(name,value)=>{const node={dataset:{[name]:value},hasAttribute(){return false;},closest(){return this;}};listeners.get('click')({target:node});};
 const submit=entries=>{const error={textContent:''},form={dataset:{workflowForm:'reassign_unit'},entries,querySelector(){return error;},closest(){return this;}};listeners.get('submit')({target:form,submitter:{disabled:false},preventDefault(){}});return error;};
 return {context,element,writes,click,submit,html:()=>element('deviceWorkflowDialog').innerHTML};
}
const text=html=>html.replace(/<[^>]*>/g,'');
const optionLabel=(html,id)=>{const found=[...html.matchAll(/<option value="([^"]*)"[^>]*>([\s\S]*?)<\/option>/g)].find(row=>row[1]===id);assert.ok(found,'missing exact unit option '+id);return text(found[2]);};
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 const board=fixture(),page=harness(board),flow=page.context.DeviceWorkflow;
 const old=board.device_workflow.units[0],current=board.device_workflow.units[1],other=board.device_workflow.units[2];
 assert.match(flow.positionText(board.phone_cards.cards[0]),/甲办公室/);assert.match(flow.positionText(board.phone_cards.cards[0]),/旧机/);assert.doesNotMatch(flow.positionText(board.phone_cards.cards[0]),/过期/);
 assert.match(flow.accessText(board.accounts[0]),/甲办公室/);assert.doesNotMatch(flow.accessText(board.accounts[0]),/过期/);
 flow.openAccount('a');let html=page.html();
 assert.match(optionLabel(html,current.id),/甲办公室/);assert.match(optionLabel(html,other.id),/乙办公室/);
 assert.match(optionLabel(html,current.id),/29/);assert.match(optionLabel(html,other.id),/29/);
 assert.doesNotMatch(html,/<img\b/);assert.match(html,/&lt;img src=x onerror=alert\(1\)&gt;/);
 assert.doesNotMatch(html,/过期的登录短名称|过期的案例短名称/);
 flow.openCard(1);html=page.html();assert.match(optionLabel(html,old.id),/甲办公室/);assert.match(optionLabel(html,other.id),/乙办公室/);assert.match(optionLabel(html,old.id),/旧机/);
 flow.openCase('case-a');assert.match(page.html(),/甲办公室/);assert.doesNotMatch(page.html(),/过期的案例短名称/);
 flow.openDevice(board.phone_devices.devices[0].id);html=page.html();assert.match(html,/<h2>甲办公室/);
 const timeline=text(html.slice(html.indexOf('<ol class="workflow-timeline">')));assert.match(timeline,/甲办公室/);assert.match(timeline,/乙办公室/);assert.doesNotMatch(timeline,/phone:synthetic/);assert.doesNotMatch(html,/（旧机） · 旧机/);
 page.click('workflowReassign',old.id);html=page.html();assert.match(html,/name="number"/);assert.doesNotMatch(html,/name="device_id"|name="device_prefix"|name="office/);
 page.submit([['unit_id',old.id],['number','87'],['name',''],['confirmed_usable','on']]);await flush();
 assert.deepEqual(page.writes[0],{action:'reassign_unit',unit_id:old.id,number:'87',name:'',confirmed_usable:true});
 for(const offices of [[],['office-a','office-b']]){const uncertain=fixture();uncertain.phone_devices.devices[0].office_ids=offices;const blocked=harness(uncertain);blocked.click('workflowReassign',old.id);assert.match(blocked.html(),/先核对手机设备资料/);assert.doesNotMatch(blocked.html(),/data-workflow-form="reassign_unit"/);assert.equal(blocked.writes.length,0);}
 for(const legacyId of ['29','office3:29']){
  const legacy=fixture(false),previous=legacy.phone_devices.devices[0].id;legacy.phone_devices.devices[0].id=legacyId;
  for(const unit of legacy.device_workflow.units)if(unit.device_id===previous){unit.device_id=legacyId;unit.original_device_id=legacyId;}
  const local=harness(legacy,false);local.click('workflowReassign',old.id);assert.match(local.html(),/name="device_id"/);assert.match(local.html(),/name="device_prefix"/);assert.doesNotMatch(local.html(),/name="number"/);
  local.submit([['unit_id',old.id],['device_id','87'],['device_prefix',legacyId.startsWith('office3:')?'office3:':''],['confirmed_usable','on']]);await flush();assert.equal(local.writes[0].device_id,legacyId.startsWith('office3:')?'office3:87':'87');assert.equal(local.writes[0].confirmed_usable,true);assert.ok(!('number' in local.writes[0]));
 }
 console.log('PASS: office-qualified workflow labels, exact physical IDs, old references, safe timeline names, scoped/legacy reassign payloads, ambiguous-office block and XSS escaping (synthetic DOM only)');
})().catch(error=>{console.error(error);process.exitCode=1;});
