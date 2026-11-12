// Presentation only: retain the existing per-field predicates and office scope.
const accountFilterFields=[['query','昵称'],['accountDouyinQuery','抖音号'],['accountIdQuery','账号编号'],['computer','登记电脑'],['status','账号状态'],['published','发布情况'],['accountDeviceQuery','登录手机序号'],['accountPhoneQuery','注册手机号'],['verificationFilter','核对情况'],['loginPhoneFilter','登录手机'],['accountType','账号类型'],['ageFilter','注册天数'],['lifecycleFilter','注册进度']];
const accountAdvancedFields=new Set(['computer','published','accountDeviceQuery','accountPhoneQuery','verificationFilter','loginPhoneFilter','accountType','ageFilter','lifecycleFilter']);
function compactAccountRow(account,index){
 const inline=(field,content)=>window.AccountInline?AccountInline.cell(account,field,content):content;
 const status=`<span class="badge ${account.status==='正常'?'good':abnormal(account)?'warn':''}">${esc(account.status||'待确认')}</span>`;
 const phoneTitle=typeof accountDeviceDisplay==='function'?accountDeviceDisplay(account,data):account.phone_device_name||'登录手机待确认';
 const unit=(data.device_workflow?.units||[]).find(row=>String(row.id)===String(account.device_access?.last_unit_id));
 const device=(data.phone_devices?.devices||[]).find(row=>row.id===(unit?.device_id||account.phone_device_id));
 const phone=device&&typeof deviceOfficeText==='function'?deviceOfficeText(device,data)+' · '+(deviceNumber(device)||'未编号')+'号手机'+(unit&&!unit.active?' · 旧机':unit?.generation>1?' · 第'+unit.generation+'台':''):phoneTitle;
 const note=esc(String(account.note||'').slice(0,80)||'填写备注');
 return `<tr><td><input type="checkbox" data-select="${esc(account.id)}" ${selected.has(account.id)?'checked':''} aria-label="选择账号 ${esc(account.name)}"></td>
  <td class="account-identity">${inline('name','<strong>'+esc(account.name||'昵称待补充')+'</strong>')}<button type="button" class="text-button account-detail-link" data-account-pilot-detail="${esc(account.id)}" aria-label="查看账号 ${esc(account.name||'未命名')} 详情，抖音号 ${esc(account.douyin_id||'待补充')}">${account.douyin_id?'抖音号 '+esc(account.douyin_id):'抖音号待补充'}<span aria-hidden="true"> ↗</span></button></td>
  <td class="account-status">${inline('status',status)}</td>
  <td class="account-phone"><span title="${esc(phoneTitle)}">${esc(phone||'登录手机待确认')}</span><small>注册卡 ${esc(account.registration_phone||'待确认')}</small>${account.device_access?.login_state==='登录受阻'?'<small class="account-pilot-warning">登录受阻</small>':''}</td>
  <td class="account-position">${actualPublishingText(account)}</td>
  <td><button class="publish-toggle" data-publish="${esc(account.id)}" aria-pressed="${!!account.published}">${account.published?'✓ 已发':'○ 未发'}</button></td>
  <td class="account-notes">${inline('note',note)}</td></tr>`;
}
function accountFilterValue(id){
 const field=$(id);if(!field||!String(field.value||'').trim())return '';
 const option=field.options&&Array.from(field.options).find(item=>item.value===field.value);
 return option?.textContent||option?.label||String(field.value);
}
function renderAccountFilterSummary(accounts){
 const host=$('accountActiveFilters');if(!host)return;
 const conditions=[...accountFilterFields,...(window.AccountPilot?.enabled()?window.AccountPilot.fields:[])].map(([id,label])=>({id,label:id==='computer'&&page==='board'?'发布电脑':label,value:accountFilterValue(id)})).filter(item=>item.value);
 const preset=typeof activeLifecyclePreset==='undefined'?'':activeLifecyclePreset;
 if(preset)conditions.push({id:'lifecyclePreset',label:'注册进度快捷筛选',value:({all:'全部大号',ready:'满30天待注册',registered:'已注册小号',young:'未满30天',missing:'待补日期',blocked:'暂无法注册',unknown:'类型待确认'})[preset]||preset});
 const advancedCount=conditions.filter(item=>accountAdvancedFields.has(item.id)||item.id==='lifecyclePreset'||window.AccountPilot?.advanced.has(item.id)).length;
 if($('accountAdvancedCount'))$('accountAdvancedCount').textContent=advancedCount?'（已选 '+advancedCount+' 项）':'';
 host.innerHTML=`<span class="account-filter-count">筛选结果 <strong>${accounts.length}</strong> 个账号</span>`+(conditions.length?conditions.map(item=>`<button type="button" class="account-filter-chip" data-clear-account-filter="${esc(item.id)}" aria-label="清除${esc(item.label)}筛选">${esc(item.label)}：${esc(item.value)} <span aria-hidden="true">×</span></button>`).join(''):'<span class="account-filter-empty">未附加筛选条件</span>');
}
function clearAccountFilter(id){
 if(id==='lifecyclePreset'){activeLifecyclePreset='';['accountType','ageFilter','lifecycleFilter'].forEach(key=>$(key).value='');}
 else if([...accountFilterFields,...(window.AccountPilot?.enabled()?window.AccountPilot.fields:[])].some(([key])=>key===id)){$(id).value='';if(['accountType','ageFilter','lifecycleFilter'].includes(id))activeLifecyclePreset='';}
 else return;
 render();
}

