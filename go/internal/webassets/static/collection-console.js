/* Collection progress is read independently of the account board and editors. */
(()=>{
  const $=id=>document.getElementById(id);
  const esc=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const pending=new Map();
  let user,page=1,status,requestNumber=0,reading=false,writing=false,interval,action='';
  let targets=null,targetRequest=null,targetError='',accessGeneration=0;

  async function api(path,body,timeout=15000){
    const response=await fetch(path,{cache:'no-store',signal:globalThis.AbortSignal?.timeout?.(timeout),
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
  function number(value){return Number.isSafeInteger(value)&&value>=0?value:null;}
  function validStatus(next){
    const c=next?.collection;
    return c&&typeof next.can_control==='boolean'&&typeof next.can_manage==='boolean'&&
      typeof c.enabled==='boolean'&&typeof c.worker_online==='boolean'&&typeof c.worker_status==='string'&&
      [true,false,0,1].includes(c.paused)&&['waiting','done','failed'].every(key=>number(c[key])!==null);
  }
  function ready(c){return c?.enabled&&c.worker_online&&!c.paused&&/^(待命|正在采集)(?:（无界面）)?$/.test(c.worker_status||'');}
  function canSubmit(){return status?.can_manage&&ready(status.collection);}
  function clearScope(){accessGeneration++;targets=null;targetRequest=null;targetError='';pending.clear();}
  function setUser(next){
    if(user&&(user.id!==next.id||JSON.stringify(user.permissions)!==JSON.stringify(next.permissions))){
      clearScope();status=null;++requestNumber;reading=false;$('collectionSheet').hidden=true;$('collectionRows').textContent='';
    }
    user=next;window.WorkbenchNav?.update({user});
  }
  function setStatus(next){
    if(status&&(status.can_manage!==next.can_manage||status.can_control!==next.can_control))clearScope();
    status=next;renderControls();
  }
  function denied(error){
    if(![401,403].includes(error.status))return;
    clearScope();status=null;++requestNumber;reading=false;
    $('collectionSheet').hidden=true;$('collectionRows').textContent='';
  }

  function renderControls(){
    const c=status?.collection,control=!!status?.can_control;
    $('collectionStart').disabled=writing||(status?(!status.can_manage||!c.enabled||(!!c.waiting&&ready(c))||(!ready(c)&&!control)||(!c.waiting&&!targets&&!!targetRequest)):!!user&&!user.permissions.includes('collection_manage'));
    $('collectionPause').disabled=writing||!control||!c?.enabled||!!c.paused;
    $('collectionStart').textContent=action==='start'?'正在开始…':'开始采集';
    $('collectionPause').textContent=action==='pause'?'正在暂停…':'暂停采集';
    $('collectionControls').hidden=!status?.can_manage;
    $('collectionAccountLink').hidden=!status?.can_manage;
    $('collectionOpenBrowser').hidden=!control;
    $('collectionOpenBrowser').disabled=writing||!c?.browser_control;
    $('collectionOpenBrowser').title=c?.browser_control?'窗口在已连接的采集电脑打开；请在窗口内完成登录或验证':'采集电脑离线或未提供图形窗口，请先启动采集器';
    for(const button of $('collectionControls').querySelectorAll('button')){
      button.hidden=!control;
      button.disabled=writing||!control||(button.dataset.collectionCommand==='open_browser'?!c?.browser_control:!c?.waiting);
    }
    for(const [key,id] of [['done','collectionDone'],['waiting','collectionWaiting'],['failed','collectionFailed']]){
      $(id).textContent=number(c?.[key])??'—';
    }
    $('collectionNeedsProfile').textContent=number(c?.needs_profile)??'—';
    $('collectionNeedsProfileGroup').hidden=!c?.profile_only&&!(number(c?.needs_profile)>0);
    const waiting=number(c?.waiting)||0;
    $('collectionScope').textContent=waiting?`现有共享队列：授权范围内 ${waiting} 个待处理，开始只继续队列`:
      targets?`全部授权账号 ${targets.length} 个`:
        targetError?'采集范围读取失败，点击“开始采集”重试':'正在确认全部授权账号…';
    let text,tone='muted';
    if(user&&!user.permissions.includes('collection_manage'))text='当前账号没有作品采集管理权限';
    else if(!c)text='暂时无法确认采集状态，可点击“开始采集”重试';
    else if(!c.enabled)text='采集暂未启用，历史记录可查看';
    else if(!status.can_manage)text='当前账号没有作品采集提交权限';
    else if(!c.worker_online){text='采集器离线，请启动已配置的本机采集器后再开始';tone='warn';}
    else if(c.paused){
      text='已暂停，当前任务完成后停止'+(c.pause_reason?'：'+c.pause_reason:'')+(!control?'；请管理员继续':'');
      if(/安全验证|登录已失效|重新登录|需要验证/.test(c.pause_reason||''))text+='；请在采集帮助打开窗口处理';
      tone='warn';
    }
    else if(!ready(c)){text=control?'待开始，点击“开始采集”继续后台采集':'待开始，请管理员继续后台采集';tone='muted';}
    else{text=waiting?'正在处理现有队列':'采集器已就绪';tone='good';}
    $('collectionStatus').textContent=text;$('collectionStatus').dataset.tone=tone;
    notice(text);
  }

  async function loadTargets(){
    if(targets)return targets;
    if(targetRequest)return targetRequest;
    const owner=user?.id,generation=accessGeneration;
    const request=(async()=>{
      const result=await api('/api/board?view=work&version_scope=day');
      if(user?.id!==owner||generation!==accessGeneration)throw Error('权限已更新，请重新确认采集范围');
      if(result.workspace_view!=='summary'||!Array.isArray(result.accounts)||
         result.accounts.some(account=>typeof account.id!=='string'||!account.id||account.id.length>256||account.id.includes('\0'))){
        throw Error('无法确认授权账号范围，请重试');
      }
      targets=[...new Set(result.accounts.map(account=>account.id))];targetError='';return targets;
    })();
    targetRequest=request;
    try{return await request;}
    catch(error){if(generation===accessGeneration){targetError=error.message;denied(error);}throw error;}
    finally{if(targetRequest===request)targetRequest=null;renderControls();}
  }

  function renderRows(result){
    page=result.page;
    const pages=Math.max(1,Math.ceil(result.total/result.page_size));
    $('collectionTotal').textContent=`${result.total} 个账号`;
    $('collectionPagination').hidden=pages===1;
    $('collectionPrevious').disabled=page<=1;$('collectionNext').disabled=page>=pages;
    $('collectionPage').textContent=`第 ${page} / ${pages} 页`;
    $('collectionRows').innerHTML=result.jobs.length?result.jobs.map(job=>{
      const inactive=!job.current&&['queued','running'].includes(job.job_state);
      const state=inactive?'旧任务（未运行）':job.state||'历史记录';
      const tone=state==='采集成功'?'good':['采集失败','需要验证'].includes(state)?'warn':'';
      const needsProfile=job.state==='待补主页';
      const canRetry=!needsProfile&&(inactive||['采集失败','需要验证','已取消'].includes(job.state));
      const link='/?page=accounts&account_id='+encodeURIComponent(job.account_id);
      const id=job.douyin_id?'抖音号 '+job.douyin_id:'抖音号待补';
      const office=job.office_name||'办公室待核对';
      return `<tr><td><a href="${esc(link)}">${esc(job.name||'昵称待补充')}</a><small>${esc(id)} · ${esc(office)}</small></td>`+
        `<td><span class="badge ${tone}">${esc(state)}</span>${job.error?`<small>${esc(job.error)}</small>`:''}</td>`+
        `<td>${esc(job.count??'—')}</td><td>${esc(time(job.last_collected))}</td>`+
        `<td>${needsProfile?`<a class="text-button" href="${esc(link)}">补充主页</a>`:canRetry?`<button class="text-button" data-retry="${esc(job.account_id)}" ${canSubmit()&&!writing?'':'disabled'}>重新采集</button>`:'—'}</td></tr>`;
    }).join(''):`<tr><td colspan="5" class="collection-empty">${$('collectionState').value?'没有符合筛选的任务':'暂无采集任务'}</td></tr>`;
    $('collectionSheet').hidden=false;
  }

  async function readStatus(timeout){
    const generation=accessGeneration;
    const next=await api('/api/collection/status',undefined,timeout);
    if(generation!==accessGeneration)throw Error('权限已更新，请重新读取采集状态');
    if(!validStatus(next))throw Error('采集状态响应无效，请重试');
    setStatus(next);return next;
  }

  async function refresh({background=false}={}){
    if(!user?.permissions.includes('collection_manage')||(background&&(reading||writing||document.visibilityState!=='visible'||document.querySelector('dialog[open]'))))return;
    const serial=++requestNumber;reading=true;
    if(!background)fail(null);
    try{
      const query=new URLSearchParams({page,state:$('collectionState').value});
      const [next,rows]=await Promise.all([api('/api/collection/status'),api('/api/collection/jobs?'+query)]);
      if(serial!==requestNumber)return;
      if(rows.user)setUser(rows.user);
      if(!validStatus(next))throw Error('采集状态响应无效，请重试');
      setStatus(next);renderRows(rows);
      if(status.collection.enabled&&!interval)interval=setInterval(()=>void refresh({background:true}),8000);
      else if(!status.collection.enabled&&interval){clearInterval(interval);interval=null;}
    }catch(error){if(serial===requestNumber){denied(error);fail(error);renderControls();}}
    finally{if(serial===requestNumber)reading=false;}
  }

  async function start(){
    fail(null);
    try{
      const result=await api('/api/auth/me');
      if(!result.user){location.replace('/login.html?next=%2Fcollection.html');return;}
      setUser(result.user);
      if(!user.permissions.includes('collection_manage')){notice('当前账号没有作品采集管理权限。');renderControls();return;}
      await Promise.all([refresh(),loadTargets()]);
    }catch(error){denied(error);fail(error);renderControls();}
  }

  async function submit(ids,key){
    if(!pending.has(key))pending.set(key,crypto.randomUUID());
    const request_id=pending.get(key);
    let result;
    try{result=await api('/api/collection/enqueue',{ids,request_id});}
    catch(error){
      if(error.status){pending.delete(key);throw error;}
      try{result=await api('/api/idempotency/result?request_id='+encodeURIComponent(request_id));}
      catch(receipt){
        if(receipt.status&&![404,409].includes(receipt.status)){pending.delete(key);throw receipt;}
        throw Error('提交结果待确认，再次点击会核对同一次请求。');
      }
    }
    pending.delete(key);
    return `已加入 ${result.queued||0} 个；队列中已有 ${result.duplicate||0} 个；资料待核对 ${(result.invalid||0)+(result.missing||0)} 个`;
  }

  function requireSubmit(){
    if(!status?.can_manage)throw Error('当前账号没有作品采集提交权限');
    if(!status.collection.enabled)throw Error('作品采集暂未启用');
    if(!status.collection.worker_online)throw Error('采集器离线，请启动采集器后再次开始');
  }
  async function ensureReady(resume){
    if(resume){
      if(!status.can_control)throw Error('队列需要管理员继续，请联系管理员开始采集');
      await api('/api/collection/settings',{resume:true});
    }
    const deadline=Date.now()+8000;
    for(let attempt=0;attempt<3;attempt++){
      if(Date.now()>=deadline)break;
      await readStatus(Math.max(1,deadline-Date.now()));requireSubmit();
      if(ready(status.collection))return;
      if(status.collection.paused)throw Error(status.collection.pause_reason||'队列仍已暂停，请核对后再次开始');
      if(attempt<2&&Date.now()<deadline)await new Promise(resolve=>setTimeout(resolve,Math.min(3100,deadline-Date.now())));
    }
    throw Error('采集器尚未确认就绪，请稍后再次点击“开始采集”');
  }

  async function begin(){
    if(writing)return;
    if(!user||!status){await start();return;}
    if(!status.can_manage)return;
    writing=true;action='start';++requestNumber;reading=false;renderControls();fail(null);
    let error,message;
    try{
      await readStatus();requireSubmit();
      const existing=number(status.collection.waiting)||0;
      if(existing){
        pending.delete('all');
        if(ready(status.collection))message='现有共享队列正在处理，没有重复添加账号';
        else{await ensureReady(true);message='已继续现有共享队列，没有重复添加账号';}
      }else{
        const confirmed=targets!==null;
        const ids=await loadTargets();
        if(!ids.length)throw Error('授权范围内没有可采集账号');
        if(ids.length>2000)throw Error('全部授权账号超过单次2000个，请在“采集帮助”中到账号页缩小范围');
        if(!confirmed)message=`已确认全部授权账号 ${ids.length} 个，请再次点击“开始采集”`;
        else{
          await ensureReady(!ready(status.collection));
          if(status.collection.waiting){pending.delete('all');message='现有共享队列已开始处理，没有重复添加账号';}
          else message=await submit(ids,'all');
        }
      }
    }catch(failure){error=failure;denied(failure);}
    writing=false;action='';await refresh();renderControls();
    if(error)fail(error);else if(message)notice(message);
  }

  async function act(command,id){
    if(writing||!status||(id?!canSubmit():!status.can_control))return;
    writing=true;action=command||'retry';++requestNumber;reading=false;renderControls();fail(null);
    for(const button of $('collectionRows').querySelectorAll('button'))button.disabled=true;
    let error,message;
    try{
      if(id)message=await submit([id],'account:'+id);
      else{
        const result=await api('/api/collection/settings',{[command]:true});
        message=command==='cancel_waiting'?`已取消 ${result.cancelled} 个等待任务`:
          ({open_browser:'已通知采集电脑打开窗口；窗口在采集电脑显示',pause:'队列已暂停，当前任务完成后停止'})[command];
      }
    }catch(failure){error=failure;denied(failure);}
    writing=false;action='';await refresh();renderControls();
    if(error)fail(error);else if(message)notice(message);
  }

  $('collectionStart').onclick=()=>void begin();
  $('collectionPause').onclick=()=>{if(!$('collectionPause').disabled)void act('pause');};
  const initialState=new URLSearchParams(location.search||'').get('state');
  if(['pending','采集成功','采集失败','需要验证','待补主页','已取消'].includes(initialState))$('collectionState').value=initialState;
  $('collectionState').onchange=()=>{page=1;void refresh();};
  $('collectionPrevious').onclick=()=>{page--;void refresh();};
  $('collectionNext').onclick=()=>{page++;void refresh();};
  $('collectionRows').onclick=event=>{const id=event.target.closest('[data-retry]')?.dataset.retry;if(id)void act(null,id);};
  $('collectionControls').onclick=event=>{
    const button=event.target.closest('[data-collection-command]');
    if(button?.dataset.collectionCommand==='cancel_waiting'&&!button.disabled){$('collectionCancelDialog').returnValue='';$('collectionCancelDialog').showModal();}
  };
  $('collectionOpenBrowser').onclick=()=>{if(!$('collectionOpenBrowser').disabled)void act('open_browser');};
  $('collectionCancelDialog').onclose=()=>{if($('collectionCancelDialog').returnValue==='cancel')void act('cancel_waiting');};
  void start();
})();
