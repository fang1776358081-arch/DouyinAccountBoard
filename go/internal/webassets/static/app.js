const $=id=>document.getElementById(id);
function renderBackup(){$('saveState').textContent=data.user?.scope_mode?'数据保存在共享服务':'数据保存在本机';const b=data.backup||{};$('backupState').textContent=b.enabled?(b.last_backup?'自动备份：'+new Date(b.last_backup).toLocaleString('zh-CN',{hour12:false})+' · 每4小时 / 保留30天':'自动备份：等待首次成功'):'自动备份未启动';$('backupState').title=b.directory||'';$('backupError').hidden=!b.error;$('backupError').textContent=b.error||'';}
let data=null,page=['board','accounts','cards','phones'].includes(new URLSearchParams(location.search).get('page'))?new URLSearchParams(location.search).get('page'):'board',selected=new Set(),requestSerial=0;
// Read responses never cross a local write or an editing interaction.
let boardMutationEpoch=0,boardWrites=0;
function boardSyncInteraction(){boardMutationEpoch++;}
for(const event of ['input','change','focusin','click'])document.addEventListener(event,boardSyncInteraction,true);
if(typeof MutationObserver!=='undefined'&&document.body)new MutationObserver(records=>{if(records.some(record=>record.target?.tagName==='DIALOG'))boardSyncInteraction();}).observe(document.body,{subtree:true,attributes:true,attributeFilter:['open']});
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const abnormal=a=>['短期封禁','封禁','发不了','停用'].includes(a.status);
const badge=a=>`<span class="badge ${a.status==='正常'?'good':abnormal(a)?'warn':''}">${esc(a.status)}</span>`+(a.device_access?.login_state==='登录受阻'?`<button type="button" class="text-button" data-workflow-account="${esc(a.id)}">登录受阻 · 查看恢复</button>`:'');
function toast(message){$('toast').textContent=message;$('toast').classList.add('show');clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').classList.remove('show'),2300);}
const pendingRequestIds=new Map();
function newRequestId(){if(typeof crypto.randomUUID==='function')return crypto.randomUUID();const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);bytes[6]=bytes[6]&15|64;bytes[8]=bytes[8]&63|128;let hex='';for(let i=0;i<16;i++)hex+=bytes[i].toString(16).padStart(2,'0');return hex.slice(0,8)+'-'+hex.slice(8,12)+'-'+hex.slice(12,16)+'-'+hex.slice(16,20)+'-'+hex.slice(20);}
function claimRequestId(key){if(!pendingRequestIds.has(key))pendingRequestIds.set(key,newRequestId());return pendingRequestIds.get(key);}
function settleRequestId(key,error){if(error&&error.status===undefined)return;pendingRequestIds.delete(key);}
async function sendApi(path,body,options={}){
 const writing=!!body,trackWrite=writing&&options.trackWrite!==false;if(writing){if(trackWrite)boardWrites++;boardMutationEpoch++;}
 try{
  if(body&&data&&options.fixedPayload!==true)body=versionPayload(path,body,data,activeEditVersions());
  const response=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-CSRF-Token':currentUser?.csrf||data?.user?.csrf||''}:{},body:body?JSON.stringify(body):undefined,...(options.signal?{signal:options.signal}:{})});
  const result=await response.json();if(response.status===401){clearBoardCache();location.replace(location.search==='?office=three'?'/login.html?next='+encodeURIComponent('/?office=three'):'/login.html');const failure=Error('登录已失效，请重新登录');failure.status=401;throw failure;}
  if(!response.ok){const failure=Error(result.error||'保存失败');failure.status=response.status;failure.code=result.code;throw failure;}
  if(data&&options.applyVersions!==false)applyVersionDelta(result._record_versions,data);return result;
 }finally{if(writing){if(trackWrite)boardWrites--;boardMutationEpoch++;}}
}
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
 if(window.AccountInline?.hasDraft()){window.AccountInline.announceBlocked();return;}
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
async function boardReadWithSignal(read,signal){
 if(!signal)return read;
 let cancel;const aborted=new Promise((resolve,reject)=>{cancel=()=>{const error=Error('读取更新已取消');error.name='AbortError';reject(error);};if(signal.aborted)cancel();else signal.addEventListener('abort',cancel,{once:true});});
 try{return await Promise.race([read,aborted]);}finally{signal.removeEventListener('abort',cancel);}
}
async function load(allowWorkflowReload=false,syncOptions=null){
 if(window.DeviceInline?.hasDraft()){window.DeviceInline.announceBlocked();return false;}
 if(window.CardInline?.hasDraft()){window.CardInline.announceBlocked();return false;}
 if(window.EmployeeCardLink?.hasDraft()){window.EmployeeCardLink.announceBlocked();return false;}
 if(window.AccountInline?.hasDraft()){window.AccountInline.announceBlocked();return false;}
 const background=syncOptions?.background===true;
 if(window.DeviceWorkflow?.isOpen()&&allowWorkflowReload!==true||background&&typeof boardSyncBlocked==='function'&&boardSyncBlocked())return false;
 const opened=data,epoch=boardMutationEpoch,view=++dailyViewGeneration;dailyViewLoading=view;const serial=++requestSerial;globalThis.requestSerial=requestSerial;
 if(!background)window.CardPageUI?.state('loading',!!data);
 try{
  const next=await boardReadWithSignal(api('/api/board?version_scope=day'+($('date').value?'&date='+encodeURIComponent($('date').value):'')+(page==='accounts'?'&compact=accounts':''),undefined,{signal:syncOptions?.signal,applyVersions:background?false:undefined}),syncOptions?.signal);
  if(serial!==requestSerial||window.DeviceInline?.hasDraft()||window.CardInline?.hasDraft()||window.DeviceWorkflow?.isOpen()&&allowWorkflowReload!==true)return false;
  if(background&&(opened!==data||epoch!==boardMutationEpoch||typeof boardSyncBlocked==='function'&&boardSyncBlocked(true)||syncOptions.isCurrent&&!syncOptions.isCurrent()))return false;
  if(!background)window.CardPageUI?.state('ready',true);showBoard(next);return true;
 }catch(e){if(background)throw e;if(serial!==requestSerial)return false;window.CardPageUI?.state('error',!!data);$('loadError').textContent='加载失败：'+e.message;$('loadError').hidden=page==='cards'&&!!window.CardPageUI;return false;}
 finally{if(dailyViewLoading===view)dailyViewLoading=0;}
}
function boardStorage(){if(typeof localStorage==='undefined')return null;return localStorage;}
function readBoardCache(){const storage=boardStorage();if(!storage)return null;try{const parsed=JSON.parse(storage.getItem('douyin-board-view')||'null');if(!parsed||typeof parsed!=='object'||parsed.userId==null||!parsed.board||!Number.isInteger(parsed.board.cursor)||parsed.board.user?.id==null||!parsed.board.device_workflow||typeof parsed.board.device_workflow!=='object')return null;if(String(parsed.board.user.id)!==String(parsed.userId))return null;return parsed;}catch(error){return null;}}
function writeBoardCache(){const storage=boardStorage();if(!storage||!data||data.user?.id==null||!Number.isInteger(data.cursor))return;try{storage.setItem('douyin-board-view',JSON.stringify({userId:data.user.id,board:data}));}catch(error){}}
function clearBoardCache(){const storage=boardStorage();if(!storage)return;try{storage.removeItem('douyin-board-view');}catch(error){}}
function showBoard(next){window.AccountInline?.acceptBoard();data=next;globalThis.data=data;renderVerificationFilter();$('date').value=data.date;$('loadError').hidden=true;const statusValue=$('status').value;$('status').innerHTML='<option value="">全部账号状态</option>'+data.statuses.map(s=>`<option>${esc(s)}</option>`).join('');$('status').value=statusValue;const bulkValue=$('bulkStatus').value;$('bulkStatus').innerHTML=data.statuses.map(s=>`<option>${esc(s)}</option>`).join('');if(bulkValue)$('bulkStatus').value=bulkValue;render();if(typeof CustomEvent!=='undefined')document.dispatchEvent(new CustomEvent('board-ready'));writeBoardCache();}
async function loadPublished(day){if(window.DeviceInline?.hasDraft()){$('date').value=data?.date||'';window.DeviceInline.announceBlocked();return false;}if(window.CardInline?.hasDraft()){$('date').value=data?.date||'';window.CardInline.announceBlocked();return false;}if(window.EmployeeCardLink?.hasDraft()){$('date').value=data?.date||'';window.EmployeeCardLink.announceBlocked();return false;}if(window.AccountInline?.hasDraft()){$('date').value=data?.date||'';window.AccountInline.announceBlocked();return false;}const view=++dailyViewGeneration;dailyViewLoading=view;const serial=++requestSerial;globalThis.requestSerial=requestSerial;try{const result=await api('/api/daily-flags?date='+encodeURIComponent(day)+'&versions=1',undefined,{applyVersions:false});if(serial!==requestSerial||!data||window.DeviceInline?.hasDraft()||window.CardInline?.hasDraft())return;data.date=result.date;$('date').value=result.date;const versions=Object.fromEntries(Object.entries(result.record_versions||{}).filter(([key])=>key.startsWith('daily:')));data.record_versions={...data.record_versions,...versions};const flags=result.published||{};for(const account of data.accounts)account.published=!!flags[account.id];render();writeBoardCache();}catch(e){$('loadError').textContent='加载失败：'+e.message;$('loadError').hidden=false;}finally{if(dailyViewLoading===view)dailyViewLoading=0;}}
async function openBoard(){
 if(typeof prepareWorkspaceTodoShortcut==='function'&&prepareWorkspaceTodoShortcut()){await load();return;}
 const cached=readBoardCache();if(!cached){clearBoardCache();await load();return;}let me=null;try{const response=await fetch('/api/auth/me');if(response.status===401){clearBoardCache();location.replace(location.search==='?office=three'?'/login.html?next='+encodeURIComponent('/?office=three'):'/login.html');return;}if(response.ok)me=(await response.json()).user||null;}catch(error){}if(!me||me.id==null||String(cached.userId)!==String(me.id)){clearBoardCache();await load();return;}showBoard(cached.board);
 try{if(window.boardLiveSync)await window.boardLiveSync.poke();else if(typeof catchUpBoard==='function')await catchUpBoard();else await load();}
 catch(error){if(error.status!==401){if(window.LiveSync)window.LiveSync.status('boardSyncState',{phase:'error',forbidden:error.status===403});else if($('boardSyncState'))$('boardSyncState').textContent='同步暂时中断，请稍后重试';}}
}
function filtered(){return officeAccounts(pageAccounts()).filter(a=>phoneDeviceMatches(a)&&lifecyclePresetMatches(a)&&accountSearchMatches(a)&&verificationMatches(a)&&(!window.AccountPilot||window.AccountPilot.matches(a))&&(!$('computer').value||a.computer_id===$('computer').value)&&(!$('status').value||a.status===$('status').value)&&(!$('accountType').value||a.account_type===$('accountType').value)&&(!$('ageFilter').value||(a.account_type==='大号'&&($('ageFilter').value==='missing'?!a.registered_on:a.registered_on&&($('ageFilter').value==='mature'?a.days_remaining===0:a.days_remaining>0))))&&(!$('lifecycleFilter').value||a.lifecycle===$('lifecycleFilter').value)&&(!$('published').value||(!!a.published===($('published').value==='yes'))));}
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
function renderDailyMatrix(hosts,accounts,officeScope,layoutCells,layoutActive,index){
 const displayedHosts=new Set(hosts.map(host=>host.id));
 const byHost=new Map(),visiblePositions=new Map();
 const officePositions=new Set(officeScope.map(account=>renderPositionKey(account.computer_id,account.slot)));
 const visibleLayoutCells=new Set(layoutCells);
 const body=$('matrixBody');
 const expanded=new Set([...body.querySelectorAll('.publish-cell-details[open]')].map(node=>node.dataset.publishDetail));
 for(const account of accounts){
  appendRenderGroup(byHost,account.computer_id,account);
  const key=renderPositionKey(account.computer_id,account.slot);
  if(!visiblePositions.has(key))visiblePositions.set(key,account);
 }
 const slots=layoutActive
  ?data.publishing_layout.cells.reduce((max,cell)=>displayedHosts.has(cell.computer_id)?Math.max(max,cell.slot):max,3)
  :data.accounts.reduce((max,account)=>Math.max(max,account.slot),3);
 $('matrixHead').innerHTML='<tr><th scope="col">电脑 / 主机</th>'+Array.from({length:slots},(_,i)=>`<th scope="col">${layoutActive?'发布设备':'账号位置'} ${i+1}</th>`).join('')+'</tr>';
 body.innerHTML=hosts.map(host=>{
  const own=byHost.get(host.id)||[],done=own.filter(account=>account.published).length;
  const hostInfo=[host.shadow_account,host.ip].filter(Boolean).join(' · ');
  const header=`<td class="host"><strong${hostInfo?` title="${esc(hostInfo)}"`:''}>${esc(host.name)}</strong><div class="host-progress">${done} / ${own.length} 已发</div></td>`;
  const cells=Array.from({length:slots},(_,i)=>{
   const slot=i+1,key=renderPositionKey(host.id,slot),account=visiblePositions.get(key);
   if(account){
    const detailKey=JSON.stringify([host.id,slot,account.id]);
    const canAdjust=layoutActive&&index.manualPositions.has(key)&&hasPermission('publishing_manage');
    return renderPublishAccountCell(account,host.id,slot,detailKey,expanded.has(detailKey),canAdjust);
   }
   if(layoutActive){
    const cell=index.layoutsByPosition.get(key);
    if(!cell?.label)return renderEmptyPublishCell('暂无发布安排');
    if(activeOffice!=='all'&&publishingCellOffice(cell)!==activeOffice)return renderEmptyPublishCell('其他办公室账号');
    if(cell.account_id===null&&!cell.manual_link&&visibleLayoutCells.has(cell))return renderPendingPublishCell(cell);
    return renderEmptyPublishCell('已被筛选隐藏');
   }
   if(!index.accountsByPosition.has(key))return renderEmptyPublishCell('＋ 暂无账号');
   return renderEmptyPublishCell(officePositions.has(key)?'已被筛选隐藏':'其他办公室账号');
  });
  return `<tr>${header}${cells.join('')}</tr>`;
 }).join('')||`<tr><td colspan="${slots+1}" class="empty-result">没有符合条件的账号，试试重置筛选。</td></tr>`;
}

