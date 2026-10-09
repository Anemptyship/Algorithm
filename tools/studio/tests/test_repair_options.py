"""Offline checks for scail_repair (no GPU, no ComfyUI submission, no Telegram)."""
import contextlib
import importlib.util
import json
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, '.')
spec = importlib.util.spec_from_file_location('scail_repair_under_test', sys.argv[1])
rep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rep)
gen = rep.gen

ok = 0


def check(name, cond, extra=''):
    global ok
    print(('PASS ' if cond else 'FAIL ') + name + (f'  {extra}' if extra and not cond else ''))
    if not cond:
        raise SystemExit(1)
    ok += 1


def raises(fn, text):
    try:
        fn()
    except ValueError as e:
        return text in str(e)
    return False


# 1. option validation
check('empty options', rep.sanitize_options('') == {} and rep.sanitize_options(None) == {})
good = rep.sanitize_options(json.dumps(dict(steps='10', dpo=True, dpo_strength='1.2', lora_strength=.9, shift=3, cfg=2,
                                            pose_strength=1.1, tail='4', width=544, height=960, color_match=False,
                                            prompt=' hello ')))
check('good options parsed', good == dict(steps=10, dpo=True, dpo_strength=1.2, lora_strength=.9, shift=3.0, cfg=2.0,
                                          pose_strength=1.1, tail=4, width=544, height=960, color_match=False,
                                          prompt='hello'), str(good))
check('native size is not an override', 'width' not in rep.sanitize_options(dict(width=736, height=1280)))
check('bad steps', raises(lambda: rep.sanitize_options(dict(steps=7)), '스텝'))
check('bad res multiple', raises(lambda: rep.sanitize_options(dict(width=500, height=960)), '16의 배수'))
check('res too big', raises(lambda: rep.sanitize_options(dict(width=1024, height=1536)), '범위'))
check('res needs both', raises(lambda: rep.sanitize_options(dict(width=544)), '숫자'))
check('cfg range', raises(lambda: rep.sanitize_options(dict(cfg=9)), 'CFG'))
check('nan rejected', raises(lambda: rep.sanitize_options(dict(shift='nan')), 'shift'))
check('json garbage', raises(lambda: rep.sanitize_options('{nope'), '형식'))

# 2. frame_range with tail override
a = rep.frame_range(3.0, 3.5, 360)
b = rep.frame_range(3.0, 3.5, 360, 0)
c = rep.frame_range(3.0, 3.5, 360, 12)
check('tail default/0/12', (a['tail'], b['tail'], c['tail']) == (6, 0, 12), str((a, b, c)))
check('length is 4n+1 and <= 81', all((x['length'] - 1) % 4 == 0 and x['length'] <= 81 for x in (a, b, c)))

# 3. real job settings -> graph with overrides
jobs = sorted(p for p in gen.ROOT.iterdir() if re.fullmatch(r'scail_[a-f0-9]{32}', p.name) and (p / 'request.json').exists())
identity = jobs[-1].name
base = gen.settings_for(gen.ROOT / identity)
chunk = dict(offset=24, overlap=5, keep=30, length=37)
masks0, graph0 = gen.graphs(identity, base, chunk, 901)
opts = dict(steps=10, dpo=True, dpo_strength=1.3, lora_strength=.5, shift=3.0, cfg=2.0, pose_strength=1.2, width=544, height=960)
settings = rep.apply_settings(base, opts)
masks, graph = gen.graphs(identity, settings, chunk, 901)
rep.patch_graph(graph, opts)
check('steps applied', graph['sample']['inputs']['steps'] == 10)
check('dpo node present', 'dpo' in graph and graph['dpo']['inputs']['strength_model'] == 1.3)
check('lora strength', graph['lora']['inputs']['strength_model'] == .5)
check('shift', graph['sampling']['inputs']['shift'] == 3.0)
check('cfg', graph['sample']['inputs']['cfg'] == 2.0)
check('pose strength', graph['conditioning']['inputs']['pose_strength'] == 1.2)
check('resolution', (graph['conditioning']['inputs']['width'], graph['conditioning']['inputs']['height']) == (544, 960))
check('masks graph untouched by options', json.dumps(masks, sort_keys=True) == json.dumps(masks0, sort_keys=True))
check('no options -> graph identical',
      json.dumps(rep.patch_graph(json.loads(json.dumps(graph0)), {}), sort_keys=True) == json.dumps(graph0, sort_keys=True))
print('     job used:', identity, '| base steps', base['steps'], '| dpo', base.get('dpo_lora'))

# 4. resolution helpers
f = np.random.randint(0, 255, (3, 960, 544, 3), dtype=np.uint8)
check('to_delivered 544x960 -> 720x1280', rep.to_delivered(f).shape == (3, 1280, 720, 3), str(rep.to_delivered(f).shape))
f = np.random.randint(0, 255, (2, 640, 352, 3), dtype=np.uint8)
check('to_delivered 352x640', rep.to_delivered(f).shape == (2, 1280, 720, 3))
f = np.random.randint(0, 255, (2, 1280, 720, 3), dtype=np.uint8)
check('fit_frames up/down', rep.fit_frames(f, 416, 736).shape == (2, 736, 416, 3))

