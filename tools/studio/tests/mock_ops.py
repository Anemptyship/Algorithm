import json, re, sys, threading, time, uuid
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(sys.argv[1]); PORT = int(sys.argv[2])
CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'"
LOCK = threading.Lock(); REQ = {}; CALLS = []
STATE = {'pending': [], 'restart': {}, 'busy': True}
TARGETS = [dict(path='studio_static/scail-repair.js', label='구간 수정 화면 동작 (JS)', tier='static'),
           dict(path='scail_repair.py', label='구간 수정 서버 (Python · 재시작 필요)', tier='python'),
           dict(path='youtube_api.py', label='유튜브 예약 서버 (Python · 재시작 필요)', tier='python')]
EVIL = '<img src=x onerror="window.__pwned=1"> <script>window.__pwned=2</script> 토큰은 만료됐어요'
DIFF = "--- a.js\n+++ a.js\n@@ -1,3 +1,3 @@\n a\n-t._t=setTimeout(()=>x,2600);\n+t._t=setTimeout(()=>x,4000);\n c\n"

def fields(headers, body):
    msg = BytesParser().parsebytes(b'Content-Type: ' + headers['Content-Type'].encode() + b'\r\nMIME-Version: 1.0\r\n\r\n' + body)
    return {p.get_param('name', header='content-disposition'): p.get_payload(decode=True).decode() for p in msg.get_payload()}

def finish(rid):
    time.sleep(2.0)
    with LOCK:
        r = REQ[rid]
        if r['kind'] == 'ask':
            r.update(status='answered', reply=EVIL)
        else:
            tier = 'python' if r['target'].endswith('.py') else 'static'
            r.update(status='proposed', reply='토스트가 보이는 시간을 4초로 늘려요.', diff=DIFF, changed=2, tier=tier,
                     checks=[['자바스크립트 문법', True, ''], ['정의되지 않은 이름 없음', False, "undefined name 'x'"]] if False else [['문법', True, ''], ['이름 검사', True, '']],
                     flags=['⚠ 위험할 수 있는 코드가 추가됐어요: 파일 삭제(os.remove)'] if tier == 'python' else [])

def public(r, full=False):
    d = dict(r)
    d['target_label'] = next((t['label'] for t in TARGETS if t['path'] == r['target']), '')
    if not full:
        for k in ('reply', 'diff', 'edits'): d.pop(k, None)
    return d

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, body, ctype='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        p = self.path.split('?')[0]
        if p == '/ops-assistant':
            h = (ROOT / 'ops-assistant.html').read_text().replace('<head>', f'<head>\n<meta http-equiv="Content-Security-Policy" content="{CSP}">', 1)
            return self.send(200, h.encode(), 'text/html; charset=utf-8')
        if p == '/static/ops-assistant.js': return self.send(200, (ROOT / 'ops-assistant.js').read_bytes(), 'text/javascript')
        if p == '/api/ops/targets': return self.send(200, dict(items=TARGETS))
        if p == '/api/ops/suggest':
            q = self.path.split('prompt=')[-1]
            return self.send(200, dict(target='youtube_api.py' if '%EC%9C%A0%ED%8A%9C%EB%B8%8C' in q else 'studio_static/scail-repair.js'))
        if p == '/api/ops/requests':
            with LOCK: return self.send(200, dict(items=[public(r) for r in sorted(REQ.values(), key=lambda r: -r['created'])], pending_restart=STATE['pending'], restart=STATE['restart']))
        m = re.fullmatch(r'/api/ops/requests/([a-f0-9]{32})', p)
        if m and m[1] in REQ:
            with LOCK: return self.send(200, public(REQ[m[1]], True))
        if p == '/__calls': return self.send(200, CALLS)
        self.send(404, {'detail': 'nf'})
    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0)); body = self.rfile.read(n); p = self.path
        if self.headers.get('X-Ops-Assistant') != '1': return self.send(403, {'detail': '허용되지 않은 요청이에요.'})
        f = fields(self.headers, body) if body else {}
        if p == '/api/ops/requests':
            rid = uuid.uuid4().hex
            with LOCK:
                REQ[rid] = dict(id=rid, created=time.time(), kind=f['kind'], prompt=f['prompt'], target=f['target'], status='queued', reply='', diff='',
                                checks=[], flags=[], changed=0, tier='', error='', restart_needed=0, applied_at=None, finished_at=None)
                CALLS.append(('create', dict(f)))
            threading.Thread(target=finish, args=(rid,), daemon=True).start()
            return self.send(200, dict(ok=True, id=rid, target=f['target'], preempted=None))
        m = re.fullmatch(r'/api/ops/requests/([a-f0-9]{32})/(apply|reject|undo)', p)
        if m:
            with LOCK:
                r = REQ[m[1]]; CALLS.append((m[2], m[1], dict(f)))
                if m[2] == 'apply':
                    r.update(status='applied', applied_at=time.time(), restart_needed=1 if r['tier'] == 'python' else 0)
                    if r['tier'] == 'python': STATE['pending'] = [m[1]]
                elif m[2] == 'reject': r.update(status='rejected')
                else: r.update(status='undone', restart_needed=0); STATE['pending'] = []
            return self.send(200, public(REQ[m[1]], True))
        if p == '/api/ops/restart':
            if STATE['busy'] and f.get('confirm_busy') != 'true':
                CALLS.append(('restart-refused',)); return self.send(409, {'detail': '지금 영상 생성이 돌고 있어요. 재시작하면 처음부터 다시 돼요.'})
            CALLS.append(('restart', dict(f))); STATE['restart'] = dict(state='running', at=time.time())
            return self.send(200, dict(ok=True, ids=STATE['pending']))
        self.send(404, {'detail': 'nf'})

ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