function renderEmptyPublishCell(label){
 return `<td class="publish-cell"><div class="empty-slot">${label}</div></td>`;
}

function renderPendingPublishCell(cell){
 const reason=[cell.source_cell,cell.reason||'需核对账号身份'].filter(Boolean).join(' · ');
 const content=`<span class="badge warn">待关联</span><span class="account-name publish-pending-label" title="${esc(cell.label)}">${esc(cell.label)}</span><small class="publish-pending-reason">${esc(reason)}</small>`;
 const card=hasPermission('publishing_manage')
  ?`<button type="button" class="account-card unresolved-card unresolved-link" data-match-computer="${esc(cell.computer_id)}" data-match-slot="${esc(cell.slot)}">${content}<span class="publish-review-action">核对并关联</span></button>`
  :`<div class="account-card unresolved-card" tabindex="-1">${content}<small class="publish-pending-reason">由有权限的人员关联</small></div>`;
 return `<td class="publish-cell" data-publish-computer="${esc(cell.computer_id)}" data-publish-slot="${esc(cell.slot)}">${card}</td>`;
}

function publishAccountExceptions(account){
 const rows=[];
 if(account.status==='封禁'||account.status==='短期封禁'){
  rows.push(`<small>核对关联的手机卡、手机和发布格</small><button type="button" class="text-button publish-review-action" data-publish-exception-action data-board-ban-account="${esc(account.id)}">查看关联影响</button>`);
 }else if(account.status==='发不了'||account.status==='停用'){
  const message=account.status==='停用'?'请复核此发布安排是否仍需保留':'请核对账号状态和发布安排';
  const action=hasPermission('account_status')
   ?`<button type="button" class="text-button publish-review-action" data-publish-exception-action data-edit="${esc(account.id)}">核对账号状态</button>`
   :`<a class="text-button publish-review-action" data-publish-exception-action href="/workspace.html?tab=accounts&amp;account=${encodeURIComponent(account.id)}">查看账号</a>`;
  rows.push(`<small>${message}</small>${action}`);
 }
 if(account.device_access?.login_state==='登录受阻'){
  const canOpen=window.DeviceWorkflow?.enabled();
  const action=canOpen?`<button type="button" class="text-button publish-review-action" data-publish-exception-action data-workflow-account="${esc(account.id)}">${hasPermission('device_manage')?'处理登录恢复':'查看恢复记录'}</button>`:'';
  const reason=String(account.device_access.reason||'').trim();
  const message=reason?'登录受阻 · '+reason:'登录受阻';
  rows.push(`<small title="${esc(message)}">${esc(message)}</small>${action}`);
 }
 return rows.length?`<div class="publish-cell-exceptions">${rows.map(row=>`<div>${row}</div>`).join('')}</div>`:'';
}

