"""Go worker transport, identity and verification checks without network or browsers."""
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import f2_worker


TOKEN='synthetic-only-worker-token-0000000000000000'
ENV={'DOUYIN_BOARD_COLLECTION_ENABLED':'1',
     'DOUYIN_BOARD_WORKER_ORIGIN':'http://127.0.0.1:18790',
     'DOUYIN_BOARD_WORKER_TOKEN':TOKEN}
JOB={'account_id':'synthetic-account','token':'synthetic-job-lease',
     'douyin_id':'0012345678','homepage':''}
PROFILE_URL='https://www.douyin.com/user/MS4wSyntheticOnly'


class GoWorkerTest(unittest.TestCase):
    def worker(self,*,wait_for_resume=False,**extra):
        with patch.dict(os.environ,{**ENV,**extra},clear=True):
            return f2_worker.Worker(wait_for_resume=wait_for_resume)

    def test_opt_in_stops_before_reading_files_or_starting_processes(self):
        with patch.dict(os.environ,{},clear=True),patch.object(Path,'open') as read,patch.object(f2_worker.subprocess,'Popen') as launch:
            with self.assertRaisesRegex(ValueError,'未启用'):f2_worker.Worker()
            with self.assertRaisesRegex(ValueError,'未启用'):f2_worker.main([])
        read.assert_not_called();launch.assert_not_called()

    def test_requires_explicit_exact_loopback_origin(self):
        for origin in ('','http://example.invalid:18790','https://127.0.0.1:18790',
                       'http://127.0.0.1:18790/','http://127.0.0.1:18790?x=1',
                       'http://user@127.0.0.1:18790','http://127.0.0.1:99999'):
            with self.subTest(origin=origin),self.assertRaises(ValueError):
                self.worker(DOUYIN_BOARD_WORKER_ORIGIN=origin)
        self.assertEqual(self.worker().origin,ENV['DOUYIN_BOARD_WORKER_ORIGIN'])

    def test_environment_token_must_be_visible_ascii_with_bounded_length(self):
        for token in ('','short',' '+TOKEN,TOKEN+' ',TOKEN+'\n','x'*257,'令牌'*32):
            with self.subTest(length=len(token)),self.assertRaises(ValueError):
                self.worker(DOUYIN_BOARD_WORKER_TOKEN=token)
        self.assertEqual(self.worker().token,TOKEN)

    def test_private_token_file_and_environment_are_exclusive_and_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'synthetic-token'
            path.write_text(TOKEN+'\n',encoding='ascii')
            path.chmod(0o600)
            worker=self.worker(DOUYIN_BOARD_WORKER_TOKEN='',DOUYIN_BOARD_WORKER_TOKEN_FILE=str(path))
            self.assertEqual(worker.token,TOKEN)
            with self.assertRaisesRegex(ValueError,'一种'):
                self.worker(DOUYIN_BOARD_WORKER_TOKEN_FILE=str(path))
            for text in ('x'*259,' '+TOKEN,TOKEN+'\n'+'injected',TOKEN+'\n\n',TOKEN+'\r',TOKEN+'\r\n\r\n'):
                path.write_text(text,encoding='ascii')
                with self.assertRaises(ValueError):
                    self.worker(DOUYIN_BOARD_WORKER_TOKEN='',DOUYIN_BOARD_WORKER_TOKEN_FILE=str(path))
            path.write_bytes(b'\xff'*40)
            with self.assertRaises(ValueError):
                self.worker(DOUYIN_BOARD_WORKER_TOKEN='',DOUYIN_BOARD_WORKER_TOKEN_FILE=str(path))
            with self.assertRaises(ValueError):
                self.worker(DOUYIN_BOARD_WORKER_TOKEN='',DOUYIN_BOARD_WORKER_TOKEN_FILE=folder)

    def test_private_token_file_requires_absolute_path_and_private_posix_mode(self):
        with patch.object(f2_worker.os,'open') as opened,self.assertRaises(ValueError):
            self.worker(DOUYIN_BOARD_WORKER_TOKEN='',DOUYIN_BOARD_WORKER_TOKEN_FILE='relative-secret')
        opened.assert_not_called()
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'synthetic-token';path.write_bytes((TOKEN+'\r\n').encode('ascii'))
            info=f2_worker.os.stat(path)
            for permissions,allowed in ((0o600,True),(0o400,True),(0o640,False),(0o604,False),(0o660,False)):
                mocked=Mock(st_mode=(info.st_mode & ~0o777)|permissions,st_size=info.st_size,
                            st_dev=info.st_dev,st_ino=info.st_ino)
                with self.subTest(mode=permissions),patch.object(f2_worker,'WINDOWS',False),patch.object(Path,'lstat',return_value=mocked),patch.object(f2_worker.os,'fstat',return_value=mocked):
                    if allowed:
                        self.assertEqual(self.worker(DOUYIN_BOARD_WORKER_TOKEN='',DOUYIN_BOARD_WORKER_TOKEN_FILE=str(path)).token,TOKEN)
                    else:
                        with self.assertRaises(ValueError):
                            self.worker(DOUYIN_BOARD_WORKER_TOKEN='',DOUYIN_BOARD_WORKER_TOKEN_FILE=str(path))

    def test_nonregular_token_file_is_rejected_before_opening(self):
        absolute=Path(__file__).resolve()
        for kind in (stat.S_IFIFO,stat.S_IFDIR,stat.S_IFLNK,stat.S_IFSOCK):
            info=Mock(st_mode=kind|0o600,st_size=0,st_dev=1,st_ino=1)
            with self.subTest(kind=kind),patch.object(Path,'lstat',return_value=info),patch.object(f2_worker.os,'open') as opened:
                with self.assertRaises(ValueError):
                    self.worker(DOUYIN_BOARD_WORKER_TOKEN='',DOUYIN_BOARD_WORKER_TOKEN_FILE=str(absolute))
            opened.assert_not_called()

    def test_token_file_swap_between_lstat_and_open_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'synthetic-token';path.write_text(TOKEN,encoding='ascii');path.chmod(0o600)
            info=path.lstat()
            changed=Mock(st_mode=info.st_mode,st_size=info.st_size,st_dev=info.st_dev,st_ino=info.st_ino+1)
            with patch.object(f2_worker.os,'fstat',return_value=changed),self.assertRaises(ValueError):
                self.worker(DOUYIN_BOARD_WORKER_TOKEN='',DOUYIN_BOARD_WORKER_TOKEN_FILE=str(path))

    def test_runtime_profile_and_output_cannot_be_in_python_or_go_webassets(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'project';safe=Path(folder)/'private-profile';safe.mkdir()
            for static in (root/'static',root/'go/internal/webassets/static'):
                for field in ('profile','output'):
                    candidate=static/'private-material'
                    runtime={**ENV,'DOUYIN_BOARD_F2_PYTHON':__file__,'DOUYIN_BOARD_F2_SCRIPT':__file__,
                             'DOUYIN_BOARD_F2_PROFILE':str(candidate if field=='profile' else safe),
                             'DOUYIN_BOARD_F2_OUTPUT':str(candidate if field=='output' else Path(folder)/'private-output')}
                    with self.subTest(static=str(static),field=field),patch.object(f2_worker,'ROOT',root),patch.dict(os.environ,runtime,clear=True):
                        worker=f2_worker.Worker()
                        with self.assertRaisesRegex(ValueError,'网页资源'):worker.configure_runtime()
                    self.assertFalse(candidate.exists())

    def test_visible_query_is_explicitly_opt_in_and_never_overrides_headless(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime={**ENV,'DOUYIN_BOARD_F2_PYTHON':__file__,'DOUYIN_BOARD_F2_SCRIPT':__file__,
                     'DOUYIN_BOARD_F2_PROFILE':folder,'DOUYIN_BOARD_F2_OUTPUT':str(Path(folder)/'output'),
                     'DISPLAY':':synthetic-only','DOUYIN_BOARD_F2_HEADLESS':'0'}
            with patch.dict(os.environ,runtime,clear=True):
                worker=f2_worker.Worker();worker.configure_runtime();self.assertFalse(worker.visible_query)
            for visible,headless,expected in (('0','0',False),('1','0',True),('1','1',False)):
                with self.subTest(visible=visible,headless=headless),patch.dict(os.environ,{**runtime,
                        'DOUYIN_BOARD_F2_VISIBLE_QUERY':visible,'DOUYIN_BOARD_F2_HEADLESS':headless},clear=True):
                    worker=f2_worker.Worker();worker.configure_runtime();self.assertEqual(worker.visible_query,expected)
            with patch.dict(os.environ,{**runtime,'DOUYIN_BOARD_F2_VISIBLE_QUERY':'yes'},clear=True):
                worker=f2_worker.Worker()
                with self.assertRaisesRegex(ValueError,'VISIBLE_QUERY'):worker.configure_runtime()

    def test_default_background_query_keeps_manual_window_capability(self):
        worker=self.worker();worker.headless=False
        def stop_poll(**unused):
            self.assertTrue(worker.browser_control)
            raise KeyboardInterrupt()
        worker.poll=Mock(side_effect=stop_poll)
        with self.assertRaises(KeyboardInterrupt):worker.run()
        process=Mock();process.poll.return_value=0;process.stdin=None;process.stdout=None
        worker.launch=Mock(return_value=process)
        with tempfile.TemporaryDirectory() as folder:
            worker.output=Path(folder);worker.query(JOB)
        args=worker.launch.call_args.args[0]
        self.assertNotIn('--headed',args);self.assertNotIn('--interactive',args)
        self.assertFalse(worker.launch.call_args.kwargs['observe_verification'])
        self.assertTrue(worker.browser_control)

    def test_no_redirects_or_environment_proxy_for_worker_requests(self):
        with patch.object(f2_worker,'build_opener') as build:
            self.worker()
        handlers=build.call_args.args
        self.assertEqual(handlers[0].proxies,{})
        self.assertIsInstance(handlers[1],f2_worker.NoRedirects)
        self.assertIsNone(handlers[1].redirect_request(None,None,302,'',{},'https://example.invalid'))

    def test_heartbeat_advertises_browser_control_and_keeps_only_safe_command(self):
        worker=self.worker();worker.browser_control=True
        worker.request=Mock(return_value={'command':'open'})
        worker.heartbeat('待命')
        worker.request.assert_called_once_with('/api/collection/heartbeat',{'status':'待命','browser_control':True})
        self.assertEqual(worker.command,'open')

    def test_scraper_child_does_not_inherit_worker_or_database_secrets(self):
        worker=self.worker();worker.python=Path('synthetic-python');worker.script=Path('synthetic-collector')
        worker.profile=Path('synthetic-profile');worker.browser=Path('synthetic-browser')
        env={**ENV,'DOUYIN_BOARD_WORKER_TOKEN_FILE':'private-token-file',
             'BOARD_GO_PG_DSN':'synthetic-private-dsn','BOARD_GO_COLLECTION_TOKEN':'synthetic-server-token',
             'DATABASE_URL':'synthetic-database-url','PGPASSWORD':'synthetic-password',
             'PGPASSFILE':'synthetic-passfile','PATH':'synthetic-safe-path'}
        with patch.dict(os.environ,env,clear=True),patch.object(f2_worker.subprocess,'Popen') as launch:
            worker.launch(['login'],f2_worker.subprocess.PIPE)
        inherited=launch.call_args.kwargs['env']
        self.assertEqual(inherited,{'PATH':'synthetic-safe-path'})
        self.assertIn('--browser-executable',launch.call_args.args[0])
        self.assertNotIn(TOKEN,launch.call_args.args[0])
        self.assertEqual(launch.call_args.kwargs['stdout'],f2_worker.subprocess.DEVNULL)

    def test_exact_handle_and_bound_homepage_are_required_zero_is_valid(self):
        good={'status':'success','account':JOB['douyin_id'],'count':0,'url':PROFILE_URL}
        self.assertEqual(f2_worker.result_body(JOB,good)['count'],0)
        for changed in ({'account':'12345678'},{'account':'other'},{'count':True},{'count':None},{'count':-1}):
            with self.subTest(changed=changed):
                result=f2_worker.result_body(JOB,{**good,**changed})
                self.assertIn('error',result);self.assertNotIn('count',result)
        result=f2_worker.result_body({**JOB,'homepage':PROFILE_URL},{**good,'url':PROFILE_URL+'Other'})
        self.assertIn('error',result);self.assertNotIn('count',result)

    def test_failure_never_fabricates_a_count_or_resets_prior_success(self):
        for failure in ('LoginRequired','SearchNeedsVerification','ProfileUnavailable','RuntimeError'):
            with self.subTest(error=failure):
                result=f2_worker.result_body(JOB,{'status':'failed','error':failure,'count':0})
                self.assertNotIn('count',result);self.assertNotIn('source_url',result)
                self.assertEqual(result.get('blocked',False),failure in f2_worker.BLOCKED_ERRORS)
        worker=self.worker();worker.query=Mock(return_value={'status':'failed','error':'SearchNeedsVerification'})
        worker.deliver=Mock(return_value=False)
        self.assertFalse(worker.collect(JOB,once=True))
        self.assertTrue(worker.deliver.call_args.args[1]['blocked'])
        self.assertNotIn('count',worker.deliver.call_args.args[1])
        self.assertIn('sampled_at',worker.deliver.call_args.args[1])

    def test_verification_marker_is_observed_without_emitting_upstream_output(self):
        waiting=threading.Event()
        raw='other output\n搜索接口要求安全验证（verify_check）。窗口已保留；请本人验证\n'.encode('utf-8')
        with patch('builtins.print') as output:
            f2_worker.Worker.observe_verification(io.BytesIO(raw),waiting)
        output.assert_not_called();self.assertTrue(waiting.is_set())

    def test_only_explicit_resume_can_continue_the_same_verification_process(self):
        worker=self.worker();waiting=threading.Event();waiting.set()
        process=Mock();process.poll.return_value=None;process.stdin=io.BytesIO()
        worker.command='open';worker.resume_query(process,waiting)
        self.assertTrue(waiting.is_set());self.assertEqual(process.stdin.getvalue(),b'')
        worker.command='resume';worker.resume_query(process,waiting)
        self.assertFalse(waiting.is_set());self.assertEqual(process.stdin.getvalue(),b'\n')
        worker.command='resume';worker.resume_query(process,waiting)
        self.assertEqual(process.stdin.getvalue(),b'\n')

    def test_idle_worker_does_not_open_any_browser_automatically(self):
        worker=self.worker();worker.browser_control=True;worker.launch=Mock()
        worker.request=Mock(return_value={'job':None});worker.heartbeat=Mock()
        self.assertIsNone(worker.poll())
        worker.launch.assert_not_called()

    def test_startup_gate_sends_waiting_heartbeat_without_claiming_any_job(self):
        worker=self.worker(wait_for_resume=True);worker.request=Mock(return_value={})
        self.assertIsNone(worker.poll())
        worker.request.assert_called_once_with('/api/collection/heartbeat',{'status':'等待手动登录或验证','browser_control':False})
        self.assertTrue(worker.waiting_for_resume)

    def test_startup_gate_survives_open_and_only_resume_allows_next(self):
        worker=self.worker(wait_for_resume=True);worker.browser_control=True
        process=Mock();process.poll.return_value=None
        worker.launch=Mock(return_value=process)
        worker.heartbeat=Mock(side_effect=lambda status:setattr(worker,'command','open'))
        worker.request=Mock(return_value={'job':None})
        self.assertIsNone(worker.poll());worker.launch.assert_called_once()
        self.assertTrue(worker.waiting_for_resume);worker.request.assert_not_called()
        worker.heartbeat=Mock(side_effect=lambda status:setattr(worker,'command','resume'))
        self.assertIsNone(worker.poll());self.assertFalse(worker.waiting_for_resume)
        process.stdin.write.assert_called_once_with(b'\n');worker.request.assert_not_called()
        process.poll.return_value=0;worker.heartbeat=Mock()
        self.assertIsNone(worker.poll());worker.request.assert_called_once_with('/api/collection/next')
        restarted=self.worker(wait_for_resume=True);self.assertTrue(restarted.waiting_for_resume)

    def query_process(self):
        process=Mock();process.poll.return_value=None
        process.stdout=io.BytesIO('搜索接口要求安全验证（verify_check）。窗口已保留；请本人验证\n'.encode('utf-8'))
        process.stdin=io.BytesIO()
        return process

    def immediate_reader(self):
        class Reader:
            def __init__(self,target,args,daemon):self.target=target;self.args=args;self.joined=False
            def start(self):self.target(*self.args)
            def join(self,timeout):self.joined=True
        return Reader

    def test_interactive_search_wait_expires_before_lease_and_closes_pipes(self):
        worker=self.worker();worker.browser_control=True;worker.headless=False;worker.visible_query=True
        process=self.query_process()
        worker.launch=Mock(return_value=process)
        worker.stop=Mock(side_effect=lambda child:setattr(child.poll,'return_value',1))
        with tempfile.TemporaryDirectory() as folder:
            worker.output=Path(folder)
            with patch.object(f2_worker.threading,'Thread',self.immediate_reader()),patch.object(f2_worker.time,'monotonic',side_effect=[0,481]):
                result=worker.query(JOB)
        self.assertEqual(result,{'status':'failed','error':'SearchNeedsVerification'})
        self.assertIn('--interactive',worker.launch.call_args.args[0])
        self.assertTrue(worker.launch.call_args.kwargs['observe_verification'])
        worker.stop.assert_called_once_with(process)
        self.assertTrue(process.stdin.closed);self.assertTrue(process.stdout.closed)

    def test_interactive_search_only_resumes_same_process_then_reads_one_report(self):
        worker=self.worker();worker.browser_control=True;worker.headless=False;worker.visible_query=True
        process=self.query_process();resumed=[]
        class Input(io.BytesIO):
            def write(self,data):
                resumed.append(data);process.poll.return_value=0
                return super().write(data)
        process.stdin=Input()
        good={'status':'success','account':JOB['douyin_id'],'count':0,'url':PROFILE_URL}
        with tempfile.TemporaryDirectory() as folder:
            worker.output=Path(folder)
            def launch(args,*unused,**keywords):
                report=Path(args[args.index('--output')+1])/'synthetic-report'
                report.mkdir()
                (report/'results.json').write_text(json.dumps({'results':[good]}),encoding='utf-8')
                return process
            worker.launch=Mock(side_effect=launch)
            def heartbeat(status):
                self.assertEqual(status,'等待手动登录或验证');worker.command='resume'
            worker.heartbeat=Mock(side_effect=heartbeat)
            with patch.object(f2_worker.threading,'Thread',self.immediate_reader()),patch.object(f2_worker.time,'monotonic',side_effect=[0,1]),patch.object(f2_worker.time,'sleep'):
                self.assertEqual(worker.query(JOB),good)
            source=next(worker.output.glob('job-*/accounts.json'))
            self.assertEqual(json.loads(source.read_text()),[JOB['douyin_id']])
        worker.launch.assert_called_once();self.assertEqual(resumed,[b'\n'])
        self.assertTrue(process.stdin.closed);self.assertTrue(process.stdout.closed)


if __name__=='__main__':unittest.main()
