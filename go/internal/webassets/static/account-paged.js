// Office 3 account list. The current page and its full matching ID set
// are applied together, only while the request still owns the view.
if(typeof window!=='undefined')window.AccountPaged=(()=>{
 const filters={query:'query',accountDouyinQuery:'douyin_id',accountIdQuery:'account_id',computer:'computer_id',status:'status',published:'published'};
 const advanced=['accountDeviceQuery','accountPhoneQuery','verificationFilter','loginPhoneFilter','accountType','ageFilter','lifecycleFilter','accountResponsible','accountUsage','accountViolation','accountBusinessType'];
 let result=null,applied='',controller=null,generation=0,timer=null,busy=false;
 const field=id=>document.getElementById(id);
 const value=id=>String(field(id)?.value||'').trim();
 function intent(){
  if(page!=='accounts'||activeOffice!=='three')return false;
  const params=new URLSearchParams(window.location?.search);
  return !['todo_action','account_id','card_id','device_id','match_computer'].some(key=>params.has(key));
 }
 const eligible=()=>intent()&&!advanced.some(value)&&!activeLifecyclePreset;
 function query(pageNumber){
  const params=new URLSearchParams({office_id:'three',page:String(pageNumber)});
  if(value('date'))params.set('date',value('date'));
  for(const [id,key] of Object.entries(filters)){
   let filter=value(id);
   if(id==='accountIdQuery')filter=filter.replace(/^#/,'');
   if(filter)params.set(key,filter);
  }
  return params.toString();
 }
 function draftOpen(){
  return !!(window.AccountInline?.hasDraft()||window.DeviceInline?.hasDraft()||window.CardInline?.hasDraft()||window.EmployeeCardLink?.hasDraft());
 }
 function writesPending(){
  return (typeof boardWrites!=='undefined'&&boardWrites>0)||(typeof dailyRequests!=='undefined'&&[...dailyRequests.values()].some(entry=>entry.running));
 }
 function mark(loading){
  busy=loading;
  document.body?.classList.toggle('account-page-loading',loading);
  const status=field('accountActiveFilters');
  if(status)status.setAttribute('aria-busy',String(loading));
  if(loading&&field('loadError'))field('loadError').hidden=true;
 }
 function invalidate(){
  clearTimeout(timer);
  timer=null;
  controller?.abort();
  controller=null;
  return ++generation;
 }
 function scopeKey(user){
  return JSON.stringify([user?.id,user?.authorization_version,user?.scope_mode,[...(user?.office_ids||[])].sort(),[...(user?.permissions||[])].sort()]);
 }
 function fail(error){
  if(error.status===401||error.status===403){
   result=null;
   applied='';
   if(typeof clearPagedAccountView==='function')clearPagedAccountView();
  }
  const node=field('loadError');
  if(node){node.textContent='账号清单加载失败：'+(error.message||'请稍后重试');node.hidden=false;}
 }
 function canAct(){
  if(busy||!result&&intent()&&typeof data!=='undefined'&&!data)return false;
  if(!result)return true;
  const current=new URLSearchParams(query(result.page));
  current.sort();
  return eligible()&&applied===current.toString();
 }
 async function load(pageNumber=result?.page||1,options={}){
  if(!eligible()||draftOpen()||writesPending()||options.signal?.aborted)return false;
  if(options.background&&(busy||document.querySelector?.('dialog[open]')?.open))return false;
  const token=invalidate(),requested=query(pageNumber);
  controller=new AbortController();
  const abort=controller;
  const cancel=()=>abort.abort();
  options.signal?.addEventListener('abort',cancel,{once:true});
  mark(true);
  try{
   const next=await api('/api/accounts?'+requested,undefined,{applyVersions:false,signal:abort.signal});
   if(token!==generation||abort.signal.aborted||!eligible()||query(pageNumber)!==requested||draftOpen()||writesPending()||options.isCurrent&&!options.isCurrent()||document.querySelector?.('dialog[open]')?.open)return false;
   if(next?.workspace_view!=='accounts_page'||next.office_id!=='three'||!next.user||!Array.isArray(next.accounts)||!Array.isArray(next.matching_ids))throw Error('账号分页响应不完整');
   const former=result?.user||(typeof data==='undefined'?null:data?.user);
   if(former&&scopeKey(former)!==scopeKey(next.user)){
    if(typeof selected!=='undefined')selected.clear();
    window.AccountInline?.clearSuspended?.();
   }
   const accepted=new URLSearchParams(requested);
   accepted.set('page',String(next.page));
   accepted.set('date',next.date);
   accepted.sort();
   result=next;
   applied=accepted.toString();
   accountPageIndex=next.page;
   mark(false);
   showBoard(next);
   return true;
  }catch(error){
   if(error.name!=='AbortError'&&token===generation)fail(error);
   return false;
  }finally{
   options.signal?.removeEventListener('abort',cancel);
   if(token===generation){controller=null;mark(false);}
  }
 }
 function schedule(pageNumber=1){
  invalidate();
  if(draftOpen()||writesPending()){mark(false);window.AccountInline?.announceBlocked?.();return;}
  mark(true);
  timer=setTimeout(()=>{
   timer=null;
   if(draftOpen()||writesPending()){mark(false);return;}
   if(eligible())void load(pageNumber);
   else if(intent()&&result)void ensureLegacy();
   else{mark(false);if(intent())void window.load();}
  },120);
 }
 function prepare(rows){
  if(!result)return rows;
  let controls=field('accountPagination');
  if(!controls){
   controls=document.createElement('div');
   controls.id='accountPagination';
   controls.className='card-pagination';
   field('accountsView')?.append(controls);
  }
  const {total,page:pageNumber,page_size:size,pages}=result;
  const start=total?(pageNumber-1)*size+1:0,end=Math.min(pageNumber*size,total);
  controls.innerHTML=`<button id="accountsPrevious" type="button" class="button">上一页账号</button><span>第 ${pageNumber} / ${pages} 页 · 共 ${total} 个账号 · 本页 ${start}–${end} 条</span><button id="accountsNext" type="button" class="button">下一页账号</button>`;
  field('accountsPrevious').disabled=pageNumber<=1;
  field('accountsNext').disabled=pageNumber>=pages;
  field('accountsPrevious').onclick=()=>{if(canAct())void load(pageNumber-1);};
  field('accountsNext').onclick=()=>{if(canAct())void load(pageNumber+1);};
  return rows;
 }
 async function ensureLegacy(){
  if(!result)return true;
  if(draftOpen()||writesPending())return false;
  const token=invalidate(),targetPage=page,targetOffice=activeOffice,day=value('date');
  const compact=targetPage==='accounts'?'&compact=accounts':'';
  controller=new AbortController();
  const signal=controller.signal;
  mark(true);
  try{
   const next=await api('/api/board?version_scope=day'+compact+(day?'&date='+encodeURIComponent(day):''),undefined,{applyVersions:false,signal});
   if(token!==generation||signal.aborted||targetPage!==page||targetOffice!==activeOffice||draftOpen()||writesPending()||day!==value('date'))return false;
   if(typeof selected!=='undefined'&&scopeKey(result.user)!==scopeKey(next.user))selected.clear();
   result=null;
   applied='';
   mark(false);
   showBoard(next);
   return true;
  }catch(error){
   if(error.name!=='AbortError'&&token===generation)fail(error);
   return false;
  }finally{
   if(token===generation){controller=null;mark(false);}
  }
 }
 const matchingIds=()=>result?result.matching_ids.map(String):[];
 function cancelPending(){invalidate();mark(false);}
 function restorePage(pageNumber){
  if(result&&(result.page!==pageNumber||!canAct()))schedule(pageNumber);
 }
 return Object.freeze({intent,eligible,active:()=>!!result,canAct,cancelPending,scopeKey,load,schedule,prepare,ensureLegacy,matchingIds,restorePage,total:()=>result?.total??null,publishedTotal:()=>result?.published_total??null,computerTotal:()=>result?.computer_total??null});
})();