function renderPublishAccountCell(account,computer,slot,detailKey,expanded,canAdjust){
 const name=account.name||account.publishing_label||'昵称待补充';
 const nameTitle=account.publishing_label&&account.publishing_label!==name?`${name} · 原表：${account.publishing_label}`:name;
 const status=account.status||'待确认',statusTone=status==='正常'?'good':abnormal(account)?'warn':'';
 const batch=typeof batchPick==='function'?`<label class="board-batch-pick">${batchPick('daily',account.id)}</label>`:'';
 const adjust=canAdjust?`<button type="button" class="text-button manual-link-button" data-match-computer="${esc(computer)}" data-match-slot="${slot}">调整关联</button>`:'';
 const exceptions=publishAccountExceptions(account);
 const collectionState=account.collection_state||'待采集';
 const collectionInfo=[account.last_collected||'尚无成功采集',collectionState,account.collection_error].filter(Boolean).join(' · ');
 return `<td class="publish-cell" data-publish-computer="${esc(computer)}" data-publish-slot="${esc(slot)}"><div class="account-card${account.published?' done':''}">
  <div class="publish-cell-top">${batch}<div class="account-name" title="${esc(nameTitle)}">${esc(name)}</div></div>
  <div class="publish-cell-status"><span class="badge ${statusTone}">${esc(status)}</span><button type="button" class="publish-toggle" data-publish="${esc(account.id)}" aria-pressed="${!!account.published}">${account.published?'✓ 已发':'○ 未发'}</button></div>
  ${exceptions}
  <details class="publish-cell-details" data-publish-detail="${esc(detailKey)}"${expanded?' open':''}><summary>核对与详情</summary>
   ${verificationMarkup(account,true)}
   <div class="publish-cell-detail-actions"><button type="button" class="edit-card" data-edit="${esc(account.id)}">账号资料 ↗</button>${adjust}</div>
   <small class="card-count" title="${esc(collectionInfo)}">作品 ${esc(account.work_count??'待采集')} · ${esc(collectionState)}</small>
  </details>
 </div></td>`;
}

