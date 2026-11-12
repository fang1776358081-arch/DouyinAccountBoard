let pendingReviewPage=0;
const pendingReviewSize=15;
const pendingReviewReasons={all:'全部原因',position:'原表有多个位置',check:'原文待核对',nohandle:'未找到相同抖音号',duplicate:'抖音号重复',exact:'有同抖音号候选',missing:'手机序号未识别',noaccount:'同手机未找到账号',occupied:'候选已有发布位',ambiguous:'有多个相近账号',different:'同手机昵称有差异',ready:'有相近候选'};

function pendingPublishingCells(board=data){
 const layout=board?.publishing_layout;
 if(!layout?.enabled)return [];
 const computers=new Set((board.computers||[]).map(computer=>computer.id));
 // The board omits occupied cells outside the viewer's account scope.
 return (layout.cells||[]).filter(cell=>cell.account_id===null&&!cell.manual_link&&cell.label?.trim()&&computers.has(cell.computer_id));
}

function pendingReviewRows(cells=pendingPublishingCells()){
 const positions=new Map();
 for(const cell of data.publishing_layout?.cells||[]){
  if(!cell.account_id)continue;
  if(!positions.has(cell.account_id))positions.set(cell.account_id,[]);
  positions.get(cell.account_id).push(cell);
 }
 return cells.map(cell=>{
  const sourceHandle=publishingLabelHandle(cell.label);
  const inScope=data.accounts.filter(account=>publishingCandidateScope(account,cell));
  const sourceRepeated=/多个发布(?:位置|格)/.test(cell.reason||'');
  if(sourceHandle){
   const candidates=inScope.filter(account=>account.douyin_id===sourceHandle).map(account=>({account,positions:positions.get(account.id)||[],basis:'抖音号与原文一致'}));
   let reason='exact';
   if(sourceRepeated)reason='position';
   else if(candidates.length>1||String(cell.reason).includes('多条账号资料'))reason='duplicate';
   else if(!candidates.length)reason='nohandle';
   else if(candidates[0].positions.length)reason='occupied';
   return {cell,reason,candidates};
  }
  const phone=String(cell.label).match(/^\s*[.。]?\s*(\d+)(?:号手机|多开(?:抖音)?|小号)?\s*[-—－–]/)?.[1]||'';
  const candidate=(account,basis)=>({account,suggestion:publishingSuggestion(account,cell),positions:positions.get(account.id)||[],basis});
  let candidates=phone?inScope.filter(account=>deviceNumberMatches(account.phone_device_id,phone)).map(account=>candidate(account,'同手机')):[];
  const near=candidates.filter(choice=>choice.suggestion.recommended);
  let reason='ready';
  if(sourceRepeated)reason='position';
  else if(/待核对|待确认/.test(cell.label+near.map(choice=>choice.account.name).join('')))reason='check';
  else if(!phone)reason='missing';
  else if(!candidates.length)reason='noaccount';
  else if(!near.length)reason='different';
  else if(near.length>1)reason='ambiguous';
  else if(near[0].positions.length)reason='occupied';
  if(!phone){
   const name=publishingNameCore(cell.label);
   if(name.length>=5)candidates=inScope.map(account=>candidate(account,'仅名称线索')).filter(choice=>{
    const candidateLength=publishingNameCore(choice.account.name).length;
    return candidateLength>=5&&choice.suggestion.distance/Math.max(name.length,candidateLength)<=0.4;
   }).sort((left,right)=>left.suggestion.distance-right.suggestion.distance).slice(0,3);
  }
  candidates.sort((left,right)=>Number(right.suggestion.recommended)-Number(left.suggestion.recommended)||left.suggestion.distance-right.suggestion.distance);
  return {cell,phone,reason,candidates};
 });
}

function pendingReviewOfficeSelection(cells,hosts){
 const scoped=!!data.user?.scope_mode;
 const offices=scoped?(data.offices||[]):legacyOfficeDefinitions;
 const names=new Map(offices.filter(office=>office.id!=='all'&&office.id!=='pending').map(office=>[office.id,office.name]));
 const ids=cell=>{
  const related=scoped?(hosts.get(cell.computer_id)?.office_ids||[]):[cell.office];
  const known=related.filter(id=>names.has(id));
  return known.length?known:['pending'];
 };
 if(cells.some(cell=>ids(cell).includes('pending')))names.set('pending','归属待核对');
 const select=$('pendingReviewOffice'),value=select.value;
 select.innerHTML=`<option value="all">${scoped?'全部授权办公室':'全部办公室'}</option>`+
  Array.from(names,([id,name])=>`<option value="${esc(id)}">${esc(name)}</option>`).join('');
 select.value=names.has(value)?value:'all';
 return {value:select.value,names,ids};
}

