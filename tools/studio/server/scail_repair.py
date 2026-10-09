"""Regenerate a short time range of a finished SCAIL dance (e.g. a bad hand) and splice it back.

Gemma reads frames of the bad range plus the owner's note and adds one corrective sentence to the
original prompt. Only that range is regenerated, conditioned on the 5 frames before it, then blended
back into the finished video over a short tail so there is no seam.
"""
import base64
import hashlib
import io
import json
import math
import random
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import httpx
import numpy as np
from fastapi import File, Form, HTTPException, UploadFile
from PIL import Image, ImageOps
from fastapi.responses import FileResponse

import scail_generation as gen
import scail_trial as trial
import wan_uploads
from media_runtime import gpu_session

FPS = 24
CONTEXT = 5          # frames before the range used as continuation (SCAIL-2 trained overlap)
TAIL = 6             # frames after the range cross-faded from new to old
MAX_FRAMES = 81
NATIVE_W, NATIVE_H = 736, 1280      # SCAIL-2 sampling size; the delivered video is its 720x1280 centre crop
GEMMA = 'gemma4:26b-a4b-it-qat'
FALLBACK = ('Both hands keep a natural anatomical shape throughout: five distinct fingers per hand, '
            'clean knuckles and wrists, no fused, missing or extra fingers.')
TERMINAL = {'done', 'error', 'cancelled'}     # same meaning as durable_video_queue.TERMINAL
impl = None


def root(identity):
    return gen.ROOT / identity


def current_video(identity):
    state = root(identity) / 'current.json'
    if state.exists():
        return Path(json.loads(state.read_text())['video'])
    return trial.COMFY / 'output/scail_user' / identity / 'full.mp4'


def frame_range(start, end, total, tail_max=None):
    """Inclusive frame indices covering [start, end) seconds, validated against the model window."""
    first = max(0, math.floor(start * FPS))
    last = min(total - 1, math.ceil(end * FPS) - 1)
    if last < first:
        raise ValueError('구간 끝이 시작보다 빨라요.')
    context = min(CONTEXT, first)
    tail = min(TAIL if tail_max is None else tail_max, total - 1 - last)
    raw = context + (last - first + 1) + tail
    length = math.ceil((raw - 1) / 4) * 4 + 1
    if length > MAX_FRAMES:
        limit = (MAX_FRAMES - CONTEXT - TAIL) / FPS
        raise ValueError(f'한 번에 고칠 수 있는 구간은 최대 {limit:.1f}초예요.')
    return dict(first=first, last=last, context=context, tail=tail, length=length)


def decode(path, width=720, height=1280):
    raw = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                         capture_output=True, check=True, timeout=300).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, height, width, 3).copy()


def encode(frames, path, fps=FPS):
    h, w = frames.shape[1:3]
    process = subprocess.Popen(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
        '-s', f'{w}x{h}', '-r', str(fps), '-i', '-', '-an', '-c:v', 'libx264', '-crf', '10', '-preset', 'fast',
        '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(path)], stdin=subprocess.PIPE)
    process.stdin.write(frames.tobytes()); process.stdin.close()
    if process.wait():
        raise RuntimeError('repair encode failed')


def jpeg(frame):
    out = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', '720x1280',
                          '-i', '-', '-vf', 'scale=360:640', '-f', 'mjpeg', '-'], input=frame.tobytes(),
                         capture_output=True, check=True, timeout=60).stdout
    return base64.b64encode(out).decode()


OPTION_LABELS = dict(steps='스텝', dpo_strength='DPO 강도', lora_strength='가속 LoRA 강도', shift='shift', cfg='CFG',
                     pose_strength='포즈 강도', tail='이음새 프레임', width='가로', height='세로', prompt='프롬프트')


def sanitize_options(raw):
    """Validate the optional per-repair overrides. Only keys the user set are returned."""
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise ValueError('고급 옵션 형식이 올바르지 않아요.')
    if not isinstance(raw, dict):
        raise ValueError('고급 옵션 형식이 올바르지 않아요.')
    out = {}

    def number(key, low, high, cast=float):
        value = raw.get(key)
        if value in (None, ''):
            return
        try:
            value = cast(value)
        except (TypeError, ValueError):
            raise ValueError(f'{OPTION_LABELS[key]} 값을 확인해 주세요.')
        if not math.isfinite(value) or value < low or value > high:
            raise ValueError(f'{OPTION_LABELS[key]}은(는) {low}~{high} 사이로 입력해 주세요.')
        out[key] = value

    if raw.get('steps') not in (None, ''):
        if int(raw['steps']) not in (6, 8, 10):
            raise ValueError('스텝은 6, 8, 10 중에서 골라 주세요.')
        out['steps'] = int(raw['steps'])
    for key in ('dpo', 'color_match', 'ref_to_scail'):
        if raw.get(key) is not None and raw.get(key) != '':
            out[key] = bool(raw[key]) if isinstance(raw[key], bool) else str(raw[key]).lower() in ('1', 'true', 'on')
    number('dpo_strength', 0, 1.5)
    number('lora_strength', 0, 1.5)
    number('shift', 1, 12)
    number('cfg', 1, 4)
    number('pose_strength', 0, 1.5)
    number('tail', 0, 12, int)
    width, height = raw.get('width'), raw.get('height')
    if width not in (None, '') or height not in (None, ''):
        try:
            width, height = int(width), int(height)
        except (TypeError, ValueError):
            raise ValueError('해상도는 가로·세로를 모두 숫자로 입력해 주세요.')
        if width % 16 or height % 16:
            raise ValueError('해상도는 16의 배수여야 해요. (예: 544×960)')
        if not (192 <= width <= 1024 and 256 <= height <= 1536) or width * height > NATIVE_W * NATIVE_H:
            raise ValueError('해상도 범위를 벗어났어요. (가로 192~1024, 세로 256~1536, 기본 736×1280 이하)')
        if (width, height) != (NATIVE_W, NATIVE_H):
            out.update(width=width, height=height)
    prompt = (raw.get('prompt') or '').strip()
    if prompt:
        if len(prompt) > 2000:
            raise ValueError('프롬프트는 2,000자 이하로 입력해 주세요.')
        out['prompt'] = prompt
    return out


