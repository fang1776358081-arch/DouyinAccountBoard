// Actual import UI with synthetic DOM/data only. No spreadsheets or network.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('static/office-three-import.js','utf8');
const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function fixture(response,role='owner'){
 const nodes=new Map(),calls=[];let counter=0;
 function node(initialId=''){
  let id='',html='';
  const result={value:'',disabled:false,hidden:false,textContent:'',required:false,parentElement:{hidden:false},files:[],
   get id(){return id;},set id(next){id=next;if(id)nodes.set(id,this);},
   get innerHTML(){return html;},set innerHTML(next){html=String(next);for(const match of html.matchAll(/\bid="([^"]+)"/g))node(match[1]);},
   querySelector(selector){return nodes.get(selector.slice(1))||null;},
   after(){},before(){},prepend(){},replaceChildren(){html='';},remove(){if(id)nodes.delete(id);},
   insertAdjacentHTML(position,next){html=position==='afterbegin'?next+html:html+next;}};
  result.id=initialId;return result;
 }
 const host=node('syntheticHost'),context={window:{},document:{createElement:()=>node()},TextEncoder,
  btoa:binary=>Buffer.from(binary,'binary').toString('base64')};
 vm.createContext(context);vm.runInContext(source,context,{filename:'office-three-import.js'});
 context.window.OfficeThreeImport.mount(host,{data:{user:{role}},esc:escape,
  table:(headers,rows)=>'<table><thead><tr>'+headers.map(h=>'<th>'+escape(h)+'</th>').join('')+'</tr></thead><tbody>'+rows.join('')+'</tbody></table>',
  claimRequestId:()=>`synthetic-${++counter}`,settleRequestId(){},
  async send(path,body){calls.push({path,body:structuredClone(body)});if(path.endsWith('-preview'))return structuredClone(response);
   return {ok:true,summary:response.summary,verification:{original_records_preserved:true,other_offices_unchanged:true,active_batch_unchanged:true}};}});
 return {host,nodes,calls,async preview(){
  nodes.get('officeThreeSourceMode').value='text';nodes.get('officeThreeSourceMode').onchange();
  nodes.get('officeThreeAccountsText').value='synthetic,accounts';nodes.get('officeThreeProgressText').value='synthetic,progress';
  await nodes.get('officeThreeImportForm').onsubmit({preventDefault(){}});
 }};
}
const suggestions=Array.from({length:53},(_,index)=>({row:index+2,account_id:`synthetic-${index}`,
 douyin_id:`synthetic_handle_${index}`,current_name:index?'昵称待补充（手机9）':'',
 source_name:index===0?'<script>synthetic</script>':`合成昵称${index}`}));
const response={preview_id:'a'.repeat(32),summary:{source_accounts:60,source_computers:3,new_accounts:2,reused_accounts:53,
 new_computers:0,new_devices:2,new_layouts:0,conflicts:17},rows:[],warnings:[],scope_candidates:[],
 missing_nickname_suggestions:suggestions,missing_nickname_suggestion_count:53};
let checks=0;function check(name,fn){fn();checks++;}
(async()=>{
 const test=fixture(response);await test.preview();
 check('preview is read-only and preserves the established endpoint',()=>{
  assert.equal(test.calls.length,1);assert.equal(test.calls[0].path,'/api/workspace/office-three-import-preview');
  assert.equal(test.nodes.get('officeThreeImportError').textContent,'');
 });
 const section=test.nodes.get('officeThreeNicknameSuggestions');
 check('all suggestions are pageable, not silently truncated',()=>{
  assert.equal(section.hidden,false);assert.match(section.innerHTML,/共 53 个账号/);assert.match(section.innerHTML,/synthetic_handle_49/);
  assert.doesNotMatch(section.innerHTML,/synthetic_handle_50/);assert.equal(test.nodes.get('officeThreeNicknamePrev').disabled,true);
  test.nodes.get('officeThreeNicknameNext').onclick();assert.match(section.innerHTML,/第 2 \/ 2 页/);assert.match(section.innerHTML,/synthetic_handle_52/);
  assert.equal(test.nodes.get('officeThreeNicknameNext').disabled,true);test.nodes.get('officeThreeNicknamePrev').onclick();
  assert.match(section.innerHTML,/第 1 \/ 2 页/);
 });
 check('nickname suggestions explain the preservation boundary and escape cells',()=>{
  assert.match(section.innerHTML,/仅预览，不会自动补写/);assert.match(section.innerHTML,/正式补全需另行核对并确认/);
  assert.match(section.innerHTML,/&lt;script&gt;synthetic&lt;\/script&gt;/);assert.doesNotMatch(section.innerHTML,/<script>/);
  assert.match(section.innerHTML,/（空白）/);assert.doesNotMatch(section.innerHTML,/<input|<button[^>]+(?:submit|Apply)/);
  assert.match(test.nodes.get('officeThreeImportResult').innerHTML,/来源账号资料行 60/);
 });
 test.nodes.get('officeThreeConfirmation').value='导入办公室三';
 await test.nodes.get('officeThreeApplyForm').onsubmit({preventDefault(){}});
 check('normal apply sends no proposed nickname updates',()=>{
  assert.equal(test.calls.length,2);assert.equal(test.calls[1].path,'/api/workspace/office-three-import-apply');
  assert.deepEqual(Object.keys(test.calls[1].body).sort(),['confirmation','office_id','preview_id','request_id']);
  assert.equal(test.calls[1].body.office_id,'three');assert.match(section.innerHTML,/不会修改这些原账号/);
 });
 const legacy=fixture({...response,missing_nickname_suggestions:undefined,missing_nickname_suggestion_count:undefined});await legacy.preview();
 check('older preview responses safely hide the optional suggestions',()=>{
  assert.equal(legacy.nodes.get('officeThreeNicknameSuggestions').hidden,true);assert.equal(legacy.calls.length,1);
  assert.equal(legacy.nodes.get('officeThreeImportError').textContent,'');
 });
 const empty=fixture({...response,missing_nickname_suggestions:[],missing_nickname_suggestion_count:0});await empty.preview();
 check('empty suggestions create no write workflow',()=>assert.equal(empty.nodes.get('officeThreeNicknameSuggestions').hidden,true));
 check('employees cannot mount the owner-only import',()=>{
  const employee=fixture(response,'employee');assert.equal(employee.host.innerHTML,'');assert.equal(employee.calls.length,0);
 });
 console.log(`PASS: ${checks} read-only nickname preview UI groups (synthetic only)`);
})().catch(error=>{console.error(error);process.exitCode=1;});
