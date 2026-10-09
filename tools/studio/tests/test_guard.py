"""Runs ops_restart_guard.py for real (as a subprocess) against a fake service, fake systemctl and a fake health endpoint.
Nothing outside the temp directory is touched.   usage: python test_guard.py <ops_restart_guard.py>"""
import http.server
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

GUARD = Path(sys.argv[1]).resolve()
ok = 0


def check(name, cond, extra=''):
    global ok
    print(('PASS ' if cond else 'FAIL ') + name + (f'  :: {extra}' if not cond and extra != '' else ''))
    if not cond:
        raise SystemExit(1)
    ok += 1


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    base = tmp / 'app'
    (base / 'telegram_data').mkdir(parents=True)
    tg = sqlite3.connect(base / 'telegram_data/bridge.sqlite3')
    tg.executescript("create table config(key text primary key, value text);"
                     "create table outbox(id text primary key, created real, method text, payload text, attachment text, state text default 'pending');"
                     "insert into config values('owner','1');")
    tg.commit()
    bin_dir = tmp / 'bin'
    bin_dir.mkdir()
    log = tmp / 'systemctl.log'
    fake = bin_dir / 'systemctl'
    fake.write_text(f'#!/bin/sh\necho "$@" >> {log}\nexit 0\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    mode = {'healthy': True, 'needs_old_file': False}
    app_file = base / 'scail_repair.py'

    class Health(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            good = mode['healthy'] and (not mode['needs_old_file'] or 'OLD' in app_file.read_text())
            self.send_response(200 if good else 500)
            self.end_headers()
            self.wfile.write(b'ok')

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    health = f'http://127.0.0.1:{server.server_address[1]}/'

    def scenario(name, healthy, needs_old):
        mode.update(healthy=healthy, needs_old_file=needs_old)
        log.write_text('')
        app_file.write_text('NEW code')
        backup = tmp / f'backup-{name}.py'
        backup.write_text('OLD code')
        db = tmp / f'db-{name}.sqlite3'
        conn = sqlite3.connect(db)
        conn.execute("create table requests(id text primary key, status text, restart_needed integer, restarted_at real, error text)")
        conn.executemany('insert into requests values(?,?,?,?,?)', [('a1', 'applied', 1, None, ''), ('u1', 'undone', 1, None, '')])
        conn.commit()
        conn.close()
        status = tmp / f'status-{name}.json'
        job = dict(base=str(base), db=str(db), service='fake.service', health=health, ids=['a1', 'u1'], status_path=str(status),
                   restore=[['scail_repair.py', str(backup), 'a1']],
                   timing=dict(first_pause=0, startup_pause=0, healthy_wait=4, settle=1, poll=1))
        path = tmp / f'job-{name}.json'
        path.write_text(json.dumps(job))
        env = dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}')
        subprocess.run([sys.executable, str(GUARD), str(path)], env=env, check=True, timeout=90)
        rows = {r[0]: r[1:] for r in sqlite3.connect(db).execute('select id,status,restart_needed,restarted_at,error from requests')}
        sent = [json.loads(r[0])['text'] for r in sqlite3.connect(base / 'telegram_data/bridge.sqlite3').execute('select payload from outbox')]
        return json.loads(status.read_text()), rows, app_file.read_text(), log.read_text().count('restart'), sent

    state, rows, content, restarts, sent = scenario('fine', healthy=True, needs_old=False)
    check('healthy restart is reported ok', state['state'] == 'ok', state)
    check('new code is kept', content == 'NEW code')
    check('restarted exactly once', restarts == 1, restarts)
    check('applied change marked as loaded', rows['a1'][1] == 0 and rows['a1'][2] is not None and rows['a1'][0] == 'applied', rows)
    check('undone change only has its restart flag cleared', rows['u1'][1] == 0 and rows['u1'][0] == 'undone', rows)
    check('owner is told', any('정상으로 돌아왔어요' in t for t in sent), sent)

    state, rows, content, restarts, sent = scenario('recovers', healthy=True, needs_old=True)
    check('new code that never comes up is rolled back', state['state'] == 'rolled_back' and state['restored'] == ['scail_repair.py'], state)
    check('previous file is back', content == 'OLD code')
    check('restarted twice (new code, then old code)', restarts == 2, restarts)
    check('the applied request is marked rolled back with a reason', rows['a1'][0] == 'rolled_back' and '되돌렸어요' in rows['a1'][3], rows)
    check('the undone request is not rolled back', rows['u1'][0] == 'undone' and rows['u1'][1] == 0, rows)
    check('owner is told it recovered', any('정상 복구' in t for t in sent[-1:]), sent)

    state, rows, content, restarts, sent = scenario('broken', healthy=False, needs_old=False)
    check('still down after rollback is reported loudly', state['state'] == 'rollback_failed', state)
    check('previous file was restored anyway', content == 'OLD code')
    check('owner is warned to check by hand', any('직접 확인이 필요' in t for t in sent[-1:]), sent)

    # a restore entry pointing outside the app directory is ignored
    mode.update(healthy=False, needs_old_file=False)
    outside = tmp / 'outside.txt'
    outside.write_text('keep me')
    job = dict(base=str(base), db=str(tmp / 'db-fine.sqlite3'), service='fake.service', health=health, ids=['a1'],
               status_path=str(tmp / 'status-x.json'),
               restore=[['../outside.txt', str(tmp / 'backup-fine.py'), 'a1']],
               timing=dict(first_pause=0, startup_pause=0, healthy_wait=2, settle=0, poll=1))
    (tmp / 'job-x.json').write_text(json.dumps(job))
    subprocess.run([sys.executable, str(GUARD), str(tmp / 'job-x.json')], env=dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}'),
                   check=True, timeout=60)
    check('a path outside the app directory is never overwritten', outside.read_text() == 'keep me')
    server.shutdown()

print(f'\nALL {ok} CHECKS PASSED')