// Office-three UI pilot. Source facts stay in authorized memory; only enum
// filters, page numbers and scroll positions may survive this browser session.
if(typeof window!=='undefined')window.AccountPilot=(()=>{
 const fields=[['accountResponsible','负责人'],['accountUsage','使用情况'],['accountViolation','违规状态'],['accountBusinessType','业务类型']];
 const advanced=new Set(fields.map(([id])=>id));
 const sourceKeys={accountResponsible:'responsible',accountUsage:'usage',accountViolation:'violation',accountBusinessType:'business_type'};
 const safeEnums=['status','published','accountType','ageFilter','lifecycleFilter','verificationFilter'];
 let activeKey='',lastScope='',lastUser='',lastPage='',wasActive=false,headerHTML=null,filterTimer=null,sourceFlight=null,sourceGeneration=0,sourcePhase='idle',sourceMessage='',sourceSignature='',sourceRows=new Map(),lastBoard=null,restoredScroll=null;
 const memories=new Map();
 const board=()=>typeof data==='undefined'?null:data;
 const enabled=()=>typeof page!=='undefined'&&page==='accounts'&&typeof activeOffice!=='undefined'&&activeOffice==='three'&&!!board()?.user;
 const scopeKey=()=>{const user=board()?.user;return user?.id==null?'':JSON.stringify([String(user.id),user.authorization_version??null,user.scope_mode??null,[...(user.office_ids||[])].sort(),[...(user.permissions||[])].sort(),'three']);};
 const field=id=>document.getElementById(id);
 const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const text=value=>typeof value==='string'?value:'';
 const sourceFor=account=>sourceRows.get(String(account.id));
 function cancelFilter(){if(filterTimer!==null){clearTimeout(filterTimer);filterTimer=null;}}
 function scheduleFilter(){cancelFilter();const key=scopeKey();filterTimer=setTimeout(()=>{filterTimer=null;if(enabled()&&scopeKey()===key)render();},180);}
 function signature(){return enabled()?fields.map(([id])=>field(id)?.value||''):[];}
 function matches(account){if(!enabled())return true;const source=sourceFor(account);return fields.every(([id])=>!field(id)?.value||source?.[sourceKeys[id]]===field(id).value);}
 function resetFilters(){cancelFilter();for(const [id]of fields)if(field(id))field(id).value='';}
 function uiState(){
  const ids=[...accountFilterFields.map(([id])=>id),...fields.map(([id])=>id)],scroll=document.querySelector('#accountsView > .account-scroll');
  return {fields:Object.fromEntries(ids.map(id=>[id,field(id)?.value||''])),page:accountPageIndex,preset:activeLifecyclePreset||'',selected:[...selected],advanced:!!field('accountAdvancedFilters')?.open,x:window.scrollX||0,y:window.scrollY||0,left:scroll?.scrollLeft||0,top:scroll?.scrollTop||0};
 }
 function remember(){
  if(!enabled()||!activeKey||activeKey!==scopeKey())return;
  const saved=uiState();memories.set(activeKey,saved);
  try{window.sessionStorage?.setItem('douyin-account-pilot:'+activeKey,JSON.stringify({v:1,page:saved.page,fields:Object.fromEntries(safeEnums.map(id=>[id,saved.fields[id]])),advanced:saved.advanced,x:saved.x,y:saved.y,left:saved.left,top:saved.top}));}catch(error){/* Storage restrictions do not block the page. */}
 }
 function readUI(key){
  if(memories.has(key))return memories.get(key);
  try{const saved=JSON.parse(window.sessionStorage?.getItem('douyin-account-pilot:'+key)||'null');if(saved?.v!==1||!Number.isInteger(saved.page)||saved.page<1)return null;return {...saved,fields:Object.fromEntries(safeEnums.map(id=>[id,typeof saved.fields?.[id]==='string'&&saved.fields[id].length<=50?saved.fields[id]:'']))};}catch(error){return null;}
 }
 function restoreUI(saved){
  if(!saved)return;
  for(const [id,value]of Object.entries(saved.fields||{})){const node=field(id);if(node)node.value=value;}
  if(field('accountAdvancedFilters'))field('accountAdvancedFilters').open=saved.advanced===true;
  if(Array.isArray(saved.selected)){const allowed=new Set(officeAccounts(board().accounts).map(row=>String(row.id)));selected.clear();for(const id of saved.selected)if(allowed.has(String(id)))selected.add(String(id));}
  activeLifecyclePreset=saved.preset||'';
  const base=[...accountSearchFields,'computer','status','published','accountType','ageFilter','lifecycleFilter','loginPhoneFilter','verificationFilter'];
  accountPageSignature=JSON.stringify([activeOffice,board().date,activeLifecyclePreset,...base.map(id=>field(id)?.value||''),...signature()]);
  accountPageIndex=saved.page;restoredScroll=saved;
 }
 function showSourceState(){const node=field('accountPilotSourcesState');if(node){node.textContent=sourceMessage;node.hidden=!sourceMessage;node.dataset.phase=sourcePhase;}}
 function fillSourceFilters(){
  for(const [id]of fields){const node=field(id);if(!node)continue;const current=node.value,values=[...new Set([...sourceRows.values()].map(row=>row[sourceKeys[id]]).filter(Boolean))].sort((a,b)=>a.localeCompare(b,'zh-CN'));
   node.innerHTML='<option value="">全部'+fields.find(([key])=>key===id)[1]+'</option>'+values.map(value=>'<option value="'+escape(value)+'">'+escape(value)+'</option>').join('');
   if(current&&!values.includes(current))node.innerHTML+='<option value="'+escape(current)+'">'+escape(current)+'（本次来源未提供）</option>';
   node.value=current;node.disabled=sourcePhase!=='ready';
  }
  showSourceState();
 }
 function clearSources(){sourceGeneration++;sourceFlight?.token.abort?.abort();sourceFlight=null;sourceRows=new Map();sourceSignature='';sourcePhase='idle';sourceMessage='';lastBoard=null;}
 async function loadSources(force=false){
  if(!enabled())return false;const key=scopeKey();if(sourceFlight?.key===key)return sourceFlight.promise;
  if(!force&&sourceSignature===key&&sourcePhase==='ready')return true;
  const token={key,generation:++sourceGeneration,abort:typeof AbortController==='function'?new AbortController():null};sourcePhase='loading';sourceMessage='正在读取本办公室表格来源字段…';fillSourceFilters();
  const promise=(async()=>{
   let timer;try{const deadline=new Promise((resolve,reject)=>{timer=setTimeout(()=>{token.abort?.abort();reject(Error('来源资料读取超时'));},15000);});const result=await Promise.race([api('/api/table-sync/account-sources?office_id=three',undefined,{applyVersions:false,signal:token.abort?.signal}),deadline]);
    if(token.generation!==sourceGeneration||scopeKey()!==key||!enabled())return false;
    if(result?.office_id!=='three'||!Array.isArray(result.sources)||typeof result.available!=='boolean')throw Error('来源字段响应不完整');
    if(!result.available){sourceRows=new Map();sourceSignature=key;sourcePhase='unavailable';sourceMessage='表格来源字段尚未启用；原账号资料仍可正常查看和编辑。';}
    else{const ids=new Set(officeAccounts(board().accounts).map(row=>String(row.id))),next=new Map();for(const row of result.sources){if(row?.account_id==null||!ids.has(String(row.account_id)))continue;next.set(String(row.account_id),{responsible:text(row.responsible),usage:text(row.usage),violation:text(row.violation),business_type:text(row.business_type),storefront:text(row.storefront),source_data:row.source_data&&typeof row.source_data==='object'?row.source_data:null,revision:row.revision});}sourceRows=next;sourceSignature=key;sourcePhase='ready';sourceMessage='';}
    fillSourceFilters();if(!window.AccountInline?.hasDraft()&&!document.querySelector('dialog[open]')?.open)render();return true;
   }catch(error){if(token.generation!==sourceGeneration||scopeKey()!==key)return false;sourcePhase='error';sourceMessage='表格来源字段读取失败；不猜测负责人或业务分类。可点“重试来源字段”。';fillSourceFilters();return false;}
   finally{clearTimeout(timer);if(sourceFlight?.token===token)sourceFlight=null;}
  })();sourceFlight={key,token,promise};return promise;
 }
 function mount(){
  if(document.querySelector('[data-account-pilot-filters]'))return;
  const more=document.querySelector('.account-more-fields');if(!more?.append)return;
  for(const [id,label]of fields){const wrapper=document.createElement('label');wrapper.className='field-search account-pilot-filter';wrapper.dataset.accountPilotFilters='';wrapper.innerHTML=escape(label)+'<select id="'+id+'" aria-label="'+escape(label)+'"><option value="">全部'+escape(label)+'</option></select>';more.append(wrapper);const node=field(id);if(node)node.onchange=()=>{cancelFilter();render();};}
  const status=document.createElement('p');status.id='accountPilotSourcesState';status.className='account-pilot-source-state';status.setAttribute('role','status');field('accountActiveFilters')?.after(status);
  const retry=document.createElement('button');retry.type='button';retry.id='accountPilotSourcesRetry';retry.className='text-button account-pilot-source-retry';retry.textContent='重试来源字段';retry.onclick=()=>loadSources(true);status.after(retry);
 }
 function beforeRender(){
  const key=scopeKey(),user=String(board()?.user?.id??'');
  if(lastUser&&lastUser!==user){memories.clear();clearSources();window.AccountInline?.clearSuspended?.();}
  lastUser=user;
  if(lastScope&&lastScope!==key){clearSources();window.AccountInline?.clearSuspended?.();}
  lastScope=key;
  if(!enabled()){cancelFilter();if(wasActive){wasActive=false;activeKey='';}return;}
  mount();
  if(activeKey!==key||lastPage!=='accounts'){activeKey=key;restoreUI(readUI(key));}
  if(sourceSignature&&sourceSignature!==key)clearSources();
 }
 function afterRender(){
  const active=enabled();document.body.classList.toggle('account-pilot',active);
  const head=document.querySelector('.account-table thead tr');
  if(active){
   if(head){const all=field('selectAll');if(headerHTML===null)headerHTML=head.innerHTML;head.innerHTML='<th></th><th>账号</th><th>账号状态</th><th>登录手机 / 注册卡</th><th>发布位置</th><th>当天发布</th><th>备注</th>';if(all)head.querySelector('th')?.append(all);}
   field('title').textContent='账号资料';field('breadcrumb').textContent='账号资料';field('subtitle').textContent='双击修改昵称、状态和备注，点抖音号查看完整资料。';document.title='账号资料 · 账号工作台';
   const actions=document.querySelector('.header-actions');if(actions)actions.hidden=false;
   const retry=field('accountPilotSourcesRetry');if(retry)retry.hidden=sourcePhase!=='error';
   wasActive=true;remember();
   if(lastBoard!==board()){lastBoard=board();void loadSources(true);}else if(sourcePhase==='idle')void loadSources();
   if(restoredScroll){const saved=restoredScroll;restoredScroll=null;const scroll=document.querySelector('#accountsView > .account-scroll');if(scroll){scroll.scrollLeft=Number(saved.left)||0;scroll.scrollTop=Number(saved.top)||0;}window.scrollTo?.(Number(saved.x)||0,Number(saved.y)||0);}
   void window.AccountInline?.restoreSuspended?.();
  }else if(headerHTML!==null&&head){head.innerHTML=headerHTML;headerHTML=null;const all=field('selectAll');if(all)all.onchange=()=>{filtered().forEach(account=>all.checked?selected.add(account.id):selected.delete(account.id));render();};}
  for(const node of document.querySelectorAll('.account-pilot-filter,.account-pilot-source-state,.account-pilot-source-retry'))node.hidden=!active||(node.id==='accountPilotSourcesState'&&!sourceMessage)||(node.id==='accountPilotSourcesRetry'&&sourcePhase!=='error');
  lastPage=typeof page==='undefined'?'':page;
 }
 function row(account,index){return compactAccountRow(account,index);}
 function openDetail(id,trigger){
  if(typeof page==='undefined'||page!=='accounts'||!board()?.user||window.AccountInline?.hasDraft()||window.CardInline?.hasDraft()||window.DeviceInline?.hasDraft())return false;
  const account=officeAccounts(board().accounts).find(row=>String(row.id)===String(id));if(!account)return false;
  let dialog=field('accountPilotDetailDialog');if(!dialog){dialog=document.createElement('dialog');dialog.id='accountPilotDetailDialog';dialog.className='account-pilot-detail';dialog.setAttribute('aria-labelledby','accountPilotDetailTitle');document.body.append(dialog);}
  const source=enabled()?sourceFor(account):null,host=board().computers.find(row=>row.id===account.computer_id),saved=uiState();
  const collectionReason=collectionSubmitReason();
  const sourceFields=source?[['负责人',source.responsible],['使用情况',source.usage],['违规状态',source.violation],['业务类型',source.business_type],['门头',source.storefront],['来源手机编号（参考）',text(source.source_data?.source_phone_number)],['小号说明（来源）',text(source.source_data?.auxiliary_note)],['原表状态',text(source.source_data?.original_status)],['来源轮次',text(source.source_data?.source_round)]]:[];
  dialog.innerHTML=`<div class="dialog-head"><div><small>账号详情</small><h2 id="accountPilotDetailTitle">${escape(account.name)}</h2></div><button id="accountPilotDetailClose" type="button" class="text-button">关闭</button></div><div class="account-pilot-detail-body"><section><h3>账号身份</h3><p>抖音号：${escape(account.douyin_id||'待补充')}</p><p>状态：${escape(account.status)}</p><p>登录手机：${escape(typeof accountDeviceDisplay==='function'?accountDeviceDisplay(account,board()):account.phone_device_name||'待确认')}</p><p>登记电脑：${escape(host?.name||'待确认')} · 位置 ${escape(account.slot??'未分配')}</p><p>实际发布：${actualPublishingText(account)}</p><p>${escape(board().date)}：${account.published?'已记录发布':'未记录发布'}</p></section><section><h3>账号类型 / 注册进度</h3>${lifecycleDetails(account)}<p>注册手机号：${escape(account.registration_phone||'未填写')}</p></section><section><h3>稳定与改密</h3><p>稳定：${escape(account.card_stability||'待确认')} · 改密：${escape(account.password_state||'待确认')}</p>${verificationMarkup(account)}</section><section><h3>表格来源资料</h3>${source?sourceFields.map(([label,value])=>'<p>'+escape(label)+'：'+escape(value||'来源未提供')+'</p>').join(''):'<p>来源资料尚未登记或暂不可用，不根据其他关系推断。</p>'}<p class="permission-note">表格来源与人工备注分别保留；来源手机编号不代表登录关系。</p></section><section><h3>完整备注</h3><p class="account-detail-note">${escape(account.note||'未填写')}</p></section><section><h3>公开作品数量 / 采集记录</h3><p>${escape(account.work_count??'尚未采集')} · ${escape(account.last_collected||'无成功记录')}</p><p>${escape(account.collection_state||'尚无采集记录')}</p>${account.collection_error?'<p class="error-text">'+escape(account.collection_error)+'</p>':''}<p id="accountPilotCollectionReason" class="permission-note">${escape(collectionReason||'手动采集公开作品数量')}</p><button id="accountPilotCollect" type="button" class="button" data-collect="${escape(account.id)}" ${collectionReason?'disabled':''} title="${escape(collectionReason)}">采集该账号</button><p id="accountPilotCollectionFeedback" role="status"></p></section></div><div class="account-pilot-detail-footer"><button type="button" class="button" data-edit="${escape(account.id)}" id="accountPilotDetailEdit" ${['account_manage','account_status','account_progress','device_manage'].some(hasPermission)?'':'hidden'}>编辑完整资料</button>${board().device_workflow?.enabled?`<button type="button" class="button" data-workflow-account="${escape(account.id)}" id="accountPilotDeviceWorkflow">登录恢复 / 处理记录</button>`:''}<a class="button" href="/workspace.html?account=${encodeURIComponent(account.id)}${enabled()?'&office=three':''}">关联资料 / 历史</a></div>`;
  const close=()=>dialog.close();field('accountPilotDetailClose').onclick=close;field('accountPilotDetailEdit').onclick=close;const workflow=field('accountPilotDeviceWorkflow');if(workflow)workflow.onclick=close;
  dialog.onclose=()=>{const scroll=document.querySelector('#accountsView > .account-scroll');if(scroll){scroll.scrollLeft=saved.left;scroll.scrollTop=saved.top;}window.scrollTo?.(saved.x,saved.y);trigger?.focus?.({preventScroll:true});window.boardLiveSync?.poke();};
  dialog.showModal();void window.refreshBoardCollection?.();return true;
 }
 document.addEventListener('click',event=>{if(event.target.closest?.('[data-office]'))remember();const button=event.target.closest?.('[data-account-pilot-detail]');if(button){event.preventDefault();openDetail(button.dataset.accountPilotDetail,button);}},true);
 document.addEventListener('board-ready',()=>{if(enabled()){lastBoard=board();void loadSources(true);}});
 window.addEventListener?.('pagehide',remember);
 return Object.freeze({fields,advanced,enabled,matches,signature,beforeRender,afterRender,remember,row,openDetail,scheduleFilter,cancelFilter,resetFilters,loadSources,refreshSources:()=>loadSources(true),getState:()=>({phase:sourcePhase,sourceCount:sourceRows.size,scope:activeKey})});
})();
document.addEventListener('click',event=>{const button=event.target.closest('[data-clear-account-filter]');if(button)clearAccountFilter(button.dataset.clearAccountFilter);});

