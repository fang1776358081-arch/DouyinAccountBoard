/* Card-page presentation state. Never changes data, permissions, requests or filters. */
window.CardPageUI=(()=>{
 let awaitingCards=false;
 function activate(active){
  document.body.classList.toggle('cards-console',active);
  const actions=document.getElementById('cardsHeaderActions');if(actions)actions.hidden=!active;
 }
 function state(phase,hasData=false){
  const active=typeof page!=='undefined'&&page==='cards';
  const cleanup=()=>{
   const get=id=>document.getElementById(id),host=get('cardReadState'),workspace=get('cardsWorkspace');
   if(host)host.hidden=true;if(get('cardReadRetry'))get('cardReadRetry').hidden=true;if(workspace)workspace.setAttribute('aria-busy','false');
   let remains=false;
   for(const element of workspace?.querySelectorAll('.card-filter-toolbar input,.card-filter-toolbar select,.card-filter-toolbar button,.card-selection-entry button')||[]){
    if(!element.dataset.cardAwaiting)continue;
    if(hasData||element.id==='refreshCards'){element.disabled=false;delete element.dataset.cardAwaiting;}else remains=true;
   }
   awaitingCards=remains;
  };
  if(!active){if(awaitingCards&&phase!=='loading')cleanup();return;}
  if(phase==='ready'){cleanup();activate(true);return;}
  awaitingCards=true;
  activate(true);
  const get=id=>document.getElementById(id),host=get('cardReadState');if(!host)return;
  const loading=phase==='loading',error=phase==='error';host.hidden=!loading&&!error;
  if(get('cardFilterLoadHint'))get('cardFilterLoadHint').hidden=true;
  host.classList.toggle('is-error',error);host.setAttribute('role',error?'alert':'status');
  get('cardReadMessage').textContent=loading?(hasData?'正在刷新资料，当前清单仍可查看。':'正在读取手机卡资料，请稍候…'):(hasData?'刷新失败，保留上次资料。可重新加载，或稍后重试。':'手机卡资料加载失败，请重新加载。当前不能判定是否有记录。');
  get('cardReadRetry').hidden=!error;
  get('cardsWorkspace').setAttribute('aria-busy',String(loading));
  if(!hasData){
   for(const id of ['accountWorkspace','phonesWorkspace','collectionBar','officeWorkspace','lifecycleSummary'])if(get(id))get(id).hidden=true;
   get('cardsWorkspace').hidden=false;get('stats').innerHTML='';
   get('title').textContent='手机卡管理';get('breadcrumb').textContent='手机卡资料';get('subtitle').textContent='查看号码、卡主、当前插卡位置和关联账号。';
   document.querySelector('.header-actions').hidden=true;
   get('newCard').hidden=true;get('cardsBody').innerHTML=`<tr><td colspan="7" class="empty-result">${loading?'正在加载清单…':'资料尚未加载，重新加载后显示清单。'}</td></tr>`;
   get('cardsPrevious').disabled=true;get('cardsNext').disabled=true;
   if(get('backupState'))get('backupState').textContent='资料加载后显示备份状态';if(get('saveState'))get('saveState').textContent='正在读取保存状态';
  }
  for(const element of get('cardsWorkspace').querySelectorAll('.card-filter-toolbar input,.card-filter-toolbar select,.card-filter-toolbar button,.card-selection-entry button')){
   if(!hasData){element.disabled=true;element.dataset.cardAwaiting='yes';}
   else if(element.dataset.cardAwaiting){element.disabled=false;delete element.dataset.cardAwaiting;}
  }
 }
 document.addEventListener('DOMContentLoaded',()=>{const retry=document.getElementById('cardReadRetry');if(retry)retry.onclick=()=>load();});
 return {activate,state};
})();
