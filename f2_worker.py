"""Local F2 queue consumer. The worker token stays out of the scraper process."""
import argparse
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from collection_profiles import ProfileIndex,ProfileRequired,canonical_profile

ROOT=Path(__file__).resolve().parent
WINDOWS=os.name=='nt'
ORIGINAL_SCRIPT_SHA256='1c4434f4a70377e0924e86b2473980c88f2d38d5588ddfe05c96ba3311f1beee'

# This opt-in belongs to the Go worker only; the legacy service stays disabled.
def require_collection():
    if os.environ.get('DOUYIN_BOARD_COLLECTION_ENABLED') != '1':
        raise ValueError('作品采集未启用，请先配置本机采集器')


def valid_worker_token(value):
    return isinstance(value,str) and 32 <= len(value) <= 256 and all(33 <= ord(char) <= 126 for char in value)


def worker_token(environ):
    token=environ.get('DOUYIN_BOARD_WORKER_TOKEN','')
    token_file=environ.get('DOUYIN_BOARD_WORKER_TOKEN_FILE','')
    if token and token_file:
        raise ValueError('采集令牌只能选择环境变量或私有文件中的一种')
    if token_file:
        try:
            path=Path(token_file)
            if not path.is_absolute():raise ValueError()
            before=path.lstat()
            if (not stat.S_ISREG(before.st_mode) or before.st_size>258 or
                    (not WINDOWS and before.st_mode & 0o077)):raise ValueError()
            flags=(os.O_RDONLY|getattr(os,'O_BINARY',0)|getattr(os,'O_NOFOLLOW',0)|
                   getattr(os,'O_NONBLOCK',0))
            with os.fdopen(os.open(path,flags),'rb') as stream:
                info=os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_size>258 or
                        (info.st_dev,info.st_ino)!=(before.st_dev,before.st_ino) or
                        (not WINDOWS and info.st_mode & 0o077)):raise ValueError()
                data=stream.read(259)
                if len(data)>258:raise ValueError()
                if data.endswith(b'\r\n'):data=data[:-2]
                elif data.endswith(b'\n'):data=data[:-1]
                token=data.decode('ascii')
        except (OSError,UnicodeError,ValueError):
            raise ValueError('无法读取有效的私有采集令牌文件') from None
    if not valid_worker_token(token):
        raise ValueError('请配置32至256位可见ASCII采集令牌')
    return token

def collection_tool(name):
    value=os.environ.get(name,'').strip()
    if not value:raise ValueError('采集环境未配置 '+name)
    return Path(value).expanduser().resolve()

def bound_homepage(job):
    return canonical_profile(job.get('homepage'))

def original_script():
    path=ROOT/'collectors/f2_original/douyin_counts.py'
    try:
        info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size!=10632 or hashlib.sha256(path.read_bytes()).hexdigest()!=ORIGINAL_SCRIPT_SHA256:raise ValueError()
    except (OSError,ValueError):
        raise ValueError('内置原始采集程序校验失败，请核对安装文件') from None
    return path

ERRORS={'LoginRequired':'抖音登录已失效，请在独立采集浏览器中重新登录',
        'SearchNeedsVerification':'抖音搜索需要安全验证，请在采集浏览器中手动完成',
        'SearchUnavailable':'没有搜索到抖音号完全一致的账号，请核对真实抖音号',
        'IdentityMismatch':'账号身份校验失败，未更新数量',
        'ProfileMissing':'缺少已确认主页，请补充主页后再采集',
        'ProfileConflict':'历史主页记录有冲突，请核对主页后再采集',
        'ProfileUnavailable':'公开作品数量不可用或账号私密',
        'CollectorStartupFailed':'采集程序未返回有效报告，请检查运行环境和浏览器',
        'CollectorTimedOut':'抖音采集超时，请检查采集器网络'}
BLOCKED_ERRORS={'LoginRequired','SearchNeedsVerification','CollectorStartupFailed'}