def apply_settings(settings, opts):
    """Overrides that must be known before the graph is built."""
    out = dict(settings)
    if 'steps' in opts:
        out['steps'] = opts['steps']
    if 'dpo' in opts:
        out['dpo_lora'] = opts['dpo']
    if 'prompt' in opts:
        out['prompt'] = opts['prompt']
    return out


def patch_graph(graph, opts):
    """Overrides applied to the finished graph. Missing nodes (a speed profile may rename them) are skipped."""
    def inputs(key):
        node = graph.get(key)
        return node['inputs'] if node else None
    if 'lora_strength' in opts and inputs('lora') is not None:
        inputs('lora')['strength_model'] = opts['lora_strength']
    if 'dpo_strength' in opts and inputs('dpo') is not None:
        inputs('dpo')['strength_model'] = opts['dpo_strength']
    if 'shift' in opts and inputs('sampling') is not None:
        inputs('sampling')['shift'] = opts['shift']
    if 'cfg' in opts and inputs('sample') is not None:
        inputs('sample')['cfg'] = opts['cfg']
    if 'pose_strength' in opts and inputs('conditioning') is not None:
        inputs('conditioning')['pose_strength'] = opts['pose_strength']
    if 'width' in opts and inputs('conditioning') is not None:
        inputs('conditioning').update(width=opts['width'], height=opts['height'])
    return graph


def probe_size(path):
    out = subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                                   'stream=width,height', '-of', 'csv=p=0', str(path)], timeout=60).decode().strip()
    width, height = (int(v) for v in out.split(',')[:2])
    return width, height


def fit_frames(frames, width, height):
    """Resize a frame stack to width x height."""
    return np.stack([np.asarray(Image.fromarray(f).resize((width, height), Image.LANCZOS)) for f in frames])


def to_delivered(frames):
    """Centre-crop to 9:16, then resize to the delivered 720x1280 (used for non-native sampling sizes)."""
    h, w = frames.shape[1:3]
    want_w = round(h * 720 / 1280)
    if want_w <= w:
        x = (w - want_w) // 2
        frames = frames[:, :, x:x + want_w]
    else:
        want_h = round(w * 1280 / 720)
        y = (h - want_h) // 2
        frames = frames[:, y:y + want_h]
    return fit_frames(frames, 720, 1280)


def save_reference(upload, folder):
    """Validate and normalise an optional reference image; returns its short hash."""
    raw = upload.file.read(8 * 1024**2 + 1)
    if len(raw) > 8 * 1024**2:
        raise ValueError('참고 이미지는 8MB 이하로 올려 주세요.')
    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert('RGB')
    except Exception:
        raise ValueError('참고 이미지를 읽을 수 없어요. JPG/PNG/WebP를 올려 주세요.')
    image.thumbnail((1280, 1280))
    image.save(folder / 'ref.jpg', quality=92)
    return hashlib.sha256(raw).hexdigest()[:16]


def ref_b64(path):
    if not path.exists():
        return None
    image = Image.open(path).convert('RGB')
    image.thumbnail((480, 480))
    buffer = io.BytesIO()
    image.save(buffer, 'JPEG', quality=85)
    return base64.b64encode(buffer.getvalue()).decode()


def gemma_addition(frames, base_prompt, note, reference=None):
    """One English sentence fixing the complaint; never new actions. Falls back to a fixed hand rule."""
    system = ('You repair one short range of an AI dance video. Write ONE English sentence (max 40 words) that will be '
              'appended to the existing prompt to fix the owner\'s complaint, usually hands or fingers. Describe only the '
              'correct appearance (e.g. five distinct fingers, relaxed natural hand, clear knuckles). Do not add or change '
              'the dance, pose, camera, clothing or background. Return JSON {"addition": "..."}.')
    user = (f'Owner complaint (Korean): {note}\nExisting prompt: {base_prompt[:1500]}\n'
            'The attached images are frames from the bad range.')
    images = [jpeg(f) for f in frames]
    if reference:
        user += (' The LAST attached image is a REFERENCE chosen by the owner that shows the desired look '
                 '(for example the correct hand shape or pose). Describe that desired appearance in the sentence.')
        images.append(reference)
    try:
        with gpu_session('llm'):
            r = httpx.post('http://127.0.0.1:11434/api/chat', timeout=600, json=dict(
                model=GEMMA, stream=False, think=False, keep_alive=0, format='json',
                options=dict(temperature=0, num_ctx=8192, num_predict=200, num_gpu=22),
                messages=[dict(role='system', content=system),
                          dict(role='user', content=user, images=images)]))
        r.raise_for_status()
        text = json.loads(r.json()['message']['content']).get('addition', '').strip()
        if not text or len(text.split()) > 45 or re.search(r'[가-힣]', text):
            raise ValueError('invalid addition')
        return text, 'gemma'
    except Exception as error:
        gen.LOG.warning('Gemma repair prompt fallback: %s', error)
        return FALLBACK, 'fallback'


