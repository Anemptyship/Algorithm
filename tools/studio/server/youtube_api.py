"""YouTube Shorts preprocessing, resumable upload, and scheduled publishing queue."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
from sqlite_runtime import ClosingConnection
import subprocess
import threading
import time
import uuid
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
import httpx
from media_runtime import video_preprocess_lock
from publishing_priority import priority
import caption_translation
import publish_timing
import media_thumbnails


router = APIRouter(prefix="/api/youtube")
ROOT = Path(__file__).resolve().parent / "youtube_data"
MEDIA_ROOT = ROOT / "media"
DB_PATH = ROOT / "jobs.sqlite3"
CONFIG_PATH = ROOT / "config.json"
KEY_PATH = ROOT / ".token-key"
KST = ZoneInfo("Asia/Seoul")
REDIRECT_URI = "https://yunalee.shop/api/youtube/oauth/callback"
SCOPE = "https://www.googleapis.com/auth/youtube"
WATERMARK_PYTHON = "/home/ben/ComfyUI/.venv/bin/python"
VIDEO_WATERMARK_WORKER = Path(__file__).resolve().parent / "watermark_video_worker.py"
MAX_VIDEO_BYTES = 8 * 1024**3
_db_lock = threading.RLock()
_worker_started = False
_last_remote_sync = 0.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    MEDIA_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def _init_db() -> None:
    with _db_lock, _connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '[]',
                scheduled_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                source_filename TEXT NOT NULL,
                filename TEXT NOT NULL,
                preprocess_attempts INTEGER NOT NULL DEFAULT 0,
                upload_attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                upload_bytes INTEGER NOT NULL DEFAULT 0,
                upload_total INTEGER NOT NULL DEFAULT 0,
                youtube_video_id TEXT,
                notify_subscribers INTEGER NOT NULL DEFAULT 0,
                made_for_kids INTEGER NOT NULL DEFAULT 0,
                contains_synthetic_media INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                content_hash TEXT,
                fingerprint TEXT,
                reservation_key TEXT
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS youtube_jobs_queue ON jobs(status, next_attempt_at)")
        columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
        for column in ("content_hash", "fingerprint", "reservation_key"):
            if column not in columns:
                db.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
        additions = {
            "publish_immediately": "INTEGER NOT NULL DEFAULT 0",
            "metadata_language": "TEXT NOT NULL DEFAULT 'original'",
            "title_original": "TEXT NOT NULL DEFAULT ''",
            "description_original": "TEXT NOT NULL DEFAULT ''",
            "translation_state": "TEXT NOT NULL DEFAULT ''",
            "translation_attempts": "INTEGER NOT NULL DEFAULT 0",
            "translation_next_at": "TEXT NOT NULL DEFAULT ''",
            "translation_error": "TEXT",
            "output_width": "INTEGER NOT NULL DEFAULT 1080",
            "output_height": "INTEGER NOT NULL DEFAULT 1920",
        }
        for column, definition in additions.items():
            if column not in columns:
                db.execute(f"ALTER TABLE jobs ADD COLUMN {column} {definition}")
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS youtube_active_reservation_key "
            "ON jobs(reservation_key) WHERE reservation_key IS NOT NULL AND status != 'cancelled'"
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS youtube_active_fingerprint "
            "ON jobs(fingerprint) WHERE fingerprint IS NOT NULL AND status != 'cancelled'"
        )


def _fernet() -> Fernet:
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not KEY_PATH.exists():
        temporary = KEY_PATH.with_suffix(".tmp")
        temporary.write_bytes(Fernet.generate_key())
        os.chmod(temporary, 0o600)
        temporary.replace(KEY_PATH)
    return Fernet(KEY_PATH.read_bytes().strip())


def _load_config(include_secrets: bool = False) -> dict:
    if not CONFIG_PATH.exists():
        return {}
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if include_secrets:
        for name in ("client_secret", "access_token", "refresh_token"):
            encrypted = data.pop(f"{name}_encrypted", "")
            if encrypted:
                data[name] = _fernet().decrypt(encrypted.encode()).decode()
    return data


def _save_config(data: dict) -> None:
    previous = _load_config(include_secrets=True)
    merged = {**previous, **data}
    for name in ("client_secret", "access_token", "refresh_token"):
        value = merged.pop(name, "")
        merged.pop(f"{name}_encrypted", None)
        if value:
            merged[f"{name}_encrypted"] = _fernet().encrypt(value.encode()).decode()
    temporary = CONFIG_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(CONFIG_PATH)


def _public_config() -> dict:
    config = _load_config()
    secret_valid = False
    if config.get("client_secret_encrypted"):
        try:
            secret_valid = len(_load_config(include_secrets=True).get("client_secret", "")) >= 20
        except Exception:
            secret_valid = False
    return {
        "oauth_ready": bool(config.get("client_id") and secret_valid),
        "credentials_warning": "저장된 OAuth 클라이언트 보안 비밀번호가 올바르지 않습니다. Google Cloud의 같은 웹 클라이언트에서 다시 복사해 저장하세요." if config.get("client_secret_encrypted") and not secret_valid else "",
        "configured": bool(config.get("refresh_token_encrypted")),
        "reauth_required": bool(config.get("reauth_required")),
        "reauth_reason": config.get("reauth_reason", "") if config.get("reauth_required") else "",
        "reauth_url": REAUTH_URL,
        "client_id": config.get("client_id", ""),
        "channel_id": config.get("channel_id", ""),
        "channel_title": config.get("channel_title", ""),
        "channel_thumbnail_url": config.get("channel_thumbnail_url", ""),
        "redirect_uri": REDIRECT_URI,
        "timezone": "Asia/Seoul",
        "worker": _worker_started,
    }


def _parse_schedule(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise HTTPException(400, "예약 시간이 올바르지 않습니다.") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    parsed = parsed.astimezone(timezone.utc)
    if parsed <= _utc_now() + timedelta(minutes=2):
        raise HTTPException(400, "YouTube 처리 시간을 위해 현재보다 최소 2분 뒤로 예약해주세요.")
    return parsed


async def _save_upload(upload: UploadFile, destination: Path) -> str:
    size = 0
    digest = hashlib.sha256()
    with destination.open("wb") as output:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_VIDEO_BYTES:
                raise HTTPException(413, "영상은 최대 8GB까지 업로드할 수 있습니다.")
            output.write(chunk)
            digest.update(chunk)
    if not size:
        raise HTTPException(400, "빈 영상 파일입니다.")
    return digest.hexdigest()


def _job_fingerprint(content_hash: str, title: str, description: str, tags: list[str], scheduled_at: str,
                     notify_subscribers: bool, made_for_kids: bool, contains_synthetic_media: bool,
                     metadata_language: str = "original") -> str:
    payload = {
        "content_hash": content_hash, "title": title, "description": description, "tags": tags,
        "scheduled_at": scheduled_at, "notify_subscribers": bool(notify_subscribers),
        "made_for_kids": bool(made_for_kids), "contains_synthetic_media": bool(contains_synthetic_media),
        "metadata_language": metadata_language,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _safe_name(name: str, suffix: str) -> str:
    stem = re.sub(r"[^\w.가-힣-]", "_", Path(name).stem)[:80] or "short"
    return f"{stem}{suffix}"


def _row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item.pop("content_hash", None)
    item.pop("fingerprint", None)
    item.pop("reservation_key", None)
    item["tags"] = json.loads(item.get("tags") or "[]")
    for key in ("notify_subscribers", "made_for_kids", "contains_synthetic_media"):
        item[key] = bool(item[key])
    item["translation_label"] = {
        "pending": "일본어 번역 대기", "working": "일본어 번역 중", "done": "일본어 번역 완료",
        "failed": "일본어 번역 실패",
    }.get(item.get("translation_state", ""), "원문 유지")
    item["scheduled_kst"] = datetime.fromisoformat(item["scheduled_at"]).astimezone(KST).isoformat(timespec="minutes")
    progress = MEDIA_ROOT / item["id"] / ".watermark-progress.json"
    if progress.is_file():
        try:
            item["watermark_progress"] = json.loads(progress.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return item


def _google_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
        error = payload.get("error", payload)
        if isinstance(error, str) and payload.get("error_description"):
            return f"{error}: {payload['error_description']}"[:1200]
        if isinstance(error, dict):
            message = error.get("message") or error.get("error_description")
            details = error.get("errors") or []
            reason = details[0].get("reason") if details and isinstance(details[0], dict) else ""
            return f"{message or error}{f' ({reason})' if reason else ''}"[:1200]
        return str(error)[:1200]
    except Exception:
        return (response.text or f"Google API HTTP {response.status_code}")[:1200]


class ReauthRequired(RuntimeError):
    """Google rejected the stored refresh token. Retrying cannot help: the owner must connect the account again."""


REAUTH_URL = "https://yunalee.shop/api/youtube/oauth/start"


def _telegram(key: str, text: str) -> None:
    """One text message to the owner through the Telegram bridge outbox. Never raises."""
    try:
        path = Path(__file__).resolve().parent / "telegram_data" / "bridge.sqlite3"
        with sqlite3.connect(path, timeout=20) as db:
            owner = db.execute("SELECT value FROM config WHERE key='owner'").fetchone()
            if not owner:
                return
            db.execute("INSERT OR IGNORE INTO outbox(id,created,method,payload,attachment) VALUES(?,?,?,?,?)",
                       (key, time.time(), "sendMessage",
                        json.dumps({"chat_id": int(owner[0]), "text": text}, ensure_ascii=False), None))
    except Exception:
        pass


def _reauth_pending() -> bool:
    try:
        return bool(_load_config().get("reauth_required"))
    except Exception:
        return False


def _mark_reauth(detail: str) -> None:
    first = not _reauth_pending()
    _save_config({"reauth_required": True, "reauth_reason": detail[:300], "reauth_at": _iso(_utc_now())})
    if first:
        _telegram("youtube-reauth:" + _iso(_utc_now()),
                  "YouTube 연결이 끊겼어요 (" + detail[:120] + ")\n"
                  "예약 업로드는 다시 연결하기 전까지 멈춰 있어요. 아래 링크로 Google 계정을 다시 연결해 주세요.\n"
                  + REAUTH_URL + "\n\n"
                  "다시 연결되면 아직 예약 시간이 남은 작업은 자동으로 이어서 올라가요.\n"
                  "7일마다 반복되면 Google Cloud의 OAuth 동의 화면이 '테스트' 상태일 가능성이 커요. '프로덕션'으로 게시하면 해결돼요.")


def _retry_after_reauth() -> tuple[int, int]:
    """After a successful reconnect, put back the uploads that only failed because of the dead token.
    Jobs whose scheduled time has already passed are left for the owner (YouTube cannot schedule the past)."""
    now = _utc_now()
    resumed = skipped = 0
    with _db_lock, _connect() as db:
        rows = db.execute("SELECT * FROM jobs WHERE status IN ('failed','upload_retry') AND "
                          "(last_error LIKE '%invalid_grant%' OR last_error LIKE '%재연결 필요%')").fetchall()
        for row in rows:
            try:
                due = datetime.fromisoformat(row["scheduled_at"]) if row["scheduled_at"] else None
            except ValueError:
                due = None
            if due is not None and due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            if not row["publish_immediately"] and (due is None or due <= now + timedelta(minutes=3)):
                db.execute("UPDATE jobs SET status='failed',last_error=?,updated_at=? WHERE id=?",
                           ("재연결은 됐지만 예약 시간이 이미 지났어요. 예약 시간을 새로 정하거나 즉시 게시로 바꾼 뒤 '다시 시도'를 눌러 주세요.",
                            _iso(now), row["id"]))
                skipped += 1
                continue
            processed = (MEDIA_ROOT / row["id"] / row["filename"]).is_file()
            db.execute("UPDATE jobs SET status=?,preprocess_attempts=0,upload_attempts=0,next_attempt_at=?,last_error=NULL,"
                       "updated_at=? WHERE id=?",
                       ("ready" if processed else "preprocessing", _iso(now), _iso(now), row["id"]))
            resumed += 1
    return resumed, skipped


def _access_token() -> str:
    config = _load_config(include_secrets=True)
    if not config.get("refresh_token"):
        raise RuntimeError("YouTube 계정 연결이 필요합니다.")
    expires = datetime.fromisoformat(config["token_expires_at"]) if config.get("token_expires_at") else datetime.min.replace(tzinfo=timezone.utc)
    if config.get("access_token") and expires > _utc_now() + timedelta(minutes=5):
        return config["access_token"]
    response = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": config["client_id"], "client_secret": config["client_secret"],
            "refresh_token": config["refresh_token"], "grant_type": "refresh_token",
        },
        timeout=60,
    )
    if response.is_error:
        detail = _google_error(response)
        if "invalid_grant" in detail:
            _mark_reauth(detail)
            raise ReauthRequired(f"재연결 필요: Google이 저장된 로그인 권한을 거절했어요 ({detail}). {REAUTH_URL}")
        raise RuntimeError(f"Google 토큰 갱신 실패: {detail}")
    payload = response.json()
    _save_config({
        "reauth_required": False,
        "access_token": payload["access_token"],
        "token_expires_at": _iso(_utc_now() + timedelta(seconds=int(payload.get("expires_in", 3600)))),
    })
    return payload["access_token"]


def _video_resource(job: dict) -> dict:
    resource = {
        "snippet": {
            "title": job["title"], "description": job["description"],
            "tags": json.loads(job["tags"]) if isinstance(job["tags"], str) else job["tags"],
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": job["scheduled_at"],
            "selfDeclaredMadeForKids": bool(job["made_for_kids"]),
            "containsSyntheticMedia": bool(job["contains_synthetic_media"]),
        },
    }
    if job.get('publish_immediately'):
        resource['status']['privacyStatus'] = 'public'
        resource['status'].pop('publishAt', None)
    return resource


def _set_upload_progress(job_id: str, uploaded: int, total: int) -> None:
    with _db_lock, _connect() as db:
        db.execute(
            "UPDATE jobs SET upload_bytes=?, upload_total=?, updated_at=? WHERE id=? AND status='uploading'",
            (uploaded, total, _iso(_utc_now()), job_id),
        )


def _upload_job(job: dict) -> str:
    path = MEDIA_ROOT / job["id"] / job["filename"]
    total = path.stat().st_size
    token = _access_token()
    with httpx.Client(timeout=httpx.Timeout(120, read=600), follow_redirects=False) as client:
        response = client.post(
            "https://www.googleapis.com/upload/youtube/v3/videos",
            params={
                "uploadType": "resumable", "part": "snippet,status",
                "notifySubscribers": "true" if job["notify_subscribers"] else "false",
            },
            headers={
                "Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(total), "X-Upload-Content-Type": "video/mp4",
            },
            json=_video_resource(job),
        )
        if response.is_error:
            raise RuntimeError(f"YouTube 업로드 시작 실패: {_google_error(response)}")
        upload_url = response.headers.get("Location")
        if not upload_url:
            raise RuntimeError("YouTube가 재개 가능한 업로드 주소를 반환하지 않았습니다.")
        chunk_size = 8 * 1024 * 1024
        uploaded = 0
        with path.open("rb") as source:
            while uploaded < total:
                source.seek(uploaded)
                data = source.read(min(chunk_size, total - uploaded))
                end = uploaded + len(data) - 1
                for attempt in range(4):
                    response = client.put(
                        upload_url,
                        headers={
                            "Content-Length": str(len(data)), "Content-Type": "video/mp4",
                            "Content-Range": f"bytes {uploaded}-{end}/{total}",
                        },
                        content=data,
                    )
                    if response.status_code in {200, 201}:
                        payload = response.json()
                        video_id = str(payload.get("id", ""))
                        if not video_id:
                            raise RuntimeError("YouTube가 영상 ID를 반환하지 않았습니다.")
                        _set_upload_progress(job["id"], total, total)
                        return video_id
                    if response.status_code == 308:
                        server_end = response.headers.get("Range", "").split("-")[-1]
                        uploaded = int(server_end) + 1 if server_end.isdigit() else end + 1
                        _set_upload_progress(job["id"], uploaded, total)
                        break
                    if response.status_code in {500, 502, 503, 504} and attempt < 3:
                        time.sleep(2 ** attempt)
                        continue
                    raise RuntimeError(f"YouTube 영상 업로드 실패: {_google_error(response)}")
                else:
                    raise RuntimeError("YouTube 업로드 재시도 한도를 초과했습니다.")
        raise RuntimeError("YouTube 업로드가 완료되지 않았습니다.")


def _update_remote(job: dict) -> None:
    if not job.get("youtube_video_id"):
        return
    resource = _video_resource(job)
    resource["id"] = job["youtube_video_id"]
    response = httpx.put(
        "https://www.googleapis.com/youtube/v3/videos",
        params={"part": "snippet,status"},
        headers={"Authorization": f"Bearer {_access_token()}"},
        json=resource,
        timeout=90,
    )
    if response.is_error:
        raise RuntimeError(f"YouTube 예약 수정 실패: {_google_error(response)}")


def _delete_remote(video_id: str) -> None:
    response = httpx.delete(
        "https://www.googleapis.com/youtube/v3/videos",
        params={"id": video_id}, headers={"Authorization": f"Bearer {_access_token()}"}, timeout=90,
    )
    if response.status_code not in {204, 404}:
        raise RuntimeError(f"YouTube 영상 취소 실패: {_google_error(response)}")


def _sync_published_jobs() -> None:
    """Mark past scheduled uploads public once YouTube reports them public."""
    with _db_lock, _connect() as db:
        rows = db.execute(
            "SELECT id,youtube_video_id FROM jobs WHERE status='scheduled' AND scheduled_at<=? AND youtube_video_id IS NOT NULL LIMIT 50",
            (_iso(_utc_now()),),
        ).fetchall()
    if not rows:
        return
    response = httpx.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={"part": "status", "id": ",".join(row["youtube_video_id"] for row in rows)},
        headers={"Authorization": f"Bearer {_access_token()}"}, timeout=60,
    )
    if response.is_error:
        raise RuntimeError(f"YouTube 게시 상태 확인 실패: {_google_error(response)}")
    public_ids = {item["id"] for item in response.json().get("items", []) if item.get("status", {}).get("privacyStatus") == "public"}
    if public_ids:
        now = _iso(_utc_now())
        with _db_lock, _connect() as db:
            db.executemany("UPDATE jobs SET status='published',updated_at=? WHERE youtube_video_id=?", [(now, video_id) for video_id in public_ids])


def _claim(statuses: tuple[str, ...], active: str, extra_where: str = "1=1") -> dict | None:
    placeholders = ",".join("?" for _ in statuses)
    now = _iso(_utc_now())
    with _db_lock, _connect() as db:
        row = db.execute(
            f"SELECT * FROM jobs WHERE status IN ({placeholders}) AND next_attempt_at<=? AND ({extra_where}) ORDER BY created_at LIMIT 1",
            (*statuses, now),
        ).fetchone()
        if not row:
            return None
        changed = db.execute(
            f"UPDATE jobs SET status=?, updated_at=? WHERE id=? AND status IN ({placeholders})",
            (active, now, row["id"], *statuses),
        ).rowcount
        return dict(row) if changed else None


def _preprocess(job: dict) -> None:
    job_dir = MEDIA_ROOT / job["id"]
    source = job_dir / "source" / job["source_filename"]
    output = job_dir / job["filename"]
    progress = job_dir / ".watermark-progress.json"
    environment = os.environ.copy()
    environment["CUDA_VISIBLE_DEVICES"] = ""
    try:
        # Claim publishing priority before waiting for the shared GPU. A video
        # generation already running may finish, but no later generation may
        # overtake this scheduled upload.
        with priority.publishing("YouTube 게시 전처리"), video_preprocess_lock:
            result = subprocess.run(
                [WATERMARK_PYTHON, str(VIDEO_WATERMARK_WORKER), str(source), str(output), str(progress), "1080x1920"],
                env=environment, capture_output=True, timeout=3 * 3600,
            )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("워터마크 제거 처리 시간이 3시간을 초과했습니다.") from error
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace")[-1200:] or "영상 전처리 실패")
    shutil.rmtree(job_dir / "source", ignore_errors=True)
    progress.unlink(missing_ok=True)


def _claim_translation() -> dict | None:
    now = _iso(_utc_now())
    with _db_lock, _connect() as db:
        row = db.execute(
            "SELECT * FROM jobs WHERE translation_state='pending' AND translation_next_at<=? "
            "AND status IN ('ready','upload_retry','scheduled') ORDER BY created_at LIMIT 1", (now,),
        ).fetchone()
        if not row:
            return None
        changed = db.execute(
            "UPDATE jobs SET translation_state='working',updated_at=? WHERE id=? AND translation_state='pending'",
            (now, row['id']),
        ).rowcount
        return dict(row) if changed else None


def _translate_job(job: dict) -> None:
    fields = {"title": job["title_original"] or job["title"], "description": job["description_original"] or job["description"]}
    candidates = {key: value for key, value in fields.items() if caption_translation.needs(value, "ja")}
    translated = caption_translation.translate_fields(candidates, "ja", {"title": 100, "description": 5000})
    translated = {key: translated.get(key, value) for key, value in fields.items()}
    updated = dict(job)
    updated.update(translated)
    if job.get("youtube_video_id"):
        _update_remote(updated)
    with _db_lock, _connect() as db:
        db.execute(
            "UPDATE jobs SET title=?,description=?,translation_state='done',translation_attempts=0,"
            "translation_error=NULL,updated_at=? WHERE id=? AND translation_state='working'",
            (translated["title"], translated["description"], _iso(_utc_now()), job["id"]),
        )


def _queue_loop() -> None:
    global _last_remote_sync
    while True:
        job = None
        try:
            # Finish each prepared video all the way into YouTube's native
            # scheduler before spending time preprocessing the next source.
            job = None if _reauth_pending() else _claim(("ready", "upload_retry"), "uploading", "COALESCE(translation_state,'') IN ('','done')")
            if job:
                try:
                    video_id = _upload_job(job)
                except ReauthRequired as error:
                    # Not this video's fault and not worth 3 tries: park it until the account is connected again.
                    with _db_lock, _connect() as db:
                        db.execute(
                            "UPDATE jobs SET status='upload_retry', next_attempt_at=?, last_error=?, updated_at=? WHERE id=? AND status='uploading'",
                            (_iso(_utc_now() + timedelta(minutes=5)), str(error)[:900], _iso(_utc_now()), job["id"]))
                except Exception as error:
                    attempts = int(job["upload_attempts"]) + 1
                    retrying = attempts < 3
                    with _db_lock, _connect() as db:
                        db.execute(
                            "UPDATE jobs SET status=?, upload_attempts=?, next_attempt_at=?, last_error=?, updated_at=? WHERE id=? AND status='uploading'",
                            ("upload_retry" if retrying else "failed", attempts,
                             _iso(_utc_now() + timedelta(seconds=30 if attempts == 1 else 120)),
                             f"업로드 {attempts}/3회 실패: {str(error)[:900]}", _iso(_utc_now()), job["id"]),
                        )
                else:
                    with _db_lock, _connect() as db:
                        db.execute(
                            "UPDATE jobs SET status='scheduled', youtube_video_id=?, last_error=NULL, upload_bytes=upload_total, updated_at=? WHERE id=? AND status='uploading'",
                            (video_id, _iso(_utc_now()), job["id"]),
                        )
                continue
            job = _claim_translation()
            if job:
                try:
                    _translate_job(job)
                except Exception as error:
                    attempts = int(job.get("translation_attempts") or 0) + 1
                    retrying = attempts < 3
                    with _db_lock, _connect() as db:
                        db.execute(
                            "UPDATE jobs SET translation_state=?,translation_attempts=?,translation_next_at=?,"
                            "translation_error=?,updated_at=? WHERE id=? AND translation_state='working'",
                            ("pending" if retrying else "failed", attempts,
                             _iso(_utc_now() + timedelta(seconds=30 if attempts == 1 else 120)),
                             f"일본어 번역 {attempts}/3회 실패: {str(error)[:700]}", _iso(_utc_now()), job["id"]),
                        )
                continue
            job = _claim(("preprocessing", "preprocessing_retry"), "preprocessing_active")
            if job:
                try:
                    _preprocess(job)
                except Exception as error:
                    attempts = int(job["preprocess_attempts"]) + 1
                    retrying = attempts < 3
                    with _db_lock, _connect() as db:
                        db.execute(
                            "UPDATE jobs SET status=?, preprocess_attempts=?, next_attempt_at=?, last_error=?, updated_at=? WHERE id=? AND status='preprocessing_active'",
                            ("preprocessing_retry" if retrying else "preprocess_failed", attempts,
                             _iso(_utc_now() + timedelta(seconds=15 if attempts == 1 else 60)),
                             f"전처리 {attempts}/3회 실패: {str(error)[:900]}", _iso(_utc_now()), job["id"]),
                        )
                else:
                    with _db_lock, _connect() as db:
                        db.execute(
                            "UPDATE jobs SET status='ready', preprocess_attempts=0, next_attempt_at=?, last_error=NULL, updated_at=? WHERE id=? AND status='preprocessing_active'",
                            (_iso(_utc_now()), _iso(_utc_now()), job["id"]),
                        )
                continue
            if time.monotonic() - _last_remote_sync >= 60:
                _last_remote_sync = time.monotonic()
                try:
                    _sync_published_jobs()
                except Exception:
                    pass
        except Exception as error:
            if job:
                with _db_lock, _connect() as db:
                    db.execute("UPDATE jobs SET last_error=?, updated_at=? WHERE id=?", (str(error)[:900], _iso(_utc_now()), job["id"]))
        time.sleep(2)


def start_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    _init_db()
    now = _iso(_utc_now())
    with _db_lock, _connect() as db:
        db.execute("UPDATE jobs SET status='preprocessing_retry', next_attempt_at=?, last_error='서버 재시작 후 전처리 재개', updated_at=? WHERE status='preprocessing_active'", (now, now))
        db.execute("UPDATE jobs SET status='upload_retry', next_attempt_at=?, last_error='서버 재시작 후 업로드 재개', updated_at=? WHERE status='uploading'", (now, now))
        db.execute("UPDATE jobs SET translation_state='pending',translation_next_at=?,translation_error='서버 재시작 후 번역 재개',updated_at=? WHERE translation_state='working'", (now, now))
    _worker_started = True
    threading.Thread(target=_queue_loop, name="youtube-shorts-queue", daemon=True).start()


@router.get("/status")
def status() -> dict:
    public = _public_config()
    # Older connections predate profile-photo storage. Fetch it once so the
    # shared channel switcher can identify YouTube visually without requiring
    # the user to reconnect the channel.
    if public["configured"] and not public["channel_thumbnail_url"]:
        try:
            response = httpx.get(
                "https://www.googleapis.com/youtube/v3/channels",
                params={"part": "snippet", "mine": "true"},
                headers={"Authorization": f"Bearer {_access_token()}"}, timeout=30,
            )
            response.raise_for_status()
            channel = (response.json().get("items") or [])[0]
            snippet = channel.get("snippet", {})
            thumbnails = snippet.get("thumbnails", {})
            thumbnail = next((thumbnails.get(size, {}).get("url", "") for size in ("high", "medium", "default")
                              if thumbnails.get(size, {}).get("url")), "")
            _save_config({"channel_id": channel.get("id", public["channel_id"]),
                          "channel_title": snippet.get("title", public["channel_title"]),
                          "channel_thumbnail_url": thumbnail})
            public = _public_config()
        except Exception:
            pass
    return public


@router.post("/oauth/settings")
def save_oauth_settings(client_id: str = Form(...), client_secret: str = Form(...)) -> dict:
    client_id, client_secret = client_id.strip(), client_secret.strip()
    if not client_id.endswith(".apps.googleusercontent.com"):
        raise HTTPException(400, "Google OAuth 클라이언트 ID 형식이 올바르지 않습니다.")
    if len(client_secret) < 20:
        raise HTTPException(400, "OAuth 클라이언트 보안 비밀번호가 너무 짧습니다. API 키나 서비스 계정 ID가 아니라 같은 웹 클라이언트의 Client secret을 복사해주세요.")
    _save_config({"client_id": client_id, "client_secret": client_secret})
    return _public_config()


@router.get("/oauth/start")
def oauth_start():
    config = _load_config(include_secrets=True)
    if not config.get("client_id") or not config.get("client_secret"):
        raise HTTPException(400, "먼저 Google OAuth 앱 정보를 저장해주세요.")
    state = secrets.token_urlsafe(32)
    _save_config({"oauth_state": state, "oauth_state_expires_at": _iso(_utc_now() + timedelta(minutes=15))})
    query = urlencode({
        "client_id": config["client_id"], "redirect_uri": REDIRECT_URI, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true", "state": state,
    })
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{query}")


@router.get("/oauth/callback")
def oauth_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse(f"/?youtube_oauth=error&message={quote(error[:300])}", status_code=302)
    try:
        config = _load_config(include_secrets=True)
        expires = datetime.fromisoformat(config.get("oauth_state_expires_at", ""))
        if not state or not secrets.compare_digest(state, config.get("oauth_state", "")) or expires < _utc_now():
            raise RuntimeError("OAuth 요청이 만료됐거나 state가 일치하지 않습니다.")
        token_response = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code, "client_id": config["client_id"], "client_secret": config["client_secret"],
                "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code",
            }, timeout=60,
        )
        if token_response.is_error:
            raise RuntimeError(_google_error(token_response))
        tokens = token_response.json()
        access_token = tokens["access_token"]
        channel_response = httpx.get(
            "https://www.googleapis.com/youtube/v3/channels", params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"}, timeout=60,
        )
        if channel_response.is_error:
            raise RuntimeError(_google_error(channel_response))
        channels = channel_response.json().get("items", [])
        if not channels:
            raise RuntimeError("연결한 Google 계정에서 YouTube 채널을 찾지 못했습니다.")
        channel = channels[0]
        snippet = channel.get("snippet", {})
        thumbnails = snippet.get("thumbnails", {})
        thumbnail = next((thumbnails.get(size, {}).get("url", "") for size in ("high", "medium", "default")
                          if thumbnails.get(size, {}).get("url")), "")
        _save_config({
            "access_token": access_token,
            "refresh_token": tokens.get("refresh_token") or config.get("refresh_token", ""),
            "token_expires_at": _iso(_utc_now() + timedelta(seconds=int(tokens.get("expires_in", 3600)))),
            "channel_id": channel["id"], "channel_title": snippet.get("title", ""),
            "channel_thumbnail_url": thumbnail,
            "oauth_state": "", "oauth_state_expires_at": "",
            "reauth_required": False, "reauth_reason": "", "reauth_at": "",
        })
        try:
            resumed, skipped = _retry_after_reauth()
            if resumed or skipped:
                _telegram("youtube-reauth-done:" + _iso(_utc_now()),
                          f"YouTube 다시 연결됐어요. 자동으로 이어서 올리는 작업 {resumed}개"
                          + (f", 예약 시간이 이미 지나 직접 확인이 필요한 작업 {skipped}개 (예약 시간을 새로 정하고 '다시 시도')" if skipped else "")
                          + ".")
        except Exception:
            pass
        return RedirectResponse("/?youtube_oauth=success", status_code=302)
    except Exception as failure:
        return RedirectResponse(f"/?youtube_oauth=error&message={quote(str(failure)[:300])}", status_code=302)


@router.delete("/config")
def disconnect() -> dict:
    config = _load_config(include_secrets=True)
    if config.get("refresh_token"):
        try:
            httpx.post("https://oauth2.googleapis.com/revoke", params={"token": config["refresh_token"]}, timeout=30)
        except Exception:
            pass
    CONFIG_PATH.unlink(missing_ok=True)
    return {"configured": False}


@router.get("/jobs")
def list_jobs(limit: int = 200) -> dict:
    with _db_lock, _connect() as db:
        rows = db.execute("SELECT * FROM jobs ORDER BY scheduled_at DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall()
    return {"items": [_row(row) for row in rows]}


@router.post("/jobs")
async def create_job(
    file: UploadFile = File(...), title: str = Form(...), description: str = Form(""), tags: str = Form(""),
    scheduled_at: str = Form(""), notify_subscribers: bool = Form(False), made_for_kids: bool = Form(False),
    contains_synthetic_media: bool = Form(False), remove_watermark: bool = Form(True),
    metadata_language: str = Form("original"),
    schedule_mode: str = Form("manual"),
) -> dict:
    title, description = title.strip(), description.strip()
    if not title or len(title) > 100:
        raise HTTPException(400, "제목은 1~100자로 입력해주세요.")
    if len(description) > 5000:
        raise HTTPException(400, "설명은 5,000자까지 입력할 수 있습니다.")
    if metadata_language not in {"original", "ja"}:
        raise HTTPException(400, "지원하지 않는 메타데이터 언어입니다.")
    if schedule_mode not in ('manual','immediate'):
        raise HTTPException(400,'등록 방식이 올바르지 않습니다.')
    immediate = schedule_mode == 'immediate'
    schedule = None if immediate else _parse_schedule(scheduled_at)
    content_type = (file.content_type or mimetypes.guess_type(file.filename or "")[0] or "").lower()
    suffix = Path(file.filename or "video.mp4").suffix.lower()
    if not content_type.startswith("video/") or suffix not in {".mp4", ".mov"}:
        raise HTTPException(400, "MP4 또는 MOV 영상만 선택해주세요.")
    parsed_tags = [item.strip() for item in tags.split(",") if item.strip()][:50]
    if sum(len(item) for item in parsed_tags) > 450:
        raise HTTPException(400, "태그가 너무 깁니다.")
    job_id = uuid.uuid4().hex
    job_dir = MEDIA_ROOT / job_id
    source_dir = job_dir / "source"
    source_dir.mkdir(mode=0o700, parents=True)
    source_name = _safe_name(file.filename or "video", suffix)
    final_name = _safe_name(file.filename or "short", ".mp4")
    try:
        content_hash = await _save_upload(file, source_dir / source_name)
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    finally:
        await file.close()
    now = _iso(_utc_now())
    schedule_iso = _iso(schedule) if schedule else now
    fingerprint = _job_fingerprint(content_hash, title, description, parsed_tags, 'immediate' if immediate else schedule_iso, notify_subscribers,
                                   made_for_kids, contains_synthetic_media, metadata_language)
    try:
        with _db_lock, _connect() as db:
            duplicate = db.execute(
                "SELECT * FROM jobs WHERE fingerprint=? AND status != 'cancelled' LIMIT 1", (fingerprint,)
            ).fetchone()
            if duplicate:
                created = duplicate
            else:
                conflict = db.execute(
                    "SELECT id FROM jobs WHERE scheduled_at=? AND status != 'cancelled' LIMIT 1", (schedule_iso,)
                ).fetchone()
                if conflict and not immediate:
                    raise HTTPException(409, f"같은 시각에 이미 YouTube 예약이 있습니다. ({conflict['id'][:8]})")
                db.execute(
                    """INSERT INTO jobs(id,status,title,description,tags,scheduled_at,created_at,updated_at,source_filename,filename,
                       next_attempt_at,notify_subscribers,made_for_kids,contains_synthetic_media,content_hash,fingerprint,reservation_key,
                       metadata_language,title_original,description_original,translation_state,translation_next_at,output_width,output_height)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (job_id, "preprocessing", title, description, json.dumps(parsed_tags, ensure_ascii=False), schedule_iso, now, now,
                     source_name, final_name, now, int(notify_subscribers), int(made_for_kids), int(contains_synthetic_media),
                     content_hash, fingerprint, None if immediate else schedule_iso, metadata_language, title, description,
                     "pending" if metadata_language == "ja" else "", now, 1080, 1920),
                )
                if immediate:
                    db.execute('UPDATE jobs SET publish_immediately=1 WHERE id=?',(job_id,))
                created = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    except sqlite3.IntegrityError:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(409, "같은 시각에 이미 YouTube 예약이 있습니다.")
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    if created["id"] != job_id:
        shutil.rmtree(job_dir, ignore_errors=True)
        item = _row(created)
        item["duplicate"] = True
        item["duplicate_of"] = item["id"]
        return item
    return _row(created)


