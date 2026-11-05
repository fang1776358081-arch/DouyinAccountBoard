// Legacy route compatibility with wholly synthetic identities; no server or database.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const fields={},element=id=>fields[id]??={value:'',textContent:'',innerHTML:'',disabled:false,hidden:false,showModal(){this.open=true;},close(){this.open=false;}};
const context={$:element,document:{addEventListener(){}},hasPermission:()=>true,esc:value=>String(value??''),data:{computers:[{id:'synth-pc',name:'合成发布电脑'}],accounts:[
 {id:'synth-target',name:'合成雪原道路救援',phone_device_id:'87',douyin_id:'synthetic_target_handle'},
 {id:'synth-occupied',name:'合成同机其他账号',phone_device_id:'87',douyin_id:'synthetic_occupied_handle'},
 {id:'synth-long-phone',name:'合成名称含87',phone_device_id:'187',douyin_id:'synthetic_unrelated_handle'},
 {id:'synth-foreign',name:'合成三办账号',phone_device_id:'office3:87',douyin_id:'synthetic_foreign_handle'},
],publishing_layout:{enabled:true,batch:'synthetic-matrix-batch',cells:[
 {computer_id:'synth-pc',slot:4,source_cell:'synthetic-cell-a',label:'87-合成雪原货车救援',account_id:null},
 {computer_id:'synth-pc',slot:5,source_cell:'synthetic-cell-b',label:'合成已占用',account_id:'synth-occupied'},
]}}};
vm.createContext(context);
for(const file of ['identity-labels.js','field-search.js','edit-versions.js','publishing-match.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../static',file),'utf8'),context,{filename:file});
context.openPublishingMatch('synth-pc',4);
assert.equal(element('matchPhoneQuery').value,'');
assert.equal(element('publishingMatchDialog').open,true);
assert.deepEqual(Array.from(context.publishingCandidates(),account=>account.id),['synth-target','synth-occupied','synth-long-phone']);
assert.match(element('matchCount').textContent,/仅作排序线索/);
assert.match(element('matchCandidates').innerHTML,/value="synth-occupied" disabled/);
assert.match(element('matchCandidates').innerHTML,/选择账号 合成雪原道路救援/);
assert.equal(element('confirmPublishingMatch').disabled,true);
element('matchPhoneQuery').value='87';context.renderPublishingCandidates();
assert.deepEqual(Array.from(context.publishingCandidates(),account=>account.id),['synth-target','synth-occupied']);
element('matchIdQuery').value='synth-target';context.renderPublishingCandidates();
assert.deepEqual(Array.from(context.publishingCandidates(),account=>account.id),['synth-target']);
element('matchIdQuery').value='87';context.renderPublishingCandidates();
assert.equal(context.publishingCandidates().length,0);
assert.equal(context.publishingCandidateScope(context.data.accounts[3]),false);
console.log('ok synthetic legacy scope, source-phone ranking, explicit exact search, occupied-position disabling and manual confirmation');

context.data.accounts.push(
 {id:'synth-different-name',name:'合成极速货车救援',phone_device_id:'228',douyin_id:'synthetic_different_name'},
 {id:'synth-near-name',name:'合成畅行拖车汽车救援',phone_device_id:'228',douyin_id:'synthetic_near_name'},
 {id:'synth-exact-other-phone',name:'合成畅行拖车道路救援',phone_device_id:'229',douyin_id:'synthetic_exact_other_phone'},
);
context.data.publishing_layout.cells.push({computer_id:'synth-second-pc',slot:4,source_cell:'synthetic-cell-c',label:'228-合成畅行拖车道路救援',account_id:null});
context.openPublishingMatch('synth-second-pc',4);
assert.equal(element('matchPhoneQuery').value,'');
const candidates=Array.from(context.publishingCandidates(),account=>account.id);
assert.deepEqual(candidates.slice(0,2),['synth-near-name','synth-different-name']);
assert.ok(candidates.includes('synth-exact-other-phone'));
const suggestion=context.publishingSuggestion(context.data.accounts.find(account=>account.id==='synth-near-name'));
assert.equal(suggestion.recommended,true);assert.equal(suggestion.distance,2);
assert.equal(context.publishingSuggestion(context.data.accounts.find(account=>account.id==='synth-different-name')).recommended,false);
assert.equal(context.publishingSuggestion(context.data.accounts.find(account=>account.id==='synth-exact-other-phone')).recommended,false);
assert.match(element('matchCandidates').innerHTML,/近似建议 · 同手机228 · 名称相差2字/);
assert.equal(element('confirmPublishingMatch').disabled,true);
assert.equal(context.publishingNameCore('228-合成畅行拖车汽车救援（30）'),'合成畅行拖车汽车救援');
assert.equal(context.publishingNameDistance('abc','abc'),0);
assert.equal(context.publishingNameDistance('abc','axc'),1);
assert.equal(context.publishingSuggestion({name:'合成短名',phone_device_id:'228'}).recommended,false);
element('matchPhoneQuery').value='228';context.renderPublishingCandidates();
assert.deepEqual(Array.from(context.publishingCandidates(),account=>account.id),['synth-near-name','synth-different-name']);
element('matchNameQuery').value='汽车';context.renderPublishingCandidates();
assert.deepEqual(Array.from(context.publishingCandidates(),account=>account.id),['synth-near-name']);
console.log('ok synthetic same-phone name similarity ranking, two-character explanation, cross-phone inclusion, short-name exclusion and explicit user filters');
console.log(JSON.stringify({passed:true,checks:2,environment:'legacy-publishing-match-actual-functions-synthetic-dom-only'}));
