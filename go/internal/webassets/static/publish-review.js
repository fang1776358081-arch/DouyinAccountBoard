/* Dedicated scoped read API; no /api/board, public cache or collection requests. */
(()=>{
 'use strict';
 const $=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const day=()=>new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
 const time=v=>{if(!v)return '尚未留存';const d=new Date(v);return Number.isNaN(d.getTime())?'时间待核验':new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(d);};
 const count=v=>v===null||v===undefined?'待核验':esc(v);
 const requestedOffice=new URLSearchParams(location.search).get('office');
 const state={date:day(),office:['all','pending',null].includes(requestedOffice)?'':requestedOffice,computer:'',filter:'',page:1,computerPage:1};
 // Same UUIDv4 fallback as workspace.js: production HTTP is not a secure context.
 function requestId(){if(typeof crypto.randomUUID==='function')return crypto.randomUUID();const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);bytes[6]=bytes[6]&15|64;bytes[8]=bytes[8]&63|128;const hex=Array.from(bytes,n=>n.toString(16).padStart(2,'0')).join('');return hex.slice(0,8)+'-'+hex.slice(8,12)+'-'+hex.slice(12,16)+'-'+hex.slice(16,20)+'-'+hex.slice(20);}
 let data=null,serial=0,abort=null,editing=null,pending=null;
 async function load(){
  const version=++serial;if(abort)abort.abort();abort=new AbortController();
  $('reviewLoading').hidden=false;$('reviewError').hidden=true;$('reviewContent').hidden=true;
  const query=new URLSearchParams({date:state.date,office:state.office,computer:state.computer,state:state.filter,page:state.page});
  try{
   const response=await fetch('/api/publish-review?'+query,{cache:'no-store',credentials:'same-origin',signal:abort.signal});
   const result=await response.json();if(version!==serial)return;
   if(response.status===401){$('reviewError').innerHTML='请先登录工作台。<a href="/login.html">前往登录</a>';$('reviewError').hidden=false;return;}
   if(!response.ok)throw Error(result.error||'读取统计失败，请稍后重试');
   data=result;window.WorkbenchNav?.update({user:result.user,office:state.office});state.page=result.page;render();$('reviewContent').hidden=false;
  }catch(error){if(version===serial&&error.name!=='AbortError'){void window.WorkbenchNav?.loadSession();$('reviewError').textContent='读取失败：'+error.message+'。未把失败当作无匹配资料；请点击刷新统计重试。';$('reviewError').hidden=false;}}
  finally{if(version===serial)$('reviewLoading').hidden=true;}
 }
 function computerCards(){
  const total=data.computers.length,pages=Math.max(1,Math.ceil(total/24));state.computerPage=Math.max(1,Math.min(state.computerPage,pages));
  $('reviewComputers').innerHTML=data.computers.slice((state.computerPage-1)*24,state.computerPage*24).map(c=>`<button type="button" class="review-computer" data-review-computer="${esc(c.computer_id||'unassigned')}" aria-pressed="${state.computer===(c.computer_id||'unassigned')}"><strong>${esc(c.computer_name)}</strong><small>负责人：${esc(c.operator||'待补充')} · 当日已安排 ${c.scheduled} 个账号</small><div class="review-computer-counts"><span class="review-good">达标 ${c.met}</span><span class="review-pending">待完成 ${c.pending}</span><span class="review-check">待核验 ${c.review}</span></div><small>核对资料 ${c.accounts} 个（含暂停、无任务及计划待确认）</small></button>`).join('')||'<p class="review-muted">没有可展示的当日电脑归属。历史日期不会使用今天的电脑补算。</p>';
  $('reviewComputerPage').textContent=`第 ${state.computerPage} / ${pages} 组 · ${total} 台电脑`;$('reviewComputersPrevious').disabled=state.computerPage===1;$('reviewComputersNext').disabled=state.computerPage===pages;
 }
 function render(){
  $('reviewUser').textContent=(data.user.name||'当前账号')+' · 仅统计当前授权资料';
  const office=$('reviewOffice'),chosenOffice=data.offices.find(o=>o.id===state.office),oldOffice=!!state.office&&!chosenOffice;
  office.innerHTML='<option value="">全部授权办公室</option>'+data.offices.map(o=>`<option value="${esc(o.id)}">${esc(o.name)}</option>`).join('')+(oldOffice?`<option value="${esc(state.office)}">原办公室（当前选项已更新）</option>`:'');office.value=state.office;
  $('reviewScopeText').textContent=oldOffice?'原办公室条件仍生效，请重新选择当前办公室':'汇总范围：'+(chosenOffice?.name||'全部授权办公室');
  $('reviewStats').innerHTML=[['met','净增达标'],['pending','待完成'],['review','数据待核验']].map(([key,label])=>`<div class="review-stat"><span>${label}</span><strong>${data.summary[key]}</strong></div>`).join('');
  $('reviewExcluded').textContent=`另有暂停 ${data.summary.paused} 个、明确无任务 ${data.summary.no_task} 个，不计入待完成。`+(data.historical?'历史仅展示留有当日计划的账号；缺失历史计划不使用今天资料补造。':'');
  $('reviewFreshness').textContent=data.freshness;computerCards();
  const computer=$('reviewComputer');
  const selected=data.computers.find(c=>(c.computer_id||'unassigned')===state.computer);
  computer.innerHTML='<option value="">全部电脑</option>'+data.computers.map(c=>`<option value="${esc(c.computer_id||'unassigned')}">${esc(c.computer_name)}</option>`).join('');
  if(state.computer&&!selected)computer.add(new Option('原电脑（当前范围已无记录）',state.computer));
  computer.value=state.computer;
  $('reviewActiveFilters').textContent=state.computer&&!selected?'当前范围没有这台电脑的记录。可改选全部电脑。':'';
  $('reviewCount').textContent=`匹配 ${data.total} 个账号 · 本页 ${data.rows.length} 个`;
  document.querySelectorAll('[data-review-state]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.reviewState===state.filter)));
  $('reviewRows').innerHTML=data.rows.map((r,index)=>{
   const excluded=['paused','no_task'].includes(r.state);
   const net=r.net===0?'暂无净增':r.net===null?'待核验':(r.net>0?'+':'')+r.net;
   const progress=excluded?'<small>本日无需执行</small>':r.progress===null?'<small>数据未齐，暂不绘制进度</small>':`<div class="review-progress" role="progressbar" aria-label="${esc(r.name)}任务进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(r.progress)}"><span style="width:${r.progress}%"></span></div>`;
   return `<tr><td><strong>${esc(r.name||'名称待补充')}</strong><small>抖音号 ${esc(r.douyin_id||'待补充')}</small></td><td>${esc(r.computer_name)}<small>负责人 ${esc(r.operator||'待补充')}</small>${!r.task_state?'<small>登记电脑参考，任务归属未确认</small>':''}</td><td><strong>${esc(net)} / ${count(r.target)}</strong></td><td>${progress}<small>${excluded?'不计入待完成':r.remaining===null?'差额待核验':r.remaining===0?'已达到目标':'还差 '+r.remaining+' 条净增'}</small></td><td><strong class="${r.state==='met'?'review-good':r.state==='pending'?'review-pending':excluded?'review-muted':'review-check'}">${esc(r.label)}</strong></td><td><div class="review-row-actions"><button type="button" class="text-button" data-review-detail="reviewDetail${index}" aria-controls="reviewDetail${index}" aria-expanded="false">查看依据</button>${data.can_manage&&!data.historical?`<button type="button" class="text-button" data-review-target="${esc(r.account_id)}">设置任务</button>`:''}</div></td></tr>
   <tr class="review-details" id="reviewDetail${index}" hidden><td colspan="6"><div class="review-detail-panel"><p><strong>${esc(r.name||'名称待补充')}</strong> · 系统 ID ${esc(r.account_id)}</p>${r.reasons.length?`<p>${esc(r.reasons.join('；'))}</p>`:''}<div class="review-detail-grid"><p>昨日收口数量：${count(r.baseline_total)}<br>实际采样：${esc(time(r.baseline_at))}<br>基线封存：${esc(time(r.sealed_at))}</p><p>最近成功数量：${count(r.current_total)}<br>实际采样：${esc(time(r.current_at))}<br>最近尝试：${esc(time(r.attempt_at))} ${r.attempt_result==='failed'?'（失败）':''}</p><p>当日目标：${count(r.target)}<br>截止时间：${r.deadline?esc(time(r.deadline)):'未设置'}<br>目标依据：${esc(r.target_reason||'未保存当日计划')}<br>累计数量变化可能来自删除或隐藏等，并非准确发布条数。</p></div>${r.legacy_total!==null&&r.current_at===null?`<p>旧版最新值 ${count(r.legacy_total)}，时间 ${esc(time(r.legacy_at))}；没有采样历史，不拿它伪造昨日基线。</p>`:''}</div></td></tr>`;
  }).join('')||`<tr><td colspan="6" class="review-empty">没有符合当前条件的账号。${data.historical?'若未保存当日计划，无法还原历史任务与归属。':'可以调整电脑、状态或办公室。'}<br><button type="button" class="button" data-review-reset>重置筛选（保留日期、办公室）</button></td></tr>`;
  $('reviewPage').textContent=`第 ${data.page} / ${data.pages} 页 · 每页 ${data.page_size} 个`;$('reviewPrevious').disabled=data.page===1;$('reviewNext').disabled=data.page===data.pages;
 }
 function reset(){state.computer='';state.filter='';state.page=1;load();}
 function openTask(id){
  editing=data.rows.find(r=>r.account_id===id);if(!editing||data.historical||!data.can_manage)return;
  pending=null;$('reviewTaskForm').querySelectorAll('input,select,textarea').forEach(n=>n.disabled=false);
  $('reviewTaskAccount').textContent=editing.name+' · '+state.date+' · 系统ID '+id;
  $('reviewTaskTarget').value=editing.target??'';$('reviewTaskState').value=editing.task_state||'active';$('reviewTaskDeadline').value=editing.deadline?editing.deadline.slice(11,16):'';$('reviewTaskReason').value='';$('reviewTaskError').textContent='';
  $('reviewTaskComputer').innerHTML='<option value="">未确认任务电脑</option>'+data.computers.filter(c=>c.computer_id).map(c=>`<option value="${esc(c.computer_id)}">${esc(c.computer_name)} · ${esc(c.operator||'负责人待补充')}</option>`).join('');$('reviewTaskComputer').value=editing.computer_id;$('reviewTaskSave').disabled=false;$('reviewTaskSave').textContent='保存当日任务';$('reviewTaskDialog').showModal();
 }
 async function save(event){
  event.preventDefault();const button=$('reviewTaskSave');button.disabled=true;
  if(!pending)pending={account_id:editing.account_id,day:state.date,target:$('reviewTaskTarget').value===''?null:Number($('reviewTaskTarget').value),task_state:$('reviewTaskState').value,computer_id:$('reviewTaskComputer').value,deadline:$('reviewTaskDeadline').value,reason:$('reviewTaskReason').value,expected_revision:editing.revision,request_id:requestId()};
  $('reviewTaskError').textContent='';$('reviewTaskForm').querySelectorAll('input,select,textarea').forEach(n=>n.disabled=true);
  try{
   const response=await fetch('/api/publish-review/task',{method:'POST',cache:'no-store',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':data.user.csrf},body:JSON.stringify(pending)});
   const result=await response.json();if(!response.ok){if(response.status<500){pending=null;$('reviewTaskForm').querySelectorAll('input,select,textarea').forEach(n=>n.disabled=false);}throw Error(result.error||'保存失败');}
   pending=null;$('reviewTaskDialog').close();await load();
  }catch(error){$('reviewTaskError').textContent=pending?'结果待确认：'+error.message+'。重试将使用原请求标识，不重复创建任务。':error.message;button.textContent=pending?'核对 / 重试原请求':'保存当日任务';}
  finally{button.disabled=false;}
 }
 function bind(){
  $('reviewDate').value=state.date;$('reviewDate').max=day();$('reviewDate').onchange=()=>{if(!$('reviewDate').value)return;state.date=$('reviewDate').value;state.page=1;load();};
  $('reviewOffice').onchange=()=>{state.office=$('reviewOffice').value;state.computer='';state.page=1;state.computerPage=1;load();};
  $('reviewComputer').onchange=()=>{state.computer=$('reviewComputer').value;state.page=1;load();};
  $('reviewRows').onclick=event=>{
   const button=event.target.closest('[data-review-detail]');
   if(!button)return;
   const details=$(button.dataset.reviewDetail);
   details.hidden=!details.hidden;
   button.setAttribute('aria-expanded',String(!details.hidden));
   button.textContent=details.hidden?'查看依据':'收起依据';
  };
  $('reviewRefresh').onclick=load;$('reviewReset').onclick=reset;$('reviewAllComputers').onclick=()=>{state.computer='';state.page=1;load();};
  $('reviewPrevious').onclick=()=>{state.page--;load();};$('reviewNext').onclick=()=>{state.page++;load();};
  $('reviewComputersPrevious').onclick=()=>{state.computerPage--;computerCards();};$('reviewComputersNext').onclick=()=>{state.computerPage++;computerCards();};
  document.addEventListener('click',event=>{const computer=event.target.closest('[data-review-computer]'),filter=event.target.closest('[data-review-state]'),target=event.target.closest('[data-review-target]');if(computer){state.computer=computer.dataset.reviewComputer;state.page=1;load();}else if(filter){state.filter=filter.dataset.reviewState;state.page=1;load();}else if(target)openTask(target.dataset.reviewTarget);else if(event.target.closest('[data-review-reset]'))reset();});
  $('reviewTaskForm').onsubmit=save;$('reviewTaskClose').onclick=()=>{if(pending){$('reviewTaskError').textContent='仍有结果待确认，请先核对 / 重试原请求。';return;}$('reviewTaskDialog').close();};$('reviewTaskDialog').addEventListener('cancel',event=>{if(pending)event.preventDefault();});load();
 }
 document.addEventListener('DOMContentLoaded',bind);
 // Pure presentation helpers are exposed only for the repository's VM checks.
 window.PublishReviewUI={time,count,esc};
})();
