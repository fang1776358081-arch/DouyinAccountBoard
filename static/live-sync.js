/* Visible-page read scheduler. The adapter owns authorization, cursors and draft safety. */
window.LiveSync=(()=>{
 function create(options){
  const interval=Math.max(1000,Number(options.intervalMs)||5000),timeout=Math.max(1000,Number(options.timeoutMs)||15000);
  let started=false,timer=null,flight=null,generation=0,failures=0,lastSyncedAt=null,nextAllowedAt=0,state={phase:'waiting',lastSyncedAt:null,changes:0,failures:0};
  const hidden=()=>document.visibilityState==='hidden';
  const ready=()=>typeof options.ready!=='function'||options.ready();
  const blocked=()=>typeof options.blocked==='function'&&options.blocked();
  const emit=(phase,extra={})=>{state={phase,lastSyncedAt,changes:0,failures,...extra};try{options.onState?.({...state});}catch(error){/* A missing status widget must not break reads. */}};
  const clearTimer=()=>{if(timer!==null){clearTimeout(timer);timer=null;}};
  const schedule=delay=>{clearTimer();nextAllowedAt=Date.now()+delay;if(started&&!hidden())timer=setTimeout(()=>{timer=null;void tick();},delay);};
  const cancelRead=()=>{if(flight){flight.cancelled=true;flight.abort?.abort();flight.cancel?.(Error('read cancelled'));}};
  function detach(){
   window.removeEventListener?.('focus',wake);window.removeEventListener?.('pagehide',pause);
   document.removeEventListener?.('visibilitychange',visibility);document.removeEventListener?.('close',wake,true);
  }
  function pause(){generation++;clearTimer();cancelRead();if(started)emit('paused');}
  function visibility(){if(hidden())pause();else void wake();}
  function wake(){if(started&&!hidden()&&failures&&Date.now()<nextAllowedAt){schedule(nextAllowedAt-Date.now());return;}return poke();}
  function poke(){clearTimer();return tick();}
  async function tick(){
   if(!started)return;
   if(hidden()){clearTimer();emit('paused');return;}
   if(flight)return flight.promise;
   if(!ready()){emit('waiting');schedule(interval);return;}
   if(blocked()){emit('editing');schedule(interval);return;}
   const token={generation,cancelled:false,abort:typeof AbortController==='function'?new AbortController():null,timedOut:false};
   const deadline=new Promise((resolve,reject)=>{token.cancel=reject;});
   deadline.catch(()=>{}); // A synchronous status listener may stop before the read starts.
   flight=token;emit('syncing');
   let nextDelay=interval;
   const isCurrent=()=>started&&!token.cancelled&&token.generation===generation&&!hidden()&&ready()&&!blocked();
   const watchdog=setTimeout(()=>{token.timedOut=true;token.cancelled=true;token.abort?.abort();token.cancel(Error('read timeout'));},timeout);
   token.promise=(async()=>{
    try{
     if(!started||token.cancelled)return;
     const result=await Promise.race([options.run({signal:token.abort?.signal,isCurrent}),deadline]);
     if(!started||token.generation!==generation||hidden())return;
     if(token.timedOut){const failure=Error('read timeout');failure.name='TimeoutError';throw failure;}
     if(!ready()){emit('waiting');return;}
     // Resume promptly when a long read was made obsolete by a completed edit.
     // blocked() still prevents all reads while the edit remains open.
     if(['idle','updated','waiting','deferred'].includes(result?.status)&&typeof result.nextDelayMs==='number'&&Number.isFinite(result.nextDelayMs))nextDelay=Math.max(250,Math.min(60000,result.nextDelayMs));
     if(blocked()||result?.status==='deferred'){emit('editing',{changes:Number(result?.changes)||0});return;}
     if(token.cancelled)return;
     if(result?.status==='waiting'){emit('waiting');return;}
     if(result?.status==='unsupported'){nextDelay=60000;emit('unsupported');return;}
     if(!['idle','updated'].includes(result?.status))throw Error('invalid read result');
     failures=0;lastSyncedAt=Date.now();emit('synced',{changes:Number(result?.changes)||0});
    }catch(error){
     if(!started||token.generation!==generation||hidden())return;
     if(error?.status===401){started=false;clearTimer();detach();emit('auth');return;}
     failures++;nextDelay=Math.min(60000,interval*Math.pow(2,Math.min(failures,4)));
     emit('error',{retryInMs:nextDelay,forbidden:error?.status===403});
    }finally{
     clearTimeout(watchdog);if(flight===token)flight=null;schedule(nextDelay);
    }
   })();
   return token.promise;
  }
  function start(){
   if(started)return flight?.promise;
   started=true;generation++;
   window.addEventListener('focus',wake);window.addEventListener('pagehide',pause);
   document.addEventListener('visibilitychange',visibility);document.addEventListener('close',wake,true);
   return poke();
  }
  function stop(){started=false;generation++;clearTimer();cancelRead();detach();emit('paused');}
  return Object.freeze({start,poke,stop,getState:()=>({...state})});
 }
 function status(id,state){
  const element=document.getElementById(id);if(!element)return;
  const checked=state.lastSyncedAt==null?'':new Date(state.lastSyncedAt).toLocaleTimeString('zh-CN',{hour12:false});
  const words={waiting:'等待页面资料，自动同步将随后开始',paused:'页面在后台，自动同步已暂停',editing:'正在编辑或处理，暂缓自动同步',syncing:'正在检查其他人的修改…',synced:'已同步'+(checked?' · '+checked:''),unsupported:'自动同步暂不可用，请手动刷新',auth:'登录已失效，请重新登录',error:state.forbidden?'暂无读取更新权限，请刷新核对':'同步暂时中断，正在重试'};
  element.textContent=words[state.phase]||words.waiting;element.dataset.syncPhase=state.phase;
  const retry=document.getElementById(id==='boardSyncState'?'boardSyncRetry':'workSyncRetry');
  if(retry){retry.hidden=!['error','unsupported'].includes(state.phase);retry.disabled=state.phase==='syncing'||state.phase==='editing';}
 }
 return Object.freeze({create,status});
})();
