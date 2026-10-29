/* Office-three import is deliberately additive. Existing records cannot be selected for overwrite. */
window.OfficeThreeImport={mount(host,{data,send,esc,table,claimRequestId,settleRequestId}){
 if(data.user.role!=='owner')return;
 host.innerHTML=`<section class="danger-box" aria-labelledby="officeThreeImportTitle"><h3 id="officeThreeImportTitle">办公室三 · 双表联合导入</h3><p>同时读取“账号情况表”和“工作进度表”，按真实抖音号核对账号与发布位置。只新增可以安全确定的资料；原有资料不覆盖、不删除，其他办公室和待分配资料不调整。</p><p class="permission-note">请分别上传两张表的 CSV 或单工作表 Excel 文件（合计不超过 8MB）。密码、远程控制信息不导入；发布数量、颜色和停发栏不转换为每日发布记录。手机编号仅为来源参考，不证明设备已登录。</p><form id="officeThreeImportForm"><div class="work-filters"><label>账号情况表<input id="officeThreeAccountsFile" type="file" accept=".csv,.xlsx" required></label><label>工作进度表<input id="officeThreeProgressFile" type="file" accept=".csv,.xlsx" required></label><button id="officeThreePreview" type="submit" class="button primary">联合预览（不导入）</button></div></form><p id="officeThreeImportError" role="alert"></p><div id="officeThreeImportResult" aria-live="polite"></div></section>`;
 const get=id=>host.querySelector('#'+id);
 const sourceMode=document.createElement('label');sourceMode.className='work-filters';sourceMode.innerHTML='读取方式<select id="officeThreeSourceMode"><option value="files">上传两份表格文件</option><option value="text">粘贴两份 CSV 文本</option></select>';get('officeThreeImportForm').prepend(sourceMode);
 const textPanel=document.createElement('div');textPanel.hidden=true;textPanel.innerHTML='<label>账号情况表 CSV 文本<textarea id="officeThreeAccountsText" rows="6" style="width:100%" spellcheck="false" autocomplete="off"></textarea></label><label>工作进度表 CSV 文本<textarea id="officeThreeProgressText" rows="6" style="width:100%" spellcheck="false" autocomplete="off"></textarea></label><p class="permission-note">粘贴时保留标题行，仅接收 CSV 文本；不含密码和远程访问资料。预览时与文件上传使用相同的服务端校验。</p>';sourceMode.after(textPanel);
 let preview=null,busy=false,page=1,nicknamePage=1;
 const readFile=file=>new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve({filename:file.name,content:String(reader.result).split(',')[1]});reader.onerror=()=>reject(Error('读取文件失败，请重新选择'));reader.readAsDataURL(file);});
 const lock=value=>{busy=value;get('officeThreePreview').disabled=value;for(const id of ['officeThreeSourceMode','officeThreeAccountsFile','officeThreeProgressFile','officeThreeAccountsText','officeThreeProgressText'])get(id).disabled=value;};
 const invalidate=()=>{if(busy)return;preview=null;get('officeThreeImportResult').replaceChildren();get('officeThreeImportError').textContent='';};
 get('officeThreeAccountsFile').onchange=invalidate;get('officeThreeProgressFile').onchange=invalidate;
 get('officeThreeAccountsText').oninput=invalidate;get('officeThreeProgressText').oninput=invalidate;
 get('officeThreeSourceMode').onchange=()=>{const textMode=get('officeThreeSourceMode').value==='text';textPanel.hidden=!textMode;for(const id of ['officeThreeAccountsFile','officeThreeProgressFile']){get(id).required=!textMode;get(id).parentElement.hidden=textMode;}for(const id of ['officeThreeAccountsText','officeThreeProgressText'])get(id).required=textMode;invalidate();};
 const textFile=(filename,content)=>{const bytes=new TextEncoder().encode(content);let binary='';for(let offset=0;offset<bytes.length;offset+=16384)binary+=String.fromCharCode(...bytes.subarray(offset,offset+16384));return {filename,content:btoa(binary)};};
 const titles={source_accounts:'来源账号资料行',source_computers:'来源发布电脑',new_accounts:'新增账号',reused_accounts:'保留并复用账号',new_computers:'新增发布电脑',new_devices:'新增手机编号',new_layouts:'新增发布格',conflicts:'冲突 / 待核对项目'};
 const summaryText=summary=>Object.entries(titles).map(([key,label])=>label+' '+Number(summary?.[key]||0)).join('；');
 function showRows(){
  if(!preview)return;
  const rows=preview.rows||[],pages=Math.max(1,Math.ceil(rows.length/50));page=Math.min(page,pages);
  get('officeThreePreviewRows').innerHTML=table(['类别','来源行 / 位置','处理方式','资料','说明'],rows.slice((page-1)*50,page*50).map(row=>`<tr><td>${esc({account:'账号',computer:'电脑',device:'手机编号',layout:'发布位置'}[row.kind]||row.kind)}</td><td>${esc(row.row??'')}</td><td>${esc({create:'新增',reuse:'保留 / 复用',conflict:'待核对',skip:'不导入',same:'保持不变'}[row.action]||row.action)}</td><td>${esc(row.label)}</td><td>${esc(row.reason)}</td></tr>`));
  get('officeThreePageLabel').textContent=`第 ${page} / ${pages} 页，共 ${rows.length} 项`;
  get('officeThreePrev').disabled=page<=1;get('officeThreeNext').disabled=page>=pages;
 }
 function showNicknameSuggestions(){
  const rows=preview?.missing_nickname_suggestions||[],section=get('officeThreeNicknameSuggestions');
  if(!rows.length){section.hidden=true;return;}
  section.hidden=false;
  const pages=Math.max(1,Math.ceil(rows.length/50));nicknamePage=Math.min(Math.max(1,nicknamePage),pages);
  section.innerHTML=`<h4>缺失昵称核对 · 仅预览，不会自动补写</h4><p>有 ${rows.length} 个办公室三账号的旧昵称为空或系统占位，但来源表有昵称。以下只按唯一真实抖音号匹配，不按手机序号、手机号或门头猜测。确认安全新增也不会修改这些原账号；正式补全需另行核对并确认。</p><div class="preview-scroll">${table(['来源行','真实抖音号','工作台当前昵称','来源表昵称'],rows.slice((nicknamePage-1)*50,nicknamePage*50).map(row=>`<tr><td>${esc(row.row??'')}</td><td>${esc(row.douyin_id)}</td><td>${esc(row.current_name||'（空白）')}</td><td>${esc(row.source_name)}</td></tr>`))}</div><div class="auth-actions"><button id="officeThreeNicknamePrev" type="button" class="button">上一页昵称</button><span>第 ${nicknamePage} / ${pages} 页，共 ${rows.length} 个账号</span><button id="officeThreeNicknameNext" type="button" class="button">下一页昵称</button></div>`;
  get('officeThreeNicknamePrev').disabled=nicknamePage<=1;get('officeThreeNicknameNext').disabled=nicknamePage>=pages;
  get('officeThreeNicknamePrev').onclick=()=>{nicknamePage--;showNicknameSuggestions();};get('officeThreeNicknameNext').onclick=()=>{nicknamePage++;showNicknameSuggestions();};
 }
 function showPreview(){
  const result=get('officeThreeImportResult');
  result.innerHTML=`<h4>导入预览 · 目标固定为办公室三</h4><p id="officeThreePreviewSummary">${esc(summaryText(preview.summary))}</p><p>“保留 / 复用”不会修改原账号字段；冲突和无法唯一匹配的项目不会自动写入。预览后资料有变化时必须重新预览。</p>${(preview.warnings||[]).length?'<details><summary>查看来源说明与警告（'+preview.warnings.length+'）</summary><ul>'+preview.warnings.map(item=>'<li>'+esc(item)+'</li>').join('')+'</ul></details>':''}<div id="officeThreePreviewRows" class="preview-scroll"></div><div class="auth-actions"><button id="officeThreePrev" type="button" class="button">上一页</button><span id="officeThreePageLabel"></span><button id="officeThreeNext" type="button" class="button">下一页</button></div><form id="officeThreeApplyForm"><label>输入“导入办公室三”确认仅执行安全新增<input id="officeThreeConfirmation" autocomplete="off" required></label><button id="officeThreeApply" type="submit" class="button primary">确认安全新增，保留冲突待核对</button></form>`;
  const scopeNote=document.createElement('p');scopeNote.textContent='冲突按账号、手机编号、电脑、发布格分别计项，不等于缺少的账号数；来源账号资料行也不等于可导入账号数。';get('officeThreePreviewSummary').after(scopeNote);
  const nicknameSection=document.createElement('section');nicknameSection.id='officeThreeNicknameSuggestions';get('officeThreeApplyForm').before(nicknameSection);showNicknameSuggestions();
  get('officeThreePrev').onclick=()=>{page--;showRows();};get('officeThreeNext').onclick=()=>{page++;showRows();};showRows();
  const candidates=preview.scope_candidates||[];
  if(candidates.length){
   const section=document.createElement('form');section.id='officeThreeScopeForm';
   section.innerHTML=`<h4>待分配发布电脑 · 单独确认归属</h4><p>来源表里的以下 ${candidates.length} 台电脑已存在，但尚未分配办公室。只有确认归属后，它们才能参与办公室三的发布安排。此操作只调整列出的电脑归属和相应可见范围；原业务字段、账号归属、发布格行及批次不改，不会导入账号。</p><p id="officeThreeScopeCandidates">${candidates.map(item=>`<span data-computer-scope-id="${esc(item.id)}">${esc(item.id)}</span>`).join('、')}</p><label>核对上方电脑后，输入“仅分配电脑到办公室三”<input id="officeThreeScopeConfirmation" required autocomplete="off"></label><button type="submit" id="officeThreeScopeApply" class="button">仅确认这些电脑的归属，之后重新预览</button>`;
   get('officeThreeApplyForm').before(section);
   section.onsubmit=async event=>{
    event.preventDefault();if(busy||!preview)return;
    if(get('officeThreeScopeConfirmation').value!=='仅分配电脑到办公室三'){get('officeThreeImportError').textContent='请输入“仅分配电脑到办公室三”确认';return;}
    const key='office-three-import-computer-scopes:'+preview.preview_id,button=get('officeThreeScopeApply');button.disabled=true;get('officeThreeApply').disabled=true;lock(true);get('officeThreeImportError').textContent='';
    try{
     const response=await send('/api/workspace/office-three-import-computer-scopes',{office_id:'three',preview_id:preview.preview_id,computer_ids:candidates.map(item=>item.id),confirmation:'仅分配电脑到办公室三',request_id:claimRequestId(key)});settleRequestId(key);
     const checks=response.verification||{};preview=null;
     result.innerHTML=`<h4>电脑归属确认完成</h4><p id="officeThreeScopeApplied">已将 ${(response.assigned_computer_ids||[]).length} 台来源电脑归入办公室三；尚未导入账号或发布安排。</p><p id="officeThreeScopeVerification">${checks.business_fields_preserved===true&&checks.only_requested_computers_assigned===true&&checks.other_entity_scopes_preserved===true?'校验通过：仅指定电脑归属变化；原业务字段、账号归属、旧发布格和全局批次保留。':'未收到完整的归属保留校验，请核对后再继续。'}</p><p>原预览已失效。请点击上方“联合预览（不导入）”，重新核对新增项与冲突，再确认导入。</p>`;
    }catch(error){settleRequestId(key,error);get('officeThreeImportError').textContent=error.message||'归属确认未完成';button.disabled=false;get('officeThreeApply').disabled=false;}
    finally{lock(false);}
   };
  }
  get('officeThreeApplyForm').onsubmit=async event=>{
   event.preventDefault();if(busy||!preview)return;
   if(get('officeThreeConfirmation').value!=='导入办公室三'){get('officeThreeImportError').textContent='请输入“导入办公室三”确认';return;}
   const button=get('officeThreeApply');button.disabled=true;lock(true);get('officeThreeImportError').textContent='';
   const requestKey='office-three-import-apply:'+preview.preview_id;
   try{
    const response=await send('/api/workspace/office-three-import-apply',{office_id:'three',preview_id:preview.preview_id,confirmation:'导入办公室三',request_id:claimRequestId(requestKey)});
    settleRequestId(requestKey);const checks=response.verification||{};
    result.insertAdjacentHTML('afterbegin',`<h4>导入完成 · 办公室三</h4><p id="officeThreeAppliedSummary">${esc(summaryText(response.summary||response))}</p><p id="officeThreeVerification">${checks.original_records_preserved===true&&checks.other_offices_unchanged===true&&checks.active_batch_unchanged===true?'校验通过：本次导入前的原有资料保留，其他办公室与待分配资料未改，全局发布批次未变。':'请刷新资料并核对导入结果；未收到完整保留校验。'}</p><p>冲突项仍未导入，请根据下方预览记录人工核对。没有自动确认设备登录、开启采集或生成每日发布记录。</p><a class="button" href="/?office=three&page=accounts">查看办公室三账号</a>`);get('officeThreeApplyForm').remove();get('officeThreeScopeForm')?.remove();
   }catch(error){settleRequestId(requestKey,error);get('officeThreeImportError').textContent=error.message||'导入未完成';button.disabled=false;}
   finally{lock(false);}
  };
 }
 get('officeThreeImportForm').onsubmit=async event=>{
  event.preventDefault();if(busy)return;
  const textMode=get('officeThreeSourceMode').value==='text';
  const accounts=textMode?get('officeThreeAccountsText').value:get('officeThreeAccountsFile').files[0],progress=textMode?get('officeThreeProgressText').value:get('officeThreeProgressFile').files[0];
  if(!accounts||!progress){get('officeThreeImportError').textContent='请同时提供账号情况表和工作进度表';return;}
  const totalSize=textMode?new TextEncoder().encode(accounts).length+new TextEncoder().encode(progress).length:accounts.size+progress.size;
  if(totalSize>8*1024*1024){get('officeThreeImportError').textContent='两份文件合计不能超过8MB';return;}
  preview=null;lock(true);get('officeThreeImportResult').textContent='正在联合核对账号、手机编号和发布位置…';get('officeThreeImportError').textContent='';
  try{const files=textMode?[textFile('accounts.csv',accounts),textFile('progress.csv',progress)]:await Promise.all([readFile(accounts),readFile(progress)]);preview=await send('/api/workspace/office-three-import-preview',{office_id:'three',accounts_file:files[0],progress_file:files[1]});page=1;nicknamePage=1;showPreview();}
  catch(error){get('officeThreeImportResult').replaceChildren();get('officeThreeImportError').textContent=error.message||'预览失败';}
  finally{lock(false);}
 };
}};
