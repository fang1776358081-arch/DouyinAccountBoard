// Actual association UI, synthetic identities and DOM only. No server or database.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');

function harness({role='employee',phone=null,permissions=['publishing_manage'],extra=0}={}){
 const fields=new Map(),events=new Map(),calls=[],toasts=[];
 const element=id=>{if(!fields.has(id))fields.set(id,{value:'',textContent:'',innerHTML:'',disabled:false,hidden:false,open:false,showModal(){this.open=true;},close(){this.open=false;}});return fields.get(id);};
 const accounts=[
  {id:'synth-other-phone',name:'合成稳定目标账号',phone_device_id:'office3:88',douyin_id:'synth_other_phone',office_ids:['three']},
  {id:'synth-target',name:'合成稳定目标账号',phone_device_id:phone,douyin_id:'000synth_target_87',office_ids:['three']},
  {id:'synth-same-phone',name:'合成同手机其他账号',phone_device_id:'office3:87',douyin_id:'synth_same_phone',office_ids:['three']},
  {id:'synth-occupied',name:'合成已有发布位置',phone_device_id:'office3:87',douyin_id:'synth_occupied',office_ids:['three']},
  {id:'synth-foreign',name:'合成稳定目标账号',phone_device_id:'other:87',douyin_id:'synth_foreign',office_ids:['other']},
 ];
 for(let i=0;i<extra;i++)accounts.push({id:'synth-more-'+i,name:'合成其他账号'+i,phone_device_id:null,douyin_id:'synth_more_'+i,office_ids:['three']});
 const context={console,$:element,document:{addEventListener(type,fn){if(!events.has(type))events.set(type,[]);events.get(type).push(fn);}},hasPermission:permission=>permissions.includes(permission),toast:message=>toasts.push(message),captureVersions:()=>calls.push({kind:'capture'}),api:(...args)=>{calls.push({kind:'api',args});return Promise.reject(Error('Unexpected synthetic write'));},esc:value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  data:{user:{id:'synth-user-'+role,role,scope_mode:role==='owner'?'all':'assigned',office_ids:['three'],permissions},computers:[{id:'office3:1',name:'合成三办电脑',office_ids:['three']}],accounts,
   phone_devices:{devices:[{id:'office3:87',number:87,name:'合成设备',office_ids:['three']}]},device_workflow:{units:[]},offices:[{id:'three',name:'合成办公室三'}],
   publishing_layout:{enabled:true,batch:'synth-batch',cells:[{computer_id:'office3:1',slot:1,source_cell:'synth-cell-1',label:'87-合成稳定目标账号',account_id:null},{computer_id:'office3:1',slot:2,source_cell:'synth-cell-2',label:'合成占用位置',account_id:'synth-occupied'}]}}};
 element('confirmPublishingMatch').disabled=true;
 const before=JSON.stringify(context.data);vm.createContext(context);
 for(const name of ['identity-labels.js','field-search.js','publishing-match.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'static',name),'utf8'),context,{filename:name});
 for(const fn of events.get('DOMContentLoaded')||[])fn();
 return {context,element,calls,toasts,before,open(){context.openPublishingMatch('office3:1',1);},ids(){return Array.from(context.publishingCandidates(),row=>row.id);},run:source=>vm.runInContext(source,context),writes:()=>calls.filter(call=>call.kind==='api')};
}

let checks=0;function test(name,fn){fn();checks++;console.log('ok '+name);}
test('owner and employee can both discover the same authorized account despite a redacted phone relation',()=>{
 const owner=harness({role:'owner',phone:'office3:87'}),employee=harness();
 for(const h of [owner,employee]){h.open();assert.equal(h.element('matchPhoneQuery').value,'');assert.ok(h.ids().includes('synth-target'));assert.ok(h.ids().includes('synth-other-phone'));assert.ok(!h.ids().includes('synth-foreign'));assert.equal(h.element('publishingMatchDialog').open,true);assert.equal(h.element('confirmPublishingMatch').disabled,true);assert.equal(h.writes().length,0);assert.equal(JSON.stringify(h.context.data),h.before);}
 assert.deepEqual(new Set(owner.ids()),new Set(employee.ids()));
});
test('complete handle search finds a phone-redacted account without creating an implicit source-phone condition',()=>{
 const h=harness();h.open();h.element('matchDouyinQuery').value='000synth_target_87';h.element('matchDouyinQuery').oninput();assert.deepEqual(h.ids(),['synth-target']);assert.match(h.element('matchCandidates').innerHTML,/000synth_target_87/);assert.equal(h.element('matchPhoneQuery').value,'');h.element('matchDouyinQuery').value='000synth_target';h.element('matchDouyinQuery').oninput();assert.deepEqual(h.ids(),[]);assert.equal(h.writes().length,0);
});
test('an explicit phone condition still excludes unknown phone relations and clearing it restores eligible accounts',()=>{
 const h=harness();h.open();h.element('matchPhoneQuery').value='87';h.element('matchPhoneQuery').oninput();assert.ok(!h.ids().includes('synth-target'));assert.ok(h.ids().includes('synth-same-phone'));assert.match(h.element('matchCount').textContent,/手机信息待核对.*不会匹配/);assert.match(h.element('matchCount').textContent,/清空/);h.element('resetMatchSearch').onclick();assert.ok(h.ids().includes('synth-target'));assert.ok(!h.ids().includes('synth-foreign'));assert.equal(h.element('matchPhoneQuery').value,'');assert.equal(h.writes().length,0);
});
test('source phone and name similarity only rank suggestions and never infer a hidden phone or select an account',()=>{
 const h=harness();h.open();const ids=h.ids();assert.ok(ids.indexOf('synth-same-phone')<ids.indexOf('synth-other-phone'));const target=h.context.data.accounts.find(row=>row.id==='synth-target');assert.equal(h.context.publishingSuggestion(target).samePhone,false);assert.equal(h.context.publishingSuggestion(target).recommended,false);assert.equal(target.phone_device_id,null);assert.equal(h.run('matchingAccountId'),null);assert.doesNotMatch(h.element('matchCandidates').innerHTML,/\bchecked\b/);assert.match(h.element('matchCandidates').innerHTML,/手机信息待核对/);assert.equal(h.element('confirmPublishingMatch').disabled,true);assert.match(h.element('matchCount').textContent,/仅作排序线索/);assert.equal(h.writes().length,0);
});
test('an account occupying another publication cell remains visible but disabled',()=>{
 const h=harness();h.open();assert.ok(h.ids().includes('synth-occupied'));assert.match(h.element('matchCandidates').innerHTML,/value="synth-occupied"\s+disabled/);assert.match(h.element('matchCandidates').innerHTML,/已有安排.*不可重复关联/);assert.equal(h.element('confirmPublishingMatch').disabled,true);assert.equal(h.writes().length,0);
});
test('missing association permission cannot open the window, capture locks or send an API request',()=>{
 const h=harness({permissions:['publish']});h.open();assert.equal(h.element('publishingMatchDialog').open,false);assert.equal(h.calls.length,0);assert.match(h.toasts[0],/没有.*发布格子关联权限/);assert.equal(JSON.stringify(h.context.data),h.before);
});
test('the forty-row display cap is explicit and a complete handle can locate a later authorized record',()=>{
 const h=harness({extra:45});h.open();assert.equal(h.ids().length,49);assert.equal((h.element('matchCandidates').innerHTML.match(/<tr>/g)||[]).length,40);assert.match(h.element('matchCount').textContent,/49.*40/);assert.match(h.element('matchCount').textContent,/完整抖音号/);h.element('matchDouyinQuery').value='synth_more_44';h.element('matchDouyinQuery').oninput();assert.deepEqual(h.ids(),['synth-more-44']);assert.match(h.element('matchCandidates').innerHTML,/synth_more_44/);assert.equal(h.writes().length,0);
});
test('fresh openings clear earlier explicit conditions and preserve business state without cross-office candidates',()=>{
 const h=harness();h.open();for(const id of ['matchPhoneQuery','matchIdQuery','matchNameQuery','matchDouyinQuery'])h.element(id).value='previous-synthetic-filter';h.element('publishingMatchDialog').close();h.open();for(const id of ['matchPhoneQuery','matchIdQuery','matchNameQuery','matchDouyinQuery'])assert.equal(h.element(id).value,'');assert.ok(h.ids().includes('synth-target'));assert.ok(!h.ids().includes('synth-foreign'));assert.equal(h.run('matchingAccountId'),null);assert.equal(h.element('confirmPublishingMatch').disabled,true);assert.equal(JSON.stringify(h.context.data),h.before);assert.equal(h.writes().length,0);
});
console.log(JSON.stringify({passed:true,checks,environment:'actual-publishing-match-functions-synthetic-dom-no-network-or-database'}));