def color_match(new, reference):
    """Match the regenerated range to the old range's LAB mean/std (computed on small copies, applied per frame)."""
    from skimage.color import lab2rgb, rgb2lab
    small = lambda frames: np.stack([f[::8, ::8] for f in frames]) / 255.
    a, b = rgb2lab(small(new)).reshape(-1, 3), rgb2lab(small(reference)).reshape(-1, 3)
    scale, ma, mb = b.std(0) / (a.std(0) + 1e-6), a.mean(0), b.mean(0)
    out = np.empty_like(new)
    for i, frame in enumerate(new):
        lab = (rgb2lab(frame / 255.) - ma) * scale + mb
        out[i] = (np.clip(lab2rgb(lab), 0, 1) * 255 + .5).astype(np.uint8)
    return out


def _deliverable(raw, settings, offset, seconds, reference):
    """Silent frames -> add the matching slice of the song -> finishing gate, exactly like a normal SCAIL job.
    Without the audio step the gate refuses the file ("Missing or effectively silent audio")."""
    import choreography_music
    import video_finishing
    with_music, info = choreography_music.attach(raw, settings['choreography'], settings['start'] + offset, seconds,
                                                 root=wan_uploads.ROOT)
    return video_finishing.finish(with_music, reference), info


def _web(path):
    return '/comfy-output/' + str(Path(path).relative_to(trial.COMFY / 'output'))


def probe_frames(path):
    out = subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                                   'stream=nb_frames', '-of', 'csv=p=0', str(path)], timeout=60).decode().strip()
    return int(out)


def _results(identity):
    """Every finished repair record of a job, oldest first: (request id, result.json dict)."""
    found = []
    for path in sorted((root(identity) / 'repairs').glob('*/result.json'), key=lambda p: p.stat().st_mtime):
        try:
            found.append((path.parent.name, json.loads(path.read_text())))
        except (OSError, ValueError):
            continue
    return found


def span_of_version(identity, version):
    """Frame range, note and settings of the repair that produced fix-<version>.mp4 (None if unknown)."""
    for rid, result in reversed(_results(identity)):
        if str(result.get('video', '')).endswith(f'/fix-{version}.mp4') and result.get('first') is not None:
            note = ''
            try:
                note = json.loads((root(identity) / 'repairs' / rid / 'request.json').read_text()).get('note', '')
            except (OSError, ValueError):
                pass
            return dict(first=result['first'], last=result['last'], note=note, addition=result.get('addition', ''),
                        seed=result.get('seed'), options=result.get('options') or {})
    return None


def list_versions(identity, runtime=None):
    """Saved repair versions of a job with their raw / delivered files and any export in progress."""
    out_dir = trial.COMFY / 'output/scail_user' / identity
    current = None
    state = root(identity) / 'current.json'
    if state.exists():
        try:
            current = json.loads(state.read_text()).get('version')
        except (OSError, ValueError):
            pass
    delivered = {}
    for _, result in _results(identity):
        match = re.search(r'/fix-(\d+)\.mp4$', str(result.get('video', '')))
        if match and result.get('delivered') and Path(result['delivered']).exists():
            delivered[int(match[1])] = result
    busy = set()
    if runtime is not None:
        with runtime._JOB_QUEUE_LOCK:
            live = {k: v.get('status') for k, v in runtime._JOB_HISTORY.items() if k.startswith('scailfix_')}
        for path in (root(identity) / 'repairs').glob('*/request.json'):
            try:
                request = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if request.get('deliver') and live.get('scailfix_' + path.parent.name) in ('queued', 'running', 'waiting'):
                busy.add(int(request['deliver']))
    items = []
    for path in sorted(out_dir.glob('fix-*.mp4')):
        match = re.fullmatch(r'fix-(\d+)\.mp4', path.name)
        if not match:
            continue
        version = int(match[1])
        seg = out_dir / f'fix-{version}-segment.mp4'
        info = span_of_version(identity, version) or {}
        done = delivered.get(version)
        items.append(dict(version=version, created=path.stat().st_mtime, current=(version == current),
            video=_web(path), segment=_web(seg) if seg.exists() else None,
            delivered=_web(done['delivered']) if done else None,
            segment_delivered=_web(done['segment_delivered']) if done and done.get('segment_delivered')
            and Path(done['segment_delivered']).exists() else None,
            first=info.get('first'), last=info.get('last'), note=info.get('note', ''),
            exporting=version in busy))
    return sorted(items, key=lambda i: i['version'], reverse=True)


