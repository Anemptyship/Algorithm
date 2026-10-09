"""Mock of the studio API for browser-testing scail-repair.html under the site's CSP."""
import json
import re
import sys
import threading
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(sys.argv[1])
PORT = int(sys.argv[2])
CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'"
ID = 'scail_8b51b78c5cd645b4b1badbc24f3ba909'
POSTS = []
BY_RID = {}
LOCK = threading.Lock()
DELIVERS = []
VERSIONS = [
    dict(version=2, created=1791561285, current=True, video='/v/b.mp4', segment='/v/c.mp4', delivered=None, segment_delivered=None,
         first=224, last=268, note='검지 손가락', exporting=False),
    dict(version=1, created=1791560000, current=False, video='/v/a.mp4', segment=None, delivered=None, segment_delivered=None,
         first=239, last=241, note='', exporting=False),
]


def finish_export(version):
    import time
    time.sleep(2.0)
    with LOCK:
        for v in VERSIONS:
            if v['version'] == version:
                v.update(exporting=False, delivered='/v/b.mp4', segment_delivered='/v/c.mp4' if v['segment'] else None)


def parse_multipart(content_type, body):
    msg = BytesParser().parsebytes(b'Content-Type: ' + content_type.encode() + b'\r\nMIME-Version: 1.0\r\n\r\n' + body)
    fields = {}
    for part in msg.get_payload():
        name = part.get_param('name', header='content-disposition')
        payload = part.get_payload(decode=True) or b''
        if part.get_filename():
            fields[name] = {'file': part.get_filename(), 'size': len(payload)}
        else:
            fields[name] = payload.decode()
    return fields


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype='application/json', extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Accept-Ranges', 'bytes')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = self.path.split('?')[0]
        if p in ('/scail-repair', '/scail-repair.html'):
            html = (ROOT / 'scail-repair.html').read_text()
            html = html.replace('<head>', f'<head>\n<meta http-equiv="Content-Security-Policy" content="{CSP}">', 1)
            return self.send(200, html.encode(), 'text/html; charset=utf-8')
        if p == '/static/scail-repair.js':
            return self.send(200, (ROOT / 'scail-repair.js').read_bytes(), 'text/javascript')
        if p.startswith('/v/'):
            f = ROOT / 'mock_v' / p[3:]
            if f.exists():
                data = f.read_bytes()
                rng = self.headers.get('Range')
                if rng:
                    m = re.match(r'bytes=(\d+)-(\d*)', rng)
                    a = int(m[1]); b = int(m[2]) if m[2] else len(data) - 1
                    self.send_response(206)
                    self.send_header('Content-Type', 'video/webm')
                    self.send_header('Content-Range', f'bytes {a}-{b}/{len(data)}')
                    self.send_header('Content-Length', str(b - a + 1))
                    self.end_headers()
                    self.wfile.write(data[a:b + 1])
                    return
                return self.send(200, data, 'video/webm')
        if p == '/api/scail/repair/candidates':
            return self.send(200, {'items': [
                {'identity': ID, 'title': '테스트 영상 A', 'version': 0, 'updated_at': 1791554296, 'video': '/v/a.mp4', 'reference': '/v/ref.jpg'},
                {'identity': 'scail_' + 'e' * 32, 'title': '테스트 영상 B', 'version': 2, 'updated_at': 1791550000, 'video': '/v/a.mp4', 'reference': '/v/ref.jpg'}]})
        if p == '/v/ref.jpg':
            return self.send(200, (ROOT / 'mock_v/ref.jpg').read_bytes(), 'image/jpeg')
        m = re.fullmatch(r'/api/scail/repair/(scail_[a-f0-9]{32})/defaults', p)
        if m:
            return self.send(200, dict(title='t', steps=8, dpo=True, lora_strength=0.8, shift=5.0, cfg=1.0, pose_strength=1.0,
                                       dpo_strength=1.0, tail=6, width=736, height=1280, prompt='p', max_seconds=2.92, fps=24))
        m = re.fullmatch(r'/api/scail/repair/(scail_[a-f0-9]{32})/([a-f0-9]{32})/status', p)
        if m:
            with LOCK:
                post = BY_RID.get(m[2])
            if not post:
                return self.send(200, {'state': 'queued', 'position': 1, 'active': False})
            start, end = float(post['start']), float(post['end'])
            first, last = int(start * 24), int(end * 24) - 1
            return self.send(200, dict(state='done', video='/v/b.mp4', segment='/v/c.mp4', first=first, last=last,
                                       addition=post.get('addition') or 'Both hands keep five distinct fingers.',
                                       seed=int(post['seed']) if post.get('seed') else 4242,
                                       options=json.loads(post.get('options') or '{}'),
                                       request=dict(start=start, end=end, note=post.get('note'))))
        mv = re.fullmatch(r'/api/scail/repair/(scail_[a-f0-9]{32})/versions', p)
        if mv:
            with LOCK:
                return self.send(200, {'items': [dict(v) for v in VERSIONS]})
        if p == '/__delivers':
            return self.send(200, DELIVERS)
        if p == '/__posts':
            return self.send(200, POSTS)
        self.send(404, {'detail': 'nf'})

    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(n)
        md = re.fullmatch(r'/api/scail/repair/(scail_[a-f0-9]{32})/deliver', self.path)
        if md:
            fields = parse_multipart(self.headers['Content-Type'], body)
            version = int(fields['version'])
            with LOCK:
                DELIVERS.append(fields)
                for v in VERSIONS:
                    if v['version'] == version:
                        v['exporting'] = True
            threading.Thread(target=finish_export, args=(version,), daemon=True).start()
            return self.send(200, dict(ok=True, queued=True, version=version))
        m = re.fullmatch(r'/api/scail/jobs/(scail_[a-f0-9]{32})/repair', self.path)
        if not m:
            return self.send(404, {'detail': 'nf'})
        fields = parse_multipart(self.headers['Content-Type'], body)
        with LOCK:
            POSTS.append(fields)
            BY_RID[fields['request_id']] = fields
        self.send(200, dict(ok=True, queued=True, rid=fields['request_id'], prioritised=True, preempted={'action': 'cancelled', 'job': 'x'}))


ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
