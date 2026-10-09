"""Gemma maintenance assistant: ask questions about the studio, or have small fixes proposed (and applied) by the local model.

No external AI is used: the request is queued like a video job (one GPU), answered by the local Gemma, and every change is
  * limited to an explicit allow-list of files (auth, tokens, queue core, studio_app and this module are not on it),
  * a small search/replace patch that must match exactly once,
  * checked before it touches disk (Python syntax/undefined names, JS syntax, HTML without inline scripts),
  * backed up, with one-click undo, and
  * for server code: never applied without approval, and restarted under a watchdog that rolls back if the app does not come up.
"""
import ast
import contextlib
import difflib
import hashlib
import html.parser
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

import httpx
from fastapi import Form, HTTPException, Request

from media_runtime import gpu_session

BASE = Path(__file__).resolve().parent
DATA = BASE / 'ops_assistant_data'
DB_PATH = DATA / 'assistant.sqlite3'
BACKUPS = DATA / 'backups'
GUARD = BASE / 'ops_restart_guard.py'
PYTHON = '/home/ben/anaconda3/bin/python'
GEMMA = os.environ.get('OPS_ASSISTANT_MODEL', 'gemma4:26b-a4b-it-qat')
SERVICE = 'zstudio.service'
HEALTH_URL = 'http://127.0.0.1:7870/'
PUBLIC = 'https://yunalee.shop'

# Explicit allow-list. A path that is not here can never be proposed, applied or restored.
TARGETS = {
    'studio_static/scail-repair.js': ('구간 수정 화면 동작 (JS)', 'static'),
    'studio_static/scail-repair.html': ('구간 수정 화면 모양 (HTML/CSS)', 'static'),
    'studio_static/scail-live.js': ('생성 중 보기 화면 동작 (JS)', 'static'),
    'studio_static/scail-live.html': ('생성 중 보기 화면 모양 (HTML/CSS)', 'static'),
    'studio_static/scail-entry.js': ('메뉴·바로가기 버튼 (JS)', 'static'),
    'scail_repair.py': ('구간 수정 서버 (Python · 재시작 필요)', 'python'),
    'scail_live.py': ('생성 중 보기 서버 (Python · 재시작 필요)', 'python'),
    'youtube_api.py': ('유튜브 예약 서버 (Python · 재시작 필요)', 'python'),
}
MAX_BLOCKS = 4
MAX_CHANGED_LINES = 80
PROMPT_BUDGET = 9000           # characters of file excerpt sent to the model (the app caps the context at 8192 tokens)
GPU_WAIT_SECONDS = 25 * 60

impl = None
_lock = threading.RLock()
_active = threading.Lock()


# ---------------------------------------------------------------- storage
@contextlib.contextmanager
def db():
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        with _lock:
            conn.executescript('''
              CREATE TABLE IF NOT EXISTS requests(
                id TEXT PRIMARY KEY, created REAL, kind TEXT, prompt TEXT, target TEXT, status TEXT, reply TEXT,
                edits TEXT, diff TEXT, checks TEXT, flags TEXT, changed INTEGER DEFAULT 0, tier TEXT,
                applied_at REAL, original_sha TEXT, applied_sha TEXT, restart_needed INTEGER DEFAULT 0,
                error TEXT, include_logs INTEGER DEFAULT 0, finished_at REAL, restarted_at REAL);
            ''')
            with conn:
                yield conn
    finally:
        conn.close()


def row_dict(row, full=True):
    data = dict(row)
    for key in ('edits', 'checks', 'flags'):
        data[key] = json.loads(data[key]) if data.get(key) else []
    if not full:
        for key in ('edits', 'diff', 'reply'):
            data.pop(key, None)
    data['target_label'] = TARGETS.get(data.get('target'), ('', ''))[0]
    return data


def set_state(rid, **fields):
    for key in ('edits', 'checks', 'flags'):
        if key in fields and not isinstance(fields[key], str):
            fields[key] = json.dumps(fields[key], ensure_ascii=False)
    sets = ','.join(f'{k}=?' for k in fields)
    with db() as conn:
        conn.execute(f'UPDATE requests SET {sets} WHERE id=?', (*fields.values(), rid))


def get_request(rid):
    with db() as conn:
        row = conn.execute('SELECT * FROM requests WHERE id=?', (rid,)).fetchone()
    return row_dict(row) if row else None


def sha_of(text):
    return hashlib.sha256(text.encode()).hexdigest()


