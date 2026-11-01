// Local-only actual-static visual fixture. No browser automation or real data.
// Before is a fixed local Git snapshot; after assets are read live by Python.
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),crypto=require('node:crypto');
const {spawn,execFileSync}=require('node:child_process');
const assert=require('node:assert/strict');
const ROOT=path.resolve(__dirname,'..'),BASELINE='4597858e258d4d20f3fd83ec5c97eb3bc1ac3be3';
const {fixture}=require(path.join(ROOT,'test_performance_ui.cjs'));
const args=process.argv.slice(2),options={};
for(let i=0;i<args.length;i++){if(args[i]==='--smoke')options.smoke=true;else if(['--root','--python'].includes(args[i]))options[args[i].slice(2)]=args[++i];else throw Error('Unsupported preview option');}
assert.ok(options.root&&options.python,'Use --root <new .synthetic-tests child> --python <absolute python.exe> [--smoke]');
const runRoot=path.resolve(ROOT,options.root),allowedRoot=path.join(ROOT,'.synthetic-tests')+path.sep;
assert.ok(runRoot.startsWith(allowedRoot)&&runRoot!==allowedRoot.slice(0,-1));
assert.ok(path.isAbsolute(options.python)&&path.basename(options.python).toLowerCase()==='python.exe');
const assetName=/^[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:html|js|css|svg|png|ico)$/;
const tree=execFileSync('git',['ls-tree','-r','--name-only',BASELINE,'--','static'],{cwd:ROOT,encoding:'utf8',windowsHide:true});
const before={};for(const entry of tree.trim().split(/\r?\n/)){const name=entry.replace(/^static\//,'');if(assetName.test(name))before[name]=execFileSync('git',['show',BASELINE+':'+entry],{cwd:ROOT,windowsHide:true,maxBuffer:2*1024*1024}).toString('base64');}
assert.ok(before['index.html']&&before['phone-cards.js']&&before['card-filters.js']);

function syntheticBoard(){
 const board=fixture(360),names={one:'合成办公室一',two:'合成办公室二',three:'合成办公室三',pending:'待分配'};
 const office=i=>i<60?'three':i<90?'one':i<110?'two':'pending';
 board.date=board.today='2026-11-02';
 board.user={id:'synthetic-card-preview-user',username:'synthetic-preview',name:'合成预览人员',role:'owner',scope_mode:'all',office_ids:['one','two','three'],csrf:'synthetic-preview-only',permissions:['card_create','card_import','card_manage','card_tasks','account_progress','device_manage','account_manage','account_status','publish']};
 board.offices=['one','two','three'].map(id=>({id,name:names[id]}));
 board.phone_cards.statuses=['待确认','正常','欠费','停机','停用','待注销','已注销'];
 board.phone_cards.carriers=['待确认','中国移动','中国联通','中国电信','中国广电','民生电信','长城移动','其他'];
 board.phone_cards.ownerships=['待确认','自有','借用','代管'];board.phone_cards.code_states=['无待办','待接码','已完成','暂无法接码'];board.phone_cards.return_states=['待确认','不需归还','待归还','已归还'];
 board.phone_cards.importable=0;const units=[];
 board.phone_cards.cards.forEach((card,i)=>{
  const group=office(i),number=i%30+1,deviceId='preview-phone:'+group+':'+number,unitId='synthetic-preview-unit-'+i;
  Object.assign(card,{phone:'000'+String(i+1).padStart(8,'0'),label:'合成卡 '+String(i+1).padStart(3,'0'),office_ids:group==='pending'?[]:[group],
   status:i%11===10?'':board.phone_cards.statuses[i%7],carrier:board.phone_cards.carriers[1+i%7],custom_carrier:i%7===6?'合成虚拟运营商':'',
   owner:'合成卡主'+(i%6+1),keeper:'合成负责人'+(i%4+1),ownership:board.phone_cards.ownerships[i%4],
   code_state:board.phone_cards.code_states[i%4],code_contact:'合成接码联系人'+(i%3+1),code_note:'合成接码记录（不保存）',return_state:board.phone_cards.return_states[(i+2)%4],
   return_due:i%4===0?'2026-11-07':'',returned_on:i%4===1?'2026-10-28':'',expires_on:i%3===0?'2026-11-03':'',
   location:i%4===3?'':String(number),note:'合成备注：用于同数据页面对比，不是真实手机卡。',source_note:i%9===0?'合成来源：卡主标注不一致，待核对':'合成来源原文（仅预览）'});
  if(i%4===0)card.placement={unit_id:unitId,slot:1};else if(i%4===1)card.placement={storage:'合成卡盒第'+(i%5+1)+'排'};
  const device=board.phone_devices.devices[i];Object.assign(device,{id:deviceId,name:names[group]+' · '+number+'号手机',office_ids:card.office_ids,current_unit:{id:unitId}});
  units.push({id:unitId,device_id:deviceId,label:names[group]+' · 合成实物'+number,active:true,status:'在用',generation:1});
 });
 board.phone_cards.cards[7].note='合成较长备注，用于检查换行、行高、摘要和完整内容入口。'.repeat(7);
 board.phone_cards.cards[5].label='合成较长卡名称，检查窄屏是否正确截断或换行。'.repeat(3);
 board.accounts=board.accounts.filter((account,index)=>{
  const i=Math.floor(index/3),card=board.phone_cards.cards[i],device=board.phone_devices.devices[i];
  Object.assign(account,{name:'合成关联账号 '+String(index+1).padStart(3,'0'),registration_phone:card.phone,office_ids:card.office_ids,phone_device_id:device.id,phone_device_name:device.name,
   card_stability:i%6===0?'未稳定':'已稳定',password_state:i%8===0?'未修改':'已修改',status:i%10===0?'封禁':'正常'});
  return i%5!==0;
 });
 board.device_workflow={enabled:true,units,placements:[],access:[],cases:[],todos:[],events:[]};
 board.collection={enabled:false,auto_enabled:false,worker_status:'合成预览：采集关闭',linked:0,waiting:0,done:0,failed:0};board.backup={enabled:false};
 board.publishing_layout={enabled:false,cells:[]};
 for(const card of board.phone_cards.cards)board.record_versions['card:'+card.id]='synthetic-card-version-'+card.id;
 return board;
}
const board=syntheticBoard(),boardJSON=JSON.stringify(board),fingerprint=crypto.createHash('sha256').update(boardJSON).digest('hex');
const child=spawn(options.python,['-B',path.join(__dirname,'card_ui_preview_server.py'),'--root',runRoot],{cwd:ROOT,windowsHide:true,stdio:['pipe','pipe','pipe'],env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',PYTHONIOENCODING:'utf-8'}});
let buffer='',ready,stopping=false,ended=false;const pending=[];
child.stdout.setEncoding('utf8');child.stdout.on('data',chunk=>{buffer+=chunk;for(;;){const at=buffer.indexOf('\n');if(at<0)break;const raw=buffer.slice(0,at);buffer=buffer.slice(at+1);try{const message=JSON.parse(raw);if(message.kind==='ready'){ready=message;console.log(JSON.stringify(message));for(const fn of pending.splice(0))fn(message);}else if(message.kind==='error'){console.error('Preview fixture failed ['+message.type+']');void stop(1);}}catch{console.error('Invalid preview fixture protocol');void stop(1);}}});
child.stderr.on('data',()=>{});child.on('error',()=>{console.error('Unable to start local preview fixture');process.exitCode=1;});
child.on('exit',code=>{ended=true;if(code&&process.exitCode===undefined)process.exitCode=code;});
child.stdin.write(JSON.stringify({baseline:BASELINE,before,candidate:path.join(ROOT,'static'),board_json:boardJSON,fingerprint})+'\n');
function get(base,route,method='GET',headers={}){return new Promise((resolve,reject)=>{
 const origin=new URL(base);assert.equal(origin.protocol,'http:');assert.equal(origin.hostname,'127.0.0.1');
 const req=http.request({hostname:'127.0.0.1',port:Number(origin.port),path:route,method,headers,agent:false},response=>{const chunks=[];response.on('data',chunk=>chunks.push(chunk));response.on('end',()=>resolve({status:response.statusCode,body:Buffer.concat(chunks),headers:response.headers}));});req.on('error',()=>reject(Error('Loopback preview probe failed')));req.setTimeout(8000,()=>req.destroy());req.end();
});}
async function smoke(meta){
 const results=[];
 for(const key of ['before_url','after_url']){
  const base=new URL(meta[key]).origin;const page=await get(base,'/?page=cards');assert.equal(page.status,200);assert.ok(page.body.includes(Buffer.from('cardsBody')));assert.match(page.headers['content-security-policy'],/connect-src 'self'/);
  for(const asset of ['/styles.css','/phone-cards.js','/card-filters.js'])assert.equal((await get(base,asset)).status,200);
  const loaded=await get(base,'/api/board');assert.equal(loaded.status,200);assert.equal(crypto.createHash('sha256').update(loaded.body).digest('hex'),fingerprint);const value=JSON.parse(loaded.body);assert.equal(value.phone_cards.cards.length,120);assert.equal(value.accounts.length,288);
  assert.equal((await get(base,'/api/auth/me')).status,200);assert.equal((await get(base,'/api/board-updates?cursor=10&limit=200')).status,200);assert.equal((await get(base,'/api/record-versions?keys=card:1')).status,200);
  assert.equal((await get(base,'/api/board','GET',{Referer:base+'/?page=cards&preview_fail=1'})).status,503);
  assert.equal((await get(base,'/api/board','GET',{Referer:base+'/?page=cards&preview_delay=30'})).status,200);
  assert.equal((await get(base,'/api/batch-edit','POST')).status,405);assert.equal((await get(base,'/data/board.db')).status,404);
  results.push({variant:key.startsWith('before')?'before':'after',get_static:true,get_api:true,data_sha256:fingerprint,post_status:405,protected_path_status:404,error_status:503});
 }
 const evidence={passed:true,kind:'read-only-local-actual-static-preview-smoke',baseline:BASELINE,cards:120,accounts:288,data_sha256:fingerprint,results};
 fs.writeFileSync(path.join(runRoot,'smoke-evidence.json'),JSON.stringify(evidence,null,2));console.log(JSON.stringify(evidence));return evidence;
}
async function stop(code=0){if(stopping)return;stopping=true;process.exitCode=code;if(!ended){child.stdin.write('{"command":"stop"}\n');child.stdin.end();await Promise.race([new Promise(resolve=>child.once('exit',resolve)),new Promise(resolve=>setTimeout(resolve,3000))]);if(!ended)child.kill();}}
let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',chunk=>{input+=chunk;for(;;){const at=input.indexOf('\n');if(at<0)break;const line=input.slice(0,at);input=input.slice(at+1);try{const command=JSON.parse(line);if(command.command==='stop')void stop();else if(command.command==='smoke'&&ready)void smoke(ready).catch(()=>stop(1));else console.error('Only stop or smoke preview commands are supported');}catch{console.error('Preview commands must be JSON lines');}}});
process.on('SIGINT',()=>void stop());process.on('SIGTERM',()=>void stop());process.on('exit',()=>{if(!ended)child.kill();});
(async()=>{const meta=ready||await new Promise((resolve,reject)=>{pending.push(resolve);const timer=setTimeout(()=>reject(Error('Preview startup timed out')),12000);timer.unref();});if(options.smoke){await smoke(meta);await stop();}})().catch(async()=>{console.error('Preview smoke/start failed');await stop(1);});
