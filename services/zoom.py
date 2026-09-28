"""
Інтеграція з Zoom (Server-to-Server OAuth): токени, API хмарних записів,
вибір файлу для обробки, завантаження VTT/медіа, перевірка підпису вебхука.
"""
import hashlib
import hmac
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from services.timeutil import parse_iso
from services.transcript_text import transcript_from_file_bytes

log = logging.getLogger(__name__)

API_BASE = "https://api.zoom.us/v2"
TOKEN_URL = "https://zoom.us/oauth/token"
UPLOAD_FOLDER = Path(__file__).resolve().parent.parent / "uploads"

ZOOM_USER = os.environ.get("ZOOM_USER_ID", "me")
MAX_MEDIA_BYTES = 3 * 1024 ** 3
_HTTP_TIMEOUT = (15, 120)  # (connect, read)


def redact(text: str) -> str:
    """Прибирає OAuth-токен з текстів помилок (URL завантаження містить access_token)."""
    return re.sub(r"(access_token=)[^&\s'\"]+", r"\1***", str(text))


class ZoomError(RuntimeError):
    """Помилка Zoom API. transient=True — варто повторити пізніше."""

    def __init__(self, message: str, transient: bool = False, status: int | None = None):
        super().__init__(redact(message))
        self.transient = transient
        self.status = status


def is_configured() -> bool:
    return all(os.environ.get(k) for k in ("ZOOM_ACCOUNT_ID", "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET"))


# ── OAuth ─────────────────────────────────────────────────────────────────────

_token_lock = threading.Lock()
_token_cache = {"token": None, "expires_at": 0.0, "scopes": ""}
# Із цим доступом застосунок бачить записи ВСІХ організаторів акаунта, а не лише власника застосунку.
ACCOUNT_RECORDINGS_SCOPES = {"cloud_recording:read:list_account_recordings:admin", "recording:read:admin"}


def get_access_token(force_refresh: bool = False) -> str:
    """Токен Server-to-Server OAuth (живе 1 год, кешується)."""
    if not is_configured():
        raise ZoomError("Zoom не налаштовано: задайте ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET")
    with _token_lock:
        if (not force_refresh and _token_cache["token"]
                and time.time() < _token_cache["expires_at"] - 120):
            return _token_cache["token"]
        try:
            resp = requests.post(
                TOKEN_URL,
                params={"grant_type": "account_credentials", "account_id": os.environ["ZOOM_ACCOUNT_ID"]},
                auth=(os.environ["ZOOM_CLIENT_ID"], os.environ["ZOOM_CLIENT_SECRET"]),
                timeout=20,
            )
        except requests.RequestException as e:
            raise ZoomError(f"Zoom OAuth недоступний: {e}", transient=True) from e
        if resp.status_code != 200:
            raise ZoomError(
                f"Zoom OAuth відхилив ключі ({resp.status_code}): {resp.text[:200]}",
                transient=resp.status_code >= 500, status=resp.status_code,
            )
        data = resp.json()
        _token_cache["scopes"] = data.get("scope") or ""
        _token_cache["token"] = data["access_token"]
        _token_cache["expires_at"] = time.time() + int(data.get("expires_in", 3600))
        return _token_cache["token"]


def token_info() -> dict:
    """Короткий опис підключення (для сторінки «Система»)."""
    get_access_token()
    return {"account_id": os.environ.get("ZOOM_ACCOUNT_ID"), "expires_in_sec": int(_token_cache["expires_at"] - time.time())}


def _retry_delay(resp: requests.Response, attempt: int) -> float:
    try:
        return min(float(resp.headers.get("Retry-After", "")), 10.0)
    except ValueError:
        return 2.0 * (attempt + 1)


def _request(method: str, url: str, params: dict | None = None) -> requests.Response:
    """HTTP-запит до Zoom API: повтор після 401 (новий токен), 429/5xx та збоїв мережі."""
    resp = None
    for attempt in range(3):
        headers = {"Authorization": f"Bearer {get_access_token()}"}
        try:
            resp = requests.request(method, url, headers=headers, params=params, timeout=_HTTP_TIMEOUT)
        except requests.RequestException as e:
            if attempt == 2:
                raise ZoomError(f"Zoom API недоступний: {e}", transient=True) from e
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code == 401 and attempt < 2:
            get_access_token(force_refresh=True)
            continue
        if (resp.status_code == 429 or resp.status_code >= 500) and attempt < 2:
            time.sleep(_retry_delay(resp, attempt))
            continue
        return resp
    return resp


def _api_get(path: str, params: dict | None = None) -> dict | None:
    resp = _request("GET", f"{API_BASE}{path}", params=params or {})
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise ZoomError(
            f"Zoom API {path} → {resp.status_code}: {resp.text[:300]}",
            transient=resp.status_code == 429 or resp.status_code >= 500, status=resp.status_code,
        )
    return resp.json()


