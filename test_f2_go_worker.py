"""Go worker transport, identity and verification checks without network or browsers."""
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import importlib.util
import unittest
from unittest.mock import Mock, patch

import f2_worker


TOKEN='synthetic-only-worker-token-0000000000000000'
ENV={'DOUYIN_BOARD_COLLECTION_ENABLED':'1',
     'DOUYIN_BOARD_WORKER_ORIGIN':'http://127.0.0.1:18790',
     'DOUYIN_BOARD_WORKER_TOKEN':TOKEN}
PROFILE_URL='https://www.douyin.com/user/MS4wSyntheticOnly'
JOB={'account_id':'synthetic-account','token':'synthetic-job-lease',
     'douyin_id':'0012345678','homepage':PROFILE_URL}


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
                for field in ('profile','output','index'):
                    candidate=static/'private-material'
                    runtime={**ENV,'DOUYIN_BOARD_F2_PYTHON':__file__,'DOUYIN_BOARD_F2_SCRIPT':__file__,
                             'DOUYIN_BOARD_F2_PROFILE':str(candidate if field=='profile' else safe),
                             'DOUYIN_BOARD_F2_OUTPUT':str(candidate if field=='output' else Path(folder)/'private-output'),
                             'DOUYIN_BOARD_F2_PROFILE_INDEX':str(candidate) if field=='index' else ''}
                    with self.subTest(static=str(static),field=field),patch.object(f2_worker,'ROOT',root),patch.dict(os.environ,runtime,clear=True):
                        worker=f2_worker.Worker()
                        with self.assertRaisesRegex(ValueError,'网页资源'):worker.configure_runtime()
                    self.assertFalse(candidate.exists())

    def test_runtime_uses_pinned_original_and_ignores_legacy_script_or_visible_query(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime={**ENV,'DOUYIN_BOARD_F2_PYTHON':__file__,'DOUYIN_BOARD_F2_SCRIPT':__file__,
                     'DOUYIN_BOARD_F2_PROFILE':folder,'DOUYIN_BOARD_F2_OUTPUT':str(Path(folder)/'output'),
                     'DISPLAY':':synthetic-only','DOUYIN_BOARD_F2_HEADLESS':'0'}
            with patch.dict(os.environ,runtime,clear=True):
                worker=f2_worker.Worker();worker.configure_runtime();self.assertEqual(worker.script,f2_worker.original_script())
            for visible,headless in (('0','0'),('1','0'),('1','1'),('obsolete','0')):
                with self.subTest(visible=visible,headless=headless),patch.dict(os.environ,{**runtime,
                        'DOUYIN_BOARD_F2_VISIBLE_QUERY':visible,'DOUYIN_BOARD_F2_HEADLESS':headless},clear=True):
                    worker=f2_worker.Worker();worker.configure_runtime()
                    self.assertEqual(worker.script,f2_worker.original_script());self.assertNotEqual(worker.script,Path(__file__))
                    self.assertFalse(hasattr(worker,'visible_query'))

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
        self.assertEqual(worker.launch.call_args.args[1],f2_worker.subprocess.DEVNULL)
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
             'DOUYIN_BOARD_F2_PROFILE_INDEX':'synthetic-private-index-file',
             'BOARD_GO_PG_DSN':'synthetic-private-dsn','BOARD_GO_COLLECTION_TOKEN':'synthetic-server-token',
             'DATABASE_URL':'synthetic-database-url','PGPASSWORD':'synthetic-password',
             'PGPASSFILE':'synthetic-passfile','PATH':'synthetic-safe-path'}
        with patch.dict(os.environ,env,clear=True),patch.object(f2_worker.subprocess,'Popen') as launch:
            worker.launch(['login'],f2_worker.subprocess.PIPE)
        inherited=launch.call_args.kwargs['env']
        self.assertEqual(inherited,{'PATH':'synthetic-safe-path'})
        self.assertIn('--browser-executable',launch.call_args.args[0])
        self.assertNotIn(TOKEN,launch.call_args.args[0])
        self.assertEqual(launch.call_args.args[0][3],str(f2_worker.original_script()))
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

    def delivery_body(self,**changed):
        return {'account_id':JOB['account_id'],'token':JOB['token'],'count':1961,
                'source_url':PROFILE_URL,'matched_douyin_id':JOB['douyin_id'],
                'sampled_at':'2026-11-19T12:34:56.123456+00:00',**changed}

    def test_delivery_receipt_records_only_confirmed_allowlisted_values_and_canonical_url(self):
        worker=self.worker(DOUYIN_BOARD_WORKER_ORIGIN='http://127.0.0.1:18880')
        secret='synthetic-secret-that-must-never-be-saved'
        body=self.delivery_body(count=0,source_url=PROFILE_URL+'/?share='+secret+'#'+secret,
                                cookie=secret,headers={'Authorization':secret})
        worker.request=Mock(return_value={'ok':True,'duplicate':False,'ignored':False,'debug':secret})
        with tempfile.TemporaryDirectory() as folder:
            worker.output=Path(folder)
            self.assertTrue(worker.deliver({**JOB,'cookie':secret},body,once=True))
            files=list(worker.output.glob('delivery-*.json'));self.assertEqual(len(files),1)
            text=files[0].read_text(encoding='utf-8');receipt=json.loads(text)
            if os.name!='nt':self.assertEqual(stat.S_IMODE(files[0].stat().st_mode),0o600)
        self.assertEqual(receipt['count'],0);self.assertEqual(receipt['source_url'],PROFILE_URL)
        self.assertEqual(receipt['server_origin'],'http://127.0.0.1:18880')
        self.assertEqual(receipt['sampled_at'],body['sampled_at']);self.assertIn('deliveredAt',receipt)
        self.assertTrue(receipt['ok']);self.assertTrue(receipt['persisted']);self.assertEqual(receipt['status'],'success')
        self.assertEqual(set(receipt),{'account_id','douyin_id','count','source_url','sampled_at','deliveredAt',
            'server_origin','ok','duplicate','ignored','persisted','status'})
        for hidden in (TOKEN,JOB['token'],secret,'Authorization','cookie','headers','matched_douyin_id'):
            self.assertNotIn(hidden,text)

    def test_ignored_and_non_boolean_ok_cannot_be_reported_as_persisted_success(self):
        for result in ({'ignored':True},{'ok':True,'ignored':True},{'ok':1},{'ok':'true'},{'ok':False}):
            with self.subTest(result=result),tempfile.TemporaryDirectory() as folder:
                worker=self.worker();worker.output=Path(folder);worker.request=Mock(return_value=result)
                self.assertFalse(worker.deliver(JOB,self.delivery_body(),once=True))
                receipt=json.loads(next(worker.output.glob('delivery-*.json')).read_text(encoding='utf-8'))
                self.assertFalse(receipt['persisted']);self.assertEqual(receipt['status'],'failed')
                self.assertEqual(receipt['ok'],result.get('ok') is True)

    def test_failed_receipt_never_writes_zero_or_error_details_or_overwrites_prior_success(self):
        worker=self.worker();worker.request=Mock(return_value={'ok':True})
        with tempfile.TemporaryDirectory() as folder:
            worker.output=Path(folder)
            self.assertTrue(worker.deliver(JOB,self.delivery_body(),once=True))
            original=next(worker.output.glob('delivery-*.json'));before=original.read_bytes()
            failed=self.delivery_body(error='synthetic-private-error',count=0)
            self.assertFalse(worker.deliver(JOB,failed,once=True))
            self.assertEqual(original.read_bytes(),before)
            receipts=[json.loads(file.read_text(encoding='utf-8')) for file in worker.output.glob('delivery-*.json')]
        failed=next(receipt for receipt in receipts if receipt['status']=='failed')
        self.assertFalse(failed['persisted']);self.assertNotIn('count',failed);self.assertNotIn('source_url',failed)
        self.assertNotIn('synthetic-private-error',json.dumps(receipts))

    def test_delivery_receipts_reject_boolean_negative_or_excessive_count(self):
        for count in (True,-1,10000001,None):
            with self.subTest(count=count),tempfile.TemporaryDirectory() as folder:
                worker=self.worker();worker.output=Path(folder);worker.request=Mock(return_value={'ok':False})
                self.assertFalse(worker.deliver(JOB,self.delivery_body(count=count),once=True))
                receipt=json.loads(next(worker.output.glob('delivery-*.json')).read_text(encoding='utf-8'))
                self.assertNotIn('count',receipt);self.assertNotIn('source_url',receipt);self.assertFalse(receipt['persisted'])

    def test_receipt_io_failure_does_not_retry_delivery_or_repeat_upstream_or_leak_error(self):
        worker=self.worker();worker.output=Path('synthetic-output')
        worker.query=Mock(return_value={'status':'success','account':JOB['douyin_id'],'count':1961,'url':PROFILE_URL})
        worker.request=Mock(return_value={'ok':True})
        with patch.object(f2_worker.tempfile,'NamedTemporaryFile',side_effect=OSError(TOKEN)), \
             patch.object(f2_worker.time,'sleep') as slept,patch('builtins.print') as output:
            self.assertTrue(worker.collect(JOB,once=True))
        worker.query.assert_called_once_with(JOB);worker.request.assert_called_once();slept.assert_not_called()
        self.assertEqual(output.call_args.args,('服务器回执已收到，但本机回执文件未保存',))
        self.assertNotIn(TOKEN,str(output.call_args))

    def test_delivery_retry_reuses_same_sample_then_records_duplicate_ack_once(self):
        worker=self.worker(DOUYIN_BOARD_WORKER_ORIGIN='http://127.0.0.1:8187')
        worker.query=Mock(return_value={'status':'success','account':JOB['douyin_id'],'count':1961,'url':PROFILE_URL})
        worker.request=Mock(side_effect=[OSError('synthetic transport failure'),{'ok':True,'duplicate':True}])
        with tempfile.TemporaryDirectory() as folder,patch.object(f2_worker.time,'sleep'):
            worker.output=Path(folder);self.assertTrue(worker.collect(JOB,once=True))
            files=list(worker.output.glob('delivery-*.json'));self.assertEqual(len(files),1)
            receipt=json.loads(files[0].read_text(encoding='utf-8'))
        worker.query.assert_called_once_with(JOB);self.assertEqual(worker.request.call_count,2)
        self.assertEqual(worker.request.call_args_list[0],worker.request.call_args_list[1])
        self.assertEqual(receipt['server_origin'],'http://127.0.0.1:8187')
        self.assertTrue(receipt['duplicate']);self.assertTrue(receipt['persisted']);self.assertEqual(receipt['count'],1961)

    def test_receipt_is_optional_without_a_configured_output(self):
        worker=self.worker();worker.request=Mock(return_value={'ok':True})
        with patch.object(f2_worker.tempfile,'NamedTemporaryFile') as opened:
            self.assertTrue(worker.deliver(JOB,self.delivery_body(),once=True))
        opened.assert_not_called();worker.request.assert_called_once()

    def test_missing_or_conflicting_profile_does_not_start_upstream_or_pause_queue(self):
        for profiles,code in (({},'ProfileMissing'),({JOB['douyin_id']:{PROFILE_URL,PROFILE_URL+'Other'}},'ProfileConflict')):
            worker=self.worker();worker.profiles=f2_worker.ProfileIndex(profiles);worker.failures=3
            worker.launch=Mock();worker.deliver=Mock(return_value=False)
            job={**JOB,'homepage':''}
            self.assertFalse(worker.collect(job,once=True));worker.launch.assert_not_called()
            self.assertEqual(worker.failures,3);worker.deliver.assert_called_once()
            body=worker.deliver.call_args.args[1]
            self.assertEqual(body['error'],f2_worker.ERRORS[code]);self.assertTrue(body['profile_required']);self.assertFalse(body['blocked'])
            self.assertNotIn('count',body);self.assertNotIn('source_url',body);self.assertNotIn('sampled_at',body)
            self.assertEqual(worker.query(job),{'status':'failed','error':code})
            worker.launch.assert_not_called()

    def test_real_success_and_failure_still_have_one_sampling_timestamp(self):
        for row in ({'status':'success','account':JOB['douyin_id'],'count':0,'url':PROFILE_URL},
                    {'status':'failed','error':'RuntimeError'}):
            worker=self.worker();worker.query=Mock(return_value=row);worker.deliver=Mock(return_value=False)
            worker.collect(JOB,once=True);worker.query.assert_called_once();worker.deliver.assert_called_once()
            body=worker.deliver.call_args.args[1]
            self.assertIsNotNone(f2_worker.datetime.fromisoformat(body['sampled_at']).tzinfo)
            self.assertNotIn('profile_required',body)
            if row['status']=='success':self.assertEqual(body['count'],0)
            else:self.assertNotIn('count',body)

    def test_profile_required_receipt_is_needs_profile_without_a_sample_or_failed_count(self):
        for code in ('ProfileMissing','ProfileConflict'):
            worker=self.worker();worker.request=Mock(return_value={'ok':True})
            body={'account_id':JOB['account_id'],'token':JOB['token'],'error':f2_worker.ERRORS[code],
                  'profile_required':True,'blocked':False}
            with tempfile.TemporaryDirectory() as folder:
                worker.output=Path(folder);self.assertFalse(worker.deliver(JOB,body,once=True))
                receipt=json.loads(next(worker.output.glob('delivery-*.json')).read_text(encoding='utf-8'))
            self.assertEqual(receipt['status'],'needs_profile');self.assertTrue(receipt['ok']);self.assertFalse(receipt['persisted'])
            self.assertNotIn('sampled_at',receipt);self.assertNotIn('count',receipt);self.assertNotIn('source_url',receipt)
            self.assertIn('deliveredAt',receipt)

    def run_http_failure(self,raw,route='/api/collection/result'):
        secret='synthetic-private-response-value'
        error=f2_worker.HTTPError('http://127.0.0.1:18790/'+secret,400,secret,{'Set-Cookie':secret},io.BytesIO(raw))
        worker=self.worker();worker.headless=True;worker.opener=Mock();worker.opener.open.side_effect=error
        worker.poll=Mock(side_effect=lambda **unused:worker.request(route,{'token':TOKEN,'account_id':JOB['account_id']}))
        with patch('builtins.print') as output:self.assertEqual(worker.run(once=True),1)
        rendered=str(output.call_args_list)
        for hidden in (secret,TOKEN,JOB['account_id'],'Set-Cookie','Authorization','SELECT * FROM'):
            self.assertNotIn(hidden,rendered)
        worker.opener.open.assert_called_once()
        return output.call_args.args[0],error

    def test_http_diagnostics_only_recognize_the_fixed_sampling_error(self):
        raw=json.dumps({'error':'采样时间无效，未写入采集结果','Authorization':'synthetic-private-response-value'}).encode()
        message,error=self.run_http_failure(raw)
        self.assertEqual(message,'采集器请求失败（阶段：结果回填；HTTP 400；分类：采样时间无效）')
        self.assertEqual(error.worker_category,'invalid_sampling_time')

    def test_http_diagnostics_unknown_oversized_invalid_or_secret_responses_stay_safe(self):
        raw_values=[b'not-json Authorization SELECT * FROM',
            json.dumps({'error':'synthetic-private-response-value'}).encode(),
            json.dumps({'error':'采样时间无效，未写入采集结果synthetic-private-response-value'}).encode(),
            json.dumps({'error':'unknown','padding':'x'*9000}).encode()]
        for raw in raw_values:
            with self.subTest(size=len(raw)):
                message,error=self.run_http_failure(raw)
                self.assertEqual(message,'采集器请求失败（阶段：结果回填；HTTP 400；分类：未分类HTTP错误）')
                self.assertLessEqual(error.fp.tell(),8193)
        message,error=self.run_http_failure(b'{}','/unknown?token=synthetic-private-response-value')
        self.assertEqual(message,'采集器请求失败（阶段：请求；HTTP 400；分类：未分类HTTP错误）')

    def test_http_diagnostics_do_not_trust_exception_attributes_as_log_content(self):
        error=f2_worker.HTTPError('synthetic-private-url',400,'synthetic-private-message',{},None)
        error.worker_stage='synthetic-private-stage';error.worker_category=['synthetic-private-category'];error.code='synthetic-private-code'
        self.assertEqual(f2_worker.http_error_message(error),'采集器请求失败（阶段：请求；HTTP ?；分类：未分类HTTP错误）')

    def test_resolved_profile_is_bound_to_identity_and_cannot_be_replaced_by_report(self):
        worker=self.worker();worker.profiles=f2_worker.ProfileIndex({JOB['douyin_id']:{PROFILE_URL}})
        worker.query=Mock(return_value={'status':'success','account':JOB['douyin_id'],'count':0,'url':PROFILE_URL+'Other'})
        worker.deliver=Mock(return_value=False)
        self.assertFalse(worker.collect({**JOB,'homepage':''},once=True))
        self.assertEqual(worker.query.call_args.args[0]['homepage'],PROFILE_URL)
        body=worker.deliver.call_args.args[1];self.assertIn('error',body);self.assertNotIn('count',body)

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

    def test_original_query_timeout_never_opens_an_interactive_retry(self):
        worker=self.worker();worker.browser_control=True;worker.headless=False
        process=Mock();process.poll.return_value=None;process.stdout=io.BytesIO(b'synthetic-hidden-output');process.stdin=None
        worker.launch=Mock(return_value=process)
        worker.stop=Mock(side_effect=lambda child:setattr(child.poll,'return_value',1))
        with tempfile.TemporaryDirectory() as folder:
            worker.output=Path(folder)
            with patch.object(f2_worker.time,'monotonic',side_effect=[0,181]):
                result=worker.query(JOB)
        self.assertEqual(result,{'status':'failed','error':'CollectorTimedOut'})
        self.assertNotIn('--interactive',worker.launch.call_args.args[0]);self.assertNotIn('--headed',worker.launch.call_args.args[0])
        worker.launch.assert_called_once()
        worker.stop.assert_called_once_with(process)
        self.assertTrue(process.stdout.closed)

    def test_original_query_receives_only_bound_url_and_exact_expected_handle(self):
        worker=self.worker();worker.browser_control=True;worker.headless=False
        process=Mock();process.poll.return_value=0;process.stdin=None;process.stdout=None
        good={'status':'success','account':JOB['douyin_id'],'count':0,'url':PROFILE_URL}
        with tempfile.TemporaryDirectory() as folder:
            worker.output=Path(folder)
            def launch(args,*unused,**keywords):
                report=Path(args[args.index('--output')+1])/'synthetic-report'
                report.mkdir()
                (report/'results.json').write_text(json.dumps({'results':[good]}),encoding='utf-8')
                return process
            worker.launch=Mock(side_effect=launch)
            self.assertEqual(worker.query(JOB),good)
            source=next(worker.output.glob('job-*/accounts.json'))
            self.assertEqual(json.loads(source.read_text()),[{'url':PROFILE_URL,'account':JOB['douyin_id']}])
            self.assertNotIn(JOB['token'],source.read_text())
        worker.launch.assert_called_once();self.assertEqual(worker.launch.call_args.args[1],f2_worker.subprocess.DEVNULL)
        self.assertNotIn('--interactive',worker.launch.call_args.args[0]);self.assertNotIn('--headed',worker.launch.call_args.args[0])

    def test_original_source_hash_and_parser_identity_zero_count_are_unchanged(self):
        script=f2_worker.original_script()
        spec=importlib.util.spec_from_file_location('original_core_fixture',script);core=importlib.util.module_from_spec(spec);spec.loader.exec_module(core)
        item={'sec_uid':'MS4wSyntheticOnly','expected_account':JOB['douyin_id']}
        data={'user':{'sec_uid':item['sec_uid'],'unique_id':JOB['douyin_id'],'aweme_count':0}}
        self.assertEqual(core.parse_user(data,item)['count'],0)
        for changed in ({'sec_uid':'MS4wOther'},{'unique_id':'12345678'}):
            with self.assertRaises(core.IdentityMismatch):core.parse_user({'user':{**data['user'],**changed}},item)
        for count in (None,True,-1):
            with self.assertRaises(core.ProfileUnavailable):core.parse_user({'user':{**data['user'],'aweme_count':count}},item)

    def test_modified_original_core_is_never_executed(self):
        worker=self.worker();worker.python=Path('synthetic-python');worker.profile=Path('synthetic-profile')
        with patch.object(Path,'read_bytes',return_value=b'not-the-original'),patch.object(f2_worker.subprocess,'Popen') as opened:
            with self.assertRaisesRegex(ValueError,'^内置原始采集程序校验失败，请核对安装文件$'):worker.launch(['login'])
        opened.assert_not_called()


if __name__=='__main__':unittest.main()
