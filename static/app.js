const $=id=>document.getElementById(id);
function renderBackup(){$('saveState').textContent=data.user?.scope_mode?'数据保存在共享服务':'数据保存在本机';const b=data.backup||{};$('backupState').textContent=b.enabled?(b.last_backup?'自动备份：'+new Date(b.last_backup).toLocaleString('zh-CN',{hour12:false})+' · 每4小时 / 保留30天':'自动备份：等待首次成功'):'自动备份未启动';$('backupState').title=b.directory||'';$('backupError').hidden=!b.error;$('backupError').textContent=b.error||'';}
let data=null,page=['board','accounts','cards','phones'].includes(new URLSearchParams(location.search).get('page'))?new URLSearchParams(location.search).get('page'):'board',selected=new Set(),requestSerial=0;
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const abnormal=a=>['短期封禁','封禁','发不了','停用'].includes(a.status);
const badge=a=>`<span class="badge ${a.status==='正常'?'good':abnormal(a)?'warn':''}">${esc(a.status)}</span>`+(a.device_access?.login_state==='登录受阻'?`<button type="button" class="text-button" data-workflow-account="${esc(a.id)}">登录受阻 · 查看恢复</button>`:'');
function toast(message){$('toast').textContent=message;$('toast').classList.add('show');clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').classList.remove('show'),2300);}
const pendingRequestIds=new Map();
function newRequestId(){if(typeof crypto.randomUUID==='function')return crypto.randomUUID();const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);bytes[6]=bytes[6]&15|64;bytes[8]=bytes[8]&63|128;let hex='';for(let i=0;i<16;i++)hex+=bytes[i].toString(16).padStart(2,'0');return hex.slice(0,8)+'-'+hex.slice(8,12)+'-'+hex.slice(12,16)+'-'+hex.slice(16,20)+'-'+hex.slice(20);}
function claimRequestId(key){if(!pendingRequestIds.has(key))pendingRequestIds.set(key,newRequestId());return pendingRequestIds.get(key);}
function settleRequestId(key,error){if(error&&error.status===undefined)return;pendingRequestIds.delete(key);}
async function sendApi(path,body,options={}){if(body&&data&&options.fixedPayload!==true)body=versionPayload(path,body,data,activeEditVersions());const response=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-CSRF-Token':currentUser?.csrf||data?.user?.csrf||''}:{},body:body?JSON.stringify(body):undefined});const result=await response.json();if(response.status===401){clearBoardCache();location.replace(location.search==='?office=three'?'/login.html?next='+encodeURIComponent('/?office=three'):'/login.html');const failure=Error('登录已失效，请重新登录');failure.status=401;throw failure;}if(!response.ok){const failure=Error(result.error||'保存失败');failure.status=response.status;failure.code=result.code;throw failure;}if(data&&options.applyVersions!==false)applyVersionDelta(result._record_versions,data);return result;}
async function api(path,body,options){try{return await sendApi(path,body,options);}catch(error){if(error.status!==undefined||!body?.request_id)throw error;try{return await sendApi('/api/idempotency/result?request_id='+encodeURIComponent(body.request_id),undefined,options);}catch(queryError){if(queryError.status===undefined||queryError.code==='REQUEST_PENDING'||queryError.status===404){const pending=Error('结果待确认');throw pending;}throw queryError;}}}

// Preserve an uncertain write's complete payload, including original versions.
// A later click resolves it before submitting the opposite published flag.
const dailyRequests=new Map();
let dailyViewGeneration=0,dailyViewLoading=0;
function dailyKey(id,day){return 'daily:'+id+':'+day;}
function syncDailyButtons(){if(!data)return;const accounts=new Map(data.accounts.map(account=>[String(account.id),account]));for(const button of document.querySelectorAll('[data-publish]')){const entry=dailyRequests.get(dailyKey(button.dataset.publish,data.date)),busy=!!entry?.running;if(busy)button.disabled=true;button.setAttribute?.('aria-busy',String(busy));button.textContent=busy?'保存中…':entry?'待确认 · 重试':accounts.get(String(button.dataset.publish))?.published?'✓ 已发':'○ 未发';button.title=entry?'正在保存或核对这条发布记录；结果未确认时，重试仍核对原操作。':'';}}
function dailyVersion(key){return data?.record_versions?.[key]??null;}
function mergeDailyVersions(versions,before){if(!data)return;const accepted={};for(const [key,value] of Object.entries(versions||{}))if(dailyVersion(key)===(before[key]??null)||dailyVersion(key)===value)accepted[key]=value;data.record_versions={...data.record_versions,...accepted};}
function rememberDaily(id,day,published){if(!data||data.date!==day||$('date').value!==day)return;const row=data.accounts.find(account=>String(account.id)===String(id));if(row){row.published=published;globalThis.requestSerial=++requestSerial;}}
async function readDailyState(id,day,retries=1){
 if(dailyViewLoading)throw Error('页面正在刷新，保存结果待确认，请稍后点击原记录重试核对');
 const keys=['account:'+id,dailyKey(id,day)],before=Object.fromEntries(keys.map(key=>[key,dailyVersion(key)])),cursor=data?.cursor,view=dailyViewGeneration,opened=data;
 // A full board read supplies the flag and versions from one read transaction.
 // It is a recovery path, not a second read on every successful daily write.
 const board=await api('/api/board?date='+encodeURIComponent(day)+'&version_scope=day'+(page==='accounts'?'&compact=accounts':''),undefined,{applyVersions:false});
 const row=board.accounts?.find(account=>String(account.id)===String(id));
 if(board.date!==day||!row||!board.record_versions||![true,false,0,1].includes(row.published))throw Error('发布记录尚未核对成功，请刷新后重试');
 const versions=Object.fromEntries(keys.map(key=>[key,board.record_versions[key]??null])),key=dailyKey(id,day);
 const viewChanged=view!==dailyViewGeneration||opened!==data,stale=viewChanged||data?.cursor!==cursor||dailyVersion(key)!==before[key]&&dailyVersion(key)!==versions[key];
 if(stale){
  if(data?.date!==day||$('date').value!==day)return {id,date:day,published:!!row.published,stale:true};
  // A newer page/date load owns the view. Do not start another recovery read
  // whose completion could invalidate that load before its response arrives.
  if(viewChanged)throw Error('页面正在刷新，保存结果待确认，请稍后点击原记录重试核对');
  if(retries>0)return readDailyState(id,day,retries-1);
  throw Error('记录仍在更新，保存结果待确认，请点击原记录重试核对');
 }
 // Account fields were not refreshed: never attach a foreign account token to
 // their old contents or advance an open editor's captured account version.
 mergeDailyVersions({[key]:versions[key]},before);rememberDaily(id,day,!!row.published);writeBoardCache();
 return {id,date:day,published:!!row.published};
}
async function confirmDailyResult(entry,result){
 const daily=result?.daily,key=dailyKey(entry.id,entry.day),incoming=result?._record_versions;
 if(entry.view!==dailyViewGeneration||entry.opened!==data){if(data?.date!==entry.day||$('date').value!==entry.day)return {id:entry.id,date:entry.day,stale:true};throw Error('页面正在刷新，保存结果待确认，请稍后点击原记录重试核对');}
 if(entry.replayed||result?.code==='REQUEST_SUCCEEDED'||!daily||String(daily.id)!==entry.id||daily.date!==entry.day||typeof daily.published!=='boolean'||daily.published!==entry.published||!incoming||!Object.prototype.hasOwnProperty.call(incoming,key)||entry.cursor!==data?.cursor)return readDailyState(entry.id,entry.day);
 const current=dailyVersion(key),original=entry.payload._expected_versions[key]??null;
 if(current!==original&&current!==incoming[key])return readDailyState(entry.id,entry.day);
 mergeDailyVersions({[key]:incoming[key]},entry.payload._expected_versions);
 rememberDaily(entry.id,entry.day,daily.published);writeBoardCache();
 return daily;
}
async function retryDailyResult(entry){
 try{return await api('/api/idempotency/result?request_id='+encodeURIComponent(entry.payload.request_id),undefined,{applyVersions:false});}
 catch(error){if(error.status===404||error.code==='REQUEST_PENDING')return api('/api/daily',entry.payload,{applyVersions:false,fixedPayload:true});throw error;}
}
async function toggleDaily(id){
 if(!data)return;id=String(id);const day=data.date;
 if(dailyViewLoading){toast('页面正在刷新，请稍后重试');return;}
 if($('date').value!==day){toast('日期切换中，请稍后重试');return;}
 const account=data.accounts.find(row=>String(row.id)===id);if(!account)return;
 const key=dailyKey(id,day),previous=dailyRequests.get(key);if(previous?.running){syncDailyButtons();return previous.running;}
 const entry=previous||{id,day,published:!account.published,seen:!!account.published,payload:null,sent:false};
 dailyRequests.set(key,entry);
 entry.running=(async()=>{
  try{
   if(!entry.payload){
    if(!Object.prototype.hasOwnProperty.call(data.record_versions||{},'account:'+id)){const missing=Error('账号资料版本尚未就绪，请刷新页面后重试');missing.status=400;throw missing;}
    const body=versionPayload('/api/daily',{id,date:day,published:entry.published,request_id:claimRequestId(key)},data,data.record_versions);
    entry.payload=Object.freeze({...body,_expected_versions:Object.freeze({...body._expected_versions})});entry.view=dailyViewGeneration;entry.opened=data;entry.cursor=data?.cursor;
   }
   let result;
   if(entry.sent){entry.replayed=true;entry.view=dailyViewGeneration;entry.opened=data;entry.cursor=data?.cursor;result=await retryDailyResult(entry);}
   else{entry.sent=true;result=await api('/api/daily',entry.payload,{applyVersions:false,fixedPayload:true});}
   const actual=await confirmDailyResult(entry,result);
   dailyRequests.delete(key);settleRequestId(key);
   const message=actual.stale?'记录已有更新，请查看当前状态':actual.published===entry.published?(actual.published?'已记录为已发':'已取消发布标记'):'已核对当前记录：'+(actual.published?'已发':'未发');
   toast(message+(data?.date!==day?'（'+day+'）':''));
  }catch(error){
   if(!entry.sent||error.status!==undefined){dailyRequests.delete(key);pendingRequestIds.delete(key);}
   if(error.status===409&&entry.view===dailyViewGeneration&&entry.opened===data){try{await readDailyState(id,day);}catch(readError){}}
   toast(error.status===undefined&&entry.sent?'保存结果待确认，请点击原记录重试核对':error.message||'保存失败，请重试');
  }finally{entry.running=null;render();syncDailyButtons();}
 })();
 syncDailyButtons();return entry.running;
}
async function load(allowWorkflowReload=false){if(window.DeviceWorkflow?.isOpen()&&allowWorkflowReload!==true)return;const view=++dailyViewGeneration;dailyViewLoading=view;const serial=++requestSerial;globalThis.requestSerial=requestSerial;try{const next=await api('/api/board?version_scope=day'+($('date').value?'&date='+encodeURIComponent($('date').value):'')+(page==='accounts'?'&compact=accounts':''));if(serial!==requestSerial||window.DeviceWorkflow?.isOpen()&&allowWorkflowReload!==true)return;showBoard(next);}catch(e){$('loadError').textContent='加载失败：'+e.message;$('loadError').hidden=false;}finally{if(dailyViewLoading===view)dailyViewLoading=0;}}
function boardStorage(){if(typeof localStorage==='undefined')return null;return localStorage;}
function readBoardCache(){const storage=boardStorage();if(!storage)return null;try{const parsed=JSON.parse(storage.getItem('douyin-board-view')||'null');if(!parsed||typeof parsed!=='object'||parsed.userId==null||!parsed.board||!Number.isInteger(parsed.board.cursor)||parsed.board.user?.id==null||!parsed.board.device_workflow||typeof parsed.board.device_workflow!=='object')return null;if(String(parsed.board.user.id)!==String(parsed.userId))return null;return parsed;}catch(error){return null;}}
function writeBoardCache(){const storage=boardStorage();if(!storage||!data||data.user?.id==null||!Number.isInteger(data.cursor))return;try{storage.setItem('douyin-board-view',JSON.stringify({userId:data.user.id,board:data}));}catch(error){}}
function clearBoardCache(){const storage=boardStorage();if(!storage)return;try{storage.removeItem('douyin-board-view');}catch(error){}}
function showBoard(next){data=next;globalThis.data=data;renderVerificationFilter();$('date').value=data.date;$('loadError').hidden=true;const statusValue=$('status').value;$('status').innerHTML='<option value="">全部账号状态</option>'+data.statuses.map(s=>`<option>${esc(s)}</option>`).join('');$('status').value=statusValue;const bulkValue=$('bulkStatus').value;$('bulkStatus').innerHTML=data.statuses.map(s=>`<option>${esc(s)}</option>`).join('');if(bulkValue)$('bulkStatus').value=bulkValue;render();if(typeof CustomEvent!=='undefined')document.dispatchEvent(new CustomEvent('board-ready'));writeBoardCache();}
async function loadPublished(day){const view=++dailyViewGeneration;dailyViewLoading=view;const serial=++requestSerial;globalThis.requestSerial=requestSerial;try{const result=await api('/api/daily-flags?date='+encodeURIComponent(day)+'&versions=1',undefined,{applyVersions:false});if(serial!==requestSerial||!data)return;data.date=result.date;$('date').value=result.date;const versions=Object.fromEntries(Object.entries(result.record_versions||{}).filter(([key])=>key.startsWith('daily:')));data.record_versions={...data.record_versions,...versions};const flags=result.published||{};for(const account of data.accounts)account.published=!!flags[account.id];render();writeBoardCache();}catch(e){$('loadError').textContent='加载失败：'+e.message;$('loadError').hidden=false;}finally{if(dailyViewLoading===view)dailyViewLoading=0;}}
async function openBoard(){const cached=readBoardCache();if(!cached){clearBoardCache();await load();return;}let me=null;try{const response=await fetch('/api/auth/me');if(response.status===401){clearBoardCache();location.replace(location.search==='?office=three'?'/login.html?next='+encodeURIComponent('/?office=three'):'/login.html');return;}if(response.ok)me=(await response.json()).user||null;}catch(error){}if(!me||me.id==null||String(cached.userId)!==String(me.id)){clearBoardCache();await load();return;}showBoard(cached.board);if(typeof catchUpBoard==='function')await catchUpBoard();else await load();}
function filtered(){return officeAccounts(pageAccounts()).filter(a=>phoneDeviceMatches(a)&&lifecyclePresetMatches(a)&&accountSearchMatches(a)&&verificationMatches(a)&&(!$('computer').value||a.computer_id===$('computer').value)&&(!$('status').value||a.status===$('status').value)&&(!$('accountType').value||a.account_type===$('accountType').value)&&(!$('ageFilter').value||(a.account_type==='大号'&&($('ageFilter').value==='missing'?!a.registered_on:a.registered_on&&($('ageFilter').value==='mature'?a.days_remaining===0:a.days_remaining>0))))&&(!$('lifecycleFilter').value||a.lifecycle===$('lifecycleFilter').value)&&(!$('published').value||(!!a.published===($('published').value==='yes'))));}
let activeRenderIndex=null,accountPageIndex=1,accountPageSignature='';
const accountPageSize=50;
function renderPositionKey(computer,slot){return JSON.stringify([typeof computer,computer,typeof slot,slot]);}
function appendRenderGroup(map,key,value){if(!map.has(key))map.set(key,[]);map.get(key).push(value);}
function createRenderIndex(){
 const index={accountsById:new Map(),computersById:new Map((data.computers||[]).map(row=>[row.id,row])),devicesById:new Map((data.phone_devices?.devices||[]).map(row=>[row.id,row])),unitsById:new Map((data.device_workflow?.units||[]).map(row=>[String(row.id),row])),accountsByPhone:new Map(),accountsByDevice:new Map(),accountsWithoutAccessByDevice:new Map(),accountsByUnit:new Map(),accountOrder:new Map(),accountsByPosition:new Map(),layoutsByAccount:new Map(),layoutsByPosition:new Map(),manualPositions:new Set()};
 for(const [order,account] of data.accounts.entries()){
  index.accountsById.set(account.id,account);index.accountOrder.set(account,order);
  appendRenderGroup(index.accountsByPhone,account.registration_phone,account);appendRenderGroup(index.accountsByDevice,account.phone_device_id,account);
  if(account.device_access){if(account.device_access.last_unit_id!=null)appendRenderGroup(index.accountsByUnit,String(account.device_access.last_unit_id),account);}else appendRenderGroup(index.accountsWithoutAccessByDevice,account.phone_device_id,account);
  const key=renderPositionKey(account.computer_id,account.slot);if(!index.accountsByPosition.has(key))index.accountsByPosition.set(key,account);
 }
 for(const cell of data.publishing_layout?.cells||[]){
  appendRenderGroup(index.layoutsByAccount,cell.account_id,cell);
  const key=renderPositionKey(cell.computer_id,cell.slot);if(!index.layoutsByPosition.has(key))index.layoutsByPosition.set(key,cell);
  if(cell.manual_link)index.manualPositions.add(key);
 }
 return index;
}
function accountPageRows(rows){return rows.slice((accountPageIndex-1)*accountPageSize,accountPageIndex*accountPageSize);}
function prepareAccountPage(accounts){
 const fields=[...accountSearchFields,'computer','status','published','accountType','ageFilter','lifecycleFilter','loginPhoneFilter','verificationFilter'];
 const signature=JSON.stringify([activeOffice,data.date,typeof activeLifecyclePreset==='undefined'?'':activeLifecyclePreset,...fields.map(id=>$(id)?.value||'')]);
 if(signature!==accountPageSignature){accountPageSignature=signature;accountPageIndex=1;}
 const pages=Math.max(1,Math.ceil(accounts.length/accountPageSize));accountPageIndex=Math.min(Math.max(1,accountPageIndex),pages);
 let controls=$('accountPagination');if(!controls){controls=document.createElement('div');controls.id='accountPagination';controls.className='card-pagination';$('accountsView').append(controls);}
 controls.innerHTML=`<button id="accountsPrevious" type="button" class="button">上一页账号</button><span>第 ${accountPageIndex} / ${pages} 页 · 共 ${accounts.length} 个账号 · 本页 ${accounts.length?(accountPageIndex-1)*accountPageSize+1:0}–${Math.min(accountPageIndex*accountPageSize,accounts.length)} 条</span><button id="accountsNext" type="button" class="button">下一页账号</button>`;
 $('accountsPrevious').disabled=accountPageIndex<=1;$('accountsNext').disabled=accountPageIndex>=pages;
 $('accountsPrevious').onclick=()=>{accountPageIndex--;render();};$('accountsNext').onclick=()=>{accountPageIndex++;render();};
 return accountPageRows(accounts);
}
function render(){if(!data)return;const previous=activeRenderIndex;activeRenderIndex=createRenderIndex();try{renderPageContent(activeRenderIndex);}finally{activeRenderIndex=previous;}}
function renderPageContent(index){document.body.classList.toggle('board-fit',page==='board');renderBackup();renderOffices();const cardsPage=page==='cards',phonesPage=page==='phones';$('cardsWorkspace').hidden=!cardsPage;$('phonesWorkspace').hidden=!phonesPage;$('accountWorkspace').hidden=cardsPage||phonesPage;$('collectionBar').hidden=cardsPage||phonesPage;document.querySelector('.header-actions').hidden=cardsPage||phonesPage;if(cardsPage||phonesPage){$('matrixHead').innerHTML='';$('matrixBody').innerHTML='';$('accountsBody').innerHTML='';$('lifecycleSummary').hidden=true;phonesPage?renderPhones():renderCards();applyPermissions();return;}renderComputerFilter();renderPhoneFilter();lifecycleSummary();const officeScope=officeAccounts();const accounts=filtered().filter(a=>page!=='board'||a.computer_id!=='unassigned'),visibleIds=new Set(accounts.map(a=>a.id));for(const id of selected)if(!visibleIds.has(id))selected.delete(id);if(typeof renderAccountFilterSummary==='function')renderAccountFilterSummary(accounts);const done=accounts.filter(a=>a.published).length;const layoutCells=page==='board'?filteredPublishingCells(accounts):[],layoutActive=page==='board'&&publishingEnabled();const computerFilter=!!$('computer').value,hostIds=new Set((layoutActive?layoutCells:accounts).map(row=>row.computer_id));const hosts=data.computers.filter(c=>c.id!=='unassigned'&&(!computerFilter||c.id===$('computer').value)&&hostIds.has(c.id));
 $('stats').innerHTML=[['负责电脑',hosts.length,'本次筛选中的主机','▣'],['账号总数',accounts.length,layoutActive?'已匹配到发布格子的账号':'已导入的账号资料','◎'],['当天已发',done,accounts.length?Math.round(done/accounts.length*100)+'% 完成':'暂无账号','✓'],['当天未发',accounts.length-done,'待人工勾选记录','◷']].map((s,i)=>`<div class="stat ${i===2?'highlight':''}"><span class="stat-symbol">${s[3]}</span><div class="stat-label">${s[0]}</div><strong>${s[1]}</strong><small>${s[2]}</small></div>`).join('');
 $('autoCollect').checked=!!data.collection.auto_enabled;$('resumeCollect').hidden=!data.collection.paused&&data.collection.worker_status!=='等待手动登录或验证';$('collectionInfo').textContent=(data.collection.worker_online?'采集器在线 · '+data.collection.worker_status+' · ':data.collection.worker_status+' · ')+(data.collection.paused?'采集已暂停：'+data.collection.pause_reason:`${data.collection.linked} / ${data.accounts.length} 个账号已填写抖音号或主页 · ${data.collection.waiting} 个任务等待或正在采集 · ${data.collection.done} 个成功 / ${data.collection.failed} 个失败（全库共用）`);$('scope').textContent=`${officeName()} · ${data.date===data.today?'今天':data.date} · ${hosts.length} 台电脑 / ${accounts.length} 个账号${layoutActive?' · 工作进度表布局 / '+layoutCells.filter(c=>c.label&&!c.account_id).length+' 个格子待匹配':''}`;
 if(page==='board'){
 const displayedHosts=new Set(hosts.map(host=>host.id)),byHost=new Map(),visiblePositions=new Map(),officePositions=new Set(officeScope.map(account=>renderPositionKey(account.computer_id,account.slot))),visibleLayoutCells=new Set(layoutCells);
 for(const account of accounts){appendRenderGroup(byHost,account.computer_id,account);const key=renderPositionKey(account.computer_id,account.slot);if(!visiblePositions.has(key))visiblePositions.set(key,account);}
 const slots=layoutActive?data.publishing_layout.cells.reduce((max,cell)=>displayedHosts.has(cell.computer_id)?Math.max(max,cell.slot):max,3):data.accounts.reduce((max,account)=>Math.max(max,account.slot),3);$('matrixHead').innerHTML='<tr><th>电脑 / 主机</th>'+Array.from({length:slots},(_,i)=>`<th>${layoutActive?'发布设备':'账号位置'} ${i+1}</th>`).join('')+'</tr>';
 $('matrixBody').innerHTML=hosts.map(c=>{const own=byHost.get(c.id)||[];return `<tr><td class="host"><strong>${esc(c.name)}</strong><small>${esc(c.shadow_account)} · ${esc(c.ip)}</small><div class="host-progress">${own.filter(a=>a.published).length} / ${own.length} 已发</div></td>`+Array.from({length:slots},(_,i)=>{const a=visiblePositions.get(renderPositionKey(c.id,i+1));if(!a){if(layoutActive){const cell=index.layoutsByPosition.get(renderPositionKey(c.id,i+1));if(cell?.label){if(publishingCellOffice(cell)!==activeOffice&&activeOffice!=='all')return '<td><div class="empty-slot">其他办公室账号</div></td>';if(!cell.account_id&&visibleLayoutCells.has(cell))return `<td><button type="button" class="account-card unresolved-card unresolved-link" data-match-computer="${esc(cell.computer_id)}" data-match-slot="${cell.slot}" ${hasPermission('publishing_manage')?'':'disabled title="需要管理发布格子关联权限"'}><div class="account-name">${esc(cell.label)}</div><span class="badge warn">待匹配账号 · 点击关联</span><small>${esc(cell.source_cell)} · ${esc(cell.reason||'需核对账号身份')}</small></button></td>`;return '<td><div class="empty-slot">已被筛选隐藏</div></td>';}return '<td><div class="empty-slot">暂无发布安排</div></td>';}const occupied=index.accountsByPosition.has(renderPositionKey(c.id,i+1));return `<td><div class="empty-slot">${occupied?(officePositions.has(renderPositionKey(c.id,i+1))?'已被筛选隐藏':'其他办公室账号'):'＋ 暂无账号'}</div></td>`;}return `<td><div class="account-card ${a.published?'done':''}">${typeof batchPick==='function'?`<label class="board-batch-pick">${batchPick('daily',a.id)} 选择账号</label>`:''}<div class="card-top"><div class="account-name">${esc(a.publishing_label||a.name)}</div>${badge(a)}</div>${verificationMarkup(a,true)}<div class="card-bottom"><button class="edit-card" data-edit="${esc(a.id)}">账号资料 ↗</button>${layoutActive&&index.manualPositions.has(renderPositionKey(c.id,i+1))&&hasPermission('publishing_manage')?`<button class="text-button manual-link-button" data-match-computer="${esc(c.id)}" data-match-slot="${i+1}">调整关联</button>`:''}<small class="card-count" title="${esc((a.last_collected||'尚无成功采集')+' · '+(a.collection_state||'待采集')+' '+(a.collection_error||''))}">作品 ${a.work_count===null?"待采集":a.work_count}<br>${esc(a.collection_state||'待采集')}</small><button class="publish-toggle" data-publish="${esc(a.id)}" aria-pressed="${!!a.published}">${a.published?'✓ 已发':'○ 未发'}</button></div></div></td>`;}).join('')+'</tr>';}).join('')||`<tr><td colspan="${slots+1}" class="empty-result">没有符合条件的账号，试试重置筛选。</td></tr>`;
 $('accountsBody').innerHTML='';
 }else{
 $('matrixHead').innerHTML='';$('matrixBody').innerHTML='';
 const accountRows=prepareAccountPage(accounts);
 $('accountsBody').innerHTML=accountRows.map(a=>`<tr><td><input type="checkbox" data-select="${esc(a.id)}" ${selected.has(a.id)?'checked':''} aria-label="选择账号 ${esc(a.name)}"></td><td class="account-identity"><strong>${esc(a.name)}</strong><small>${a.douyin_id?'抖音号 '+esc(a.douyin_id):'抖音号待补充'}</small></td><td class="account-position">登录手机：${esc(a.phone_device_name||'待确认')}<small>实际发布：${actualPublishingText(a)}</small><small>登记电脑：${esc(index.computersById.get(a.computer_id)?.name||'待确认')}${a.slot===null?' · 尚未分配位置':' · 账号位置 '+esc(a.slot)}</small></td><td class="account-status">${badge(a)}</td><td><button class="publish-toggle" data-publish="${esc(a.id)}" aria-pressed="${!!a.published}">${a.published?'✓ 已发':'○ 未发'}</button></td><td><details class="account-row-details"><summary>查看资料</summary><div class="account-row-detail-content"><section><h3>账号类型 / 注册进度</h3>${lifecycleDetails(a)}</section><section><h3>稳定与改密</h3><small>稳定：${esc(a.card_stability||'待确认')}</small><small>改密：${esc(a.password_state||'待确认')}</small></section><section><h3>账号核对</h3>${verificationMarkup(a)}</section><section><h3>公开作品数量 / 采集记录</h3>${a.work_count===null?'待采集':a.work_count}<small>${esc(a.last_collected||'尚无成功采集')}</small><small class="collection-state" title="${esc(a.collection_error)}">${esc(a.collection_state||(a.homepage||a.douyin_id?'等待首次采集':'需填写真实抖音号'))}</small>${a.collection_error?`<small class="collection-error">${esc(a.collection_error)}</small>`:''}<button class="text-button" data-collect="${esc(a.id)}">采集</button></section><section><h3>完整备注</h3><div class="account-detail-note">${esc(a.note||'—')}</div></section></div></details></td><td class="account-row-actions"><button class="text-button" data-edit="${esc(a.id)}">编辑</button><a class="text-button" href="/workspace.html?account=${encodeURIComponent(a.id)}">关联资料 / 历史</a></td></tr>`).join('')||'<tr><td colspan="7" class="empty-result">没有符合条件的账号，试试清除筛选条件。</td></tr>';
 }
 $('selectedCount').textContent=`已选择 ${selected.size} 个账号`;$('selectAll').checked=accounts.length>0&&accounts.every(a=>selected.has(a.id));$('selectAll').indeterminate=selected.size>0&&!$('selectAll').checked;
 document.querySelector('#boardView .footnote').textContent=layoutActive?'发布位置来自工作进度表。点击待匹配格子选择对应账号，确认后即可记录发布；空位表示原表没有发布安排。':'点击未发或已发切换当天记录。';$('boardView').hidden=page!=='board';$('accountsView').hidden=page!=='accounts';$('title').textContent=page==='board'?'每日发布看板':'账号管理';$('breadcrumb').textContent=page==='board'?'每日发布':'账号资料';$('panelTitle').textContent=page==='board'?'主机发布矩阵':'账号清单';$('subtitle').textContent=page==='board'?'以工作进度表的电脑行、发布设备列为准。待匹配格子保留原文，确认账号后才能记录发布。':'状态人工维护，按电脑统一筛选。修改会同步到每日发布看板。';document.querySelectorAll('[data-page]').forEach(b=>b.classList.toggle('active',b.dataset.page===page));document.querySelectorAll('.nav-group').forEach(group=>group.open=!!group.querySelector('.nav-item.active'));if(typeof renderBatchControls==='function')renderBatchControls();applyPermissions();syncDailyButtons();
}
function edit(id){const a=data.accounts.find(a=>a.id===id)||{id:'',name:'',computer_id:'unassigned',slot:1,status:'待确认',douyin_id:'',homepage:'',note:''};lifecycleEdit(a);readinessEdit(a);phoneDeviceEdit(a);$('editId').value=a.id;$('editOfficeGroup').value=a.office_group||(a.id?'':activeOffice==='three'?'three':'');$('editTitle').textContent=a.id?`编辑账号 · ${a.name}`:'新增账号 / 小号';$('editName').value=a.name;$('editComputer').innerHTML=data.computers.map(c=>`<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');$('editComputer').value=a.computer_id;$('editSlot').value=a.slot||1;$('editStatus').innerHTML=data.statuses.map(s=>`<option>${esc(s)}</option>`).join('');$('editStatus').value=a.status;$('editDouyin').value=a.douyin_id;$('editHomepage').value=a.homepage;$('editNote').value=a.note;$('editError').textContent='';ScopeUI.mount('editForm',data,'accounts',a);$('editor').showModal();captureVersions('editor');applyAccountPermissions(a);accountEditBaseline=a.id?accountEditValues():{};}
document.addEventListener('click',async e=>{const collect=e.target.closest('[data-collect]');if(collect)await queue([collect.dataset.collect]);const nav=e.target.closest('[data-page]');if(nav){page=nav.dataset.page;history.replaceState(null,'','?page='+page);render();}const editor=e.target.closest('[data-edit]');if(editor)edit(editor.dataset.edit);const toggle=e.target.closest('[data-publish]');if(toggle)await toggleDaily(toggle.dataset.publish);});
$('accountsBody').addEventListener('change',e=>{if(e.target.dataset.select){const id=e.target.dataset.select;e.target.checked?selected.add(id):selected.delete(id);render();}});
$('selectAll').onchange=()=>{filtered().forEach(a=>$('selectAll').checked?selected.add(a.id):selected.delete(a.id));render();};
$('applyBulk').onclick=async()=>{if(!selected.size)return toast('请先勾选账号');const ids=[...selected],status=$('bulkStatus').value,requestKey='bulk-status:'+ids.slice().sort().join(',')+':'+status;try{await api('/api/bulk-status',{ids,status,request_id:claimRequestId(requestKey)});settleRequestId(requestKey);selected.clear();await load();toast('账号状态已批量更新');}catch(error){settleRequestId(requestKey,error);toast(error.message);}};
$('closeDialog').onclick=()=>$('editor').close();
let accountEditBaseline={};
function accountEditValues(){return {...ScopeUI.payload('editForm'),...lifecyclePayload(),...readinessPayload(),phone_device_id:$('editPhoneDevice').value||null,name:$('editName').value,computer_id:$('editComputer').value,slot:Number($('editSlot').value),status:$('editStatus').value,note:$('editNote').value,douyin_id:$('editDouyin').value,homepage:$('editHomepage').value};}
$('editForm').onsubmit=async event=>{
 event.preventDefault();const button=event.submitter;button.disabled=true;
 const accountId=$('editId').value;const requestKey=accountId?'account-patch:'+accountId:'account-create';
 try{
  const values=accountEditValues();
  if(accountId){
   const changes=Object.fromEntries(Object.entries(values).filter(([key,value])=>JSON.stringify(value)!==JSON.stringify(accountEditBaseline[key])));
   if(!Object.keys(changes).length){$('editor').close();toast('没有需要保存的修改');return;}
   const result=await api('/api/account',{id:accountId,...values,request_id:claimRequestId(requestKey)});
   settleRequestId(requestKey);
   $('editor').close();
   const index=data.accounts.findIndex(account=>account.id===result.account.id);
   if(index>=0)data.accounts[index]=result.account;else data.accounts.push(result.account);
   render();toast('已保存，两个页面已同步');
  }else{await api('/api/account/create',{...values,office_group:$('editOfficeGroup').value,request_id:claimRequestId(requestKey)});settleRequestId(requestKey);$('editor').close();await load();toast('已保存，两个页面已同步');}
 }catch(error){settleRequestId(requestKey,error);$('editError').textContent=error.message;}finally{button.disabled=false;}
};
accountSearchFields.forEach(id=>{$(id).oninput=()=>{activeLifecyclePreset='';render();};$(id).onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();activeLifecyclePreset='';render();}};});['computer','status','published','loginPhoneFilter','verificationFilter'].forEach(id=>$(id).onchange=render);$('date').onchange=()=>{selected.clear();if(data)return loadPublished($('date').value);};$('today').onclick=()=>{if(!data)return;$('date').value=data.today;return loadPublished(data.today);};$('refresh').onclick=load;$('clear').onclick=()=>{clearAccountSearch();activeLifecyclePreset='';['query','computer','status','published','accountType','lifecycleFilter','ageFilter','loginPhoneFilter'].forEach(id=>$(id).value='');selected.clear();render();};async function refreshFromFocus(){if(!data)return;const editing=['editor','cardEditor','phoneDeviceEditor','publishingMatchDialog','verificationEditor','batchEditor'].some(id=>$(id)?.open);if(Number.isInteger(data.cursor)&&typeof catchUpBoard==='function'){await catchUpBoard();return;}if(!editing)await load();}
window.addEventListener('focus',()=>{const job=refreshFromFocus();job.catch(()=>{});return job;});openBoard();

async function queue(ids){if(!ids.length)return toast('没有可采集的账号');try{const result=await api('/api/collection/enqueue',{ids});await load();toast('已加入 '+result.queued+' 个任务'+(result.missing?'；'+result.missing+' 个缺少真实抖音号或主页':''));}catch(e){toast(e.message);}}
$('collectVisible').onclick=()=>queue(filtered().filter(a=>page!=='board'||a.computer_id!=='unassigned').map(a=>a.id));$('collectSelected').onclick=()=>queue([...selected]);$('autoCollect').onchange=async()=>{try{await api('/api/collection/settings',{auto_enabled:$('autoCollect').checked});await load();}catch(e){toast(e.message);}};$('resumeCollect').onclick=async()=>{try{await api('/api/collection/settings',{resume:true});await load();}catch(e){toast(e.message);}};$('openCollector').onclick=async()=>{try{await api('/api/collection/settings',{open_browser:true});toast('已通知服务器电脑打开抖音窗口；登录或验证完成后点击继续采集');await load();}catch(e){toast(e.message);}};{const boardUpdates=document.createElement('script');boardUpdates.src='/board-updates.js';boardUpdates.onload=async()=>{let status=0;try{status=(await probeBoardUpdates()).status;}catch(error){status=0;}if(status!==404)return;setInterval(()=>{if(data&&!$('editor').open&&!$('cardEditor').open&&!$('phoneDeviceEditor').open&&!$('publishingMatchDialog').open&&!$('verificationEditor')?.open&&!$('batchEditor')?.open&&document.visibilityState==='visible')load();},5000);};document.body.appendChild(boardUpdates);}

$('pauseCollect').onclick=async()=>{try{await api('/api/collection/settings',{pause:true});await load();}catch(e){toast(e.message);}};
$('cancelCollect').onclick=async()=>{if(!confirm('取消所有办公室尚未开始的采集任务？已有作品数量保留，正在采集的任务会继续完成。'))return;try{await api('/api/collection/settings',{cancel_waiting:true});await load();toast('等待任务已取消；每日自动采集开启时仍会生成新任务');}catch(e){toast(e.message);}};
