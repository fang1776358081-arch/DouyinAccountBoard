const {app,dialog}=require('electron');
const fs=require('node:fs');
const path=require('node:path');
const {spawn}=require('node:child_process');
const {pipeline}=require('node:stream/promises');
const {Readable}=require('node:stream');

const OWNER='fang1776358081-arch';
const REPO='DouyinAccountBoard';
const RELEASE_PREFIX=`https://github.com/${OWNER}/${REPO}/releases/download/`;
let getWindow=()=>null;
let manual=false;
let checking=false;
let nsisBound=false;

function log(line){
  try{
    const dir=path.join(app.getPath('userData'),'logs');
    fs.mkdirSync(dir,{recursive:true});
    fs.appendFileSync(path.join(dir,'update.log'),new Date().toISOString()+' '+line+'\n');
  }catch{}
}

function installedByNsis(){
  try{
    return fs.readdirSync(path.dirname(process.execPath)).some(name=>/^uninstall.*\.exe$/i.test(name));
  }catch{
    return false;
  }
}

function windowTitle(text){
  const current=getWindow();
  if(current&&!current.isDestroyed())current.setTitle(text);
}

function compareVersion(left,right){
  const a=String(left).split('.').map(Number);
  const b=String(right).split('.').map(Number);
  for(let index=0;index<3;index++){
    if((a[index]||0)!==(b[index]||0))return (a[index]||0)-(b[index]||0);
  }
  return 0;
}

function promptRestart(apply){
  return dialog.showMessageBox(getWindow()||undefined,{
    type:'info',buttons:['立即重启','稍后'],defaultId:0,cancelId:1,noLink:true,
    message:'新版本已准备好',
    detail:'重启后自动换上。账号数据在服务器上，不会因为更新丢掉。'
  }).then(result=>{if(result.response===0)apply();});
}

function bindNsis(){
  if(nsisBound)return;
  nsisBound=true;
  const {autoUpdater}=require('electron-updater');
  autoUpdater.autoDownload=true;
  autoUpdater.autoInstallOnAppQuit=true;
  autoUpdater.on('update-available',()=>windowTitle('抖音账号工作台（正在下载更新）'));
  autoUpdater.on('update-downloaded',()=>{
    windowTitle('抖音账号工作台');
    promptRestart(()=>autoUpdater.quitAndInstall(false,true));
  });
  autoUpdater.on('update-not-available',()=>{
    windowTitle('抖音账号工作台');
    if(manual)dialog.showMessageBox(getWindow()||undefined,{type:'info',message:'已是最新版本',detail:'当前版本 '+app.getVersion()});
    manual=false;
  });
  autoUpdater.on('error',error=>{
    log(error&&error.stack||String(error));
    windowTitle('抖音账号工作台');
    if(manual)dialog.showMessageBox(getWindow()||undefined,{type:'error',message:'暂时无法检查更新',detail:'请稍后再试。当前版本仍可继续使用。'});
    manual=false;
  });
}

async function download(url,file,expectedSize){
  if(!url.startsWith(RELEASE_PREFIX))throw new Error('unexpected download url');
  if(expectedSize&&fs.existsSync(file)&&fs.statSync(file).size===expectedSize)return;
  const response=await fetch(url,{headers:{'User-Agent':'DouyinAccountBoard','Accept':'application/octet-stream'}});
  if(!response.ok||!response.body)throw new Error('download '+response.status);
  const part=file+'.part';
  await pipeline(Readable.fromWeb(response.body),fs.createWriteStream(part));
  const size=fs.statSync(part).size;
  if(expectedSize&&size!==expectedSize){
    fs.rmSync(part,{force:true});
    throw new Error('size mismatch');
  }
  fs.renameSync(part,file);
}

function applyPortableZip(zip){
  const dest=path.dirname(process.execPath);
  const exe=process.execPath;
  const script=path.join(app.getPath('temp'),'douyin-board-update.ps1');
  const quote=value=>String(value).replace(/'/g,"''");
  fs.writeFileSync(script,[
    "$ErrorActionPreference = 'Stop'",
    `$zip = '${quote(zip)}'`,
    `$dest = '${quote(dest)}'`,
    `$exe = '${quote(exe)}'`,
    `$stage = Join-Path $env:TEMP ('douyin-board-stage-' + [guid]::NewGuid().ToString('N'))`,
    'New-Item -ItemType Directory -Path $stage | Out-Null',
    'tar -xf $zip -C $stage',
    'for ($i = 0; $i -lt 30; $i++) {',
    '  & robocopy $stage $dest /E /R:1 /W:1 /NFL /NDL /NJH /NJS | Out-Null',
    '  if ($LASTEXITCODE -lt 8) { break }',
    '  Start-Sleep -Seconds 1',
    '}',
    'Start-Process -LiteralPath $exe',
    'Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue'
  ].join('\r\n'),'utf8');
  spawn('powershell.exe',['-NoProfile','-ExecutionPolicy','Bypass','-File',script],{detached:true,stdio:'ignore',windowsHide:true}).unref();
  app.quit();
}

async function checkPortable(){
  const response=await fetch(`https://api.github.com/repos/${OWNER}/${REPO}/releases?per_page=20`,{headers:{'User-Agent':'DouyinAccountBoard','Accept':'application/vnd.github+json'}});
  if(!response.ok)throw new Error('release list '+response.status);
  const releases=await response.json();
  let best=null;
  for(const release of releases){
    if(!release||release.draft||release.prerelease)continue;
    for(const asset of release.assets||[]){
      const matched=String(asset.name||'').match(/^DouyinAccountBoard-Portable-(\d+\.\d+\.\d+)\.zip$/);
      if(!matched||!String(asset.browser_download_url||'').startsWith(RELEASE_PREFIX))continue;
      if(!best||compareVersion(matched[1],best.version)>0)best={version:matched[1],url:asset.browser_download_url,size:asset.size,name:asset.name};
    }
  }
  if(!best||compareVersion(best.version,app.getVersion())<=0){
    if(manual)await dialog.showMessageBox(getWindow()||undefined,{type:'info',message:'已是最新版本',detail:'当前版本 '+app.getVersion()});
    manual=false;
    return;
  }
  windowTitle('抖音账号工作台（正在下载更新）');
  const file=path.join(app.getPath('temp'),best.name);
  await download(best.url,file,best.size);
  windowTitle('抖音账号工作台');
  manual=false;
  await promptRestart(()=>applyPortableZip(file));
}

async function check(){
  if(!app.isPackaged||checking)return;
  checking=true;
  try{
    if(installedByNsis()){
      bindNsis();
      const {autoUpdater}=require('electron-updater');
      await autoUpdater.checkForUpdates();
    }else{
      await checkPortable();
    }
  }catch(error){
    log(error&&error.stack||String(error));
    windowTitle('抖音账号工作台');
    if(manual&&!installedByNsis())await dialog.showMessageBox(getWindow()||undefined,{type:'error',message:'暂时无法检查更新',detail:'请稍后再试。当前版本仍可继续使用。'});
    manual=false;
  }finally{
    checking=false;
  }
}

function start(windowGetter){
  getWindow=windowGetter||(()=>null);
  if(!app.isPackaged)return;
  if(installedByNsis())bindNsis();
  setTimeout(()=>check(),8000);
  setInterval(()=>{manual=false;check();},6*60*60*1000);
}

function checkNow(){
  manual=true;
  return check();
}

module.exports={start,checkNow,compareVersion};
