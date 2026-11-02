"""Read-only loopback preview of actual review queries, invented fixtures only."""
import argparse
from datetime import timedelta
import json
from pathlib import Path
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from uuid import uuid4

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
from test_isolation import create_root,install


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--bulk',type=int,default=0);args=parser.parse_args()
    if not 0<=args.bulk<=10000:raise ValueError('Synthetic bulk range is 0..10000')
    root=create_root(args.root);install(root)
    import app
    import auth
    import publish_review as review
    import json_transport
    app.DB=root/'data/board.db';app.initialize()
    owner=dict(id=1,name='合成管理员',role='owner',permissions=list(auth.PERMISSIONS))
    today=review.now().date();day=today.isoformat();yesterday=(today-timedelta(days=1)).isoformat()
    source_at=min(review.now()-timedelta(minutes=1),review.now().replace(hour=8,minute=0,second=0,microsecond=0))
    if source_at.date()!=today:source_at=review.now()
    previous=source_at-timedelta(days=1)
    labels=['合成 · 超额达标','合成 · 部分完成','合成 · 暂无净增','合成 · 缺昨日基线','合成 · 作品减少','合成 · 采样失败','合成 · 目标未定','合成 · 无任务','合成 · 暂停任务','合成 · 采样过期']
    with app.connect() as db:
        app.begin_write(db)
        db.execute("UPDATE computers SET name='合成电脑甲',operator='合成负责人甲'")
        db.execute("INSERT INTO computers(id,name,shadow_account,ip,operator,category) VALUES('synthetic-preview-pc','合成电脑乙','','','合成负责人乙','')")
        for i,label in enumerate(labels):
            ident='synthetic-review-'+str(i);computer='synth-pc-1' if i<5 else 'synthetic-preview-pc'
            db.execute('INSERT INTO accounts(id,computer_id,slot,name,status,note,douyin_id,homepage) VALUES(?,?,?,?,?,?,?,?)',(ident,computer,10+i,label,'正常','','synthetic-handle-'+str(i),''))
            review.save_task(db,owner,dict(account_id=ident,day=day,target=None if i==6 else 5,task_state='none' if i==7 else 'paused' if i==8 else 'active',computer_id=computer,reason='仅供本机验证的合成任务',expected_revision=0))
            if i!=3:review.ingest(db,ident,str(uuid4()),previous.isoformat(),100)
            if i not in (7,8,9):review.ingest(db,ident,str(uuid4()),source_at.isoformat(),None if i==5 else 127 if i==0 else 100 if i==2 else 97 if i==4 else 102,failed=i==5)
        db.execute("DELETE FROM accounts WHERE id='synth-account-1'")
        if args.bulk:
            db.execute("INSERT INTO computers(id,name,shadow_account,ip,operator,category) VALUES('synthetic-bulk-pc','验证 · 大批量电脑','','','合成大批量负责人','')")
            db.executemany("INSERT INTO accounts(id,computer_id,slot,name,status,note,douyin_id,homepage) VALUES(?,'synthetic-bulk-pc',?,?,'正常','','','')",[('synthetic-bulk-'+str(i),i+1,'合成批量账号'+str(i)) for i in range(args.bulk)])
    actor=dict(id=1,name='本地合成预览（不是正式资料）',role='viewer',permissions=[])

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self,*a,**kw):super().__init__(*a,directory=str(PROJECT/'static'),**kw)
        def log_message(self,*args):pass
        def end_headers(self):self.send_header('Cache-Control','no-store');super().end_headers()
        def do_POST(self):self.send_error(405,'Read-only synthetic preview')
        def do_GET(self):
            path=urlparse(self.path)
            if path.path=='/api/publish-review':
                try:
                    with app.connect() as db:
                        app.begin_read(db)
                        data=review.overview(db,actor,{k:v[-1] for k,v in parse_qs(path.query).items()})
                    payload=json.dumps(data,ensure_ascii=False).encode();prepared=json_transport.prepare(payload,self.headers.get('Accept-Encoding'))
                    self.send_response(200 if prepared.acceptable else 406);self.send_header('Content-Type','application/json;charset=utf-8');self.send_header('Content-Length',str(prepared.content_length));self.send_header('Vary','Accept-Encoding')
                    if prepared.content_encoding:self.send_header('Content-Encoding',prepared.content_encoding)
                    self.end_headers();self.wfile.write(prepared.body)
                except ValueError:self.send_error(400,'Invalid synthetic query')
                return
            if path.path.startswith('/api/'):self.send_error(404);return
            if path.path=='/':self.path='/publish-review.html'
            super().do_GET()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    (root/'preview.json').write_text(json.dumps(dict(kind='synthetic-read-only',port=server.server_port,date=day,bulk=args.bulk)),encoding='utf-8')
    print('SYNTHETIC_READ_ONLY http://127.0.0.1:'+str(server.server_port)+'/publish-review.html',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()


if __name__=='__main__':main()
