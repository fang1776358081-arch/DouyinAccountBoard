const {app,BrowserWindow,dialog,Menu,shell}=require('electron');
const {randomBytes}=require('node:crypto');
const {spawn}=require('node:child_process');
const path=require('node:path');
const fs=require('node:fs');
app.setName('抖音账号工作台');
app.setAppUserModelId('local.douyin.account.board');
const updates=require('./updater.cjs');
let child,window,serverOrigin,collectorChild,quitting=false;
let workerToken=randomBytes(32).toString('hex');
const gotLock=app.requestSingleInstanceLock();
if(!gotLock){app.quit();}else{
app.on('second-instance',()=>{if(window){if(window.isMinimized())window.restore();window.show();window.focus();}});
function stop(){if(collectorChild&&!collectorChild.killed){spawn('taskkill',['/PID',String(collectorChild.pid),'/T','/F'],{windowsHide:true,stdio:'ignore'});collectorChild=undefined;}if(child&&!child.killed){child.kill();child=undefined;}}
app.on('before-quit',()=>{quitting=true;stop();});
app.on('window-all-closed',()=>app.quit());
async function start(){
  const profile=app.getPath('userData');
  fs.mkdirSync(path.join(profile,'logs'),{recursive:true});
  const log=fs.createWriteStream(path.join(profile,'logs','desktop.log'),{flags:'a'});
  window=new BrowserWindow({width:1440,height:960,minWidth:920,minHeight:650,show:false,title:'抖音账号工作台',backgroundColor:'#f5f7f5',icon:path.join(__dirname,'board.ico'),webPreferences:{contextIsolation:true,nodeIntegration:false,sandbox:true,preload:path.join(__dirname,'preload.cjs')}});
  window.on('closed',()=>app.quit());
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    {label:'工作台',submenu:[{label:'备份账号数据',click:async()=>{const result=await dialog.showSaveDialog(window,{title:'备份账号数据库',defaultPath:path.join(profile,'backups','账号数据-'+new Date().toISOString().slice(0,10)+'.db'),filters:[{name:'SQLite 数据库',extensions:['db']}]});if(!result.canceled){fs.mkdirSync(path.dirname(result.filePath),{recursive:true});try{const backup=spawn(path.join(process.resourcesPath,'python','python.exe'),[path.join(process.resourcesPath,'backend','backup.py'),path.join(profile,'data','board.db'),result.filePath],{windowsHide:true});backup.on('exit',code=>dialog.showMessageBox(window,{type:code===0?'info':'error',message:code===0?'备份完成':'备份失败，请重试'}));backup.on('error',e=>dialog.showErrorBox('备份失败',e.message));}catch(e){dialog.showErrorBox('备份失败',e.message);}}}},{label:'退出',click:()=>app.quit()}]},
    {label:'视图',submenu:[{role:'reload'},{role:'resetZoom'},{role:'zoomIn'},{role:'zoomOut'},{role:'togglefullscreen'}]},
    {label:'帮助',submenu:[{label:'检查更新',click:()=>updates.checkNow()},{label:'打开数据文件夹',click:()=>shell.openPath(path.join(profile,'data'))},{label:'打开自动备份文件夹',click:()=>shell.openPath(path.join(profile,'backups','automatic'))},{label:'关于',click:()=>dialog.showMessageBox(window,{type:'info',message:'抖音账号工作台 '+app.getVersion(),detail:'打开服务器上的账号工作台。发现新版本后会自动下载，重启后生效。'})}]}
  ]));
  window.webContents.setWindowOpenHandler(()=>({action:'deny'}));
  window.webContents.on('will-navigate',(event,url)=>{if(!serverOrigin||!url.startsWith(serverOrigin+'/'))event.preventDefault();});
  window.webContents.session.setPermissionRequestHandler((_wc,_permission,callback)=>callback(false));
  serverOrigin='http://42.192.45.194';
  await window.loadURL(serverOrigin+'/login.html');
  window.show();
  window.focus();
  updates.start(()=>window);
}
app.whenReady().then(start).catch(e=>{dialog.showErrorBox('启动失败',e.message);app.quit();});
}
