/* Presentation and read-only filtering. Card status/readiness writes keep their original routes. */
window.CardFilters=(()=>{
 const blank='__card_blank__',all='all';
 const fields=[['cardOfficeFilter','办公室'],['cardQuery','卡号'],['cardStatus','卡状态'],['cardAssessment','注销核对'],['cardLocationQuery','位置 / 序号'],['cardOwnerQuery','卡主'],['cardLabelQuery','卡名称'],['cardAccountNameQuery','关联昵称'],['cardCarrier','运营商'],['cardLinked','账号关联'],['cardExpiry','到期情况'],['cardCodeFilter','接码事项'],['cardReturnFilter','归还事项'],['cardPositionFilter','位置确认'],['cardLinkedStateFilter','关联账号状态'],['cardSourceFilter','来源核对'],['cardTask','原处理事项']];
 const notes={'待确认':'尚未人工确认卡的使用情况。','正常':'人工登记为正常；不代表实时运营商检测。','欠费':'已登记欠费，核对续费或后续处理。','停机':'已登记停机，核对具体原因；与注销分别记录。','停用':'内部标记暂停使用，保留记录；不代表运营商已销户。','待注销':'已人工列入办理注销清单，尚未登记完成。','已注销':'已登记注销完成，不会注销或删除抖音账号。'};
 const val=id=>String($(id)?.value||'');
 const isBlank=value=>value===undefined||value===null||value==='';
 function allowedOffices(board){
  let offices=board.offices||[];
  if(board.user?.scope_mode&&board.user.scope_mode!=='all')offices=offices.filter(o=>(board.user.office_ids||[]).includes(o.id));
  return offices;
 }
 function scopeRows(board,office=all){
  const scoped=!!board.user?.scope_mode,global=board.user?.scope_mode==='all',ids=new Set(allowedOffices(board).map(o=>o.id));
  return (board.phone_cards?.cards||[]).filter(card=>{
   const own=Array.isArray(card.office_ids)?card.office_ids:[];
   if(scoped&&!global&&!own.some(id=>ids.has(id)))return false;
   if(office===all||!office)return true;
   if(office==='pending')return (!scoped||global)&&own.length===0;
   return ids.has(office)&&own.includes(office);
  });
 }
 function read(){return Object.fromEntries(fields.map(([id])=>[id,val(id)]));}
 function position(card,index,board){
  if(!board.device_workflow?.enabled)return {value:card.location||'',kind:card.location?'source':'unknown'};
  const placement=card.placement;
  if(placement?.unit_id){const unit=index.unitsById.get(String(placement.unit_id));return {value:unit?.device_id||'',kind:unit?'phone':'unavailable'};}
  if(placement?.storage)return {value:placement.storage,kind:'storage'};
  return {value:card.location||'',kind:card.location?'source':'unknown'};
 }
 const enumMatches=(actual,wanted)=>!wanted||(wanted===blank?isBlank(actual):actual===wanted);
 function filter(board,filters=read(),index=cardAccountLookup()){
  return scopeRows(board,filters.cardOfficeFilter).filter(card=>{
   if(!containsText(card.phone,(filters.cardQuery||'').trim().toLowerCase())||!containsText(card.owner,(filters.cardOwnerQuery||'').trim().toLowerCase())||!containsText(card.label,(filters.cardLabelQuery||'').trim().toLowerCase()))return false;
   if(!enumMatches(card.status,filters.cardStatus))return false;
   if(filters.cardCarrier&&card.carrier!==filters.cardCarrier&&cardCarrierLabel(card)!==filters.cardCarrier)return false;
   if(filters.cardCodeFilter&&!enumMatches(card.code_state,filters.cardCodeFilter))return false;
   if(filters.cardReturnFilter&&!enumMatches(card.return_state,filters.cardReturnFilter))return false;
   const location=position(card,index,board),query=(filters.cardLocationQuery||'').trim().toLowerCase();
   if(query&&!(location.kind==='phone'?deviceNumberMatches(location.value,query):locationNumberMatches(location.value,query)))return false;
   if(filters.cardPositionFilter==='phone'&&location.kind!=='phone'||filters.cardPositionFilter==='storage'&&location.kind!=='storage'||filters.cardPositionFilter==='unconfirmed'&&!['unknown','source','unavailable'].includes(location.kind))return false;
   if(filters.cardExpiry&&(filters.cardExpiry==='soon'?!cardExpiring(card):!!card.expires_on))return false;
   const related=cardAccounts(card.phone,index),ready=cardReady(card,index,related);
   if(filters.cardLinkedStateFilter==='blocked'&&!related.some(a=>a.status==='封禁'))return false;
   if(filters.cardSourceFilter==='review'&&!cardTaskMatches(card,index,'review'))return false;
   if(filters.cardAssessment==='ready'&&!ready||filters.cardAssessment==='notready'&&ready)return false;
   if(!cardTaskMatches(card,index,filters.cardTask||''))return false;
   if(filters.cardLinked&&(related.length>0)!==(filters.cardLinked==='yes'))return false;
   return !filters.cardAccountNameQuery||related.some(a=>containsText(a.name,filters.cardAccountNameQuery.trim().toLowerCase()));
  });
 }
 function reasons(card,index){
  const related=cardAccounts(card.phone,index),active=related.filter(a=>a.status!=='封禁'),result=[];
  if(!related.length)return ['未关联同注册手机号的账号，无法核对条件'];
  if(!active.length)result.push('没有未封禁的关联账号');
  if(!related.some(a=>a.account_type==='小号'))result.push('未登记关联小号');
  if(related.filter(a=>a.account_type==='大号').length>1)result.push('关联多个大号，需核对');
  const unknown=active.filter(a=>!['大号','小号'].includes(a.account_type)).length,unstable=active.filter(a=>a.card_stability!=='已稳定').length,password=active.filter(a=>a.password_state!=='已修改').length;
  if(unknown)result.push(unknown+' 个账号类型待确认');
  if(unstable)result.push(unstable+' 个账号未登记已稳定');
  if(password)result.push(password+' 个账号未登记已改密');
  return result.length?result:['原稳定改密条件已记录；仍需人工核对接码、验证及找回用途'];
 }
 function setOptions(id,options,first){
  const node=$(id);if(!node)return;const value=val(id);
  node.innerHTML=`<option value="">${esc(first)}</option>`+options.map(([value,label])=>`<option value="${esc(value)}">${esc(label)}</option>`).join('');
  node.value=options.some(([key])=>key===value)?value:'';
 }
 function prepare(board,index){
  const office=$('cardOfficeFilter');
  if(office){const previous=val('cardOfficeFilter')||all,offices=allowedOffices(board);office.innerHTML='<option value="all">全部授权办公室</option>'+offices.map(o=>`<option value="${esc(o.id)}">${esc(o.name)}</option>`).join('')+(board.user?.scope_mode==='all'?'<option value="pending">待分配办公室</option>':'');office.value=[all,...offices.map(o=>o.id),...(board.user?.scope_mode==='all'?['pending']:[])].includes(previous)?previous:all;}
  const cards=scopeRows(board,val('cardOfficeFilter')||all),selected=val('cardStatus'),states=[...new Set([...(board.phone_cards?.statuses||[]),...cards.map(c=>c.status).filter(v=>!isBlank(v)),...(selected&&selected!==blank?[selected]:[])])];
  setOptions('cardStatus',[...states.map(s=>[s,notes[s]?s:'历史值：'+s]),...(selected===blank||cards.some(c=>isBlank(c.status))?[[blank,'未登记（空白）']]:[])],'全部卡状态');
  setOptions('cardCarrier',[...new Set([...cardCarrierOptions(),...cards.map(cardCarrierLabel),...(val('cardCarrier')?[val('cardCarrier')]:[])])].map(s=>[s,s]),'全部运营商');
  for(const [id,field,known,title]of [['cardCodeFilter','code_state',board.phone_cards?.code_states||['无待办','待接码','已完成','暂无法接码'],'全部接码事项'],['cardReturnFilter','return_state',board.phone_cards?.return_states||['待确认','不需归还','待归还','已归还'],'全部归还事项']]){
   const values=[...new Set([...known,...cards.map(c=>c[field]).filter(v=>!isBlank(v)),...(val(id)&&val(id)!==blank?[val(id)]:[])])];
   setOptions(id,[...values.map(s=>[s,known.includes(s)?s:'历史值：'+s]),[blank,'未登记（空白）']],title);
  }
 }
 function shortcut(filters,key){
  const next={...filters,cardStatus:'',cardAssessment:'',cardTask:''};
  if(key==='ready'||key==='notready')next.cardAssessment=key;
  if(key==='pending')next.cardStatus='待注销';if(key==='closed')next.cardStatus='已注销';return next;
 }
 function refresh(){
  const error=$('cardFilterError');if(error)error.hidden=true;
  try{render();}catch(failure){if(!error)throw failure;error.textContent='手机卡筛选显示失败，请刷新资料后重试。当前显示可能仍是上一次结果，不代表没有匹配记录。';error.hidden=false;}
 }
 function reset(keepOffice=true){for(const [id] of fields)if(!keepOffice||id!=='cardOfficeFilter')$(id).value=id==='cardOfficeFilter'?all:'';cardPageIndex=1;refresh();}
 function clear(id){if(!fields.some(([key])=>key===id))return;$(id).value=id==='cardOfficeFilter'?all:'';cardPageIndex=1;refresh();}
 function emptyReason(board,filters,index){
  const scope=scopeRows(board,filters.cardOfficeFilter);
  if(!scope.length){const pending=board.user?.scope_mode==='all'?scopeRows(board,'pending').length:0;return filters.cardOfficeFilter&&filters.cardOfficeFilter!==all?'当前所选办公室没有明确归属的手机卡。'+(pending?'当前授权范围有 '+pending+' 张卡在待分配；不会自动调整归属。':''):'当前授权范围没有手机卡资料。';}
  let reason='已加载本办公室范围 '+scope.length+' 张卡，当前条件同时匹配 0 张；请清除不需要的条件。';
  if(filters.cardAssessment==='ready'){
   const base=filter(board,{...filters,cardAssessment:''},index),counts=new Map();
   for(const card of base)for(const why of reasons(card,index)){const label=why.replace(/^\d+ 个账号/,'关联账号');counts.set(label,(counts.get(label)||0)+1);}
   if(base.length&&counts.size)reason+=' 未满足的条件：'+[...counts].slice(0,4).map(([label,count])=>label+'（'+count+'张）').join('、')+'；原因可能重叠。';
  }
  return reason;
 }
 function renderUI(board,rows,index){
  const filters=read(),scope=scopeRows(board,filters.cardOfficeFilter),host=$('cardActiveFilters');if(!host)return;
  const conditions=fields.filter(([id])=>filters[id]&&!(id==='cardOfficeFilter'&&filters[id]===all));
  const labels={ready:'稳定改密条件已记录（需人工核对注销）',notready:'稳定改密条件未齐',cancel:'旧入口：已标记待注销',code:'旧入口：待接码',return:'旧入口：待归还',blocked:'关联账号有封禁',review:'来源文字有疑点'};
  const display=id=>{const field=$(id),option=Array.from(field.options||[]).find(o=>o.value===field.value);return option?.textContent||option?.label||(filters[id]===blank?'未登记（空白）':id==='cardTask'?labels[filters[id]]:filters[id])||filters[id];};
  host.innerHTML=`<span>当前结果 <strong>${rows.length}</strong> / 本办公室范围 ${scope.length} 张</span>`+(conditions.length?conditions.map(([id,label])=>`<button type="button" class="card-filter-chip" data-card-filter-clear="${id}" aria-label="清除${esc(label)}筛选">${esc(label)}：${esc(display(id))} ×</button>`).join(''):'<span class="muted">没有附加筛选条件</span>');
  const pending=board.user?.scope_mode==='all'&&filters.cardOfficeFilter&&![all,'pending'].includes(filters.cardOfficeFilter)?filter(board,{...filters,cardOfficeFilter:'pending'},index).length:0;
  if(pending)host.innerHTML+=`<span>相同其他条件下，另有 ${pending} 张卡未登记办公室，未计入当前结果。</span><button type="button" class="card-filter-chip" data-card-office-pending>查看待分配（${pending}）</button>`;
  $('cardQuickFilters').innerHTML=[['all','清除状态 / 核对条件'],['ready','注销核对 · 条件已记录'],['notready','条件未齐'],['pending','已标记待注销'],['closed','已注销']].map(([key,label])=>`<button type="button" class="button" data-card-quick="${key}" title="保留办公室及其他条件，替换卡状态和注销核对条件">${label}（${filter(board,shortcut(filters,key),index).length}）</button>`).join('');
  const codeNotes={'无待办':'当前没有登记接码待办','待接码':'已登记接码请求，等待处理','已完成':'本次接码事项已登记完成','暂无法接码':'已登记当前无法接码，需继续跟进'},returnNotes={'待确认':'尚未确认是否需要归还','不需归还':'已登记无需归还','待归还':'计划归还，尚未登记完成','已归还':'已登记实际归还'};
  const positionNotes={phone:'已登记插卡实物，按当前实物编号查找。',storage:'已登记卡盒或其他存放位置。',unconfirmed:'尚未核对插卡实物，可能只有原表文字，也可能关联实物已不可见。'};
  $('cardFilterHelp').textContent=(notes[filters.cardStatus]||(filters.cardStatus===blank?'卡状态尚未登记；与人工登记的待确认分别筛选。':filters.cardStatus?'原样保留的历史状态，不自动当作其他状态。':'卡状态为人工登记，接码、归还和位置确认可独立叠加；多个条件同时满足才显示。'))+(filters.cardAssessment?' 注销核对沿用原稳定/改密规则，只按当前可见关联资料计算，不代表可以直接注销；不改变卡状态。':'')+(codeNotes[filters.cardCodeFilter]?' 接码：'+codeNotes[filters.cardCodeFilter]+'。':filters.cardCodeFilter===blank?' 接码事项尚未登记，与无待办分别筛选。':'')+(returnNotes[filters.cardReturnFilter]?' 归还：'+returnNotes[filters.cardReturnFilter]+'。':filters.cardReturnFilter===blank?' 归还事项尚未登记，与待确认分别筛选。':'')+(positionNotes[filters.cardPositionFilter]?' 位置：'+positionNotes[filters.cardPositionFilter]:'')+(filters.cardLinkedStateFilter?' 任一同注册手机号的可见关联账号被标记封禁。':'')+(filters.cardSourceFilter?' 来源疑点沿用原关键词规则，仅检查旧来源文字，不代表全部位置未确认。':'')+(filters.cardLocationQuery?' 未确认实物位置的卡也按原表文字搜索，搜索命中不会自动确认插卡。':'');
  $('cardFilterLoadHint').hidden=true;
 }
 function bind(){
  for(const id of ['cardOfficeFilter','cardAssessment','cardCodeFilter','cardReturnFilter','cardPositionFilter','cardLinkedStateFilter','cardSourceFilter'])if($(id))$(id).onchange=()=>{cardPageIndex=1;refresh();};
  if($('cardResetScope'))$('cardResetScope').onclick=()=>reset(true);
  document.addEventListener('click',event=>{
   const chip=event.target.closest('[data-card-filter-clear]');if(chip){clear(chip.dataset.cardFilterClear);return;}
   const quick=event.target.closest('[data-card-quick]');if(quick){const next=shortcut(read(),quick.dataset.cardQuick);for(const id of ['cardStatus','cardAssessment','cardTask'])$(id).value=next[id];cardPageIndex=1;refresh();return;}
   if(event.target.closest('[data-card-office-pending]')&&data.user?.scope_mode==='all'){$('cardOfficeFilter').value='pending';cardPageIndex=1;refresh();return;}
   if(event.target.closest('[data-card-empty-reset]'))reset(true);
  });
 }
 document.addEventListener('DOMContentLoaded',bind);
 return {read,position,scopeRows,filter,reasons,prepare,renderUI,shortcut,emptyReason,refresh,reset,clear,bind,blank};
})();
