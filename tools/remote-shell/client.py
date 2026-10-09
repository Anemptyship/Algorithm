#!/usr/bin/env python3
"""server.py 에 명령을 보내는 클라이언트.

    export REMOTE_SHELL_URL=https://xxxx.trycloudflare.com
    export REMOTE_SHELL_TOKEN=...
    python3 client.py 'uname -a'
    python3 client.py --cwd ~/project 'ls -al'
"""
import argparse
import json
import os
import sys
import urllib.request


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd")
    ap.add_argument("--cwd")
    ap.add_argument("--timeout", type=float, default=120)
    args = ap.parse_args()

    url = os.environ["REMOTE_SHELL_URL"].rstrip("/") + "/exec"
    payload = {"cmd": args.cmd, "timeout": args.timeout}
    if args.cwd:
        payload["cwd"] = args.cwd
    if not sys.stdin.isatty():
        payload["stdin"] = sys.stdin.read()

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {os.environ['REMOTE_SHELL_TOKEN']}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=args.timeout + 30) as r:
        res = json.load(r)

    sys.stdout.write(res.get("stdout", ""))
    sys.stderr.write(res.get("stderr", ""))
    if res.get("timeout"):
        sys.stderr.write("[timeout]\n")
    if res.get("error"):
        sys.stderr.write(f"[error] {res['error']}\n")
    code = res.get("code")
    sys.exit(code if isinstance(code, int) else 1)


if __name__ == "__main__":
    main()
