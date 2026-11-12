/* Collection progress is read independently of the account board and editors. */
(()=>{
  const $=id=>document.getElementById(id);
  const esc=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const pending=new Map();
  let user,page=1,status,requestNumber=0,reading=false,writing=false,interval;

  async function api(path,body){
    const response=await fetch(path,{cache:'no-store',signal:globalThis.AbortSignal?.timeout?.(15000),
      ...(body?{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':user.csrf},body:JSON.stringify(body)}:{})});
    const result=await response.json();
    if(response.status===401)location.replace('/login.html?next=%2Fcollection.html');
    if(!response.ok){const error=Error(result.error||'采集操作失败');error.status=response.status;throw error;}
    return result;
  }

  function time(value){
    if(!value)return '—';
    const instant=new Date(value);
    return Number.isFinite(instant.getTime())?instant.toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'}):value;
  }

  function notice(text){$('collectionNotice').textContent=text;}
  function fail(error){$('collectionError').hidden=!error;$('collectionError').textContent=error?.message||'';}
  function canSubmit(){return status?.can_manage&&status.collection?.worker_online&&!status.collection.paused;}

  function renderControls(){
    const c=status.collection;
    $('collectionAccountLink').hidden=!status.can_manage;
    $('collectionControls').hidden=!status.can_control;
    for(const button of $('collectionControls').querySelectorAll('button')){
      const command=button.dataset.collectionCommand;
      button.disabled=writing||(command==='open_browser'&&!c.browser_control)||
        (command==='pause'&&!!c.paused)||(command==='resume'&&!c.paused)||(command==='cancel_waiting'&&!c.waiting);
    }
    if(!c.enabled){notice('采集暂未启用，历史记录可查看。');return;}
    const online=c.worker_online?'采集器在线 · '+c.worker_status:'采集器离线，请启动已配置的本机采集器';
    notice(`${online} · ${c.paused?'队列已暂停'+(c.pause_reason?'：'+c.pause_reason:''):c.waiting+' 个待处理'}`);
  }

  function renderRows(result){
    page=result.page;
    const pages=Math.max(1,Math.ceil(result.total/result.page_size));
    $('collectionTotal').textContent=`${result.total} 个账号`;
    $('collectionPagination').hidden=pages===1;
    $('collectionPrevious').disabled=page<=1;
    $('collectionNext').disabled=page>=pages;
    $('collectionPage').textContent=`第 ${page} / ${pages} 页`;
    $('collectionRows').innerHTML=result.jobs.length?result.jobs.map(job=>{
      const inactive=!job.current&&['queued','running'].includes(job.job_state);
      const state=inactive?'旧任务（未运行）':job.state||'历史记录';
      const tone=state==='采集成功'?'good':['采集失败','需要验证'].includes(state)?'warn':'';
      const canRetry=inactive||['采集失败','需要验证','已取消'].includes(job.state);
      const link='/?page=accounts&account_id='+encodeURIComponent(job.account_id);
      const id=job.douyin_id?'抖音号 '+job.douyin_id:'抖音号待补';
      const office=job.office_name||'办公室待核对';
      return `<tr><td><a href="${esc(link)}">${esc(job.name||'昵称待补充')}</a><small>${esc(id)} · ${esc(office)}</small></td>`+
        `<td><span class="badge ${tone}">${esc(state)}</span>${job.error?`<small>${esc(job.error)}</small>`:''}</td>`+
        `<td>${esc(job.count??'—')}</td><td>${esc(time(job.last_collected))}</td>`+
        `<td>${canRetry?`<button class="text-button" data-retry="${esc(job.account_id)}" ${canSubmit()&&!writing?'':'disabled'}>重新采集</button>`:'—'}</td></tr>`;
    }).join(''):`<tr><td colspan="5" class="collection-empty">${$('collectionState').value?'没有符合筛选的任务':'暂无采集任务'}</td></tr>`;
    $('collectionSheet').hidden=false;
  }

  async function refresh({background=false}={}){
    if(!user?.permissions.includes('collection_manage')||(background&&(reading||writing||document.visibilityState!=='visible'||document.querySelector('dialog[open]'))))return;
    const serial=++requestNumber;
    reading=true;
    if(!background)fail(null);
    try{
      const query=new URLSearchParams({page,state:$('collectionState').value});
      const [next,rows]=await Promise.all([api('/api/collection/status'),api('/api/collection/jobs?'+query)]);
      if(serial!==requestNumber)return;
      if(rows.user){user=rows.user;window.WorkbenchNav?.update({user});}
      status=next;renderControls();renderRows(rows);
      if(status.collection.enabled&&!interval)interval=setInterval(()=>void refresh({background:true}),8000);
      else if(!status.collection.enabled&&interval){clearInterval(interval);interval=null;}
    }catch(error){
      if(serial!==requestNumber)return;
      if(error.status===401||error.status===403){
        $('collectionSheet').hidden=true;$('collectionRows').textContent='';$('collectionAccountLink').hidden=true;status=null;
      }
      fail(error);
    }
    finally{if(serial===requestNumber)reading=false;}
  }

  async function start(){
    fail(null);
    try{
      ({user}=await api('/api/auth/me'));
      if(!user){location.replace('/login.html?next=%2Fcollection.html');return;}
      window.WorkbenchNav?.update({user});
      if(!user.permissions.includes('collection_manage')){notice('当前账号没有作品采集管理权限。');return;}
      await refresh();
    }catch(error){fail(error);notice('采集队列暂时无法读取。');}
  }

  async function submit(id){
    if(!pending.has(id))pending.set(id,crypto.randomUUID());
    const request_id=pending.get(id);
    let result;
    try{result=await api('/api/collection/enqueue',{ids:[id],request_id});}
    catch(error){
      if(error.status){pending.delete(id);throw error;}
      try{result=await api('/api/idempotency/result?request_id='+encodeURIComponent(request_id));}
      catch(receipt){
        if(receipt.status&&![404,409].includes(receipt.status)){pending.delete(id);throw receipt;}
        throw Error('提交结果待确认，再次点击会核对同一次请求。');
      }
    }
    pending.delete(id);
    return result.queued?'已加入采集队列':result.duplicate?'该账号已在队列中':result.invalid?'账号主页格式待核对':'账号缺少抖音号或主页';
  }

  async function act(command,id){
    if(writing||!status||(id?!canSubmit():!status.can_control))return;
    writing=true;
    ++requestNumber;
    reading=false;
    renderControls();
    fail(null);
    for(const button of $('collectionRows').querySelectorAll('button'))button.disabled=true;
    let error,message;
    try{
      if(id){message=await submit(id);}
      else{
        const result=await api('/api/collection/settings',{[command]:true});
        message=command==='cancel_waiting'?`已取消 ${result.cancelled} 个等待任务`:
          ({open_browser:'已通知本机采集器打开登录窗口',pause:'队列已暂停，当前任务将继续完成',resume:'已继续队列'})[command];
      }
    }catch(failure){error=failure;}
    writing=false;
    await refresh();
    if(error)fail(error);
    else if(message)notice(message);
  }

  $('collectionRefresh').onclick=()=>{if(user)void refresh();else void start();};
  $('collectionState').onchange=()=>{page=1;void refresh();};
  $('collectionPrevious').onclick=()=>{page--;void refresh();};
  $('collectionNext').onclick=()=>{page++;void refresh();};
  $('collectionRows').onclick=event=>{const id=event.target.closest('[data-retry]')?.dataset.retry;if(id)void act(null,id);};
  $('collectionControls').onclick=event=>{
    const button=event.target.closest('[data-collection-command]');
    if(!button||button.disabled)return;
    if(button.dataset.collectionCommand==='cancel_waiting'){
      $('collectionCancelDialog').returnValue='';$('collectionCancelDialog').showModal();
    }else void act(button.dataset.collectionCommand);
  };
  $('collectionCancelDialog').onclose=()=>{if($('collectionCancelDialog').returnValue==='cancel')void act('cancel_waiting');};
  void start();
})();