# ── Записи ────────────────────────────────────────────────────────────────────

def token_scopes() -> set[str]:
    """Дозволи (scopes) застосунку Zoom з OAuth-токена."""
    get_access_token()
    return set(_token_cache.get("scopes", "").split())


def account_wide() -> bool:
    """Чи бачить застосунок записи всіх користувачів акаунта (а не лише власника застосунку)."""
    if ZOOM_USER != "me":  # явно вказаний ZOOM_USER_ID — працюємо лише з ним
        return False
    try:
        return bool(token_scopes() & ACCOUNT_RECORDINGS_SCOPES)
    except ZoomError:
        return False


def list_recordings(date_from: date, date_to: date | None = None) -> list[dict]:
    """Усі зустрічі з хмарними записами за період (вікнами по 30 днів, з пагінацією)."""
    date_to = date_to or datetime.now(timezone.utc).date()
    path = "/accounts/me/recordings" if account_wide() else f"/users/{ZOOM_USER}/recordings"
    meetings: list[dict] = []
    window_start = date_from
    while window_start <= date_to:
        window_end = min(window_start + timedelta(days=29), date_to)
        page_token = ""
        while True:
            params = {"from": window_start.isoformat(), "to": window_end.isoformat(), "page_size": 300}
            if page_token:
                params["next_page_token"] = page_token
            data = _api_get(path, params) or {}
            meetings.extend(data.get("meetings", []))
            page_token = data.get("next_page_token") or ""
            if not page_token:
                break
        window_start = window_end + timedelta(days=1)
    return meetings


def _encode_uuid(meeting_uuid: str) -> str:
    # Zoom вимагає подвійного URL-кодування UUID (особливо з '/' або '//').
    return quote(quote(meeting_uuid, safe=""), safe="")


def get_meeting_recordings(meeting_uuid: str) -> dict | None:
    """Актуальний список файлів зустрічі (свіжі download_url). None — запис видалено."""
    return _api_get(f"/meetings/{_encode_uuid(meeting_uuid)}/recordings")


def find_meeting_by_file_id(file_id: str, months_back: int = 6) -> dict | None:
    """Для старих записів, де збережено лише id файлу: шукаємо зустріч у записах акаунта."""
    today = datetime.now(timezone.utc).date()
    for m in list_recordings(today - timedelta(days=30 * months_back), today):
        if any(str(f.get("id")) == str(file_id) for f in m.get("recording_files", [])):
            return m
    return None


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def _safe_share_url(url) -> str:
    """Посилання на запис показуємо, лише якщо це справді https://*.zoom.us."""
    if not isinstance(url, str):
        return ""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https" and (host == "zoom.us" or host.endswith(".zoom.us")):
        return url
    return ""


def meeting_info(obj: dict) -> dict:
    """Нормалізує зустріч з вебхука (payload.object) або API у спільний формат (стійко до сміття)."""
    obj = obj if isinstance(obj, dict) else {}
    raw_files = obj.get("recording_files")
    files = [f for f in raw_files if isinstance(f, dict)] if isinstance(raw_files, list) else []
    ends = [e for e in (parse_iso(_str(f.get("recording_end"))) for f in files) if e]
    start = parse_iso(_str(obj.get("start_time")))
    duration = _int(obj.get("duration"))
    end = max(ends) if ends else (start + timedelta(minutes=duration) if start else None)
    topic = _str(obj.get("topic")) or "Zoom Meeting"
    return {
        "uuid": _str(obj.get("uuid")),
        "meeting_id": str(obj.get("id") or ""),
        "topic": topic,
        "start_time": _str(obj.get("start_time")),
        "end_time": end.strftime("%Y-%m-%dT%H:%M:%SZ") if end else "",
        "duration": duration,
        "host_email": _str(obj.get("host_email")),
        "share_url": _safe_share_url(obj.get("share_url")),
        "is_breakout": "breakout" in topic.lower()
                       or any("breakout" in _str(f.get("recording_type")).lower() for f in files),
        "files": [
            {
                "id": str(f.get("id") or ""),
                "file_type": _str(f.get("file_type")).upper(),
                "file_extension": _str(f.get("file_extension")).lower(),
                "file_size": _int(f.get("file_size")),
                "recording_type": _str(f.get("recording_type")),
                "status": _str(f.get("status")),
                "download_url": _str(f.get("download_url")),
                "recording_start": _str(f.get("recording_start")),
            }
            for f in files
        ],
    }


def select_best_file(files: list[dict]) -> tuple[dict | None, str | None]:
    """
    Що обробляти: готова транскрипція Zoom (VTT) → аудіо M4A → відео MP4
    (active speaker → будь-яке). Повертає (file, 'transcript' | 'media' | None).
    """
    done = [f for f in files if f.get("status") == "completed" and f.get("download_url")]
    transcript = next((f for f in done if f["file_type"] == "TRANSCRIPT"), None)
    if transcript:
        return transcript, "transcript"
    media = [f for f in done if f["file_type"] in ("M4A", "MP4") and f.get("file_size", 0) > 100_000]
    for predicate in (
        lambda f: f["file_type"] == "M4A",
        lambda f: "active_speaker" in f["recording_type"].lower(),
        lambda f: f["file_type"] == "MP4",
    ):
        best = next((f for f in media if predicate(f)), None)
        if best:
            return best, "media"
    return None, None