def deliver_existing(identity, rid, request):
    """Give an already saved fix-<N>.mp4 (and its segment) the song, the finishing gate, the web list and Telegram."""
    version = int(request['deliver'])
    out_dir = trial.COMFY / 'output/scail_user' / identity
    out, segment = out_dir / f'fix-{version}.mp4', out_dir / f'fix-{version}-segment.mp4'
    if not out.exists():
        raise RuntimeError(f'수정본 {version}을(를) 찾을 수 없어요.')
    folder = root(identity) / 'repairs' / rid
    settings = gen.settings_for(root(identity))
    info = span_of_version(identity, version)
    impl._progress_start('SCAIL-2 수정본 내보내기', 1, '음악 붙이기 · 후보정 중')
    reference_png = root(identity) / 'reference.png'
    shown, music = _deliverable(out, settings, 0.0, probe_frames(out) / FPS, reference_png)
    shown_segment = None
    if segment.exists() and info:
        shown_segment, _ = _deliverable(segment, settings, info['first'] / FPS, probe_frames(segment) / FPS, reference_png)
    # Markers left by the earlier, audio-less attempts describe the raw files, which now have a proper deliverable.
    for stale in (out.with_suffix('.finishing-pending.json'), Path(str(out) + '.publishing-pending.json'),
                  segment.with_suffix('.finishing-pending.json'), Path(str(segment) + '.publishing-pending.json')):
        stale.unlink(missing_ok=True)
    result = dict(video=str(out), segment=str(segment) if segment.exists() else None, delivered=str(shown),
                  segment_delivered=str(shown_segment) if shown_segment else None, deliver=version,
                  first=(info or {}).get('first'), last=(info or {}).get('last'), finished_at=time.time())
    trial.save(folder / 'result.json', result)
    span_text = f'{info["first"] / FPS:.2f}~{(info["last"] + 1) / FPS:.2f}초' if info else ''
    _tell(f'{identity}:fix:{version}:export:{rid[:8]}',
        f'SCAIL-2 구간 수정본 {version} 내보내기\n{span_text}\n'
        f'{"음악 포함" if music.get("audio_included") else "음악 기준 확인 필요"}\nID: {identity}', shown)
    if shown_segment:
        _tell(f'{identity}:fix:{version}:export-segment:{rid[:8]}',
              f'수정 구간만 · {span_text} (수정본 {version})', shown_segment)
    impl._progress_done('수정본 내보내기 완료')
    poster = f'/api/scail/jobs/{identity}/reference'
    videos = [dict(name=shown.name, url=_web(shown), poster=poster, label=f'구간 수정본 {version}')]
    if shown_segment:
        videos.append(dict(name=shown_segment.name, url=_web(shown_segment), poster=poster,
                           label=f'수정 구간만 {version} · {span_text}'))
    return dict(music=music, videos=videos, finished_at=time.time())


def _tell(key, text, path):
    """Telegram delivery must never fail the repair itself."""
    try:
        trial.notify(key, text, path)
    except Exception:
        gen.LOG.exception('SCAIL repair Telegram delivery failed: %s', key)


def _next_version(out_dir):
    numbers = [int(m[1]) for p in out_dir.glob('fix-*.mp4') if (m := re.fullmatch(r'fix-(\d+)\.mp4', p.name))]
    return max(numbers, default=0) + 1


