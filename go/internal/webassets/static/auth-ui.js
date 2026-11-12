let currentUser=null;
function hasPermission(key){return !!(data?.user||currentUser)?.permissions.includes(key);}
function applyPermissions(){
 currentUser=data.user;
 $('currentUser').textContent=currentUser?`${currentUser.name} · ${{owner:'总管理员',admin:'管理员',employee:'员工',viewer:'只读人员'}[currentUser.role]}`:'';
 $('manageUsers').hidden=currentUser?.role!=='owner';$('changeOwnUsername').hidden=currentUser?.role!=='owner';
 for(const [id,key] of [['newAccount','account_create'],['newCard','card_create'],['newPhoneDevice','device_create']])$(id).hidden=!hasPermission(key);
 for(const [id,key] of [['applyBulk','account_status'],['bulkStatus','account_status']])$(id).disabled=!hasPermission(key);
 $('importCards').disabled=!hasPermission('card_import')||!data.phone_cards?.importable;
 for(const button of document.querySelectorAll('[data-publish]'))button.disabled=!hasPermission('publish');
 applyCollectionPermissions();
 if(!hasPermission('account_progress'))for(const select of document.querySelectorAll('[data-aux-status],[data-readiness-id]'))select.disabled=true;
}
function collectionSubmitReason(){
 const collection=data?.collection;
 return !hasPermission('collection_manage')?'需要作品采集管理权限':collection?.enabled!==true?'采集暂未启用；历史记录保留':collection.paused?'采集队列已暂停':!collection.worker_online?'本机采集器离线':'';
}
function applyCollectionPermissions(){
 const permitted=hasPermission('collection_manage');
 const submitReason=collectionSubmitReason();
 const detailReason=$('accountPilotCollectionReason');
 if(detailReason)detailReason.textContent=submitReason||'手动采集公开作品数量';
 for(const button of [$('collectSelected'),$('collectVisible'),...document.querySelectorAll('[data-collect]')]){
   button.disabled=!!submitReason;button.title=submitReason;
 }
 $('autoCollect').closest('label').hidden=true;$('autoCollect').checked=false;$('autoCollect').disabled=true;
 for(const id of ['pauseCollect','cancelCollect','resumeCollect','openCollector']){$(id).hidden=true;$(id).disabled=true;}
 if($('collectionQueueLink'))$('collectionQueueLink').hidden=!permitted;
}
function applyAccountPermissions(account){
 const manage=hasPermission(account.id?'account_manage':'account_create'),status=hasPermission('account_status'),progress=hasPermission('account_progress');
 for(const field of document.querySelectorAll('#editForm input,#editForm select,#editForm textarea'))field.disabled=!manage;
 $('editStatus').disabled=!status;
 for(const id of ['editCardStability','editPasswordState'])$(id).disabled=!progress;
 $('editAux').disabled=!progress||(!manage&&account.account_type!=='大号')||!!account.child_accounts?.length;
 if(!hasPermission('device_manage'))$('editPhoneDevice').disabled=true;
 document.querySelector('#editForm button[type="submit"]').hidden=!(manage||status||progress);
}
function applyCardPermissions(){
 const creating=!$('cardId').value,manage=hasPermission(creating?'card_create':'card_manage'),tasks=hasPermission('card_tasks')||(creating&&manage);
 for(const field of document.querySelectorAll('#cardForm input,#cardForm select,#cardForm textarea'))field.disabled=!manage;
 for(const id of ['cardEditStatus','cardCodeState','cardCodeNote','cardReturnState','cardReturnDue','cardReturnedOn'])$(id).disabled=!tasks;
 for(const field of document.querySelectorAll('[data-readiness-id]'))field.disabled=!hasPermission('account_progress');
 document.querySelector('#cardForm button[type="submit"]').hidden=!(manage||tasks);
}
function applyDevicePermissions(){
 const manage=hasPermission($('deviceIsNew').value?'device_create':'device_manage');
 for(const field of document.querySelectorAll('#phoneDeviceForm input,#phoneDeviceForm select,#phoneDeviceForm textarea'))field.disabled=!manage;
 if($('deviceKey').value)$('deviceGroup').disabled=true;
 for(const button of document.querySelectorAll('#phoneDeviceForm [data-device-remove],#assignPhoneAccount,#phoneDeviceForm button[type="submit"]'))button.disabled=!manage;
}
document.addEventListener('DOMContentLoaded',()=>{
 let credentialPending=false;
 const passwordSubmit=$('passwordForm').querySelector('button[type="submit"]'),usernameSubmit=$('usernameForm').querySelector('button[type="submit"]');
 const setCredentialPending=value=>{
  credentialPending=value;
  for(const button of [passwordSubmit,usernameSubmit,$('closePassword'),$('closeUsername'),$('changeOwnPassword'),$('changeOwnUsername')])button.disabled=value;
 };
 for(const [dialogId,formId,closeId,errorId] of [['passwordDialog','passwordForm','closePassword','passwordError'],['usernameDialog','usernameForm','closeUsername','usernameError']]){
  const reset=()=>{$(formId).reset();$(errorId).textContent='';};
  $(closeId).onclick=()=>{if(credentialPending)return;reset();$(dialogId).close();};
  $(dialogId).addEventListener('cancel',event=>{if(credentialPending)event.preventDefault();else reset();});
  $(dialogId).addEventListener('close',reset);
 }
 $('manageUsers').onclick=()=>{location.href='/users.html';};
 $('logout').onclick=async()=>{try{if(typeof clearBoardCache==='function')clearBoardCache();await api('/api/auth/logout',{});location.replace('/login.html');}catch(error){toast(error.message);}};
 $('changeOwnPassword').onclick=()=>{if(credentialPending)return;$('passwordForm').reset();$('passwordError').textContent='';$('passwordDialog').showModal();};
 $('changeOwnUsername').onclick=()=>{
  if(credentialPending||(data?.user||currentUser)?.role!=='owner')return;
  $('usernameForm').reset();$('currentUsername').value=(data?.user||currentUser).username;$('usernameError').textContent='';$('usernameDialog').showModal();
 };
 $('passwordForm').onsubmit=async event=>{
  event.preventDefault();if(credentialPending)return;
  if($('newPassword').value!==$('confirmPassword').value){$('passwordError').textContent='两次新密码不一致';return;}
  setCredentialPending(true);$('passwordError').textContent='';
  try{await api('/api/auth/password',{old_password:$('oldPassword').value,password:$('newPassword').value});$('passwordForm').reset();$('passwordDialog').close();await load();toast('密码已更新，其他设备的旧登录已失效');}catch(error){$('passwordError').textContent=error.message;}finally{setCredentialPending(false);}
 };
 $('usernameForm').onsubmit=async event=>{
  event.preventDefault();if(credentialPending)return;
  const user=data?.user||currentUser,username=$('newUsername').value.trim();
  if(user?.role!=='owner'){$('usernameError').textContent='仅总管理员可修改本人的登录名';return;}
  if(!/^[A-Za-z0-9_.-]{3,40}$/.test(username)){$('usernameError').textContent='登录名请使用3至40位字母、数字、点、横线或下划线';return;}
  if(username===user.username){$('usernameError').textContent='新登录名与当前登录名相同';return;}
  if(!$('usernamePassword').value){$('usernameError').textContent='请输入当前密码确认';return;}
  setCredentialPending(true);$('usernameError').textContent='';
  try{
   await api('/api/auth/username',{username,current_password:$('usernamePassword').value});
   $('usernameForm').reset();$('passwordForm').reset();$('usernameDialog').close();
   if(typeof clearBoardCache==='function')clearBoardCache();
   location.replace('/login.html');
  }catch(error){
   $('usernamePassword').value='';
   $('usernameError').textContent=error.status===undefined?'请求结果未确认：请先尝试用新登录名和原密码重新登录；不要重复保存。':error.message;
  }
  finally{setCredentialPending(false);}
 };
});

document.addEventListener('invalid',event=>{
 const section=event.target.closest?.('[data-editor-extra]');
 if(section)section.open=true;
},true);
document.addEventListener('close',event=>{
 event.target.querySelectorAll?.('details[data-editor-extra][open]').forEach(section=>{section.open=false;});
},true);