function accountPageRows(rows){return rows.slice((accountPageIndex-1)*accountPageSize,accountPageIndex*accountPageSize);}
function prepareAccountPage(accounts){
 const fields=[...accountSearchFields,'computer','status','published','accountType','ageFilter','lifecycleFilter','loginPhoneFilter','verificationFilter'];
 const signature=JSON.stringify([activeOffice,data.date,typeof activeLifecyclePreset==='undefined'?'':activeLifecyclePreset,...fields.map(id=>$(id)?.value||''),...(window.AccountPilot?.signature()||[])]);
 if(signature!==accountPageSignature){accountPageSignature=signature;accountPageIndex=1;}
 const pages=Math.max(1,Math.ceil(accounts.length/accountPageSize));accountPageIndex=Math.min(Math.max(1,accountPageIndex),pages);
 let controls=$('accountPagination');if(!controls){controls=document.createElement('div');controls.id='accountPagination';controls.className='card-pagination';$('accountsView').append(controls);}
 controls.innerHTML=`<button id="accountsPrevious" type="button" class="button">上一页账号</button><span>第 ${accountPageIndex} / ${pages} 页 · 共 ${accounts.length} 个账号 · 本页 ${accounts.length?(accountPageIndex-1)*accountPageSize+1:0}–${Math.min(accountPageIndex*accountPageSize,accounts.length)} 条</span><button id="accountsNext" type="button" class="button">下一页账号</button>`;
 $('accountsPrevious').disabled=accountPageIndex<=1;$('accountsNext').disabled=accountPageIndex>=pages;
 $('accountsPrevious').onclick=()=>{accountPageIndex--;render();};$('accountsNext').onclick=()=>{accountPageIndex++;render();};
 return accountPageRows(accounts);
}
function render(){if(window.DeviceInline?.hasDraft())return;if(window.CardInline?.hasDraft())return;if(window.EmployeeCardLink?.hasDraft())return;if(window.AccountInline?.hasDraft())return;if(!data)return;const previous=activeRenderIndex;activeRenderIndex=createRenderIndex();try{window.AccountPilot?.beforeRender();renderPageContent(activeRenderIndex);window.WorkbenchNav?.update({user:data.user,page,office:page==='cards'?$('cardOfficeFilter').value:activeOffice});window.AccountPilot?.afterRender();window.BanImpact?.sync(data,page);}finally{activeRenderIndex=previous;}}
function renderPageContent(index){if(typeof activateAccountConsole==='function')activateAccountConsole(page==='accounts');window.CardPageUI?.activate(page==='cards');document.body.classList.toggle('phones-console',page==='phones');document.body.classList.toggle('board-fit',page==='board');renderBackup();renderOffices();const cardsPage=page==='cards',phonesPage=page==='phones';$('cardsWorkspace').hidden=!cardsPage;$('phonesWorkspace').hidden=!phonesPage;$('accountWorkspace').hidden=cardsPage||phonesPage;$('collectionBar').hidden=cardsPage||phonesPage;document.querySelector('.header-actions').hidden=cardsPage||phonesPage;if(cardsPage||phonesPage){$('matrixHead').innerHTML='';$('matrixBody').innerHTML='';$('accountsBody').innerHTML='';$('lifecycleSummary').hidden=true;phonesPage?renderPhones():renderCards();applyPermissions();return;}renderComputerFilter();renderPhoneFilter();lifecycleSummary();const officeScope=officeAccounts();const accounts=filtered().filter(a=>page!=='board'||a.computer_id!=='unassigned'),visibleIds=new Set(accounts.map(a=>a.id));for(const id of selected)if(!visibleIds.has(id))selected.delete(id);if(typeof renderAccountFilterSummary==='function')renderAccountFilterSummary(accounts);const done=accounts.filter(a=>a.published).length;const layoutCells=page==='board'?filteredPublishingCells(accounts):[],layoutActive=page==='board'&&publishingEnabled();const computerFilter=!!$('computer').value,hostIds=new Set((layoutActive?layoutCells:accounts).map(row=>row.computer_id));const hosts=data.computers.filter(c=>c.id!=='unassigned'&&(!computerFilter||c.id===$('computer').value)&&hostIds.has(c.id));
 $('stats').innerHTML=[['负责电脑',hosts.length,'本次筛选中的主机','▣'],['账号总数',accounts.length,layoutActive?'已匹配到发布格子的账号':'已导入的账号资料','◎'],['当天已发',done,accounts.length?Math.round(done/accounts.length*100)+'% 完成':'暂无账号','✓'],['当天未发',accounts.length-done,'待人工勾选记录','◷']].map((s,i)=>`<div class="stat ${i===2?'highlight':''}"><span class="stat-symbol">${s[3]}</span><div class="stat-label">${s[0]}</div><strong>${s[1]}</strong><small>${s[2]}</small></div>`).join('');
 renderBoardCollectionInfo();
 const pendingCount=layoutCells.filter(cell=>cell.label?.trim()&&cell.account_id===null&&!cell.manual_link).length;
 const resultCount=layoutActive?`${accounts.length} 格已关联 · ${pendingCount} 格待关联`:`${accounts.length} 个账号`;
 $('scope').textContent=`${officeName()} · ${data.date===data.today?'今天':data.date} · ${hosts.length} 台电脑 · ${resultCount}`;
 if(page==='board'){
 renderDailyMatrix(hosts,accounts,officeScope,layoutCells,layoutActive,index);
 $('accountsBody').innerHTML='';
 }else{
 $('matrixHead').innerHTML='';$('matrixBody').innerHTML='';
 const accountRows=prepareAccountPage(accounts);
 $('accountsBody').innerHTML=accountRows.map(account=>compactAccountRow(account,index)).join('')||'<tr><td colspan="7" class="empty-result">没有符合条件的账号，试试清除筛选条件。</td></tr>';
 }
 $('selectedCount').textContent=`已选择 ${selected.size} 个账号`;$('selectAll').checked=accounts.length>0&&accounts.every(a=>selected.has(a.id));$('selectAll').indeterminate=selected.size>0&&!$('selectAll').checked;
 document.querySelector('#boardView .footnote').textContent=layoutActive?'发布位置来自工作进度表。待关联格确认账号后即可记录发布。':'点击未发或已发切换当天记录。';$('boardView').hidden=page!=='board';$('accountsView').hidden=page!=='accounts';$('title').textContent=page==='board'?'每日发布看板':'账号资料';$('breadcrumb').textContent=page==='board'?'每日发布':'账号资料';$('panelTitle').textContent=page==='board'?'主机发布矩阵':'账号清单';$('subtitle').textContent=page==='board'?'按电脑和发布格记录当天进度。':'维护账号资料与发布安排，支持直接编辑和自动保存。';if(typeof renderBatchControls==='function')renderBatchControls();applyPermissions();syncDailyButtons();
}
function edit(id){if(window.AccountInline?.hasDraft()){window.AccountInline.announceBlocked();return;}const a=data.accounts.find(a=>a.id===id)||{id:'',name:'',computer_id:'unassigned',slot:1,status:'待确认',douyin_id:'',homepage:'',note:''};lifecycleEdit(a);readinessEdit(a);phoneDeviceEdit(a);$('editId').value=a.id;$('editPlacement').open=!a.id;$('editOfficeGroup').value=a.office_group||(a.id?'':activeOffice==='three'?'three':'');$('editTitle').textContent=a.id?`编辑账号 · ${a.name}`:'新增账号 / 小号';$('editName').value=a.name;$('editComputer').innerHTML=data.computers.map(c=>`<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');$('editComputer').value=a.computer_id;$('editSlot').value=a.slot||1;$('editStatus').innerHTML=data.statuses.map(s=>`<option>${esc(s)}</option>`).join('');$('editStatus').value=a.status;$('editDouyin').value=a.douyin_id;$('editHomepage').value=a.homepage;$('editNote').value=a.note;$('editError').textContent='';ScopeUI.mount('editForm',data,'accounts',a);$('editor').showModal();captureVersions('editor');applyAccountPermissions(a);accountEditBaseline=a.id?accountEditValues():{};}
document.addEventListener('click',async e=>{const collect=e.target.closest('[data-collect]');if(collect)await queue([collect.dataset.collect],collect);const nav=e.target.closest('[data-page]');if(nav&&!e.ctrlKey&&!e.metaKey&&!e.shiftKey&&!e.altKey){e.preventDefault();if(window.DeviceInline?.hasDraft()){window.DeviceInline.announceBlocked();return;}if(window.CardInline?.hasDraft()){window.CardInline.announceBlocked();return;}window.AccountPilot?.remember();page=nav.dataset.page;const target=new URL(nav.href,location.href);const office=target.searchParams.get('office');if(office&&officeDefinitions.some(item=>item.id===office)){activeOffice=office;if(page==='cards'&&[...$('cardOfficeFilter').options].some(option=>option.value===office))$('cardOfficeFilter').value=office;}history.replaceState(null,'',target.pathname+target.search);render();}const editor=e.target.closest('[data-edit]');if(editor)edit(editor.dataset.edit);const toggle=e.target.closest('[data-publish]');if(toggle)await toggleDaily(toggle.dataset.publish);});
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
   if(result.account){
    const index=data.accounts.findIndex(account=>account.id===result.account.id);
    if(index>=0)data.accounts[index]=result.account;else data.accounts.push(result.account);
    render();
   }else await load();
   toast('已保存，两个页面已同步');
  }else{await api('/api/account/create',{...values,office_group:$('editOfficeGroup').value,request_id:claimRequestId(requestKey)});settleRequestId(requestKey);$('editor').close();await load();toast('已保存，两个页面已同步');}
 }catch(error){settleRequestId(requestKey,error);$('editError').textContent=error.message;}finally{button.disabled=false;}
};
accountSearchFields.forEach(id=>{$(id).oninput=()=>{activeLifecyclePreset='';if(window.AccountPilot?.enabled())window.AccountPilot.scheduleFilter();else render();};$(id).onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();activeLifecyclePreset='';window.AccountPilot?.cancelFilter();render();}};});['computer','status','published','loginPhoneFilter','verificationFilter'].forEach(id=>$(id).onchange=render);$('date').onchange=()=>{selected.clear();if(data)return loadPublished($('date').value);};$('today').onclick=()=>{if(!data)return;$('date').value=data.today;return loadPublished(data.today);};$('refresh').onclick=load;$('clear').onclick=()=>{window.AccountPilot?.resetFilters();clearAccountSearch();activeLifecyclePreset='';['query','computer','status','published','accountType','lifecycleFilter','ageFilter','loginPhoneFilter'].forEach(id=>$(id).value='');selected.clear();render();};async function refreshFromFocus(){if(!data)return;if(window.boardLiveSync)return;const editing=['editor','cardEditor','phoneDeviceEditor','publishingMatchDialog','verificationEditor','batchEditor','employeeCardLinkDialog'].some(id=>$(id)?.open);if(Number.isInteger(data.cursor)&&typeof catchUpBoard==='function'){await catchUpBoard();return;}if(!editing)await load();}
window.addEventListener('focus',()=>{const job=refreshFromFocus();job.catch(()=>{});return job;});openBoard();

function renderBoardCollectionInfo(){
 const collection=data?.collection;
 if(!collection)return;
 $('collectionInfo').textContent=collection.enabled!==true?'采集暂未启用，历史记录可查看':
   `${collection.worker_online?'采集器在线':'采集器离线'} · ${collection.paused?'队列已暂停':collection.waiting+' 个待处理'} · ${collection.failed} 个失败或待验证`;
}
let collectionSubmitting=false,collectionStatusRequest=null;
const collectionPendingRequests=new Map();
function refreshBoardCollection(){
 if(!data||collectionStatusRequest)return collectionStatusRequest;
 const source=data;
 collectionStatusRequest=fetch('/api/collection/status',{cache:'no-store',signal:globalThis.AbortSignal?.timeout?.(8000)})
   .then(async response=>response.ok?response.json():null).then(result=>{
     if(!result?.collection||data!==source)return;
     data.collection=result.collection;renderBoardCollectionInfo();applyCollectionPermissions();
   }).catch(()=>{}).finally(()=>{collectionStatusRequest=null;});
 return collectionStatusRequest;
}
async function queue(ids,trigger){
 if(collectionSubmitting)return;
 const feedback=trigger?.closest('dialog[open]')?.querySelector('#accountPilotCollectionFeedback');
 const show=message=>{if(feedback)feedback.textContent=message;else toast(message);};
 ids=[...new Set(ids)].sort();
 if(!ids.length)return show('没有可采集的账号');
 if(ids.length>2000)return show('每次最多采集 2000 个账号，请缩小筛选范围');
 const key=JSON.stringify(ids);
 if(!collectionPendingRequests.has(key))collectionPendingRequests.set(key,newRequestId());
 collectionSubmitting=true;
 try{
  const result=await api('/api/collection/enqueue',{ids,request_id:collectionPendingRequests.get(key)},{fixedPayload:true});
  collectionPendingRequests.delete(key);
  const parts=[`已提交 ${result.queued} 个`];
  if(result.duplicate)parts.push(`${result.duplicate} 个已在队列`);
  if(result.missing)parts.push(`${result.missing} 个缺少抖音号或主页`);
  if(result.invalid)parts.push(`${result.invalid} 个主页格式待核对`);
  show(parts.join(' · '));
 }catch(error){if(error.status!==undefined)collectionPendingRequests.delete(key);show(error.message);}
 finally{collectionSubmitting=false;void refreshBoardCollection();}
}
$('collectVisible').onclick=()=>queue(filtered().filter(a=>page!=='board'||a.computer_id!=='unassigned').map(a=>a.id));$('collectSelected').onclick=()=>queue([...selected]);
document.addEventListener('toggle',event=>{if(event.target.open&&event.target.contains($('collectionBar')))void refreshBoardCollection();},true);
window.addEventListener('focus',()=>{if(['board','accounts'].includes(page))void refreshBoardCollection();});
{const boardUpdates=document.createElement('script');boardUpdates.src='/board-updates.js?v=20261104-pilot1';boardUpdates.onload=()=>{if(typeof startBoardLiveSync==='function')startBoardLiveSync();};document.body.appendChild(boardUpdates);}
