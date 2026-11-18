/* Account-sheet presentation. Saving, scope, selection and realtime stay in
   the existing modules. No account data is persisted by this helper. */
window.AccountSheet=(()=>{
 const columns=[['select','选择',true],['name','账号昵称',true],['douyin','完整抖音号',true],['status','状态'],['phone','登录手机 / 注册卡'],['position','发布位置'],['published','当天发布'],['notes','备注']];
 const hidden=new Set(['published']);
 const localPreview=['localhost','127.0.0.1','[::1]'].includes(location.hostname);
 let density='comfortable',filtersOpen=true,toolbar=null,actionsHome=null,active=false;
 const node=id=>document.getElementById(id);
 const icon=(paths)=>'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+paths+'</svg>';
 const gridIcon=icon('<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 4v16"/>');
 const filterIcon=icon('<path d="M4 6h16M7 12h10M10 18h4"/>');
 const columnsIcon=icon('<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16M15 4v16"/>');
 const hasDraft=()=>!!window.AccountInline?.hasDraft();
 function block(event){
  if(!hasDraft())return false;
  event?.preventDefault();event?.stopImmediatePropagation();
  window.AccountInline.announceBlocked();return true;
 }
 function mount(){
  if(toolbar)return;
  const workspace=node('accountWorkspace');if(!workspace)return;
  toolbar=document.createElement('div');toolbar.className='account-sheet-toolbar toolbar';
  toolbar.setAttribute('role','toolbar');toolbar.setAttribute('aria-label','账号表格工具');
  toolbar.innerHTML='<div class="account-sheet-view">'+gridIcon+'<span>表格视图</span><span id="accountSheetCount" class="account-sheet-count"></span></div><div class="account-sheet-tools"><button id="accountSheetFilterToggle" type="button" aria-controls="accountSheetFilters" aria-expanded="true">'+filterIcon+'<span>筛选</span></button><details class="account-sheet-columns" id="accountSheetColumns"><summary>'+columnsIcon+'显示字段</summary><fieldset><legend>调整当前表格</legend>'+columns.map(([key,label,required])=>'<label><input type="checkbox" data-sheet-column="'+key+'" '+(!hidden.has(key)?'checked ':'')+(required?'disabled ':'')+'>'+label+'</label>').join('')+'</fieldset></details><div class="account-sheet-density" role="group" aria-label="表格密度"><button type="button" data-sheet-density="comfortable" aria-pressed="true">舒适</button><button type="button" data-sheet-density="compact" aria-pressed="false">紧凑</button></div></div>';
  workspace.prepend(toolbar);
  const filters=workspace.querySelector('.account-filter-toolbar');if(filters)filters.id='accountSheetFilters';
  const actions=node('accountListActions');if(actions){actionsHome=document.createComment('account-list-actions-home');actions.before(actionsHome);}
  const eyebrow=document.querySelector('main > header .eyebrow');
  if(eyebrow&&localPreview){const label=document.createElement('span');label.id='accountSheetSample';label.className='account-sheet-sample-label';label.textContent='本机试用';eyebrow.append(label);}
  // Prevent focus moving out of an unfinished editor before a display control
  // can hide its column. The original AccountInline capture guard also remains.
  toolbar.addEventListener('pointerdown',event=>block(event),true);
  toolbar.addEventListener('keydown',event=>{if(['Enter',' ','Spacebar'].includes(event.key))block(event);},true);
  toolbar.addEventListener('click',event=>{
   if(block(event))return;
   const nextDensity=event.target.closest('button[data-sheet-density]');
   if(nextDensity){density=nextDensity.dataset.sheetDensity;applyPresentation();return;}
   if(event.target.closest('#accountSheetFilterToggle')){filtersOpen=!filtersOpen;applyPresentation();}
  });
  toolbar.addEventListener('change',event=>{
   const input=event.target.closest('[data-sheet-column]');if(!input)return;
   if(block(event)){input.checked=!hidden.has(input.dataset.sheetColumn);return;}
   if(input.checked)hidden.delete(input.dataset.sheetColumn);else hidden.add(input.dataset.sheetColumn);
   applyPresentation();
  });
 }
 function applyPresentation(){
  if(!active)return;
  document.body.dataset.sheetDensity=density;
  for(const button of toolbar.querySelectorAll('[data-sheet-density]'))button.setAttribute('aria-pressed',String(button.dataset.sheetDensity===density));
  const filters=node('accountSheetFilters');if(filters)filters.hidden=!filtersOpen;
  const filterButton=node('accountSheetFilterToggle');if(filterButton)filterButton.setAttribute('aria-expanded',String(filtersOpen));
  const table=document.querySelector('#accountsView .account-table');if(!table)return;
  for(const cell of table.querySelectorAll('[data-column]'))cell.hidden=hidden.has(cell.dataset.column);
  const empty=table.querySelector('.empty-result');if(empty)empty.colSpan=columns.length-hidden.size;
 }
 function afterRender(){
  active=typeof page!=='undefined'&&page==='accounts';
  document.body.classList.toggle('accounts-sheet',active);
  if(!active){
   if(toolbar)toolbar.hidden=true;
   if(node('accountSheetSample'))node('accountSheetSample').hidden=true;
   if(node('accountSheetFilters'))node('accountSheetFilters').hidden=false;
   const actions=node('accountListActions');if(actions&&actionsHome?.parentNode)actionsHome.after(actions);
   return;
  }
  mount();if(!toolbar)return;toolbar.hidden=false;
  if(node('accountSheetSample'))node('accountSheetSample').hidden=false;
  const actions=node('accountListActions'),tools=toolbar.querySelector('.account-sheet-tools');
  if(actions&&actions.parentElement!==tools)tools.append(actions);
  const count=node('accountActiveFilters')?.querySelector('.account-filter-count strong')?.textContent;
  if(node('accountSheetCount'))node('accountSheetCount').textContent=count?'· '+count+' 条':'';
  const filtersCount=node('accountActiveFilters')?.querySelectorAll('[data-clear-account-filter]').length||0;
  if(node('accountActiveFilters'))node('accountActiveFilters').dataset.hasFilters=String(filtersCount>0);
  const filterText=node('accountSheetFilterToggle')?.querySelector('span');if(filterText)filterText.textContent=filtersCount?'筛选 · '+filtersCount:'筛选';
  applyPresentation();
 }
 return Object.freeze({afterRender});
})();
