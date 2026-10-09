#!/usr/bin/env python3
"""HTTPS(터널) 너머에서 명령을 받아 실행하는 최소 원격 셸 서버.

127.0.0.1 에만 바인딩하고, cloudflared 같은 터널로만 외부에 노출한다.
모든 요청은 Authorization: Bearer <TOKEN> 헤더가 있어야 한다.

사용:
    python3 server.py              # 토큰 자동 생성 후 출력
    REMOTE_SHELL_TOKEN=... python3 server.py --port 8722
"""
import argparse
import hmac
import json
import os
import secrets
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 10 * 1024 * 1024
TOKEN = ""


def _text(out):
    # TimeoutExpired 의 출력은 text=True 여도 bytes 로 올 수 있다.
    if isinstance(out, bytes):
        return out.decode(errors="replace")
    return out or ""


class Handler(BaseHTTPRequestHandler):
    def _authorized(self):
        got = self.headers.get("Authorization", "")
        return hmac.compare_digest(got.encode(), f"Bearer {TOKEN}".encode())

    def _send(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        if self.path == "/health":
            return self._send(200, {"ok": True, "user": os.environ.get("USER"), "cwd": os.getcwd()})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        if self.path != "/exec":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY:
            return self._send(413, {"error": "body too large"})
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
            cmd = req["cmd"]
        except (ValueError, KeyError):
            return self._send(400, {"error": 'expected JSON {"cmd": "..."}'})

        cwd = os.path.expanduser(req.get("cwd") or "~")
        timeout = min(float(req.get("timeout", 120)), 3600)
        try:
            p = subprocess.run(
                ["bash", "-lc", cmd],
                cwd=cwd,
                input=req.get("stdin"),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            self._send(200, {"code": p.returncode, "stdout": p.stdout, "stderr": p.stderr})
        except subprocess.TimeoutExpired as e:
            self._send(200, {"code": None, "timeout": True,
                             "stdout": _text(e.stdout), "stderr": _text(e.stderr)})
        except OSError as e:
            self._send(200, {"code": None, "error": str(e)})

    def log_message(self, fmt, *args):
        print(f"[{self.client_address[0]}] {fmt % args}", flush=True)


def main():
    global TOKEN
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8722)
    args = ap.parse_args()

    TOKEN = os.environ.get("REMOTE_SHELL_TOKEN") or secrets.token_urlsafe(32)
    print(f"TOKEN: {TOKEN}", flush=True)
    print(f"listening on http://127.0.0.1:{args.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