def is_too_short(info: dict) -> bool:
    """Випадкові записи на 0–2 хвилини (у Zoom їх багато) не аналізуємо."""
    from services import settings
    return info.get("duration", 0) < settings.get("zoom_min_duration_minutes")


def file_local_name(file: dict) -> str:
    ext = "vtt" if file["file_type"] == "TRANSCRIPT" else (file.get("file_extension") or "mp4")
    return f"zoom_{file['id']}.{ext.lower()}"


# ── Завантаження ──────────────────────────────────────────────────────────────

def _download(url: str, stream: bool) -> requests.Response:
    """Завантаження файлу запису. Токен у query-параметрі переживає редірект на CDN."""
    for attempt in range(2):
        token = get_access_token(force_refresh=attempt > 0)
        try:
            resp = requests.get(url, params={"access_token": token}, stream=stream,
                                timeout=_HTTP_TIMEOUT, allow_redirects=True)
        except requests.RequestException as e:
            raise ZoomError(f"Не вдалося завантажити файл із Zoom: {e}", transient=True) from e
        if resp.status_code == 401 and attempt == 0:
            resp.close()
            continue
        if resp.status_code >= 400:
            resp.close()
            raise ZoomError(
                f"Zoom не віддав файл ({resp.status_code})",
                transient=resp.status_code == 429 or resp.status_code >= 500, status=resp.status_code,
            )
        if "text/html" in resp.headers.get("content-type", ""):
            resp.close()
            raise ZoomError("Zoom повернув HTML замість файлу — перевірте права застосунку (scopes)")
        return resp
    raise ZoomError("Zoom відхилив токен при завантаженні файлу")


def download_transcript(download_url: str) -> str:
    """Завантажує VTT-транскрипцію Zoom і повертає текст діалогу."""
    resp = _download(download_url, stream=False)
    text = transcript_from_file_bytes(resp.content, "vtt")
    if not text.strip():
        raise ZoomError("Транскрипція Zoom порожня")
    return text


def looks_like_media(head: bytes) -> bool:
    """Перевірка «магічних байтів» медіафайлу (відсікає HTML/JSON-помилки)."""
    if len(head) >= 8 and head[4:8] == b"ftyp":            # MP4 / M4A / MOV
        return True
    if head[:3] == b"ID3":                                  # MP3 з тегом
        return True
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:  # MP3 frame sync
        return True
    return head[:4] in (b"RIFF", b"\x1a\x45\xdf\xa3", b"OggS", b"fLaC")  # WAV, WebM, OGG, FLAC


def download_media(download_url: str, filename: str) -> Path:
    """Потокове завантаження аудіо/відео у uploads/ з перевіркою, що це медіафайл."""
    UPLOAD_FOLDER.mkdir(exist_ok=True)
    target = UPLOAD_FOLDER / filename
    partial = target.with_suffix(target.suffix + ".part")
    resp = _download(download_url, stream=True)
    size = 0
    try:
        with open(partial, "wb") as out:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_MEDIA_BYTES:
                    raise ZoomError("Файл запису завеликий (понад 3 ГБ)")
                out.write(chunk)
    except requests.RequestException as e:
        partial.unlink(missing_ok=True)
        raise ZoomError(f"Обрив зʼєднання під час завантаження: {e}", transient=True) from e
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    finally:
        resp.close()
    with open(partial, "rb") as f:
        head = f.read(16)
    if size < 10_000 or not looks_like_media(head):
        partial.unlink(missing_ok=True)
        raise ZoomError(f"Завантажений файл не схожий на аудіо/відео ({size} байт)")
    partial.replace(target)
    log.info("Zoom: завантажено %s (%.1f МБ)", filename, size / 1024 / 1024)
    return target


# ── Вебхук ────────────────────────────────────────────────────────────────────

def webhook_secret() -> str:
    return os.environ.get("ZOOM_WEBHOOK_SECRET", "")


def verify_webhook_signature(body: bytes, timestamp: str, signature: str) -> bool:
    secret = webhook_secret()
    if not (secret and timestamp and signature):
        return False
    message = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def url_validation_response(plain_token: str) -> dict:
    encrypted = hmac.new(webhook_secret().encode(), plain_token.encode(), hashlib.sha256).hexdigest()
    return {"plainToken": plain_token, "encryptedToken": encrypted}


def email_to_name(email: str) -> str:
    if not email:
        return "Невідомо"
    return email.split("@")[0].replace(".", " ").replace("_", " ").title()
