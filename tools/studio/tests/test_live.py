"""Offline test of scail_live + the generalised _requeue against the REAL durable Queue class and real ffmpeg.
Temp directories only: no GPU, no ComfyUI, no production database, no Telegram.
usage: python test_live.py <new scail_repair.py> <scail_live.py>"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, '.')
from fastapi import FastAPI
from fastapi.testclient import TestClient

import durable_video_queue as dvq


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rep = load('scail_repair', sys.argv[1])
live = load('scail_live', sys.argv[2])
gen = rep.gen
ok = 0


def check(name, cond, extra=''):
    global ok
    print(('PASS ' if cond else 'FAIL ') + name + (f'  :: {extra}' if not cond and extra != '' else ''))
    if not cond:
        raise SystemExit(1)
    ok += 1


class Impl:
    def __init__(self):
        self._JOB_QUEUE_LOCK = threading.Lock()
        self._JOB_HISTORY, self._JOB_QUEUE = {}, []
        self._ACTIVE_QUEUE_JOB_ID = None
        self._CANCEL_EVENT = threading.Event()
        self._enqueue_work = self._comfy_post = self._wait_for_comfy_video = self._save_video_copy = None
        self.snapshot = {}

    def _ensure_queue_worker_locked(self):
        pass

    def _progress_snapshot(self):
        return self.snapshot


def read(sql, *args):
    with queue.store.db() as db:
        return [dict(r) for r in db.execute(sql, args)]


def jid(c):
    return 'scail_' + c * 32


def tiny(path, frames=6):
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'testsrc=duration={frames / 24}:size=64x112:rate=24',
                    '-pix_fmt', 'yuv420p', '-c:v', 'libx264', str(path)], check=True)


def nframes(path):
    return int(subprocess.check_output(['ffprobe', '-v', 'error', '-count_frames', '-select_streams', 'v:0', '-show_entries',
                                        'stream=nb_read_frames', '-of', 'csv=p=0', str(path)]).decode().strip())


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    old_root, old_comfy = gen.ROOT, rep.trial.COMFY
    gen.ROOT, rep.trial.COMFY = tmp / 'data', tmp / 'comfy'
    try:
        impl = Impl()
        queue = dvq.Queue(impl, tmp / 'q', {'scail': lambda *a, **k: None})
        now = time.time()

        def add_job(identity, status, attempts=0, created=None, parts=0, broken=False, func='scail'):
            (gen.ROOT / identity).mkdir(parents=True, exist_ok=True)
            if not broken:
                (gen.ROOT / identity / 'request.json').write_text(json.dumps(dict(
                    title=f'job {identity[6:9]}', steps=8, fps=24, frames=360, start=0.0, seconds=15.0, choreography='c',
                    prompt='p', chunk_frames=81)))
            out = rep.trial.COMFY / 'output/scail_user' / identity
            out.mkdir(parents=True, exist_ok=True)
            for n in range(1, parts + 1):
                tiny(out / f'part-{n:03d}.mp4')
            with queue.store.db() as db:
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (identity, created or now, func, json.dumps([[], {}]), 'k', '{}', status, '{}',
                            'boom' if status == 'error' else '', attempts))

        A, B, C, D, E, F, R = jid('a'), jid('b'), jid('c'), jid('d'), jid('e'), jid('f'), jid('9')
        add_job(A, 'running', 1, now - 5, parts=2)
        add_job(B, 'cancelled', 2, now - 10, parts=1)
        add_job(C, 'queued', 0, now - 20, parts=0)
        add_job(D, 'done', 1, now - 30, parts=5)
        add_job(E, 'error', 1, now - 40, parts=1, broken=True)
        add_job(F, 'error', 1, now - 50, parts=3)
        add_job(R, 'queued', 0, now - 60, parts=0, func='scail')
        impl._ACTIVE_QUEUE_JOB_ID = A
        impl._JOB_HISTORY[A] = {'id': A, 'status': 'running'}
        impl.snapshot = dict(message='SCAIL-2 · 3/5구간 영상 생성', step=4, total_steps=8)
        cp = gen.ROOT / A / 'chunk-progress.json'
        cp.write_text(json.dumps(dict(current=3, total=5, completed=2)))

        # ---- listing ----
        items = live.list_live(impl, queue)
        ids = [i['id'] for i in items]
        check('running job first, finished/empty/broken ones hidden', ids[0] == A and D not in ids and C not in ids and E not in ids, str(ids))
        check('stopped jobs with parts are listed', B in ids and F in ids)
        a = items[0]
        check('5 chunks planned like the generator', a['total'] == 5 and a['done'] == 2, str((a['total'], a['done'])))
        check('parts carry urls and cumulative start times',
              [p['url'] for p in a['parts']] == [f'/comfy-output/scail_user/{A}/part-001.mp4', f'/comfy-output/scail_user/{A}/part-002.mp4']
              and a['parts'][0]['start'] == 0 and abs(a['parts'][1]['start'] - 81 / 24) < .001, str(a['parts']))
        check('seconds summary', abs(a['seconds_done'] - (81 + 76) / 24) < .01 and abs(a['seconds_total'] - 360 / 24) < .5, str((a['seconds_done'], a['seconds_total'])))
        check('active job shows live progress', a['active'] and a['progress']['step'] == 4 and a['state'] == 'running')
        check('only stopped jobs can be resumed',
              next(i for i in items if i['id'] == B)['can_resume'] and not a['can_resume'])
        check('error text kept short', next(i for i in items if i['id'] == F)['error'] == 'boom')

        # a gap in the numbering ends the usable run
        gap = rep.trial.COMFY / 'output/scail_user' / F
        (gap / 'part-002.mp4').unlink()
        check('gap in parts ends the run', len(live.parts_of(F)) == 1)
        tiny(gap / 'part-002.mp4')
        check('restored middle part reconnects the run', len(live.parts_of(F)) == 3)
        (gap / 'part-004.mp4').write_bytes(b'')
        check('an empty (unfinished) file does not count', len(live.parts_of(F)) == 3)
        tiny(gap / 'part-005.mp4')
        check('a part after a gap is ignored', len(live.parts_of(F)) == 3)

        # ---- joined preview ----
        p2 = live.build_preview(A, 2)
        check('preview of 2 parts joins their frames', nframes(p2) == 12, nframes(p2))
        stamp = p2.stat().st_mtime
        time.sleep(0.05)
        check('second request is served from cache', live.build_preview(A, 2).stat().st_mtime == stamp)
        newest = rep.trial.COMFY / 'output/scail_user' / A / 'part-002.mp4'
        time.sleep(1.1)
        tiny(newest, frames=8)
        check('changed part rebuilds the preview', nframes(live.build_preview(A, 2)) == 14)
        check('preview of 1 part', nframes(live.build_preview(A, 1)) == 6)
        for bad in (0, 3):
            try:
                live.build_preview(A, bad)
                check(f'k={bad} rejected', False)
            except ValueError:
                check(f'k={bad} rejected', True)
        check('no leftovers next to the parts',
              not list((rep.trial.COMFY / 'output/scail_user' / A).glob('*.tmp.mp4')) and not list((rep.trial.COMFY / 'output/scail_user' / A).glob('*.txt')))

        # ---- requeue on the real Queue ----
        impl._JOB_QUEUE.extend([{'id': C}, {'id': 'x1'}])
        check('requeue stopped job at the front', rep._requeue(queue, impl, B, None, True))
        check('front position', impl._JOB_QUEUE[0]['id'] == B and impl._JOB_HISTORY[B]['status'] == 'queued')
        with queue.store.db() as db:
            row = dict(db.execute('SELECT status,attempts,error FROM jobs WHERE id=?', (B,)).fetchone())
            order = {r[0]: r[1] for r in db.execute('SELECT job,position FROM queue_order')}
        check('db: queued, error cleared, one attempt given back', row == dict(status='queued', attempts=1, error=''), str(row))
        check('queue order persisted by sync', order.get(B) == 0 and order.get(C) == 1, str(order))
        check('job object is runnable and keeps its id', impl._JOB_HISTORY[B]['id'] == B and callable(impl._JOB_HISTORY[B]['func']))
        check('requeue of a queued job is refused', not rep._requeue(queue, impl, B, None, True))
        check('requeue of the active job is refused', not rep._requeue(queue, impl, A, None, True))
        check('requeue of an unknown id is refused', not rep._requeue(queue, impl, jid('1'), None, True))
        check('attempts floor at zero', rep._requeue(queue, impl, F, None, False) and read('SELECT attempts FROM jobs WHERE id=?', F)[0]['attempts'] == 0)
        check('back position appends', impl._JOB_QUEUE[-1]['id'] == F)
        impl._JOB_QUEUE.insert(2, {'id': 'repair1'})
        with queue.store.db() as db:
            db.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (R,))
        rep._requeue(queue, impl, R, 'repair1')
        pos = [j['id'] for j in impl._JOB_QUEUE]
        check('after-repair position', pos.index(R) == pos.index('repair1') + 1, str(pos))

        # ---- endpoints ----
        app = FastAPI()
        live.configure(app, impl, queue)
        client = TestClient(app)
        r = client.get('/api/scail/live')
        check('GET /api/scail/live', r.status_code == 200 and A in [i['id'] for i in r.json()['items']])
        r = client.get(f'/api/scail/live/{A}/preview/2.mp4')
        check('GET preview serves a video', r.status_code == 200 and r.headers['content-type'] == 'video/mp4' and len(r.content) > 500)
        check('preview beyond finished parts is 404', client.get(f'/api/scail/live/{A}/preview/3.mp4').status_code == 404)
        check('preview with a bad id is 404', client.get('/api/scail/live/..%2f..%2fetc/preview/1.mp4').status_code == 404)
        with queue.store.db() as db:
            db.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (F,))
        impl._JOB_QUEUE[:] = [j for j in impl._JOB_QUEUE if j['id'] != F]
        r = client.post(f'/api/scail/live/{F}/resume', data={'where': 'back'})
        check('POST resume (back)', r.status_code == 200 and r.json()['position'] == len(impl._JOB_QUEUE), r.text)
        r = client.post(f'/api/scail/live/{F}/resume')
        check('resume of a queued job is 409', r.status_code == 409, r.text)
        r = client.post(f'/api/scail/live/{A}/resume')
        check('resume of the running job is 409', r.status_code == 409, r.text)
        with queue.store.db() as db:
            db.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (A,))
        r = client.post(f'/api/scail/live/{A}/resume')
        check('resume while it is still stopping is 409 with a hint', r.status_code == 409 and '멈추는 중' in r.json()['detail'], r.text)
        check('resume of a finished job is 409', client.post(f'/api/scail/live/{D}/resume').status_code == 409)
        check('resume with a bad id is 404', client.post('/api/scail/live/nope/resume').status_code == 404)
        check('resume of an unknown id is 404', client.post(f'/api/scail/live/{jid("2")}/resume').status_code == 404)
    finally:
        gen.ROOT, rep.trial.COMFY = old_root, old_comfy

print(f'\nALL {ok} CHECKS PASSED')
