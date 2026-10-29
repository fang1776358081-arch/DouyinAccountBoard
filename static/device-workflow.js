(function(){
 'use strict';
 const byId=id=>document.getElementById(id);
 const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const same=(a,b)=>String(a??'')===String(b??'');
 const board=()=>typeof data==='undefined'?null:data;
 const flow=()=>board()?.device_workflow||{};
 const enabled=()=>flow().enabled===true;
 const allowed=key=>!!board()?.user?.permissions?.includes(key);
 const unitById=id=>(flow().units||[]).find(row=>same(row.id,id));
 const accountById=id=>(board()?.accounts||[]).find(row=>same(row.id,id));
 const cardById=id=>(board()?.phone_cards?.cards||[]).find(row=>same(row.id,id));
 const deviceById=id=>(board()?.phone_devices?.devices||[]).find(row=>same(row.id,id));
 const accountName=row=>row?(typeof accountDisplay==='function'?accountDisplay(row):(row.name||'未命名账号')):'历史账号';
 const deviceName=row=>row?(typeof deviceDisplay==='function'?deviceDisplay(row,board()):row.name||'手机编号待确认'):'历史手机（当前不可见）';
 const unitName=row=>row?(typeof unitDisplay==='function'?unitDisplay(row,board()):row.label||row.device_name||'实物手机待确认'):'实物手机待确认';
 const unitReference=(id,label='')=>unitById(id)?unitName(unitById(id)):label||'实物手机待确认';
 const deviceReference=id=>deviceById(id)?deviceName(deviceById(id)):typeof deviceDisplay==='function'?'历史手机（当前不可见）':String(id);
 const unitTitle=row=>unitName(row)+(!row.active&&!unitName(row).includes('旧机')?' · 旧机':'');
 const usable=row=>!!row?.active&&['在用','可用闲置'].includes(row.status);
 let context=null,versionSnapshot=null,requestKey=null,saving=false;
 function button(text,attribute,value,primary=false){return `<button type="button" class="${primary?'button primary':'text-button'}" ${attribute}="${escape(value)}">${escape(text)}</button>`;}
 function options(values,value){return values.map(item=>`<option value="${escape(item)}" ${same(item,value)?'selected':''}>${escape(item)}</option>`).join('');}
 function field(name,label,value='',settings=''){return `<label>${escape(label)}<input name="${name}" aria-label="${escape(label)}" value="${escape(value)}" ${settings}></label>`;}
 function memo(name,label,value='',required=false){return `<label>${escape(label)}<textarea name="${name}" aria-label="${escape(label)}" rows="2" maxlength="2000" ${required?'required':''}>${escape(value)}</textarea></label>`;}
 function select(name,label,values,value){return `<label>${escape(label)}<select name="${name}" aria-label="${escape(label)}">${options(values,value)}</select></label>`;}
 function dateOwner(value={}){return `<div class="form-row">${field('owner','处理负责人',value.owner||board()?.user?.name||'','maxlength="100"')}${field('followup_on','下次跟进日期',value.followup_on||'','type="date"')}</div>`;}
 function form(action,body,submit='保存记录'){return `<form data-workflow-form="${action}">${body}<p class="workflow-form-error" role="alert"></p><button type="submit" class="button primary">${escape(submit)}</button></form>`;}
 function hidden(name,value){return `<input type="hidden" name="${name}" value="${escape(value)}">`;}
 function summary(label,value){return `<div><small>${escape(label)}</small><strong>${escape(value||'待确认')}</strong></div>`;}
 function positionText(card){const placement=card?.placement||(flow().placements||[]).find(row=>same(row.card_id,card?.id));if(placement?.unit_id)return `${unitReference(placement.unit_id,placement.unit_label)} · 卡槽${placement.slot||'待确认'}`;return placement?.storage||'待核对位置';}
 function accessText(account){const row=account?.device_access||(flow().access||[]).find(row=>same(row.account_id,account?.id));return row?`${row.login_state||'待确认'} · ${unitReference(row.last_unit_id,row.last_unit_label)}`:'登录实物待确认';}
 function dialog(){let node=byId('deviceWorkflowDialog');if(!node){node=document.createElement('dialog');node.id='deviceWorkflowDialog';node.className='workflow-dialog';document.body.append(node);node.addEventListener('close',()=>{context=null;versionSnapshot=null;requestKey=null;});node.addEventListener('cancel',event=>{if(saving)event.preventDefault();});}return node;}
 function open(title,content,nextContext){
  const node=dialog();context=nextContext;requestKey=null;versionSnapshot={...(board()?.record_versions||{})};
  node.innerHTML=`<div class="dialog-head"><div><small>设备、手机卡与账号处理记录</small><h2>${escape(title)}</h2></div><button type="button" class="text-button" data-workflow-close>关闭</button></div><div id="workflowContent">${content}</div><p id="workflowNotice" role="status"></p>`;
  if(typeof captureVersions==='function')captureVersions('deviceWorkflowDialog');
  if(!node.open)node.showModal();
 }
 const actions={open_case:'登记设备故障',update_case:'更新故障处理',unit_status:'更新实物状态',replace_unit:'新机补号',place_card:'移动手机卡',account_recovery:'更新账号登录恢复',reassign_unit:'返修手机重新编号'};
 function eventDetails(row){if(row.details&&typeof row.details==='object')return row.details;if(row.detail){try{return JSON.parse(row.detail)||{};}catch(_){return {note:row.detail};}}return {};}
 function eventTouchesUnit(row,unitId){const detail=eventDetails(row),ids=[row.unit_id,...['old_unit_id','new_unit_id','replaces_unit_id','from_unit_id','target_unit_id'].map(key=>detail[key]),detail.before?.unit_id,detail.after?.unit_id];return !!unitId&&ids.some(id=>id!=null&&same(id,unitId));}
 function caseEvent(row,entry){
  if(same(entry.case_id,row.id))return true;
  if(entry.case_id||entry.account_id)return false;
  const at=Number(entry.at),start=Number(row.created_at)||0,end=Number(row.closed_at)||Infinity;
  const actions=new Set(['unit_status','replace_unit','place_card','reassign_unit','更新实物状态','新机补号','新机登记','移动手机卡','核对手机卡位置','返修旧机重新编号']);
  return at>=start&&at<=end&&actions.has(entry.action)&&eventTouchesUnit(entry,row.unit_id);
 }
 function timeline(filter){
  const rows=(flow().events||[]).filter(filter).sort((a,b)=>Number(b.at)-Number(a.at));
  const labels={reason:'原因',note:'备注',status:'状态',before_status:'原状态',before_state:'原恢复进度',recovery_state:'恢复进度',login_state:'可操作情况',owner:'负责人',followup_on:'跟进日期',storage:'存放位置',slot:'卡槽',closure_reason:'结案结果',model:'机型',affected_account_ids:'受影响账号',unit_id:'实物手机',last_unit_id:'原登录实物',target_unit_id:'目标实物',previous_unit_id:'原实物',new_unit_id:'新实物',old_unit_id:'旧实物',replaces_unit_id:'接替的旧实物',from_unit_id:'原登录实物',device_id:'手机编号',card_id:'手机卡',account_id:'账号',before:'变更前',after:'变更后',revision:'记录版本',updated_at:'更新时间',generation:'第几台实物',cards_and_logins_inherited:'自动继承卡和登录记录',new_device_id:'新手机编号',original_device_id:'原手机编号'};
  function describe(key,value){if(value==null||value==='')return '未填写';if(key.includes('unit_id'))return unitName(unitById(value));if(key==='device_id'||key.endsWith('_device_id'))return deviceReference(value);if(key==='account_id')return accountName(accountById(value));if(key==='card_id')return cardById(value)?.phone||'历史手机卡';if(Array.isArray(value))return value.map(item=>accountName(accountById(item))).join('、')||'无';if(typeof value==='object')return Object.entries(value).filter(([name])=>!name.startsWith('_')).map(([name,item])=>`${labels[name]||name}：${describe(name,item)}`).join('；');return typeof value==='boolean'?(value?'是':'否'):String(value);}
  return `<h3>处理时间线</h3><ol class="workflow-timeline">${rows.map(row=>{const detail=eventDetails(row),text=typeof detail==='object'&&detail?Object.entries(detail).filter(([key])=>!['request_id','confirmed_login','confirmed_usable'].includes(key)&&!key.startsWith('_')).map(([key,value])=>`<small>${escape(labels[key]||key)}：${escape(describe(key,value))}</small>`).join(''):'';return `<li><strong>${escape(actions[row.action]||row.action)}</strong><span>${escape(row.at?new Date(Number(row.at)*1000).toLocaleString('zh-CN'):'')} · ${escape(row.actor_name||'操作人待确认')}</span>${text}</li>`;}).join('')||'<li>暂无处理记录；历史数据不会补造操作。</li>'}</ol>`;
 }
 function caseCards(rows){return rows.map(row=>`<article class="workflow-case"><strong>${escape(unitReference(row.unit_id,row.unit_label))}</strong><span class="badge ${row.status==='已结案'?'good':'warn'}">${escape(row.status)}</span><p>${escape(row.reason||'故障详情待补充')}</p><small>负责人 ${escape(row.owner||'待安排')}${row.followup_on?' · 跟进 '+escape(row.followup_on):''}</small>${button('处理详情 / 时间线','data-workflow-case',row.id)}</article>`).join('')||'<p class="card-help">暂无故障处理记录。</p>';}
 function renderCaseList(host,settings={}){if(typeof host==='string')host=byId(host);if(!host)return;if(!enabled()){host.innerHTML='';return;}const rows=(flow().cases||[]).filter(row=>settings.all||row.status!=='已结案');host.innerHTML=`<div class="workflow-case-list"><h3>设备故障与账号恢复${settings.all?'记录':'待办'}</h3>${caseCards(rows)}</div>`;}
 function relatedAccounts(unit){return (board()?.accounts||[]).filter(account=>{const access=account.device_access||(flow().access||[]).find(row=>same(row.account_id,account.id));return access?same(access.last_unit_id,unit.id):!!unit.active&&same(account.phone_device_id,unit.device_id);});}
 function deviceView(deviceId){
  if(!enabled())return;const device=deviceById(deviceId);if(!device)return;
  const units=(flow().units||[]).filter(unit=>same(unit.device_id,device.id)).sort((a,b)=>Number(b.generation)-Number(a.generation));
  const cards=board()?.phone_cards?.cards||[];
  const content=`<p class="workflow-help">手机编号可以由新机接替。每台实物的卡、账号和历史独立保留。</p>${units.map(unit=>{const accounts=relatedAccounts(unit),placed=cards.filter(card=>same(card.placement?.unit_id,unit.id)||(flow().placements||[]).some(p=>same(p.card_id,card.id)&&same(p.unit_id,unit.id)));return `<section class="workflow-unit"><div class="workflow-section-head"><h3>${escape(unitTitle(unit))}</h3><span class="badge ${usable(unit)?'good':'warn'}">${escape(unit.status)}</span></div><p>${escape(unit.model||'机型未登记')}${unit.note?' · '+escape(unit.note):''}</p><div class="workflow-summary">${summary('已确认位置的手机卡',placed.map(card=>`${card.phone}（卡槽${card.placement?.slot||(flow().placements||[]).find(p=>same(p.card_id,card.id))?.slot||'待确认'}）`).join('、')||'暂无明确插卡记录')}${summary('最后登录在此实物的账号',accounts.length+' 个')}</div><div class="workflow-account-list">${accounts.map(account=>`<div><strong>${escape(accountName(account))}</strong><small>${escape(accessText(account))}</small>${button('登录恢复','data-workflow-account',account.id)}</div>`).join('')}</div><div class="workflow-actions">${allowed('device_manage')?button('登记设备故障','data-workflow-fault',unit.id,true)+button('更新售后 / 实物状态','data-workflow-status',unit.id):''}${unit.active&&allowed('device_manage')&&['故障','送修','返修待验','回收'].includes(unit.status)?button('新机补齐此编号','data-workflow-replace',device.id):''}${placed.map(card=>button('移卡 '+card.phone,'data-workflow-card',card.id)).join('')}</div></section>`;}).join('')||'<p>当前没有可用的实物记录，请刷新资料后重试。</p>'}<h3>该编号的故障处理记录</h3>${caseCards((flow().cases||[]).filter(row=>units.some(unit=>same(unit.id,row.unit_id))))}${timeline(row=>units.some(unit=>eventTouchesUnit(row,unit.id)))}`;
  open(deviceName(device)+' · 故障 / 换机记录',content,{type:'device',id:device.id});
  if(allowed('device_manage')&&allowed('device_create'))units.forEach((unit,index)=>{if(!unit.active&&['返修待验','故障','待确认'].includes(unit.status))byId('workflowContent').querySelectorAll('.workflow-unit')[index]?.querySelector('.workflow-actions').insertAdjacentHTML('beforeend',button('返修手机重新编号','data-workflow-reassign',unit.id));});
  const renumbered=(flow().units||[]).filter(unit=>same(unit.original_device_id,device.id)&&!same(unit.device_id,device.id));if(renumbered.length)byId('workflowContent').insertAdjacentHTML('beforeend',`<h3>旧实物返修后重新编号</h3>${renumbered.map(unit=>`<section class="workflow-unit"><strong>${escape(unitName(unit))}</strong><p>原 ${escape(deviceName(device))} 的实物，现已使用新编号；卡和账号记录继续跟随该实物。</p>${button('查看现编号与实物历史','data-workflow-device',unit.device_id)}${caseCards((flow().cases||[]).filter(row=>same(row.unit_id,unit.id)))}</section>`).join('')}${timeline(row=>renumbered.some(unit=>eventTouchesUnit(row,unit.id)))}`);
 }
 function faultView(unitId){
  const unit=unitById(unitId);if(!unit||!allowed('device_manage'))return;
  const activeCase=(flow().cases||[]).find(row=>same(row.unit_id,unit.id)&&row.status!=='已结案');if(activeCase){caseView(activeCase.id);byId('workflowNotice').textContent='这台实物已有进行中的故障记录，请继续处理。';return;}
  const accounts=relatedAccounts(unit);
  open('登记故障 · '+unitName(unit),form('open_case',hidden('unit_id',unit.id)+memo('reason','故障情况 / 影响', '',true)+dateOwner()+`<fieldset><legend>确认受到影响的账号</legend><p class="workflow-help">只勾选实际无法操作的账号；手机卡保持原位置。</p><div class="workflow-check-list">${accounts.map(account=>`<label><input type="checkbox" name="affected_account_ids" value="${escape(account.id)}"><span>${escape(accountName(account))}<small>${escape(account.status)} · ${escape(accessText(account))}</small></span></label>`).join('')||'<p>未找到最后登录在本实物上的账号，可从账号页面单独登记恢复。</p>'}</div></fieldset>${allowed('nurture_manage')?'<label class="workflow-check"><input type="checkbox" name="pause_nurture"><span>同时暂停已勾选账号正在进行的养号计时，保留已累计时间</span></label>':''}`,'登记故障并生成处理记录'),{type:'device',id:unit.device_id});
 }
 function unitStatusView(unitId){
  const unit=unitById(unitId);if(!unit||!allowed('device_manage'))return;
  open('售后 / 实物状态 · '+unitName(unit),form('unit_status',hidden('unit_id',unit.id)+select('status','当前实物状态',flow().options?.unit_statuses||['待确认','在用','待清理','可用闲置','故障','送修','返修待验','回收'],unit.status)+memo('note','送修、返修验机或回收记录',unit.note||'')+'<p class="workflow-help">实物状态与账号是否已恢复分别确认。返修完成后，仍需核对账号登录情况。</p>'),{type:'device',id:unit.device_id});
 }
 function replaceView(deviceId){
  const device=deviceById(deviceId),unit=(flow().units||[]).find(row=>same(row.device_id,deviceId)&&row.active);if(!device||!unit||!allowed('device_manage'))return;
  open('新机补号 · '+deviceName(device),form('replace_unit',hidden('device_id',device.id)+`<p class="workflow-help">旧实物 ${escape(unitName(unit))} 的记录会保留。新机接替 ${escape(deviceName(device))}，手机卡和账号按实际操作另外确认。</p>`+field('model','新手机机型 / 区分信息','','maxlength="100" required')+select('new_status','新手机当前状态',['待确认','可用闲置'],'待确认')+memo('note','新机登记备注')+'<label class="workflow-check"><input type="checkbox" required><span>确认是另一台新实物接替该编号，已核对旧机去向</span></label>','确认登记新手机'),{type:'device',id:device.id});
 }
 function reassignView(unitId){
  const unit=unitById(unitId);if(!unit||unit.active||!allowed('device_manage')||!allowed('device_create'))return;
  const scoped=!!board()?.user?.scope_mode,device=deviceById(unit.device_id);
  if(scoped&&(!Array.isArray(device?.office_ids)||device.office_ids.length!==1)){open('返修手机重新编号',`<p class="workflow-help">${escape(unitName(unit))} 的办公室归属${device?.office_ids?.length>1?'存在多项':'待核对'}。请先核对手机设备资料，确认实物只归属一个办公室，再重新编号。</p>`,{type:'device',id:unit.device_id});return;}
  const numberField=scoped?'number':'device_id',prefix=scoped?'':hidden('device_prefix',unit.device_id.startsWith('office3:')?'office3:':'');
  open('返修手机重新编号',form('reassign_unit',hidden('unit_id',unit.id)+prefix+`<p class="workflow-help">${escape(unitName(unit))} 返修后，原编号已被新机接替。选择尚未登记的新编号，保留这台实物上的卡、账号与处理历史；办公室归属沿用原记录。</p>`+field(numberField,'尚未登记的新手机编号','','pattern="[0-9]{1,6}" inputmode="numeric" maxlength="6" required')+field('name','新编号名称（选填）','','maxlength="100"')+'<label class="workflow-check"><input name="confirmed_usable" type="checkbox" required><span>已实际复检返修手机，确认能够正常使用并重新贴号</span></label>','确认重新编号'),{type:'device',id:unit.device_id});
 }
 function unitSelect(name,label,value='',onlyUsable=true){const units=(flow().units||[]).filter(unit=>onlyUsable?usable(unit):true);return `<label>${escape(label)}<select name="${name}" aria-label="${escape(label)}"><option value="">请选择${onlyUsable?'已确认可用的':''}实物手机</option>${units.map(unit=>`<option value="${escape(unit.id)}" ${same(unit.id,value)?'selected':''}>${escape(unitName(unit))} · ${escape(unit.status)}</option>`).join('')}</select></label>`;}
 function cardView(cardId){
  if(!enabled())return;const card=cardById(cardId);if(!card)return;
  const placement=card.placement||(flow().placements||[]).find(row=>same(row.card_id,card.id));
  let content=`<div class="workflow-summary">${summary('当前位置',positionText(card))}${summary('原文字位置（仅参考）',card.location||'未填写')}</div><p class="workflow-help">按实际拔卡、插卡的位置更新；移卡后，账号仍保留原登录设备记录。</p>`;
  if(allowed('card_manage')&&allowed('device_manage'))content+=form('place_card',hidden('card_id',card.id)+select('placement_mode','此次实际去向',['插入手机','核对原本插卡位置','卡盒 / 待安置'],placement?.unit_id?'核对原本插卡位置':'卡盒 / 待安置')+`<div data-workflow-phone-placement>${unitSelect('unit_id','实物手机',placement?.unit_id||'',false)}<div class="form-row">${select('slot','卡槽',['1','2','待确认'],placement?.slot||'待确认')}</div><label class="workflow-check"><input name="confirmed_usable" type="checkbox"><span>已核对实物和卡槽，确认手机卡实际位于所选实物</span></label><p class="workflow-help">“核对原本插卡位置”可记录卡仍在故障旧机，槽位未知可选“待确认”；实际搬卡请选“插入手机”。</p></div><div data-workflow-storage-placement>${field('storage','具体存放位置',placement?.storage||'','maxlength="200" placeholder="例如：卡盒第3排 / 待核对位置"')}</div>`,'确认更新卡位置');
  content+=timeline(row=>same(row.card_id,card.id));open('手机卡移位 · '+card.phone,content,{type:'card',id:card.id});togglePlacement();
 }
 function togglePlacement(){const node=byId('deviceWorkflowDialog'),mode=node?.querySelector('[name="placement_mode"]')?.value;if(!mode)return;const inPhone=mode!=='卡盒 / 待安置',unitSelect=node.querySelector('[name="unit_id"]'),slot=node.querySelector('[name="slot"]'),selected=unitSelect.value,selectedSlot=slot.value;node.querySelector('[data-workflow-phone-placement]').hidden=!inPhone;node.querySelector('[data-workflow-storage-placement]').hidden=inPhone;const units=(flow().units||[]).filter(unit=>mode==='核对原本插卡位置'||usable(unit));unitSelect.innerHTML='<option value="">请选择实物手机</option>'+units.map(unit=>`<option value="${escape(unit.id)}">${escape(unitName(unit))} · ${escape(unit.status)}</option>`).join('');unitSelect.value=units.some(unit=>same(unit.id,selected))?selected:'';slot.innerHTML=options(mode==='核对原本插卡位置'?['1','2','待确认']:['1','2'],selectedSlot);}
 function accountView(accountId){
  if(!enabled())return;const account=accountById(accountId);if(!account)return;
  const access=account.device_access||(flow().access||[]).find(row=>same(row.account_id,account.id))||{};
  let content=`<div class="workflow-summary">${summary('最后确认登录实物',unitReference(access.last_unit_id,access.last_unit_label))}${summary('现在能否操作',access.login_state||'待确认')}${summary('恢复进度',access.recovery_state||'待处理')}</div><p class="workflow-help">账号状态 ${escape(account.status)} 单独保留。只有新手机实际登录成功，才更新最后确认登录实物。</p>`;
  if(allowed('device_manage')){const cases=(flow().cases||[]).filter(row=>row.status!=='已结案'&&((row.account_ids||[]).some(id=>same(id,account.id))||same(row.id,access.case_id)||same(row.unit_id,access.last_unit_id)));content+=form('account_recovery',hidden('account_id',account.id)+select('recovery_state','恢复进度',flow().options?.recovery_states||['待处理','等待协助','恢复成功','暂不恢复','无法恢复'],access.recovery_state||'待处理')+unitSelect('target_unit_id','准备恢复到 / 已成功登录的实物手机',access.target_unit_id||'')+`<label>关联故障处理<select name="case_id"><option value="">未关联故障处理</option>${cases.map(row=>`<option value="${escape(row.id)}" ${same(row.id,access.case_id)?'selected':''}>${escape(unitReference(row.unit_id,row.unit_label))} · ${escape(row.reason)}</option>`).join('')}</select></label>`+memo('reason','受阻原因 / 本次恢复结果',access.reason||'')+dateOwner(access)+'<label class="workflow-check" data-workflow-login-confirm><input type="checkbox" name="confirmed_login"><span>已在所选实物手机实际登录成功，当前可以操作此账号</span></label>','保存账号恢复记录');}
  content+=timeline(row=>same(row.account_id,account.id));open('登录恢复 · '+accountName(account),content,{type:'account',id:account.id});toggleRecovery();
 }
 function toggleRecovery(){const node=byId('deviceWorkflowDialog'),state=node?.querySelector('[name="recovery_state"]')?.value,confirm=node?.querySelector('[data-workflow-login-confirm]');if(!confirm)return;confirm.hidden=state!=='恢复成功';confirm.querySelector('input').required=state==='恢复成功';}
 function caseView(caseId){
  if(!enabled())return;const row=(flow().cases||[]).find(item=>same(item.id,caseId));if(!row)return;
  const unit=unitById(row.unit_id),accounts=(row.account_ids||[]).map(accountById).filter(Boolean);
  let content=`<div class="workflow-summary">${summary('故障实物',unitReference(row.unit_id,row.unit_label))}${summary('处理进度',row.status)}${summary('负责人',row.owner||'待安排')}</div><p>${escape(row.reason)}</p><div class="workflow-actions">${unit?button('查看实物与卡位置','data-workflow-device',unit.device_id):''}${allowed('device_manage')&&unit?button('记录售后 / 实物状态','data-workflow-status',unit.id):''}</div><h3>受影响账号（分别恢复）</h3><div class="workflow-account-list">${accounts.map(account=>`<div><strong>${escape(accountName(account))}</strong><small>${escape(accessText(account))} · ${escape(account.device_access?.recovery_state||'待处理')}</small>${button('登录恢复','data-workflow-account',account.id)}</div>`).join('')||'<p>本次未登记受影响账号。</p>'}</div>`;
  if(allowed('device_manage')&&row.status!=='已结案')content+=`<details class="workflow-edit" open><summary>更新处理 / 暂挂 / 结案</summary>${form('update_case',hidden('case_id',row.id)+select('status','处理进度',flow().options?.case_statuses||['待处理','处理中','暂挂','已结案'],row.status)+dateOwner(row)+memo('note','当前进度 / 下一步',row.note||'')+memo('closure_reason','结案结果（结案时填写，可记录部分账号未恢复）',row.closure_reason||''))}</details>`;
  else content+=`<p class="workflow-help">${escape(row.note||'')}${row.closure_reason?'<br>结案结果：'+escape(row.closure_reason):''}</p>`;
  content+=timeline(event=>caseEvent(row,event));open('故障处理详情',content,{type:'case',id:row.id});
 }
 function payloadFor(formNode){
  const raw=new FormData(formNode),action=formNode.dataset.workflowForm,payload={action};
  for(const [key,value] of raw){if(!['affected_account_ids','placement_mode','confirmed_usable','confirmed_login','pause_nurture'].includes(key))payload[key]=String(value).trim();}
  if(action==='open_case'){payload.affected_account_ids=raw.getAll('affected_account_ids');payload.pause_nurture=raw.has('pause_nurture');}
  if(action==='reassign_unit'){if(!raw.has('confirmed_usable'))throw Error('请先复检并确认返修机可用');if(board()?.user?.scope_mode){if(!/^\d{1,6}$/.test(payload.number||''))throw Error('请填写有效的新手机编号');delete payload.device_id;}else{payload.device_id=(payload.device_prefix||'')+payload.device_id;delete payload.number;}delete payload.device_prefix;payload.confirmed_usable=true;}
  if(action==='place_card'){
   payload.card_id=Number(payload.card_id);
   if(raw.get('placement_mode')!=='卡盒 / 待安置'){if(!payload.unit_id)throw Error('请选择实际插卡的实物手机');if(!raw.has('confirmed_usable'))throw Error('请先核对实际插卡及卡槽，再勾选确认');payload.confirmed_usable=true;if(raw.get('placement_mode')==='核对原本插卡位置')payload.confirm_existing=true;delete payload.storage;}
   else{if(!payload.storage)throw Error('请填写具体存放位置，未核对时可填写“待核对位置”');delete payload.unit_id;delete payload.slot;}
  }
  if(action==='account_recovery'){if(!payload.case_id)payload.case_id=null;if(!payload.target_unit_id)payload.target_unit_id=null;if(payload.recovery_state==='恢复成功'){if(!payload.target_unit_id)throw Error('恢复成功需要选择实际登录的实物手机');if(!raw.has('confirmed_login'))throw Error('请确认已经实际登录成功');payload.confirmed_login=true;}}
  if(action==='update_case'&&payload.status==='已结案'&&!payload.closure_reason)throw Error('请填写结案结果，说明设备、卡和账号各自的处理结果');
  if(action==='update_case'&&payload.status==='暂挂'&&!payload.followup_on)throw Error('暂挂时请填写下次跟进日期');
  return payload;
 }
 async function save(formNode,event){
  const errorNode=formNode.querySelector('.workflow-form-error'),submit=event.submitter||formNode.querySelector('[type="submit"]');errorNode.textContent='';let payload;
  try{payload=payloadFor(formNode);}catch(error){errorNode.textContent=error.message;return;}
  const previous={...context};submit.disabled=true;saving=true;byId('workflowNotice').textContent='正在保存…';let saved=false;
  const fingerprint='device-workflow:'+JSON.stringify(payload);requestKey=typeof claimRequestId==='function'?claimRequestId(fingerprint):requestKey;
  if(requestKey)payload.request_id=requestKey;
  try{
   let result;
   if(typeof send==='function')result=await send('/api/device-workflow',payload,versionSnapshot);
   else if(typeof api==='function')result=await api('/api/device-workflow',payload);
   else throw Error('页面未准备好，请刷新后重试');
   saved=true;
   if(typeof settleRequestId==='function')settleRequestId(fingerprint);
   if(typeof reload==='function')await reload(true);else if(typeof load==='function')await load(true);
   if(payload.action==='reassign_unit'&&result?.device_id)previous.id=result.device_id;
   if(payload.action==='open_case'&&result?.case_id){previous.type='case';previous.id=result.case_id;}
   refresh(previous);if(byId('workflowNotice'))byId('workflowNotice').textContent='已保存处理记录。';
  }catch(error){if(saved){errorNode.textContent='记录已保存，但最新资料读取失败：'+(error.message||'请关闭后刷新页面确认');byId('workflowNotice').textContent='保存已完成，请勿重复提交。';}else{if(typeof settleRequestId==='function')settleRequestId(fingerprint,error);errorNode.textContent=error.message||'保存失败，请重试';byId('workflowNotice').textContent=error.status===undefined?'结果待确认，请保持表单并重试；系统会查询同一次请求的结果。':'';submit.disabled=false;}}finally{saving=false;}
 }
 function refresh(previous=context){if(!previous)return;if(previous.type==='device')deviceView(previous.id);if(previous.type==='card')cardView(previous.id);if(previous.type==='account')accountView(previous.id);if(previous.type==='case')caseView(previous.id);}
 document.addEventListener('click',event=>{
  const target=event.target.closest('[data-workflow-close],[data-workflow-device],[data-workflow-card],[data-workflow-account],[data-workflow-case],[data-workflow-fault],[data-workflow-status],[data-workflow-replace],[data-workflow-reassign]');if(!target)return;
  if(saving&&target.closest('#deviceWorkflowDialog'))return;
  if(target.hasAttribute('data-workflow-close'))dialog().close();
  for(const [attribute,handler] of [['workflowDevice',deviceView],['workflowCard',cardView],['workflowAccount',accountView],['workflowCase',caseView],['workflowFault',faultView],['workflowStatus',unitStatusView],['workflowReplace',replaceView],['workflowReassign',reassignView]])if(attribute in target.dataset)handler(target.dataset[attribute]);
 });
 document.addEventListener('change',event=>{if(!event.target.closest('#deviceWorkflowDialog'))return;if(event.target.name==='placement_mode')togglePlacement();if(event.target.name==='recovery_state')toggleRecovery();});
 document.addEventListener('submit',event=>{const node=event.target.closest('[data-workflow-form]');if(node){event.preventDefault();save(node,event);}});
 window.DeviceWorkflow={enabled,openDevice:deviceView,openCard:cardView,openAccount:accountView,openCase:caseView,renderCaseList,refresh,positionText,accessText,isOpen:()=>!!byId('deviceWorkflowDialog')?.open};
})();