def run(identity, rid):
    folder = root(identity) / 'repairs' / rid
    request = json.loads((folder / 'request.json').read_text())
    if request.get('deliver'):
        return deliver_existing(identity, rid, request)
    opts = request.get('options') or {}
    settings = apply_settings(gen.settings_for(root(identity)), opts)
    source = current_video(identity)
    video = decode(source)
    span = frame_range(request['start'], request['end'], len(video), opts.get('tail'))
    first, last, context, tail, length = (span[k] for k in ('first', 'last', 'context', 'tail', 'length'))
    begin = first - context
    width, height = opts.get('width', NATIVE_W), opts.get('height', NATIVE_H)
    custom = (width, height) != (NATIVE_W, NATIVE_H)
    reference_path = folder / 'ref.jpg'
    manual = (request.get('addition') or '').strip()
    if manual:
        impl._progress_start('SCAIL-2 구간 수정', settings['steps'], '선택 구간 다시 생성')
        addition, how = manual[:400], ('reuse' if request.get('reuse') else 'manual')
    else:
        impl._progress_start('SCAIL-2 구간 수정', settings['steps'], 'Gemma 프롬프트 보완')
        picks = np.linspace(first, last, min(4, last - first + 1)).round().astype(int)
        addition, how = gemma_addition([video[i] for i in picks], settings['prompt'], request['note'],
                                       ref_b64(reference_path))
    trial.save(folder / 'prompt.json', dict(addition=addition, source=how))
    settings = dict(settings, prompt=settings['prompt'] + ' ' + addition, fps=FPS)

    index = 900 + int(rid[-4:], 16) % 99          # unique input/output slot, never a real chunk index
    target = gen.INPUT / identity
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root(identity) / 'reference.png', target / 'reference.png')
    entry = wan_uploads.entry(settings['choreography'])
    driver = wan_uploads.ROOT / settings['choreography'] / ('source' + entry['extension'])
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-ss', str(settings['start'] + begin / FPS), '-i', str(driver),
        '-t', str(length / FPS), '-vf', 'fps=24,scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,setsar=1,'
        'tpad=stop_mode=clone:stop_duration=1', '-frames:v', str(length), '-an', '-c:v', 'libx264', '-crf', '10',
        str(target / f'driver-{index}.mp4')], check=True, capture_output=True, timeout=180)
    if context:
        if custom:
            prev = fit_frames(video[begin:first], width, height)
        else:
            # Continuation frames are native 736px wide; the finished video is the 720px centre crop.
            prev = np.pad(video[begin:first], ((0, 0), (0, 0), (8, 8), (0, 0)), mode='edge')
        encode(prev, target / f'previous-{index}.mp4')
    chunk = dict(offset=begin, overlap=context, keep=length - context, length=length)
    masks, graph = gen.graphs(identity, settings, chunk, index)
    if opts.get('ref_to_scail') and reference_path.exists():
        # Experimental: let SCAIL itself see the owner's reference instead of the job's original photo.
        swapped = target / f'reference-{index}.png'
        Image.open(reference_path).convert('RGB').save(swapped)
        masks['photo']['inputs']['image'] = f'scail_user/{identity}/{swapped.name}'
        graph['photo']['inputs']['image'] = f'scail_user/{identity}/{swapped.name}'
    if not context:   # range starts at frame 0: nothing to continue from
        graph.pop('previous', None)
        graph['conditioning']['inputs'].pop('previous_frames', None)
    patch_graph(graph, opts)
    seed = request.get('seed')
    seed = int(seed) % (2**31) if isinstance(seed, int) and seed >= 0 else random.randrange(2**31)
    graph['sample']['inputs']['seed'] = seed
    trial.save(folder / 'graph.json', graph)
    mask_path = gen.submit_graph(masks, identity, 'SCAIL-2 인물 추적')
    trial.validate_masks(mask_path, expected_frames=length + 1)
    shutil.copy2(mask_path, target / f'masks-{index}.mkv')
    impl._progress_update(step=0, total_steps=settings['steps'], message='SCAIL-2 · 선택 구간 다시 생성')
    output = gen.submit_graph(graph, identity, 'SCAIL-2 구간 수정')
    if custom:
        got_w, got_h = probe_size(output)
        new = to_delivered(decode(output, width=got_w, height=got_h))
    else:
        new = decode(output, width=NATIVE_W)[:, :, 8:728]
    keep = last - first + 1 + tail
    new = new[:keep]
    if opts.get('color_match', True):
        new = color_match(new, video[first:first + keep])
    fixed = video.copy()
    fixed[first:last + 1] = new[:last - first + 1]
    impl._check_cancelled()
    for t in range(tail):
        alpha = (tail - t) / (tail + 1)
        k = last + 1 + t
        fixed[k] = (new[last - first + 1 + t].astype(np.float32) * alpha + video[k] * (1 - alpha)).round().astype(np.uint8)
    out_dir = trial.COMFY / 'output/scail_user' / identity
    version = _next_version(out_dir)
    out = out_dir / f'fix-{version}.mp4'
    segment = out_dir / f'fix-{version}-segment.mp4'
    encode(fixed, out)
    encode(new[:last - first + 1], segment)       # the regenerated range on its own
    gen.verify(out, 720, len(video), FPS)
    trial.save(root(identity) / 'current.json', dict(video=str(out), version=version, updated_at=time.time()))
    impl._progress_update(message='음악 붙이기 · 후보정 중')
    reference_png = root(identity) / 'reference.png'
    shown, music = _deliverable(out, settings, 0.0, len(video) / FPS, reference_png)
    shown_segment, _ = _deliverable(segment, settings, first / FPS, (last - first + 1) / FPS, reference_png)
    result = dict(video=str(out), segment=str(segment), delivered=str(shown), segment_delivered=str(shown_segment),
                  first=first, last=last, addition=addition, prompt_source=how, seed=seed, options=opts,
                  finished_at=time.time())
    trial.save(folder / 'result.json', result)
    how_label = {'gemma': 'Gemma', 'manual': '직접 입력', 'reuse': '이전 구간과 동일'}.get(how, '기본 손 규칙')
    span_text = f'{first / FPS:.2f}~{(last + 1) / FPS:.2f}초'
    _tell(f'{identity}:fix:{version}',
        f'SCAIL-2 구간 수정 완료 · 수정본 {version}\n' + gen.source_line(settings) +
        f'수정 구간 {span_text} (나머지는 그대로)\n'
        f'요청: {request["note"][:200]}\n추가 프롬프트({how_label}): {addition}\n'
        f'{"음악 포함" if music.get("audio_included") else "음악 기준 확인 필요"}\n'
        f'이 영상에 다시 답장하면 이어서 고칠 수 있어요.\nID: {identity}', shown)
    _tell(f'{identity}:fix:{version}:segment',
        f'수정 구간만 · {span_text} (수정본 {version})\n시드 {seed}', shown_segment)
    impl._progress_done('SCAIL-2 구간 수정 완료')
    url = lambda p: '/comfy-output/' + str(p.relative_to(trial.COMFY / 'output'))
    poster = f'/api/scail/jobs/{identity}/reference'
    return dict(music=music,
                videos=[dict(name=shown.name, url=url(shown), poster=poster, label=f'구간 수정본 {version}'),
                        dict(name=shown_segment.name, url=url(shown_segment), poster=poster,
                             label=f'수정 구간만 {version} · {span_text}')],
                finished_at=time.time())


def parse_range(text):
    """'3.2-3.6', '3.2~3.6초', or a single '3.4초' (±0.4s). Returns (start, end, note) or None."""
    m = re.search(r'(\d+(?:\.\d+)?)\s*초?\s*[-~]\s*(\d+(?:\.\d+)?)\s*초?', text)
    if m:
        start, end = float(m.group(1)), float(m.group(2))
    else:
        m = re.search(r'(\d+(?:\.\d+)?)\s*초', text)
        if not m:
            return None
        start, end = max(0.0, float(m.group(1)) - .4), float(m.group(1)) + .4
    note = (text[:m.start()] + text[m.end():]).strip(' ,.') or text
    return start, end, note


def _prioritise(runtime, queue, job_id):
    """Move the just-enqueued repair to the front of the pending queue and persist that order.
    Uses the queue's own intended mechanism (reorder _JOB_QUEUE, then sync())."""
    try:
        with runtime._JOB_QUEUE_LOCK:
            q = runtime._JOB_QUEUE
            job = next((j for j in q if j.get('id') == job_id), None)
            if job is not None:
                q.remove(job)
                q.insert(0, job)
        queue.sync()
    except Exception:
        gen.LOG.exception('SCAIL repair prioritise failed: %s', job_id)