function renderPendingReview(){
 if(!data||!$('pendingReviewToggle'))return;
 const cells=pendingPublishingCells();
 $('pendingReviewToggle').textContent=`待关联（${cells.length}）`;
 if($('pendingReviewPanel')?.hidden)return;
 const hosts=new Map(data.computers.map(host=>[host.id,host]));
 const office=pendingReviewOfficeSelection(cells,hosts);
 const scope=pendingReviewRows(cells.filter(cell=>office.value==='all'||office.ids(cell).includes(office.value)));
 const reasonSelect=$('pendingReviewReason'),reason=reasonSelect.value||'all';
 reasonSelect.innerHTML=Object.entries(pendingReviewReasons).map(([key,label])=>{
  const count=key==='all'?scope.length:scope.filter(row=>row.reason===key).length;
  return `<option value="${key}">${label}（${count}）</option>`;
 }).join('');
 reasonSelect.value=reason;
 const query=$('pendingReviewCell').value.trim().toUpperCase();
 const rows=scope.filter(row=>(reason==='all'||row.reason===reason)&&(!query||String(row.cell.source_cell||'').toUpperCase()===query));
 const pages=Math.ceil(rows.length/pendingReviewSize),canLink=hasPermission('publishing_manage');
 pendingReviewPage=Math.max(0,Math.min(pendingReviewPage,pages-1));
 const nextStep=canLink?'按原表位置核对账号，再确认关联。':'当前角色可查看，关联需发布格管理权限。';
 $('pendingReviewSummary').textContent=cells.length?`待关联 ${cells.length} 格，当前筛选 ${rows.length} 格。${nextStep}`:'';
 const empty=!data.publishing_layout?.enabled?'尚未导入发布安排。':cells.length?'当前筛选没有匹配的发布格，可重置筛选。':'当前范围没有待关联的发布格。';
 $('pendingReviewRows').innerHTML=rows.slice(pendingReviewPage*pendingReviewSize,(pendingReviewPage+1)*pendingReviewSize).map(row=>{
  const {cell,candidates}=row;
  const host=hosts.get(cell.computer_id);
  const location=office.ids(cell).map(id=>office.names.get(id)).filter(Boolean).join('、');
  const choices=candidates.slice(0,4).map(({account,suggestion,positions,basis})=>{
   const availability=positions.length?'已有安排：'+positions.map(position=>`${esc(hosts.get(position.computer_id)?.name||position.computer_id)} / ${esc(position.source_cell)}`).join('、'):'尚无发布位置';
   const detail=suggestion?` · ${suggestion.distance===0?'名称一致':'名称相差 '+suggestion.distance+' 字'}`:'';
   return `<div class="pending-candidate"><strong>${esc(accountDisplay(account))}</strong><small>${esc(account.status)} · ${esc(basis)}${detail}</small><small>${availability}</small></div>`;
  }).join('');
  const guidance=canLink?'可打开关联窗口，按抖音号、昵称或手机序号查找。':'暂无候选可供核对。';
  return `<tr><td><strong>${esc(host?.name||cell.computer_id)} / ${esc(cell.source_cell)}</strong><small>发布设备 ${esc(cell.slot)} · ${esc(location)}</small>${row.phone?`<small>手机 ${esc(row.phone)}</small>`:''}</td><td>${esc(cell.label)}${cell.reason?`<small>${esc(cell.reason)}</small>`:''}</td><td><span class="badge warn">${esc(pendingReviewReasons[row.reason])}</span></td><td>${choices||`<small>${guidance}</small>`}${candidates.length>4?`<small>另有 ${candidates.length-4} 个候选。</small>`:''}</td><td><button class="button" data-match-computer="${esc(cell.computer_id)}" data-match-slot="${esc(cell.slot)}" ${canLink?'':'disabled title="需要发布格管理权限"'}>关联账号</button></td></tr>`;
 }).join('')||`<tr><td colspan="5">${empty}</td></tr>`;
 $('pendingReviewPage').textContent=rows.length?`第 ${pendingReviewPage+1} / ${pages} 页`:'';
 $('pendingReviewPrevious').disabled=pendingReviewPage===0;
 $('pendingReviewNext').disabled=pendingReviewPage+1>=pages;
}

document.addEventListener('board-ready',renderPendingReview);
document.addEventListener('DOMContentLoaded',()=>{
 $('pendingReviewToggle').onclick=()=>{const panel=$('pendingReviewPanel');panel.hidden=!panel.hidden;$('pendingReviewToggle').setAttribute('aria-expanded',String(!panel.hidden));renderPendingReview();};
 for(const id of ['pendingReviewOffice','pendingReviewReason','pendingReviewCell'])$(id).oninput=()=>{pendingReviewPage=0;renderPendingReview();};
 $('pendingReviewPrevious').onclick=()=>{pendingReviewPage--;renderPendingReview();};$('pendingReviewNext').onclick=()=>{pendingReviewPage++;renderPendingReview();};
 $('pendingReviewReset').onclick=()=>{$('pendingReviewOffice').value='all';$('pendingReviewReason').value='all';$('pendingReviewCell').value='';pendingReviewPage=0;renderPendingReview();};
});
