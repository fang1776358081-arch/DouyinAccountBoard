// Presentation only: retain the existing per-field predicates and office scope.
const accountFilterFields=[['query','昵称'],['accountDouyinQuery','抖音号'],['accountIdQuery','账号编号'],['computer','登记电脑'],['status','账号状态'],['published','发布情况'],['accountDeviceQuery','登录手机序号'],['accountPhoneQuery','注册手机号'],['verificationFilter','核对情况'],['loginPhoneFilter','登录手机'],['accountType','账号类型'],['ageFilter','注册天数'],['lifecycleFilter','注册进度']];
const accountAdvancedFields=new Set(['accountDeviceQuery','accountPhoneQuery','verificationFilter','loginPhoneFilter','accountType','ageFilter','lifecycleFilter']);
function accountFilterValue(id){
 const field=$(id);if(!field||!String(field.value||'').trim())return '';
 const option=field.options&&Array.from(field.options).find(item=>item.value===field.value);
 return option?.textContent||option?.label||String(field.value);
}
function renderAccountFilterSummary(accounts){
 const host=$('accountActiveFilters');if(!host)return;
 const conditions=accountFilterFields.map(([id,label])=>({id,label,value:accountFilterValue(id)})).filter(item=>item.value);
 const preset=typeof activeLifecyclePreset==='undefined'?'':activeLifecyclePreset;
 if(preset)conditions.push({id:'lifecyclePreset',label:'注册进度快捷筛选',value:({all:'全部大号',ready:'满30天待注册',registered:'已注册小号',young:'未满30天',missing:'待补日期',blocked:'暂无法注册',unknown:'类型待确认'})[preset]||preset});
 const advancedCount=conditions.filter(item=>accountAdvancedFields.has(item.id)||item.id==='lifecyclePreset').length;
 if($('accountAdvancedCount'))$('accountAdvancedCount').textContent=advancedCount?'（已选 '+advancedCount+' 项）':'';
 host.innerHTML=`<span class="account-filter-count">筛选结果 <strong>${accounts.length}</strong> 个账号</span>`+(conditions.length?conditions.map(item=>`<button type="button" class="account-filter-chip" data-clear-account-filter="${esc(item.id)}" aria-label="清除${esc(item.label)}筛选">${esc(item.label)}：${esc(item.value)} <span aria-hidden="true">×</span></button>`).join(''):'<span class="account-filter-empty">未附加筛选条件</span>');
}
function clearAccountFilter(id){
 if(id==='lifecyclePreset'){activeLifecyclePreset='';['accountType','ageFilter','lifecycleFilter'].forEach(key=>$(key).value='');}
 else if(accountFilterFields.some(([key])=>key===id)){$(id).value='';if(['accountType','ageFilter','lifecycleFilter'].includes(id))activeLifecyclePreset='';}
 else return;
 render();
}
document.addEventListener('click',event=>{const button=event.target.closest('[data-clear-account-filter]');if(button)clearAccountFilter(button.dataset.clearAccountFilter);});
