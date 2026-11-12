function probeBoardUpdates(){return fetch('/api/board-updates?version=0');}
function applyRemoteBoardUpdate(update){
 if(!update||typeof data==='undefined'||!data)return;
 if(update.removed!=null){const at=data.accounts.findIndex(item=>String(item.id)===String(update.removed));if(at>=0)data.accounts.splice(at,1);}
 const account=update.account;if(!account||account.id==null)return;
 const at=data.accounts.findIndex(item=>item.id===account.id);if(at>=0)data.accounts[at]=account;else data.accounts.push(account);
 if(update.record_version)data.record_versions={...(data.record_versions||{}),['account:'+account.id]:update.record_version};
}
function boardData(){if(typeof data!=='undefined'&&data)return data;return globalThis.data;}
function boardPilotScope(){return typeof activeOffice!=='undefined'&&activeOffice==='three';}
function boardEditorOpen(){
 const lookup=typeof $==='function'?$:id=>document.getElementById(id);
 return !!globalThis.DeviceWorkflow?.isOpen()||document.querySelector?.('dialog[open]')?.open===true||['editor','cardEditor','phoneDeviceEditor','publishingMatchDialog','verificationEditor','batchEditor'].some(id=>lookup(id)?.open);
}
let boardBackgroundReads=0;
function boardReadControl(active){
 if(!active)return false;
 if(active.matches?.('.toolbar input, .toolbar select, .header-actions input, .header-actions select, [data-select], [data-batch-pick]'))return true;
 const fields=[...(typeof accountSearchFields==='undefined'?[]:accountSearchFields),...(typeof cardSearchFields==='undefined'?[]:cardSearchFields),...(typeof deviceSearchFields==='undefined'?[]:deviceSearchFields),'date','computer','status','published','accountType','ageFilter','lifecycleFilter','loginPhoneFilter','verificationFilter','selectAll','cardOfficeFilter','cardStatus','cardCarrier','cardLinked','cardExpiry','cardTask','cardAssessment','cardCodeFilter','cardReturnFilter','cardPositionFilter','cardLinkedStateFilter','cardSourceFilter','deviceStateFilter','deviceOccupancyFilter','pendingReviewOffice','pendingReviewReason','pendingReviewCell'];
 return fields.includes(active.id)||active.type==='checkbox'&&(active.dataset?.select!==undefined||active.dataset?.batchPick!==undefined);
}
function boardSyncBlocked(ignoreRead=false){
 const active=document.activeElement,typing=active&&['INPUT','TEXTAREA','SELECT'].includes(active.tagName)&&!boardReadControl(active);
 return !!globalThis.AccountInline?.hasDraft()||!!globalThis.CardInline?.hasDraft()||!!globalThis.DeviceInline?.hasDraft()||boardEditorOpen()||!!typing||typeof boardWrites!=='undefined'&&boardWrites>0||typeof dailyRequests!=='undefined'&&dailyRequests.size>0||typeof pendingRequestIds!=='undefined'&&pendingRequestIds.size>0||!ignoreRead&&!boardBackgroundReads&&typeof dailyViewLoading!=='undefined'&&!!dailyViewLoading;
}
function boardSerial(){if(typeof requestSerial==='number')return requestSerial;return Number(globalThis.requestSerial)||0;}
function boardEpoch(){return typeof boardMutationEpoch==='number'?boardMutationEpoch:0;}
function rememberBoardVersion(entity,version){const state=boardData();if(entity&&version!=null&&state)state.record_versions={...(state.record_versions||{}),[entity]:version};}
function sameBoardValue(left,right){return JSON.stringify(left)===JSON.stringify(right);}
function boardChangeMode(change,state){
 if(!change||typeof change.entity!=='string')return 'reload';
 const entity=change.entity,after=change.after;
 if(entity.startsWith('account-source:'))return after&&after.account_id===entity.slice(15)?'source':'reload';
 if(entity.startsWith('daily:')){
  const rest=entity.slice(6),split=rest.lastIndexOf(':'),id=rest.slice(0,split),day=rest.slice(split+1);
  if(split<1||!/^\d{4}-\d{2}-\d{2}$/.test(day))return 'reload';
  if(day!==state.date)return 'skip';
  return after&&[true,false,0,1].includes(after.published)&&(state.accounts||[]).some(row=>String(row.id)===id)?'patch':'reload';
 }
 // Only scalar display edits can bypass recomputing derived office, assignment,
 // lifecycle and physical-device relations from an authorized snapshot.
 const kinds=[['account:',state.accounts,new Set(['name','note'])],['card:',state.phone_cards?.cards,new Set(['note'])],['device:',state.phone_devices?.devices,new Set(['note'])]];
 for(const [prefix,list,allowed] of kinds)if(entity.startsWith(prefix)){
  const row=(list||[]).find(item=>String(item.id)===entity.slice(prefix.length));
  if(!row||!after||typeof after!=='object'||Array.isArray(after)||change.kind==='remove')return 'reload';
  if(after.id!=null&&String(after.id)!==String(row.id))return 'reload';
  for(const key of Object.keys(after)){
   if(key==='id'||key==='updated_at'||key==='created_at'||sameBoardValue(row[key],after[key]))continue;
   if(!allowed.has(key))return 'reload';
  }
  // Parent/child labels are embedded on related accounts in existing snapshots.
  if(prefix==='account:'&&!sameBoardValue(row.name,after.name)&&after.name!==undefined&&(row.parent_account_id||(row.child_accounts||[]).length||(state.accounts||[]).some(item=>String(item.parent_account_id||'')===String(row.id))))return 'reload';
  return 'patch';
 }
 return 'reload';
}
function applyCaughtChange(change){
 const state=boardData(),entity=change.entity,after=change.after;if(!state||!after)return;
 if(entity.startsWith('daily:')){
  const rest=entity.slice(6),split=rest.lastIndexOf(':'),id=rest.slice(0,split);
  if(rest.slice(split+1)!==state.date)return;
  const row=state.accounts.find(item=>String(item.id)===id);if(row)row.published=!!after.published;
 }else{
  const [prefix,list]=entity.startsWith('account:')?['account:',state.accounts]:entity.startsWith('card:')?['card:',state.phone_cards?.cards]:['device:',state.phone_devices?.devices];
  const row=(list||[]).find(item=>String(item.id)===entity.slice(prefix.length));
  if(row)for(const key of Object.keys(after))if(key!=='id'&&(key in row||['name','note','label'].includes(key)))row[key]=after[key];
 }
 // Remote notices must never advance the lock snapshot captured by an editor.
 rememberBoardVersion(entity,change.record_version);
}
function boardSyncFailure(status,message){const error=Error(message);if(status!==undefined)error.status=status;return error;}
let catchUpFlight=null,lastLegacyBoardRead=null;
function watchBoardFlight(flight,signal){
 if(!signal)return;
 const cancel=()=>{flight.cancelled=true;if(catchUpFlight===flight)catchUpFlight=null;};
 flight.cleanups.push(()=>signal.removeEventListener('abort',cancel));
 if(signal.aborted)cancel();else signal.addEventListener('abort',cancel,{once:true});
}
async function catchUpBoard(options={}){
 const state=boardData();if(!state||!Number.isInteger(state.cursor))return {status:'waiting'};
 if(options.signal?.aborted)return {status:'waiting'};
 if(catchUpFlight){watchBoardFlight(catchUpFlight,options.signal);return catchUpFlight.promise;}
 if(boardSyncBlocked())return {status:'deferred'};
 const view=typeof page==='undefined'?'board':page;
 const pilot=boardPilotScope();
 const result=(status,extra={})=>({status,...extra,...(pilot?{nextDelayMs:12000}:view==='accounts'&&['waiting','deferred'].includes(status)?{nextDelayMs:250}:{})});
 const requested=state.cursor,serial=boardSerial(),epoch=boardEpoch(),day=state.date,flight={cancelled:false,cleanups:[],promise:null};
 const active=()=>!flight.cancelled&&!options.signal?.aborted&&(!options.isCurrent||options.isCurrent());
 const isCurrent=()=>active()&&boardData()===state&&boardSerial()===serial&&boardEpoch()===epoch&&state.cursor===requested&&state.date===day&&(typeof page==='undefined'||page===view)&&!boardSyncBlocked();
 async function reload(){
  if(!isCurrent()||typeof load!=='function')return result(boardSyncBlocked()?'deferred':'waiting');
  boardBackgroundReads++;
  try{return result(await load(false,{background:true,signal:options.signal,isCurrent:active})?'updated':boardSyncBlocked()?'deferred':'waiting',{changes:1});}
  finally{boardBackgroundReads--;}
 }
 catchUpFlight=flight;watchBoardFlight(flight,options.signal);
 flight.promise=(async()=>{
  const response=await fetch('/api/board-updates?cursor='+requested+'&limit=200'+(view==='accounts'&&!pilot?'&wait=5':''),options.signal?{signal:options.signal}:undefined);
  if(response.status===401){if(typeof clearBoardCache==='function')clearBoardCache();location.replace(location.search==='?office=three'?'/login.html?next='+encodeURIComponent('/?office=three'):'/login.html');throw boardSyncFailure(401,'登录已失效，请重新登录');}
  if(!response.ok&&response.status!==404)throw boardSyncFailure(response.status,'读取更新失败');
  if(!isCurrent())return result(boardSyncBlocked()?'deferred':'waiting');
  if(response.status===404){
   // Old servers get a slow bounded fallback, never a five-second full read.
   if(lastLegacyBoardRead===null||Date.now()-lastLegacyBoardRead>=60000){const result=await reload();if(result.status==='updated')lastLegacyBoardRead=Date.now();}
   return {status:'unsupported'};
  }
  const payload=await response.json();
  if(!isCurrent())return result(boardSyncBlocked()?'deferred':'waiting');
  if(!payload||!Number.isInteger(payload.cursor)||payload.cursor<requested||payload.reload!==true&&!Array.isArray(payload.changes))throw boardSyncFailure(undefined,'更新响应格式不正确');
  if(payload.reload===true||payload.changes.some(change=>boardChangeMode(change,state)==='reload'))return reload();
  const changes=payload.changes.filter(change=>boardChangeMode(change,state)==='patch');
  if(payload.changes.some(change=>boardChangeMode(change,state)==='source'||change.entity.startsWith('account:'))){
   const sourceReady=await globalThis.AccountPilot?.refreshSources?.();
   if(sourceReady===false&&globalThis.AccountPilot?.enabled?.())throw boardSyncFailure(undefined,'来源字段同步失败，将保留同步记号后重试');
  }
  if(!isCurrent())return result(boardSyncBlocked()?'deferred':'waiting');
  for(const change of changes)applyCaughtChange(change);
  globalThis.AccountInline?.acceptBoard(changes);
  const cursorChanged=payload.cursor!==requested;state.cursor=payload.cursor;
  if(changes.length&&typeof render==='function')render();
  if((changes.length||cursorChanged)&&typeof writeBoardCache==='function')writeBoardCache();
  return {status:changes.length?'updated':'idle',changes:changes.length,...(pilot?{nextDelayMs:12000}:view==='accounts'&&payload.wait_supported===true?{nextDelayMs:250}:{})};
 })().finally(()=>{for(const cleanup of flight.cleanups)cleanup();if(catchUpFlight===flight)catchUpFlight=null;});
 return flight.promise;
}
function startBoardLiveSync(){
 if(typeof window==='undefined'||!window.LiveSync||window.boardLiveSync)return;
 const controller=window.LiveSync.create({ready:()=>!!boardData()&&Number.isInteger(boardData().cursor),blocked:()=>boardSyncBlocked(),run:options=>catchUpBoard(options),onState:state=>window.LiveSync.status('boardSyncState',state),intervalMs:boardPilotScope()?12000:5000});
 window.boardLiveSync=controller;const retry=document.getElementById('boardSyncRetry');if(retry)retry.onclick=()=>controller.poke();
 controller.start();
}
startBoardLiveSync();