def telegram(key, text):
    """One text message to the owner through the Telegram bridge outbox. Never raises."""
    try:
        with sqlite3.connect(BASE / 'telegram_data/bridge.sqlite3', timeout=20) as conn:
            owner = conn.execute("SELECT value FROM config WHERE key='owner'").fetchone()
            if owner:
                conn.execute('INSERT OR IGNORE INTO outbox(id,created,method,payload,attachment) VALUES(?,?,?,?,?)',
                             (key, time.time(), 'sendMessage',
                              json.dumps({'chat_id': int(owner[0]), 'text': text[:3500]}, ensure_ascii=False), None))
    except Exception:
        pass


# ---------------------------------------------------------------- files
def target_path(rel):
    if rel not in TARGETS:
        raise ValueError('이 파일은 도우미가 고칠 수 없어요.')
    path = (BASE / rel).resolve()
    if BASE.resolve() not in path.parents:
        raise ValueError('허용되지 않은 경로예요.')
    return path


def read_target(rel):
    return target_path(rel).read_text(encoding='utf-8')


def suggest_target(prompt):
    text = prompt.lower()
    server = any(w in text for w in ('서버', '오류', '에러', 'traceback', 'error', '실패', '500', '로그'))
    if any(w in text for w in ('유튜브', 'youtube', '쇼츠', '토큰', 'invalid_grant')):
        return 'youtube_api.py'
    if any(w in text for w in ('생성 중', '생성중', '미리보기', '구간 보기', '중단', '이어서 생성')):
        return 'scail_live.py' if server else 'studio_static/scail-live.js'
    if any(w in text for w in ('메뉴', '바로가기', '링크 버튼')):
        return 'studio_static/scail-entry.js'
    if any(w in text for w in ('구간 수정', '손가락', '해상도', '프리셋', '타임라인', '연장', '내보내기', '수정본')):
        if server:
            return 'scail_repair.py'
        return 'studio_static/scail-repair.html' if any(w in text for w in ('색', '모양', '크기', '글자', '간격', '디자인', '스타일')) else 'studio_static/scail-repair.js'
    return 'studio_static/scail-repair.js'


# ---------------------------------------------------------------- context for the model
def redact(text):
    text = re.sub(r'(?i)(bearer\s+)[\w\-.~+/=]+', r'\1<가림>', text)
    text = re.sub(r'bot\d+:[\w-]+', 'bot<가림>', text)
    text = re.sub(r'(?i)(token|secret|password|passwd|authorization|api[_-]?key)(["\']?\s*[:=]\s*["\']?)[^\s"\',}]+', r'\1\2<가림>', text)
    return re.sub(r'[A-Za-z0-9_\-]{40,}', '<가림>', text)