def _requeue(queue, runtime, identity, repair_id=None, front=True):
    """Re-add a stopped job to the pending queue right after the repair, mirroring
    Queue.restore(). Stable submission slots make re-running reattach the already-finished
    chunks (chunks 1,2 kept) and regenerate only the interrupted chunk onward."""
    with queue.store.db() as db:
        row = db.execute('SELECT * FROM jobs WHERE id=?', (identity,)).fetchone()
        if not row:
            return False
        row = dict(row)
        if row['func'] not in queue.functions or row['status'] not in TERMINAL:
            return False
        args, kwargs = queue.store.decode(json.loads(row['payload']), db, lazy=True)
        # Stopping on purpose is not a failure: do not count the interrupted run towards the 3-attempt limit.
        db.execute("UPDATE jobs SET status='queued',error='',attempts=MAX(0,attempts-1) WHERE id=?", (identity,))
        row['status'] = 'queued'
    job = queue.make_job(row, args, kwargs)
    job['status'] = 'queued'
    job.setdefault('meta', {})['recovery'] = '구간 수정 우선 처리 · 완료 구간 보존 후 이어서 재개'
    with runtime._JOB_QUEUE_LOCK:
        if identity == getattr(runtime, '_ACTIVE_QUEUE_JOB_ID', None):
            return False
        q = runtime._JOB_QUEUE
        q[:] = [j for j in q if j.get('id') != identity]
        idx = next((i for i, j in enumerate(q) if j.get('id') == repair_id), None) if repair_id else None
        if idx is not None:
            q.insert(idx + 1, job)          # right after the repair
        elif front:
            q.insert(0, job)
        else:
            q.append(job)
        runtime._JOB_HISTORY[identity] = job
        if hasattr(runtime, '_ensure_queue_worker_locked'):
            runtime._ensure_queue_worker_locked()
    queue.sync()
    return True


def _resume_after_stop(queue, runtime, identity, repair_id):
    """Wait for the cancelled job to actually stop (cancel is cooperative), then resume it
    right after the repair. Runs in a daemon thread so the request returns immediately."""
    deadline = time.time() + 300
    while time.time() < deadline:
        with runtime._JOB_QUEUE_LOCK:
            active = getattr(runtime, '_ACTIVE_QUEUE_JOB_ID', None)
        if active != identity:          # the old instance released the worker
            try:
                with queue.store.db() as db:
                    row = db.execute('SELECT status FROM jobs WHERE id=?', (identity,)).fetchone()
                if row and row['status'] in TERMINAL:
                    _requeue(queue, runtime, identity, repair_id)
                    return
            except Exception:
                gen.LOG.exception('SCAIL repair resume-after-stop failed: %s', identity)
                return
        time.sleep(0.5)
    gen.LOG.warning('SCAIL repair: preempted job %s did not stop in time; not resumed', identity)


NEAR_DONE = 0.85     # don't discard a job already this far through; let it finish


def _near_done(runtime, active, threshold=NEAR_DONE):
    """True when the active job is close enough to finishing that we let it complete rather
    than throw away nearly finished work (e.g. the final step of the final chunk)."""
    try:
        snap = runtime._progress_snapshot() if hasattr(runtime, '_progress_snapshot') else {}
    except Exception:
        snap = {}
    step, total_steps = snap.get('step') or 0, snap.get('total_steps') or 0
    frac_step = (step / total_steps) if total_steps else 0.0
    cur = tot = 0
    if re.fullmatch(r'scail_[a-f0-9]{32}', active or ''):
        cp = gen.ROOT / active / 'chunk-progress.json'
        if cp.exists():
            try:
                c = json.loads(cp.read_text())
                cur, tot = c.get('current', 0), c.get('total', 0)
            except Exception:
                pass
    if tot:
        overall = ((cur - 1) + frac_step) / tot
        return overall >= threshold or (cur >= tot and frac_step >= 0.75)
    return frac_step >= threshold


def _preempt_active(runtime, queue, repair_id):
    """Cancel the running video job so the prioritised repair starts now, then resume the
    cancelled job afterwards (completed chunks preserved). A job that is almost finished is
    left alone — the repair is already next in line. Returns a small action dict, or None."""
    with runtime._JOB_QUEUE_LOCK:
        active = getattr(runtime, '_ACTIVE_QUEUE_JOB_ID', None)
    if not active or active == repair_id:
        return None
    if _near_done(runtime, active):
        return {'action': 'waited', 'job': active}
    try:
        httpx.post(f'http://127.0.0.1:7870/api/jobs/{active}/cancel', timeout=10).raise_for_status()
    except Exception:
        gen.LOG.exception('SCAIL repair preempt-cancel failed: %s', active)
        return None
    threading.Thread(target=_resume_after_stop, args=(queue, runtime, active, repair_id), daemon=True).start()
    return {'action': 'cancelled', 'job': active}