// Move existing controls without replacing nodes, listeners or permission flags.
const accountFilterHomes=new Map();
function compactAccountFilters(active){
 const more=document.querySelector('.account-more-fields');
 if(!more)return;
 for(const id of ['computer','published']){
  const label=$(id)?.closest('label');if(!label)continue;
  if(active&&!accountFilterHomes.has(id)){const home=document.createComment('account-filter-'+id);label.before(home);accountFilterHomes.set(id,home);}
  const home=accountFilterHomes.get(id);
  if(active)more.append(label);else if(home?.parentNode)home.after(label);
 }
}

function activateAccountConsole(active){
 document.body.classList.toggle('accounts-console',active);compactAccountFilters(active);
 const board=page==='board';
 const move=(id,enabledHost,home)=>{const node=$(id),host=$(active?enabledHost:home);if(node&&host&&node.parentElement!==host)host.append(node);};
 move('newAccount','accountsHeaderActions',board?'boardSecondaryActions':'accountCreateHome');
 move('refresh','accountListActions','accountToolsHome');
 move('exportVisibleAccounts','accountListActions',board?'boardSecondaryActions':'accountToolsHome');
 move('collectionBar','accountSupplementBody',board?'boardSupplementBody':'accountSupplementHome');
 move('lifecycleSummary','accountSupplementBody',board?'boardSupplementBody':'accountSupplementHome');
 const supplement=$('boardSupplement');if(supplement)supplement.hidden=!board;
 const computerLabel=$('computerFilterLabel');if(computerLabel){computerLabel.textContent=board?'发布电脑':'登记电脑';$('computer').setAttribute('aria-label',computerLabel.textContent);}
 for(const id of ['accountsHeaderActions','accountListActions','accountSupplement']){const node=$(id);if(node)node.hidden=!active;}
 if(active){const summary=$('accountSupplement')?.querySelector('summary');if(summary)summary.innerHTML='作品采集与注册进度<span>'+esc(data?.collection?.enabled===false?'采集暂未启用 · 历史记录保留':'采集入口与历史记录')+'</span>';}
 $('newAccount')?.classList.toggle('primary',active);
}
