"""Offline test of ops_assistant with a scripted fake model. The app directory is a temp copy of the real files:
no GPU, no Ollama, no production file, no real systemctl.
usage (cwd = the server dir): python test_ops.py <scail_repair.py> <ops_assistant.py>"""
import importlib.util
import json
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, '.')
SERVER = Path('/home/ben/zit-22-2716')
from fastapi import FastAPI
from fastapi.testclient import TestClient


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rep = load('scail_repair', sys.argv[1])
ops = load('ops_assistant', sys.argv[2])
ok = 0


def check(name, cond, extra=''):
    global ok
    print(('PASS ' if cond else 'FAIL ') + name + (f'  :: {extra}' if not cond and extra != '' else ''))
    if not cond:
        raise SystemExit(1)
    ok += 1


def blocks(*pairs, note='설명: 테스트 수정이에요.'):
    out = [note]
    for search, replace in pairs:
        out.append(f'<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE')
    return '\n'.join(out)


class Impl:
    class GenerationCancelled(Exception):
        pass

    def __init__(self):
        self._JOB_QUEUE_LOCK = threading.Lock()
        self._JOB_QUEUE, self._JOB_HISTORY = [], {}
        self._ACTIVE_QUEUE_JOB_ID = None
        self.progress = []

    def _check_cancelled(self): pass
    def _progress_start(self, *a): self.progress.append(a)
    def _progress_update(self, **k): self.progress.append(k)
    def _progress_done(self, *a): self.progress.append(a)


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    app = tmp / 'app'
    for rel in ops.TARGETS:
        (app / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SERVER / rel, app / rel)
    (app / 'secret.env').write_text('TOKEN=abc')
    (app / 'telegram_data').mkdir()
    import sqlite3
    tg = sqlite3.connect(app / 'telegram_data/bridge.sqlite3')
    tg.executescript("create table config(key text primary key, value text);"
                     "create table outbox(id text primary key, created real, method text, payload text, attachment text, state text default 'pending');"
                     "insert into config values('owner','1');")
    tg.commit()
    ops.BASE, ops.DATA = app, app / 'ops_assistant_data'
    ops.DB_PATH, ops.BACKUPS, ops.GUARD = ops.DATA / 'assistant.sqlite3', ops.DATA / 'backups', app / 'ops_restart_guard.py'
    impl = Impl()
    ops.impl = impl

    def outbox():
        return [json.loads(r[0])['text'] for r in sqlite3.connect(app / 'telegram_data/bridge.sqlite3').execute('select payload from outbox order by created')]

    JS = 'studio_static/scail-repair.js'
    PY = 'scail_repair.py'
    HTML = 'studio_static/scail-repair.html'

    # ------------------------------------------------ the allow-list
    check('only the 8 listed files are editable', len(ops.TARGETS) == 8)
    for bad in ('studio_app.py', 'durable_video_queue.py', 'secret.env', '../etc/passwd', 'studio_static/../studio_app.py',
                'youtube_data/config.json', 'ops_assistant.py', 'request_security.py', 'instagram_api.py'):
        try:
            ops.target_path(bad)
            check(f'refuses {bad}', False)
        except ValueError:
            check(f'refuses {bad}', True)
    check('suggest: youtube', ops.suggest_target('유튜브 토큰 오류') == 'youtube_api.py')
    check('suggest: live page', ops.suggest_target('중단 버튼 색을 바꿔줘') == 'studio_static/scail-live.js')
    check('suggest: server error in repair', ops.suggest_target('구간 수정 서버 오류가 나') == 'scail_repair.py')
    check('suggest: look and feel -> html', ops.suggest_target('구간 수정 화면 글자 크기') == HTML)

    # ------------------------------------------------ secrets never reach the prompt
    secret = 'sk-' + 'a1B2c3D4' * 8
    cleaned = ops.redact(f'Authorization: Bearer {secret}\nurl=https://api.telegram.org/bot123456:ABC-def_ghi/send\n'
                         f'client_secret="{secret}" password=hunter2 token: xyz123 plain text ok {secret}')
    check('bearer / bot token / secrets / long keys are masked',
          secret not in cleaned and 'hunter2' not in cleaned and 'ABC-def' not in cleaned and 'xyz123' not in cleaned and 'plain text ok' in cleaned, cleaned)

    class Run:
        def __init__(self, out): self.stdout = out

    fake_log = '\n'.join(['INFO:     1.2.3.4:0 - "GET /api/x HTTP/1.1" 200 OK', 'Traceback (most recent call last):',
                          f'  File "{app}/scail_repair.py", line 120, in run', '    boom()', 'ValueError: nope',
                          'INFO:     1.2.3.4:0 - "POST /api/error HTTP/1.1" 500', f'token=zzz{secret}'])
    real_run = ops.subprocess.run
    ops.subprocess.run = lambda *a, **k: Run(fake_log)
    errs = ops.recent_errors()
    ops.subprocess.run = real_run
    check('log context keeps tracebacks, drops access lines, masks secrets',
          'Traceback' in errs and 'ValueError: nope' in errs and 'GET /api' not in errs and 'POST /api' not in errs and secret not in errs, errs)

    # ------------------------------------------------ excerpts fit the model's context
    big = (SERVER / PY).read_text()
    excerpt, mode = ops.pick_excerpt(big, '해상도 검증 메시지 sanitize_options 16의 배수')
    check('big file is excerpted, not sent whole', mode == 'excerpt' and len(excerpt) < ops.PROMPT_BUDGET + 4000, (mode, len(excerpt)))
    check('the excerpt contains the function that was asked about', 'def sanitize_options' in excerpt and '16의 배수' in excerpt)
    check('the excerpt keeps line numbers and the file head', re.search(r'^\s+1\| ', excerpt, re.M) and 'import' in excerpt.split('...')[0])
    other = f'  File "{app}/scail_repair.py", line 30, in x\n  File "/usr/lib/python3/other.py", line 500, in y'
    ex2, _ = ops.pick_excerpt(big, 'zzz', other, name='scail_repair.py')
    check('traceback lines force their own file region only', re.search(r'^\s+30\| ', ex2, re.M) and not re.search(r'^\s+500\| ', ex2, re.M))
    small, mode2 = ops.pick_excerpt('a\nb\nc', 'x')
    check('a small file is sent whole', mode2 == 'full' and small.count('\n') == 2)
    system, prompt, _ = ops.build_prompt('fix', '토스트 시간을 늘려줘', JS, (app / JS).read_text(), False)
    check('prompt names the file and carries the request', JS in prompt and '토스트 시간을 늘려줘' in prompt and 'SEARCH' in system)

    # ------------------------------------------------ parsing and exact matching
    explanation, parsed = ops.parse_reply(blocks(('a\nb', 'a\nB'), ('x', 'y')))
    check('reply parsed into explanation and blocks', explanation == '테스트 수정이에요.' and parsed == [('a\nb', 'a\nB'), ('x', 'y')], (explanation, parsed))
    check('no block -> explanation only', ops.parse_reply('설명: 못 해요.') == ('못 해요.', []))
    check('deleting lines (empty replacement) is parsed', ops.parse_reply('<<<<<<< SEARCH\nfoo\n=======\n>>>>>>> REPLACE')[1] == [('foo', '')])
    text = 'one\ntwo\nthree\ntwo\nfour\n'
    check('unique match applies', ops.apply_blocks(text, [('three', 'THREE')]) == ('one\ntwo\nTHREE\ntwo\nfour\n', []))
    check('ambiguous match is refused', '2군데' in ops.apply_blocks(text, [('two', 'X')])[1][0])
    check('missing text is refused', '찾지 못' in ops.apply_blocks(text, [('nothing here', 'X')])[1][0])
    check('empty search is refused', '비어' in ops.apply_blocks(text, [('  ', 'X')])[1][0])
    check('trailing-whitespace differences are tolerated', ops.apply_blocks('a  \nb\n', [('a\nb', 'A\nB')])[0] == 'A\nB\n')
    check('a copied line-number gutter is stripped', ops.apply_blocks(text, [('    3| three', '    3| THREE')])[0] == 'one\ntwo\nTHREE\ntwo\nfour\n')
    check('two blocks apply regardless of order', ops.apply_blocks(text, [('four', '4'), ('one', '1')])[0] == '1\ntwo\nthree\ntwo\n4\n')
    check('overlapping blocks are refused', '겹쳐' in ' '.join(ops.apply_blocks(text, [('one\ntwo', 'x'), ('two\nthree', 'y')])[1]))

    # ------------------------------------------------ checks before anything touches disk
    js = (app / JS).read_text()
    good_old = "t._t=setTimeout(()=>t.className='toast',2600);"
    check('the real file contains the line the tests patch', js.count(good_old) == 1)

    def propose(rel, search, replace, reply_note='설명: x'):
        original = (app / rel).read_text()
        return ops.build_proposal('rid', rel, original, blocks((search, replace), note=reply_note))

    p = propose(JS, good_old, good_old.replace('2600', '4000'))
    check('valid small JS patch is proposed (not applied)', p['status'] == 'proposed' and p['changed'] == 2 and p['tier'] == 'static', p.get('error'))
    check('JS syntax check ran and passed', any('자바스크립트 문법' in c[0] and c[1] for c in p['checks']), p['checks'])
    check('nothing on disk changed yet', good_old in (app / JS).read_text())
    p = propose(JS, good_old, "t._t=setTimeout(()=>t.className='toast',2600;")
    check('JS syntax error is blocked', p['status'] == 'invalid' and any(not c[1] for c in p['checks']), p)
    p = propose(JS, good_old, good_old + "\neval('1+1');")
    check('eval in a page script is blocked', p['status'] == 'invalid' and any('eval' in c[0] for c in p['checks'] if not c[1]), p['checks'])
    p = propose(JS, good_old, good_old + "\nfetch('https://evil.example/x');")
    check('external address in a page script is blocked', p['status'] == 'invalid')
    ht = (app / HTML).read_text()
    anchor = '<div class="toast" id="toast"></div>'
    check('html fixture anchor exists', ht.count(anchor) == 1)
    p = propose(HTML, anchor, anchor + '\n<script>alert(1)</script>')
    check('inline script in HTML is blocked (site CSP)', p['status'] == 'invalid' and any('인라인' in c[0] and not c[1] for c in p['checks']), p['checks'])
    p = propose(HTML, anchor, '<div class="toast" id="toast" onclick="x()"></div>')
    check('on...= handler in HTML is blocked', p['status'] == 'invalid' and any('on' in c[0] and not c[1] for c in p['checks']))
    p = propose(HTML, anchor, anchor + '\n<div>')
    check('unbalanced tags are blocked', p['status'] == 'invalid' and any('짝' in c[0] and not c[1] for c in p['checks']))
    p = propose(HTML, anchor, anchor + '\n<img src="https://evil.example/p.png">')
    check('new external resource in HTML is blocked', p['status'] == 'invalid')
    p = propose(HTML, anchor, anchor + '\n<p class="note">안내 문구</p>')
    check('a harmless HTML addition passes', p['status'] == 'proposed' and all(c[1] for c in p['checks']), p['checks'])

    py = (app / PY).read_text()
    py_old = "MAX_FRAMES = 81\n"
    check('python fixture anchor exists', py.count(py_old) == 1)
    p = propose(PY, py_old, "MAX_FRAMES = 81 +\n")
    check('python syntax error is blocked', p['status'] == 'invalid' and any('문법' in c[0] and not c[1] for c in p['checks']))
    p = propose(PY, py_old, py_old + "UNUSED_CONST = does_not_exist_anywhere\n")
    check('a new undefined name is blocked', p['status'] == 'invalid' and any('정의되지' in c[0] and not c[1] for c in p['checks']), p['checks'])
    p = propose(PY, py_old, py_old + "ANOTHER = MAX_FRAMES * 2\n")
    check('a clean python patch is proposed, tier python', p['status'] == 'proposed' and p['tier'] == 'python' and not p['flags'], p)
    p = propose(PY, py_old, py_old + "import os\nREMOVER = os.remove\n")
    check('risky calls are only flagged for the reviewer', p['status'] == 'proposed' and any('파일 삭제' in f for f in p['flags']), p['flags'])
    big_replace = '\n'.join(f'LINE_{i} = {i}' for i in range(ops.MAX_CHANGED_LINES + 5))
    p = propose(PY, py_old, big_replace)
    check('a patch that is too big is refused with advice', p['status'] == 'failed' and '너무 커요' in p['error'])
    original = (app / JS).read_text()
    p = ops.build_proposal('rid', JS, original, blocks(*[(f'x{i}', f'y{i}') for i in range(ops.MAX_BLOCKS + 1)]))
    check('too many blocks is refused', p['status'] == 'failed' and '곳까지만' in p['error'])
    p = ops.build_proposal('rid', JS, original, blocks(('this text is not in the file', 'z')))
    check('a block that does not match is a failure with the reason', p['status'] == 'failed' and '찾지 못' in p['error'])
    p = ops.build_proposal('rid', JS, original, '설명: 이 요청은 작은 수정으로 안전하게 할 수 없어요.')
    check('no blocks is a plain answer', p['status'] == 'answered' and '안전하게' in p['reply'])
    p = ops.build_proposal('rid', JS, original, blocks((good_old, good_old)))
    check('a no-op patch is reported as nothing to change', p['status'] == 'answered' and '이미 같아서' in p['reply'])

    # ------------------------------------------------ the queued job, end to end with a scripted model
    def new_request(kind, prompt_text, target, logs=0):
        rid = __import__('uuid').uuid4().hex
        with ops.db() as conn:
            conn.execute('INSERT INTO requests(id,created,kind,prompt,target,status,include_logs,tier) VALUES(?,?,?,?,?,?,?,?)',
                         (rid, time.time(), kind, prompt_text, target, 'queued', logs, ops.TARGETS[target][1]))
        return rid

    script, seen = [], []

    def fake_model(system, user, rid, num_predict=1600):
        seen.append(user)
        reply = script.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    ops.ask_gemma = fake_model
    ops.recent_errors = lambda limit=1800: ''

    rid = new_request('ask', '토스트는 몇 초 동안 보이나요?', JS)
    script[:] = ['토스트는 2.6초 동안 보여요.']
    ops.run(rid)
    r = ops.get_request(rid)
    check('a question is answered and nothing is changed', r['status'] == 'answered' and '2.6초' in r['reply'] and good_old in (app / JS).read_text())
    check('owner gets a Telegram note with the link', any('토스트는 2.6초' in t and f'ops-assistant?id={rid}' in t for t in outbox()))

    rid = new_request('fix', '토스트를 4초로 늘려줘', JS)
    script[:] = [blocks((good_old, good_old.replace('2600', '4000')))]
    ops.run(rid)
    r = ops.get_request(rid)
    check('a fix request ends as a PROPOSAL, never applied by itself', r['status'] == 'proposed' and good_old in (app / JS).read_text(), r['status'])
    check('the diff and checks are stored for the reviewer', '-' in r['diff'] and '+' in r['diff'] and r['checks'] and r['changed'] == 2)
    check('owner is told approval is needed', any('승인 필요' in t for t in outbox()))
    applied = ops.apply_request(rid)
    check('approval applies exactly the diff', applied['status'] == 'applied' and "4000" in (app / JS).read_text() and good_old not in (app / JS).read_text())
    check('a backup copy of the original exists', (ops.BACKUPS / rid / JS).read_text() == js)
    check('static change needs no restart', applied['restart_needed'] == 0)
    try:
        ops.apply_request(rid)
        check('cannot apply twice', False)
    except ValueError:
        check('cannot apply twice', True)
    undone = ops.undo_request(rid)
    check('undo restores the original file byte for byte', undone['status'] == 'undone' and (app / JS).read_text() == js)

    # file changed after the proposal -> stale, nothing written
    rid = new_request('fix', '토스트 5초', JS)
    script[:] = [blocks((good_old, good_old.replace('2600', '5111')))]
    ops.run(rid)
    (app / JS).write_text(js + '\n// someone else edited this\n')
    try:
        ops.apply_request(rid)
        check('stale proposal is refused', False)
    except ValueError as error:
        check('stale proposal is refused', '바뀌' in str(error))
    check('stale proposal changed nothing and is marked', ops.get_request(rid)['status'] == 'stale' and '5111' not in (app / JS).read_text())
    (app / JS).write_text(js)

    # undo refuses when the file moved on, unless forced
    rid = new_request('fix', '토스트 6초', JS)
    script[:] = [blocks((good_old, good_old.replace('2600', '6000')))]
    ops.run(rid)
    ops.apply_request(rid)
    (app / JS).write_text((app / JS).read_text() + '\n// later edit\n')
    try:
        ops.undo_request(rid)
        check('undo refuses after later edits', False)
    except PermissionError:
        check('undo refuses after later edits', True)
    ops.undo_request(rid, force=True)
    check('forced undo restores the backup', (app / JS).read_text() == js)

    # a mismatching first answer gets one correction round with the real text as a hint
    rid = new_request('fix', '토스트 7초', JS)
    seen.clear()
    script[:] = [blocks(("t._t=setTimeout(()=>t.className='toast', 2600);", 'x')),
                 blocks((good_old, good_old.replace('2600', '7000')))]
    ops.run(rid)
    r = ops.get_request(rid)
    check('mismatch is retried once and then proposed', r['status'] == 'proposed' and len(seen) == 2, (r['status'], len(seen)))
    check('the retry prompt contains the real lines as a hint', good_old in seen[1] and '정확히 일치하지 않았어요' in seen[1])

    # python tier: proposal only, restart needed after approval, undo before restart needs none
    rid = new_request('fix', '프레임 한도 상수 추가', PY)
    script[:] = [blocks((py_old, py_old + 'ANOTHER = MAX_FRAMES * 2\n'))]
    ops.run(rid)
    check('server code is proposed, never auto-applied', ops.get_request(rid)['status'] == 'proposed' and 'ANOTHER' not in (app / PY).read_text())
    py_rid = rid
    a = ops.apply_request(rid)
    check('approved server code is written and flagged for restart', 'ANOTHER' in (app / PY).read_text() and a['restart_needed'] == 1)
    check('it shows up as waiting for a restart', ops.pending_restart_ids() == [rid])

    # model / GPU failures end the request cleanly
    rid = new_request('fix', '실패 시나리오', JS)
    script[:] = [RuntimeError('다른 GPU 작업 종료 대기 초과 · 중복 모델 적재 방지')]
    try:
        ops.run(rid)
        check('failure propagates to the queue', False)
    except RuntimeError:
        check('failure propagates to the queue', True)
    r = ops.get_request(rid)
    check('failed request records the reason', r['status'] == 'failed' and 'GPU' in r['error'])
    check('owner is told about the failure', any('실패' in t for t in outbox()))
    rid = new_request('fix', '취소 시나리오', JS)
    script[:] = [Impl.GenerationCancelled('x')]
    try:
        ops.run(rid)
    except Impl.GenerationCancelled:
        pass
    check('cancelling is recorded as cancelled, not as an error', ops.get_request(rid)['error'] == '취소했어요')

    # the model call itself: waits for the GPU politely, but only for GPU problems
    fresh = load('ops_assistant_gpu', sys.argv[2])
    fresh.impl = impl
    attempts = []

    class Session:
        def __init__(self, fail): self.fail = fail
        def __enter__(self):
            attempts.append(1)
            if self.fail:
                raise RuntimeError('다른 GPU 작업 종료 대기 초과 · 중복 모델 적재 방지')
        def __exit__(self, *a): return False

    class Resp:
        def raise_for_status(self): pass
        def json(self): return {'message': {'content': '  답변이에요  '}}

    sequence = [True, True, False]
    fresh.gpu_session = lambda kind: Session(sequence.pop(0))
    fresh.httpx.post = lambda *a, **k: Resp()
    fresh.time.sleep = lambda s: None
    check('waits through GPU-busy errors and then answers', fresh.ask_gemma('s', 'u', 'rid') == '답변이에요' and len(attempts) == 3, len(attempts))
    fresh.gpu_session = lambda kind: (_ for _ in ()).throw(RuntimeError('Ollama 연결 실패'))
    try:
        fresh.ask_gemma('s', 'u', 'rid')
        check('a non-GPU error is not retried', False)
    except RuntimeError as error:
        check('a non-GPU error is not retried', '연결 실패' in str(error))

    # ------------------------------------------------ HTTP: who may call what
    class FakeQueue:
        def __init__(self): self.calls = []

        def enqueue(self, func, args, kwargs, kind=None, identity=None):
            self.calls.append((func, args, kind, identity))
            job = {'id': identity}
            with impl._JOB_QUEUE_LOCK:
                impl._JOB_QUEUE.append(job)
                impl._JOB_HISTORY[identity] = job
            return dict(ok=True, queued=True, job_id=identity, position=len(impl._JOB_QUEUE))

        def sync(self): pass

    queue = FakeQueue()
    api = FastAPI()
    ops.configure(api, impl, queue)
    client = TestClient(api)
    H = {'X-Ops-Assistant': '1'}
    with ops.db() as conn:
        conn.execute("UPDATE requests SET status='answered' WHERE status IN ('queued','running')")

    check('GET targets lists labels and tiers', len(client.get('/api/ops/targets').json()['items']) == 8)
    check('GET suggest', client.get('/api/ops/suggest', params={'prompt': '유튜브 오류'}).json()['target'] == 'youtube_api.py')
    r = client.post('/api/ops/requests', data={'prompt': '토스트 시간을 늘려줘', 'kind': 'fix'})
    check('POST without the custom header is refused (cross-site forms cannot send it)', r.status_code == 403, r.text)
    r = client.post('/api/ops/requests', data={'prompt': '토스트 시간을 늘려줘', 'kind': 'fix'}, headers={**H, 'Origin': 'https://evil.example', 'Host': 'yunalee.shop'})
    check('POST from another origin is refused', r.status_code == 403, r.text)
    r = client.post('/api/ops/requests', data={'prompt': '토스트 시간을 늘려줘', 'kind': 'fix', 'target': JS},
                    headers={**H, 'Origin': 'https://yunalee.shop', 'Host': 'yunalee.shop'})
    check('same-origin POST with the header is accepted', r.status_code == 200 and r.json()['target'] == JS, r.text)
    new_id = r.json()['id']
    check('it was queued as a durable job at the front', queue.calls[-1][3] == 'opsasst_' + new_id and impl._JOB_QUEUE[0]['id'] == 'opsasst_' + new_id)
    check('queued row is visible in the list', any(i['id'] == new_id and i['status'] == 'queued' for i in client.get('/api/ops/requests').json()['items']))
    check('too short prompt is 422', client.post('/api/ops/requests', data={'prompt': 'ab'}, headers=H).status_code == 422)
    check('unknown kind is 422', client.post('/api/ops/requests', data={'prompt': '충분히 긴 요청', 'kind': 'rm'}, headers=H).status_code == 422)
    check('a file outside the allow-list is 422', client.post('/api/ops/requests', data={'prompt': '충분히 긴 요청', 'target': 'studio_app.py'}, headers=H).status_code == 422)
    check('path tricks are 422', client.post('/api/ops/requests', data={'prompt': '충분히 긴 요청', 'target': '../../etc/passwd'}, headers=H).status_code == 422)
    for _ in range(2):
        client.post('/api/ops/requests', data={'prompt': '대기 중인 요청이에요', 'target': JS}, headers=H)
    check('more than 3 waiting requests is 429', client.post('/api/ops/requests', data={'prompt': '넷째 요청이에요', 'target': JS}, headers=H).status_code == 429)
    with ops.db() as conn:
        conn.execute("UPDATE requests SET status='answered' WHERE status IN ('queued','running')")

    check('GET one request', client.get(f'/api/ops/requests/{rid}').status_code == 200)
    check('GET with a bad id is 404', client.get('/api/ops/requests/..%2f..').status_code == 404)
    check('apply needs the header', client.post(f'/api/ops/requests/{py_rid}/reject').status_code == 403)
    rid = new_request('fix', '반려할 요청', JS)
    script[:] = [blocks((good_old, good_old.replace('2600', '3000')))]
    ops.run(rid)
    r = client.post(f'/api/ops/requests/{rid}/apply', headers=H)
    check('POST apply works with the header', r.status_code == 200 and r.json()['status'] == 'applied', r.text)
    check('POST apply a second time is 409', client.post(f'/api/ops/requests/{rid}/apply', headers=H).status_code == 409)
    r = client.post(f'/api/ops/requests/{rid}/undo', headers=H)
    check('POST undo works', r.status_code == 200 and r.json()['status'] == 'undone')
    rid = new_request('fix', '반려할 요청 2', JS)
    script[:] = [blocks((good_old, good_old.replace('2600', '3100')))]
    ops.run(rid)
    check('POST reject', client.post(f'/api/ops/requests/{rid}/reject', headers=H).json()['status'] == 'rejected')
    check('a rejected proposal can no longer be applied', client.post(f'/api/ops/requests/{rid}/apply', headers=H).status_code == 409)

    # ------------------------------------------------ restart under the watchdog
    launched = []
    ops.subprocess.run = lambda cmd, **k: launched.append((cmd, k.get('env', {}))) or Run('')
    impl._ACTIVE_QUEUE_JOB_ID = 'some-video-job'
    r = client.post('/api/ops/restart', headers=H)
    check('restart while a video is generating asks for confirmation first', r.status_code == 409 and '영상 생성' in r.json()['detail'], r.text)
    check('nothing was launched without confirmation', launched == [])
    r = client.post('/api/ops/restart', data={'confirm_busy': 'true'}, headers=H)
    check('confirmed restart is handed to the watchdog', r.status_code == 200 and r.json()['ids'] == [py_rid], r.text)
    cmd, env = launched[0]
    check('watchdog runs outside the service (systemd-run --user) with the guard script',
          cmd[0] == 'systemd-run' and '--user' in cmd and any('ops_restart_guard.py' in c for c in cmd) and env['XDG_RUNTIME_DIR'].startswith('/run/user/'), cmd)
    job = json.loads(next(ops.DATA.glob('restart-job-*.json')).read_text())
    check('the job lists exactly the applied server change with its backup',
          job['restore'] == [[PY, str(ops.BACKUPS / py_rid / PY), py_rid]] and job['ids'] == [py_rid] and job['health'].startswith('http://127.0.0.1'), job)
    check('status shows running', ops.restart_status()['state'] == 'running')
    r = client.post('/api/ops/restart', data={'confirm_busy': 'true'}, headers=H)
    check('a second restart while one is running is refused', r.status_code == 409 and '이미 재시작' in r.json()['detail'], r.text)
    (ops.DATA / 'restart.json').write_text(json.dumps({'state': 'ok', 'at': time.time()}))
    ops.set_state(py_rid, restart_needed=0, restarted_at=time.time())
    r = client.post('/api/ops/restart', headers=H)
    check('nothing to restart -> 409', r.status_code == 409 and '변경이 없어요' in r.json()['detail'], r.text)
    # undoing a change that is already loaded needs another restart; the watchdog then has nothing to roll back
    u = client.post(f'/api/ops/requests/{py_rid}/undo', headers=H)
    check('undo of a loaded server change asks for a restart', u.status_code == 200 and u.json()['restart_needed'] == 1 and ops.pending_restart_ids() == [py_rid])
    impl._ACTIVE_QUEUE_JOB_ID = None
    launched.clear()
    for f in ops.DATA.glob('restart-job-*.json'):
        f.unlink()
    r = client.post('/api/ops/restart', headers=H)
    job = json.loads(next(ops.DATA.glob('restart-job-*.json')).read_text())
    check('such a restart has no rollback list', r.status_code == 200 and job['restore'] == [] and job['ids'] == [py_rid], job)
    check('the module never writes outside its own folder and the allow-list',
          sorted(p.name for p in app.iterdir()) == sorted(['studio_static', 'scail_repair.py', 'scail_live.py', 'youtube_api.py', 'secret.env', 'telegram_data', 'ops_assistant_data'])
          and (app / 'secret.env').read_text() == 'TOKEN=abc')

print(f'\nALL {ok} CHECKS PASSED')
