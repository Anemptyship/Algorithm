"""Restart the studio service after an approved server-code change and put the previous code back if it does not come up.

Started by ops_assistant through `systemd-run --user`, so it lives outside the service it restarts.
usage: ops_restart_guard.py <job.json>
Only files named in the job's "restore" list (each with its own backup copy) are ever touched.
"""
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

job = json.loads(Path(sys.argv[1]).read_text())
BASE = Path(job['base']).resolve()
STATUS = Path(job['status_path'])
ALLOWED = {rel for rel, _, _ in job['restore']}
TIMING = dict(first_pause=2, startup_pause=6, healthy_wait=150, settle=20, poll=3)
TIMING.update(job.get('timing') or {})


def say(state, **extra):
    STATUS.write_text(json.dumps(dict(state=state, at=time.time(), ids=job['ids'], **extra), ensure_ascii=False))


def healthy(seconds=5):
    try:
        with urllib.request.urlopen(job['health'], timeout=seconds) as response:
            return response.status == 200
    except Exception:
        return False


def restart():
    subprocess.run(['systemctl', '--user', 'restart', job['service']], timeout=90, check=False)


def wait_healthy(limit):
    end = time.time() + limit
    time.sleep(TIMING['startup_pause'])
    while time.time() < end:
        if healthy():
            return True
        time.sleep(TIMING['poll'])
    return False


def stable():
    """Up, and still up a while later (a crash loop restarts quickly and looks healthy in between)."""
    if not wait_healthy(TIMING['healthy_wait']):
        return False
    time.sleep(TIMING['settle'])
    return healthy() and healthy()


def telegram(key, text):
    try:
        with sqlite3.connect(BASE / 'telegram_data/bridge.sqlite3', timeout=20) as conn:
            owner = conn.execute("SELECT value FROM config WHERE key='owner'").fetchone()
            if owner:
                conn.execute('INSERT OR IGNORE INTO outbox(id,created,method,payload,attachment) VALUES(?,?,?,?,?)',
                             (key, time.time(), 'sendMessage', json.dumps({'chat_id': int(owner[0]), 'text': text}, ensure_ascii=False), None))
    except Exception:
        pass


def mark(state, error=''):
    with sqlite3.connect(job['db'], timeout=30) as conn:
        for rid in job['ids']:
            if state == 'ok':
                conn.execute("UPDATE requests SET restart_needed=0, restarted_at=? WHERE id=?", (time.time(), rid))
            else:
                conn.execute("UPDATE requests SET status='rolled_back', restart_needed=0, error=? WHERE id=? AND status='applied'",
                             (error, rid))
                conn.execute("UPDATE requests SET restart_needed=0 WHERE id=? AND status='undone'", (rid,))


say('running')
time.sleep(TIMING['first_pause'])
restart()
if stable():
    mark('ok')
    say('ok')
    telegram('ops-restart:' + str(int(time.time())), '🛠 서버를 재시작했고 정상으로 돌아왔어요. 적용한 변경이 반영됐습니다.')
    sys.exit(0)

# The new code did not come up: put the previous files back (only those that were just applied) and restart again.
restored = []
for rel, backup, _ in job['restore']:
    target = (BASE / rel).resolve()
    if rel in ALLOWED and BASE in target.parents and Path(backup).is_file():
        shutil.copy2(backup, target)
        restored.append(rel)
restart()
recovered = stable()
note = '재시작 뒤 앱이 뜨지 않아 방금 적용한 변경을 되돌렸어요: ' + ', '.join(restored)
mark('rolled_back', note)
say('rolled_back' if recovered else 'rollback_failed', restored=restored)
telegram('ops-restart:' + str(int(time.time())),
         ('🛠 ' + note + '\n이전 코드로 정상 복구됐어요.') if recovered else
         ('🛠 ' + note + '\n⚠ 되돌린 뒤에도 앱이 정상으로 돌아오지 않았어요. 직접 확인이 필요해요: systemctl --user status ' + job['service']))
