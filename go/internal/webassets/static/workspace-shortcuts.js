document.addEventListener('DOMContentLoaded',()=>{
 $('exportVisibleAccounts').onclick=async()=>{const button=$('exportVisibleAccounts');button.disabled=true;try{const result=await api('/api/workspace/export',{kind:'accounts',ids:filtered().map(account=>account.id),format:'xlsx',date:data.date}),bytes=Uint8Array.from(atob(result.content),value=>value.charCodeAt(0)),url=URL.createObjectURL(new Blob([bytes],{type:result.mime})),link=document.createElement('a');link.href=url;link.download=result.filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);}catch(error){toast(error.message);}finally{button.disabled=!hasPermission('data_export');}};
});
let openedWorkspaceLink=false;
const workspaceTodoPages={code:'cards',return:'cards',cancel:'cards',clean:'phones',publish:'board',abnormal:'board',match:'board',aux:'accounts',nurture_schedule:'accounts'};
const workspaceTodoCardFields={code:'cardCodeState',return:'cardReturnState',cancel:'cardEditStatus'};

function validWorkspaceWorkDate(day){
 if(!/^\d{4}-\d{2}-\d{2}$/.test(day||'')||day.startsWith('0000'))return false;
 const date=new Date(day+'T00:00:00Z');
 return !Number.isNaN(date.valueOf())&&date.toISOString().slice(0,10)===day;
}

function prepareWorkspaceTodoShortcut(){
 const params=new URLSearchParams(location.search),action=params.get('todo_action');
 if(!Object.hasOwn(workspaceTodoPages,action))return false;
 page=workspaceTodoPages[action];
 const date=params.get('work_date');
 if(page==='board'&&validWorkspaceWorkDate(date))$('date').value=date;
 return true;
}

function workspaceShortcutCanEdit(record,permission){
 if(!record||!hasPermission(permission))return false;
 if(!data.user?.scope_mode||data.user.scope_mode==='all')return true;
 const offices=record.office_ids;
 return !!offices?.length&&offices.every(id=>data.user.office_ids?.includes(id));
}

function showWorkspaceAccountShortcut(account,action){
 activeOffice='all';clearAccountSearch();page='accounts';render();
 if(window.AccountPilot?.openDetail){
  if(!AccountPilot.openDetail(account.id))return;
  if(action==='aux')$('accountPilotDetailDialog')?.querySelector('[data-aux-status]:not(:disabled)')?.focus();
 }else if(['account_manage','account_status','account_progress','device_manage'].some(hasPermission))edit(account.id);
}

function showWorkspaceCardShortcut(card,action){
 page='cards';render();
 const field=workspaceTodoCardFields[action];
 const pending=action==='code'?card.code_state==='待接码':action==='return'?card.return_state==='待归还':card.status==='待注销';
 if(field&&pending&&workspaceShortcutCanEdit(card,'card_tasks')){
  editCard(card.id);
  const control=$(field);
  const section=control?.closest('[data-editor-extra]');
  if(section)section.open=true;
  control?.focus();
 }else if(window.CardDetails)CardDetails.open(card.id);
 else if(workspaceShortcutCanEdit(card,'card_manage')||workspaceShortcutCanEdit(card,'card_tasks'))editCard(card.id);
}

function showWorkspaceDeviceShortcut(device,action){
 page='phones';render();
 if(action==='clean'&&device.state==='待清理'){
  const canManage=workspaceShortcutCanEdit(device,'device_manage');
  if(window.DeviceWorkflow?.enabled()){
   DeviceWorkflow.openDevice(device.id);
   const unit=device.current_unit;
   const original=unit?.original_device_id?(data.phone_devices?.devices||[]).find(row=>row.id===unit.original_device_id):device;
   if(canManage&&unit&&workspaceShortcutCanEdit(original,'device_manage')){
    const button=Array.from($('deviceWorkflowDialog')?.querySelectorAll('[data-workflow-status]')||[])
     .find(item=>item.dataset.workflowStatus===String(unit.id));
    button?.click();
   }
   return;
  }
  if(canManage){editPhoneDevice(device.id);$('deviceState')?.focus();return;}
 }
 if(window.DeviceDetails)DeviceDetails.open(device.id);
 else if(workspaceShortcutCanEdit(device,'device_manage'))editPhoneDevice(device.id);
}

