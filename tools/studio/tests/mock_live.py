import json, re, sys, threading
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(sys.argv[1]); PORT = int(sys.argv[2])
CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'"
A, B = 'scail_' + 'a' * 32, 'scail_' + 'b' * 32
LOCK = threading.Lock()
CALLS = []
S = {
    A: dict(id=A, title='SCAIL-2 · 8스텝 · A.png', state='running', active=True, position=None, total=5, done=2, can_resume=False, error='', progress=dict(message='SCAIL-2 · 3/5구간 영상 생성', step=4, total_steps=8)),
    B: dict(id=B, title='SCAIL-2 · 8스텝 · B.png', state='cancelled', active=False, position=None, total=5, done=1, can_resume=True, error='', progress={}),
}

def item(j):
    parts, cur = [], 0.0
    for i in range(j['done']):
        parts.append(dict(index=i + 1, url=f'/v/a.mp4?{j["id"][6:8]}{i}', start=round(cur, 2), seconds=3.4)); cur += 3.4
    return dict(j, parts=parts, seconds_done=round(cur, 2), seconds_total=15.0, thumb='/v/ref.jpg', created=1)

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, body, ctype='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(data)))
        self.send_header('Accept-Ranges', 'bytes'); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        p = self.path.split('?')[0]
        if p == '/scail-live':
            h = (ROOT / 'scail-live.html').read_text().replace('<head>', f'<head>\n<meta http-equiv="Content-Security-Policy" content="{CSP}">', 1)
            return self.send(200, h.encode(), 'text/html; charset=utf-8')
        if p == '/static/scail-live.js': return self.send(200, (ROOT / 'scail-live.js').read_bytes(), 'text/javascript')
        if p == '/api/scail/live':
            with LOCK: return self.send(200, dict(items=[item(j) for j in S.values()], updated_at=1))
        if p.startswith('/api/scail/live/') and p.endswith('.mp4'):
            CALLS.append(('GET', p)); return self.send(200, (ROOT / 'mock_v/b.mp4').read_bytes(), 'video/webm')
        if p.startswith('/v/'):
            f = ROOT / 'mock_v' / p[3:]
            ct = 'image/jpeg' if p.endswith('.jpg') else 'video/webm'
            if f.exists():
                data = f.read_bytes(); rng = self.headers.get('Range')
                if rng:
                    m = re.match(r'bytes=(\d+)-(\d*)', rng); a = int(m[1]); b = int(m[2]) if m[2] else len(data) - 1
                    self.send_response(206); self.send_header('Content-Type', ct); self.send_header('Content-Range', f'bytes {a}-{b}/{len(data)}')
                    self.send_header('Content-Length', str(b - a + 1)); self.end_headers(); self.wfile.write(data[a:b + 1]); return
                return self.send(200, data, ct)
        if p == '/__calls': return self.send(200, CALLS)
        if p == '/__advance':
            with LOCK: S[A]['done'] += 1
            return self.send(200, {})
        if p == '/__empty':
            with LOCK: S.clear()
            return self.send(200, {})
        self.send(404, {'detail': 'nf'})
    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0)); body = self.rfile.read(n); p = self.path
        m = re.fullmatch(r'/api/jobs/(scail_[a-f0-9]{32})/cancel', p)
        if m:
            with LOCK:
                CALLS.append(('cancel', m[1]))
                j = S[m[1]]; j.update(state='cancelled', active=False, can_resume=True, progress={})
            return self.send(200, {'ok': True})
        m = re.fullmatch(r'/api/scail/live/(scail_[a-f0-9]{32})/resume', p)
        if m:
            msg = BytesParser().parsebytes(b'Content-Type: ' + self.headers['Content-Type'].encode() + b'\r\nMIME-Version: 1.0\r\n\r\n' + body)
            where = next(x.get_payload(decode=True).decode() for x in msg.get_payload() if x.get_param('name', header='content-disposition') == 'where')
            with LOCK:
                CALLS.append(('resume', m[1], where))
                j = S[m[1]]; j.update(state='running' if where == 'front' else 'queued', active=where == 'front', can_resume=False, position=None if where == 'front' else 3)
            return self.send(200, {'ok': True, 'position': 1})
        self.send(404, {'detail': 'nf'})

ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
