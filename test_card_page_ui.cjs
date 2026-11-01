// Actual card presentation/filter/batch/load scripts with controlled DOM and fetch.
// Invented fixtures only: no browser, business data, backend import or network.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {harness:baseHarness,fixture,root}=require('./test_performance_ui.cjs');
const {board:cardFixture}=require('./test_card_filters_ui.cjs');
const read=name=>fs.readFileSync(path.join(root,name),'utf8');
const plain=value=>JSON.parse(JSON.stringify(value));
const response=value=>({ok:true,status:200,json:async()=>structuredClone(value)});
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const filterIds=['cardOfficeFilter','cardQuery','cardStatus','cardCarrier','clearCards','refreshCards','cardAssessment','cardLocationQuery','cardOwnerQuery','cardLabelQuery','cardAccountNameQuery','cardLinked','cardCodeFilter','cardReturnFilter','cardPositionFilter','cardLinkedStateFilter','cardSourceFilter','cardExpiry','importCards'];
function board(count=null){
 const value=cardFixture();value.user={...value.user,name:'合成页面人员',permissions:['card_manage','card_import','card_create']};
 if(count!==null){value.phone_cards.cards=Array.from({length:count},(_,index)=>({...value.phone_cards.cards[0],id:index+1,phone:'synthetic-bulk-'+(index+1),office_ids:['three']}));value.accounts=[];}
 return value;
}
function harness(value=board(),{loaded=true}={}){
 const page=baseHarness(root),requests=[],bodyClasses=new Set();
 page.context.document.body.classList.toggle=(name,on)=>{if(on)bodyClasses.add(name);else bodyClasses.delete(name);};
 const controls=filterIds.map(id=>page.element(id));const selectPage=page.element('synthetic-card-page-selection');controls.push(selectPage);
 page.element('cardsWorkspace').querySelectorAll=selector=>selector==='.card-filter-toolbar input,.card-filter-toolbar select,.card-filter-toolbar button,.card-selection-entry button'?controls:[];
 const phases=new Set();page.element('cardReadState').classList.toggle=(name,on)=>{if(on)phases.add(name);else phases.delete(name);};
 page.run(read('static/auth-ui.js'));page.run(read('static/card-filters.js'));page.run(read('static/card-page.js'));
 page.context.fetch=(url,options)=>{const pending=deferred();requests.push({url,options,...pending});return pending.promise;};
 page.reset(structuredClone(value),'cards');page.run('CardFilters.bind()');
 if(loaded)page.show(structuredClone(value));else page.run('data=null;globalThis.data=null;page="cards"');
 return Object.assign(page,{requests,controls,bodyClasses,phases});
}
const ids=page=>plain(page.run('filteredCards().map(card=>card.id)'));
const rowIds=page=>[...page.element('cardsBody').innerHTML.matchAll(/data-batch-pick="cards" value="([^"]+)"/g)].map(match=>Number(match[1]));
let checks=0,failed=0;
async function test(name,job){try{await job();checks++;console.log('PASS '+name);}catch(error){failed++;console.error('FAIL '+name+': '+error.message);}}
(async()=>{
 await test('cold load has an explicit busy state and never invents zero records',async()=>{
  const page=harness(board(),{loaded:false}),pending=page.run('load()');
  assert.equal(page.requests.length,1);assert.equal(page.element('cardReadState').hidden,false);assert.equal(page.element('cardReadState').attrs.role,'status');assert.equal(page.element('cardsWorkspace').attrs['aria-busy'],'true');assert.match(page.element('cardReadMessage').textContent,/正在读取手机卡资料/);
  assert.match(page.element('cardsBody').innerHTML,/正在加载清单/);assert.doesNotMatch(page.element('cardsBody').innerHTML,/没有符合|共\s*0|没有手机卡/);assert.equal(page.element('stats').innerHTML,'');assert.ok(page.controls.every(field=>field.disabled));assert.equal(page.element('newCard').hidden,true);assert.equal(page.bodyClasses.has('cards-console'),true);
  page.requests[0].resolve(response(board()));assert.equal(await pending,true);assert.equal(page.element('cardReadState').hidden,true);assert.equal(page.element('cardsWorkspace').attrs['aria-busy'],'false');assert.equal(rowIds(page).length,10);assert.equal(page.element('newCard').hidden,false);assert.ok(page.controls.filter(field=>field.id!=='importCards').every(field=>!field.disabled));
 });
 await test('cold failure is distinct from an empty list and successful retry removes the error',async()=>{
  const page=harness(board(),{loaded:false}),pending=page.run('load()');page.requests[0].reject(Error('synthetic-private-network-detail'));assert.equal(await pending,false);
  assert.equal(page.element('cardReadState').hidden,false);assert.equal(page.element('cardReadState').attrs.role,'alert');assert.equal(page.element('cardReadRetry').hidden,false);assert.equal(page.element('loadError').hidden,true);assert.equal(page.element('cardsWorkspace').attrs['aria-busy'],'false');assert.match(page.element('cardReadMessage').textContent,/加载失败.*不能判定是否有记录/);assert.doesNotMatch(page.element('cardReadMessage').textContent,/synthetic-private/);assert.match(page.element('cardsBody').innerHTML,/资料尚未加载/);
  const retry=page.run('load()');page.requests[1].resolve(response(board(0)));assert.equal(await retry,true);assert.equal(page.element('cardReadState').hidden,true);assert.doesNotMatch(page.element('cardsBody').innerHTML,/资料尚未加载|正在加载/);assert.match(page.element('cardsBody').innerHTML,/当前授权范围没有手机卡资料/);
 });
 await test('refresh keeps the current filtered table and selection when loading and failing',async()=>{
  const page=harness(),before=page.run('JSON.stringify(data)');page.element('cardOfficeFilter').value='three';page.run('batchSelection("cards").add("1");render()');const table=page.element('cardsBody').innerHTML;
  const pending=page.run('load()');assert.equal(page.element('cardsBody').innerHTML,table);assert.match(page.element('cardReadMessage').textContent,/正在刷新.*仍可查看/);assert.ok(page.controls.filter(field=>field.id!=='importCards').every(field=>!field.disabled));page.requests[0].reject(Error('synthetic-refresh-failed'));assert.equal(await pending,false);
  assert.equal(page.element('cardsBody').innerHTML,table);assert.equal(page.run('JSON.stringify(data)'),before);assert.equal(page.run('batchSelection("cards").has("1")'),true);assert.equal(page.element('cardOfficeFilter').value,'three');assert.match(page.element('cardReadMessage').textContent,/刷新失败.*保留上次资料/);assert.equal(page.element('cardReadRetry').hidden,false);
 });
 await test('a stale load failure cannot hide or relabel a newer successful refresh',async()=>{
  const page=harness(),older=page.run('load()'),newer=page.run('load()');page.requests[1].resolve(response(board(1)));assert.equal(await newer,true);const table=page.element('cardsBody').innerHTML;page.requests[0].reject(Error('synthetic-obsolete-failure'));assert.equal(await older,false);
  assert.equal(page.element('cardsBody').innerHTML,table);assert.equal(page.element('cardReadState').hidden,true);assert.equal(page.element('loadError').hidden,true);assert.equal(page.element('cardsWorkspace').attrs['aria-busy'],'false');
 });
 await test('background refresh neither flashes card loading UI nor clears retained rows before response',async()=>{
  const page=harness(),table=page.element('cardsBody').innerHTML;page.run('CardPageUI.state("ready",true)');const pending=page.run('load(false,{background:true,isCurrent:()=>true})');assert.equal(page.element('cardReadState').hidden,true);assert.equal(page.element('cardsBody').innerHTML,table);page.requests[0].resolve(response(board(1)));assert.equal(await pending,true);assert.equal(rowIds(page).length,1);
 });
 await test('cold recovery restores filters but reapplies existing create/import permissions',async()=>{
  const value=board();value.user={...value.user,role:'viewer',permissions:[]};value.phone_cards.importable=4;const page=harness(value,{loaded:false}),pending=page.run('load()');page.requests[0].resolve(response(value));assert.equal(await pending,true);
  assert.equal(page.element('newCard').hidden,true);assert.equal(page.element('importCards').disabled,true);assert.ok(page.controls.filter(field=>field.id!=='importCards').every(field=>!field.disabled));assert.equal(page.element('cardQuery').value,'');assert.equal(page.element('cardOfficeFilter').value,'all');
 });
 await test('cold card load finishing on another page clears busy and restores filters before returning',async()=>{
  const page=harness(board(),{loaded:false}),pending=page.run('load()');page.run('page="accounts";render()');page.requests[0].resolve(response(board()));assert.equal(await pending,true);
  assert.equal(page.run('page'),'accounts');assert.equal(page.bodyClasses.has('cards-console'),false);assert.equal(page.element('cardReadState').hidden,true);assert.equal(page.element('cardsWorkspace').attrs['aria-busy'],'false');assert.ok(page.controls.filter(field=>field.id!=='importCards').every(field=>!field.disabled));
  page.run('page="cards";render()');assert.equal(rowIds(page).length,10);assert.equal(page.element('cardReadState').hidden,true);assert.equal(page.element('refreshCards').disabled,false);assert.equal(page.bodyClasses.has('cards-console'),true);
 });
 await test('warm card refresh finishing on another page does not leave a stale loading banner on return',async()=>{
  const page=harness();page.element('cardOfficeFilter').value='three';page.run('batchSelection("cards").add("1");render()');const pending=page.run('load()');page.run('page="accounts";render()');page.requests[0].resolve(response(board()));assert.equal(await pending,true);
  assert.equal(page.bodyClasses.has('cards-console'),false);assert.equal(page.element('cardReadState').hidden,true);assert.equal(page.element('cardsWorkspace').attrs['aria-busy'],'false');page.run('page="cards";render()');assert.equal(page.element('cardOfficeFilter').value,'three');assert.equal(page.run('batchSelection("cards").has("1")'),true);assert.equal(rowIds(page).length,8);assert.equal(page.element('cardReadState').hidden,true);
 });
 await test('off-page cold failure releases only refresh until a later successful read can restore authorized controls',async()=>{
  const page=harness(board(),{loaded:false}),pending=page.run('load()');page.run('page="accounts";CardPageUI.activate(false)');page.requests[0].reject(Error('synthetic-off-page-failure'));assert.equal(await pending,false);
  assert.equal(page.bodyClasses.has('cards-console'),false);assert.equal(page.element('refreshCards').disabled,false);assert.equal(page.element('importCards').disabled,true);assert.equal(page.element('cardsWorkspace').attrs['aria-busy'],'false');assert.equal(page.element('cardReadState').hidden,true);
  const retry=page.run('load()');page.requests[1].resolve(response(board()));assert.equal(await retry,true);page.run('page="cards";render()');assert.equal(rowIds(page).length,10);assert.ok(page.controls.filter(field=>field.id!=='importCards').every(field=>!field.disabled));assert.equal(page.element('cardReadState').hidden,true);
 });
 await test('contextual batch bar is absent with zero picks and keeps exact page versus filtered scope',()=>{
  const page=harness(board(121));assert.equal(page.element('batchCardsBar').innerHTML,'');assert.equal(rowIds(page).length,50);assert.equal(page.run('batchCurrentPage("cards").length'),50);
  page.click('[data-batch-all]',{batchAll:'cards',batchPage:'yes'});assert.equal(page.run('batchSelection("cards").size'),50);assert.match(page.element('batchCardsBar').innerHTML,/<strong>50<\/strong>/);assert.match(page.element('batchCardsBar').innerHTML,/本页 50 张.*筛选共 121 张/);
  page.run('cardPageIndex=2;render()');assert.equal(rowIds(page)[0],51);page.click('[data-batch-all]',{batchAll:'cards',batchPage:'yes'});assert.equal(page.run('batchSelection("cards").size'),100);page.click('[data-batch-all]',{batchAll:'cards'});assert.equal(page.run('batchSelection("cards").size'),121);
  page.element('cardQuery').value='synthetic-bulk-1';page.run('render()');assert.equal(page.run('batchSelection("cards").size'),ids(page).length);assert.equal(page.run('[...batchSelection("cards")].every(id=>filteredCards().some(row=>String(row.id)===id))'),true);page.click('[data-batch-clear]',{batchClear:'cards'});assert.equal(page.element('batchCardsBar').innerHTML,'');assert.equal(page.requests.length,0);
 });
 await test('batch presentation cannot add card fields or enable writes for task-only or readonly staff',()=>{
  for(const permissions of [[],['card_tasks']]){const value=board();value.user={...value.user,role:'employee',permissions};const page=harness(value);page.run('batchSelection("cards").add("1");render()');assert.match(page.element('batchCardsBar').innerHTML,/data-batch-open="cards" data-batch-source="cards" disabled/);assert.deepEqual(plain(page.run('batchFields("cards").map(field=>field[0])')),['owner','ownership','keeper','code_contact']);assert.equal(page.run('batchFields("cards").every(field=>field[3]==="card_manage")'),true);assert.equal(page.requests.length,0);}
 });
 await test('common and advanced controls remain independent, visible as chips and counted accurately',()=>{
  const page=harness(),before=page.run('JSON.stringify(data)');page.element('cardOfficeFilter').value='three';page.element('cardCarrier').value='中国移动';page.element('cardAssessment').value='ready';page.element('cardCodeFilter').value='待接码';page.run('render()');assert.deepEqual(ids(page),[5]);assert.match(page.element('cardActiveFilters').innerHTML,/data-card-filter-clear="cardCarrier"/);assert.match(page.element('cardActiveFilters').innerHTML,/data-card-filter-clear="cardAssessment"/);assert.match(page.element('cardAdvancedCount').textContent,/2 项已生效/);
  page.click('[data-card-filter-clear]',{cardFilterClear:'cardCodeFilter'});assert.deepEqual(ids(page),[5,8]);assert.equal(page.element('cardCarrier').value,'中国移动');assert.equal(page.element('cardOfficeFilter').value,'three');assert.match(page.element('cardAdvancedCount').textContent,/1 项已生效/);assert.equal(page.run('JSON.stringify(data)'),before);
 });
 await test('reset keeps the office, removes hidden legacy constraints and clears the advanced count',()=>{
  const page=harness();page.element('cardOfficeFilter').value='three';page.element('cardTask').value='blocked';page.element('cardAssessment').value='ready';page.element('cardQuery').value='unmatched-synthetic';page.run('render()');assert.equal(ids(page).length,0);assert.match(page.element('cardActiveFilters').innerHTML,/data-card-filter-clear="cardTask"/);page.run('CardFilters.reset(true)');assert.equal(page.element('cardOfficeFilter').value,'three');assert.equal(page.element('cardTask').value,'');assert.equal(page.element('cardAdvancedCount').textContent,'');assert.equal(ids(page).length,8);
 });
 await test('compact rows retain 50-record pagination and no view operation changes business rows or sends writes',()=>{
  const value=board(121),page=harness(value),before=page.run('JSON.stringify(data)');page.run('cardPageIndex=3;render()');assert.equal(rowIds(page).length,21);assert.match(page.element('cardsPage').textContent,/第 3 \/ 3 页/);page.run('cardPageIndex=1;render();CardFilters.reset(true);render()');assert.equal(page.run('JSON.stringify(data)'),before);assert.equal(page.requests.length,0);assert.deepEqual(value,board(121));
 });
 await test('HTML has unique dimensions, explicit load state and independent common versus advanced controls',()=>{
  const html=read('static/index.html'),all=[...html.matchAll(/\bid="([^"]+)"/g)].map(match=>match[1]);assert.equal(new Set(all).size,all.length);
  const common=html.slice(html.indexOf('class="card-common-filters"'),html.indexOf('<details class="card-advanced"'));for(const id of ['cardOfficeFilter','cardQuery','cardStatus','cardCarrier'])assert.match(common,new RegExp('id="'+id+'"'));
  const advanced=html.match(/<details\b[^>]*id="cardAdvancedFilters"[^>]*>([\s\S]*?)<\/details>/)[1];for(const id of ['cardAssessment','cardCodeFilter','cardReturnFilter','cardPositionFilter'])assert.match(advanced,new RegExp('id="'+id+'"'));
  for(const id of ['cardReadState','cardReadMessage','cardReadRetry','cardAdvancedCount'])assert.ok(all.includes(id));assert.match(html,/<select id="cardTask"[^>]*hidden/);assert.match(html,/data-batch-all="cards" data-batch-page="yes"/);
 });
 await test('production source package and HTML include every new presentation asset',()=>{
  const source=read('server_package.py'),listed=source.match(/STATIC_FILES = '''([\s\S]*?)'''\.split\(\)/)[1].trim().split(/\s+/),html=read('static/index.html');
  for(const name of ['card-page.js','card-details.js','card-console.css']){assert.ok(listed.includes(name),'package includes '+name);assert.ok(fs.existsSync(path.join(root,'static',name)),name+' exists');assert.ok(html.includes('/'+name+'?v='),'versioned link for '+name);}
  assert.ok(html.indexOf('/card-page.js?')<html.indexOf('/app.js?'));assert.ok(html.indexOf('/card-filters.js?')<html.indexOf('/phone-cards.js?'));
 });
 console.log(JSON.stringify({passed:failed===0,checks,failed,kind:'real-card-presentation-filter-batch-load-with-synthetic-dom-fetch-only'}));if(failed)process.exitCode=1;
})().catch(error=>{console.error(error);process.exitCode=1;});
