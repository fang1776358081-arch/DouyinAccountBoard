/* One account cell at a time. Draft versions never become full-board versions. */
window.AccountInline=(()=>{
 const labels={name:'昵称',status:'账号状态',note:'备注'};
 let draft=null,composing=false,defaultMessage='正在核对编辑权限…',lastMessage=defaultMessage,undoEntry=null,suspended=null,restoring=false;
 const confirmed=new Map();
 let painting=0;
 const confirmedKey=(user,id,field)=>JSON.stringify([String(user),String(id),field]);
 const state=()=>typeof data==='undefined'?null:data;
 const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const permission=field=>field==='status'?'account_status':'account_manage';
 const allowed=field=>Object.hasOwn(labels,field)&&typeof hasPermission==='function'&&hasPermission(permission(field));
 const cellId=(id,field)=>'account-inline-'+field+'-'+encodeURIComponent(String(id));
 const element=id=>document.getElementById(id);
 const interaction=()=>{if(typeof boardSyncInteraction==='function')boardSyncInteraction();};
 const pilotActive=()=>window.AccountPilot?.enabled()===true;
 const pilotKey=()=>{const user=state()?.user;return JSON.stringify([String(user?.id),user?.authorization_version??null,user?.scope_mode??null,[...(user?.office_ids||[])].sort(),[...(user?.permissions||[])].sort(),'three']);};
 function validUndo(){return !!undoEntry&&pilotActive()&&undoEntry.key===pilotKey()&&Date.now()<=undoEntry.until&&allowed(undoEntry.field);}
 function hint(message=lastMessage){lastMessage=message;const node=element('accountInlineHint');if(node){node.textContent=message;node.dataset.inlinePhase=draft?.phase||'idle';if(!draft&&validUndo()){const button=document.createElement('button');button.type='button';button.className='account-inline-undo';button.dataset.inlineUndo='';button.textContent='撤销刚才的'+labels[undoEntry.field]+'修改（30秒内）';node.append?.(button);}}}
 function refreshHint(){
  const fields=state()?.user?Object.keys(labels).filter(allowed).map(field=>labels[field]):null;
  const next=fields===null?'正在核对编辑权限…':fields.length?fields.join('、')+'支持点击编辑；Enter 或移开焦点保存，Esc 取消。':'当前账号为只读，暂无可直接编辑的账号字段。';
  const preserve=!!draft||lastMessage!==defaultMessage;defaultMessage=next;if(!preserve)hint(next);
 }
 function announceBlocked(){hint(draft?.phase==='unknown'?'保存结果待确认，请先点击“重试核对”；草稿和原请求已保留。':'正在编辑'+(labels[draft?.field]||'账号')+'，请先完成保存或取消，再继续此操作。');}
 function hasDraft(){return !!draft;}
 function cell(account,field,fallback){
  if(!allowed(field))return fallback;
  const held=confirmed.get(confirmedKey(state()?.user?.id,account.id,field)),actual=held===undefined?account[field]:held;
  const value=field==='note'?escape(String(actual||'').slice(0,100)||'填写备注'):held===undefined?fallback:field==='name'?'<strong>'+escape(actual)+'</strong>':fallback.replace(/^<span class="badge[^"]*">[\s\S]*?<\/span>/,'<span class="badge '+(actual==='正常'?'good':['短期封禁','封禁','发不了','停用'].includes(actual)?'warn':'')+'">'+escape(actual)+'</span>');
  return `<span class="account-inline-cell" id="${escape(cellId(account.id,field))}" data-inline-cell="${escape(account.id)}" data-inline-field="${field}"><span class="account-inline-value">${value}</span>${field==='status'&&held!==undefined?'<small class="account-inline-pending" role="status">同步中</small>':''}<button type="button" class="account-inline-open" data-inline-account="${escape(account.id)}" data-inline-field="${field}" aria-label="编辑${labels[field]}">编辑</button></span>`;
 }
 function phaseText(entry){return ({loading:'正在读取当前资料…',editing:'Enter 保存 · Esc 取消',saving:'正在保存，请稍候…',checking:'正在核对保存结果…',unknown:'保存结果待确认，原输入与请求已保留',conflict:entry.message||'其他人已修改，草稿已保留；取消后重新编辑',error:entry.message||'保存失败，草稿已保留'})[entry.phase]||'';}
 function paint(focus=false){
  const entry=draft;if(!entry)return;const host=element(cellId(entry.id,entry.field));if(!host){announceBlocked();return;}
  // Replacing a focused editor dispatches focusout synchronously in browsers.
  // Only a user's blur should save; repainting must not capture the old input.
  painting++;
  try{
  const busy=['loading','saving','checking'].includes(entry.phase),locked=busy||entry.phase==='unknown';
  const input=entry.field==='note'?`<textarea id="accountInlineInput" data-inline-input rows="3" maxlength="2000" aria-label="编辑备注" ${locked?'disabled':''}>${escape(entry.value)}</textarea>`:entry.field==='status'?`<select id="accountInlineInput" data-inline-input aria-label="编辑账号状态" ${locked?'disabled':''}>${(state()?.statuses||[]).map(value=>`<option ${value===entry.value?'selected':''}>${escape(value)}</option>`).join('')}</select>`:`<input id="accountInlineInput" data-inline-input maxlength="100" aria-label="编辑昵称" value="${escape(entry.value)}" ${locked?'disabled':''}>`;
  host.innerHTML=`<span class="account-inline-editor" data-inline-editor>${input}<span class="account-inline-actions"><button type="button" data-inline-action="save" ${busy||entry.phase==='conflict'?'disabled':''}>${entry.phase==='unknown'?'重试核对':'保存'}</button><button type="button" data-inline-action="cancel" ${busy&&entry.phase!=='loading'||entry.phase==='unknown'?'disabled':''}>${entry.current?'采用当前内容':'取消'}</button></span><span id="accountInlineStatus" class="account-inline-status" role="status">${escape(phaseText(entry))}</span></span>`;
  const inputNode=element('accountInlineInput');if(inputNode){inputNode.value=entry.value;if(focus&&!locked){inputNode.focus?.();if(entry.field!=='status')inputNode.setSelectionRange?.(entry.value.length,entry.value.length);}}
  hint(phaseText(entry)+(entry.field==='note'&&entry.phase==='editing'?'；备注可换行，Ctrl+Enter 保存。':''));
  }finally{painting--;}
 }
 function canonical(result,id){
  const row=result?.account;if(!row||String(row.id)!==String(id)||!['name','status','note'].every(key=>typeof row[key]==='string')||typeof result.record_version!=='string'||!result.record_version||!Number.isInteger(result.cursor)||result.cursor<0)throw Error('当前资料核对失败，请重试');
  return {account:{id:String(row.id),name:row.name,status:row.status,note:row.note},record_version:result.record_version,cursor:result.cursor};
 }
 async function readCurrent(entry){
  const abort=typeof AbortController==='function'?new AbortController():null;entry.readAbort=abort;
  const timer=abort?setTimeout(()=>abort.abort(),15000):null;
  try{const reading=api('/api/account/inline?id='+encodeURIComponent(entry.id),undefined,{applyVersions:false,signal:abort?.signal});return canonical(typeof boardReadWithSignal==='function'?await boardReadWithSignal(reading,abort?.signal):await reading,entry.id);}
  finally{if(timer!==null)clearTimeout(timer);if(entry.readAbort===abort)entry.readAbort=null;}
 }
 function otherWritePending(){return typeof boardWrites!=='undefined'&&boardWrites>0||typeof dailyViewLoading!=='undefined'&&!!dailyViewLoading||typeof dailyRequests!=='undefined'&&dailyRequests.size>0||typeof pendingRequestIds!=='undefined'&&pendingRequestIds.size>0;}
 async function begin(id,field){
  if(draft){announceBlocked();return false;}if(!allowed(field))return false;
  const board=state(),row=board?.accounts?.find(item=>String(item.id)===String(id));
  if(!row||otherWritePending()||document.querySelector?.('dialog[open]')?.open){hint('页面正在处理其他操作，请稍后再编辑。');return false;}
  composing=false;const entry=draft={id:String(id),field,value:String(row[field]||''),phase:'loading',baseline:null,payload:null,sent:false,running:null,opened:board,userId:String(board.user?.id),readAbort:null};interaction();paint();
  try{const fresh=await readCurrent(entry);if(draft!==entry)return false;entry.baseline=fresh;entry.value=fresh.account[field];entry.phase='editing';paint(true);return true;}
  catch(error){if(draft!==entry)return false;entry.phase='error';entry.message=error.status===401?'登录已失效，请重新登录':error.message||'读取失败，草稿已保留';entry.lastError=error;paint();return false;}
 }
 function captureInput(){const input=element('accountInlineInput');if(draft&&input&&!input.disabled)draft.value=String(input.value??'');}
 function inputChanged(){
  if(!draft)return;captureInput();interaction();
  if(draft.phase==='error'&&draft.baseline){draft.phase='editing';draft.message='';const status=element('accountInlineStatus');if(status)status.textContent=phaseText(draft);hint(phaseText(draft));}
 }
 function validate(entry){
  let value=entry.value;if(entry.field==='name'){value=value.trim();if(!value||value.length>100)throw Error('昵称不能为空或超过100字');}
  if(entry.field==='note'&&value.length>2000)throw Error('备注不能超过2000字');
  if(entry.field==='status'&&!(state()?.statuses||[]).includes(value))throw Error('请选择有效的账号状态');
  return value;
 }
 function currentEntry(entry){return draft===entry&&state()===entry.opened&&String(state()?.user?.id)===entry.userId;}
 function leave(message){draft=null;composing=false;interaction();if(typeof render==='function')render();hint(message);window.boardLiveSync?.poke();}
 function attemptCurrent(entry,attempt){return currentEntry(entry)&&entry.attempt===attempt&&!attempt.expired;}
 async function saved(entry,result,attempt){
  if(!attemptCurrent(entry,attempt))return false;
  const receipt=canonical(result,entry.id);if(result.ok!==true||String(result.id)!==entry.id||result._record_versions?.['account:'+entry.id]!==receipt.record_version)throw Error('保存回执尚未核对成功，原请求已保留');
  // Even a stored idempotency success is historical. Read the current authorized
  // value and token together, and never graft that token onto other stale fields.
  entry.phase='checking';paint();const fresh=await readCurrent(entry);if(!attemptCurrent(entry,attempt))return false;
  const row=state().accounts.find(item=>String(item.id)===entry.id);if(!row)throw Error('账号已不在当前页面，请取消后刷新核对');
  const expected=entry.payload.changes[entry.field];
  if(fresh.account[entry.field]!==expected){entry.current=fresh;entry.phase='conflict';entry.message='本次修改已保存过，但随后有人修改了此项。草稿已保留；当前内容：'+fresh.account[entry.field];paint(true);return false;}
  confirmed.set(confirmedKey(entry.userId,entry.id,entry.field),fresh.account[entry.field]);
  if(pilotActive()&&['name','note'].includes(entry.field))undoEntry=entry.undoing?null:{key:pilotKey(),id:entry.id,field:entry.field,before:entry.baseline.account[entry.field],after:fresh.account[entry.field],version:fresh.record_version,until:Date.now()+30000};
  leave(entry.undoing?'已撤销刚才的'+labels[entry.field]+'修改。':entry.field==='status'?'已保存账号状态，正在更新筛选结果和统计…':'已保存'+labels[entry.field]+'，正在同步其他修改。');return true;
 }
 async function transmit(entry,attempt){
  const options={fixedPayload:true,applyVersions:false,trackWrite:false};let result;
  if(entry.sent){
   try{result=await api('/api/idempotency/result?request_id='+encodeURIComponent(entry.payload.request_id),undefined,options);}
   catch(error){if(error.status===404||error.code==='REQUEST_PENDING')result=await api('/api/account/patch',entry.payload,options);else throw error;}
  }else{entry.sent=true;result=await api('/api/account/patch',entry.payload,options);}
  if(!attemptCurrent(entry,attempt))return false;return saved(entry,result,attempt);
 }
 async function save(){
  const entry=draft;if(!entry)return false;if(entry.running)return entry.running;
  if(entry.phase==='loading')return false;if(entry.phase==='conflict'){announceBlocked();return false;}
  if(!entry.baseline){entry.phase='error';entry.message='资料尚未核对，请取消后重新编辑。';paint();return false;}
  if(!currentEntry(entry)||!allowed(entry.field)){entry.phase='error';entry.message='当前页面或权限已变化，请取消后重新核对。';paint();return false;}
  if(entry.phase!=='unknown')captureInput();
  let value;try{value=validate(entry);}catch(error){entry.phase='error';entry.message=error.message;paint(true);return false;}
  if(!entry.sent&&value===entry.baseline.account[entry.field]){leave('没有需要保存的修改。');return true;}
  if(!entry.payload||entry.phase!=='unknown'&&entry.lastError?.status!==undefined&&entry.payload.changes[entry.field]!==value){
   entry.payload=Object.freeze({id:entry.id,changes:Object.freeze({[entry.field]:value}),request_id:newRequestId(),return_account:true,_expected_versions:Object.freeze({['account:'+entry.id]:entry.baseline.record_version})});entry.sent=false;
  }
  const attempt={expired:false};entry.attempt=attempt;entry.phase='saving';entry.message='';paint();interaction();
  let timer;const deadline=new Promise((resolve,reject)=>{timer=setTimeout(()=>{attempt.expired=true;reject(Error('保存结果待确认'));},15000);});
  entry.running=(async()=>{
   try{return await Promise.race([transmit(entry,attempt),deadline])===true;}
   catch(error){
    if(draft!==entry||entry.attempt!==attempt)return false;entry.lastError=error;
    if(error.status===409&&error.code!=='REQUEST_PENDING'){entry.phase='conflict';entry.message='其他人已修改，草稿已保留；请取消后重新编辑。';}
    else if(error.status===undefined||error.status>=500||error.code==='REQUEST_PENDING'||entry.phase==='checking'){entry.phase='unknown';entry.message='保存结果待确认，请重试核对。';}
    else{entry.phase='error';entry.message=error.message||'保存失败，草稿已保留';}
    paint();return false;
   }finally{clearTimeout(timer);if(entry.attempt===attempt)entry.running=null;}
  })();return entry.running;
 }
 function cancel(){
  if(!draft)return true;if(draft.running||['saving','checking','unknown'].includes(draft.phase)){announceBlocked();return false;}
  const entry=draft;if(entry.current)confirmed.set(confirmedKey(entry.userId,entry.id,entry.field),entry.current.account[entry.field]);draft=null;composing=false;entry.readAbort?.abort();interaction();if(typeof render==='function')render();hint(entry.current?'已采用当前内容，未覆盖其他人的修改。':'已取消编辑，原资料未被此草稿覆盖。');window.boardLiveSync?.poke();return true;
 }
 async function undo(){
  if(draft){announceBlocked();return false;}
  if(!validUndo()){undoEntry=null;hint('撤销时限已过或当前范围已变化，请重新核对资料。');return false;}
  const previous=undoEntry;if(!await begin(previous.id,previous.field))return false;
  if(!draft||!validUndo()||draft.baseline.record_version!==previous.version||draft.baseline.account[previous.field]!==previous.after){cancel();undoEntry=null;hint('资料已被更新，本次撤销不会覆盖后续修改。');return false;}
  draft.value=previous.before;draft.undoing=true;paint();return save();
 }
 function suspendForNavigation(nextPage){
  if(!draft||!pilotActive()||nextPage==='accounts'||draft.sent||draft.running||!['name','note'].includes(draft.field)||!['editing','error'].includes(draft.phase)||!draft.baseline)return false;
  captureInput();const entry=draft;suspended={key:pilotKey(),id:entry.id,field:entry.field,value:entry.value,version:entry.baseline.record_version};draft=null;composing=false;interaction();hint('编辑草稿已在当前页面内暂存，返回办公室三账号中心后继续；尚未保存。');return true;
 }
 async function restoreSuspended(){
  if(restoring||draft||!suspended||!pilotActive())return false;
  if(suspended.key!==pilotKey()){suspended=null;return false;}
  const held=suspended;restoring=true;
  try{const begun=await begin(held.id,held.field);if(!draft)return false;
   suspended=null;draft.value=held.value;
   if(begun&&draft.baseline?.record_version!==held.version){draft.phase='conflict';draft.message='暂存期间资料已被更新，原输入已保留；取消后重新核对，不覆盖他人修改。';}
   paint(true);return begun;
  }finally{restoring=false;}
 }
 function clearSuspended(){suspended=null;undoEntry=null;}
 document.addEventListener('click',event=>{
  if(event.target.closest?.('[data-inline-undo]')){event.preventDefault();void undo();return;}
  const action=event.target.closest?.('[data-inline-action]');if(action){event.preventDefault();if(action.dataset.inlineAction==='cancel')cancel();else void save();return;}
  const open=event.target.closest?.('[data-inline-cell]');
  if(open&&!event.target.closest?.('[data-workflow-account]')){
   if(draft&&String(open.dataset.inlineCell)===draft.id&&open.dataset.inlineField===draft.field)return;
   event.preventDefault();if(!draft)void begin(open.dataset.inlineCell,open.dataset.inlineField);else announceBlocked();return;
  }
  const navigation=event.target.closest?.('[data-page]');if(draft&&navigation&&suspendForNavigation(navigation.dataset.page))return;
  if(draft&&event.target.closest?.('a[href],button,input[type="submit"]')){event.preventDefault();event.stopImmediatePropagation?.();announceBlocked();}
 },true);
 document.addEventListener('input',event=>{if(event.target.id==='accountInlineInput')inputChanged();},true);
 document.addEventListener('change',event=>{if(event.target.id==='accountInlineInput')inputChanged();},true);
 document.addEventListener('compositionstart',event=>{if(event.target.id==='accountInlineInput')composing=true;},true);
 document.addEventListener('compositionend',event=>{if(event.target.id==='accountInlineInput')composing=false;},true);
 document.addEventListener('board-ready',refreshHint);
 document.addEventListener('DOMContentLoaded',refreshHint);
 document.addEventListener('focusout',event=>{
  if(painting)return;
  const fromEditor=event.target.id==='accountInlineInput'||!!event.target.closest?.('[data-inline-editor]');
  const stayingInside=!!event.relatedTarget?.closest?.('[data-inline-editor]');
  if(fromEditor&&!stayingInside&&!composing&&draft&&!['loading','saving','checking','unknown','conflict'].includes(draft.phase))void save();
 },true);
 document.addEventListener('keydown',event=>{
  if(event.target.id==='accountInlineInput'){
   if(composing||event.isComposing||event.keyCode===229)return;
   if(event.key==='Escape'){event.preventDefault();cancel();}
   else if(event.key==='Enter'&&(draft?.field!=='note'||event.ctrlKey||event.metaKey)){event.preventDefault();void save();}
  }else if((event.key==='Enter'||event.key===' ')&&event.target.closest?.('[data-inline-account]')){event.preventDefault();const open=event.target.closest('[data-inline-cell]');if(open)void begin(open.dataset.inlineCell,open.dataset.inlineField);}
 },true);
 window.addEventListener('beforeunload',event=>{if(draft||suspended){event.preventDefault();event.returnValue='';}});
 function acceptBoard(changes){
  let statusUpdated=false;
  if(!changes){statusUpdated=[...confirmed.keys()].some(key=>JSON.parse(key)[2]==='status');confirmed.clear();}
  else for(const change of changes)if(String(change.entity||'').startsWith('account:'))for(const field of Object.keys(labels)){const removed=confirmed.delete(confirmedKey(state()?.user?.id,String(change.entity).slice(8),field));if(removed&&field==='status')statusUpdated=true;}
  if(statusUpdated&&!draft)hint('账号状态、筛选结果和统计已更新。');
 }
 return Object.freeze({cell,begin,save,cancel,hasDraft,announceBlocked,inputChanged,acceptBoard,refreshHint,undo,suspendForNavigation,restoreSuspended,clearSuspended,getUndo:()=>validUndo()?{id:undoEntry.id,field:undoEntry.field,until:undoEntry.until}:null,getState:()=>draft?{id:draft.id,field:draft.field,value:draft.value,phase:draft.phase,requestId:draft.payload?.request_id}:null});
})();
