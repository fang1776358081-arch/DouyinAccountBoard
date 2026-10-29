// Actual frontend helpers and handlers; synthetic DOM only, no network or DB.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const read=file=>fs.readFileSync('static/'+file,'utf8');
const selects=new Set(['computer','loginPhoneFilter','editPhoneDevice','deviceGroup','deviceState','deviceAccountChoice','deviceStateFilter','deviceOccupancyFilter']);
const fields=new Map(),listeners=new Map();
function element(id){
 if(fields.has(id))return fields.get(id);
 let html='',value='',options=[],index=-1;
 const node={id,hidden:false,disabled:false,readOnly:false,textContent:'',dataset:{},open:false,
  classList:{toggle(){}},addEventListener(){},after(){},before(){},closest(){return element('label:'+id);},
  showModal(){this.open=true;},close(){this.open=false;},
  querySelector(selector){if(selector==='[data-office-picker]')return element('officePicker');return null;},
  get innerHTML(){return html;},set innerHTML(next){html=next;if(selects.has(id)){options=[...next.matchAll(/<option\b([^>]*)>(.*?)<\/option>/gs)].map(match=>({value:match[1].match(/value="([^"]*)"/)?.[1]??match[2],textContent:match[2]}));index=options.length?0:-1;}},
  get value(){return selects.has(id)?options[index]?.value??'':value;},set value(next){value=String(next);if(selects.has(id))index=options.findIndex(option=>option.value===value);},
  get options(){return options;},get selectedIndex(){return index;}};
 fields.set(id,node);return node;
}
const picker=element('officePicker');picker.dataset.officePicker='devices';picker.selected=[];
picker.querySelector=()=>({selectedOptions:picker.selected});
const fixture={user:{role:'owner',scope_mode:'all',permissions:['device_create','device_manage']},offices:[{id:'one',name:'办公室一'},{id:'two',name:'办公室二'},{id:'three',name:'办公室三'}],
 accounts:[],phone_devices:{statuses:['待确认','可用闲置'],devices:[
 {id:'29',name:'29号手机',number:'29',office_ids:['one'],state:'待确认',account_count:0},
 {id:'phone:'+'a'.repeat(32)+':29',name:'备用机',number:'29',office_ids:['two'],state:'可用闲置',account_count:0},
 {id:'office3:29',name:'29号手机',office_ids:['three'],state:'待确认',account_count:0},
 {id:'35',name:'旧待核对',office_ids:[],state:'待确认',account_count:0},
 {id:'36',name:'旧多归属',office_ids:['one','two'],state:'待确认',account_count:0}]},
 phone_cards:{cards:[]},device_workflow:{enabled:false},computers:[]};
const context={console,URLSearchParams,location:{search:'?office=three'},data:structuredClone(fixture),page:'phones',selected:new Set(),activeLifecyclePreset:'',
 $:element,esc:value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
 document:{getElementById:element,addEventListener(type,fn){if(!listeners.has(type))listeners.set(type,[]);listeners.get(type).push(fn);},querySelectorAll(){return [];},createElement(){return {};},body:{append(){}}},
 window:{},captureVersions(){},applyDevicePermissions(){},renderBatchControls(){},claimRequestId(){return 'synthetic-request';},settleRequestId(){},toast(){},async load(){},calls:[],
 async api(path,body){context.calls.push({path,body});return {ok:true,id:'synthetic-server-id'};}};
vm.createContext(context);
for(const file of ['identity-labels.js','scope-ui.js','field-search.js','offices.js','phone-cards.js','phone-devices.js','publishing-match.js'])vm.runInContext(read(file),context,{filename:file});
const run=code=>vm.runInContext(code,context);
context.render=()=>{run('renderOffices();renderPhones()');};
for(const fn of listeners.get('DOMContentLoaded')||[])fn();
element('deviceGroup').innerHTML='<option value="">原有</option><option value="office3:">三办</option>';
element('deviceStateFilter').innerHTML='<option value="">全部</option>';
element('deviceOccupancyFilter').innerHTML='<option value="">全部</option>';
let checks=0;function check(name,work){work();checks++;}
(async()=>{
 const original=JSON.stringify(context.data);
 check('different offices show distinct device labels with the same number',()=>{
  assert.match(run('deviceDisplay(data.phone_devices.devices[0],data)'),/办公室一 · 29号手机/);
  assert.match(run('deviceDisplay(data.phone_devices.devices[1],data)'),/办公室二 · 29号手机 · 备用机/);
  assert.doesNotMatch(run('deviceDisplay(data.phone_devices.devices[1],data)'),/phone:|aaaa/);
  assert.equal(run('deviceNumber({id:"phone:"+"b".repeat(32)+":029"})'),'29');
 });
 check('phone page exposes office tabs and counts devices rather than accounts',()=>{
  run('renderOffices();renderPhones()');assert.equal(element('officeWorkspace').hidden,false);
  assert.match(element('officeTabs').innerHTML,/手机设备数量/);
  assert.deepEqual(Array.from(run('filteredPhones()'),d=>d.id),['office3:29']);
  assert.doesNotMatch(element('phonesBody').innerHTML,/办公室一 · 29号手机/);
 });
 check('phone number search remains inside the selected office',()=>{
  element('deviceQuery').value='29';run('activeOffice="two";renderOffices();renderPhones()');
  assert.deepEqual(Array.from(run('filteredPhones()'),d=>d.id),[fixture.phone_devices.devices[1].id]);
  run('activeOffice="all";renderOffices();renderPhones()');assert.equal(run('filteredPhones().length'),3);
 });
 check('all-office relation picker keeps complete IDs and distinguishes same numbers',()=>{
  run('renderPhoneFilter();phoneDeviceEdit({id:"synthetic-account",phone_device_id:"29"})');
  assert.equal(element('editPhoneDevice').value,'29');
  assert.match(element('editPhoneDevice').innerHTML,/办公室一 · 29号手机/);
  assert.match(element('editPhoneDevice').innerHTML,/办公室二 · 29号手机/);
  assert.equal(element('editPhoneDevice').options.length,fixture.phone_devices.devices.length+1);
 });
 check('device scope is single but card and personnel grants stay multi',()=>{
  assert.equal(run('ScopeUI.model(data,"devices").single'),true);
  assert.equal(run('ScopeUI.model(data,"cards").single'),false);
  assert.match(read('users.js'),/byId\('scopeOffices'\)\.multiple=!device/);
  assert.match(read('users.js'),/type="checkbox" value="\$\{safe\(office.id\)\}"/);
 });
 check('legacy pending/multi scope defaults to preserve, never silently first office',()=>{
  run('ScopeUI.mount("phoneDeviceForm",data,"devices",data.phone_devices.devices[4])');
  assert.match(picker.innerHTML,/value="__keep__" selected/);
  assert.doesNotMatch(picker.innerHTML,/value="one" selected|value="two" selected/);
  picker.selected=[{value:'__keep__'}];assert.deepEqual(Object.keys(run('ScopeUI.payload("phoneDeviceForm")')),[]);
  picker.selected=[{value:'one'},{value:'two'}];assert.throws(()=>run('ScopeUI.payload("phoneDeviceForm")'),/一个办公室/);
 });
 check('new device defaults explicitly to the current office without old batch inference',()=>{
  run('activeOffice="two";editPhoneDevice("")');
  assert.match(picker.innerHTML,/value="two" selected/);
  assert.equal(element('label:deviceGroup').hidden,true);assert.equal(element('deviceId').readOnly,false);
 });
 picker.selected=[{value:'two'}];element('deviceId').value='29';element('deviceState').value='待确认';
 await element('phoneDeviceForm').onsubmit({preventDefault(){},submitter:{disabled:false}});
 check('new scoped save submits number and exactly one office, never a guessed internal ID',()=>{
  const saved=context.calls.at(-1);assert.equal(saved.path,'/api/phone-device/create');
  assert.equal(saved.body.number,'29');assert.equal(saved.body.office_ids.join(),'two');assert.equal(Object.hasOwn(saved.body,'id'),false);
 });
 check('existing edits retain exact internal IDs and do not renumber or select another office',()=>{
  run('editPhoneDevice(data.phone_devices.devices[1].id)');assert.equal(element('deviceKey').value,fixture.phone_devices.devices[1].id);
  assert.equal(element('deviceId').readOnly,true);assert.match(picker.innerHTML,/value="two" selected/);
 });
 check('rendering and opening editors did not change business records',()=>assert.equal(JSON.stringify(context.data),original));
 context.data.device_workflow={enabled:true,units:[{id:'unit-one',device_id:'office3:29'},{id:'unit-two',device_id:fixture.phone_devices.devices[1].id}]};
 context.data.phone_cards.cards=[{id:1,phone:'synthetic-card-1',placement:{unit_id:'unit-one'}},{id:2,phone:'synthetic-card-2',placement:{unit_id:'unit-two'}},{id:3,phone:'synthetic-card-3',placement:{storage:'卡盒第3排'}}];
 check('known SIM placements search exact sticker numbers, not office/UUID digits',()=>{
  element('cardLocationQuery').value='29';assert.deepEqual(Array.from(run('filteredCards()'),c=>c.id),[1,2]);
  element('cardLocationQuery').value='3';assert.deepEqual(Array.from(run('filteredCards()'),c=>c.id),[3]);
 });
 check('HTML loads versioned office helpers on both pages',()=>{
  for(const file of ['index.html','workspace.html'])assert.match(read(file),/identity-labels\.js\?v=20261029-device-offices/);
  assert.match(read('index.html'),/scope-ui\.js\?v=20261029-device-offices/);
 });
 check('publishing candidates use real offices, never the new phone ID or stale legacy batch',()=>{
  context.data.computers=[{id:'pc-three',office_ids:['three']}];
  assert.equal(run('publishingCandidateScope({office_ids:["three"],phone_device_id:"phone:"+"c".repeat(32)+":29",office_group:""},{computer_id:"pc-three"})'),true);
  assert.equal(run('publishingCandidateScope({office_ids:["one"],phone_device_id:"office3:29",office_group:"three"},{computer_id:"pc-three"})'),false);
 });
 console.log(`PASS: ${checks} office-local device UI groups (actual frontend helpers/handlers, synthetic DOM only)`);
})().catch(error=>{console.error(error);process.exitCode=1;});