def recent_errors(limit=1800):
    try:
        out = subprocess.run(['journalctl', '--user', '-u', SERVICE, '-n', '600', '--no-pager', '-o', 'cat'],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return ''
    lines = out.splitlines()
    keep, seen = [], set()
    for i, line in enumerate(lines):
        if re.search(r'Traceback|Error|Exception|fallback|실패|오류', line) and not re.search(r'"(GET|POST|PUT|DELETE) /', line):
            for item in lines[max(0, i - 1):i + 4]:
                if item not in seen and not re.search(r'"(GET|POST|PUT|DELETE) /', item):
                    seen.add(item)
                    keep.append(item)
    return redact('\n'.join(keep))[-limit:]


OUTLINE = re.compile(r'^\s*(?:async\s+def|def|class|function|const\s+\w+\s*=\s*(?:async\s*)?\(|@app\.\w+|<(?:section|details|h1|h2|button|script)\b)')


def outline(lines, limit=1500):
    out, size = [], 0
    for number, line in enumerate(lines, 1):
        if OUTLINE.match(line):
            item = f'{number}: {line.strip()[:90]}'
            size += len(item) + 1
            if size > limit:
                break
            out.append(item)
    return '\n'.join(out)


def words(text):
    found = set(w.lower() for w in re.findall(r'[가-힣]{2,}|[A-Za-z_][A-Za-z0-9_]{3,}', text))
    return found


def pick_excerpt(text, query, error_text='', budget=PROMPT_BUDGET, name=''):
    """Pieces of the file most related to the request, with line numbers. Always the head, plus lines named in a traceback."""
    lines = text.splitlines()
    if len(text) <= budget:
        return '\n'.join(f'{n:>5}| {l}' for n, l in enumerate(lines, 1)), 'full'
    window, step = 36, 28
    chunks = []
    for start in range(0, len(lines), step):
        chunks.append((start, min(len(lines), start + window)))
    terms = words(query)
    ids = words(error_text)
    scored = []
    for start, end in chunks:
        blob = '\n'.join(lines[start:end]).lower()
        score = sum(blob.count(t) for t in terms) + 3 * sum(blob.count(t) for t in ids)
        scored.append((score, start, end))
    forced = set()
    for path, number in re.findall(r'File "([^"]+)", line (\d+)', error_text):
        if name and Path(path).name != name:
            continue
        n = int(number)
        forced.add((max(0, n - 26), min(len(lines), n + 26)))
    chosen = [(0, min(len(lines), 12))] + sorted(forced)
    used = sum(sum(len(l) + 8 for l in lines[a:b]) for a, b in chosen)
    for score, start, end in sorted(scored, reverse=True):
        if score <= 0:
            break
        cost = sum(len(l) + 8 for l in lines[start:end])
        if used + cost > budget:
            continue
        chosen.append((start, end))
        used += cost
    merged = []
    for a, b in sorted(chosen):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    parts = []
    for a, b in merged:
        parts.append('\n'.join(f'{n:>5}| {lines[n - 1]}' for n in range(a + 1, b + 1)))
    return '\n   ...\n'.join(parts), 'excerpt'


SYSTEM_ASK = ('You are the maintenance assistant of a small private web studio (Python/FastAPI server, plain HTML/JS pages). '
              'Answer in Korean, concisely (at most about 12 lines), for a non-programmer owner. '
              'Use only the provided file excerpt and log lines; if they are not enough, say exactly what is missing. '
              'Never invent file contents or settings. Do not output code unless asked.')
SYSTEM_FIX = ('You maintain a small private web studio (Python/FastAPI server, plain HTML/JS pages). You get ONE file excerpt '
              '(each line is prefixed with its line number and "| ", which is NOT part of the file) and a change request.\n'
              'Make the SMALLEST possible change. Reply in exactly this format:\n'
              '설명: <one to three short Korean lines saying what you change and why>\n'
              '<<<<<<< SEARCH\n(existing lines copied exactly from the file, WITHOUT the line-number prefix)\n=======\n'
              '(replacement lines)\n>>>>>>> REPLACE\n'
              'Rules: every SEARCH must match the file character for character and be unique, so include 2-3 unchanged neighbouring '
              'lines. Up to 4 blocks. Do not touch unrelated code. Keep the existing indentation and style. In HTML never add inline '
              '<script> blocks or on*= attributes (the site CSP blocks them); never add network calls to other hosts. '
              'If the request cannot be done safely with a small edit, reply only with the "설명:" line explaining why and NO blocks.')


def build_prompt(kind, request_text, rel, text, include_logs):
    lines = text.splitlines()
    errors = recent_errors() if include_logs else ''
    base = Path(rel).name
    excerpt, mode = pick_excerpt(text, request_text, errors, name=base)
    traceback_here = '\n'.join(l for l in errors.splitlines() if base in l)
    parts = [f'요청: {request_text.strip()[:1200]}', f'대상 파일: {rel} ({len(lines)}줄, {"전체" if mode == "full" else "관련 부분만 발췌"})']
    if mode != 'full':
        parts.append('파일 구조(줄 번호: 선언):\n' + outline(lines))
    if errors:
        parts.append('최근 서버 오류 로그(민감값은 가림):\n' + errors[-1500:])
    parts.append('파일 내용:\n' + excerpt)
    prompt = '\n\n'.join(parts)
    return (SYSTEM_ASK if kind == 'ask' else SYSTEM_FIX), prompt, bool(traceback_here)


def ask_gemma(system, user, request_id, num_predict=1600):
    """Local Gemma only. It needs the whole GPU, so wait (politely, cancellably) until the video work in front is done."""
    deadline = time.monotonic() + GPU_WAIT_SECONDS
    while True:
        impl._check_cancelled()
        try:
            with gpu_session('llm'):
                response = httpx.post('http://127.0.0.1:11434/api/chat', timeout=900, json=dict(
                    model=GEMMA, stream=False, think=False, keep_alive=0,
                    options=dict(temperature=0.1, num_ctx=8192, num_predict=num_predict, num_gpu=22),
                    messages=[dict(role='system', content=system), dict(role='user', content=user)]))
            response.raise_for_status()
            return response.json()['message']['content'].strip()
        except RuntimeError as error:
            if 'GPU' not in str(error) or time.monotonic() >= deadline:
                raise
            impl._progress_update(message='GPU 작업이 끝나길 기다리는 중 (영상 생성 뒤에 이어서 답해요)')
            time.sleep(10)


# ---------------------------------------------------------------- patch handling
BLOCK = re.compile(r'<{5,9}\s*SEARCH[^\n]*\n(.*?)\n={5,9}\s*\n(.*?)\n?>{5,9}\s*REPLACE', re.S)


def parse_reply(reply):
    """-> (explanation, [(search, replace), ...])"""
    blocks = [(m.group(1), m.group(2)) for m in BLOCK.finditer(reply)]
    head = reply.split('<<<<<<<', 1)[0]
    head = re.sub(r'^\s*설명\s*[:：]\s*', '', head.strip())
    return head.strip()[:900], blocks


def strip_gutter(text):
    """The model sometimes copies the 'NNN| ' line-number prefix. Remove it only if every non-empty line has it."""
    lines = text.split('\n')
    if lines and all(re.match(r'^\s*\d+\| ?', l) for l in lines if l.strip()):
        return '\n'.join(re.sub(r'^\s*\d+\| ?', '', l, count=1) for l in lines)
    return text


def locate(original, search):
    """Exact unique match first; then ignoring trailing whitespace. -> (start, end) or an error string."""
    count = original.count(search)
    if count == 1:
        i = original.index(search)
        return i, i + len(search)
    if count > 1:
        return f'일치하는 곳이 {count}군데예요 (더 많은 앞뒤 줄이 필요)'
    original_lines, search_lines = original.split('\n'), search.split('\n')
    target = [l.rstrip() for l in search_lines]
    hits = [i for i in range(len(original_lines) - len(target) + 1)
            if [l.rstrip() for l in original_lines[i:i + len(target)]] == target]
    if len(hits) == 1:
        i = hits[0]
        start = sum(len(l) + 1 for l in original_lines[:i])
        end = start + sum(len(l) + 1 for l in original_lines[i:i + len(target)]) - 1
        return start, end
    return '파일에서 찾지 못했어요' if not hits else f'일치하는 곳이 {len(hits)}군데예요'


def apply_blocks(original, blocks):
    """-> (new_text, errors[]). Blocks are located against the ORIGINAL text, must not overlap, and are applied back to front."""
    spans, errors = [], []
    for number, (search, replace) in enumerate(blocks, 1):
        search, replace = strip_gutter(search), strip_gutter(replace)
        if not search.strip():
            errors.append(f'{number}번 블록: 찾을 문장이 비어 있어요')
            continue
        found = locate(original, search)
        if isinstance(found, str):
            errors.append(f'{number}번 블록: {found}')
            continue
        spans.append((found[0], found[1], replace, number))
    spans.sort()
    for left, right in zip(spans, spans[1:]):
        if left[1] > right[0]:
            errors.append(f'{left[3]}번과 {right[3]}번 블록이 겹쳐요')
    if errors:
        return None, errors
    text = original
    for start, end, replace, _ in reversed(spans):
        text = text[:start] + replace + text[end:]
    return text, []


def make_diff(rel, old, new):
    return ''.join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        fromfile=rel, tofile=rel, n=3))