@router.post("/jobs/{job_id}/update")
def update_job(job_id: str, title: str = Form(...), description: str = Form(""), tags: str = Form(""),
               scheduled_at: str = Form(""), metadata_language: str = Form("original")) -> dict:
    title, description = title.strip(), description.strip()
    if not title or len(title) > 100 or len(description) > 5000:
        raise HTTPException(400, "제목 또는 설명 길이를 확인해주세요.")
    if metadata_language not in {"original", "ja"}:
        raise HTTPException(400, "지원하지 않는 메타데이터 언어입니다.")
    parsed_tags = [item.strip() for item in tags.split(",") if item.strip()][:50]
    with _db_lock, _connect() as db:
        current = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not current:
            raise HTTPException(404, "예약을 찾을 수 없습니다.")
        if current["status"] in {"uploading", "cancelled", "published"}:
            raise HTTPException(409, "현재 상태에서는 수정할 수 없습니다.")
        immediate = publish_timing.keep_immediate(current, scheduled_at)
        if current['publish_immediately'] and current['youtube_video_id']:
            raise HTTPException(409,'바로 공개 요청이 이미 YouTube에 전달됐습니다. YouTube에서 공개 상태를 확인해주세요.')
        schedule = datetime.fromisoformat(current['scheduled_at']) if immediate else _parse_schedule(scheduled_at)
        schedule_iso = _iso(schedule)
        conflict = db.execute(
            "SELECT id FROM jobs WHERE scheduled_at=? AND id!=? AND status != 'cancelled' LIMIT 1",
            (schedule_iso, job_id),
        ).fetchone()
        if conflict and not immediate:
            raise HTTPException(409, f"같은 시각에 이미 YouTube 예약이 있습니다. ({conflict['id'][:8]})")
        fingerprint = _job_fingerprint(
            current["content_hash"], title, description, parsed_tags, 'immediate' if immediate else schedule_iso,
            bool(current["notify_subscribers"]), bool(current["made_for_kids"]), bool(current["contains_synthetic_media"]), metadata_language,
        ) if current["content_hash"] else None
        updated = dict(current)
        remote_title = title if metadata_language == "original" else current["title"]
        remote_description = description if metadata_language == "original" else current["description"]
        updated.update({"title": remote_title, "description": remote_description, "tags": json.dumps(parsed_tags, ensure_ascii=False), "scheduled_at": schedule_iso})
        updated['publish_immediately'] = int(immediate)
    try:
        _update_remote(updated)
    except Exception as error:
        raise HTTPException(502, str(error)) from error
    with _db_lock, _connect() as db:
        try:
            db.execute("UPDATE jobs SET title=?,description=?,tags=?,scheduled_at=?,updated_at=?,fingerprint=?,reservation_key=?,"
                       "metadata_language=?,title_original=?,description_original=?,translation_state=?,translation_attempts=0,"
                       "translation_next_at=?,translation_error=NULL WHERE id=?",
                       (title, description, json.dumps(parsed_tags, ensure_ascii=False), schedule_iso, _iso(_utc_now()), fingerprint,
                        None if immediate else schedule_iso, metadata_language, title, description, "pending" if metadata_language == "ja" else "",
                        _iso(_utc_now()), job_id))
            db.execute('UPDATE jobs SET publish_immediately=? WHERE id=?',(int(immediate),job_id))
        except sqlite3.IntegrityError as error:
            raise HTTPException(409, "같은 시각에 이미 YouTube 예약이 있습니다.") from error
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _row(row)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    with _db_lock, _connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "예약을 찾을 수 없습니다.")
    if row["status"] == "uploading":
        raise HTTPException(409, "업로드가 끝난 뒤 취소해주세요.")
    if row["youtube_video_id"]:
        try:
            _delete_remote(row["youtube_video_id"])
        except Exception as error:
            raise HTTPException(502, str(error)) from error
    with _db_lock, _connect() as db:
        db.execute("UPDATE jobs SET status='cancelled', updated_at=? WHERE id=?", (_iso(_utc_now()), job_id))
        updated = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _row(updated)


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str) -> dict:
    with _db_lock, _connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or (row["status"] not in {"preprocess_failed", "failed", "cancelled"} and row["translation_state"] != "failed"):
            raise HTTPException(409, "재시도할 수 있는 상태가 아닙니다.")
        processed = (MEDIA_ROOT / job_id / row["filename"]).is_file()
        status = "ready" if processed else "preprocessing"
        translation_state = "pending" if row["metadata_language"] == "ja" else ""
        db.execute("UPDATE jobs SET status=?,preprocess_attempts=0,upload_attempts=0,next_attempt_at=?,last_error=NULL,"
                   "translation_state=?,translation_attempts=0,translation_next_at=?,translation_error=NULL,updated_at=? WHERE id=?",
                   (status, _iso(_utc_now()), translation_state, _iso(_utc_now()), _iso(_utc_now()), job_id))
        updated = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _row(updated)


