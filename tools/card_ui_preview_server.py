"""Isolated actual-static UI preview. No app import, database or write routes.

Control stdin: one initial private fixture object, then {"command":"stop"}.
Browser flags: ?preview_delay=0..5000 delays board reads; ?preview_fail=1
returns a synthetic 503. These flags clear only this preview origin's board cache.
Both variants serve the identical JSON bytes and deny every POST with 405.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from test_isolation import create_root, install


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    root = create_root(args.root)
    install(root)
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import mimetypes

    servers, threads = [], []
    try:
        line = sys.stdin.readline(8 * 1024 * 1024)
        config = json.loads(line)
        if config['baseline'] != '4597858e258d4d20f3fd83ec5c97eb3bc1ac3be3':
            raise ValueError('Unexpected frozen baseline')
        candidate = Path(config['candidate'])
        if candidate != PROJECT / 'static' or candidate.resolve() != candidate or candidate.is_symlink() or candidate.is_junction():
            raise ValueError('Candidate static directory must be the current project')
        safe_name = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:html|js|css|svg|png|ico)$')
        before = {name: base64.b64decode(content, validate=True) for name, content in config['before'].items() if safe_name.fullmatch(name)}
        if sum(map(len, before.values())) > 2 * 1024 * 1024:
            raise ValueError('Frozen assets are too large')
        board_bytes = config['board_json'].encode('utf-8')
        fingerprint = hashlib.sha256(board_bytes).hexdigest()
        if fingerprint != config['fingerprint']:
            raise ValueError('Synthetic fingerprint mismatch')
        board = json.loads(board_bytes)
        if len(board['phone_cards']['cards']) != 120 or len(board['accounts']) != 288:
            raise ValueError('Unexpected synthetic fixture size')
        guard = r"""(()=>{
 const q=new URLSearchParams(location.search),metrics={synthetic:true,boardReadyMs:null,renderMs:null,filterPaintMs:null};
 if(q.has('preview_delay')||q.has('preview_fail'))localStorage.removeItem('douyin-board-view');
 document.addEventListener('click',e=>{const a=e.target.closest?.('a[href]');if(a&&new URL(a.href,location.href).origin!==location.origin)e.preventDefault();},true);
 const visible=node=>{
  if(node.closest('details:not([open])'))return false;
  const r=node.getBoundingClientRect(),s=getComputedStyle(node);if(r.width<=0||r.height<=0||s.visibility==='hidden')return false;
  let left=0,top=0,right=innerWidth,bottom=innerHeight;
  for(let p=node.parentElement;p;p=p.parentElement){const style=getComputedStyle(p),box=p.getBoundingClientRect();if(/auto|scroll|hidden|clip/.test(style.overflowX)){left=Math.max(left,box.left);right=Math.min(right,box.right);}if(/auto|scroll|hidden|clip/.test(style.overflowY)){top=Math.max(top,box.top);bottom=Math.min(bottom,box.bottom);}}
  return Math.min(right,r.right)>Math.max(left,r.left)&&Math.min(bottom,r.bottom)>Math.max(top,r.top);
 };
 function measure(){
  const controls=[...document.querySelectorAll('#cardsWorkspace input:not([type=hidden]),#cardsWorkspace select,#cardsWorkspace button')].filter(visible);
  const overlaps=[];for(let i=0;i<controls.length;i++)for(let j=i+1;j<controls.length;j++){
   const a=controls[i],b=controls[j];if(a.contains(b)||b.contains(a))continue;
   const x=a.getBoundingClientRect(),y=b.getBoundingClientRect();
   if(Math.min(x.right,y.right)-Math.max(x.left,y.left)>2&&Math.min(x.bottom,y.bottom)-Math.max(x.top,y.top)>2)overlaps.push([a.id||a.tagName,b.id||b.tagName]);
  }
  const rows=[...document.querySelectorAll('#cardsBody tr')].filter(node=>node.getClientRects().length>0),height=innerHeight;
  const rowVisible=node=>{let top=0,bottom=height;for(let parent=node.parentElement;parent;parent=parent.parentElement){const style=getComputedStyle(parent);if(/auto|scroll|hidden|clip/.test(style.overflowY)){const box=parent.getBoundingClientRect();top=Math.max(top,box.top);bottom=Math.min(bottom,box.bottom);}}
   const table=node.closest('table');if(table)for(const th of table.querySelectorAll('thead th')){const box=th.getBoundingClientRect();if(box.top>=top&&box.top<bottom)top=Math.max(top,box.bottom);}
   const rect=node.getBoundingClientRect();return rect.top>=top-1&&rect.bottom<=bottom+1;
  };
  const value={...metrics,documentVisibility:document.visibilityState,paintMeasure:'input to two animation frames, includes scheduling',viewport:[innerWidth,height],devicePixelRatio,documentOverflow:document.documentElement.scrollWidth>innerWidth+1,
   visibleRows:rows.filter(rowVisible).length,
   controlsOverlap:overlaps.slice(0,10),rowCount:rows.length};
  document.documentElement.dataset.previewMetrics=JSON.stringify(value);
  return value;
 }
 let pendingInput=null;const frame=()=>requestAnimationFrame(()=>requestAnimationFrame(()=>{if(pendingInput!==null){metrics.filterPaintMs=+(performance.now()-pendingInput).toFixed(2);pendingInput=null;}measure();}));
 document.addEventListener('board-ready',()=>{metrics.boardReadyMs=+performance.now().toFixed(2);frame();});
 document.addEventListener('DOMContentLoaded',()=>{
  document.documentElement.dataset.previewSynthetic='120 cards / 288 accounts / 2026-11-02';
  const original=window.render;if(typeof original==='function')window.render=function(...args){const start=performance.now();try{return original.apply(this,args);}finally{metrics.renderMs=+(performance.now()-start).toFixed(2);frame();}};
  const body=document.getElementById('cardsBody');if(body)new MutationObserver(frame).observe(body,{childList:true,subtree:true});frame();
 });
 for(const event of ['input','change'])document.addEventListener(event,e=>{if(e.target.closest?.('#cardsWorkspace'))pendingInput=performance.now();},true);
 window.addEventListener('resize',frame);window.addEventListener('scroll',frame,{passive:true});
})();""".encode('utf-8')

        class PreviewHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, body, status=200, mime='application/json; charset=utf-8'):
                if not isinstance(body, bytes):
                    body = json.dumps(body, ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Synthetic-Data-Sha256', fingerprint)
                self.send_header('Content-Security-Policy', "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; base-uri 'none'; object-src 'none'; form-action 'none'")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_POST(self):
                self.close_connection = True
                self.reply({'error': '仅为本机合成预览，所有保存、导入及业务写入均被禁止', 'code': 'READ_ONLY_PREVIEW'}, 405)

            do_PUT = do_POST
            do_PATCH = do_POST
            do_DELETE = do_POST

            def do_GET(self):
                parsed = urlsplit(self.path)
                route = unquote(parsed.path)
                ref = urlsplit(self.headers.get('Referer', ''))
                flags = parse_qs(ref.query) if ref.netloc == self.headers.get('Host') else {}
                if route == '/api/board':
                    try:
                        delay = min(5000, max(0, int(flags.get('preview_delay', ['0'])[0])))
                    except ValueError:
                        delay = 0
                    if delay:
                        time.sleep(delay / 1000)
                    if flags.get('preview_fail', [''])[0] == '1':
                        return self.reply({'error': '合成本机加载失败测试；未读取任何真实资料'}, 503)
                    return self.reply(board_bytes)
                if route == '/api/auth/me':
                    return self.reply({'user': board['user'], 'initialized': True, 'can_setup': False})
                if route in ('/api/board-updates', '/api/updates'):
                    return self.reply({'cursor': board['cursor'], 'changes': []})
                if route == '/api/record-versions':
                    keys = parse_qs(parsed.query).get('keys', [''])[0].split(',')
                    return self.reply({'record_versions': {key: board['record_versions'].get(key) for key in keys if key}})
                if route == '/__preview__/meta':
                    return self.reply({'variant': self.server.variant, 'baseline': config['baseline'], 'cards': 120, 'accounts': 288, 'data_sha256': fingerprint, 'writes': False})
                if route == '/__preview__/guard.js':
                    return self.reply(guard, mime='text/javascript; charset=utf-8')
                name = 'index.html' if route == '/' else route.removeprefix('/')
                if not safe_name.fullmatch(name):
                    return self.reply({'error': '仅提供允许的本机预览资源'}, 404)
                if self.server.variant == 'before':
                    content = before.get(name)
                else:
                    path = candidate / name
                    content = path.read_bytes() if path.is_file() and path.resolve().parent == candidate and not path.is_symlink() else None
                if content is None:
                    return self.reply({'error': '本机预览资源不存在'}, 404)
                if name.endswith('.html'):
                    content = content.replace(b'<head>', b'<head><script src="/__preview__/guard.js"></script>', 1)
                mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
                return self.reply(content, mime=mime + ('; charset=utf-8' if mime.startswith('text/') or mime == 'application/javascript' else ''))

        for variant in ('before', 'after'):
            server = ThreadingHTTPServer(('127.0.0.1', 0), PreviewHandler)
            server.variant = variant
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            servers.append(server)
            threads.append(thread)
        urls = {variant + '_url': 'http://127.0.0.1:' + str(server.server_port) + '/?page=cards' for variant, server in zip(('before', 'after'), servers)}
        print(json.dumps({'kind': 'ready', **urls, 'data_sha256': fingerprint, 'cards': 120, 'accounts': 288,
            'baseline': config['baseline'], 'read_only': True, 'evidence_root': str(root),
            'flags': {'loading': '&preview_delay=2000', 'error': '&preview_fail=1'}}, ensure_ascii=False), flush=True)
        for line in sys.stdin:
            if json.loads(line) == {'command': 'stop'}:
                break
        return 0
    except BaseException as error:
        print(json.dumps({'kind': 'error', 'type': type(error).__name__, 'position': getattr(error, 'pos', None)}), flush=True)
        return 1
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


if __name__ == '__main__':
    raise SystemExit(main())