def changed_lines(diff):
    return sum(1 for l in diff.splitlines() if l[:1] in '+-' and not l.startswith(('+++', '---')))


def added_lines(diff):
    return [l[1:] for l in diff.splitlines() if l.startswith('+') and not l.startswith('+++')]


def pyflakes_messages(code, name):
    try:
        from pyflakes import api, reporter
    except ImportError:
        return None
    out, err = io.StringIO(), io.StringIO()
    api.check(code, name, reporter.Reporter(out, err))
    return [re.sub(r'^[^:]+:\d+:\d+:?\s*', '', l) for l in out.getvalue().splitlines()]


VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}


class Scan(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.inline = self.handlers = 0
        self.balance = Counter()
        self.external = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.balance[tag] += 1
        attrs = dict(attrs)
        self.handlers += sum(1 for k in attrs if k.startswith('on'))
        if tag == 'script' and not attrs.get('src'):
            self.inline += 1
        for key in ('src', 'href', 'action'):
            if attrs.get(key) and re.match(r'(?i)https?://', attrs[key]):
                self.external.append(attrs[key])

    def handle_endtag(self, tag):
        if tag not in VOID:
            self.balance[tag] -= 1


def scan_html(text):
    scan = Scan()
    scan.feed(text)
    scan.close()
    return scan


STATIC_BAD = ((r'\beval\s*\(', 'eval'), (r'\bnew\s+Function\b', 'new Function'), (r'document\.cookie', '쿠키 접근'),
              (r'importScripts', 'importScripts'), (r'https?://', '외부 주소'), (r'\bXMLHttpRequest\b', 'XMLHttpRequest'))
PY_FLAG = ((r'\bsubprocess\b', '다른 프로그램 실행(subprocess)'), (r'os\.system', '셸 명령 실행(os.system)'),
           (r'\beval\s*\(', 'eval'), (r'\bexec\s*\(', 'exec'), (r'__import__', '동적 import'), (r'\bsocket\b', '소켓 통신'),
           (r'shutil\.rmtree', '폴더 통째로 삭제'), (r'os\.remove', '파일 삭제(os.remove)'), (r'\.unlink\(', '파일 삭제(unlink)'),
           (r'\brequests\.', '네트워크 호출(requests)'), (r'\burllib\b', '네트워크 호출(urllib)'))


def validate(rel, old, new, diff):
    """-> (checks[(label, ok, note)], flags[str], blocking: bool)"""
    kind = TARGETS[rel][1]
    checks, flags = [], []
    added = '\n'.join(added_lines(diff))
    if rel.endswith('.py'):
        try:
            ast.parse(new)
            checks.append(('파이썬 문법', True, ''))
        except SyntaxError as error:
            checks.append(('파이썬 문법', False, f'{error.lineno}줄: {error.msg}'))
        before, after = pyflakes_messages(old, rel), pyflakes_messages(new, rel)
        if before is None or after is None:
            checks.append(('이름 검사', True, 'pyflakes가 없어 건너뜀'))
        else:
            fresh = [m for m in after if m not in before]
            bad = [m for m in fresh if 'undefined name' in m or 'syntax' in m.lower()]
            checks.append(('정의되지 않은 이름 없음', not bad, '; '.join(bad)[:200]))
            other = [m for m in fresh if m not in bad]
            if other:
                flags.append('새 경고: ' + '; '.join(other)[:200])
        for pattern, label in PY_FLAG:
            if re.search(pattern, added):
                flags.append(f'⚠ 위험할 수 있는 코드가 추가됐어요: {label}')
    elif rel.endswith('.js'):
        DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile('w', suffix='.js', dir=DATA, delete=False, encoding='utf-8') as handle:
            handle.write(new)
            temp = handle.name
        try:
            run = subprocess.run(['node', '--check', temp], capture_output=True, text=True, timeout=30)
            checks.append(('자바스크립트 문법', run.returncode == 0, (run.stderr or '').strip().splitlines()[-1][:200] if run.returncode else ''))
        except Exception as error:
            checks.append(('자바스크립트 문법', False, f'검사 실행 실패: {error}'))
        finally:
            Path(temp).unlink(missing_ok=True)
    elif rel.endswith('.html'):
        before, after = scan_html(old), scan_html(new)
        checks.append(('인라인 스크립트 없음 (사이트 보안 규칙)', after.inline <= before.inline, ''))
        checks.append(('on…= 속성 없음 (사이트 보안 규칙)', after.handlers <= before.handlers, ''))
        checks.append(('태그 짝이 맞음', after.balance == before.balance, ''))
        checks.append(('외부 주소 추가 없음', len(after.external) <= len(before.external), ''))
    if kind == 'static':
        for pattern, label in STATIC_BAD:
            if re.search(pattern, added):
                checks.append((f'금지된 코드 없음 ({label})', False, '화면 파일에는 추가할 수 없어요'))
    return checks, flags, any(not ok for _, ok, _ in checks)


def build_proposal(rid, rel, original, reply):
    """-> dict(fields to store). Validates everything but never writes to the target."""
    explanation, blocks = parse_reply(reply)
    if not blocks:
        return dict(status='answered', reply=explanation or reply[:900], edits=[], diff='', checks=[], flags=[], changed=0)
    if len(blocks) > MAX_BLOCKS:
        return dict(status='failed', reply=explanation, error=f'한 번에 {MAX_BLOCKS}곳까지만 고칠 수 있어요. 요청을 나눠 주세요.',
                    edits=[], diff='', checks=[], flags=[], changed=0)
    new, errors = apply_blocks(original, blocks)
    edits = [dict(search=s, replace=r) for s, r in blocks]
    if errors:
        return dict(status='failed', reply=explanation, edits=edits, diff='', checks=[], flags=[], changed=0,
                    error='패치를 파일에 맞출 수 없었어요: ' + ' / '.join(errors))
    diff = make_diff(rel, original, new)
    count = changed_lines(diff)
    if not diff:
        return dict(status='answered', reply=explanation + '\n(바꿀 내용이 이미 같아서 수정할 게 없어요.)', edits=edits, diff='',
                    checks=[], flags=[], changed=0)
    if count > MAX_CHANGED_LINES:
        return dict(status='failed', reply=explanation, edits=edits, diff=diff, checks=[], flags=[], changed=count,
                    error=f'바뀌는 줄이 {count}줄이라 너무 커요 (최대 {MAX_CHANGED_LINES}줄). 요청을 나눠 주세요.')
    checks, flags, blocking = validate(rel, original, new, diff)
    tier = TARGETS[rel][1]
    status = 'invalid' if blocking else 'proposed'
    return dict(status=status, reply=explanation, edits=edits, diff=diff, checks=[list(c) for c in checks], flags=flags,
                changed=count, tier=tier, original_sha=sha_of(original),
                error='검사를 통과하지 못해 적용할 수 없어요.' if blocking else '')


# ---------------------------------------------------------------- apply / undo
# Nothing is ever applied on the model's say-so: a person reads the diff and presses 적용.
def backup_dir(rid):
    return BACKUPS / rid


def write_atomic(path, text):
    temp = path.with_name(path.name + f'.{os.getpid()}.ops.tmp')
    temp.write_text(text, encoding='utf-8')
    with contextlib.suppress(OSError):
        shutil.copymode(path, temp)
    os.replace(temp, path)


def recompute(rid):
    """Rebuild the patched text from the stored blocks against the CURRENT file. Used at approval time."""
    request = get_request(rid)
    rel = request['target']
    original = read_target(rel)
    new, errors = apply_blocks(original, [(e['search'], e['replace']) for e in request['edits']])
    return request, rel, original, new, errors


def apply_request(rid):
    with _lock:
        request, rel, original, new, errors = recompute(rid)
        if request['status'] not in ('proposed',):
            raise ValueError('이미 처리됐거나 적용할 수 없는 상태예요.')
        if sha_of(original) != request['original_sha'] or errors:
            set_state(rid, status='stale', error='제안한 뒤 파일이 바뀌었어요. 다시 요청해 주세요.')
            raise ValueError('제안한 뒤 파일이 바뀌어서 적용하지 않았어요. 다시 요청해 주세요.')
        diff = make_diff(rel, original, new)
        checks, flags, blocking = validate(rel, original, new, diff)
        if blocking:
            raise ValueError('적용 직전 검사를 통과하지 못했어요.')
        path = target_path(rel)
        save = backup_dir(rid) / rel
        save.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, save)
        write_atomic(path, new)
        restart = TARGETS[rel][1] == 'python'
        set_state(rid, status='applied', applied_at=time.time(), applied_sha=sha_of(new),
                  restart_needed=1 if restart else 0, error='')
        return get_request(rid)


