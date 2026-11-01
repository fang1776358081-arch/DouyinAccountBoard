// Actual phone-card rows and read-only details, synthetic DOM/data only.
// No browser, external requests, database, dependencies or business files.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const {harness:baseHarness,fixture,root}=require('./test_performance_ui.cjs');
const clone=value=>structuredClone(value),plain=value=>JSON.parse(JSON.stringify(value));
function board(){
 const b=fixture(180);b.user={...b.user,role:'viewer',permissions:[],scope_mode:'all',office_ids:['one']};
 b.phone_cards.statuses=['待确认','正常','欠费','停机','停用','待注销','已注销'];b.phone_cards.code_states=['无待办','待接码','已完成','暂无法接码'];b.phone_cards.return_states=['待确认','不需归还','待归还','已归还'];
 b.phone_cards.cards.forEach((card,i)=>Object.assign(card,{office_ids:['one'],status:b.phone_cards.statuses[i%7],code_state:'无待办',return_state:'待确认',note:'普通备注',source_note:''}));
 const card=b.phone_cards.cards[0];Object.assign(card,{location:'原表 105 号手机',carrier:'其他',custom_carrier:'合成运营商',owner:'合成卡主',keeper:'合成保管人',ownership:'代管',code_state:'待接码',code_contact:'合成接码联系人',code_note:'CODE-NOTE 全文\n<script>unsafe</script>',return_state:'待归还',return_due:'2026-11-10',returned_on:'',expires_on:'2026-11-01',note:'LONG-NOTE 全文\n<unsafe>备注</unsafe>',source_note:'SOURCE-NOTE 全文\n<svg onload="unsafe">原文</svg>',placement:{unit_id:'synthetic-old-unit',slot:'待确认'}});
 b.device_workflow={enabled:true,units:[{id:'synthetic-old-unit',device_id:'office3:29',label:'合成29号旧机',active:false,status:'故障'}],placements:[],cases:[],access:[],events:[]};
 for(let i=0;i<10;i++)b.accounts[i].registration_phone=card.phone;b.accounts[9].name='最后一个完整关联账号';
 return b;
}
function page(b=board()){
 const p=baseHarness(root),append=p.context.document.body.appendChild;p.dialogs=[];
 p.context.document.body.appendChild=node=>{append(node);if(node.id){p.elements.set(node.id,node);p.dialogs.push(node);}};
 const query=p.context.document.querySelector;p.context.document.querySelector=selector=>selector==='dialog[open]'?[...p.elements.values()].find(node=>node.open)||null:query(selector);
 for(const name of ['card-filters.js','device-workflow.js','card-details.js','board-updates.js'])vm.runInContext(fs.readFileSync(path.join(root,'static',name),'utf8'),p.context,{filename:name});
 p.reset(clone(b),'cards');p.run('CardFilters.bind();render()');p.details=p.context.CardDetails;
 p.dialog=()=>p.elements.get('cardDetailsDialog');return p;
}
let checks=0;function test(name,fn){fn();checks++;console.log('ok '+name);}
(()=>{
 test('compact table keeps eight cells, fifty rows and unchanged all-result statistics',()=>{
  const p=page(),html=p.element('cardsBody').innerHTML;assert.equal((html.match(/<tr>/g)||[]).length,50);assert.equal((html.split('</tr>')[0].match(/<td\b/g)||[]).length,8);assert.equal(p.run('filteredCards().length'),60);assert.match(p.element('cardsScope').textContent,/筛选 60 \/ 60 张卡/);assert.match(p.element('cardsPage').textContent,/第 1 \/ 2 页/);assert.match(html,/data-batch-pick="cards"/);assert.match(html,/data-card-details="1"/);assert.match(html,/data-card-edit="1"/);assert.match(html,/data-workflow-card="1"/);assert.match(html,/data-card-accounts="synthetic-phone-0"/);assert.equal(p.calls.length,0);
 });
 test('row preview moves long notes, raw source, processing text and full related accounts off the first screen',()=>{
  const p=page(),html=p.element('cardsBody').innerHTML;assert.ok(!html.includes('LONG-NOTE')&&!html.includes('SOURCE-NOTE')&&!html.includes('CODE-NOTE'));assert.ok(!html.includes('最后一个完整关联账号'));assert.match(html,/合成运营商/);assert.match(html,/接码：待接码/);assert.match(html,/归还：待归还/);assert.match(html,/条件未齐|条件已记录/);
 });
 test('on-demand details show complete existing data safely escaped with no request or mutation',()=>{
  const p=page(),before=p.run('JSON.stringify(data)'),filter=p.element('cardOfficeFilter').value;assert.equal(p.dialog(),undefined);p.click('[data-card-details]',{cardDetails:'1'});const dialog=p.dialog();assert.equal(dialog.open,true);const html=dialog.innerHTML;
  for(const text of ['手机卡详情','LONG-NOTE 全文','SOURCE-NOTE 全文','CODE-NOTE 全文','最后一个完整关联账号','合成接码联系人','2026-11-10','合成保管人','关联账号（10）','原文字位置（仅参考）','原表 105 号手机','合成29号旧机'])assert.ok(html.includes(text),text);
  assert.ok(html.includes('&lt;script&gt;unsafe&lt;/script&gt;'));assert.ok(html.includes('&lt;svg onload=&quot;unsafe&quot;&gt;'));assert.ok(!html.includes('<script>')&&!html.includes('<svg')&&!html.includes('<input')&&!html.includes('<form'));assert.equal(p.run('JSON.stringify(data)'),before);assert.equal(p.element('cardOfficeFilter').value,filter);assert.equal(p.calls.length,0);
 });
 test('all seven stored statuses, blank and legacy values keep their original labels and semantic classes',()=>{
  const b=board();b.phone_cards.cards[7].status='';b.phone_cards.cards[8].status='旧卡状态';const p=page(b),html=p.element('cardsBody').innerHTML;
  for(const state of b.phone_cards.statuses)assert.ok(html.includes('>'+state+'</span>'),state);for(const kind of ['pending','normal','overdue','suspended','inactive','cancel-pending','cancelled','blank','legacy'])assert.ok(html.includes('card-status-'+kind),kind);assert.match(html,/未登记（空白）/);assert.match(html,/旧卡状态/);assert.equal(p.run('data.phone_cards.cards[8].status'),'旧卡状态');
 });
 test('read-only details reuse explicit office scope and cannot open an ungranted or unknown card',()=>{
  const b=board();b.user={...b.user,scope_mode:'assigned',office_ids:['one']};b.phone_cards.cards[1].office_ids=['three'];const p=page(b);assert.equal(p.details.open(2),false);assert.equal(p.details.open('unknown'),false);assert.equal(p.dialog(),undefined);assert.equal(p.details.open(1),true);assert.equal(p.dialog().open,true);assert.equal(p.calls.length,0);
 });
 test('native dialog blocks automatic snapshots, respects other drafts and closes cleanly without writes',()=>{
  const p=page();assert.equal(p.details.open(1),true);assert.equal(p.run('boardSyncBlocked()'),true);p.details.reset();assert.equal(p.details.isOpen(),false);assert.equal(p.dialog().innerHTML,'');assert.equal(p.run('boardSyncBlocked()'),false);
  assert.equal(p.dialog().listeners.has('cancel'),false,'native Escape remains available');p.element('editor').open=true;assert.equal(p.details.open(1),false);p.element('editor').open=false;p.context.AccountInline={hasDraft:()=>true,announceBlocked(){p.blocked=true;}};assert.equal(p.details.open(1),false);assert.equal(p.blocked,true);assert.equal(p.calls.length,0);
 });
 test('position text keeps confirmed old physical phones distinct from original source references',()=>{
  const b=board();Object.assign(b.phone_cards.cards[1],{location:'原表 29 号手机'});const p=page(b);p.details.open(1);assert.match(p.dialog().innerHTML,/合成29号旧机/);assert.match(p.dialog().innerHTML,/卡槽待确认/);assert.match(p.dialog().innerHTML,/已记录插卡实物/);p.details.reset();p.details.open(2);assert.match(p.dialog().innerHTML,/尚未确认插卡实物；原表文字仅供参考/);assert.match(p.element('cardsBody').innerHTML,/仅为原表参考/);
 });
 test('empty source inventory and an empty filtered intersection have distinct explanations',()=>{
  const noCards=board();noCards.phone_cards.cards=[];const empty=page(noCards);assert.match(empty.element('cardsBody').innerHTML,/当前授权范围没有手机卡资料/);assert.match(empty.element('cardsBody').innerHTML,/colspan="8"/);
  const filtered=page();filtered.element('cardQuery').value='never-match';filtered.run('render()');assert.match(filtered.element('cardsBody').innerHTML,/没有符合当前条件/);assert.match(filtered.element('cardsBody').innerHTML,/重置条件（保留办公室）/);assert.equal(filtered.element('cardFilterLoadHint').hidden,true);
 });
 test('details cannot retain another identity or a removed card after an authoritative refresh',()=>{
  const p=page();p.details.open(1);p.run('data.user.id="different-synthetic-user"');assert.equal(p.details.render(),false);assert.equal(p.details.isOpen(),false);assert.equal(p.dialog().innerHTML,'');
  p.run('data.user.id="synthetic-owner"');p.details.open(1);p.run('data.phone_cards.cards=data.phone_cards.cards.filter(card=>card.id!==1)');assert.equal(p.details.render(),false);assert.equal(p.dialog().innerHTML,'');assert.equal(p.calls.length,0);
 });
 console.log(JSON.stringify({passed:true,checks,kind:'actual-card-rows-and-details-synthetic-dom-only'}));
})();