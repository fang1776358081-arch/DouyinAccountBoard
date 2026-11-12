const backupTableNames={computers:'发布电脑资料',accounts:'抖音账号',daily:'每日发布记录',imports:'导入标记',collection_jobs:'采集任务（恢复时清空）',collection_settings:'采集设置（恢复后暂停）',phone_cards:'手机卡',phone_devices:'手机设备',publishing_layout:'发布格子',publishing_layout_state:'发布安排批次',publishing_layout_links:'手动关联记录'};
function entityTitle(entity){const parts=entity.split(':');if(['account','nurture','daily'].includes(parts[0])){const account=data.accounts.find(a=>a.id===parts[1]);const title=account?accountDisplay(account):'历史账号';return title+(parts[0]==='nurture'?' · 养号记录':parts[0]==='daily'?' · '+parts[2]+' 发布记录':'');}if(parts[0]==='card'){const card=data.phone_cards.cards.find(c=>String(c.id)===parts[1]);return card?'手机卡 '+card.phone:'历史手机卡';}if(parts[0]==='device')return '手机设备 '+parts.slice(1).join(':');if(parts[0]==='layout'){const slot=parts.pop();let computer=parts.pop();if(parts.at(-1)==='office3')computer='office3:'+computer;return '发布格 '+hostName(computer)+' / 设备'+slot;}return '业务资料';}
function historyChoices(){const type=$('historyType').value;const items=type==='account'?data.accounts:type==='card'?data.phone_cards.cards:data.phone_devices.devices;$('historyEntity').innerHTML='<option value="">全部记录</option>'+items.map(item=>`<option value="${esc(item.id)}">${esc(type==='account'?accountDisplay(item):type==='card'?(item.phone+' · '+(item.label||'')):item.name)}</option>`).join('');}
function historyValue(field,value){if(['account_id','parent_account_id'].includes(field)&&value){const account=data.accounts.find(a=>a.id===String(value));return account?accountDisplay(account):'历史账号';}return value??'空';}
function actionTitle(path){return ({'/api/batch-edit':'批量修改业务资料','/api/workspace/nurture/join':'加入养号','/api/workspace/nurture/start':'确认开始养号','/api/workspace/nurture/verification':'记录核对情况','/api/workspace/nurture/pause':'暂停养号','/api/workspace/nurture/resume':'继续养号','/api/workspace/nurture/upload':'保存养号截图','/api/workspace/nurture/delete-image':'移除养号截图','/api/workspace/nurture/finish':'结束养号并转正常','/api/workspace/nurture/purge':'清理养号截图','/api/maintenance/reimport':'腾讯文档重新导入','/api/account':'修改账号资料','/api/account/create':'新增账号','/api/account/status':'修改账号状态','/api/bulk-status':'批量修改账号状态','/api/daily':'记录当天发布','/api/phone-card':'保存手机卡资料 / 待办','/api/phone-device':'修改手机设备','/api/phone-device/create':'新增手机设备','/api/account/phone-device':'调整登录手机','/api/account/card-readiness':'修改稳定 / 改密记录','/api/lifecycle-status':'修改小号注册进度','/api/account/nurture':'修改养号开始日期','/api/publishing/link':'关联发布格子','/api/publishing/assignment':'调整实际发布位置','/api/workspace/import-apply':'导入确认的资料','/api/workspace/restore-apply':'恢复备份业务资料','/api/maintenance/replace-collector':'更换采集工具并清理旧记录','/api/collection/import-report':'导入作品采集结果','/api/collection/result':'更新公开作品采集结果','/api/collection/enqueue':'安排作品采集任务'})[path]||'更新业务资料';}
const $=id=>document.getElementById(id),esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const workspaceTabs=new Set(['nurture','todo','accounts','positions','history','exchange','backups','review']);
let data=null,tab=workspaceTabs.has(new URLSearchParams(location.search).get('tab'))?new URLSearchParams(location.search).get('tab'):'todo',todoKind='',listPage=1,importPreview=null,restorePreview=null,positionCell=null,historyBefore=0;
const detailTabs=new Set(['nurture','accounts','positions','history','exchange']);
const workspaceTitles={todo:'今日工作待办',nurture:'7日养号',accounts:'账号关联总览',positions:'发布位置',history:'操作历史',exchange:'资料导入 / 导出',backups:'备份与恢复',review:'资料待核对'};
let reviewPage=1,reviewFilters={office:new URLSearchParams(location.search).get('office')||'all',kind:'',query:''};
const initialWorkspaceTab=tab;
let boardRequestSerial=0,todoPages={},historyPageBefore=0,historyRenderSerial=0;
let workspaceMutationEpoch=0,workspaceReads=0,workspaceWrites=0,workspaceEditLoading=0,workspaceSyncRestore=null,workspaceSyncFlight=null,workspaceTypingUntil=0,workspaceRestoringFocus=false;
const frozenWorkspaceVersions=new WeakSet();
const workspaceSyncFieldIds=['todoOffice','todoFilter',...['accountList','positionList'].flatMap(prefix=>['Office','Phone','Id','Name','Dy'].map(key=>prefix+key)),'nurtureOffice','nurturePhone','nurtureName','nurtureOwner','nurtureStage','nurtureVerification','historyType','historyEntity'];
function workspaceDialogOpen(exceptOverview=false){return !!window.DeviceWorkflow?.isOpen()||['overviewDialog','positionDialog','nurtureDialog','batchEditor','deviceWorkflowDialog'].some(id=>!(exceptOverview&&id==='overviewDialog')&&!!$(id)?.open)||[...document.querySelectorAll('dialog[open]')].some(dialog=>!exceptOverview||dialog.id!=='overviewDialog');}
function workspaceSafeFilter(field){return !!(field?.id&&[...workspaceSyncFieldIds,'workDate','reviewOffice','reviewKind','reviewQuery'].includes(field.id)||field?.type==='checkbox'&&field.dataset?.batchPick);}
function workspaceSyncBlocked(){const active=document.activeElement,editing=active&&['INPUT','TEXTAREA','SELECT'].includes(active.tagName);return workspaceDialogOpen()||!!(editing&&(!workspaceSafeFilter(active)||Date.now()<workspaceTypingUntil))||workspaceWrites>0||workspaceEditLoading>0||pendingRequestIds.size>0||tab==='exchange'||tab==='backups';}
function workspaceViewState(){const active=document.activeElement,focus=workspaceSafeFilter(active)?{id:active.id,batchKind:active.dataset?.batchPick,value:active.value,start:active.selectionStart,end:active.selectionEnd,direction:active.selectionDirection}:null;return {focus,epoch:workspaceMutationEpoch,fields:Object.fromEntries(workspaceSyncFieldIds.map(id=>[id,$(id)?.value])),listPage,todoPages:{...todoPages},reviewPage,historyPageBefore,x:window.scrollX||0,y:window.scrollY||0,scroll:[...document.querySelectorAll('.account-scroll')].map(node=>({left:node.scrollLeft,top:node.scrollTop})),details:[...document.querySelectorAll('#workspaceBody details[id]')].map(node=>({id:node.id,open:node.open}))};}
function restoreWorkspaceSyncFields(ids=workspaceSyncFieldIds){if(!workspaceSyncRestore)return;for(const id of ids){const value=workspaceSyncRestore.fields[id],field=$(id);if(field&&value!==undefined){field.value=value;if(field.tagName==='SELECT'&&field.value!==value)field.selectedIndex=0;}}}
async function renderPreservingWorkspaceView(saved){workspaceSyncRestore=saved;listPage=saved.listPage;todoPages={...saved.todoPages};reviewPage=saved.reviewPage;try{await render();}finally{workspaceSyncRestore=null;}if(workspaceMutationEpoch!==saved.epoch)return;if(saved.focus){const field=saved.focus.id?$(saved.focus.id):[...document.querySelectorAll('[data-batch-pick]')].find(node=>node.dataset.batchPick===saved.focus.batchKind&&node.value===saved.focus.value);if(field?.focus){workspaceRestoringFocus=true;try{field.focus({preventScroll:true});if(Number.isInteger(saved.focus.start)&&field.setSelectionRange)field.setSelectionRange(saved.focus.start,saved.focus.end,saved.focus.direction);}finally{workspaceRestoringFocus=false;}}}for(const [index,node] of [...document.querySelectorAll('.account-scroll')].entries()){if(saved.scroll[index]){node.scrollLeft=saved.scroll[index].left;node.scrollTop=saved.scroll[index].top;}}for(const row of saved.details)if($(row.id))$(row.id).open=row.open;window.scrollTo?.(saved.x,saved.y);}
function freezeWorkspaceVersions(id){if(typeof capturedVersions!=='undefined'&&capturedVersions.has(id))frozenWorkspaceVersions.add(capturedVersions.get(id));}
async function prepareWorkspaceEdit(open){
 if(workspaceEditLoading||workspaceDialogOpen(true))return false;
 const selectedTab=tab,day=$('workDate').value,epoch=++workspaceMutationEpoch,saved=workspaceViewState();workspaceEditLoading++;
 const valid=()=>tab===selectedTab&&$('workDate').value===day&&workspaceMutationEpoch===epoch&&!workspaceDialogOpen(true);
 try{if(!await loadBoard(true,false,true,{valid})||!valid())return false;await renderPreservingWorkspaceView(saved);if(!valid())return false;open();return true;}
 finally{workspaceEditLoading--;}
}
let officeNames={all:'全部办公室',one:'办公室一',two:'办公室二',three:'办公室三',pending:'待分配'};
const labels={verification_state:'核对情况',verification_owner:'核对负责人',verification_note:'核对处理备注',verification_updated_on:'最后处理日期',workflow:'计时状态',banked_days:'已累计养号天数',active_since:'本段开始日期',paused_on:'暂停日期',periods:'实际养号区间',legacy_started_on:'旧版参考日期',started_on:'养号开始日期',completed_on:'养号结束日期',image_count:'本轮截图数量',cycle:'养号轮次',revision:'记录版本',id:'内部编号',name:'昵称',status:'状态',note:'备注',registration_phone:'注册手机号',phone_device_id:'登录手机',computer_id:'资料登记电脑',slot:'位置',published:'当天发布',day:'日期',account_id:'账号',nurture_started:'养号开始日期',account_type:'账号类型',parent_account_id:'关联大号',registered_on:'注册日期',auxiliary_state:'小号注册状态',card_stability:'稳定状态',password_state:'改密状态',phone:'手机卡号码',owner:'卡主',state:'设备状态',location:'位置',label:'原安排文字',code_state:'接码状态',return_state:'归还状态',source_cell:'原表位置',reason:'待匹配原因',office:'办公室分组',batch:'发布安排批次',douyin_id:'真实抖音号',homepage:'公开主页'};
const can=key=>data?.user?.permissions.includes(key),hostName=id=>data.computers.find(host=>host.id===id)?.name||id;
const pendingRequestIds=new Map();
function newRequestId(){if(typeof crypto.randomUUID==='function')return crypto.randomUUID();const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);bytes[6]=bytes[6]&15|64;bytes[8]=bytes[8]&63|128;let hex='';for(let i=0;i<16;i++)hex+=bytes[i].toString(16).padStart(2,'0');return hex.slice(0,8)+'-'+hex.slice(8,12)+'-'+hex.slice(12,16)+'-'+hex.slice(16,20)+'-'+hex.slice(20);}
function claimRequestId(key){if(!pendingRequestIds.has(key))pendingRequestIds.set(key,newRequestId());return pendingRequestIds.get(key);}
function settleRequestId(key,error){if(error&&error.status===undefined)return;pendingRequestIds.delete(key);}
async function ensureVersions(path,body,source){if(!data||source&&frozenWorkspaceVersions.has(source))return;const keys=[...new Set(versionKeys(path,body,data.accounts||[],data))];const target=source||data.record_versions||{};const missing=keys.filter(key=>!Object.prototype.hasOwnProperty.call(target,key));for(let offset=0;offset<missing.length;offset+=80){const response=await fetch('/api/record-versions?keys='+encodeURIComponent(missing.slice(offset,offset+80).join(',')));if(response.status===401){location.replace('/login.html');const failure=Error('请重新登录');failure.status=401;throw failure;}const result=await response.json().catch(()=>({}));if(!response.ok){const failure=Error(result.error||'读取保存版本失败');failure.status=response.status;throw failure;}const versions=result.record_versions||{};if(source)Object.assign(source,versions);data.record_versions={...(data.record_versions||{}),...versions};}}
async function sendOnce(path,body,source){if(body)await ensureVersions(path,body,source);const response=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-CSRF-Token':data?.user?.csrf||''}:{},body:body?JSON.stringify(versionPayload(path,body,data,source)):undefined});const result=await response.json();if(response.status===401){location.replace('/login.html');const failure=Error('请重新登录');failure.status=401;throw failure;}if(!response.ok){const failure=Error(result.error||'操作失败');failure.status=response.status;failure.code=result.code;throw failure;}if(data)applyVersionDelta(result._record_versions,data);return result;}
async function send(path,body,source){if(body){workspaceMutationEpoch++;workspaceWrites++;}try{return await sendOnce(path,body,source);}catch(error){if(error.status!==undefined||!body?.request_id)throw error;try{return await sendOnce('/api/idempotency/result?request_id='+encodeURIComponent(body.request_id));}catch(queryError){if(queryError.status===undefined||queryError.code==='REQUEST_PENDING'||queryError.status===404){const pending=Error('结果待确认');throw pending;}throw queryError;}}finally{if(body)workspaceWrites--;}}
async function loadBoard(detail,allowWorkflowReload=false,workflowVersions=false,guard=null){
 if(!allowWorkflowReload&&window.DeviceWorkflow?.isOpen())return false;
 const serial=++boardRequestSerial,params=new URLSearchParams();if($('workDate').value)params.set('date',$('workDate').value);if(!workflowVersions)params.set('view',detail?'detail':'work');else params.set('version_scope','day');
 const current=()=>serial===boardRequestSerial&&(!guard?.valid||guard.valid());if(!guard?.background)workspaceReads++;
 try{
  const response=await fetch('/api/board?'+params,guard?.signal?{signal:guard.signal}:undefined);if(!current())return false;
  if(response.status===401){location.replace('/login.html');return false;}if(!response.ok)throw Error('读取资料失败');
  const next=await response.json();if(!current()||!allowWorkflowReload&&window.DeviceWorkflow?.isOpen())return false;
  if(!next||!Array.isArray(next.accounts)||!Array.isArray(next.computers)||!next.user||!next.publishing_layout||!next.nurture)throw Error('资料响应格式不正确');
  if(guard?.background&&!Number.isInteger(next.cursor))throw Error('资料响应缺少同步记号');
  if(workflowVersions)next.workspace_view='full';data=next;if(data.user.scope_mode)officeNames={all:'全部授权办公室',...Object.fromEntries((data.offices||[]).map(o=>[o.id,o.name])),...(data.user.scope_mode==='all'?{pending:'待分配'}:{})};$('workDate').value=data.date;$('workspaceUser').textContent=data.user.name+' · 发布按所选日期，其他待办按当前资料计算；页面不自动覆盖正在编辑的内容。';$('workspaceError').textContent='';return true;
 }catch(error){if(!current())return false;throw error;}finally{if(!guard?.background)workspaceReads--;}
}
async function reload(allowWorkflowReload=false){const workflowVersions=allowWorkflowReload===true&&!!window.DeviceWorkflow?.isOpen();if(await loadBoard(detailTabs.has(tab),allowWorkflowReload,workflowVersions)){await render();return true;}return false;}
async function ensureDetail(){if(['detail','full'].includes(data?.workspace_view))return true;return loadBoard(true);}
function workspaceSyncFailure(status,message){const error=Error(message);if(status!==undefined)error.status=status;return error;}
function workspaceDailyChange(change,state){
 if(change?.entity?.startsWith('account-source:'))return 'skip';
 if(!change||typeof change.entity!=='string'||!change.entity.startsWith('daily:')||tab==='history')return 'reload';
 const rest=change.entity.slice(6),split=rest.lastIndexOf(':'),id=rest.slice(0,split),day=rest.slice(split+1);
 if(split<1||!/^\d{4}-\d{2}-\d{2}$/.test(day))return 'reload';
 if(day!==state.date)return 'skip';
 return change.after&&[true,false,0,1].includes(change.after.published)&&(state.accounts||[]).some(account=>String(account.id)===id)?'patch':'reload';
}
async function syncWorkspace(options={}){
 if(workspaceSyncFlight&&!workspaceSyncFlight.signal?.aborted)return workspaceSyncFlight.promise;
 if(!data||workspaceReads)return {status:'waiting'};
 if(!Number.isInteger(data.cursor))return {status:'unsupported'};
 if(workspaceSyncBlocked())return {status:'deferred'};
 const state=data,requested=state.cursor,epoch=workspaceMutationEpoch,selectedTab=tab,day=$('workDate').value;let serial=boardRequestSerial;
 const flight={signal:options.signal,promise:null};workspaceSyncFlight=flight;
 const current=()=>workspaceSyncFlight===flight&&!options.signal?.aborted&&data===state&&state.cursor===requested&&boardRequestSerial===serial&&workspaceMutationEpoch===epoch&&tab===selectedTab&&$('workDate').value===day&&!workspaceSyncBlocked()&&(!options.isCurrent||options.isCurrent());
 const stale=()=>({status:workspaceSyncBlocked()||workspaceMutationEpoch!==epoch?'deferred':'waiting'});
 flight.promise=(async()=>{
  const response=await fetch('/api/board-updates?cursor='+requested+'&limit=200',options.signal?{signal:options.signal}:undefined);
  if(!current())return stale();
  if(response.status===401){location.replace('/login.html');throw workspaceSyncFailure(401,'登录已失效，请重新登录');}
  if(response.status===404)return {status:'unsupported'};
  if(!response.ok)throw workspaceSyncFailure(response.status,'读取更新失败');
  const result=await response.json();if(!current())return stale();
  if(!result||!Number.isInteger(result.cursor)||result.cursor<requested||result.reload!==true&&(!Array.isArray(result.changes)||result.changes.some(change=>!change||typeof change.entity!=='string')))throw workspaceSyncFailure(undefined,'更新响应格式不正确');
  if(result.reload===true||result.changes.some(change=>workspaceDailyChange(change,state)==='reload')){
   const saved=workspaceViewState();serial=boardRequestSerial+1;
   if(!await loadBoard(detailTabs.has(tab),false,false,{background:true,signal:options.signal,valid:current}))return stale();
   await renderPreservingWorkspaceView(saved);return {status:'updated',changes:result.changes?.length||1};
  }
  const changes=result.changes.filter(change=>workspaceDailyChange(change,state)==='patch');
  for(const change of changes){const rest=change.entity.slice(6),id=rest.slice(0,rest.lastIndexOf(':')),account=state.accounts.find(account=>String(account.id)===id);account.published=!!change.after.published;if(change.record_version!=null)state.record_versions={...(state.record_versions||{}),[change.entity]:change.record_version};}
  state.cursor=result.cursor;
  if(changes.length)await renderPreservingWorkspaceView(workspaceViewState());
  return {status:changes.length?'updated':'idle',changes:changes.length};
 })().finally(()=>{if(workspaceSyncFlight===flight)workspaceSyncFlight=null;});return flight.promise;
}
function startWorkspaceLiveSync(){
 if(!window.LiveSync||window.workspaceLiveSync)return;
 const controller=window.LiveSync.create({ready:()=>!!data&&!workspaceReads,blocked:workspaceSyncBlocked,run:syncWorkspace,onState:state=>window.LiveSync.status('workSyncState',state),intervalMs:new URLSearchParams(location.search).get('office')==='three'?12000:5000});window.workspaceLiveSync=controller;
 const retry=$('workSyncRetry');if(retry)retry.onclick=()=>controller.poke();
 for(const type of ['input','change','click','focusin'])document.addEventListener(type,event=>{if(workspaceRestoringFocus)return;workspaceMutationEpoch++;if(type==='input'&&workspaceSafeFilter(event.target)&&event.target.tagName!=='SELECT'&&event.target.type!=='checkbox')workspaceTypingUntil=Date.now()+1000;},true);
 controller.start();
}
function table(headers,rows){return `<div class="account-scroll"><table class="workspace-table"><thead><tr>${headers.map(header=>`<th>${header}</th>`).join('')}</tr></thead><tbody>${rows.join('')||`<tr><td colspan="${headers.length}">暂无符合条件的记录</td></tr>`}</tbody></table></div>`;}
function officeSelect(id){const initial=new URLSearchParams(location.search).get('office');return `<label>办公室<select id="${id}">${Object.entries(officeNames).map(([key,value])=>`<option value="${esc(key)}" ${initial===key?'selected':''}>${esc(value)}</option>`).join('')}</select></label>`;}
function input(id,label,hint=''){return `<label>${label}<input id="${id}" placeholder="${hint}" autocomplete="off"></label>`;}
function fail(error){$('workspaceError').textContent=error.message||String(error);}
function refreshWorkspaceNavigation(){
 const officeField={todo:'todoOffice',nurture:'nurtureOffice',accounts:'accountListOffice',positions:'positionListOffice',review:'reviewOffice'}[tab];
 const office=(officeField&&$(officeField)?.value)||new URLSearchParams(location.search).get('office');
 window.WorkbenchNav?.update({tab,user:data?.user,office});
}
function updateTodoWorkspace(){
 const todo=tab==='todo',extra=$('todoExtraActions');
 document.body.classList.toggle('todo-console',todo);
 document.body.dataset.workspaceView=tab;
 $('workDate').hidden=!['todo','accounts','positions','exchange'].includes(tab);
 if(!extra)return;
 const collection=$('workCollectionInfo').closest('.collection-bar');
 if(collection.parentElement!==extra)extra.append(collection);
 extra.hidden=!todo;
 if(data)$('workspaceUser').textContent=data.user.name;
}
document.addEventListener('change',event=>{
 if(['nurtureOffice','accountListOffice','positionListOffice','reviewOffice'].includes(event.target.id))refreshWorkspaceNavigation();
});

function render(){updateTodoWorkspace();const title=workspaceTitles[tab]||'今日工作与管理';if($('workspaceTitle'))$('workspaceTitle').textContent=title;if($('workspaceBreadcrumb'))$('workspaceBreadcrumb').textContent=title;document.title=title+' · 账号工作台';window.WorkbenchNav?.update({tab,user:data?.user});if(!data){$('workspaceBody').innerHTML='<p>正在读取共享资料，请稍候；加载失败时可点击“刷新资料”重试。</p>';return false;}if(detailTabs.has(tab)&&!['detail','full'].includes(data.workspace_view)){const renderTab=tab;$('workspaceBody').innerHTML='<p>正在读取完整资料，请稍候…</p>';return ensureDetail().then(loaded=>{if(loaded&&tab===renderTab)return render();});}$('workspaceError').textContent='';const result=({nurture:renderNurture,todo:renderTodo,accounts:renderAccounts,positions:renderPositions,history:renderHistory,exchange:renderExchange,backups:renderBackups,review:renderReview}[tab])();refreshWorkspaceNavigation();return result;}
function taskAction(task){
 const link=(params,label)=>`<a href="/?${esc(new URLSearchParams(params).toString())}">${label}</a>`;
 if(task.kind==='device_case')return `<button type="button" class="text-button" data-workflow-case="${esc(task.case_id)}">${can('device_manage')?'处理故障':'查看故障影响'}</button>`;
 if(task.kind==='account_recovery')return `<button type="button" class="text-button" data-workflow-account="${esc(task.account_id)}">${can('device_manage')?'登录恢复':'查看恢复'}</button>`;
 if(task.kind==='publish'||task.kind==='abnormal'){
  const label=task.kind==='abnormal'?'复核发布格':can('publish')?'打开发布格':'查看发布格';
  const location=task.computer_id&&task.slot?{target_computer:task.computer_id,target_slot:task.slot}:{};
  return link({page:'board',account_id:task.account_id,todo_action:task.kind,work_date:data.date,...location},label);
 }
 if(task.kind.startsWith('nurture')&&task.kind!=='nurture_schedule'){
  const hasRecord=(data.nurture?.records||[]).some(record=>record.account_id===task.account_id&&!record.completed_on);
  if(!hasRecord)return `<button type="button" class="text-button" data-overview="${esc(task.account_id)}">查看账号与养号</button>`;
  return `<button type="button" class="text-button" data-nurture="${esc(task.account_id)}">${can('nurture_manage')?'处理养号':'查看养号'}</button>`;
 }
 if(task.account_id)return link({page:'accounts',account_id:task.account_id,todo_action:task.kind},'查看账号');
 if(task.card_id){
  const label=can('card_tasks')&&({code:'处理接码',return:'处理归还',cancel:'核对注销'})[task.kind]||'查看手机卡';
  return link({page:'cards',card_id:task.card_id,todo_action:task.kind},label);
 }
 if(task.device_id)return link({page:'phones',device_id:task.device_id,todo_action:task.kind},'核对手机清理');
 return link({match_computer:task.computer_id,match_slot:task.slot,todo_action:'match',work_date:data.date},can('publishing_manage')?'打开候选账号':'查看待关联格');
}

const todoTitles={device_case:'设备故障处理',account_recovery:'账号登录恢复',publish:'待发布',aux:'满30天待开小号',nurture:'养号到期',nurture_missing:'养号日期待补',match:'发布格待匹配',code:'待接码',return:'待归还',cancel:'卡待注销记录',clean:'手机待清理',abnormal:'异常安排复核',nurture_schedule:'养号结束待安排',nurture_check:'养号待检查',nurture_verification:'核对处理中',nurture_start:'待开始养号',nurture_paused:'养号暂停'};
const todoGroupTitles={due:'待处理',exceptions:'异常待核对',other:'其他事项'};
function todoTaskGroup(task){
 if(task.due_on&&task.due_on>data.today)return 'other';
 if(['publish','code','nurture'].includes(task.kind)||(task.kind==='return'&&task.due_on))return 'due';
 if(['abnormal','match','device_case','account_recovery'].includes(task.kind))return 'exceptions';
 return 'other';
}

function renderTodo(){
 const tasks=workspaceTodos(data);
 const previousOffice=$('todoOffice')?.value;
 let otherOpen=!!$('todoOtherTasks')?.open;
 $('workspaceBody').innerHTML=`<div class="work-filters todo-toolbar">${officeSelect('todoOffice')}
  <label>事项<select id="todoFilter"><option value="">全部事项</option>${Object.entries(todoTitles).map(([kind,title])=>`<option value="${kind}">${title}</option>`).join('')}</select></label>
  <span id="todoResultCount" class="todo-result-count" role="status"></span>
  ${data.date!==data.today?`<span class="todo-date-note">发布记录日期 ${esc(data.date)}</span>`:''}
 </div><div id="todoRows"></div>`;
 if(previousOffice&&Object.hasOwn(officeNames,previousOffice))$('todoOffice').value=previousOffice;
 $('todoFilter').value=todoKind;
 restoreWorkspaceSyncFields();
 const show=()=>{
  const office=$('todoOffice').value,kind=$('todoFilter').value;
  const groups={exceptions:[],due:[],other:[]};
  for(const task of tasks){
   if((!kind||task.kind===kind)&&(office==='all'||task.office===office||task.offices?.includes(office)))groups[todoTaskGroup(task)].push(task);
  }
  const total=Object.values(groups).reduce((sum,rows)=>sum+rows.length,0);
  const pageSize=12;
  $('todoResultCount').textContent=`${total} 条事项`;
  otherOpen=$('todoOtherTasks')?.open??otherOpen;
  const content=Object.keys(groups).map(group=>{
   const count=groups[group].length;
   if(kind&&!count)return '';
   if(group==='other'&&!count)return '';
   const heading=`${todoGroupTitles[group]} <span class="todo-section-count">${count}</span>`;
   const pages=Math.max(1,Math.ceil(count/pageSize));
   const page=Math.min(pages,Math.max(1,todoPages[group]||1));
   todoPages[group]=page;
   const visibleRows=groups[group].slice((page-1)*pageSize,page*pageSize);
   const empty=group==='due'?'当前没有待处理事项。':'当前没有异常待核对。';
   const pagination=pages>1?`<div class="pagination todo-pagination" aria-label="${todoGroupTitles[group]}分页"><button type="button" class="button" data-todo-page="-1" ${page===1?'disabled':''}>上一页</button><span>${page} / ${pages} 页</span><button type="button" class="button" data-todo-page="1" ${page===pages?'disabled':''}>下一页</button></div>`:'';
   const content=visibleRows.length?renderTodoTable(visibleRows,office)+pagination:`<p class="todo-empty">${empty}</p>`;
   if(group==='other')return `<details class="todo-other-tasks" id="todoOtherTasks" data-todo-group="other" ${kind||otherOpen?'open':''}><summary>${heading}</summary>${content}</details>`;
   return `<section class="todo-section" data-todo-group="${group}"><h2 class="todo-section-head" tabindex="-1">${heading}</h2>${content}</section>`;
  }).join('');
  $('todoRows').innerHTML=content||'<p class="todo-empty">暂无符合筛选条件的事项。</p>';
 };
 $('todoRows').onclick=event=>{
  const button=event.target.closest('[data-todo-page]');
  if(!button||button.disabled)return;
  const group=button.closest('[data-todo-group]').dataset.todoGroup;
  const step=button.dataset.todoPage;
  todoPages[group]+=Number(step);show();
  const section=$('todoRows').querySelector(`[data-todo-group="${group}"]`);
  (section?.querySelector(`[data-todo-page="${step}"]:not(:disabled)`)||section?.querySelector('h2, summary'))?.focus();
 };
 $('todoFilter').onchange=()=>{todoKind=$('todoFilter').value;todoPages={};show();};
 $('todoOffice').onchange=()=>{todoPages={};show();refreshWorkspaceNavigation();};
 show();
}

function renderTodoTable(tasks,office){
 return `<div class="todo-table">${table(['对应资料','事项','原因 / 日期','处理'],tasks.map(task=>{
  const officeId=office==='all'?task.office:office;
  const location=[officeNames[officeId]||'待核对',task.position_label].filter(Boolean).join(' · ');
  return `<tr><td class="todo-record">${esc(task.name)}<small>${esc(location)}</small></td><td>${esc(task.title)}</td><td>${esc(task.reason)}</td><td class="todo-action">${taskAction(task)}</td></tr>`;
 }))}</div>`;
}
function accountFilters(prefix){return `${officeSelect(prefix+'Office')}${input(prefix+'Phone','登录手机序号','精确序号')}<input type="hidden" id="${prefix}Id" value="">${input(prefix+'Name','昵称','只搜索昵称')}${input(prefix+'Dy','真实抖音号','完整抖音号')}`;}
function selectedAccounts(prefix){return data.accounts.filter(account=>($(prefix+'Office').value==='all'||workspaceOffice(account)===$(prefix+'Office').value)&&deviceNumberMatches(account.phone_device_id,searchValue(prefix+'Phone'))&&exactText(account.id,searchValue(prefix+'Id').replace(/^#/,''))&&containsText(account.name,searchValue(prefix+'Name'))&&exactText(account.douyin_id,searchValue(prefix+'Dy')));}
function renderAccounts(){
 $('workspaceBody').innerHTML=`<div class="work-filters">${accountFilters('accountList')}<button class="button" id="exportAccountList" ${can('data_export')?'':'hidden'}>导出筛选结果</button></div><div id="accountListRows"></div>`;
 const show=()=>{const accounts=selectedAccounts('accountList');const cellsByAccount=new Map();for(const cell of data.publishing_layout.cells){if(!cell.account_id)continue;if(!cellsByAccount.has(cell.account_id))cellsByAccount.set(cell.account_id,[]);cellsByAccount.get(cell.account_id).push(cell);}listPage=Math.min(Math.max(1,listPage),Math.max(1,Math.ceil(accounts.length/50)));$('accountListRows').innerHTML=table(['账号','手机 / 注册卡','资料登记电脑','实际发布位置','状态 / 注册进度','操作'],accounts.slice((listPage-1)*50,listPage*50).map(account=>{const cells=cellsByAccount.get(account.id)||[];return `<tr><td>${esc(accountDisplay(account))}<small>${esc(account.douyin_id||'抖音号待补充')}</small></td><td>${esc(account.phone_device_name||'登录手机未确认')}<small>${esc(account.registration_phone||'注册手机号未填')}</small></td><td>${esc(hostName(account.computer_id))}</td><td>${cells.map(cell=>`${esc(hostName(cell.computer_id))} / 设备${cell.slot} / ${esc(cell.source_cell)}`).join('<br>')||'没有发布安排'}</td><td>${esc(account.status)}<small>${esc(account.lifecycle||account.account_type)}</small></td><td><button class="text-button" data-overview="${esc(account.id)}">查看关联</button></td></tr>`;}))+`<div class="pagination"><button id="accountPrevious" class="button" ${listPage<=1?'disabled':''}>上一页</button><span>${listPage} / ${Math.max(1,Math.ceil(accounts.length/50))} 页，共 ${accounts.length} 个账号</span><button id="accountNext" class="button" ${listPage*50>=accounts.length?'disabled':''}>下一页</button></div>`;$('accountPrevious').onclick=()=>{listPage--;show();};$('accountNext').onclick=()=>{listPage++;show();};};
 ['Office','Phone','Id','Name','Dy'].forEach(key=>$( 'accountList'+key).oninput=()=>{listPage=1;show();});$('exportAccountList').onclick=()=>downloadData({kind:'accounts',ids:selectedAccounts('accountList').map(account=>account.id),format:'xlsx'}).catch(fail);restoreWorkspaceSyncFields();show();
}
function reviewOfficeOptions(){
 const options={all:'全部授权办公室'};
 for(const office of data.offices||[])if(office?.id&&office.id!=='pending'&&(!data.user?.scope_mode||data.user.scope_mode==='all'||(data.user.office_ids||[]).includes(office.id)))options[office.id]=office.name||office.id;
 if(data.user?.scope_mode==='all')options.pending='待分配';
 return options;
}
function reviewOffices(row){return Array.isArray(row.office_ids)?row.office_ids.filter(id=>id&&id!=='pending'):[];}
function authorizedReviewRows(){
 const model=window.AccountReview;if(!model)return [];
 const rows=[...model.fromBoard(data),...model.importRows(data)];
 if(!data.user?.scope_mode)return rows;
 const offices=new Set(Object.keys(reviewOfficeOptions()).filter(id=>!['all','pending'].includes(id))),pending=data.user.scope_mode==='all';
 return rows.filter(row=>{const ids=reviewOffices(row);return ids.some(id=>offices.has(id))||pending&&!ids.length;});
}
function renderReview(){
 const model=window.AccountReview;
 if(!model){$('workspaceBody').innerHTML='<h2>资料待核对</h2><p role="alert">核对组件尚未加载，请刷新页面重试。</p>';return;}
 const rows=authorizedReviewRows(),offices=reviewOfficeOptions(),types=new Map(rows.map(row=>[row.kind,row.kind_label||row.kind]));
 if(!Object.prototype.hasOwnProperty.call(offices,reviewFilters.office))reviewFilters.office='all';
 if(reviewFilters.kind&&!types.has(reviewFilters.kind))reviewFilters.kind='';
 $('workspaceBody').innerHTML=`${typeof model.identityComplete==='function'&&!model.identityComplete(data)?'<p class="review-warning" role="status">抖音号检查尚未完成：当前服务未返回所需标识，请更新服务后刷新；昵称核对仍可查看。</p>':''}<div class="work-filters review-filters"><label>办公室<select id="reviewOffice">${Object.entries(offices).map(([id,name])=>`<option value="${esc(id)}">${esc(name)}</option>`).join('')}</select></label><label>问题类别<select id="reviewKind"><option value="">全部问题类别</option>${[...types].map(([kind,label])=>`<option value="${esc(kind)}">${esc(label)}</option>`).join('')}</select></label>${input('reviewQuery','查找资料','昵称、抖音号或来源行')}<button type="button" class="button" id="resetReviewFilters">重置筛选</button><button type="button" class="button" id="exportReview" ${can('data_export')?'':'disabled'}>导出当前清单（CSV）</button></div><p id="reviewSummary" role="status"></p><details class="work-explanation"><summary>核对说明</summary><p>按问题项计数，同一账号可有多个问题。来源表问题在导入预览中核对；当前清单供人工确认。</p></details><p id="reviewError" class="error-text" role="alert"></p><div id="reviewRows"></div>`;
 $('reviewOffice').value=reviewFilters.office;$('reviewKind').value=reviewFilters.kind;$('reviewQuery').value=reviewFilters.query;
 const visible=()=>{const office=$('reviewOffice').value,kind=$('reviewKind').value,q=$('reviewQuery').value.trim().toLowerCase();return rows.filter(row=>(office==='all'||office==='pending'&&!reviewOffices(row).length||reviewOffices(row).includes(office))&&(!kind||row.kind===kind)&&(!q||[row.current_name,row.source_name,row.douyin_id,row.source_row,row.source_label,row.reason].some(value=>String(value??'').toLowerCase().includes(q))));};
 const show=()=>{
  const filtered=visible(),pages=Math.max(1,Math.ceil(filtered.length/50));reviewPage=Math.min(Math.max(1,reviewPage),pages);
  const accounts=new Set(data.accounts.map(account=>String(account.id)));
  $('reviewSummary').textContent=`当前 ${filtered.length} 项待核对问题 · 全部授权资料 ${rows.length} 项；此数量不是缺失账号数。`;
  $('reviewRows').innerHTML=table(['问题类别','账号 / 来源资料','所属办公室','核对原因','定位'],filtered.slice((reviewPage-1)*50,reviewPage*50).map(row=>{
   const ids=reviewOffices(row),officeText=ids.map(id=>offices[id]).filter(Boolean).join('、')||'待分配 / 待核对';
   const source=[row.source_label,row.source_row!==undefined&&row.source_row!==null&&row.source_row!==''?'来源行 '+row.source_row:''].filter(Boolean).join(' · ');
   return `<tr><td><span class="review-kind">${esc(row.kind_label||row.kind)}</span></td><td><strong>${esc(row.current_name||row.source_name||'名称待核对')}</strong><small>${esc(row.douyin_id?'抖音号 '+row.douyin_id:'抖音号未明确')}</small>${source?`<small>${esc(source)}</small>`:''}</td><td>${esc(officeText)}</td><td>${esc(row.reason)}</td><td>${row.account_id&&accounts.has(String(row.account_id))?`<button type="button" class="text-button" data-overview="${esc(row.account_id)}">查看关联资料</button>`:'<span class="muted">按来源核对，不猜测关联</span>'}</td></tr>`;
  }))+`<div class="pagination"><button type="button" id="reviewPrevious" class="button" ${reviewPage<=1?'disabled':''}>上一页问题</button><span>第 ${reviewPage} / ${pages} 页，共 ${filtered.length} 项</span><button type="button" id="reviewNext" class="button" ${reviewPage>=pages?'disabled':''}>下一页问题</button></div>`;
  $('reviewPrevious').onclick=()=>{reviewPage--;show();};$('reviewNext').onclick=()=>{reviewPage++;show();};$('exportReview').disabled=!can('data_export')||!filtered.length;
 };
 const changed=()=>{reviewFilters={office:$('reviewOffice').value,kind:$('reviewKind').value,query:$('reviewQuery').value};reviewPage=1;show();};
 $('reviewOffice').onchange=changed;$('reviewKind').onchange=changed;$('reviewQuery').oninput=changed;
 $('resetReviewFilters').onclick=()=>{$('reviewOffice').value='all';$('reviewKind').value='';$('reviewQuery').value='';changed();};
 $('exportReview').onclick=()=>{
  if(!can('data_export')){$('reviewError').textContent='当前人员没有导出资料权限。';return;}
  const filtered=visible();if(!filtered.length)return;
  try{const csv=model.toCSV(filtered),url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'})),link=document.createElement('a');link.href=url;link.download='资料待核对-'+data.date+'.csv';link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);$('reviewError').textContent='';}catch(error){$('reviewError').textContent=error.message||'导出未完成，请重试。';}
 };
 show();
}
async function overview(id){
 const account=data.accounts.find(account=>account.id===id);if(!account)return;
 const family=[data.accounts.find(item=>item.id===account.parent_account_id),...data.accounts.filter(item=>item.parent_account_id===id)].filter(Boolean),card=data.phone_cards.cards.find(card=>card.phone===account.registration_phone),phone=data.phone_devices.devices.find(phone=>phone.id===account.phone_device_id),positions=data.publishing_layout.cells.filter(cell=>cell.account_id===id);
 $('overviewTitle').textContent=accountDisplay(account);
 $('overviewBody').innerHTML=`<div class="detail-actions"><a class="button" href="/?account_id=${encodeURIComponent(id)}">编辑账号资料</a><span>${esc(account.status)} · ${esc(officeNames[workspaceOffice(account)])}</span></div><div class="overview-grid"><article><h3>账号与大小号关系</h3><p>抖音号：${esc(account.douyin_id||'待补充')}</p><p>类型：${esc(account.account_type)}；注册日期：${esc(account.registered_on||'未知')}</p><p>注册进度：${esc(account.lifecycle||'待确认')}</p>${family.map(item=>`<p><button class="text-button" data-overview="${esc(item.id)}">${esc(accountDisplay(item))}</button> · ${esc(item.account_type)}</p>`).join('')||'<p>暂无已确认的关联大小号</p>'}</article><article><h3>登记手机与注册卡</h3><p>登记登录手机：${esc(phone?.name||'未确认')} · ${esc(phone?.state||'')}</p>${phone?`<a href="/?device_id=${encodeURIComponent(phone.id)}">处理手机资料</a>`:''}<p>注册手机号：${esc(account.registration_phone||'未填')}</p><p>卡主：${esc(card?.owner||'未登记')}；卡状态：${esc(card?.status||'未登记')}</p><p>接码：${esc(card?.code_state||'未登记')}；归还：${esc(card?.return_state||'未登记')}</p>${card?`<a href="/?card_id=${card.id}">处理手机卡</a>`:''}</article><article><h3>发布安排与作品数量</h3><p>资料登记电脑：${esc(hostName(account.computer_id))}</p><p>实际发布位置：${positions.map(cell=>esc(hostName(cell.computer_id))+' / 设备'+cell.slot+' / '+esc(cell.source_cell)).join('；')||'尚无安排'}</p><p>${esc(data.date)}：${account.published?'已记录发布':'未记录发布'}</p><p>公开作品：${esc(account.work_count??'尚未采集')}；上次采集：${esc(account.last_collected||'无成功记录')}</p><small>作品总数不能直接作为当天发布成功依据。</small></article><article><h3>养号与稳定记录</h3><p>稳定：${esc(account.card_stability)}；改密：${esc(account.password_state)}</p><p>实际养号开始：${esc(data.nurture.records.find(row=>row.account_id===id)?.started_on||'尚未确认开始')}</p>${data.nurture.records.some(row=>row.account_id===id)?`<button class="button" data-nurture="${esc(id)}">核对与养号进度</button>`:'<a href="/workspace.html?tab=nurture">到养号面板加入待检查</a>'}<p class="permission-note">注册日期不作为养号计时。请在养号面板确认可使用后开始，核对可暂停。</p><p>${esc(account.note||'')}</p></article></div><h3>该账号操作历史</h3><div id="overviewHistory">${can('history_view')?'正在读取…':'当前人员未获查看操作历史权限。'}</div>`;
 if(!$('overviewDialog').open)$('overviewDialog').showModal();
 if(data.device_workflow?.enabled&&window.DeviceWorkflow){const panel=document.createElement('article');panel.className='workflow-unit';panel.innerHTML=`<h3>登录可用情况与恢复记录</h3><p>${esc(DeviceWorkflow.accessText(account))}</p><p>${esc(account.device_access?.reason||'按实际登录情况核对，卡的位置单独记录。')}</p><button type="button" class="button" data-workflow-account="${esc(account.id)}">登录恢复 / 处理记录</button>`;$('overviewBody').querySelector('.overview-grid').append(panel);}
 if(can('history_view'))try{const result=await send('/api/workspace/history?entity='+encodeURIComponent('account:'+id));$('overviewHistory').innerHTML=historyTable(result.entries);}catch(error){$('overviewHistory').textContent=error.message;}
}
function renderPositions(){
 $('workspaceBody').innerHTML=`<div class="work-filters">${accountFilters('positionList')}<a class="button" href="/?page=board">查看待关联格</a></div><details class="work-explanation"><summary>发布位置说明</summary><p>此处调整实际发布安排。登记电脑、原表内容和历史发布记录分别保留。</p></details><div id="positionRows"></div>`;
 const show=()=>{const accounts=new Map(selectedAccounts('positionList').map(account=>[account.id,account]));const cells=data.publishing_layout.cells.map((cell,index)=>({cell,index})).filter(({cell})=>accounts.has(cell.account_id));const pages=Math.max(1,Math.ceil(cells.length/50));listPage=Math.min(listPage,pages);$('positionRows').innerHTML=table(['实际电脑 / 发布格','原安排文字','关联账号','管理'],cells.slice((listPage-1)*50,listPage*50).map(({cell,index})=>{const account=accounts.get(cell.account_id);return `<tr><td>${esc(hostName(cell.computer_id))} / 设备${cell.slot}<small>${esc(cell.source_cell)}</small></td><td>${esc(cell.label)}</td><td>${esc(accountDisplay(account))}</td><td><button class="button" data-position="${index}" ${can('publishing_manage')?'':'disabled'}>移动 / 取消安排</button></td></tr>`;}))+`<div class="pagination" ${pages===1?'hidden':''}><button id="positionPrevious" class="button" ${listPage<=1?'disabled':''}>上一页</button><span>${listPage} / ${pages} 页 · ${cells.length} 个格</span><button id="positionNext" class="button" ${listPage>=pages?'disabled':''}>下一页</button></div>`;$("positionPrevious").onclick=()=>{listPage--;show();};$("positionNext").onclick=()=>{listPage++;show();};};
 ['Office','Phone','Id','Name','Dy'].forEach(key=>$('positionList'+key).oninput=()=>{listPage=1;show();});restoreWorkspaceSyncFields();show();
}
function position(index){positionCell=data.publishing_layout.cells[index];
 const account=data.accounts.find(item=>item.id===positionCell.account_id);
 const computers=new Map(data.computers.map(computer=>[computer.id,computer]));
 const office=account?.office_ids?.length===1?account.office_ids[0]:null;
 const canMoveTo=cell=>{
  if(cell.account_id||cell.label||cell.computer_id.startsWith('office3:')!==positionCell.computer_id.startsWith('office3:'))return false;
  if(!data.user.scope_mode)return true;
  const offices=computers.get(cell.computer_id)?.office_ids;
  return office!==null&&offices?.length===1&&offices[0]===office;
 };$('positionSource').textContent=hostName(positionCell.computer_id)+' / 设备'+positionCell.slot+' / '+positionCell.label;$('positionError').textContent='';$('positionTarget').innerHTML='<option value="">请选择空白格</option>'+data.publishing_layout.cells.map((cell,index)=>canMoveTo(cell)?`<option value="${index}">${esc(hostName(cell.computer_id))} / 设备${cell.slot} / ${esc(cell.source_cell)}</option>`:'').join('');if(typeof captureVersions==='function'){captureVersions('positionDialog');freezeWorkspaceVersions('positionDialog');}$('positionDialog').showModal();}
async function savePosition(action){const target=data.publishing_layout.cells[Number($('positionTarget').value)];if(action==='move'&&!$('positionTarget').value)throw Error('请选择目标空白格');const requestKey='assignment:'+positionCell.computer_id+':'+positionCell.slot+':'+action;try{await send('/api/publishing/assignment',{batch:data.publishing_layout.batch,computer_id:positionCell.computer_id,slot:positionCell.slot,action,target_computer:action==='move'?target.computer_id:null,target_slot:action==='move'?target.slot:null,request_id:claimRequestId(requestKey)},typeof capturedVersions==='undefined'?undefined:capturedVersions.get('positionDialog'));settleRequestId(requestKey);$('positionDialog').close();await reload();}catch(error){settleRequestId(requestKey,error);throw error;}}
function historyTable(entries){return table(['时间 / 操作人','对应资料 / 操作','修改前 → 修改后'],entries.map(entry=>`<tr><td>${esc(new Date(entry.at*1000).toLocaleString('zh-CN'))}<small>${esc(entry.actor_name)}</small></td><td>${esc(entityTitle(entry.entity))}<small>${esc(actionTitle(entry.action))}</small></td><td class="history-diff">${entry.changes.filter(change=>!['id','revision'].includes(change.field)).map(change=>`<div><strong>${esc(labels[change.field]||change.field)}</strong>：<del>${esc(historyValue(change.field,change.before))}</del> → <ins>${esc(historyValue(change.field,change.after))}</ins></div>`).join('')}</td></tr>`));}
async function renderHistory(){
 if(!can('history_view')){$('workspaceBody').innerHTML='<p>没有查看操作历史权限，请联系总管理员授权。</p>';return;}
 $('workspaceBody').innerHTML=`<div class="work-filters"><label>资料类别<select id="historyType"><option value="account">账号（含发布记录）</option><option value="card">手机卡</option><option value="device">手机设备</option></select></label><label>选择对应资料<select id="historyEntity"></select></label><button id="searchHistory" class="button">查询</button></div><div id="historyRows"></div><button id="moreHistory" class="button" hidden>更早100条</button>`;
 let request=0;const renderSerial=++historyRenderSerial;
 const load=async()=>{const serial=++request,type=$('historyType').value,entity=$('historyEntity').value.trim(),before=historyBefore;try{const result=await send('/api/workspace/history?entity='+encodeURIComponent(entity?type+':'+entity.replace(/^#/, ''):'')+'&before='+before);if(tab!=='history'||renderSerial!==historyRenderSerial||serial!==request||type!==$('historyType').value||entity!==$('historyEntity').value.trim())return;$('historyRows').innerHTML=historyTable(result.entries);historyPageBefore=before;historyBefore=result.next_before;$('moreHistory').hidden=!historyBefore;}catch(error){if(tab==='history'&&renderSerial===historyRenderSerial&&serial===request)fail(error);}};
 restoreWorkspaceSyncFields(['historyType']);historyChoices();restoreWorkspaceSyncFields(['historyEntity']);
 $('historyType').onchange=()=>{historyChoices();historyBefore=0;load();};$('searchHistory').onclick=()=>{historyBefore=0;load();};$('moreHistory').onclick=load;
 historyBefore=workspaceSyncRestore?.historyPageBefore||0;await load();
}
async function downloadData(body){body={date:data.date,...body};const result=await send('/api/workspace/export',body);const raw=atob(result.content),bytes=Uint8Array.from(raw,character=>character.charCodeAt(0)),url=URL.createObjectURL(new Blob([bytes],{type:result.mime})),link=document.createElement('a');link.href=url;link.download=result.filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);}
function renderExchange(){
 $('workspaceBody').innerHTML=`<h2>单表导入 / 导出</h2><div class="work-filters"><label>资料类别<select id="exchangeKind"><option value="accounts">账号资料</option><option value="cards">手机卡资料</option><option value="devices">手机设备</option></select></label><label>导出格式<select id="exchangeFormat"><option value="xlsx">Excel（.xlsx）</option><option value="csv">CSV</option></select></label><button id="downloadTemplate" class="button" ${can('data_export')?'':'disabled'}>下载空白模板</button><button id="exportAll" class="button" ${can('data_export')?'':'disabled'}>导出此类全部资料</button></div><div class="work-filters"><label>选择文件<input id="importFile" type="file" accept=".csv,.xlsx"></label><button id="previewImport" class="button primary" ${can('data_import')?'':'disabled'}>读取文件并预览差异</button></div><p id="dataImportError" class="error-text" role="alert"></p><div id="importResult" aria-live="polite"></div><details class="work-explanation"><summary>文件格式和更新规则</summary><p>单工作表 Excel 或 CSV，最多 5,000 条、8 MB。手机号、抖音号按文本保存，日期用 YYYY-MM-DD。空白保留原值，未知列忽略。单表导入只更新资料，发布位置请使用对应的双表流程。</p></details>`;
 const scopeHost=document.createElement('div');scopeHost.id='importOfficeScope';$('importFile').parentElement.before(scopeHost);const clearImportError=()=>{$('dataImportError').textContent='';};const failImport=error=>{$('dataImportError').textContent=error.message||String(error);};const mountScope=()=>ScopeUI.mount('importOfficeScope',data,$('exchangeKind').value,{}, {importing:true});mountScope();$('exchangeKind').onchange=()=>{importPreview=null;$('importResult').textContent='';clearImportError();mountScope();};$('importFile').onchange=()=>{importPreview=null;$('importResult').textContent='';clearImportError();};
 const dualHost=document.createElement('details');dualHost.className='work-explanation';dualHost.innerHTML='<summary>办公室三双表导入与补名</summary>';const dualBody=document.createElement('div');dualHost.append(dualBody);$('workspaceBody').append(dualHost);OfficeThreeImport.mount(dualBody,{data,send,esc,table,claimRequestId,settleRequestId,canExport:()=>can('data_export')});
 $('downloadTemplate').onclick=()=>downloadData({kind:$('exchangeKind').value,format:$('exchangeFormat').value,template:true}).catch(failImport);$('exportAll').onclick=()=>downloadData({kind:$('exchangeKind').value,format:$('exchangeFormat').value}).catch(failImport);
 $('previewImport').onclick=async()=>{const file=$('importFile').files[0];clearImportError();if(!file){failImport(Error('请选择表格文件'));return;}const button=$('previewImport');button.disabled=true;try{if(file.size>8*1024*1024)throw Error('文件不能超过8MB');const content=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file);});importPreview=await send('/api/workspace/import-preview',{...ScopeUI.payload('importOfficeScope',{optional:true}),kind:$('exchangeKind').value,filename:file.name,content});$('importResult').innerHTML=`<p>${esc(importPreview.note)}</p><p class="muted">忽略的列：${esc(importPreview.ignored_columns.join('、')||'无')}</p><div class="preview-scroll">${table(['确认','原表行','处理方式','差异 / 错误'],importPreview.rows.map(row=>`<tr><td><input type="checkbox" data-import-row="${row.row}" ${row.selected?'checked':''} ${['create','update'].includes(row.action)?'':'disabled'}></td><td>${row.row}</td><td>${esc({create:'新增',update:'已有资料（默认保留，勾选才更新）',same:'无变化',error:'错误，不能导入'}[row.action])}</td><td>${esc(row.error||'')}${row.changes.map(change=>`<small>${esc(labels[change.field]||change.field)}：${esc(change.before)} → ${esc(change.after)}</small>`).join('')}</td></tr>`))}</div><button id="applyImport" class="button primary">确认导入已勾选行</button>`;$('applyImport').onclick=async()=>{const selected=[...document.querySelectorAll('[data-import-row]:checked')].map(field=>Number(field.dataset.importRow));$('applyImport').disabled=true;clearImportError();try{const result=await send('/api/workspace/import-apply',{preview_id:importPreview.preview_id,rows:selected,request_id:claimRequestId('import-apply')});settleRequestId('import-apply');$('importResult').textContent='已完成 '+result.imported+' 行导入；重新刷新后查看资料。';}catch(error){settleRequestId('import-apply',error);failImport(error);$('applyImport').disabled=false;}};}catch(error){failImport(error);}finally{button.disabled=false;}};
}
async function renderBackups(){
 if(data.user.role!=='owner'){$('workspaceBody').innerHTML='<p>只有总管理员可以管理备份和恢复。</p>';return;}
 $('workspaceBody').innerHTML='<div class="workspace-primary-actions"><button id="manualBackup" class="button primary">立即备份当前数据</button></div><div id="backupRows">正在读取…</div><div id="restorePanel"></div>';
 const backupRows=$('backupRows');let result;try{result=await send('/api/workspace/backups');}catch(error){if(backupRows===$('backupRows')){backupRows.textContent='无法读取备份，请点击刷新资料重试。';fail(error);}return;}if(backupRows!==$('backupRows'))return;backupRows.innerHTML=table(['备份文件','时间','大小','操作'],result.backups.map(item=>`<tr><td>${esc(item.id)}</td><td>${esc(new Date(item.at*1000).toLocaleString('zh-CN'))}</td><td>${(item.bytes/1024/1024).toFixed(2)}MB</td><td><button class="button" data-restore="${esc(item.id)}">检查并预览恢复</button></td></tr>`));
 $('manualBackup').onclick=async()=>{try{$('manualBackup').disabled=true;const result=await send('/api/workspace/backup-now',{});$('workspaceError').textContent='已备份：'+result.name;await renderBackups();}catch(error){fail(error);if($('manualBackup'))$('manualBackup').disabled=false;}};
 $('backupRows').onclick=async event=>{const button=event.target.closest('[data-restore]');if(!button)return;try{restorePreview=await send('/api/workspace/restore-preview',{backup_id:button.dataset.restore});$('restorePanel').innerHTML=`<div class="danger-box"><h3>恢复预览：${esc(restorePreview.backup_id)}</h3><p>${esc(restorePreview.note)}</p>${table(['业务表','当前条数','备份条数'],Object.entries(restorePreview.counts).map(([name,value])=>`<tr><td>${esc(backupTableNames[name]||name)}</td><td>${value.current}</td><td>${value.backup}</td></tr>`))}<form id="restoreForm"><label>输入“恢复业务数据”<input id="restoreConfirm" required autocomplete="off"></label><label>你的总管理员密码<input id="restorePassword" type="password" required autocomplete="current-password"></label><p>恢复会替换当前业务资料；先自动备份当前库，之后暂停作品采集。预览后资料发生变化时会拒绝执行，需重新预览。</p><button class="button primary" type="submit">确认恢复所选备份</button></form><p id="restoreError" class="error-text"></p></div>`;$('restoreForm').onsubmit=async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;try{const result=await send('/api/workspace/restore-apply',{preview_id:restorePreview.preview_id,confirmation:$('restoreConfirm').value,password:$('restorePassword').value});$('restorePassword').value='';$('restorePanel').textContent=result.note+'；恢复前备份：'+result.backup_before_restore;}catch(error){$('restorePassword').value='';$('restoreError').textContent=error.message;button.disabled=false;}};}catch(error){fail(error);}};
}
startWorkspaceLiveSync();
document.addEventListener('click',event=>{const workflow=event.target.closest('[data-workflow-device],[data-workflow-card],[data-workflow-account],[data-workflow-case]');if(!workflow||!window.DeviceWorkflow||DeviceWorkflow.isOpen())return;event.stopPropagation();const open=()=>{for(const [attribute,method] of [['workflowDevice','openDevice'],['workflowCard','openCard'],['workflowAccount','openAccount'],['workflowCase','openCase']])if(attribute in workflow.dataset)DeviceWorkflow[method](workflow.dataset[attribute]);if(!DeviceWorkflow.isOpen())throw Error('该事项已更新，请在列表中重新核对。');};if(window.LiveSync)prepareWorkspaceEdit(open).catch(fail);else loadBoard(true,false,true).then(loaded=>{if(loaded)open();}).catch(fail);},true);
document.addEventListener('click',event=>{const nurture=event.target.closest('[data-nurture]');if(!nurture||!window.LiveSync&&['detail','full'].includes(data?.workspace_view))return;event.stopPropagation();if(window.LiveSync)prepareWorkspaceEdit(()=>{nurtureDetail(nurture.dataset.nurture);freezeWorkspaceVersions('nurtureDialog');}).catch(fail);else ensureDetail().then(loaded=>{if(loaded)nurtureDetail(nurture.dataset.nurture);}).catch(fail);},true);
document.addEventListener('click',event=>{const button=event.target.closest('[data-batch-open]');if(!button||!window.LiveSync)return;event.stopPropagation();const kind=button.dataset.batchOpen,source=button.dataset.batchSource||kind,ids=[...batchSelection(source)];prepareWorkspaceEdit(()=>{if(ids.some(id=>!batchRecord(kind,id)))throw Error('所选资料已变化，请重新选择');const current=batchSelection(source);current.clear();for(const id of ids)current.add(id);openBatchEditor(kind,source);freezeWorkspaceVersions('batchEditor');}).catch(fail);},true);
document.addEventListener('click',event=>{const navigation=event.target.closest('[data-work-tab]');if(navigation&&!(navigation.tagName==='A'&&(event.ctrlKey||event.metaKey||event.shiftKey||event.altKey))){if(navigation.tagName==='A')event.preventDefault();const nextTab=navigation.dataset.workTab;tab=nextTab;const query=new URLSearchParams(navigation.tagName==='A'?new URL(navigation.href).search:location.search);query.set('tab',tab);history.replaceState(null,'','?'+query);listPage=1;todoPages={};const ready=detailTabs.has(tab)?ensureDetail():data?Promise.resolve(true):loadBoard(false);ready.then(loaded=>{if(loaded&&tab===nextTab)return render();}).catch(error=>{if(tab===nextTab)fail(error);});}const detail=event.target.closest('[data-overview]');if(detail)ensureDetail().then(loaded=>{if(loaded)return overview(detail.dataset.overview);}).catch(fail);const button=event.target.closest('[data-position]');if(button){const cell=data.publishing_layout.cells[Number(button.dataset.position)],batch=data.publishing_layout.batch;if(!window.LiveSync)position(Number(button.dataset.position));else if(cell)prepareWorkspaceEdit(()=>{const index=data.publishing_layout.batch===batch?data.publishing_layout.cells.findIndex(row=>row.computer_id===cell.computer_id&&row.slot===cell.slot&&row.account_id===cell.account_id):-1;if(index<0)throw Error('发布安排已变化，请重新选择');position(index);}).catch(fail);}});
$('reloadWorkspace').onclick=()=>reload().catch(fail);$('workDate').onchange=()=>reload().catch(fail);$('closeOverview').onclick=()=>$('overviewDialog').close();$('closePosition').onclick=()=>$('positionDialog').close();$('positionForm').onsubmit=event=>{event.preventDefault();savePosition('move').catch(error=>$('positionError').textContent=error.message);};$('removeAssignment').onclick=()=>{if(confirm('确认取消这个发布安排？账号资料和历史发布记录会保留。'))savePosition('remove').catch(error=>$('positionError').textContent=error.message);};
reload().then(loaded=>{if(!loaded||tab!==initialWorkspaceTab)return;const id=new URLSearchParams(location.search).get('account');if(!id)return;return ensureDetail().then(loaded=>{if(loaded&&tab===initialWorkspaceTab&&new URLSearchParams(location.search).get('account')===id)return overview(id);});}).catch(fail);
