function loginDestination(search=location.search){const next=new URLSearchParams(search).get('next');return ['/?office=three','/collection.html'].includes(next)?next:'/workspace.html';}
let setupMode=false;
let needsSetupKey=false;
const input=id=>document.getElementById(id);
async function initializeLogin(){
 try{
  const response=await fetch('/api/auth/me');if(!response.ok)throw Error('登录服务暂时不可用');const state=await response.json();
  if(state.user){location.replace(loginDestination());return;}
  setupMode=!state.initialized;
  needsSetupKey=setupMode&&state.setup_key_required===true;
  input('setupKeyField').hidden=!needsSetupKey;
  input('loginSetupKey').required=needsSetupKey;
  input('setupName').hidden=!setupMode;input('confirmField').hidden=!setupMode;input('loginConfirm').required=setupMode;
  input('loginTitle').textContent=setupMode?'创建你的总管理员账号':'登录工作台';
  input('loginSubmit').textContent=setupMode?'创建并进入工作台':'登录';
  input('loginPassword').autocomplete=setupMode?'new-password':'current-password';
  input('loginHint').textContent=setupMode?'首次设置后，只有这个总账号可以管理人员和分配权限。请保管好登录名和密码。':'每人独立登录，共用一份工作数据。';
  if(needsSetupKey)input('loginHint').textContent='请输入本机启动时配置的首次设置密钥，然后创建总管理员。';
  else if(setupMode&&!state.can_setup){input('loginForm').hidden=true;input('loginHint').textContent='尚未设置总管理员。请在服务器本机授权完成首次创建。';}
 }catch(error){input('loginError').textContent=error.message;}
}
input('loginForm').onsubmit=async event=>{
 event.preventDefault();input('loginError').textContent='';
 if(setupMode&&input('loginPassword').value!==input('loginConfirm').value){input('loginError').textContent='两次密码不一致';return;}
 input('loginSubmit').disabled=true;
 try{
  if(needsSetupKey){
   const authorization=await fetch('/api/auth/desktop-setup',{method:'POST',headers:{'Content-Type':'application/json','X-Worker-Token':input('loginSetupKey').value},body:'{}'});
   if(!authorization.ok){const denied=await authorization.json();throw Error(denied.error||'首次设置授权失败');}
   needsSetupKey=false;
   input('loginSetupKey').value='';
   input('loginSetupKey').required=false;
   input('setupKeyField').hidden=true;
  }
  const response=await fetch(setupMode?'/api/auth/setup':'/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:input('loginUsername').value,name:input('loginName').value,password:input('loginPassword').value})});
  const result=await response.json();if(!response.ok)throw Error(result.error||'登录失败');input('loginPassword').value='';input('loginConfirm').value='';location.replace(loginDestination());
 }catch(error){input('loginError').textContent=error.message;}finally{input('loginSubmit').disabled=false;}
};
initializeLogin();
