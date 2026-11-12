/* Show collection status only when the task-page disclosure is opened. */
(()=>{
  const info=document.getElementById('workCollectionInfo');
  if(!info)return;
  const panel=info.closest('.collection-bar');
  const queue=panel.querySelector('.collection-actions a');
  for(const id of ['workCollectionPause','workCollectionResume'])document.getElementById(id).hidden=true;
  let request;
  function refresh(){
    if(request)return request;
    request=fetch('/api/collection/status',{cache:'no-store',signal:globalThis.AbortSignal?.timeout?.(8000)})
      .then(async response=>{
        if(!response.ok)throw Error(response.status===401?'请登录后查看采集进度':'读取采集进度失败');
        const {collection}=await response.json();
        info.textContent=collection.enabled!==true?'采集暂未启用，历史记录可查看':
          `${collection.worker_online?'采集器在线':'采集器离线'} · ${collection.paused?'队列已暂停':collection.waiting+' 个待处理'}`;
      }).catch(error=>{info.textContent=error.message;}).finally(()=>{request=null;});
    return request;
  }
  queue.href='/collection.html';queue.textContent='查看采集队列';queue.hidden=true;
  function displayEntry(){queue.hidden=typeof data==='undefined'||!data?.user?.permissions?.includes('collection_manage');}
  document.addEventListener('workspace-ready',displayEntry);
  document.addEventListener('toggle',event=>{if(event.target.open&&event.target.contains(panel)){displayEntry();void refresh();}},true);
  document.getElementById('workCollectionRefresh').onclick=refresh;
  displayEntry();
  if(panel.getClientRects().length)void refresh();
})();