# 5. version numbering ignores the segment files
with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    check('first version is 1', rep._next_version(d) == 1)
    for n in ('fix-1.mp4', 'fix-1-segment.mp4', 'fix-2.mp4', 'fix-2-segment.mp4', 'full.mp4'):
        (d / n).write_bytes(b'x')
    check('next version 3 (segments not counted)', rep._next_version(d) == 3)

# 6. work-tab items from a fake queue/runtime
class FakeStore:
    def __init__(self, conn):
        self.conn = conn

    @contextlib.contextmanager
    def db(self):
        yield self.conn

    def decode(self, payload, db, lazy=False):
        return payload[0], {}


conn = sqlite3.connect(':memory:')
conn.row_factory = sqlite3.Row
conn.execute('CREATE TABLE jobs(id TEXT,created REAL,func TEXT,payload TEXT,kind TEXT,meta TEXT,status TEXT,result TEXT,error TEXT,attempts INTEGER)')
rid1, rid2 = 'a' * 32, 'b' * 32
conn.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,0)', ('scailfix_' + rid1, 2.0, 'scail_repair', json.dumps([[identity, rid1], {}]),
             'SCAIL-2 구간 수정 · 1.00~1.50초 · 손', '{}', 'done',
             json.dumps(dict(videos=[dict(name='x.mp4', url='/comfy-output/scail_user/x/fix-1.mp4', label='구간 수정본 1')], finished_at=9.0)), ''))
conn.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,0)', ('scailfix_' + rid2, 3.0, 'scail_repair', 'garbage-not-json',
             'orphan', '{}', 'queued', '', ''))
conn.execute("INSERT INTO jobs VALUES('scail_other',1.0,'scail','[]','k','{}','done','{}','',0)")


class FakeRuntime:
    import threading
    _JOB_QUEUE_LOCK = threading.Lock()
    _JOB_HISTORY = {'scailfix_' + rid2: {'status': 'waiting'}}
    _JOB_QUEUE = [{'id': 'scailfix_' + rid2}]
    _ACTIVE_QUEUE_JOB_ID = None

    @staticmethod
    def _progress_snapshot():
        return {}


class FakeQueue:
    store = FakeStore(conn)

items = rep.workspace_items(FakeRuntime, FakeQueue)
check('only repair jobs listed', {i['id'] for i in items} == {'scailfix_' + rid1, 'scailfix_' + rid2})
done = next(i for i in items if i['id'] == 'scailfix_' + rid1)
orphan = next(i for i in items if i['id'] == 'scailfix_' + rid2)
need = {'id', 'title', 'state', 'width', 'height', 'frames', 'fps', 'videos', 'position', 'started_at', 'finished_at',
        'expected_seconds', 'progress', 'stage_message', 'reference'}
ref_items = gen.workspace_items.__code__.co_names  # sanity: the sibling function exists
check('item has every field the SCAIL cards have', need <= set(done), str(need - set(done)))
check('done item', done['state'] == 'done' and len(done['videos']) == 1 and done['finished_at'] == 9.0)
check('reference points at the parent job', done['reference'] == f'/api/scail/jobs/{identity}/reference', str(done['reference']))
check('waiting is shown as queued, with position', orphan['state'] == 'queued' and orphan['position'] == 1)
check('undecodable payload does not break the list', orphan['reference'] is None)
check('reference regex used by results tab matches',
      re.fullmatch(r'/api/(wan|scail)/jobs/\1_[a-f0-9]{32}/reference', done['reference']) is not None)


