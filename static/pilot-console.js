/* Office-three task navigation. Only UI state; never stores business records. */
window.PilotConsole=(()=>{
 let originalNav=null,navOwner='',statusFlight=null,statusActor='',pilotStatus=null;
 const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const params=()=>new URLSearchParams(location.search);
 function context(){
  const board=typeof data==='undefined'?null:data,user=board?.user;
  const workspace=!!document.getElementById('workspaceBody');
  let office=workspace?(['todoOffice','accountListOffice','positionListOffice','nurtureOffice','reviewOffice'].map(id=>document.getElementById(id)?.value).find(Boolean)||params().get('office')):(typeof activeOffice==='undefined'?params().get('office'):activeOffice);
  const authorized=user&&(user.scope_mode==='all'||(user.office_ids||[]).includes('three')||(board.offices||[]).some(item=>item.id==='three'));
  return {board,user,workspace,office,enabled:office==='three'&&!!authorized,view:workspace?(typeof tab==='undefined'?'todo':tab):(typeof page==='undefined'?'board':page)};
 }
 const href=(path,query='')=>path+(query?'?'+query+'&office=three':'?office=three');
 function organizeWork(c){
  const body=document.getElementById('workspaceBody');if(!body||!c.workspace||c.view!=='todo')return;
  if(!document.getElementById('pilotWorkFlow')){const flow=document.createElement('nav');flow.id='pilotWorkFlow';flow.className='pilot-workflow';flow.setAttribute('aria-label','今日发布处理流程');flow.innerHTML='<a href="/workspace.html?tab=todo&office=three" aria-current="step">1 待处理</a><a href="/?page=board&office=three">2 执行发布</a><a href="/publish-review.html?office=three">3 结果核对</a><a href="/workspace.html?tab=review&office=three">4 异常与资料核对</a>';body.prepend(flow);}
  const counts=body.querySelector('.todo-counts');if(!counts)return;
  const allTasks=typeof workspaceTodos==='function'?workspaceTodos(c.board):[],tasks=allTasks.filter(task=>task.office==='three'||task.offices?.includes('three'));
  let extra=document.getElementById('pilotOtherTasks');
  if(!extra){extra=document.createElement('details');extra.id='pilotOtherTasks';extra.className='pilot-other-tasks';extra.innerHTML='<summary>其他日常事项</summary><div class="todo-counts"></div>';counts.after(extra);}
  const primary=new Set(['','publish','abnormal','account_recovery']);
  for(const button of [...body.querySelectorAll('.todo-counts [data-kind]')]){
   const kind=button.dataset.kind,strong=button.querySelector('strong');if(strong)strong.textContent=kind?tasks.filter(task=>task.kind===kind).length:tasks.length;
   (primary.has(kind)?counts:extra.querySelector('.todo-counts')).append(button);
  }
 }
 function restoreWork(){
  document.getElementById('pilotWorkFlow')?.remove();const extra=document.getElementById('pilotOtherTasks'),body=document.getElementById('workspaceBody');
  const counts=body?.querySelector('.todo-counts');if(extra&&counts){counts.append(...extra.querySelectorAll('[data-kind]'));extra.remove();}
  if(body&&typeof workspaceTodos==='function'&&typeof data!=='undefined'&&data){const tasks=workspaceTodos(data);for(const button of body.querySelectorAll('.todo-counts [data-kind]')){const strong=button.querySelector('strong'),kind=button.dataset.kind;if(strong)strong.textContent=kind?tasks.filter(task=>task.kind===kind).length:tasks.length;}}
 }
 function navigation(c){
  const active=key=>c.view===key?' active':'';
  const today=['todo','nurture','positions','board'].includes(c.view);
  const syncAllowed=c.user.role==='owner'||pilotStatus?.pilot?.state==='enabled';
  const permissions=c.user.permissions||[],can=key=>permissions.includes(key);
  return `<a class="nav-item${today?' active':''}" href="${href('/workspace.html','tab=todo')}">今日工作</a><a class="nav-item${active('accounts')}" href="${href('/','page=accounts')}">账号中心</a>${syncAllowed?'<a class="nav-item" href="/table-sync.html?office_id=three">表格同步 <small>试点</small></a>':''}<details class="nav-group" ${['cards','phones'].includes(c.view)?'open':''}><summary><span>资料与设备</span><small>手机卡、设备与养号</small></summary><div class="nav-submenu"><a class="nav-item${active('cards')}" href="${href('/','page=cards')}">手机卡管理</a><a class="nav-item${active('phones')}" href="${href('/','page=phones')}">手机设备</a><a class="nav-item" href="/employee-cards.html">员工卡片台账</a><a class="nav-item${active('nurture')}" href="${href('/workspace.html','tab=nurture')}">7日养号</a></div></details><details class="nav-group" ${['history','review','exchange','backups'].includes(c.view)?'open':''}><summary><span>管理工具</span><small>核对、记录与系统管理</small></summary><div class="nav-submenu"><a class="nav-item${active('review')}" href="${href('/workspace.html','tab=review')}">资料待核对</a>${can('history_view')?`<a class="nav-item${active('history')}" href="${href('/workspace.html','tab=history')}">操作历史</a>`:''}${can('data_export')||can('data_import')?`<a class="nav-item${active('exchange')}" href="${href('/workspace.html','tab=exchange')}">其他导入 / 导出</a>`:''}${c.user.role==='owner'?`<a class="nav-item${active('backups')}" href="${href('/workspace.html','tab=backups')}">备份与恢复</a><a class="nav-item" href="/users.html">子账号与权限</a>`:''}</div></details>`;
 }
 function refresh(){
  const c=context(),nav=document.querySelector('.side-nav');if(!nav||!c.user)return;
  if(!originalNav)originalNav=[...nav.childNodes];
  document.body.classList.toggle('office-pilot',c.enabled);
  if(!c.enabled){if(navOwner){nav.replaceChildren(...originalNav);navOwner='';restoreWork();}document.getElementById('pilotOfficeContext')?.remove();return;}
  const key=[c.user.id,c.user.role,c.view,JSON.stringify(c.user.permissions),pilotStatus?.pilot?.state].join(':');
  if(navOwner!==key){nav.innerHTML=navigation(c);const legacy=document.createElement('div');legacy.hidden=true;legacy.inert=true;legacy.setAttribute('aria-hidden','true');legacy.append(...originalNav);nav.append(legacy);navOwner=key;}
  const header=document.querySelector('main > header');
  if(header&&!document.getElementById('pilotOfficeContext')){const badge=document.createElement('div');badge.id='pilotOfficeContext';badge.className='pilot-office-context';header.append(badge);}
  const badge=document.getElementById('pilotOfficeContext');if(badge)badge.textContent=c.workspace&&['history','exchange','backups'].includes(c.view)?'管理工具 · 全部授权资料':'办公室三 · 主路径试点';
  if(c.workspace){const date=document.getElementById('workDate');if(date)date.hidden=!['todo','positions'].includes(c.view);const bar=document.querySelector('.collection-bar');if(bar)bar.hidden=c.view!=='todo';organizeWork(c);}
  else{const date=document.getElementById('date');if(date?.closest('label'))date.closest('label').hidden=c.view!=='board';const today=document.getElementById('today');if(today)today.hidden=c.view!=='board';const bar=document.getElementById('collectionBar');if(bar){bar.hidden=!['board','accounts'].includes(c.view);const actions=bar.querySelector('.collection-actions');if(actions)actions.hidden=false;}}
  if(statusActor!==String(c.user.id)){
   statusActor=String(c.user.id);pilotStatus=null;const actor=statusActor;
   statusFlight=fetch('/api/table-sync?office_id=three',{cache:'no-store'}).then(async response=>response.ok?response.json():null).then(result=>{if(statusActor===actor){pilotStatus=result;navOwner='';refresh();}}).catch(()=>{}).finally(()=>{statusFlight=null;});
  }
 }
 function rememberOfficeLinks(event){
  const link=event.target.closest?.('a[href]'),c=context();if(!c.enabled||!link||link.origin!==location.origin)return;
  const url=new URL(link.href);if(['/', '/workspace.html'].includes(url.pathname)){url.searchParams.set('office','three');link.href=url.pathname+url.search;}
 }
 document.addEventListener('board-ready',refresh);
 document.addEventListener('workspace-ready',refresh);
 document.addEventListener('DOMContentLoaded',refresh,{once:true});
 if(document.readyState!=='loading')refresh();
 document.addEventListener('click',rememberOfficeLinks,true);
 document.addEventListener('change',()=>{if(typeof requestAnimationFrame==='function')requestAnimationFrame(refresh);});
 return Object.freeze({refresh,context});
})();
