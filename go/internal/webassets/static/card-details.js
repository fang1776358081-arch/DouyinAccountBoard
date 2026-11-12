/* Read-only details from the already authorized board snapshot. No requests. */
window.CardDetails=(()=>{
 let node=null,selectedId=null,openedUser=null;
 const board=()=>typeof data==='undefined'?null:data;
 const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const text=value=>value===undefined||value===null||value===''?'未登记':String(value);
 const row=(label,value)=>`<div class="card-details-row"><dt>${escape(label)}</dt><dd>${escape(text(value))}</dd></div>`;
 const section=(title,content)=>`<section class="card-details-section"><h3>${escape(title)}</h3>${content}</section>`;
 function visibleCard(id){const current=board();if(!current)return null;const cards=window.CardFilters?CardFilters.scopeRows(current,'all'):current.phone_cards?.cards||[];return cards.find(card=>String(card.id)===String(id))||null;}
 function dialog(){
  if(node)return node;node=document.createElement('dialog');node.id='cardDetailsDialog';node.className='card-details-drawer';node.setAttribute('aria-labelledby','cardDetailsTitle');document.body.appendChild(node);
  node.addEventListener('close',()=>{selectedId=null;openedUser=null;node.innerHTML='';});return node;
 }
 function reset(){if(node?.open)node.close();selectedId=null;openedUser=null;if(node)node.innerHTML='';}
 function render(){
  const current=board(),card=visibleCard(selectedId);if(!current||!card||String(current.user?.id)!==openedUser){reset();return false;}
  const lookup=cardAccountLookup(),related=cardAccounts(card.phone,lookup),workflow=!!current.device_workflow?.enabled,position=window.CardFilters?CardFilters.position(card,lookup,current):null;
  const location=workflow&&window.DeviceWorkflow?DeviceWorkflow.positionText(card):position?.kind==='phone'?position.value:position?.kind==='storage'?position.value:workflow?'实物位置待核对':card.location;
  const positionNote=workflow?(position?.kind==='phone'?'已记录插卡实物；与账号登录设备分别记录。':position?.kind==='storage'?'已记录卡盒或存放位置。':'尚未确认插卡实物；原表文字仅供参考。'):'原表文字位置仅供参考，不代表已核对插卡实物。';
  const reasons=window.CardFilters?CardFilters.reasons(card,lookup):[cardReady(card,lookup,related)?'原稳定改密条件已记录；仍需人工核对接码、验证及找回用途':'原稳定改密条件尚未齐全，需人工核对'];
  const accounts=related.length?related.map(account=>`<li class="card-details-account"><strong>${escape(typeof accountDisplay==='function'?accountDisplay(account):account.name)}</strong><dl class="card-details-grid">${row('账号类型',account.account_type)}${row('账号状态',account.status)}${row('登录手机',account.phone_device_name||'手机待确认')}${row('所属电脑',lookup.computersById.get(account.computer_id)?.name||'未分配电脑')}${row('稳定情况',account.card_stability)}${row('改密情况',account.password_state)}</dl></li>`).join(''):'<li class="card-details-empty">暂无同注册手机号的可见关联账号。</li>';
  const content=section('手机卡资料',`<dl class="card-details-grid">${row('手机号码',card.phone)}${row('手机卡编号 / 名称',card.label)}${row('运营商',cardCarrierLabel(card))}${row('卡状态',card.status||'未登记（空白）')}${row('卡主 / 提供人',card.owner)}${row('卡归属',card.ownership)}${row('内部负责人',card.keeper)}${row('到期日期',card.expires_on)}</dl>`)
   +section('位置记录',`<dl class="card-details-grid">${row(workflow?'登记的实物 / 存放位置':'位置参考',location)}${row('原文字位置（仅参考）',card.location)}</dl><p class="card-details-help">${escape(positionNote)}</p>`)
   +section('接码事项',`<dl class="card-details-grid">${row('接码状态',card.code_state||'未登记（空白）')}${row('接码联系人 / 联系方式',card.code_contact)}</dl><p class="card-details-text">${escape(card.code_note||'暂无接码处理记录')}</p>`)
   +section('归还事项',`<dl class="card-details-grid">${row('归还状态',card.return_state||'未登记（空白）')}${row('预计归还日期',card.return_due)}${row('实际归还日期',card.returned_on)}</dl>`)
   +section('注销核对说明',`<ul class="card-details-reasons">${reasons.map(reason=>`<li>${escape(reason)}</li>`).join('')}</ul><p class="card-details-help">条件仅按当前可见关联资料核对，不代表可以直接注销；卡状态仍由人工维护。</p>`)
   +section('关联账号（'+related.length+'）',`<p class="card-details-help">按账号注册手机号匹配；移动手机卡不会迁移账号登录设备。</p><ul class="card-details-accounts">${accounts}</ul>`)
   +section('完整备注',`<p class="card-details-text">${escape(card.note||'暂无备注')}</p>`)
   +section('腾讯文档导入原文 / 待核对信息',`<pre class="card-details-source">${escape(card.source_note||'暂无腾讯文档导入原文')}</pre>`);
  const inScope=current.user?.scope_mode==='all'||!!card.office_ids?.length&&card.office_ids.every(office=>current.user?.office_ids?.includes(office));
  const canManage=inScope&&hasPermission('card_manage');
  const canEdit=canManage||inScope&&hasPermission('card_tasks')||hasPermission('account_progress');
  const actions=`${canEdit?'<button type="button" class="button" data-card-details-edit>修改详细资料</button>':''}${workflow?'<button type="button" class="button" data-card-details-position>'+(canManage?'移卡 / 位置记录':'查看位置记录')+'</button>':''}`;
  dialog().innerHTML=`<div class="card-details-header"><div><h2 id="cardDetailsTitle">手机卡详情</h2><p>${escape(card.phone)}</p></div><button type="button" class="text-button" data-card-details-close aria-label="关闭手机卡详情">关闭</button></div><div class="card-details-content">${actions?'<div class="card-details-actions">'+actions+'</div>':''}${content}</div>`;
  node.querySelector('[data-card-details-close]').onclick=reset;
  const edit=node.querySelector('[data-card-details-edit]');
  if(edit)edit.onclick=()=>{reset();editCard(Number(card.id));};
  const positionButton=node.querySelector('[data-card-details-position]');
  if(positionButton)positionButton.onclick=()=>{reset();window.DeviceWorkflow?.openCard(card.id);};
  return true;
 }
 function open(id){
  if(window.AccountInline?.hasDraft()){AccountInline.announceBlocked();return false;}
  const current=board();if(!current||!visibleCard(id))return false;
  const other=document.querySelector?.('dialog[open]');if(other&&other!==node)return false;
  selectedId=String(id);openedUser=String(current.user?.id);if(!render())return false;dialog().showModal();return true;
 }
 document.addEventListener('click',event=>{const button=event.target.closest?.('[data-card-details]');if(button){event.preventDefault?.();open(button.dataset.cardDetails);}});
 document.addEventListener('board-ready',()=>{if(node?.open)render();});
 return Object.freeze({open,render,reset,isOpen:()=>!!node?.open});
})();