function workspacePublishCell(computer,slot){
 return Array.from($('matrixBody').querySelectorAll('[data-publish-computer]')).find(cell=>cell.dataset.publishComputer===computer&&cell.dataset.publishSlot===slot);
}

function showWorkspacePublishingShortcut(account,action,date,computer,slot){
 if(!validWorkspaceWorkDate(date)||data.date!==date){toast('无法打开待办日期，请返回今日工作重新打开。');return;}
 activeOffice='all';clearAccountSearch();page='board';
 $('accountIdQuery').value=account.id;render();
 const cell=computer&&slot?workspacePublishCell(computer,slot):$('matrixBody');
 const control=Array.from(cell?.querySelectorAll('[data-publish]')||[]).find(button=>button.dataset.publish===account.id);
 if(!control){toast('该账号当前没有可查看的发布位置。');return;}
 const details=control.closest('.account-card')?.querySelector('.publish-cell-details');
 if(action==='abnormal'&&details)details.open=true;
 const exception=control.closest('.account-card')?.querySelector('[data-publish-exception-action]');
 const focus=action==='publish'&&!account.published&&!control.disabled?control:exception||details?.querySelector('summary')||control;
 focus.focus();
}

function showWorkspaceMatchShortcut(computer,rawSlot,todo=false,date=''){
 if(date&&(!validWorkspaceWorkDate(date)||data.date!==date)){toast('无法打开待办日期，请返回今日工作重新打开。');return;}
 const slot=/^[1-9]\d*$/.test(rawSlot||'')?Number(rawSlot):0;
 const layout=data.publishing_layout;
 const cell=(layout?.cells||[]).find(item=>item.computer_id===computer&&item.slot===slot);
 const visibleHost=data.computers.some(host=>host.id===computer);
 const pending=cell?.account_id===null&&!cell.manual_link;
 if(!layout?.enabled||!visibleHost||!cell?.label?.trim()||!(pending||!todo&&cell.manual_link)){
  toast('发布格已变化或不在当前授权范围，请返回今日工作核对。');return;
 }
 activeOffice='all';clearAccountSearch();page='board';render();
 if(hasPermission('publishing_manage'))openPublishingMatch(computer,slot);
 else workspacePublishCell(computer,String(slot))?.querySelector('.unresolved-card')?.focus();
}

document.addEventListener('board-ready',()=>{
 $('exportVisibleAccounts').disabled=!hasPermission('data_export');
 if(openedWorkspaceLink)return;openedWorkspaceLink=true;
 const params=new URLSearchParams(location.search),action=params.get('todo_action');
 const accountId=params.get('account_id'),cardId=params.get('card_id'),deviceId=params.get('device_id'),computer=params.get('match_computer');
 const account=data.accounts.find(item=>item.id===accountId),card=(data.phone_cards?.cards||[]).find(item=>String(item.id)===cardId),device=(data.phone_devices?.devices||[]).find(item=>item.id===deviceId);
 if(action!==null){
  if(Object.hasOwn(workspaceTodoCardFields,action)&&card)showWorkspaceCardShortcut(card,action);
  else if(action==='clean'&&device)showWorkspaceDeviceShortcut(device,action);
  else if(action==='match'&&computer)showWorkspaceMatchShortcut(computer,params.get('match_slot'),true,params.get('work_date'));
  else if((action==='publish'||action==='abnormal')&&account)showWorkspacePublishingShortcut(account,action,params.get('work_date'),params.get('target_computer'),params.get('target_slot'));
  else if((action==='aux'||action==='nurture_schedule')&&account)showWorkspaceAccountShortcut(account,action);
  else toast('此待办资料已变化或不在当前授权范围，请返回今日工作核对。');
  return;
 }
 if(account)showWorkspaceAccountShortcut(account);
 else if(card)showWorkspaceCardShortcut(card);
 else if(device)showWorkspaceDeviceShortcut(device);
 else if(computer)showWorkspaceMatchShortcut(computer,params.get('match_slot'));
});
