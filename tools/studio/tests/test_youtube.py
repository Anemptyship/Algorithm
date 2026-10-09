"""Offline test of the YouTube reconnect handling. The module is copied into a temp dir, so its config, DB, key and the
Telegram outbox it writes to are all temporary. httpx is replaced by a fake: nothing is sent to Google.
usage: python test_youtube.py <youtube_api.py>"""
import importlib.util
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SERVER = Path('/home/ben/zit-22-2716')
sys.path.insert(0, str(SERVER))
ok = 0


def check(name, cond, extra=''):
    global ok
    print(('PASS ' if cond else 'FAIL ') + name + (f'  :: {extra}' if not cond and extra != '' else ''))
    if not cond:
        raise SystemExit(1)
    ok += 1


class Resp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload
        self.is_error = status >= 400
        self.text = json.dumps(payload)
        self.headers = {}

    def json(self):
        return self._p


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    shutil.copy2(sys.argv[1], tmp / 'youtube_api.py')
    (tmp / 'telegram_data').mkdir()
    tg = sqlite3.connect(tmp / 'telegram_data/bridge.sqlite3')
    tg.executescript("create table config(key text primary key, value text);"
                     "create table outbox(id text primary key, created real, method text, payload text, attachment text, state text default 'pending');"
                     "insert into config values('owner','12345');")
    tg.commit()
    spec = importlib.util.spec_from_file_location('youtube_api_under_test', tmp / 'youtube_api.py')
    yt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(yt)
    check('module paths are inside the temp dir', str(yt.CONFIG_PATH).startswith(str(tmp)), yt.CONFIG_PATH)

    def outbox():
        with sqlite3.connect(tmp / 'telegram_data/bridge.sqlite3') as db:
            return [(r[0], json.loads(r[1])['text']) for r in db.execute('select id,payload from outbox order by created')]

    yt._init_db()
    yt._save_config({'client_id': 'cid', 'client_secret': 'x' * 30, 'refresh_token': 'old-refresh', 'channel_title': 'Yuna'})

    # ---- error text keeps Google's description ----
    r = Resp(400, {'error': 'invalid_grant', 'error_description': 'Token has been expired or revoked.'})
    check('description is no longer thrown away', yt._google_error(r) == 'invalid_grant: Token has been expired or revoked.', yt._google_error(r))
    check('dict-style errors unchanged', yt._google_error(Resp(403, {'error': {'message': 'quota', 'errors': [{'reason': 'quotaExceeded'}]}})) == 'quota (quotaExceeded)')
    check('plain error string unchanged', yt._google_error(Resp(400, {'error': 'invalid_request'})) == 'invalid_request')

    # ---- dead refresh token ----
    calls = []

    def fake_post(url, **kw):
        calls.append(url)
        return Resp(400, {'error': 'invalid_grant', 'error_description': 'Token has been expired or revoked.'})

    yt.httpx.post = fake_post
    try:
        yt._access_token()
        check('invalid_grant raises ReauthRequired', False)
    except yt.ReauthRequired as e:
        check('invalid_grant raises ReauthRequired with the link and reason', 'invalid_grant' in str(e) and yt.REAUTH_URL in str(e) and str(e).startswith('재연결 필요'), str(e))
    check('flag set in the stored config', yt._load_config().get('reauth_required') is True)
    check('public status exposes it without secrets',
          yt._public_config()['reauth_required'] and 'token_expires_at' not in yt._public_config()
          and 'refresh' not in json.dumps(yt._public_config()).lower().replace('reauth', ''))
    box = outbox()
    check('exactly one Telegram message, with the link', len(box) == 1 and yt.REAUTH_URL in box[0][1] and '프로덕션' in box[0][1], box)
    for _ in range(3):
        try:
            yt._access_token()
        except yt.ReauthRequired:
            pass
    check('repeated failures do not spam Telegram', len(outbox()) == 1, outbox())
    check('worker is told not to upload while the flag is set', yt._reauth_pending())

    # ---- jobs: failed because of the dead token vs. unrelated ----
    def add_job(job_id, status, error, scheduled, immediate=0, has_file=True):
        (yt.MEDIA_ROOT / job_id).mkdir(parents=True, exist_ok=True)
        if has_file:
            (yt.MEDIA_ROOT / job_id / 'v.mp4').write_bytes(b'x')
        now = yt._iso(yt._utc_now())
        with yt._db_lock, yt._connect() as db:
            cols = [r[1] for r in db.execute('pragma table_info(jobs)')]
            values = dict(id=job_id, title='t', description='d', tags='[]', filename='v.mp4', source_filename='s.mp4', status=status,
                          last_error=error, scheduled_at=scheduled, publish_immediately=immediate, created_at=now, updated_at=now,
                          next_attempt_at=now, upload_attempts=3, preprocess_attempts=0, notify_subscribers=0, made_for_kids=0,
                          contains_synthetic_media=0, content_hash=job_id, fingerprint=job_id, reservation_key=job_id,
                          metadata_language='ko', translation_state='', translation_attempts=0)
            use = {k: v for k, v in values.items() if k in cols}
            missing = [r[1] for r in db.execute('pragma table_info(jobs)') if r[3] and r[4] is None and r[1] not in use and not r[5]]
            for m in missing:
                use[m] = ''
            db.execute(f"insert into jobs({','.join(use)}) values({','.join('?' * len(use))})", list(use.values()))

    soon = yt._iso(yt._utc_now() + timedelta(hours=3))
    past = yt._iso(yt._utc_now() - timedelta(hours=1))
    add_job('j-future', 'failed', '업로드 3/3회 실패: Google 토큰 갱신 실패: invalid_grant', soon)
    add_job('j-parked', 'upload_retry', '재연결 필요: x', soon)
    add_job('j-past', 'failed', '업로드 3/3회 실패: Google 토큰 갱신 실패: invalid_grant', past)
    add_job('j-immediate', 'failed', '업로드 3/3회 실패: Google 토큰 갱신 실패: invalid_grant', past, immediate=1)
    add_job('j-other', 'failed', '업로드 3/3회 실패: 영상 형식 오류', soon)
    add_job('j-nofile', 'failed', '재연결 필요: x', soon, has_file=False)
    resumed, skipped = yt._retry_after_reauth()
    status = {r['id']: (r['status'], r['last_error']) for r in yt._connect().execute('select id,status,last_error from jobs')}
    check('future-scheduled jobs go back to ready', status['j-future'][0] == 'ready' and status['j-parked'][0] == 'ready', status)
    check('immediate-publish job is resumed even though its time is in the past', status['j-immediate'][0] == 'ready')
    check('job without its processed file goes back to preprocessing', status['j-nofile'][0] == 'preprocessing')
    check('job whose time passed is parked as failed with a clear instruction',
          status['j-past'][0] == 'failed' and '예약 시간이 이미 지났어요' in status['j-past'][1], status['j-past'])
    check('unrelated failures are untouched', status['j-other'] == ('failed', '업로드 3/3회 실패: 영상 형식 오류'))
    check('counts returned', (resumed, skipped) == (4, 1), (resumed, skipped))
    check('attempt counters reset on resumed jobs',
          all(r[0] == 0 for r in yt._connect().execute("select upload_attempts from jobs where id in ('j-future','j-parked')")))

    # ---- a working refresh clears the flag again ----
    def good_post(url, **kw):
        return Resp(200, {'access_token': 'new-access', 'expires_in': 3600})

    yt.httpx.post = good_post
    check('refresh works again', yt._access_token() == 'new-access')
    check('flag cleared by a successful refresh', not yt._reauth_pending() and not yt._public_config()['reauth_required'])
    check('reason hidden once cleared', yt._public_config()['reauth_reason'] == '')

    # ---- the next outage notifies again (new episode) ----
    yt._save_config({'access_token': '', 'token_expires_at': ''})
    yt.httpx.post = fake_post
    time.sleep(1.1)
    try:
        yt._access_token()
    except yt.ReauthRequired:
        pass
    check('a new outage sends a new notification', len(outbox()) == 2, outbox())

    # ---- other Google failures are not turned into a reconnect request ----
    yt._save_config({'reauth_required': False, 'access_token': '', 'token_expires_at': ''})
    yt.httpx.post = lambda url, **kw: Resp(500, {'error': {'message': 'backend error'}})
    try:
        yt._access_token()
        check('500 raises', False)
    except yt.ReauthRequired:
        check('a Google 500 is not treated as reconnect-needed', False)
    except RuntimeError as e:
        check('a Google 500 stays an ordinary error and does not set the flag', '토큰 갱신 실패' in str(e) and not yt._reauth_pending(), str(e))
    check('no extra Telegram message for it', len(outbox()) == 2)

print(f'\nALL {ok} CHECKS PASSED')