def configure(app, runtime, queue):
    global impl
    impl = runtime

    @app.get('/api/scail/repair/candidates')
    def candidates(limit: int = 40):
        base = trial.COMFY / 'output/scail_user'
        # Cheap pass first: find finished videos and their mtime, newest first, then do the
        # heavier settings_for() lookup only for the handful we actually return.
        found = []
        if base.exists():
            for d in base.iterdir():
                if not d.is_dir() or not re.fullmatch(r'scail_[a-f0-9]{32}', d.name):
                    continue
                vid = current_video(d.name)
                try:
                    mt = vid.stat().st_mtime
                except OSError:
                    continue
                found.append((mt, d.name, vid))
        found.sort(reverse=True)
        items = []
        for mt, name, vid in found[:max(1, min(limit, 60))]:
            try:
                title = gen.settings_for(root(name)).get('title') or name
            except Exception:
                title = name
            version, updated = 0, mt
            cur = root(name) / 'current.json'
            if cur.exists():
                try:
                    c = json.loads(cur.read_text())
                    version, updated = c.get('version', 0), c.get('updated_at', updated)
                except Exception:
                    pass
            items.append(dict(identity=name, title=title, version=version, updated_at=updated,
                video='/comfy-output/' + str(vid.relative_to(trial.COMFY / 'output')),
                reference=f'/api/scail/repair/thumb/{name}'))
        return dict(items=items)

    @app.get('/api/scail/repair/thumb/{identity}')
    def repair_thumb(identity: str):
        if not re.fullmatch(r'scail_[a-f0-9]{32}', identity):
            raise HTTPException(404, 'not found')
        src = root(identity) / 'reference.png'
        if not src.exists():
            raise HTTPException(404, 'not found')
        thumb = root(identity) / 'repair-thumb.jpg'
        if not thumb.exists() or thumb.stat().st_mtime < src.stat().st_mtime:
            try:
                subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(src),
                    '-vf', 'scale=160:-2', '-q:v', '5', str(thumb)],
                    check=True, capture_output=True, timeout=30)
            except Exception:
                return FileResponse(src, media_type='image/png')
        return FileResponse(thumb, media_type='image/jpeg', headers={'Cache-Control': 'public, max-age=86400'})

    @app.get('/api/scail/repair/{identity}/{rid}/status')
    def repair_status(identity: str, rid: str):
        if not re.fullmatch(r'scail_[a-f0-9]{32}', identity):
            raise HTTPException(404, '작업을 찾을 수 없습니다.')
        rid = re.sub(r'[^a-f0-9]', '', rid)[:32]
        jid = 'scailfix_' + rid
        folder = root(identity) / 'repairs' / rid
        res_path = folder / 'result.json'
        with runtime._JOB_QUEUE_LOCK:
            active = getattr(runtime, '_ACTIVE_QUEUE_JOB_ID', None)
            job = dict(runtime._JOB_HISTORY.get(jid, {}))
            pos = next((i + 1 for i, j in enumerate(runtime._JOB_QUEUE) if j.get('id') == jid), None)
        state = job.get('status') or ('done' if res_path.exists() else 'queued')
        out = dict(identity=identity, rid=rid, state=state, position=pos, active=(jid == active))
        if jid == active and hasattr(runtime, '_progress_snapshot'):
            p = runtime._progress_snapshot() or {}
            out.update(message=p.get('message'), step=p.get('step'), total=p.get('total_steps'))
        if res_path.exists():
            try:
                r = json.loads(res_path.read_text())
                out['state'] = 'done'
                out['video'] = '/comfy-output/' + str(Path(r['video']).relative_to(trial.COMFY / 'output'))
                out.update(first=r.get('first'), last=r.get('last'), addition=r.get('addition'), seed=r.get('seed'))
                for key in ('segment', 'delivered', 'segment_delivered'):
                    if r.get(key):
                        out[key] = '/comfy-output/' + str(Path(r[key]).relative_to(trial.COMFY / 'output'))
                out.update(options=r.get('options') or {}, prompt_source=r.get('prompt_source'))
                req_file = folder / 'request.json'
                if req_file.exists():
                    q = json.loads(req_file.read_text())
                    out['request'] = dict(start=q.get('start'), end=q.get('end'), note=q.get('note'))
            except Exception:
                pass
        if job.get('error'):
            out['error'] = job['error']
        return out

    @app.get('/api/scail/repair/{identity}/versions')
    def repair_versions(identity: str):
        if not re.fullmatch(r'scail_[a-f0-9]{32}', identity) or not (root(identity) / 'request.json').exists():
            raise HTTPException(404, 'SCAIL 작업을 찾을 수 없습니다.')
        return dict(items=list_versions(identity, runtime))

    @app.post('/api/scail/repair/{identity}/deliver')
    def repair_deliver(identity: str, version: int = Form(...)):
        if not re.fullmatch(r'scail_[a-f0-9]{32}', identity) or not (root(identity) / 'request.json').exists():
            raise HTTPException(404, 'SCAIL 작업을 찾을 수 없습니다.')
        current = next((v for v in list_versions(identity, runtime) if v['version'] == version), None)
        if not current:
            raise HTTPException(404, f'수정본 {version}을(를) 찾을 수 없어요.')
        if current['exporting']:
            raise HTTPException(409, '이미 내보내는 중이에요.')
        if current['delivered']:
            raise HTTPException(409, '이미 음악·후보정본이 있어요.')
        info = span_of_version(identity, version) or {}
        start = (info['first'] / FPS) if info else 0.0
        end = ((info['last'] + 1) / FPS) if info else 1 / FPS
        rid = uuid.uuid4().hex
        folder = root(identity) / 'repairs' / rid
        folder.mkdir(parents=True, exist_ok=True)
        trial.save(folder / 'request.json', dict(start=start, end=end, deliver=version,
                                                 note=f'수정본 {version} 내보내기'))
        job_id = 'scailfix_' + rid
        result = queue.enqueue(run, (identity, rid), {}, kind=f'SCAIL-2 수정본 {version} 내보내기 · 음악·후보정',
                               identity=job_id)
        _prioritise(runtime, queue, job_id)     # next in line; never interrupts the running job
        return dict(result, rid=rid, version=version)

    @app.get('/api/scail/repair/{identity}/defaults')
    def repair_defaults(identity: str):
        if not re.fullmatch(r'scail_[a-f0-9]{32}', identity) or not (root(identity) / 'request.json').exists():
            raise HTTPException(404, 'SCAIL 작업을 찾을 수 없습니다.')
        s = gen.settings_for(root(identity))
        distill = bool(s.get('official_distill'))
        return dict(title=s.get('title'), steps=s.get('steps'), dpo=bool(s.get('dpo_lora')),
                    lora_strength=1.0 if distill else 0.8, shift=1.0 if distill else 5.0, cfg=1.0,
                    pose_strength=1.0, dpo_strength=1.0, tail=TAIL, width=NATIVE_W, height=NATIVE_H,
                    prompt=s.get('prompt', ''), max_seconds=(MAX_FRAMES - CONTEXT - TAIL) / FPS, fps=FPS)

    @app.post('/api/scail/jobs/{identity}/repair')
    def repair(identity: str, text: str = Form(''), start: float | None = Form(None), end: float | None = Form(None),
               note: str = Form(''), request_id: str = Form(''), addition: str = Form(''),
               seed: int | None = Form(None), preempt: bool = Form(True), options: str = Form(''),
               reuse: bool = Form(False), reference: UploadFile | None = File(None)):
        if not re.fullmatch(r'scail_[a-f0-9]{32}', identity) or not (root(identity) / 'request.json').exists():
            raise HTTPException(404, 'SCAIL 작업을 찾을 수 없습니다.')
        if not current_video(identity).exists():
            raise HTTPException(409, '완성 영상이 있어야 구간을 수정할 수 있어요.')
        if start is None or end is None:
            parsed = parse_range(text)
            if not parsed:
                raise HTTPException(422, '고칠 구간을 초로 적어 주세요. 예: 3.2-3.6 왼손 손가락')
            start, end, note = parsed
        note = (note or text or '손가락 모양이 이상함')[:500]
        if not all(math.isfinite(v) for v in (start, end)) or start < 0 or end <= start:
            raise HTTPException(422, '구간 시간을 확인해 주세요.')
        try:
            opts = sanitize_options(options)
            frame_range(start, end, 10**6, opts.get('tail'))
        except ValueError as error:
            raise HTTPException(422, str(error))
        rid = re.sub(r'[^a-f0-9]', '', request_id)[:32] or uuid.uuid4().hex
        folder = root(identity) / 'repairs' / rid
        folder.mkdir(parents=True, exist_ok=True)
        payload = dict(start=start, end=end, note=note)
        addition = (addition or '').strip()
        if addition:
            payload['addition'] = addition[:400]
            if reuse:
                payload['reuse'] = True
        if seed is not None and seed >= 0:
            payload['seed'] = int(seed) % (2**31)
        if reference is not None and reference.filename:
            try:
                payload['ref'] = save_reference(reference, folder)
            except ValueError as error:
                raise HTTPException(422, str(error))
        if opts.get('ref_to_scail') and 'ref' not in payload:
            opts.pop('ref_to_scail')
        if opts:
            payload['options'] = opts
        if (folder / 'request.json').exists():
            if json.loads((folder / 'request.json').read_text()) != payload:
                raise HTTPException(409, '같은 요청 ID에 다른 내용이 있어요.')
        else:
            trial.save(folder / 'request.json', payload)
        title = f'SCAIL-2 구간 수정 · {start:.2f}~{end:.2f}초 · {note[:40]}'
        job_id = 'scailfix_' + rid
        result = queue.enqueue(run, (identity, rid), {}, kind=title, identity=job_id)
        _prioritise(runtime, queue, job_id)
        cancelled = _preempt_active(runtime, queue, job_id) if preempt else None
        return dict(result, start=start, end=end, note=note, rid=rid, prioritised=True, preempted=cancelled)


