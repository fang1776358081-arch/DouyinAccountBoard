function probeBoardUpdates(){return fetch('/api/board-updates?version=0');}
function applyRemoteBoardUpdate(update){
 if(!update||typeof data==='undefined'||!data)return;
 if(update.removed!=null){
  const removedAt=data.accounts.findIndex(item=>String(item.id)===String(update.removed));
  if(removedAt>=0)data.accounts.splice(removedAt,1);
 }
 const account=update.account;
 if(!account||account.id==null)return;
 const index=data.accounts.findIndex(item=>item.id===account.id);
 if(index>=0)data.accounts[index]=account;
 else data.accounts.push(account);
 if(update.record_version){
  const versions={...(data.record_versions||{})};
  versions['account:'+account.id]=update.record_version;
  data.record_versions=versions;
 }
}
function boardData(){if(typeof data!=='undefined'&&data)return data;return globalThis.data;}
function boardEditorOpen(){const lookup=typeof $==='function'?$:id=>document.getElementById(id);return !!globalThis.DeviceWorkflow?.isOpen()||['editor','cardEditor','phoneDeviceEditor','publishingMatchDialog','verificationEditor','batchEditor'].some(id=>lookup(id)?.open);}
function rememberBoardVersion(entity,version){const state=boardData();if(!entity||version==null||!state)return;state.record_versions={...(state.record_versions||{}),[entity]:version};}
function applyListedChange(list,id,after,removed){if(!Array.isArray(list))return;const index=list.findIndex(item=>String(item.id)===String(id));if(removed){if(index>=0)list.splice(index,1);return;}if(!after)return;if(index>=0){const row=list[index];for(const key of Object.keys(after))if(key in row)row[key]=after[key];}else list.push(after);}
function applyCaughtChange(change){
 const state=boardData();
 if(!change||!state)return;
 const entity=String(change.entity||''),after=change.after&&typeof change.after==='object'?change.after:null,removed=change.kind==='remove'||after==null;
 if(entity.startsWith('account:')){
  const id=entity.slice('account:'.length),index=(state.accounts||[]).findIndex(item=>String(item.id)===id);
  if(removed){if(index>=0)state.accounts.splice(index,1);}
  else if(index>=0){const row=state.accounts[index];for(const key of Object.keys(after))if(key in row)row[key]=after[key];}
  else state.accounts.push({...after,published:Object.prototype.hasOwnProperty.call(after,'published')?after.published:false});
 }else if(entity.startsWith('daily:')){
  const rest=entity.slice('daily:'.length),split=rest.lastIndexOf(':'),id=rest.slice(0,split),day=rest.slice(split+1);
  if(after&&day===state.date){const row=(state.accounts||[]).find(item=>String(item.id)===id);if(row)row.published=!!after.published;}
 }else if(entity.startsWith('card:'))applyListedChange(state.phone_cards&&state.phone_cards.cards,entity.slice('card:'.length),after,removed);
 else if(entity.startsWith('device:'))applyListedChange(state.phone_devices&&state.phone_devices.devices,entity.slice('device:'.length),after,removed);
 else if(entity.startsWith('nurture:')){
  const records=state.nurture&&state.nurture.records,id=entity.slice('nurture:'.length);
  if(Array.isArray(records)){const index=records.findIndex(item=>String(item.account_id)===id);if(removed){if(index>=0)records.splice(index,1);}else if(after&&index>=0){const row=records[index];for(const key of Object.keys(after))if(key in row)row[key]=after[key];}else if(after)records.push(after);}
 }else if(entity.startsWith('layout:')){
  const rest=entity.slice('layout:'.length),slotAt=rest.lastIndexOf(':'),batchAt=rest.indexOf(':');
  if(slotAt>batchAt&&state.publishing_layout&&state.publishing_layout.batch===rest.slice(0,batchAt)){
   const computer=rest.slice(batchAt+1,slotAt),slot=Number(rest.slice(slotAt+1)),cells=state.publishing_layout.cells||[];
   const index=cells.findIndex(item=>String(item.computer_id)===computer&&Number(item.slot)===slot);
   if(removed){if(index>=0)cells.splice(index,1);}else if(after&&index>=0){const row=cells[index];for(const key of Object.keys(after))if(key in row)row[key]=after[key];}
  }
 }
 rememberBoardVersion(entity,change.record_version);
}
function boardSerial(){if(typeof requestSerial==='number')return requestSerial;if(typeof globalThis.requestSerial==='number')return globalThis.requestSerial;return 0;}
let catchUpFlight=null;
async function catchUpBoard(){
 const state=boardData();
 if(!state||!Number.isInteger(state.cursor))return;
 if(catchUpFlight)return catchUpFlight;
 const requested=state.cursor,serial=boardSerial();
 catchUpFlight=(async()=>{
  let response;try{response=await fetch('/api/board-updates?cursor='+requested);}catch(error){return;}
  if(boardSerial()!==serial)return;
  if(response.status===401){if(typeof clearBoardCache==='function')clearBoardCache();location.replace(location.search==='?office=three'?'/login.html?next='+encodeURIComponent('/?office=three'):'/login.html');return;}
  if(response.status===404){if(!boardEditorOpen()&&typeof load==='function')await load();return;}
  const payload=await response.json().catch(()=>null);
  if(!response.ok||!payload||boardSerial()!==serial)return;
  if(payload.reload||(payload.changes||[]).some(change=>/^(unit|placement|access|case|workflow_event):/.test(String(change?.entity||'')))){if(!boardEditorOpen()&&typeof load==='function')await load();return;}
  for(const change of payload.changes||[])applyCaughtChange(change);
  const latest=boardData();
  if(Number.isInteger(payload.cursor)&&latest&&latest.cursor===requested)latest.cursor=payload.cursor;
  if((payload.changes||[]).length&&typeof render==='function')render();
  if(typeof writeBoardCache==='function')writeBoardCache();
 })().finally(()=>{catchUpFlight=null;});
 return catchUpFlight;
}
