let activeOffice=new URLSearchParams(location.search).get('office')==='three'?'three':'one';
const legacyOfficeDefinitions=[
 {id:'one',name:'办公室一',range:'手机 1–300、701 及以上'},
 {id:'two',name:'办公室二',range:'手机 301–700'},
 {id:'three',name:'办公室三',range:'新表独立批次 · 保留原编号'},
 {id:'pending',name:'待分配',range:'登录手机未确认'},
 {id:'all',name:'全部办公室',range:'汇总查看'}
];
let officeDefinitions=legacyOfficeDefinitions;
function accountOffice(account){
 if(Array.isArray(account.office_ids))return account.office_ids[0]||'pending';
 const value=String(account.phone_device_id??'');
 if(/^office3:\d+$/.test(value))return 'three';
 if(!value&&account.office_group==='three')return 'three';
 if(!/^\d+$/.test(value)||!Number.isSafeInteger(Number(value))||Number(value)<1)return 'pending';
 const number=Number(value);
 return number>=301&&number<=700?'two':'one';
}
function officeAccounts(accounts=data.accounts){return accounts.filter(a=>activeOffice==='all'||accountOffice(a)===activeOffice);}
function officeFilterResources(resources,kind){
 if(activeOffice==='all')return resources;
 const accounts=kind==='computers'?officeAccounts():[];
 return resources.filter(resource=>{
  if(kind==='computers'&&resource.id==='unassigned')return accounts.some(a=>a.computer_id==='unassigned');
  if(data.user?.scope_mode)return Array.isArray(resource.office_ids)&&(activeOffice==='pending'?resource.office_ids.length===0:resource.office_ids.includes(activeOffice));
  if(kind==='devices')return accountOffice({phone_device_id:resource.id})===activeOffice;
  return accounts.some(a=>a.computer_id===resource.id)||(data.publishing_layout?.cells||[]).some(cell=>cell.computer_id===resource.id&&publishingCellOffice(cell)===activeOffice);
 });
}
function renderComputerFilter(){
 const select=$('computer'),value=select.value,computers=officeFilterResources(data.computers,'computers');
 select.innerHTML='<option value="">全部电脑</option>'+computers.map(c=>`<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');
 select.value=computers.some(c=>c.id===value)?value:'';
}
function officeName(){return officeDefinitions.find(o=>o.id===activeOffice)?.name||'全部办公室';}
function renderOffices(){
 const scoped=!!data.user?.scope_mode;
 officeDefinitions=scoped?[{id:'all',name:'全部授权办公室',range:'当前账号可访问的资料'},...(data.offices||[]).map(o=>({id:o.id,name:o.name,range:'明确分配的资料'})),...(data.user.scope_mode==='all'?[{id:'pending',name:'待分配',range:'尚未分配办公室'}]:[])]:legacyOfficeDefinitions;
 if(!officeDefinitions.some(o=>o.id===activeOffice))activeOffice='all';
 $('officeWorkspace').hidden=page!=='accounts'&&page!=='board';
 if($('officeWorkspace').hidden)return;
 $('officeTabs').innerHTML=officeDefinitions.map(o=>`<button type="button" data-office="${esc(o.id)}" aria-pressed="${activeOffice===o.id}" class="office-tab ${activeOffice===o.id?'active':''}"><strong>${esc(o.name)}<span>${data.accounts.filter(a=>o.id==='all'||accountOffice(a)===o.id).length}</span></strong><small>${esc(o.range)}</small></button>`).join('');
 $('officeHint').textContent=scoped?'按资料明确分配的办公室筛选；调整登录手机不会改变办公室归属。':'办公室一、二按原手机编号划分；办公室三设备独立编号。账号管理与发布看板同步切换，未分配电脑的账号可在账号管理查看。';
}
document.addEventListener('click',event=>{
 const button=event.target.closest('[data-office]');
 if(!button)return;
 activeOffice=button.dataset.office;
 activeLifecyclePreset='';
 clearAccountSearch();['query','computer','status','published','accountType','lifecycleFilter','ageFilter','loginPhoneFilter'].forEach(id=>$(id).value='');
 selected.clear();render();
});
