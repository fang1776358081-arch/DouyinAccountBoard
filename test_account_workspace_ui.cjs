// Actual frontend logic with synthetic DOM/fetch; no browser, database or network.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {harness,fixture}=require('./test_performance_ui.cjs');
const read=name=>fs.readFileSync(path.join(__dirname,'static',name),'utf8');
const helpers=read('account-workspace.js'),html=read('index.html');
let checks=0;
const check=(name,run)=>{run();checks++;console.log('ok '+name);};
function page(board=fixture()){
 const p=harness();p.run(helpers);p.show(board);return p;
}
const p=page();
check('common and advanced controls retain unique IDs and native disclosure',()=>{
 const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(match=>match[1]);assert.equal(new Set(ids).size,ids.length);
 const advanced=html.match(/<details id="accountAdvancedFilters"[\s\S]*?<\/details>/)?.[0];assert.ok(advanced);assert.doesNotMatch(advanced,/<details[^>]*\bopen\b/);
 for(const id of ['accountDeviceQuery','accountPhoneQuery','verificationFilter','loginPhoneFilter','accountType','ageFilter','lifecycleFilter'])assert.ok(advanced.includes('id="'+id+'"'),id);
 for(const id of ['query','accountDouyinQuery','computer','status','published'])assert.ok(html.indexOf('id="'+id+'"')<html.indexOf('id="accountAdvancedFilters"'),id);
});
check('result count covers all filtered accounts while visible rows remain paginated',()=>{
 assert.match(p.element('accountActiveFilters').innerHTML,/筛选结果 <strong>125<\/strong>/);assert.equal(p.rowIds().length,50);
 p.element('accountsNext').onclick();assert.match(p.element('accountActiveFilters').innerHTML,/125/);assert.equal(p.rowIds()[0],'account-50');
});
check('hidden account ID remains visible as a removable condition and never broadens office scope',()=>{
 const b=fixture(3);b.accounts[2].office_ids=['two'];const q=page(b);
 q.element('accountIdQuery').value='account-0';q.run('render()');assert.equal(q.run('filtered().length'),1);assert.match(q.element('accountActiveFilters').innerHTML,/账号编号：account-0/);
 q.click('[data-clear-account-filter]',{clearAccountFilter:'accountIdQuery'});assert.equal(q.element('accountIdQuery').value,'');assert.equal(q.run('filtered().length'),2);assert.equal(q.run('activeOffice'),'one');assert.equal(q.calls.length,0);
});
check('an advanced condition can be removed while unrelated search remains active',()=>{
 p.reset(fixture());p.element('query').value='0000';p.element('accountDeviceQuery').value='1';p.run('render()');assert.equal(p.run('filtered().length'),3);
 assert.match(p.element('accountActiveFilters').innerHTML,/昵称：0000/);assert.match(p.element('accountActiveFilters').innerHTML,/登录手机序号：1/);assert.match(p.element('accountAdvancedCount').textContent,/1/);
 p.click('[data-clear-account-filter]',{clearAccountFilter:'accountDeviceQuery'});assert.equal(p.element('query').value,'0000');assert.equal(p.run('filtered().length'),10);assert.equal(p.element('accountAdvancedCount').textContent,'');
});
check('exact handle conditions escape markup and clearing chips uses the original predicate',()=>{
 p.element('accountDouyinQuery').value='synthetic-handle-4';p.run('render()');assert.equal(p.run('filtered()[0].id'),'account-4');assert.equal(p.run('filtered().length'),1);
 p.element('accountDouyinQuery').value='<img src=x onerror=bad()>';p.run('render()');assert.match(p.element('accountActiveFilters').innerHTML,/&lt;img/);assert.doesNotMatch(p.element('accountActiveFilters').innerHTML,/<img/);
 p.click('[data-clear-account-filter]',{clearAccountFilter:'accountDouyinQuery'});assert.equal(p.run('filtered().length'),10);
});
check('quick lifecycle conditions remain visible and removable with linked fields',()=>{
 p.run("activeLifecyclePreset='missing';$('accountType').value='大号';render()");assert.match(p.element('accountActiveFilters').innerHTML,/注册进度快捷筛选：待补日期/);
 p.click('[data-clear-account-filter]',{clearAccountFilter:'lifecyclePreset'});assert.equal(p.run('activeLifecyclePreset'),'');assert.equal(p.element('accountType').value,'');assert.equal(p.run('filtered().length'),10);
});
check('whole reset clears ordinary, hidden and verification conditions through existing handler',()=>{
 p.element('accountIdQuery').value='account-1';p.element('verificationFilter').value='待检查';p.element('accountPhoneQuery').value='synthetic-phone-0';p.run('render()');
 p.element('clear').onclick();assert.equal(p.element('accountIdQuery').value,'');assert.equal(p.element('verificationFilter').value,'');assert.equal(p.element('accountPhoneQuery').value,'');assert.equal(p.element('query').value,'');assert.equal(p.run('filtered().length'),125);
});
check('compact rows retain registration, verification, collection and full note details',()=>{
 const b=fixture(2);b.accounts[0].note='完整合成备注\n第二行 <unsafe>';b.accounts[0].collection_error='合成采集错误';const q=page(b),rows=q.element('accountsBody').innerHTML;
 assert.match(rows,/<details class="account-row-details"><summary>查看资料<\/summary>/);for(const text of ['账号类型 / 注册进度','稳定与改密','账号核对','公开作品数量 / 采集记录','完整备注','完整合成备注\n第二行 &lt;unsafe&gt;','合成采集错误','登录手机：','实际发布：','登记电脑：'])assert.ok(rows.includes(text),text);
 assert.match(rows,/data-publish="account-0"/);assert.match(rows,/data-collect="account-0"/);assert.match(rows,/workspace.html\?account=account-0/);
 const header=html.match(/<table class="account-table"><thead><tr>([\s\S]*?)<\/tr><\/thead>/)[1];assert.equal((header.match(/<th[>\s]/g)||[]).length,7);
});
check('one visible account batch bar preserves legacy hooks, page/all scope and collection entry',()=>{
 assert.match(html,/<div class="bulk legacy-account-bulk" hidden aria-hidden="true">/);assert.match(html,/<div class="batch-toolbar account-batch-toolbar">[\s\S]*?id="collectSelected"[\s\S]*?<\/div>/);
 assert.equal((html.match(/id="collectSelected"/g)||[]).length,1);for(const id of ['selectedCount','bulkStatus','applyBulk'])assert.ok(html.includes('id="'+id+'"'));
 p.element('accountsNext').onclick();p.click('[data-batch-all]',{batchAll:'accounts',batchPage:'yes'});assert.equal(p.run('selected.size'),50);assert.match(p.element('batchAccountsBar').innerHTML,/已选 50 条账号 · 本页 50 条 \/ 筛选共 125 条/);
 p.click('[data-batch-all]',{batchAll:'accounts'});assert.equal(p.run('selected.size'),125);p.click('[data-batch-clear]',{batchClear:'accounts'});assert.equal(p.run('selected.size'),0);
});
check('read-only permissions keep batch write actions disabled and rendering creates no writes',()=>{
 const b=fixture(3),snapshot=JSON.stringify(b),q=page(b);q.context.hasPermission=()=>false;q.run("selected.add('account-0');render()");
 const buttons=q.element('batchAccountsBar').innerHTML.match(/<button[^>]*data-batch-open[^>]*>/g)||[];assert.equal(buttons.length,2);assert.ok(buttons.every(button=>/\bdisabled\b/.test(button)));assert.equal(JSON.stringify(b),snapshot);assert.equal(q.calls.length,0);
});
console.log(JSON.stringify({passed:true,checks,kind:'actual-account-ui-synthetic-dom-no-business-writes'}));
