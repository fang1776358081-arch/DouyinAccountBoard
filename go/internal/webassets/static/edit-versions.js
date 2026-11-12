const capturedVersions=new Map();
function workflowRelations(state){
 const workflow=state?.device_workflow||{},units=new Map(),access=new Map(),placements=new Map();
 for(const unit of workflow.units||[])units.set(String(unit.id),unit);
 for(const device of state?.phone_devices?.devices||[])for(const unit of [...(device.unit_history||[]),...(device.current_unit?[device.current_unit]:[])])if(!units.has(String(unit.id)))units.set(String(unit.id),unit);
 for(const row of workflow.access||[])access.set(String(row.account_id),row);
 for(const account of state?.accounts||[])if(account.device_access&&!access.has(String(account.id)))access.set(String(account.id),{account_id:account.id,...account.device_access});
 for(const row of workflow.placements||[])placements.set(String(row.card_id),row);
 for(const card of state?.phone_cards?.cards||[])if(card.placement&&!placements.has(String(card.id)))placements.set(String(card.id),{card_id:card.id,...card.placement});
 const current=new Map();for(const unit of units.values())if(unit.active)current.set(String(unit.device_id),unit);
 return {units,access,placements,current,cases:new Map((workflow.cases||[]).map(row=>[String(row.id),row])),accounts:new Map((state?.accounts||[]).map(row=>[String(row.id),row]))};
}
function workflowVersionKeys(body,state){
 const relations=workflowRelations(state),devices=new Set(),units=new Set(),accounts=new Set(),cards=new Set(),cases=new Set();
 const add=(set,value)=>{if(value!==null&&value!==undefined&&String(value)!=='')set.add(String(value));};
 const addDevice=(value,withCurrent=false)=>{add(devices,value);if(withCurrent)add(units,relations.current.get(String(value))?.id);};
 // Match the server's reference expansion in stages. Original-number references
 // are locks only: expanding their replacement phones would pull unrelated data.
 if(body.action!=='reassign_unit'&&body.device_id)addDevice(body.device_id,true);
 for(const field of ['unit_id','target_unit_id'])add(units,body[field]);
 add(cases,body.case_id);add(accounts,body.account_id);add(cards,body.card_id);
 for(const id of body.affected_account_ids||[])add(accounts,id);
 for(const id of [...accounts]){
  const row=relations.access.get(id);
  if(row){add(units,row.last_unit_id);add(units,row.target_unit_id);add(cases,row.case_id);}
  else{const account=relations.accounts.get(id);if(account?.phone_device_id)addDevice(account.phone_device_id,true);}
 }
 for(const id of [...cards])add(units,relations.placements.get(id)?.unit_id);
 for(const id of [...cases]){const row=relations.cases.get(id);if(row){add(units,row.unit_id);for(const accountId of row.account_ids||[])add(accounts,accountId);}}
 for(const id of units){const row=relations.units.get(id);if(row){addDevice(row.device_id);addDevice(row.original_device_id);}}
 for(const row of relations.placements.values())if(units.has(String(row.unit_id)))add(cards,row.card_id);
 for(const row of relations.access.values())if(units.has(String(row.last_unit_id))||units.has(String(row.target_unit_id)))add(accounts,row.account_id);
 // Include cases on the touched physical phones without following their moved
 // accounts to other phones. This is a bounded conservative superset.
 for(const row of relations.cases.values())if(units.has(String(row.unit_id))){add(cases,row.id);for(const accountId of row.account_ids||[])add(accounts,accountId);}
 if(body.action==='reassign_unit')addDevice(body.device_id);
 const keys=[];
 for(const id of devices)keys.push('device:'+id);
 for(const id of units)keys.push('unit:'+id);
 for(const id of cases)keys.push('case:'+id);
 for(const id of accounts){keys.push('account:'+id,'access:'+id);if(body.pause_nurture===true)keys.push('nurture:'+id);}
 for(const id of cards)keys.push('card:'+id,'placement:'+id);
 return keys;
}
function legacyDeviceVersionKeys(path,body,accounts,state){
 if(!state?.device_workflow?.enabled)return [];
 const relations=workflowRelations(state),keys=new Set();
 const addUnit=id=>{if(!id)return;keys.add('unit:'+id);const unit=relations.units.get(String(id));for(const value of [unit?.device_id,unit?.original_device_id])if(value)keys.add('device:'+value);};
 if(path==='/api/phone-device')addUnit(relations.current.get(String(body.id))?.id);
 if(path==='/api/batch-edit'&&body.kind==='devices'&&'state' in (body.patch||{}))for(const id of body.ids||[])addUnit(relations.current.get(String(id))?.id);
 if(['/api/account','/api/account/create','/api/account/phone-device'].includes(path)&&'phone_device_id' in body){
  const id=String(body.id??''),old=accounts.find(account=>String(account.id)===id),target=String(body.phone_device_id||'');
  if(String(old?.phone_device_id||'')!==target){
   if(id){keys.add('access:'+id);const access=relations.access.get(id);addUnit(access?.last_unit_id);addUnit(access?.target_unit_id);if(access?.case_id)keys.add('case:'+access.case_id);}
   for(const deviceId of [old?.phone_device_id,target])if(deviceId)addUnit(relations.current.get(String(deviceId))?.id);
  }
 }
 return [...keys];
}
function versionKeys(path,body,accounts,state){
 if(path==='/api/device-workflow')return workflowVersionKeys(body,state);
 if(path==='/api/account/patch'){
  const changes=body.changes||{},id=String(body.id??''),old=accounts.find(account=>account.id===id);
  const keys=versionKeys('/api/account',{...changes,id},accounts,state);
  for(const [field,prefix] of [['computer_id','computer:'],['parent_account_id','account:']])if(field in changes||field==='computer_id'&&'slot' in changes)for(const value of [old?.[field],field in changes?changes[field]:old?.[field]])if(value&&value!=='unassigned')keys.push(prefix+value);
  return [...new Set(keys)];
 }
 const id=String(body.id??''),keys=[];
 if(path==='/api/batch-edit'){const prefix={cards:'card',devices:'device'}[body.kind]||'account';for(const value of body.ids||[]){keys.push(prefix+':'+value);if(body.kind==='verification')keys.push('nurture:'+value);if(body.kind==='daily')keys.push('daily:'+value+':'+body.patch.date);}return [...new Set([...keys,...legacyDeviceVersionKeys(path,body,accounts,state)])];}
 if(path.startsWith('/api/workspace/nurture/'))keys.push('account:'+id,'nurture:'+id);
 if(['/api/account','/api/account/status','/api/account/card-readiness','/api/lifecycle-status','/api/account/phone-device','/api/account/nurture'].includes(path))keys.push('account:'+id);
 if(path==='/api/bulk-status')keys.push(...(body.ids||[]).map(id=>'account:'+id));
 if(path==='/api/daily')keys.push('account:'+id,'daily:'+id+':'+body.date);
 if(path==='/api/phone-card'&&body.id)keys.push('card:'+id);
 if(path==='/api/phone-device')keys.push('device:'+id);
 if(['/api/account','/api/account/create','/api/account/phone-device'].includes(path)&&'phone_device_id' in body){const old=accounts.find(account=>account.id===id);for(const value of [old?.phone_device_id,body.phone_device_id])if(value)keys.push('device:'+value);}
 if(path==='/api/publishing/assignment')for(const [computer,slot] of [[body.computer_id,body.slot],[body.target_computer,body.target_slot]])if(computer!=null&&slot!=null)keys.push('layout:'+body.batch+':'+computer+':'+slot);
 return [...new Set([...keys,...legacyDeviceVersionKeys(path,body,accounts,state)])];
}
function versionPayload(path,body,state,source){return {...body,_expected_versions:Object.fromEntries(versionKeys(path,body,state.accounts,state).map(key=>[key,(source||state.record_versions||{})[key]??null]))};}
function captureVersions(id){capturedVersions.set(id,{...(data.record_versions||{})});}
function activeEditVersions(){for(const id of ['deviceWorkflowDialog','editor','cardEditor','phoneDeviceEditor','publishingMatchDialog','verificationEditor','batchEditor'])if($(id)?.open&&capturedVersions.has(id))return capturedVersions.get(id);return null;}
function applyVersionDelta(delta,state){if(!delta)return;state.record_versions={...state.record_versions,...delta};for(const snapshot of capturedVersions.values())Object.assign(snapshot,delta);}
