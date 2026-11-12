/* Dedicated, scoped dual-sheet flow. Uploaded bytes and record values stay in memory. */
window.TableSync = (() => {
 'use strict';
 const pageSize = 50, maxBytes = 8 * 1024 * 1024;
 const fieldNames = {name:'昵称',registration_phone:'注册手机号',registered_on:'注册日期',status:'账号状态',computer_id:'资料登记电脑',slot:'资料位置',responsible:'负责人',usage:'使用情况',violation:'违规状态',business_type:'业务类型',storefront:'门头',source_data:'表格来源',source_info:'表格来源'};
 const sourceFieldNames = Object.fromEntries(['responsible','usage','violation','business_type','storefront','source_data'].map(key=>['source.'+key,fieldNames[key]]));
 const fieldName = key => fieldNames[key] || sourceFieldNames[key] || key;
 const summaryNames = {source_rows:'来源行数',unique_accounts:'唯一账号数',safe_accounts:'可执行账号数',new_accounts:'新增账号数',updated_accounts:'更新账号数',changed_fields:'变更字段数',unchanged_fields:'无变化字段数',conflict_accounts:'冲突账号数',conflict_items:'冲突问题数',skipped_accounts:'跳过账号数'};
 const issueStates = {open:'未处理',confirmed_no_action:'已确认无需处理',corrected:'已人工修正'};
 const actionNames = {create:'安全新增',update:'更新资料',same:'保持不变',conflict:'待核对',skip:'跳过',delete:'撤销本批新增',rollback:'恢复字段'};
 const uncertainCodes = new Set(['REQUEST_PENDING','REQUEST_ID_REUSED','RESULT_EXPIRED','RESTORE_GENERATION_EXPIRED','TABLE_SYNC_RESULT_UNKNOWN']);
 const $ = id => document.getElementById(id);
 const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const textValue = value => value === '' || value == null ? '（空白）' : typeof value === 'object' ? JSON.stringify(value) : String(value);
 const safeRow = row => !!row && row.blocked !== true && ['create','update','delete','rollback'].includes(row.action);
 const uuid = () => {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16)); bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
  return [...bytes].map((b,i) => ([4,6,8,10].includes(i)?'-':'')+b.toString(16).padStart(2,'0')).join('');
 };
 const stamp = value => { const milliseconds = typeof value === 'number' ? value * 1000 : Date.parse(value); return Number.isFinite(milliseconds) ? milliseconds : 0; };
 const timeText = value => stamp(value) ? new Date(stamp(value)).toLocaleString('zh-CN',{hour12:false}) : '时间未提供';
 let officeId = 'three', snapshot = null, preview = null, receipt = null, selected = new Set(), authorityReady = false;
 let step = 1, rowPage = 1, changesPage = 1, issuesPage = 1, generation = 0, busy = false, pending = null, initialized = false, expired = false, reviewDirty = false;
 let activeRead = null, readAbort = null, previewTimer = null, statusTimer = null;
 const readers = new Set();
 const officeName = () => snapshot?.office_name || (officeId === 'three' ? '办公室三' : '当前办公室');
 const manageable = () => authorityReady && snapshot?.available === true && snapshot?.can_manage === true;
 const writable = () => manageable() && snapshot?.pilot?.state === 'enabled';
 const hasDraft = () => !!preview || busy || !!pending || reviewDirty || Boolean($('syncAccountsFile')?.files?.length || $('syncProgressFile')?.files?.length || $('syncRound')?.value);
 const pendingKey = () => snapshot?.user?.id == null ? null : 'table-sync:request:'+snapshot.user.id+':'+officeId;
 function rememberPending() {
  const key = pendingKey(); if (!key) return;
  try {
   if (!pending) sessionStorage.removeItem(key);
   // Only the fixed request number and operation kind survive refresh. Never
   // retain csrf, filenames, source rows, upload bytes or change values here.
   else sessionStorage.setItem(key, JSON.stringify({request_id:pending.body.request_id,kind:pending.kind,path:pending.path}));
  } catch (_) { /* Recovery remains available in this tab if storage is disabled. */ }
 }
 function restorePending() {
  const key = pendingKey(); if (!key || pending) return;
  try {
   const value = JSON.parse(sessionStorage.getItem(key) || 'null');
   const paths = ['/pilot','/preview','/apply','/review','/rollback-preview','/rollback-apply'].map(path=>'/api/table-sync'+path);
   if (!value || !/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(value.request_id) || !paths.includes(value.path) || !['pilot','preview','apply','review','rollback-preview','rollback-apply'].includes(value.kind)) return;
   pending = {kind:value.kind,path:value.path,body:{office_id:officeId,request_id:value.request_id},lookupOnly:true,uncertain:true};
  } catch (_) { /* A malformed UI marker never becomes an executable request. */ }
 }
 function error(message = '') { $('syncError').textContent = message; $('syncError').hidden = !message; }
 function notice(message) { $('syncNotice').textContent = message; $('syncNotice').hidden = !message; }
 function query(path, params = {}) { return path+'?'+new URLSearchParams({office_id:officeId,...params}).toString(); }
 async function request(path, options = {}) {
  const controller = new AbortController(), abort = () => controller.abort();
  if (options.signal?.aborted) controller.abort();
  else options.signal?.addEventListener?.('abort',abort,{once:true});
  const timer = setTimeout(abort, options.timeout || 90000);
  try {
   const response = await fetch(path,{method:options.body?'POST':'GET',cache:'no-store',credentials:'same-origin',signal:controller.signal,
    headers:options.body?{'Content-Type':'application/json','X-CSRF-Token':snapshot?.user?.csrf || ''}:undefined,
    body:options.body?JSON.stringify(options.body):undefined});
   let result; try { result = await response.json(); } catch (_) { throw Object.assign(Error('服务器响应不完整，请核对原请求结果。'),{status:response.status}); }
   if (!response.ok) throw Object.assign(Error(result.error || result.message || '请求未完成'),{status:response.status,code:result.code});
   return result;
  } finally { clearTimeout(timer); options.signal?.removeEventListener?.('abort',abort); }
 }
 function steps(next) {
  step = next;
  document.querySelectorAll('[data-sync-step]').forEach(node=>{const number = Number(node.dataset.syncStep);node.removeAttribute('aria-current');if(number===step)node.setAttribute('aria-current','step');node.dataset.complete=String(number<step);});
  ['Upload','Preview','Confirm','Receipt'].forEach((name,index)=>$( 'sync'+name+'Panel').hidden = step !== index+1);
  lock();
 }
 function lock() {
  const locked = busy || !!pending;
  const previewPages=Math.max(1,Math.ceil(filteredRows().length/pageSize));
  $('syncPreviewPrevious').disabled=locked||!preview||rowPage<=1;
  $('syncPreviewNext').disabled=locked||!preview||rowPage>=previewPages;
  $('syncUploadFields').disabled = locked || !writable();
  $('syncPreviewButton').disabled = locked || !writable();
  for (const id of ['syncBackUpload','syncBackPreview','syncGoConfirm','syncApplyButton','syncStartNew','syncRollbackButton','syncReceiptExport','syncRefreshHistory','syncSelectAll','syncSelectNone','syncRowFilter']) $(id).disabled=locked;
  $('syncGoConfirm').disabled ||= expired || !preview || !writable();
  $('syncApplyButton').disabled ||= expired || !preview || !writable();
  $('syncRollbackButton').disabled ||= !manageable() || !receipt || receipt.kind==='rollback';
  $('syncPilotActions').querySelectorAll('button').forEach(button=>button.disabled=locked||!manageable());
  $('syncRecovery').hidden = !pending;
  $('syncPendingId').textContent = pending ? '原请求编号：'+pending.body.request_id : '';
  $('syncCheckRequest').disabled=busy; $('syncRetryRequest').disabled=busy||!!pending?.lookupOnly;
  document.querySelectorAll('[data-sync-row]').forEach(node=>node.disabled=locked||expired||!safeRow(preview?.rows.find(row=>row.row_key===node.dataset.syncRow)));
  document.querySelectorAll('[data-sync-batch]').forEach(node=>node.disabled=locked||!manageable());
  $('syncReceiptIssues').querySelectorAll('input,select,button').forEach(node=>node.disabled=locked||!manageable());
  $('tableSyncApp').setAttribute('aria-busy',String(busy));
 }
 function clearFiles() {
  $('syncAccountsFile').value=''; $('syncProgressFile').value=''; $('syncRound').value='';
  $('syncAccountsInfo').textContent='选择账号情况表文件'; $('syncProgressInfo').textContent='选择同一轮工作进度表文件';
 }
 function clearPreview() { preview=null;selected.clear();expired=false;clearInterval(previewTimer);previewTimer=null; }
 function resetFlow() {
  if (pending || busy) return false;
  if(reviewDirty&&!window.confirm('核对说明尚未保存。放弃这些输入并开始下一批？'))return false;
  generation++; clearPreview(); receipt=null; reviewDirty=false; clearFiles(); error(); notice('先选择同一轮的两份来源文件，生成预览后再确认。'); steps(1); return true;
 }
 function renderSummary(host, summary) {
  $(host).innerHTML=Object.entries(summaryNames).filter(([key])=>Number.isInteger(summary?.[key])).map(([key,label])=>`<div><span>${label}</span><strong>${summary[key]}</strong></div>`).join('')||'<p class="permission-note">本批次未提供数量摘要，请查看逐项记录。</p>';
 }
 function renderHistory() {
  const batches=snapshot?.recent_batches || [];
  $('syncRecentBatches').innerHTML=!manageable()?'<p class="permission-note">批次内容由总管理员核对；日常资料仍按原权限使用。</p>':!batches.length?'<p class="permission-note">尚无本办公室的同步批次。生成预览不会修改账号。</p>':batches.map(batch=>`<div class="sync-history-row"><div><strong>${esc(batch.id || batch.batch_id)}</strong><small>${esc(timeText(batch.created_at))} · ${esc(({add:'安全新增',update:'资料更新',rollback:'批次回退'})[batch.kind]||batch.kind)} · 账号 ${Number(batch.summary?.updated_accounts||0)+Number(batch.summary?.new_accounts||0)} / 字段 ${Number(batch.summary?.changed_fields||0)}</small></div><button type="button" class="text-button" data-sync-batch="${esc(batch.id || batch.batch_id)}">查看回执</button></div>`).join('');
  lock();
 }
 function renderStatus() {
  const name=officeName(),enabled=snapshot?.pilot?.state==='enabled',available=snapshot?.available===true;
  $('syncOffice').textContent=name+' · 试点';
  $('syncUser').textContent=snapshot?.user ? `${snapshot.user.name || '当前人员'} · ${snapshot.user.role==='owner'?'总管理员':'已授权人员'}` : '';
  $('syncPilotActions').hidden=!available||!manageable();
  $('syncEnablePilot').hidden=enabled; $('syncPausePilot').hidden=!enabled; $('syncEndPilot').hidden=!enabled;
  $('syncPilotState').textContent=!available?'新同步流程尚未启用':`${name} · ${({enabled:'进行中',paused:'已暂停',ended:'已结束',disabled:'尚未开启'})[snapshot?.pilot?.state]||'尚未开启'}`;
  if(!available)notice(snapshot?.note || '表格同步的新结构尚未安装。原账号资料与原有工具可正常使用；此页暂不允许写入。');
  else if(!manageable())notice('当前为只读范围。表格同步由总管理员预览、确认和核对回执；不会增加你的资料权限。');
  else if(!enabled)notice('试点尚未开启或已暂停，不能生成新预览或写入。总管理员核对范围后可开启。');
  else if(!hasDraft())notice('试点已开启。请先上传同一轮双表，核对安全行与冲突后再确认。');
  renderHistory();lock();
 }
 async function loadStatus(options={}) {
  if((busy||pending||preview)&&!options.force)return false;
  try {
   const result=await request(query('/api/table-sync'));
   if(!result || typeof result.available!=='boolean' || !result.user || result.office_id!==officeId)throw Error('试点状态响应不完整，请刷新重试。');
   if(snapshot?.user?.id!=null && snapshot.user.id!==result.user.id){clearPreview();receipt=null;clearFiles();pending=null;steps(1);}
   snapshot=result;window.WorkbenchNav?.update({user:result.user,office:officeId,pilot:result.pilot?.state});authorityReady=true;restorePending();renderStatus();return true;
  } catch(e) {
   void window.WorkbenchNav?.loadSession();authorityReady=false;lock();
   if(e.status===401){notice('登录已失效，请重新登录后核对。');error('请打开登录页完成登录，本页没有写入新的资料。');$('syncPilotActions').hidden=true;$('syncUploadFields').disabled=true;}
   else {notice('试点状态尚未确认，暂不允许写入。');error('试点状态读取失败：'+e.message+'。可点击“刷新批次”重试；当前无法判断是否允许写入。');}
   return false;
  }
 }
 function filteredRows() {
  const filter=$('syncRowFilter').value || 'all';
  return (preview?.rows||[]).filter(row=>filter==='all'||filter==='safe'&&safeRow(row)||filter==='issues'&&(row.action==='conflict'||(row.issues||[]).length)||filter==='same'&&['same','skip'].includes(row.action));
 }
 function changesMarkup(changes) {
  return (changes||[]).map(change=>`<div class="sync-diff-item"><strong>${esc(fieldName(change.field))}</strong><del>${esc(textValue(change.before))}</del> → <ins>${esc(textValue(change.after))}</ins></div>`).join('');
 }
 function sourceText(row) {return (row.source_rows||[]).map(source=>`${source.sheet==='accounts'?'账号情况表':source.sheet==='progress'?'工作进度表':'来源'} 第${source.row}行`).join('；');}
 function sourceMarkup(source,withFilenames=false) {
  const titles={accounts:'账号情况表',progress:'工作进度表'};
  return `<p>本轮标识：${esc(source?.round||'原批次来源')}</p>`+Object.entries(titles).filter(([key])=>source?.[key]).map(([key,title])=>{
   const metadata=source[key],file=withFilenames?$(key==='accounts'?'syncAccountsFile':'syncProgressFile').files?.[0]:null;
   return `<p><strong>${title}</strong>${file?' · '+esc(file.name):''} · ${Number.isInteger(metadata.bytes)?metadata.bytes+' 字节':'文件大小已记录'}<br><code>${esc(metadata.sha256||'摘要未提供')}</code></p>`;
  }).join('');
 }
 function updateExpiry() {
  if(!preview)return;
  expired=!stamp(preview.expires_at)||stamp(preview.expires_at)<=Date.now();
  $('syncPreviewMeta').textContent=`${officeName()} · ${preview.mode==='rollback'?'批次回退':preview.mode==='add'?'安全新增':'更新已有账号'} · 预览有效至 ${timeText(preview.expires_at)}${expired?' · 已过期，请重新预览':''}`;
  lock();
 }
 function selectionSummary() {
  const allSafe=(preview?.rows||[]).filter(safeRow).length;
  $('syncSelectionCount').textContent=`已选 ${selected.size} 个账号 / 全部安全账号 ${allSafe} 个；冲突账号不可选择`;
  $('syncGoConfirm').textContent=selected.size?'核对范围并继续':'记录本次核对（不改账号）';
  lock();
 }
 function renderPreviewRows() {
  const rows=filteredRows(),pages=Math.max(1,Math.ceil(rows.length/pageSize));rowPage=Math.min(Math.max(1,rowPage),pages);
  $('syncPreviewRows').innerHTML=rows.length?rows.slice((rowPage-1)*pageSize,rowPage*pageSize).map(row=>{
   const safe=safeRow(row),issues=(row.issues||[]).map(item=>item.message||item.code).filter(Boolean),reason=[row.reason,...issues].filter(Boolean);
   return `<tr><td><input type="checkbox" aria-label="选择抖音号 ${esc(row.douyin_id || '未提供')}" data-sync-row="${esc(row.row_key)}" ${selected.has(row.row_key)?'checked':''} ${!safe||expired||pending||busy?'disabled':''}></td><td><strong>${esc(row.current_name||row.source_name||'名称待核对')}</strong><small>抖音号 ${esc(row.douyin_id||'未提供')}</small></td><td>${esc(sourceText(row)||'批次记录')}</td><td><span class="sync-tag" data-tone="${safe?'success':row.action==='conflict'?'warning':'neutral'}">${esc(actionNames[row.action]||row.action)}</span></td><td class="sync-diff">${changesMarkup(row.changes)}${reason.map(value=>`<small>${esc(value)}</small>`).join('')||(!row.changes?.length?'<small>没有字段变更</small>':'')}</td></tr>`;
  }).join(''):'<tr><td colspan="5" class="sync-empty"><strong>没有符合当前条件的账号</strong>可切回“全部来源账号”核对。</td></tr>';
  $('syncPreviewPage').textContent=`第 ${rowPage} / ${pages} 页 · 共 ${rows.length} 个账号`;
  selectionSummary();
 }
 function showPreview(result) {
  if(result?.office_id!==officeId || !Array.isArray(result.rows) || !result.preview_id || !result.confirmation || !stamp(result.expires_at) || !['add','update','rollback'].includes(result.mode) || result.rows.some(row=>!row.row_key || !Array.isArray(row.changes)))throw Error('预览响应不完整，未允许确认，请核对原请求。');
  preview=result;receipt=null;selected=new Set(result.rows.filter(row=>safeRow(row)&&row.selected!==false).map(row=>row.row_key));rowPage=1;expired=false;
  $('syncRowFilter').value='all';renderSummary('syncSummary',result.summary);
  $('syncPreviewSources').innerHTML=sourceMarkup(result.source,true)+`<p>本机收到预览：${esc(new Date().toLocaleString('zh-CN',{hour12:false}))}</p>`;
  $('syncWarnings').textContent=(result.warnings||[]).join('；');$('syncWarnings').hidden=!(result.warnings||[]).length;
  clearInterval(previewTimer);previewTimer=setInterval(updateExpiry,1000);updateExpiry();steps(2);renderPreviewRows();
  notice('预览已生成，账号尚未写入。请核对来源、变更字段、选择数量及冲突原因。');
 }
 function showConfirm() {
  if(!preview||expired||busy||pending)return;
  const chosen=preview.rows.filter(row=>selected.has(row.row_key)&&safeRow(row)),fields=chosen.reduce((count,row)=>count+(row.changes||[]).length,0);
  $('syncConfirmScope').textContent=chosen.length?`本次只影响${officeName()}：${chosen.length} 个账号，${fields} 个字段；${preview.mode==='rollback'?'执行本批次反向恢复':'不改变其他办公室和待分配资料'}。`:`本次只为${officeName()}保存核对回执和问题清单，不修改任何账号资料。`;
  $('syncApplyButton').textContent=chosen.length?'确认执行本批次':'确认记录核对回执（不改账号）';
  $('syncConfirmationLabel').textContent='请输入“'+preview.confirmation+'”确认';$('syncConfirmation').value='';steps(3);$('syncConfirmation').focus();
 }
 function receiptTable(headers,rows) {return `<table class="sync-table"><thead><tr>${headers.map(header=>`<th>${esc(header)}</th>`).join('')}</tr></thead><tbody>${rows||`<tr><td colspan="${headers.length}" class="sync-empty">本批次没有此类记录。</td></tr>`}</tbody></table>`;}
 function paginate(values,page) {const pages=Math.max(1,Math.ceil(values.length/pageSize)),current=Math.min(Math.max(1,page),pages);return {items:values.slice((current-1)*pageSize,current*pageSize),page:current,pages,total:values.length};}
 function renderReceipt() {
  if(!receipt)return;
  $('syncReceiptMeta').textContent=`${officeName()} · 批次 ${receipt.batch_id} · ${timeText(receipt.created_at)} · ${receipt.actor?.name||'操作者已记录'}`;
  renderSummary('syncReceiptSummary',receipt.summary);
  $('syncReceiptSources').innerHTML=sourceMarkup(receipt.source);
  const changes=paginate(receipt.changes||[],changesPage);changesPage=changes.page;
  $('syncReceiptChanges').innerHTML=receiptTable(['账号','字段','修改前','修改后','来源'],changes.items.map(change=>`<tr><td>${esc(change.douyin_id||change.account_id)}</td><td>${esc(fieldName(change.field))}</td><td>${esc(textValue(change.before))}</td><td>${esc(textValue(change.after))}</td><td>${esc(sourceText(change)||'本批次记录')}</td></tr>`).join(''));
  $('syncChangesPage').textContent=`第 ${changes.page} / ${changes.pages} 页 · 共 ${changes.total} 项字段变更`;
  $('syncChangesPrevious').disabled=changes.page<=1; $('syncChangesNext').disabled=changes.page>=changes.pages;
  const issues=paginate(receipt.issues||[],issuesPage);issuesPage=issues.page;
  $('syncIssueCount').textContent=`共 ${(receipt.issues||[]).length} 项，其中未处理 ${(receipt.issues||[]).filter(item=>item.state==='open').length} 项。问题项数不等于账号数；标记处理结果不会自动修改账号。`;
  $('syncReceiptIssues').innerHTML=receiptTable(['账号 / 原因','处理状态','核对记录'],issues.items.map(item=>`<tr><td><strong>${esc(item.douyin_id||'标识待核对')}</strong><small>${esc(item.message||item.code)}</small></td><td><span class="sync-tag" data-tone="${item.state==='open'?'warning':'success'}">${esc(issueStates[item.state]||'待核对')}</span><small>${esc(item.note||'尚无核对说明')}</small></td><td>${manageable()?`<form class="sync-review-form" data-sync-review="${esc(item.id)}"><select aria-label="核对结果"><option value="open" ${item.state==='open'?'selected':''}>未处理</option><option value="confirmed_no_action" ${item.state==='confirmed_no_action'?'selected':''}>已确认无需处理</option><option value="corrected" ${item.state==='corrected'?'selected':''}>已人工修正</option></select><input aria-label="核对说明" placeholder="说明核对依据" value="${esc(item.note||'')}" maxlength="500" required><button type="submit" class="button">保存核对</button></form>`:'由总管理员核对'}</td></tr>`).join(''));
  $('syncIssuesPage').textContent=`第 ${issues.page} / ${issues.pages} 页 · 共 ${issues.total} 项问题`;
  $('syncIssuesPrevious').disabled=issues.page<=1; $('syncIssuesNext').disabled=issues.page>=issues.pages;
  $('syncReceiptExport').hidden=snapshot?.can_export!==true; lock();
 }
 async function showReceipt(value) {
  let result=value?.receipt || value;
  if(!Array.isArray(result?.changes) && value?.batch_id)result=await request(query('/api/table-sync/batch',{id:value.batch_id}));
  if(!result?.batch_id || result.office_id!==officeId || !Array.isArray(result.changes) || !Array.isArray(result.issues))throw Error('回执内容未完整返回，请核对原请求。');
  clearPreview();clearFiles();receipt=result;reviewDirty=false;changesPage=1;issuesPage=1;steps(4);renderReceipt();
  notice('本批次结果已确认。请核对逐字段回执，待核对问题仍保留在本批次中。');
 }
 async function completed(result,operation) {
  if(operation.kind==='preview'||operation.kind==='rollback-preview')showPreview(result);
  else if(operation.kind==='apply'||operation.kind==='rollback-apply') {
   if(result?.ok!==true||!result.batch_id)throw Error('执行结果尚未确认，请核对原请求。');
   await showReceipt(result);
  } else if(operation.kind==='review') {
   if(result?.ok!==true)throw Error('核对结果未明确返回。');
   const id=operation.body.batch_id||result.batch_id||receipt?.batch_id;
   if(id)await showReceipt(await request(query('/api/table-sync/batch',{id})));
  } else if(operation.kind==='pilot') {
   if(result?.ok!==true&&!result?.pilot)throw Error('试点状态变更尚未确认。');
  }
 }
 function definiteFailure(e) {return [400,403,404,409,413,422].includes(e.status)&&!uncertainCodes.has(e.code);}
 async function runPending(checkOnly=false) {
  if(!pending||busy||pending.lookupOnly&&!checkOnly)return false;
  const operation=pending;busy=true;error();lock();
  try {
   const result=checkOnly?await request(query('/api/table-sync/request',{request_id:operation.body.request_id})):await request(operation.path,{body:operation.body});
   await completed(result,operation);
   pending=null;rememberPending();busy=false;lock();
   if(operation.kind==='pilot'){await loadStatus({force:true});notice('试点状态已确认。'+(snapshot?.pilot?.state==='enabled'?'新预览仅限当前办公室。':'新预览和写入已停止，历史回执保留。'));}
   else if(operation.kind==='apply'||operation.kind==='rollback-apply'){await loadStatus({force:true});renderReceipt();}
   return true;
  } catch(e) {
   if(checkOnly&&e.code==='REQUEST_NOT_FOUND')error('当前尚未查到原请求回执。结果仍待确认，请稍后再次核对'+(operation.lookupOnly?'；原始输入已随刷新清除，请由管理员继续核查，勿创建新批次。':'或重试同一请求，不能换新请求编号。'));
   else if(definiteFailure(e)&&!checkOnly&&!operation.uncertain) {
    pending=null;rememberPending();
    if(operation.kind==='apply'||operation.kind==='rollback-apply'){expired=true;steps(2);}
    error(e.message+'；本次操作已被明确拒绝，请核对后重新预览。');
   } else {operation.uncertain=true;error('原请求结果待确认：'+e.message+'。请使用“核对原请求结果”，勿重新提交另一批。');}
   return false;
  } finally {busy=false;lock();}
 }
 async function write(path,body,kind) {
  if(pending||busy)return false;
  pending={path,body:{...body,office_id:officeId,request_id:body.request_id||uuid()},kind,lookupOnly:false,uncertain:false};rememberPending();
  return runPending(false);
 }
 function cancelRead() {
  if(!activeRead||pending)return;
  generation++;readAbort?.abort();readers.forEach(reader=>{try{reader.abort();}catch(_){}});readers.clear();activeRead=null;busy=false;
  $('syncReadProgress').hidden=true;notice('已取消本次文件读取，尚未提交预览或修改账号。');lock();
 }
 function readFile(file,report) {
  return new Promise((resolve,reject)=>{
   const reader=new FileReader();readers.add(reader);
   const done=()=>readers.delete(reader);
   reader.onprogress=event=>{if(event.lengthComputable)report(event.loaded,event.total);};
   reader.onerror=()=>{done();reject(Error('文件读取失败，请重新选择来源文件。'));};
   reader.onabort=()=>{done();reject(Object.assign(Error('已取消文件读取'),{name:'AbortError'}));};
   reader.onload=()=>{done();const result=String(reader.result||''),comma=result.indexOf(',');if(comma<0){reject(Error('文件读取结果不完整'));return;}report(file.size,file.size);resolve({filename:file.name,content:result.slice(comma+1)});};
   reader.readAsDataURL(file);
  });
 }
 async function generatePreview(event) {
  event?.preventDefault();if(busy||pending||!writable())return false;
  const accounts=$('syncAccountsFile').files?.[0],progress=$('syncProgressFile').files?.[0],round=$('syncRound').value.trim();
  if(!accounts||!progress||!round){error('请填写本轮资料标识，并同时选择账号情况表和工作进度表。');return false;}
  if([accounts,progress].some(file=>!/(?:\.csv|\.xlsx)$/i.test(file.name))){error('只接受 CSV 或 Excel（.xlsx）文件。');return false;}
  if(accounts.size+progress.size>maxBytes){error('两份表格合计不能超过 8 MB，请先整理来源文件。');return false;}
  if(accounts===progress){error('请选择两份不同的来源表。');return false;}
  const own=++generation,loaded=[0,0],total=accounts.size+progress.size;
  clearPreview();busy=true;error();activeRead=own;readAbort=new AbortController();$('syncReadProgress').hidden=false;$('syncCancelRead').textContent='取消预览';$('syncReadMeter').value=0;
  $('syncReadStage').textContent='读取文件：0%';lock();
  try {
   const files=await Promise.all([accounts,progress].map((file,index)=>readFile(file,(bytes)=>{
    if(own!==generation)return;loaded[index]=bytes;const percent=total?Math.min(100,Math.round(loaded.reduce((a,b)=>a+b,0)*100/total)):100;
    $('syncReadMeter').value=percent;$('syncReadStage').textContent=`读取文件：${percent}%（只表示本机文件读取进度）`;
   })));
   if(own!==generation)return false;
   $('syncReadStage').textContent='分析匹配并生成预览：等待服务器核对，账号尚未写入…';$('syncReadMeter').removeAttribute('value');
   // Preview can be safely cancelled: it never modifies account business data.
   // A cancelled response is ignored even if the server finishes its preview.
   activeRead=own;
   const mode=document.querySelector('input[name="syncMode"]:checked')?.value||'add';
   const result=await request('/api/table-sync/preview',{body:{office_id:officeId,mode,source_round:round,accounts_file:files[0],progress_file:files[1],request_id:uuid()},signal:readAbort.signal});
   if(own!==generation)return false;
   $('syncReadStage').textContent='预览已生成';showPreview(result);return true;
  } catch(e) {if(own===generation&&e.name!=='AbortError')error('预览未完成：'+e.message+'。账号尚未写入，可保留来源文件后重试。');return false;}
  finally {if(own===generation){activeRead=null;busy=false;$('syncReadProgress').hidden=true;lock();}}
 }
 async function openBatch(id) {
  if(busy||pending)return;
  if((preview||reviewDirty)&&!window.confirm('当前预览或核对说明尚未保存。打开历史批次将放弃这些输入，是否继续？'))return;
  busy=true;error();lock();
  try{await showReceipt(await request(query('/api/table-sync/batch',{id})));}catch(e){error('回执读取失败：'+e.message+'。可从最近批次重新打开。');}finally{busy=false;lock();}
 }
 function downloadReceipt() {
  if(!receipt||snapshot?.can_export!==true||busy||pending)return;
  const blob=new Blob([JSON.stringify(receipt,null,2)],{type:'application/json;charset=utf-8'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download='表格同步回执-'+receipt.batch_id+'.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);
 }
 async function pilotState(state) {
  if(!manageable()||busy||pending)return;
  if((preview||reviewDirty)&&!window.confirm('变更试点状态将放弃当前未执行的预览或核对说明，是否继续？'))return;
  const word={enabled:'开启',paused:'暂停',ended:'结束'}[state],confirmation=word+officeName()+'试点';
  if(!window.confirm(`${confirmation}？此操作只改变试点写入开关，不修改账号、设备或历史。`))return;
  clearPreview();reviewDirty=false;steps(1);
  await write('/api/table-sync/pilot',{state,expected_revision:snapshot.pilot?.revision||0,confirmation},'pilot');
 }
 function init() {
  if(initialized||!$('tableSyncApp'))return;
  initialized=true;
  const fromURL=new URLSearchParams(location.search||'').get('office');officeId=fromURL||'three';
  $('syncUploadForm').onsubmit=generatePreview;$('syncCancelRead').onclick=cancelRead;
  for(const [id,info]of [['syncAccountsFile','syncAccountsInfo'],['syncProgressFile','syncProgressInfo']])$(id).onchange=()=>{
   if(busy||pending)return;clearPreview();const file=$(id).files?.[0];$(info).textContent=file?file.name+' · '+(file.size/1024).toFixed(1)+' KB':id==='syncAccountsFile'?'选择账号情况表文件':'选择同一轮工作进度表文件';
  };
  $('syncBackUpload').onclick=()=>{if(busy||pending)return;clearPreview();steps(1);notice('已返回来源，请重新生成预览后确认。');};
  $('syncBackPreview').onclick=()=>{steps(2);renderPreviewRows();};$('syncGoConfirm').onclick=showConfirm;
  $('syncSelectAll').onclick=()=>{selected=new Set((preview?.rows||[]).filter(safeRow).map(row=>row.row_key));renderPreviewRows();};
  $('syncSelectNone').onclick=()=>{selected.clear();renderPreviewRows();};
  $('syncRowFilter').onchange=()=>{rowPage=1;renderPreviewRows();};
  $('syncPreviewRows').onchange=event=>{const node=event.target.closest?.('[data-sync-row]');if(!node||busy||pending||expired)return;const row=preview?.rows.find(row=>row.row_key===node.dataset.syncRow);if(!safeRow(row))return;if(node.checked)selected.add(row.row_key);else selected.delete(row.row_key);selectionSummary();};
  $('syncPreviewPrevious').onclick=()=>{if(busy||pending||!preview)return;rowPage--;renderPreviewRows();};$('syncPreviewNext').onclick=()=>{if(busy||pending||!preview)return;rowPage++;renderPreviewRows();};
  $('syncConfirmForm').onsubmit=async event=>{event.preventDefault();if(!preview||expired||busy||pending||!writable())return;if($('syncConfirmation').value!==preview.confirmation){error('请输入完整确认语句“'+preview.confirmation+'”。');return;}const isRollback=preview.mode==='rollback';await write('/api/table-sync/'+(isRollback?'rollback-apply':'apply'),{preview_id:preview.preview_id,selected_rows:preview.rows.filter(row=>selected.has(row.row_key)&&safeRow(row)).map(row=>row.row_key),confirmation:preview.confirmation},isRollback?'rollback-apply':'apply');};
  $('syncCheckRequest').onclick=()=>runPending(true);$('syncRetryRequest').onclick=()=>pending?.lookupOnly?false:runPending(false);
  $('syncEnablePilot').onclick=()=>pilotState('enabled');$('syncPausePilot').onclick=()=>pilotState('paused');$('syncEndPilot').onclick=()=>pilotState('ended');
  $('syncRefreshHistory').onclick=()=>loadStatus();$('syncReceiptExport').onclick=downloadReceipt;$('syncStartNew').onclick=resetFlow;
  $('syncRollbackButton').onclick=async()=>{if(!receipt||busy||pending||!manageable())return;if(reviewDirty){error('请先保存当前核对说明，再预览回退。输入已保留。');return;}const batch_id=receipt.batch_id;await write('/api/table-sync/rollback-preview',{batch_id},'rollback-preview');};
  for(const [id,key,delta]of [['syncChangesPrevious','changes',-1],['syncChangesNext','changes',1],['syncIssuesPrevious','issues',-1],['syncIssuesNext','issues',1]])$(id).onclick=()=>{if(busy||pending)return;if(reviewDirty){error('请先保存当前核对说明，再切换回执页码。输入已保留。');return;}if(key==='changes')changesPage+=delta;else issuesPage+=delta;renderReceipt();};
  $('syncRecentBatches').onclick=event=>{const button=event.target.closest?.('[data-sync-batch]');if(button)void openBatch(button.dataset.syncBatch);};
  $('syncReceiptIssues').onsubmit=async event=>{const form=event.target.closest?.('[data-sync-review]');if(!form)return;event.preventDefault();if(!manageable()||pending||busy||!receipt)return;const note=form.querySelector('input').value.trim();if(!note){error('请填写核对说明，记录处理依据。');return;}await write('/api/table-sync/review',{batch_id:receipt.batch_id,issue_id:form.dataset.syncReview,state:form.querySelector('select').value,note},'review');};
  $('syncReceiptIssues').oninput=()=>{reviewDirty=true;};$('syncReceiptIssues').onchange=()=>{reviewDirty=true;};
  window.addEventListener('beforeunload',event=>{if(hasDraft()){event.preventDefault();event.returnValue='';}});
  window.addEventListener('pagehide',()=>{readAbort?.abort();readers.forEach(reader=>{try{reader.abort();}catch(_){}});readers.clear();generation++;clearInterval(previewTimer);clearInterval(statusTimer);});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!hasDraft())void loadStatus();});
  steps(1);void loadStatus();statusTimer=setInterval(()=>{if(!document.hidden&&!hasDraft())void loadStatus();},15000);
 }
 return Object.freeze({init,loadStatus,generatePreview,cancelRead,openBatch,resetFlow,showConfirm,runPending,
  model:Object.freeze({esc,safeRow,stamp,paginate,changesMarkup,issueStates,summaryNames,maxBytes,pageSize}),
  getState:()=>({office_id:officeId,step,busy,pending:pending?{request_id:pending.body.request_id,kind:pending.kind,lookupOnly:pending.lookupOnly}:null,selected:[...selected],preview,receipt,expired})});
})();
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',()=>window.TableSync.init());else window.TableSync.init();
