"""See the chunks of a SCAIL dance while it is still being generated, then stop it or let it continue.

Every finished chunk already leaves a display-sized part-NNN.mp4 next to the job (scail_generation.publish_chunk).
This module lists those parts for running / stopped jobs, builds a "1..k joined" preview on demand, and lets a
stopped job continue: finished chunks are reattached from the durable queue, only the rest is generated.
"""
import re
import subprocess
import threading
import time

from fastapi import Form, HTTPException
from fastapi.responses import FileResponse

import scail_generation as gen
import scail_repair as rep
import scail_trial as trial

ID = re.compile(r'scail_[a-f0-9]{32}')
PART = re.compile(r'part-(\d{3})\.mp4')
_locks = {}
_locks_guard = threading.Lock()


def _out_dir(identity):
    return trial.COMFY / 'output/scail_user' / identity


def _lock_for(path):
    with _locks_guard:
        return _locks.setdefault(str(path), threading.Lock())


def parts_of(identity):
    """Finished chunk previews, in order. Gaps (a missing middle part) end the usable run."""
    found = {}
    for path in _out_dir(identity).glob('part-*.mp4'):
        match = PART.fullmatch(path.name)
        if match and path.stat().st_size > 0:
            found[int(match[1])] = path
    run = []
    while len(run) + 1 in found:
        run.append(found[len(run) + 1])
    return run


def plan_of(identity):
    """Per-chunk seconds of the job, from the same plan the generator uses."""
    settings = gen.settings_for(gen.ROOT / identity)
    fps = settings['fps']
    return settings, [chunk['keep'] / fps for chunk in gen.plan(settings['frames'], fps, settings.get('chunk_frames'))]


def build_preview(identity, k):
    """Join parts 1..k into one cached mp4. Returns its path."""
    parts = parts_of(identity)
    if not 1 <= k <= len(parts):
        raise ValueError('아직 만들어지지 않은 구간이에요.')
    used = parts[:k]
    out = _out_dir(identity) / f'preview-upto-{k:03d}.mp4'
    newest = max(p.stat().st_mtime for p in used)
    with _lock_for(out):
        if out.exists() and out.stat().st_mtime >= newest:
            return out
        listing = _out_dir(identity) / f'preview-upto-{k:03d}.txt'
        listing.write_text(''.join(f"file '{p}'\n" for p in used))
        tmp = out.with_name(out.stem + '.tmp.mp4')
        try:
            subprocess.run(['nice', '-n', '10', 'ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'concat', '-safe', '0',
                            '-i', str(listing), '-an', '-c:v', 'libx264', '-crf', '20', '-preset', 'veryfast',
                            '-threads', '2', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(tmp)],
                           check=True, capture_output=True, timeout=240)
            tmp.replace(out)
        finally:
            tmp.unlink(missing_ok=True)
            listing.unlink(missing_ok=True)
    return out


def list_live(runtime, queue):
    with queue.store.db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT id,created,status,kind,attempts,error FROM jobs WHERE func='scail' ORDER BY created DESC LIMIT 80")]
    with runtime._JOB_QUEUE_LOCK:
        live = {r['id']: dict(runtime._JOB_HISTORY.get(r['id'], {})) for r in rows}
        order = {j['id']: i + 1 for i, j in enumerate(runtime._JOB_QUEUE)}
        active = getattr(runtime, '_ACTIVE_QUEUE_JOB_ID', None)
    items = []
    for row in rows:
        identity = row['id']
        if not ID.fullmatch(identity):
            continue
        state = live[identity].get('status') or row['status']
        state = 'queued' if state == 'waiting' else state
        if state == 'done':
            continue
        parts = parts_of(identity)
        if state != 'running' and not parts:
            continue                                # waiting jobs that have shown nothing yet are just noise here
        try:
            settings, seconds = plan_of(identity)
        except Exception:
            continue
        progress = {}
        if identity == active and hasattr(runtime, '_progress_snapshot'):
            snap = runtime._progress_snapshot() or {}
            progress = dict(message=snap.get('message'), step=snap.get('step'), total_steps=snap.get('total_steps'))
        cursor, shown = 0.0, []
        for index, path in enumerate(parts):
            length = seconds[index] if index < len(seconds) else 0.0
            shown.append(dict(index=index + 1, url=rep._web(path), start=round(cursor, 3), seconds=round(length, 3)))
            cursor += length
        items.append(dict(id=identity, title=(settings.get('title') or identity)[:90], state=state, active=(identity == active),
                          position=order.get(identity), total=len(seconds), done=len(parts), parts=shown,
                          seconds_done=round(cursor, 3), seconds_total=round(sum(seconds), 3),
                          can_resume=state in ('cancelled', 'error'), error=(row['error'] or '')[:160] if state == 'error' else '',
                          thumb=f'/api/scail/repair/thumb/{identity}', created=row['created'], progress=progress))
    items.sort(key=lambda i: (not i['active'], i['state'] != 'running', -i['created']))
    return items


def configure(app, runtime, queue):
    @app.get('/api/scail/live')
    def live():
        return dict(items=list_live(runtime, queue), updated_at=time.time())

    @app.get('/api/scail/live/{identity}/preview/{k}.mp4')
    def preview(identity: str, k: int):
        if not ID.fullmatch(identity):
            raise HTTPException(404, '작업을 찾을 수 없습니다.')
        try:
            path = build_preview(identity, k)
        except ValueError as error:
            raise HTTPException(404, str(error))
        except (subprocess.SubprocessError, OSError):
            gen.LOG.exception('SCAIL live preview failed: %s %s', identity, k)
            raise HTTPException(500, '미리보기를 만들지 못했어요.')
        return FileResponse(path, media_type='video/mp4', headers={'Cache-Control': 'no-store'})

    @app.post('/api/scail/live/{identity}/resume')
    def resume(identity: str, where: str = Form('front')):
        if not ID.fullmatch(identity):
            raise HTTPException(404, '작업을 찾을 수 없습니다.')
        with queue.store.db() as db:
            row = db.execute('SELECT func,status FROM jobs WHERE id=?', (identity,)).fetchone()
        if not row or row['func'] != 'scail':
            raise HTTPException(404, '작업을 찾을 수 없습니다.')
        if row['status'] not in rep.TERMINAL or row['status'] == 'done':
            raise HTTPException(409, '이미 대기·실행 중이거나 끝난 작업이에요.')
        with runtime._JOB_QUEUE_LOCK:
            if identity == getattr(runtime, '_ACTIVE_QUEUE_JOB_ID', None):
                raise HTTPException(409, '아직 멈추는 중이에요. 잠시 뒤에 다시 눌러 주세요.')
        if not rep._requeue(queue, runtime, identity, None, front=(where != 'back')):
            raise HTTPException(409, '다시 대기열에 넣지 못했어요.')
        with runtime._JOB_QUEUE_LOCK:
            position = next((i + 1 for i, j in enumerate(runtime._JOB_QUEUE) if j.get('id') == identity), None)
        return dict(ok=True, position=position)
