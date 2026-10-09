# remote-shell

SSH(22번 포트)를 쓸 수 없는 곳(예: Claude Code 클라우드 세션)에서 HTTPS만으로 리눅스 PC에 명령을 보내는 최소 도구.

```
[Claude 클라우드] --HTTPS--> [*.trycloudflare.com] --터널--> [PC: 127.0.0.1:8722 server.py]
```

## PC에서

```bash
git clone https://github.com/anemptyship/algorithm.git   # 또는 git pull
cd algorithm/tools/remote-shell
./start.sh
```

출력되는 `REMOTE_SHELL_URL`, `REMOTE_SHELL_TOKEN` 두 줄을 Claude에게 전달한다.
python3, curl 만 있으면 되고 `cloudflared` 가 없으면 자동으로 받는다. 포트포워딩이나 방화벽 설정은 필요 없다.

## 클라이언트

```bash
export REMOTE_SHELL_URL=https://xxxx.trycloudflare.com
export REMOTE_SHELL_TOKEN=...
python3 client.py 'uname -a'
python3 client.py --cwd ~/work 'git status'
echo hello | python3 client.py 'cat > /tmp/a.txt'
```

## 주의

- 토큰을 가진 사람은 PC에서 **아무 명령이나** 실행할 수 있다. 토큰을 공유할 상대를 가려서 주고, 다 쓰면 Ctrl+C로 끈다.
- `start.sh` 를 다시 실행할 때마다 주소와 토큰이 새로 생기고, 이전 것은 무효가 된다.
- 서버는 실행한 사용자 권한으로 명령을 돌린다. root로 실행하지 말 것.
