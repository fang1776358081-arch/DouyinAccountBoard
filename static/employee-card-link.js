/* Explicit real-name links from the authorized card selection. Original cards stay unchanged. */
window.EmployeeCardLink=(()=>{
 const $=id=>document.getElementById(id),esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 let draft=null,serial=0;
 const board=()=>typeof data!=='undefined'?data:globalThis.data;
 const canManage=()=>typeof hasPermission==='function'&&hasPermission('card_manage');
 const selected=()=>typeof batchSelection==='function'?batchSelection('cards'):new Set();
 const active=()=>!!draft&&!draft.completed;
 const isOpen=()=>!!$('employeeCardLinkDialog')?.open;
 function announceBlocked(){if(isOpen())message(draft?.uncertain?'保存结果尚未确认，请保留本批内容并核对原请求。':'请先完成或取消“关联员工”，当前输入已保留。');else if(typeof toast==='function')toast('请先完成或取消关联员工，当前输入已保留。');}
 function message(value=''){if($('employeeCardLinkError')){$('employeeCardLinkError').textContent=value;$('employeeCardLinkError').hidden=!value;}}
 function ensureDialog(){
  if($('employeeCardLinkDialog'))return;
  const dialog=document.createElement('dialog');dialog.id='employeeCardLinkDialog';dialog.setAttribute('aria-labelledby','employeeCardLinkTitle');
  dialog.innerHTML=`<form id="employeeCardLinkForm"><div class="dialog-head"><div><small>手机卡实名台账</small><h2 id="employeeCardLinkTitle">关联员工</h2></div><button type="button" class="text-button" id="employeeCardLinkClose">关闭</button></div><p class="employee-link-note">明确确认实名员工后，建立实名关系，不修改原手机卡资料。卡主 / 提供人、保管人不等于实名员工。最多每批 200 张。</p><div id="employeeCardLinkReadState" role="status"></div><div id="employeeCardLinkInputs"><fieldset><legend>1. 选择实名员工</legend><div class="employee-link-search"><label>姓名或员工编号<input id="employeeCardLinkQuery" autocomplete="off" placeholder="支持没有关联卡片的员工"></label><button type="button" class="button" id="employeeCardLinkSearch">搜索员工</button></div><div id="employeeCardLinkEmployees"></div><div class="employee-link-pages"><button type="button" class="text-button" id="employeeCardLinkPrevious">上一页</button><span id="employeeCardLinkPage"></span><button type="button" class="text-button" id="employeeCardLinkNext">下一页</button></div><p id="employeeCardLinkEmployee" role="status">尚未选择实名员工</p></fieldset><fieldset><legend>2. 核对本批卡片</legend><div class="employee-link-fill"><label>实际发卡运营商<select id="employeeCardLinkUniform"><option value="">请明确选择，不按原运营商猜测</option></select></label><button type="button" class="button" id="employeeCardLinkFill">填充全部所选卡</button></div><p class="employee-link-note">原运营商仅作参考。办理日期未知可留空，不会以今天补写；重复关联保留原办理日期。</p><div class="employee-link-scroll"><table><thead><tr><th>手机号码</th><th>原运营商（参考）</th><th>实际发卡运营商</th><th>办理日期（未知可留空）</th></tr></thead><tbody id="employeeCardLinkRows"></tbody></table></div><label>实名核对来源 / 依据（必填）<textarea id="employeeCardLinkSource" rows="2" maxlength="500" placeholder="例如：员工本人确认的实名卡清单及核对日期"></textarea></label></fieldset></div><p id="employeeCardLinkError" role="alert" hidden></p><div id="employeeCardLinkPreview"></div><div id="employeeCardLinkReceipt" role="status"></div><div class="employee-link-actions"><button type="submit" class="button" id="employeeCardLinkPrepare">预览关联</button><button type="button" class="button primary" id="employeeCardLinkApply" disabled>确认建立实名关联</button></div></form>`;
  document.body.append(dialog);
  $('employeeCardLinkForm').onsubmit=event=>{event.preventDefault();preview();};
  $('employeeCardLinkClose').onclick=close;
  $('employeeCardLinkSearch').onclick=()=>loadEmployees(1);
  $('employeeCardLinkPrevious').onclick=()=>loadEmployees(draft.page-1);
  $('employeeCardLinkNext').onclick=()=>loadEmployees(draft.page+1);
  $('employeeCardLinkFill').onclick=fillOperator;
  $('employeeCardLinkApply').onclick=apply;
  $('employeeCardLinkQuery').onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();loadEmployees(1);}};
  dialog.addEventListener('cancel',event=>{if(draft?.saving||draft?.uncertain){event.preventDefault();announceBlocked();}});
  dialog.addEventListener('close',()=>{if(!draft?.saving&&!draft?.uncertain){draft=null;serial++;}});
  dialog.addEventListener('input',change);
  dialog.addEventListener('change',change);
  dialog.addEventListener('click',event=>{const choice=event.target.closest('[data-employee-link-person]');if(choice)chooseEmployee(choice.dataset.employeeLinkPerson);});
 }
 function lock(){
  const frozen=!!(draft?.saving||draft?.uncertain||draft?.identityBlocked||draft?.completed||draft?.loading);
  $('employeeCardLinkInputs').querySelectorAll('input,select,textarea,button').forEach(node=>node.disabled=frozen);
  if(!draft?.meta){$('employeeCardLinkRows').querySelectorAll('input,select').forEach(node=>node.disabled=true);$('employeeCardLinkSource').disabled=true;$('employeeCardLinkUniform').disabled=true;$('employeeCardLinkFill').disabled=true;}
  $('employeeCardLinkPrepare').disabled=frozen||!draft?.meta||draft?.readFailed===true;
  $('employeeCardLinkClose').disabled=!!(draft?.saving||draft?.uncertain);
  $('employeeCardLinkApply').disabled=!draft?.prepared||draft.prepared.can_apply!==true||draft.prepared.has_conflicts!==false||draft.saving||draft.loading||draft.readFailed||draft.identityBlocked||draft.completed;
  $('employeeCardLinkApply').textContent=draft?.uncertain?'核对并重试原请求':'确认建立实名关联';
  if(!frozen){$('employeeCardLinkPrevious').disabled=draft.page<=1||draft.loading;$('employeeCardLinkNext').disabled=draft.page*draft.pageSize>=draft.total||draft.loading;}
 }
 function invalidate(){if(!draft||draft.saving||draft.uncertain)return;draft.prepared=null;draft.requestId=null;$('employeeCardLinkPreview').innerHTML='';lock();}
 function change(event){
  if(!active()||draft.saving||draft.uncertain)return;
  const field=event.target,index=Number(field.dataset.employeeLinkIndex);
  if(field.id==='employeeCardLinkSource'){draft.source=field.value;invalidate();}
  else if(field.dataset.employeeLinkField&&Number.isInteger(index)&&draft.cards[index]){draft.cards[index][field.dataset.employeeLinkField]=field.value;invalidate();}
 }
 function operatorOptions(value=''){return '<option value="">请选择实际发卡运营商</option>'+(draft?.meta?.operators||[]).map(op=>`<option value="${esc(op.id)}" ${String(op.id)===value?'selected':''}>${esc(op.name)}</option>`).join('');}
 function renderCards(){
  $('employeeCardLinkUniform').innerHTML=operatorOptions();
  $('employeeCardLinkRows').innerHTML=draft.cards.map((card,index)=>`<tr><td>${esc(card.phone)}</td><td class="employee-link-muted">${esc(card.carrier)||'未登记'}</td><td><select id="employeeCardLinkOperator-${index}" data-employee-link-index="${index}" data-employee-link-field="operator_id" aria-label="${esc(card.phone)} 实际发卡运营商">${operatorOptions(card.operator_id)}</select></td><td><input id="employeeCardLinkDate-${index}" data-employee-link-index="${index}" data-employee-link-field="activated_on" type="date" value="${esc(card.activated_on)}" aria-label="${esc(card.phone)} 办理日期，未知可留空"></td></tr>`).join('');
 }
 async function request(url,payload){
  let response;try{response=await fetch(url,{method:payload?'POST':'GET',credentials:'same-origin',cache:'no-store',headers:payload?{'Content-Type':'application/json','X-CSRF-Token':draft?.meta?.user?.csrf||''}:undefined,body:payload?JSON.stringify(payload):undefined});}catch{throw Object.assign(Error('网络未能返回结果，请保留输入后重试。'),{uncertain:true});}
  let value;try{value=await response.json();}catch{throw Object.assign(Error('服务器未返回可核对的结果，请保留输入。'),{uncertain:true,status:response.status});}
  if(!response.ok){const error=Object.assign(Error(value?.error||value?.message||'请求失败，请保留输入后重试。'),{status:response.status,uncertain:response.status>=500});throw error;}
  return value;
 }
 function blockIdentity(){
  draft.cards=[];draft.employee=null;draft.meta=null;draft.source='';draft.prepared=null;draft.requestId=null;draft.identityBlocked=true;selected().clear();
  $('employeeCardLinkRows').innerHTML='';$('employeeCardLinkEmployees').innerHTML='';$('employeeCardLinkSource').value='';$('employeeCardLinkEmployee').textContent='登录身份已变化，本批选择已清除';$('employeeCardLinkPreview').innerHTML='';message('当前登录身份与选择手机卡时不同，请关闭窗口并在当前账号下重新读取、选择手机卡。');lock();
 }
 async function loadEmployees(page=1){
  if(!draft||draft.saving||draft.uncertain||draft.identityBlocked)return false;
  const own=++serial,current=draft,query=new URLSearchParams({page:String(Math.max(1,page)),page_size:'50'}),search=$('employeeCardLinkQuery').value;
  if(search)query.set('query',search);draft.loading=true;$('employeeCardLinkReadState').textContent='正在读取当前账号可管理的员工…';message();lock();
  try{
   const value=await request('/api/employee-cards?'+query);
   if(own!==serial||draft!==current)return false;
   if(String(value?.user?.id)!==draft.actor){blockIdentity();return false;}
   if(value.available===false)throw Error(value.message||'员工模块尚未准备，原手机卡资料不变。');
   if(!(value.can_manage===true||value.capabilities?.can_manage===true))throw Error('当前账号没有建立员工实名关系的管理权限。');
   if(!Array.isArray(value.employees)||!Array.isArray(value.operators)||!Number.isInteger(value.total))throw Error('员工列表不完整，无法确认可选范围，请重试。');
   draft.meta=value;draft.readFailed=false;draft.people=value.employees;draft.page=value.page||page;draft.total=value.total;draft.pageSize=value.page_size||50;
   $('employeeCardLinkEmployees').innerHTML=value.employees.length?value.employees.map(employee=>`<button type="button" class="employee-link-person" data-employee-link-person="${esc(employee.id)}"><strong>${esc(employee.name)}</strong><span>编号 ${esc(employee.employee_no)||'未登记'}</span><small>选择此员工</small></button>`).join(''):'<p class="employee-link-muted">没有匹配的授权员工。可调整搜索，或先到员工卡片台账新增员工。</p>';
   $('employeeCardLinkPage').textContent=`第 ${draft.page} / ${Math.max(1,Math.ceil(draft.total/draft.pageSize))} 页 · 共 ${draft.total} 位`;
   renderCards();$('employeeCardLinkReadState').textContent=`已选 ${draft.cards.length} 张手机卡 · 员工范围由当前账号权限决定`;return true;
  }catch(error){if(own!==serial||draft!==current)return false;draft.readFailed=true;message(error.message);$('employeeCardLinkReadState').textContent='员工读取失败，当前输入已保留，可搜索重试';return false;}
  finally{if(own===serial&&draft===current){draft.loading=false;lock();}}
 }
 function chooseEmployee(id){
  if(!active()||draft.saving||draft.uncertain||!draft.meta)return false;
  const person=draft.people.find(row=>String(row.id)===String(id));if(!person||!person.version){message('此员工版本不可核对，请搜索刷新后重新选择。');return false;}
  draft.employee={id:String(person.id),name:person.name,employee_no:person.employee_no,version:person.version};
  $('employeeCardLinkEmployee').textContent=`本批实名员工：${person.name} · 编号 ${person.employee_no||'未登记'}（全部 ${draft.cards.length} 张）`;invalidate();message();return true;
 }
 function fillOperator(){
  if(!active()||draft.saving||draft.uncertain)return false;const value=$('employeeCardLinkUniform').value;
  if(!(draft.meta?.operators||[]).some(op=>String(op.id)===value)){message('请先明确选择一个实际发卡运营商。');return false;}
  for(const card of draft.cards)card.operator_id=value;renderCards();invalidate();message();return true;
 }
 function payload(){
  if(!draft?.employee)throw Error('请先明确选择本批实名员工。');
  if(!draft.source.trim())throw Error('请填写实名核对来源 / 依据。');
  const allowed=new Set((draft.meta?.operators||[]).map(op=>String(op.id)));
  if(draft.cards.some(card=>!allowed.has(card.operator_id)))throw Error('请为每张卡明确选择实际发卡运营商，不会按原文字自动推断。');
  return {action:'card_link_batch',employee_id:draft.employee.id,expected_version:draft.employee.version,data:{cards:draft.cards.map(card=>({card_id:card.card_id,operator_id:card.operator_id,activated_on:card.activated_on})),source:draft.source}};
 }
 function validCounts(value,total){return !!value&&['total','new','existing','conflict'].every(key=>Number.isInteger(value[key])&&value[key]>=0)&&value.total===total&&value.new+value.existing+value.conflict===total;}
 function validatePreview(value){
  const ids=new Set(draft.cards.map(card=>card.card_id)),operators=new Map(draft.cards.map(card=>[card.card_id,card.operator_id]));
  return typeof value?.preview_id==='string'&&value.preview_id.trim()!==''&&value.employee_id===draft.employee.id&&value.action==='card_link_batch'&&Array.isArray(value.rows)&&value.rows.length===ids.size&&new Set(value.rows.map(row=>row.card_id)).size===ids.size&&value.rows.every(row=>ids.has(row.card_id)&&row.operator_id===operators.get(row.card_id)&&['new','existing','conflict'].includes(row.status)&&typeof row.phone==='string')&&validCounts(value.counts,ids.size)&&value.counts.new===value.rows.filter(row=>row.status==='new').length&&value.counts.existing===value.rows.filter(row=>row.status==='existing').length&&value.counts.conflict===value.rows.filter(row=>row.status==='conflict').length&&value.has_conflicts===(value.counts.conflict>0)&&value.can_apply===(value.counts.conflict===0);
 }
 function expiry(value){try{const seconds=Number(value);if(!Number.isSafeInteger(seconds)||seconds<=0)return '服务器指定时间（请及时确认）';return new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',dateStyle:'short',timeStyle:'medium',hourCycle:'h23'}).format(new Date(seconds*1000))+'（北京时间）';}catch{return '服务器指定时间（请及时确认）';}}
 function previewMarkup(value){
  return `<h3>确认实名关联 · ${esc(draft.employee.name)}</h3><p>新增 ${value.counts.new} 张 · 原有关联 ${value.counts.existing} 张 · 冲突 ${value.counts.conflict} 张</p><p class="employee-link-note">原有关联跳过，不重复建名额或改日期；其他员工的关联不会覆盖。以下号码以服务器本次核对为准。</p><p class="employee-link-note">本次登记存量实名关系，不自动完成在途申请；已有申请请在高级办卡管理逐项核对。</p><div class="employee-link-scroll"><table><thead><tr><th>手机号码</th><th>实际发卡运营商</th><th>处理结果</th><th>核对说明</th></tr></thead><tbody>${value.rows.map(row=>`<tr><td>${esc(row.phone)}</td><td>${esc(row.operator_name)||'待核对'}</td><td><span class="employee-link-result employee-link-${esc(row.status)}">${esc(row.status_label||({new:'新增关联',existing:'已有，跳过',conflict:'冲突，禁止保存'})[row.status])}</span></td><td>${esc(row.reason||'')}<small>${row.activated_on?'办理日期 '+esc(row.activated_on):'办理日期未知，保持未登记'}</small></td></tr>`).join('')}</tbody></table></div><p class="employee-link-note">预览有效至 ${esc(expiry(value.expires_at))}</p>${value.has_conflicts?'<p class="employee-link-conflict">本批存在冲突，不能确认。请先核对关系；不会部分保存。</p>':''}`;
 }
 async function preview(){
  if(!active()||draft.saving||draft.uncertain||draft.identityBlocked||!draft.meta||draft.readFailed)return false;
  if(String(board()?.user?.id)!==draft.actor){blockIdentity();return false;}
  let body;try{body=payload();}catch(error){message(error.message);return false;}
  const current=draft;draft.saving=true;draft.prepared=null;draft.requestId=null;message();lock();
  try{const result=await request('/api/employee-cards/preview',body);if(draft!==current)return false;if(!validatePreview(result))throw Error('预览结果不完整，尚未保存，请重试预览。');draft.prepared=result;$('employeeCardLinkPreview').innerHTML=previewMarkup(result);return true;}
  catch(error){message(error.status===409?'员工或卡片资料已变化，当前输入保留。请重新搜索并明确选择员工后再预览。':error.message);return false;}
  finally{if(draft===current){draft.saving=false;lock();}}
 }
 function validReceipt(result){return result?.ok===true&&typeof result.id==='string'&&result.employee_id===draft.employee.id&&result.action==='card_link_batch'&&result.version&&validCounts(result.counts,draft.cards.length)&&result.counts.conflict===0;}
 function uuid(){
  const secure=globalThis.crypto;if(typeof secure?.randomUUID==='function')return secure.randomUUID();
  if(typeof secure?.getRandomValues!=='function')throw Error('当前环境不能生成安全请求标识，请使用支持的浏览器。');
  const bytes=new Uint8Array(16);secure.getRandomValues(bytes);bytes[6]=bytes[6]&15|64;bytes[8]=bytes[8]&63|128;
  const hex=[...bytes].map(value=>value.toString(16).padStart(2,'0')).join('');return hex.slice(0,8)+'-'+hex.slice(8,12)+'-'+hex.slice(12,16)+'-'+hex.slice(16,20)+'-'+hex.slice(20);
 }
 async function apply(){
  if(!active()||draft.saving||draft.loading||draft.readFailed||draft.identityBlocked||!draft.prepared||draft.prepared.has_conflicts!==false||draft.prepared.can_apply!==true)return false;
  if(String(board()?.user?.id)!==draft.actor){blockIdentity();return false;}
  if(!draft.requestId){try{draft.requestId=uuid();}catch(error){message(error.message||'无法生成安全请求标识，请保留输入后重试。');return false;}}
  const current=draft,body={preview_id:draft.prepared.preview_id,request_id:draft.requestId,confirmation:'确认保存'};draft.saving=true;message();lock();
  try{
   const result=await request('/api/employee-cards/apply',body);if(draft!==current)return false;
   if(!validReceipt(result))throw Object.assign(Error('服务器返回的保存回执不完整，请核对原请求；不会另建新请求。'),{uncertain:true});
   draft.uncertain=false;draft.completed=true;
   for(const card of draft.cards)selected().delete(String(card.card_id));
   document.querySelectorAll('[data-batch-pick="cards"]').forEach(input=>{if(draft.cards.some(card=>String(card.card_id)===String(input.value)))input.checked=false;});
   if(typeof renderBatchControls==='function')renderBatchControls();
   $('employeeCardLinkReceipt').textContent=`实名关联已保存：新增 ${result.counts.new} 张，原有 ${result.counts.existing} 张（已跳过）。原手机卡资料未修改。`;
   $('employeeCardLinkReadState').textContent='保存完成，可到员工卡片台账查看';return true;
  }catch(error){
   if(error.uncertain||draft.uncertain){draft.uncertain=true;message('保存结果尚未确认。'+error.message+' 请使用“核对并重试原请求”；本批输入和请求标识保持不变。');}
   else{draft.uncertain=false;draft.prepared=null;draft.requestId=null;message(error.status===409?'资料或预览已变化，未能确认保存。当前输入保留；请重新搜索并明确选择员工，再预览。':error.message);}
   return false;
  }finally{if(draft===current){draft.saving=false;lock();}}
 }
 async function open(){
  if(active()){if(!isOpen())$('employeeCardLinkDialog').showModal();announceBlocked();return false;}
  if(!canManage())return false;
  const source=board(),actor=source?.user?.id,ids=[...selected()];
  if(actor===undefined||actor===null||!ids.length||ids.length>200){if(typeof toast==='function')toast('请先选择 1–200 张已授权手机卡。');return false;}
  const cards=ids.map(id=>(source?.phone_cards?.cards||[]).find(card=>String(card.id)===String(id)));
  if(cards.some(card=>!card||!Number.isSafeInteger(Number(card.id)))){if(typeof toast==='function')toast('选中卡片已变化，请重新读取并选择。');return false;}
  ensureDialog();draft={actor:String(actor),cards:cards.map(card=>({card_id:Number(card.id),phone:String(card.phone??''),carrier:String(card.carrier??''),operator_id:'',activated_on:''})),source:'',employee:null,meta:null,people:[],page:1,pageSize:50,total:0,prepared:null,requestId:null,saving:false,uncertain:false,identityBlocked:false,completed:false};
  for(const id of ['employeeCardLinkPreview','employeeCardLinkReceipt','employeeCardLinkEmployees','employeeCardLinkRows'])$(id).innerHTML='';$('employeeCardLinkSource').value='';$('employeeCardLinkQuery').value='';$('employeeCardLinkEmployee').textContent='尚未选择实名员工';message();$('employeeCardLinkDialog').showModal();lock();return loadEmployees();
 }
 function close(){if(draft?.saving||draft?.uncertain){announceBlocked();return false;}draft=null;serial++;$('employeeCardLinkDialog')?.close();return true;}
 document.addEventListener('click',event=>{
  if(event.target.closest('[data-employee-card-link-open]')){event.preventDefault();open();return;}
  if(active()&&!event.target.closest('#employeeCardLinkDialog')&&event.target.closest('a,[data-page],#refresh,#refreshCards,#today,[data-batch-clear],[data-batch-all],[data-batch-open]')){event.preventDefault();event.stopImmediatePropagation();announceBlocked();}
 },true);
 window.addEventListener('beforeunload',event=>{if(active()){event.preventDefault();event.returnValue='';}});
 return {open,close,isOpen,hasDraft:active,announceBlocked,loadEmployees,chooseEmployee,fillOperator,preview,apply};
})();
