/* Read-only issues from already-authorized rows; no requests or persistent source storage. */
window.AccountReview=(()=>{
 const labels={missing_name:'昵称待补充',missing_id:'抖音号缺失',invalid_id:'抖音号待核对',duplicate_id:'可见范围内抖音号重复',source_blank:'来源昵称为空',unmatched:'来源未匹配',conflict:'来源冲突 / 待核对',source_skipped:'来源未导入'};
 const text=value=>value==null?'':String(value);
 const own=(value,key)=>Object.prototype.hasOwnProperty.call(value,key);
 const missingName=value=>value==null||value===''||/^昵称待补充(?:（手机[1-9][0-9]{0,5}）)?$/.test(value);
 let recentImport=null;
 function scope(board){
  const user=board?.user||{},scoped=!!user.scope_mode;
  const ids=new Set((board?.offices||[]).map(row=>text(row.id)));
  if(scoped&&user.scope_mode!=='all')for(const id of ids)if(!(user.office_ids||[]).includes(id))ids.delete(id);
  return {scoped,ids,pending:!scoped||user.scope_mode==='all'};
 }
 function offices(account,board){
  const state=scope(board);
  let ids;
  if(Array.isArray(account.office_ids))ids=account.office_ids.map(text);
  else if(state.scoped)return [];
  else{
   // The old local-only grouping remains display metadata, never an identity match.
   const id=text(account.phone_device_id);
   ids=/^office3:\d+$/.test(id)||(!id&&account.office_group==='three')?['three']:/^[1-9]\d*$/.test(id)?[Number(id)>=301&&Number(id)<=700?'two':'one']:[];
  }
  return ids.length?[...new Set(ids.filter(id=>!state.scoped||state.ids.has(id)))]:state.pending?['pending']:[];
 }
 function authorizedAccounts(board){return (board?.accounts||[]).filter(account=>offices(account,board).length);}
 function issue(kind,fields){return {key:'',kind,kind_label:labels[kind]||kind,office_ids:[],account_id:'',douyin_id:'',current_name:'',source_name:'',source_row:'',source_label:'当前工作台资料',reason:'',...fields};}
 function fromBoard(board){
  const accounts=authorizedAccounts(board),byHandle=new Map(),rows=[];
  for(const account of accounts)if(typeof account.douyin_id==='string'&&account.douyin_id&&!/\s/.test(account.douyin_id)&&account.douyin_id.length<=100){
   if(!byHandle.has(account.douyin_id))byHandle.set(account.douyin_id,[]);
   byHandle.get(account.douyin_id).push(account);
  }
  for(const account of accounts){
   const base={office_ids:offices(account,board),account_id:text(account.id),douyin_id:text(account.douyin_id),current_name:text(account.name)};
   const add=(kind,reason)=>rows.push(issue(kind,{...base,key:'account:'+base.account_id+':'+kind,reason}));
   if(own(account,'name')&&missingName(account.name))add('missing_name','系统名称为空或为系统占位名；请核对可靠来源后补全，不按手机编号猜测。');
   // Older summary responses omit this property. Omission means unknown, not blank.
   if(!own(account,'douyin_id'))continue;
   if(account.douyin_id==null||account.douyin_id==='')add('missing_id','当前资料没有抖音号，无法与来源按标识核对；请确认原账号身份。');
   else if(typeof account.douyin_id!=='string'||/\s/.test(account.douyin_id)||account.douyin_id.length>100)add('invalid_id','抖音号格式需要人工核对；本清单不转换、截取或修改原始标识。');
   else if(byHandle.get(account.douyin_id)?.length>1)add('duplicate_id','当前可见范围有多个相同抖音号记录；请管理员核对，不自动合并。');
  }
  return rows;
 }
 function identityComplete(board){return authorizedAccounts(board).every(account=>own(account,'douyin_id'));}
 function fromImport(preview){
  if(preview?.office_id!=='three'||!Array.isArray(preview.rows))return [];
  const names=preview.mode==='fill_missing_nicknames',rows=[];
  preview.rows.forEach((row,index)=>{
   let kind;
   if(row.action==='conflict')kind='conflict';
   else if(row.action==='unmatched')kind='unmatched';
   else if(row.action==='skip')kind=names?'source_blank':'source_skipped';
   else return;
   rows.push(issue(kind,{key:'import:'+text(preview.preview_id)+':'+index,office_ids:['three'],
    douyin_id:text(row.douyin_id),current_name:text(row.current_name),source_name:text(row.source_name),
    source_row:text(row.row),source_label:names?'账号情况表 · 昵称核对':'双表预览 · '+({account:'账号',device:'手机编号',computer:'电脑',layout:'发布位置'}[row.kind]||'来源项目'),reason:text(row.reason)}));
  });
  return rows;
 }
 function binding(board){
  const user=board?.user;
  if(!user||user.id==null||user.role!=='owner')return null;
  const state=scope(board);if(state.scoped&&!state.ids.has('three'))return null;
  return JSON.stringify([text(user.id),user.role,user.authorization_version??null,user.scope_mode??null,
   [...state.ids].sort(),[...(user.office_ids||[])].sort(),[...(user.permissions||[])].sort()]);
 }
 function captureImport(preview,board){
  recentImport=null;
  const key=binding(board);if(!key||preview?.office_id!=='three'||!Array.isArray(preview.rows))return false;
  recentImport={binding:key,rows:fromImport(preview)};return true;
 }
 function clearImport(){recentImport=null;}
 function importRows(board){
  const key=binding(board);
  if(!key||recentImport?.binding!==key){recentImport=null;return [];}
  return recentImport.rows.map(row=>({...row,office_ids:[...row.office_ids]}));
 }
 function toCSV(rows){
  const cell=value=>{let valueText=text(value);if(/^[\s\uFEFF]*[=+\-@]/.test(valueText)||/^[\t\r\n]/.test(valueText))valueText="'"+valueText;return '"'+valueText.replace(/"/g,'""')+'"';};
  const columns=[['kind_label','问题类别'],['office_ids','办公室标识'],['account_id','内部账号标识'],['douyin_id','原始抖音号'],['current_name','当前昵称'],['source_name','来源昵称'],['source_row','来源行'],['source_label','来源'],['reason','核对原因']];
  return '\uFEFF'+[columns.map(([,title])=>title),...rows.map(row=>columns.map(([key])=>key==='office_ids'?(row.office_ids||[]).join('、'):row[key]))].map(row=>row.map(cell).join(',')).join('\r\n');
 }
 return Object.freeze({fromBoard,fromImport,identityComplete,captureImport,clearImport,importRows,toCSV});
})();