def undo_request(rid, force=False):
    with _lock:
        request = get_request(rid)
        if not request or request['status'] != 'applied':
            raise ValueError('되돌릴 수 있는 적용 기록이 아니에요.')
        rel = request['target']
        path = target_path(rel)
        save = backup_dir(rid) / rel
        if not save.exists():
            raise ValueError('백업 파일을 찾을 수 없어요.')
        if sha_of(path.read_text(encoding='utf-8')) != request['applied_sha'] and not force:
            raise PermissionError('적용한 뒤 파일이 또 바뀌었어요. 그래도 되돌리려면 강제로 되돌리세요.')
        shutil.copy2(save, path)
        # A server change that was never loaded needs no restart to be undone; one that was loaded does.
        needs = 1 if TARGETS[rel][1] == 'python' and request['restarted_at'] else 0
        set_state(rid, status='undone', restart_needed=needs, error='')
        return get_request(rid)


# ---------------------------------------------------------------- the queued job
def run(rid):
    request = get_request(rid)
    if not request:
        raise RuntimeError('요청을 찾을 수 없어요.')
    kind, rel = request['kind'], request['target']
    impl._progress_start('Gemma 도우미', 1, '파일과 로그를 읽는 중')
    set_state(rid, status='running')
    try:
        original = read_target(rel)
        system, prompt, _ = build_prompt(kind, request['prompt'], rel, original, bool(request['include_logs']))
        impl._progress_update(message='Gemma가 생각하는 중')
        reply = ask_gemma(system, prompt, rid)
        if kind == 'ask':
            set_state(rid, status='answered', reply=reply[:4000], finished_at=time.time())
            telegram(f'ops:{rid}', f'🛠 Gemma 도우미 답변\n질문: {request["prompt"][:80]}\n\n{reply[:1200]}\n\n{PUBLIC}/ops-assistant?id={rid}')
            impl._progress_done('Gemma 도우미 답변 완료')
            return dict(ok=True, status='answered')
        proposal = build_proposal(rid, rel, original, reply)
        if proposal['status'] == 'failed' and proposal['edits'] and not proposal['diff']:
            # Mismatch: show the model where the text really is, once.
            impl._progress_update(message='패치를 다시 맞추는 중')
            hint = mismatch_hint(original, proposal['edits'])
            reply = ask_gemma(system, prompt + '\n\n이전 답변의 SEARCH가 파일과 정확히 일치하지 않았어요. 실제 파일의 해당 부분:\n' + hint +
                              '\n\n이 텍스트를 그대로 복사해서 다시 답해 주세요.', rid)
            proposal = build_proposal(rid, rel, original, reply)
        proposal['finished_at'] = time.time()
        set_state(rid, **proposal)
        label = {'proposed': '수정안이 왔어요 (승인 필요)', 'answered': '답변이 왔어요', 'invalid': '검사에 걸려 적용 못 했어요',
                 'failed': '수정안을 만들지 못했어요'}.get(proposal['status'], proposal['status'])
        telegram(f'ops:{rid}', f'🛠 {label}\n{request["prompt"][:80]}\n{PUBLIC}/ops-assistant?id={rid}')
        impl._progress_done('Gemma 도우미 완료')
        return dict(ok=True, status=proposal['status'])
    except BaseException as error:
        cancelled = type(error).__name__ == 'GenerationCancelled'
        set_state(rid, status='failed', error='취소했어요' if cancelled else f'{type(error).__name__}: {str(error)[:300]}',
                  finished_at=time.time())
        if not cancelled:
            telegram(f'ops:{rid}', f'🛠 Gemma 도우미 실패: {str(error)[:200]}')
        raise