@router.get("/jobs/{job_id}/video")
def preview_video(job_id: str):
    with _db_lock, _connect() as db:
        row = db.execute("SELECT filename FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "예약을 찾을 수 없습니다.")
    path = MEDIA_ROOT / job_id / row["filename"]
    if not path.is_file():
        raise HTTPException(404, "전처리된 영상이 아직 없습니다.")
    return FileResponse(path, media_type="video/mp4", filename=row["filename"])


@router.get("/jobs/{job_id}/thumbnail")
def job_thumbnail(job_id: str):
    """Return a cached first-frame preview without exposing the source video."""
    try:
        job_id = uuid.UUID(job_id).hex
    except (ValueError, AttributeError) as error:
        raise HTTPException(422, "예약 ID가 올바르지 않습니다.") from error
    with _db_lock, _connect() as db:
        row = db.execute("SELECT filename,source_filename,updated_at FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "예약을 찾을 수 없습니다.")
    job_dir = MEDIA_ROOT / job_id
    processed = job_dir / row["filename"]
    source = job_dir / "source" / row["source_filename"]
    video = processed if processed.is_file() else source
    if not video.is_file():
        raise HTTPException(404, "미리보기를 만들 영상이 없습니다.")
    try:
        preview = media_thumbnails.thumbnail(video, job_dir / ".thumbnails")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise HTTPException(422, "영상 썸네일을 만들 수 없습니다.") from error
    return FileResponse(preview, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@router.delete("/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    with _db_lock, _connect() as db:
        row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "예약을 찾을 수 없습니다.")
        if row["status"] in {"preprocessing_active", "uploading"}:
            raise HTTPException(409, "현재 처리 중인 작업은 삭제할 수 없습니다.")
        db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    shutil.rmtree(MEDIA_ROOT / job_id, ignore_errors=True)
    return {"deleted": True}


start_worker()