# 7. saved versions + export mode (all fakes: no GPU, no ffmpeg grading, no Telegram)
import shutil, subprocess, threading
with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    old_root, old_comfy = gen.ROOT, rep.trial.COMFY
    gen.ROOT, rep.trial.COMFY = d / 'data', d / 'comfy'
    try:
        ident = 'scail_' + 'c' * 32
        (gen.ROOT / ident).mkdir(parents=True)
        (gen.ROOT / ident / 'request.json').write_text(json.dumps(dict(title='t', steps=8, fps=24, frames=24, start=0.0, seconds=1.0, choreography='c', prompt='p')))
        out_dir = rep.trial.COMFY / 'output/scail_user' / ident
        out_dir.mkdir(parents=True)
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc=duration=1:size=64x64:rate=24',
                        '-pix_fmt', 'yuv420p', str(out_dir / 'fix-1.mp4')], check=True)
        shutil.copy2(out_dir / 'fix-1.mp4', out_dir / 'fix-2.mp4')
        shutil.copy2(out_dir / 'fix-1.mp4', out_dir / 'fix-2-segment.mp4')
        (out_dir / 'graded-abc-v2.mp4').write_bytes(b'x')
        (out_dir / 'fix-1.finishing-pending.json').write_text('{}')
        (out_dir / 'fix-1.mp4.publishing-pending.json').write_text('{}')
        check('probe_frames reads a real file', rep.probe_frames(out_dir / 'fix-1.mp4') == 24)
        (gen.ROOT / ident / 'current.json').write_text(json.dumps(dict(video=str(out_dir / 'fix-2.mp4'), version=2)))
        r1, r2, r3 = 'a' * 32, 'b' * 32, 'd' * 32
        for rid, res, note in ((r1, dict(video=str(out_dir / 'fix-1.mp4'), first=239, last=241, addition='x', seed=1), '손'),
                               (r2, dict(video=str(out_dir / 'fix-2.mp4'), segment=str(out_dir / 'fix-2-segment.mp4'),
                                         delivered=str(out_dir / 'graded-abc-v2.mp4'), first=224, last=268, seed=2), '검지')):
            f = gen.ROOT / ident / 'repairs' / rid
            f.mkdir(parents=True)
            (f / 'result.json').write_text(json.dumps(res))
            (f / 'request.json').write_text(json.dumps(dict(start=1, end=2, note=note)))
        f3 = gen.ROOT / ident / 'repairs' / r3
        f3.mkdir(parents=True)
        (f3 / 'request.json').write_text(json.dumps(dict(start=0, end=1, deliver=1, note='export')))

        class RT:
            _JOB_QUEUE_LOCK = threading.Lock()
            _JOB_HISTORY = {'scailfix_' + r3: {'status': 'running'}}

        vs = rep.list_versions(ident, RT)
        check('versions newest first', [v['version'] for v in vs] == [2, 1], str(vs))
        v2, v1 = vs
        check('current marker', v2['current'] and not v1['current'])
        check('delivered url only where a graded file exists', v2['delivered'] and not v1['delivered'], str(vs))
        check('segment url present for fix-2 only', v2['segment'] and not v1['segment'])
        check('span and note recovered', (v1['first'], v1['last'], v1['note']) == (239, 241, '손'))
        check('running export is flagged', v1['exporting'] and not v2['exporting'])
        check('unknown version has no span', rep.span_of_version(ident, 9) is None)

        calls = []

        class Impl:
            @staticmethod
            def _progress_start(*a): pass
            @staticmethod
            def _progress_done(*a): pass

        rep.impl = Impl
        rep._deliverable = lambda raw, s, offset, seconds, ref: (
            out_dir / ('graded-' + Path(raw).stem + '.mp4'), dict(audio_included=True, offset=offset, seconds=seconds))
        rep._tell = lambda key, text, path: calls.append((key, Path(path).name))
        for n in ('graded-fix-1.mp4', 'graded-fix-2-segment.mp4', 'graded-fix-2.mp4'):
            (out_dir / n).write_bytes(b'x')
        rid4 = 'e' * 32
        f4 = gen.ROOT / ident / 'repairs' / rid4
        f4.mkdir(parents=True)
        (f4 / 'request.json').write_text(json.dumps(dict(start=0, end=1, deliver=2, note='n')))
        res = rep.run(ident, rid4)      # goes through run() -> export branch
        check('export returns both videos', [v['label'].split(' ')[0] for v in res['videos']] == ['구간', '수정'], str(res))
        check('export result urls are web paths', all(v['url'].startswith('/comfy-output/scail_user/') for v in res['videos']))
        check('telegram called for full and segment', [c[1] for c in calls] == ['graded-fix-2.mp4', 'graded-fix-2-segment.mp4'], str(calls))
        saved = json.loads((f4 / 'result.json').read_text())
        check('result.json records delivered files', saved['deliver'] == 2 and saved['delivered'].endswith('graded-fix-2.mp4'))
        check('version list now shows fix-2 delivered by the export', any(
            v['version'] == 2 and v['delivered'] for v in rep.list_versions(ident, RT)))
        calls.clear()
        f5 = gen.ROOT / ident / 'repairs' / ('f' * 32)
        f5.mkdir(parents=True)
        (f5 / 'request.json').write_text(json.dumps(dict(start=0, end=1, deliver=1, note='n')))
        rep.run(ident, 'f' * 32)
        check('export of fix-1 (no segment) sends one video', [c[1] for c in calls] == ['graded-fix-1.mp4'], str(calls))
        check('stale gate markers of the raw file are cleared',
              not (out_dir / 'fix-1.finishing-pending.json').exists() and not (out_dir / 'fix-1.mp4.publishing-pending.json').exists())
        f6 = gen.ROOT / ident / 'repairs' / ('9' * 32)
        f6.mkdir(parents=True)
        (f6 / 'request.json').write_text(json.dumps(dict(start=0, end=1, deliver=7, note='n')))
        try:
            rep.run(ident, '9' * 32)
            check('missing version raises', False)
        except RuntimeError as e:
            check('missing version raises a clear error', '7' in str(e))
    finally:
        gen.ROOT, rep.trial.COMFY = old_root, old_comfy

print(f'\nALL {ok} CHECKS PASSED')