class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def result_body(job,row):
    body={'account_id':job['account_id'],'token':job['token']}
    if row.get('status')=='success':
        homepage=bound_homepage(job)
        if ((job['douyin_id'] and row.get('account')!=job['douyin_id'])
                or (homepage and row.get('url')!=homepage)):
            return {**body,'error':ERRORS['IdentityMismatch']}
        if type(row.get('count')) is not int or row['count']<0:
            return {**body,'error':ERRORS['ProfileUnavailable']}
        return {**body,'count':row.get('count'),'source_url':row.get('url'),
                'matched_douyin_id':row.get('account')}
    error=row.get('error')
    if not isinstance(error,str):error='RuntimeError'
    message=ERRORS.get(error,'采集未完成，请检查服务器采集器和网络')
    if error in ('ProfileMissing','ProfileConflict'):
        return {**body,'error':message,'profile_required':True,'blocked':False}
    return {**body,'error':message,
            'blocked':error in BLOCKED_ERRORS}

class Worker:
    def __init__(self,*,wait_for_resume=False):
        require_collection()
        self.origin = os.environ.get('DOUYIN_BOARD_WORKER_ORIGIN', '')
        try:
            port = urlsplit(self.origin).port
        except ValueError:
            port = None
        if not port or self.origin != f'http://127.0.0.1:{port}':
            raise ValueError('采集器只允许连接本机工作台 HTTP 地址')
        self.token = worker_token(os.environ)
        self.opener=build_opener(ProxyHandler({}), NoRedirects())
        self.command='';self.login=None;self.failures=0
        self.browser_control=False
        self.browser=None
        self.profiles=ProfileIndex()
        self.waiting_for_resume=wait_for_resume

    def configure_runtime(self):
        self.python = collection_tool('DOUYIN_BOARD_F2_PYTHON')
        self.profile = collection_tool('DOUYIN_BOARD_F2_PROFILE')
        self.output = collection_tool('DOUYIN_BOARD_F2_OUTPUT')
        index_file=os.environ.get('DOUYIN_BOARD_F2_PROFILE_INDEX','')
        static_roots=((ROOT/'static').resolve(),(ROOT/'go/internal/webassets/static').resolve())
        private_paths=[self.profile,self.output]
        if index_file:private_paths.append(Path(index_file).expanduser().resolve())
        if any(path.is_relative_to(static) for path in private_paths for static in static_roots):
            raise ValueError('采集浏览器、输出和私有主页索引不能放在网页资源目录内')
        self.script=original_script()
        if not self.python.is_file() or not self.script.is_file():
            raise ValueError('采集程序或 Python 运行环境不存在')
        if not self.profile.is_dir():
            raise ValueError('独立采集浏览器目录不存在，请先完成登录')
        browser=os.environ.get('DOUYIN_BOARD_F2_BROWSER','')
        self.browser=Path(browser).expanduser().resolve() if browser else None
        if self.browser and not self.browser.is_file():
            raise ValueError('采集浏览器程序不存在')
        graphical = os.name == 'nt' or bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))
        headless = os.environ.get('DOUYIN_BOARD_F2_HEADLESS', '0' if graphical else '1')
        if headless not in ('0', '1'):
            raise ValueError('DOUYIN_BOARD_F2_HEADLESS 必须为 0 或 1')
        if headless == '0' and not graphical:
            raise ValueError('当前系统没有图形会话，请使用无界面采集模式')
        self.headless = headless == '1'
        self.profiles=ProfileIndex.from_file(index_file)
        self.output.mkdir(parents=True, exist_ok=True)

    def request(self,route,body=None):
        req=Request(self.origin+route,data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type':'application/json','X-Worker-Token':self.token,'X-Collection-Engine':'f2'})
        with self.opener.open(req,timeout=15) as response:return json.load(response)

    def heartbeat(self,status):
        command=self.request('/api/collection/heartbeat',{
            'status':status,'browser_control':self.browser_control,
        }).get('command')
        if command:self.command=command

    def launch(self,args,stdin=None):
        child_env={key:value for key,value in os.environ.items()
                   if not key.startswith(('DOUYIN_BOARD_','BOARD_GO_')) and key not in ('DATABASE_URL','PGPASSWORD','PGPASSFILE')}
        command=[str(self.python),'-X','utf8',str(original_script()),*args,'--profile',str(self.profile)]
        if self.browser:command.extend(['--browser-executable',str(self.browser)])
        return subprocess.Popen(command,
            stdin=stdin,stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,env=child_env,start_new_session=os.name!='nt',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)

    def stop(self, process):
        if os.name=='nt':
            try:
                subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],timeout=10,
                               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            except (OSError,subprocess.TimeoutExpired):
                pass
        else:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
        if process.poll() is None:process.kill()
        process.wait()

    def query(self,job):
        try:homepage=self.profiles.resolve(job['douyin_id'],job.get('homepage'))
        except ProfileRequired as error:return {'status':'failed','error':error.code}
        folder=Path(tempfile.mkdtemp(prefix='job-',dir=self.output))
        source=folder/'accounts.json'
        handle=job['douyin_id']
        item={'url':homepage,'account':handle} if handle else {'url':homepage}
        source.write_text(json.dumps([item]),encoding='utf-8')
        args=['query','--input',str(source),'--output',str(folder),'--max-failures','1']
        process=self.launch(args,subprocess.DEVNULL)
        started=time.monotonic()
        try:
            while process.poll() is None:
                if time.monotonic()-started>180:
                    self.stop(process)
                    return {'status':'failed','error':'CollectorTimedOut'}
                try:self.heartbeat('正在采集')
                except Exception:pass
                time.sleep(2)
        finally:
            if process.poll() is None:self.stop(process)
            for stream in (process.stdin,process.stdout):
                if stream:stream.close()
        reports=list(folder.glob('*/results.json'))
        if len(reports)==1:
            try:
                rows=json.loads(reports[0].read_text(encoding='utf-8'))['results']
                if isinstance(rows,list) and len(rows)==1 and isinstance(rows[0],dict):return rows[0]
            except (OSError,ValueError,KeyError,TypeError):pass
        return {'status':'failed','error':'CollectorStartupFailed'}

    def collect(self,job,*,once=False):
        active=job
        try:
            active={**job,'homepage':self.profiles.resolve(job['douyin_id'],job.get('homepage'))}
            row=self.query(active)
        except ProfileRequired as error:row={'status':'failed','error':error.code}
        except (OSError,ValueError):row={'status':'failed','error':'CollectorStartupFailed'}
        body=result_body(active,row)
        # Capture once, before delivery retries. Server receipt time may be late.
        body['sampled_at']=datetime.now(timezone.utc).isoformat(timespec='microseconds')
        if not body.get('profile_required'):self.failures=self.failures+1 if body.get('error') else 0
        if not body.get('profile_required') and self.failures>=3:
            body.update(blocked=True,error=body['error']+'；连续3次失败，队列暂停，请检查后继续')
        return self.deliver(job,body,once=once)

    def delivery_receipt(self,job,body,result):
        # Only configured, non-static F2 output receives these local receipts.
        if not hasattr(self,'output'):return
        filename=None
        try:
            ok=result.get('ok') is True
            ignored=result.get('ignored') is True
            count=body.get('count')
            valid_count=type(count) is int and 0 <= count <= 10000000 and not body.get('error')
            raw=body.get('source_url')
            parsed=urlsplit(raw.strip()) if isinstance(raw,str) else None
            source=('https://www.douyin.com'+parsed.path.rstrip('/') if parsed and
                    parsed.scheme=='https' and parsed.netloc=='www.douyin.com' and
                    re.fullmatch(r'/user/[A-Za-z0-9_-]{5,200}/?',parsed.path) else None)
            sampled_at=None
            if isinstance(body.get('sampled_at'),str):
                try:
                    sampled=datetime.fromisoformat(body['sampled_at'].replace('Z','+00:00'))
                    if sampled.tzinfo:sampled_at=sampled.astimezone(timezone.utc).isoformat(timespec='microseconds')
                except ValueError:pass
            persisted=bool(ok and not ignored and valid_count and source)
            receipt={'account_id':job['account_id'],'douyin_id':job['douyin_id'],
                'sampled_at':sampled_at,'deliveredAt':datetime.now(timezone.utc).isoformat(timespec='microseconds'),
                'server_origin':self.origin,'ok':ok,'duplicate':result.get('duplicate') is True,
                'ignored':ignored,'persisted':persisted,'status':'success' if persisted else 'failed'}
            if valid_count:receipt['count']=count
            if source and valid_count:receipt['source_url']=source
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',prefix='delivery-',suffix='.json',
                                             dir=self.output,delete=False) as stream:
                filename=stream.name
                json.dump(receipt,stream,ensure_ascii=False)
                stream.write('\n')
        except (OSError,ValueError,TypeError):
            if filename:
                try:os.unlink(filename)
                except OSError:pass
            print('服务器回执已收到，但本机回执文件未保存',file=sys.stderr,flush=True)

    def deliver(self,job,body,*,once=False):
        attempts=0
        # Keep retrying delivery, never make a second upstream request for the same lease.
        while True:
            attempts+=1
            try:
                result=self.request('/api/collection/result',body)
            except HTTPError as error:
                if error.code==400 and 'count' in body:
                    if once and attempts>=3:raise RuntimeError('未能确认采集结果已送达') from None
                    body={'account_id':job['account_id'],'token':job['token'],
                          'error':'采集结果校验失败，未更新数量','sampled_at':body['sampled_at']}
                    continue
                if error.code<500 and error.code not in (408,429):raise
            except (OSError,ValueError):pass
            else:
                # Receipt I/O is outside the transport retry block: never resend
                # or scrape again because a confirmed response cannot be logged.
                self.delivery_receipt(job,body,result)
                return result.get('ok') is True and result.get('ignored') is not True and not body.get('error')
            if once and attempts>=3:raise RuntimeError('未能确认采集结果已送达')
            time.sleep(5)

    def poll(self,*,once=False):
        if self.login and self.login.poll() is not None:self.login=None
        self.heartbeat('等待手动登录或验证' if self.login or self.waiting_for_resume else '待命')
        command,self.command=self.command,''
        if command=='open' and self.browser_control and not self.login:
            self.login=self.launch(['login'],subprocess.PIPE)
        if command=='resume':
            self.waiting_for_resume=False
            self.failures=0
            if self.login and self.login.poll() is None:
                self.login.stdin.write(b'\n');self.login.stdin.flush()
        if not self.login and not self.waiting_for_resume:
            job=self.request('/api/collection/next').get('job')
            if job:return self.collect(job,once=once)
        return None

    def run(self,*,once=False):
        self.browser_control=not (self.headless or once)
        last_error=''
        while True:
            try:
                result=self.poll(once=once)
            except Exception as error:
                message=f'采集器请求失败（HTTP {error.code}）' if isinstance(error,HTTPError) else '采集器未完成本次处理，请检查运行环境及本机服务'
                if message!=last_error:print(message,file=sys.stderr,flush=True)
                last_error=message
                if once:return 1
            else:
                last_error=''
                if once:
                    if result is None:message='没有待采集任务'
                    else:message='采集结果已保存' if result else '采集结果未更新'
                    print(message,flush=True)
                    return 2 if result is False else 0
            time.sleep(3)

def lock_worker(lock):
    if os.name=='nt':
        import msvcrt
        if lock.tell()==0:
            lock.write(b'1');lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    else:
        import fcntl
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)

def main(argv=None):
    require_collection()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once',action='store_true',help='at most one manually queued job')
    parser.add_argument('--wait-for-resume',action='store_true',help='wait for an explicit queue resume after every worker start')
    args=parser.parse_args(argv)
    worker=Worker(wait_for_resume=args.wait_for_resume)
    worker.configure_runtime()
    # One consumer per dedicated browser profile. Closing the file releases the lock.
    with (worker.profile/'.douyin-board-worker.lock').open('a+b') as lock:
        try:lock_worker(lock)
        except OSError:raise ValueError('该独立采集浏览器已有采集器在运行') from None
        return worker.run(once=args.once)

if __name__=='__main__':
    try:sys.exit(main())
    except ValueError as error:
        print(str(error),file=sys.stderr);sys.exit(1)
    except OSError:
        print('采集器无法打开运行目录，请检查目录权限',file=sys.stderr);sys.exit(1)