def mismatch_hint(original, edits):
    lines = original.split('\n')
    out = []
    for edit in edits[:3]:
        first = next((l for l in strip_gutter(edit['search']).split('\n') if l.strip()), '')
        close = difflib.get_close_matches(first.strip(), [l.strip() for l in lines], n=1, cutoff=0.5)
        if not close:
            continue
        index = next(i for i, l in enumerate(lines) if l.strip() == close[0])
        out.append('\n'.join(lines[max(0, index - 3):index + 8]))
    return ('\n-----\n'.join(out) or '(비슷한 줄을 찾지 못했어요)')[:2500]


# ---------------------------------------------------------------- restart under a watchdog
def restart_status():
    path = DATA / 'restart.json'
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def pending_restart_ids():
    with db() as conn:
        return [r['id'] for r in conn.execute(
            "SELECT id FROM requests WHERE status IN ('applied','undone') AND restart_needed=1 ORDER BY COALESCE(applied_at,created)")]


def start_restart(confirm_busy):
    ids = pending_restart_ids()
    if not ids:
        raise ValueError('재시작할 변경이 없어요.')
    state = restart_status()
    if state.get('state') == 'running' and time.time() - state.get('at', 0) < 400:
        raise ValueError('이미 재시작 중이에요.')
    with impl._JOB_QUEUE_LOCK:
        busy = getattr(impl, '_ACTIVE_QUEUE_JOB_ID', None)
    if busy and not confirm_busy:
        raise PermissionError('지금 영상 생성이 돌고 있어요. 재시작하면 진행 중인 구간 연산이 처음부터 다시 돼요(끝난 구간은 보존). 그래도 하려면 확인하세요.')
    restore = []
    for rid in ids:
        request = get_request(rid)
        if request['status'] != 'applied':
            continue                       # an undone change only needs the restart; there is nothing to roll back to
        backup = backup_dir(rid) / request['target']
        if not backup.exists():
            raise ValueError('백업이 없는 변경이 있어서 안전하게 재시작할 수 없어요.')
        restore.append([request['target'], str(backup), rid])
    job = dict(base=str(BASE), db=str(DB_PATH), service=SERVICE, health=HEALTH_URL, restore=restore, ids=ids,
               status_path=str(DATA / 'restart.json'), public=PUBLIC)
    job_path = DATA / f'restart-job-{int(time.time())}.json'
    job_path.write_text(json.dumps(job, ensure_ascii=False))
    (DATA / 'restart.json').write_text(json.dumps(dict(state='running', at=time.time(), ids=ids)))
    uid = os.getuid()
    env = dict(os.environ, XDG_RUNTIME_DIR=f'/run/user/{uid}', DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{uid}/bus')
    subprocess.run(['systemd-run', '--user', '--collector', f'--unit=ops-guard-{int(time.time())}', PYTHON, str(GUARD), str(job_path)],
                   env=env, check=True, capture_output=True, timeout=30)
    return ids


# ---------------------------------------------------------------- HTTP
def guard(request: Request):
    """State-changing calls must come from our own pages: a custom header (cross-site forms cannot send it) and a matching Origin."""
    if request.headers.get('x-ops-assistant') != '1':
        raise HTTPException(403, '허용되지 않은 요청이에요.')
    origin = request.headers.get('origin')
    if origin:
        host = request.headers.get('x-forwarded-host') or request.headers.get('host') or ''
        if origin.split('://', 1)[-1] != host:
            raise HTTPException(403, '다른 사이트에서 보낸 요청이에요.')


def configure(app, runtime, queue):
    global impl
    impl = runtime
    import scail_repair as rep

    @app.get('/api/ops/targets')
    def targets():
        return dict(items=[dict(path=p, label=v[0], tier=v[1]) for p, v in TARGETS.items()])

    @app.get('/api/ops/suggest')
    def suggest(prompt: str = ''):
        return dict(target=suggest_target(prompt))

    @app.get('/api/ops/requests')
    def listing(limit: int = 30):
        with db() as conn:
            rows = conn.execute('SELECT * FROM requests ORDER BY created DESC LIMIT ?', (max(1, min(limit, 100)),)).fetchall()
        return dict(items=[row_dict(r, full=False) for r in rows], pending_restart=pending_restart_ids(), restart=restart_status())

    @app.get('/api/ops/requests/{rid}')
    def one(rid: str):
        request = get_request(rid) if re.fullmatch(r'[a-f0-9]{32}', rid) else None
        if not request:
            raise HTTPException(404, '요청을 찾을 수 없어요.')
        return request

    @app.post('/api/ops/requests')
    def create(request: Request, prompt: str = Form(...), kind: str = Form('ask'), target: str = Form(''),
               include_logs: bool = Form(True), preempt: bool = Form(False)):
        guard(request)
        prompt = prompt.strip()
        if not 4 <= len(prompt) <= 1500:
            raise HTTPException(422, '요청은 4~1500자로 적어 주세요.')
        if kind not in ('ask', 'fix'):
            raise HTTPException(422, '종류가 올바르지 않아요.')
        target = target or suggest_target(prompt)
        if target not in TARGETS:
            raise HTTPException(422, '이 파일은 도우미가 다룰 수 없어요.')
        with db() as conn:
            waiting = conn.execute("SELECT COUNT(*) FROM requests WHERE status IN ('queued','running')").fetchone()[0]
        if waiting >= 3:
            raise HTTPException(429, '앞선 요청이 아직 처리 중이에요. 잠시 뒤에 다시 보내 주세요.')
        rid = uuid.uuid4().hex
        with db() as conn:
            conn.execute('INSERT INTO requests(id,created,kind,prompt,target,status,include_logs,tier) VALUES(?,?,?,?,?,?,?,?)',
                         (rid, time.time(), kind, prompt, target, 'queued', int(include_logs), TARGETS[target][1]))
        title = f'Gemma 도우미 · {"질문" if kind == "ask" else "수정 요청"} · {prompt[:30]}'
        try:
            result = queue.enqueue(run, (rid,), {}, kind=title, identity='opsasst_' + rid)
        except Exception as error:
            set_state(rid, status='failed', error=str(error)[:300])
            raise HTTPException(500, f'큐에 넣지 못했어요: {error}')
        rep._prioritise(runtime, queue, 'opsasst_' + rid)
        cancelled = rep._preempt_active(runtime, queue, 'opsasst_' + rid) if preempt else None
        return dict(result, id=rid, target=target, preempted=cancelled)

    @app.post('/api/ops/requests/{rid}/apply')
    def apply(request: Request, rid: str):
        guard(request)
        try:
            return apply_request(rid)
        except (ValueError, KeyError) as error:
            raise HTTPException(409, str(error))

    @app.post('/api/ops/requests/{rid}/reject')
    def reject(request: Request, rid: str):
        guard(request)
        current = get_request(rid)
        if not current or current['status'] not in ('proposed', 'answered', 'invalid', 'failed', 'stale'):
            raise HTTPException(409, '거절할 수 있는 상태가 아니에요.')
        set_state(rid, status='rejected')
        return get_request(rid)

    @app.post('/api/ops/requests/{rid}/undo')
    def undo(request: Request, rid: str, force: bool = Form(False)):
        guard(request)
        try:
            return undo_request(rid, force)
        except PermissionError as error:
            raise HTTPException(409, str(error))
        except ValueError as error:
            raise HTTPException(409, str(error))

    @app.post('/api/ops/restart')
    def restart(request: Request, confirm_busy: bool = Form(False)):
        guard(request)
        try:
            return dict(ok=True, ids=start_restart(confirm_busy))
        except PermissionError as error:
            raise HTTPException(409, str(error))
        except ValueError as error:
            raise HTTPException(409, str(error))
        except (subprocess.SubprocessError, OSError) as error:
            raise HTTPException(500, f'재시작 감시를 시작하지 못했어요: {error}')
