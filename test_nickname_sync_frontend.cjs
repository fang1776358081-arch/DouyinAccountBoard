// Actual application scripts with self-generated fixtures and DOM/Fetch stubs.
// No browser, external network, database, business files or new dependencies.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const {harness:baseHarness,fixture:baseFixture,root}=require('./test_performance_ui.cjs');
const clone=value=>structuredClone(value),plain=value=>JSON.parse(JSON.stringify(value));
const OLD_NAME='昵称待补充（手机701）',NEW_NAME='合成正式昵称·甲 & <乙> “丙”';

function fixture(name=OLD_NAME){
 const board=baseFixture(4),device=board.phone_devices.devices[0],oldId=device.id;
 device.id='synthetic-device-701';device.number='701';device.name='合成办公室三 · 701号手机';
 for(const account of board.accounts){account.office_ids=['three'];if(account.phone_device_id===oldId){account.phone_device_id=device.id;account.phone_device_name=device.name;}}
 for(const row of [...board.computers,...board.phone_devices.devices])row.office_ids=['three'];
 for(const cell of board.publishing_layout.cells)cell.office='three';
 board.accounts[0].name=name;board.accounts[0].douyin_id='synthetic-raw-000701';
 for(const row of board.phone_devices.devices){row.account_ids=board.accounts.filter(account=>account.phone_device_id===row.id).map(account=>account.id);row.unit_history=[];row.occupancy='合成占用状态';row.suggested_state='在用';}
 board.device_workflow={enabled:false,units:[],access:[],placements:[],events:[]};
 return board;
}

// Model only the reviewed compact wire shape. Real compact() and gzip are
// covered by the Python transport tests; this checks actual JS consumers.
function compactWire(full){
 const wire=clone(full);
 for(const row of wire.phone_devices.devices)for(const key of ['account_ids','unit_history','occupancy','suggested_state'])delete row[key];
 delete wire.device_workflow.access;delete wire.device_workflow.placements;
 return plain(wire);
}
function harness(){
 const page=baseHarness(root);
 vm.runInContext(fs.readFileSync(path.join(root,'static/board-updates.js'),'utf8'),page.context,{filename:'board-updates.js'});
 page.run('page="accounts";activeOffice="three";');
 page.context.location.search='?page=accounts&office=three';
 return page;
}
const response=body=>({ok:true,status:200,json:async()=>plain(body)});
function cached(page){return JSON.parse(page.context.localStorage.getItem('douyin-board-view')||'null');}
function writeCache(page,board){page.context.localStorage.setItem('douyin-board-view',JSON.stringify({userId:board.user.id,board}));}
function transport(page,read){page.context.fetch=async(url,options)=>{page.calls.push(url);return read(url,options);};}
function namedMarkup(page,name){page.context.syntheticName=name;return '<strong>'+page.run('esc(syntheticName)')+'</strong>';}