def workspace_items(runtime, queue):
    """Repair jobs as work-tab / result cards, shaped like scail_generation.workspace_items."""
    with queue.store.db() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM jobs WHERE func='scail_repair' ORDER BY created DESC LIMIT 60")]
        parents = {}
        for row in rows:
            try:
                args, _ = queue.store.decode(json.loads(row['payload']), db, lazy=True)
                parents[row['id']] = str(args[0])
            except Exception:
                parents[row['id']] = None
    with runtime._JOB_QUEUE_LOCK:
        live = {r['id']: dict(runtime._JOB_HISTORY.get(r['id'], {})) for r in rows}
        order = {j['id']: i + 1 for i, j in enumerate(runtime._JOB_QUEUE)}
        active = runtime._ACTIVE_QUEUE_JOB_ID
    items = []
    for row in rows:
        parent = parents.get(row['id'])
        job = live[row['id']]
        state = job.get('status') or row['status']
        snap = runtime._progress_snapshot() if row['id'] == active else {}
        result = json.loads(row['result'] or '{}')
        frames = None
        if parent and re.fullmatch(r'scail_[a-f0-9]{32}', parent):
            try:
                q = json.loads((root(parent) / 'repairs' / row['id'][len('scailfix_'):] / 'request.json').read_text())
                frames = max(1, round((q['end'] - q['start']) * FPS))
            except Exception:
                pass
        message = snap.get('message') or ''
        items.append(dict(id=row['id'], title=row['kind'] or 'SCAIL-2 구간 수정', model='SCAIL-2 Q5',
            state='queued' if state == 'waiting' else state, width=720, height=1280, frames=frames, fps=FPS,
            videos=result.get('videos', []), position=order.get(row['id']),
            started_at=job.get('started_at') or row['created'],
            finished_at=result.get('finished_at') or job.get('finished_at'), expected_seconds=None,
            progress={'node': 'sample' if '스텝' in message else '', 'step': snap.get('step'), 'total': snap.get('total_steps')},
            stage_message=message or '구간 수정',
            reference=f'/api/scail/jobs/{parent}/reference' if parent and re.fullmatch(r'scail_[a-f0-9]{32}', parent) else None))
    return items
