#!/usr/bin/env bash
# server.py 를 띄우고 Cloudflare 퀵 터널로 HTTPS 주소를 만든다. Ctrl+C 로 둘 다 종료.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8722}"
export REMOTE_SHELL_TOKEN="${REMOTE_SHELL_TOKEN:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')}"

CLOUDFLARED="$(command -v cloudflared || true)"
if [ -z "$CLOUDFLARED" ]; then
  case "$(uname -m)" in
    x86_64) ARCH=amd64 ;;
    aarch64|arm64) ARCH=arm64 ;;
    *) echo "unsupported arch: $(uname -m)"; exit 1 ;;
  esac
  CLOUDFLARED="./cloudflared"
  if [ ! -x "$CLOUDFLARED" ]; then
    echo "cloudflared 다운로드 중..."
    curl -fsSL -o "$CLOUDFLARED" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$ARCH"
    chmod +x "$CLOUDFLARED"
  fi
fi

python3 server.py --port "$PORT" > server.log 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID ${TUNNEL_PID:-} 2>/dev/null' EXIT

"$CLOUDFLARED" tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" > tunnel.log 2>&1 &
TUNNEL_PID=$!

echo "터널 주소 기다리는 중..."
for _ in $(seq 60); do
  URL="$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' tunnel.log | head -1 || true)"
  [ -n "$URL" ] && break
  sleep 1
done
[ -z "$URL" ] && { echo "터널 생성 실패. tunnel.log 확인"; exit 1; }

echo
echo "REMOTE_SHELL_URL=$URL"
echo "REMOTE_SHELL_TOKEN=$REMOTE_SHELL_TOKEN"
echo
echo "위 두 줄을 Claude 에게 전달하세요. 종료하려면 Ctrl+C."
wait