async function main(){
 let checks=0;const check=(name,work)=>{work();checks++;console.log('ok '+name);};
 const full=fixture(),wire=compactWire(full),original=JSON.stringify(full),changed=compactWire(fixture(NEW_NAME));
 const change={entity:'account:account-0',kind:'update',after:clone(changed.accounts[0]),record_version:'synthetic-name-v1'};
 check('compact JSON fixture retains every account field, raw identifier string and Unicode name',()=>{
  assert.deepEqual(wire.accounts,full.accounts);assert.equal(wire.accounts[0].douyin_id,'synthetic-raw-000701');
  assert.equal(JSON.parse(JSON.stringify(changed)).accounts[0].name,NEW_NAME);assert.equal(JSON.stringify(full),original);
 });
 const cold=harness();transport(cold,url=>{assert.ok(url.startsWith('/api/board?'));assert.equal(new URLSearchParams(url.split('?')[1]).get('compact'),'accounts');return response(changed);});
 await cold.run('load()');
 check('authoritative accounts response renders the name field and keeps compact opt-in',()=>{
  assert.equal(cold.run('data.accounts[0].name'),NEW_NAME);assert.ok(cold.element('accountsBody').innerHTML.includes(namedMarkup(cold,NEW_NAME)));
  assert.equal(cold.calls.length,1);assert.equal(cached(cold).board.accounts[0].name,NEW_NAME);
 });
 const page=harness();page.reset(clone(wire),'accounts');page.run('activeOffice="three";');page.show(clone(wire));
 check('placeholder is rendered only because it is the supplied account name',()=>{
  assert.ok(page.element('accountsBody').innerHTML.includes(namedMarkup(page,OLD_NAME)));
  assert.equal(cached(page).board.accounts[0].name,OLD_NAME);
 });
 transport(page,url=>{assert.match(url,/^\/api\/board-updates\?cursor=(10|11)$/);return response({cursor:11,changes:[change]});});
 await page.run('catchUpBoard()');
 check('account history delta replaces the placeholder in state and escaped rendered content',()=>{
  assert.equal(page.run('data.accounts[0].name'),NEW_NAME);assert.ok(page.element('accountsBody').innerHTML.includes(namedMarkup(page,NEW_NAME)));
  assert.ok(!page.element('accountsBody').innerHTML.includes(namedMarkup(page,OLD_NAME)));
  assert.ok(!page.element('accountsBody').innerHTML.includes('<乙>'));
 });
 check('name delta writes the current user cache and advances only its account version and cursor',()=>{
  const cache=cached(page);assert.equal(cache.userId,wire.user.id);assert.equal(cache.board.accounts[0].name,NEW_NAME);
  assert.equal(cache.board.cursor,11);assert.equal(cache.board.record_versions['account:account-0'],'synthetic-name-v1');
  assert.equal(cache.board.record_versions['daily:account-0:'+wire.date],wire.record_versions['daily:account-0:'+wire.date]);
 });
 check('raw identifier, computer, slot, phone and office remain unchanged',()=>{
  const account=plain(page.run('data.accounts[0]'));
  for(const key of ['id','douyin_id','computer_id','slot','phone_device_id','phone_device_name','office_ids'])assert.deepEqual(account[key],wire.accounts[0][key],key);
  assert.deepEqual(plain(page.run('data.computers')),wire.computers);assert.equal(page.run('data.accounts.length'),4);
 });
 check('name, office, computer and phone filtering retain the repaired name without another read',()=>{
  page.element('query').value=NEW_NAME;page.run('render()');assert.deepEqual(page.rowIds(),['account-0']);
  page.run('activeOffice="two";render()');assert.deepEqual(page.rowIds(),[]);
  page.run('activeOffice="three";render()');page.element('computer').value=wire.accounts[0].computer_id;
  page.element('loginPhoneFilter').value='device:'+wire.accounts[0].phone_device_id;page.run('render()');
  assert.deepEqual(page.rowIds(),['account-0']);assert.ok(page.element('accountsBody').innerHTML.includes(namedMarkup(page,NEW_NAME)));
  assert.deepEqual(page.calls,['/api/board-updates?cursor=10']);
 });
 await page.run('catchUpBoard()');
 check('replayed account delta does not append a duplicate or regress the name',()=>{
  assert.equal(page.run('data.accounts.length'),4);assert.equal(page.run('data.accounts[0].name'),NEW_NAME);
  assert.equal(cached(page).board.accounts[0].name,NEW_NAME);assert.deepEqual(page.calls,['/api/board-updates?cursor=10','/api/board-updates?cursor=11']);
 });
 const warmOld=harness();writeCache(warmOld,wire);let releaseAuth;
 const authWait=new Promise(resolve=>{releaseAuth=resolve;});
 transport(warmOld,url=>url==='/api/auth/me'?authWait:response({cursor:11,changes:[change]}));
 const opening=warmOld.run('openBoard()');
 check('warm cached placeholder is not painted before current-user authentication',()=>{
  assert.equal(warmOld.run('data'),null);assert.deepEqual(warmOld.calls,['/api/auth/me']);
 });
 releaseAuth(response({user:{id:wire.user.id}}));await opening;
 check('same-user warm startup repairs old cached name through a bounded delta',()=>{
  assert.equal(warmOld.run('data.accounts[0].name'),NEW_NAME);assert.equal(cached(warmOld).board.accounts[0].name,NEW_NAME);
  assert.ok(warmOld.element('accountsBody').innerHTML.includes(namedMarkup(warmOld,NEW_NAME)));
  assert.deepEqual(warmOld.calls,['/api/auth/me','/api/board-updates?cursor=10']);
 });
 const warmNew=harness();writeCache(warmNew,cached(warmOld).board);
 transport(warmNew,url=>response(url==='/api/auth/me'?{user:{id:wire.user.id}}:{cursor:11,changes:[]}));
 await warmNew.run('openBoard()');
 check('another same-user refresh preserves the real name and associations with no full reload',()=>{
  const account=plain(warmNew.run('data.accounts[0]'));
  assert.equal(account.name,NEW_NAME);assert.equal(account.douyin_id,'synthetic-raw-000701');assert.deepEqual(account.office_ids,['three']);
  assert.equal(account.phone_device_id,wire.accounts[0].phone_device_id);assert.equal(account.computer_id,wire.accounts[0].computer_id);
  assert.deepEqual(warmNew.calls,['/api/auth/me','/api/board-updates?cursor=11']);assert.equal(cached(warmNew).board.accounts[0].name,NEW_NAME);
 });
 console.log(JSON.stringify({passed:true,checks,environment:'synthetic actual JavaScript with DOM, Fetch and storage stubs; no browser, network or database'}));
}
if(require.main===module)main().catch(error=>{console.error(error);process.exitCode=1;});
