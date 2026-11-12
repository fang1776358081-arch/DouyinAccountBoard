/* User-facing account identity; database keys remain private to associations. */
function identityBoard(){return typeof data==='undefined'?null:data;}
function deviceNumber(device){
  const value=String(device?.number??device?.id?.split(':').pop()??'');
  return /^[0-9]{1,6}$/.test(value)&&Number(value)>0?String(Number(value)):'';
}
function deviceOfficeText(device,board=identityBoard()){
  if(Array.isArray(device?.office_ids)){
    const ids=device.office_ids;
    if(!ids.length)return '办公室待核对';
    const names=ids.map(id=>(board?.offices||[]).find(office=>office.id===id)?.name||'其他办公室');
    return names.join('、')+(ids.length>1?'（旧归属待核对）':'');
  }
  return board?.user?.scope_mode?'办公室待核对':device?.id?.startsWith('office3:')?'办公室三':'原有批次';
}
function deviceDisplay(device,board=identityBoard()){
  if(!device)return '手机设备待核对';
  const number=deviceNumber(device),name=device.name||'',base=deviceOfficeText(device,board)+' · '+(number?number+'号手机':'手机编号待核对');
  return base+(name&&name!==base&&name!==number+'号手机'?' · '+name:'');
}
function unitDisplay(unit,board=identityBoard()){
  if(!unit)return '实物手机待确认';
  const devices=board?.phone_devices?.devices||[],device=devices.find(row=>row.id===unit.device_id);
  if(!device)return unit.label||unit.device_name||'实物手机待确认';
  const original=devices.find(row=>row.id===unit.original_device_id);
  return deviceDisplay(device,board)+' · 第'+(unit.generation||1)+'台实物'+(!unit.active?' · 旧机':'')+(original&&original.id!==device.id?'（原'+deviceDisplay(original,board)+'）':'');
}
function accountDeviceDisplay(account,board=identityBoard()){
  const unit=(board?.device_workflow?.units||[]).find(row=>String(row.id)===String(account.device_access?.last_unit_id));
  if(unit)return unitDisplay(unit,board);
  const device=(board?.phone_devices?.devices||[]).find(row=>row.id===account.phone_device_id);
  return device?deviceDisplay(device,board):account.phone_device_name||'';
}
function accountDisplay(account){
  return [account.name||'未命名账号',account.douyin_id?'抖音号 '+account.douyin_id:'抖音号待补充',accountDeviceDisplay(account)].filter(Boolean).join(' · ');
}
