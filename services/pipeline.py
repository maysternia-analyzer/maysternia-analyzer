"""
Конвеєр обробки записів.

1. Надходження: вебхук / поллер Zoom → ingest_zoom_meeting(); ручне завантаження → app.upload.
   Запис створюється зі статусом queued (дедуплікація Zoom — за UUID зустрічі).
2. Фоновий воркер (services/background.py) бере записи з черги → process_record():
   транскрипція (Zoom VTT / файл / Whisper) → визначення типу та імені → AI-аналіз → done.
3. Тимчасові збої (мережа, 429/5xx) повторюються автоматично; постійні → status=error з поясненням.
"""
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import requests

import database as db
from services import applog, notify, settings, zoom
from services.analysis import analyze
from services.detection import detect_type_and_name, guess_type
from services.timeutil import parse_iso, zoom_start_to_local
from services.transcript_text import is_valid_transcript, strip_error_suffix, transcript_from_file_bytes
from services.transcription import is_configured as whisper_configured
from services.transcription import transcribe

log = logging.getLogger(__name__)

UPLOAD_FOLDER = Path(__file__).resolve().parent.parent / "uploads"
MAX_ATTEMPTS = int(os.environ.get("JOB_MAX_ATTEMPTS", "3"))
TRANSCRIPT_RECHECK_MINUTES = 15
TEXT_EXTENSIONS = {"vtt", "txt"}
RETRY_DELAYS_MINUTES = (2, 10, 30)
HEARTBEAT_SECONDS = 60
_META_KEYS = ("uuid", "meeting_id", "topic", "start_time", "end_time", "duration",
              "host_email", "share_url", "is_breakout")


class Deferred(Exception):
    """Обробку треба відкласти (наприклад, Zoom ще готує транскрипцію)."""

    def __init__(self, until_iso: str, reason: str):
        super().__init__(reason)
        self.until = until_iso


class JobError(RuntimeError):
    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


# ── Допоміжне ─────────────────────────────────────────────────────────────────

def record_source(record: dict) -> str:
    """Джерело запису; для старих записів визначається за іменем файлу."""
    source = record.get("source")
    if source:
        return source
    filename = record.get("filename") or ""
    if filename.startswith("zoom_manual_"):
        return "text"
    if filename.startswith("zoom_"):
        return "zoom"
    return "upload"


def source_meta(record: dict) -> dict:
    import json
    raw = record.get("source_json")
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def zoom_file_id_from_filename(filename: str) -> str:
    if not filename or not filename.startswith("zoom_") or filename.startswith("zoom_manual_"):
        return ""
    return filename[len("zoom_"):].rsplit(".", 1)[0]


def upload_path(filename: str) -> Path | None:
    """Шлях до файлу в uploads/ (захист від ../)."""
    if not filename or "\x00" in filename:
        return None
    try:
        path = (UPLOAD_FOLDER / filename).resolve()
    except (ValueError, OSError):
        return None
    if path.parent != UPLOAD_FOLDER.resolve():
        return None
    return path


def _transient_types() -> tuple:
    types = [requests.ConnectionError, requests.Timeout, sqlite3.OperationalError]
    if db.USE_POSTGRES:
        import psycopg2
        types += [psycopg2.OperationalError, psycopg2.InterfaceError]
    return tuple(types)


def is_transient(error: Exception) -> bool:
    """Мережа, 429/5xx, недоступна БД — варто повторити; решта — постійна помилка."""
    if getattr(error, "transient", False):
        return True
    return isinstance(error, _transient_types())


@contextmanager
def _heartbeat(record_id: int):
    """Поки задача виконується, щохвилини оновлює locked_at (інакше її вважатимуть зависшою)."""
    stop = threading.Event()

    def beat():
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                db.heartbeat([record_id])
            except Exception:
                log.warning("Heartbeat запису #%s не вдався", record_id, exc_info=True)

    thread = threading.Thread(target=beat, name=f"heartbeat-{record_id}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()


# ── Надходження записів із Zoom ───────────────────────────────────────────────

def _wake_waiting_record(meeting_uuid: str, kind: str) -> dict:
    """
    Зʼявилась транскрипція Zoom для вже відомої зустрічі: якщо запис чекає на неї —
    запускаємо одразу; якщо він встиг завершитись помилкою без транскрипції — повторюємо.
    """
    existing = db.find_record_by_meeting(meeting_uuid)
    if existing and kind == "transcript":
        if existing["status"] == "queued" and existing.get("not_before"):
            db.update_record(existing["id"], not_before=None)
            return {"result": "woken", "record_id": existing["id"]}
        if existing["status"] == "error":
            record = db.get_record(existing["id"])
            if record and not is_valid_transcript(record.get("transcription")) \
                    and db.enqueue_record(existing["id"], "full"):
                return {"result": "retried", "record_id": existing["id"]}
    return {"result": "duplicate", "record_id": existing["id"] if existing else None}


def ingest_zoom_meeting(info: dict, origin: str) -> dict:
    """Ставить Zoom-зустріч у чергу (ідемпотентно). info — результат zoom.meeting_info()."""
    meeting_uuid = info.get("uuid")
    if not meeting_uuid:
        return {"result": "no_uuid"}
    if zoom.is_too_short(info):
        return {"result": "skipped_short"}
    best, kind = zoom.select_best_file(info["files"])
    if not best:
        return {"result": "no_files"}

    meeting_key = f"meeting:{meeting_uuid}"
    if db.is_zoom_file_processed(meeting_key):
        return _wake_waiting_record(meeting_uuid, kind)
    # Зустріч уже обробив старий код (він позначав окремі файли).
    if db.any_zoom_key_processed([f["id"] for f in info["files"] if f["id"]]):
        db.claim_zoom_key(meeting_key)
        return {"result": "duplicate"}

    record_date, record_time = zoom_start_to_local(info["start_time"])
    not_before = None
    if kind == "media":  # даємо Zoom час підготувати транскрипцію
        not_before = db.utcnow_iso(timedelta(minutes=TRANSCRIPT_RECHECK_MINUTES))
    meta = {k: info.get(k) for k in _META_KEYS}
    meta["origin"] = origin
    # Ключ зустрічі і запис створюються в одній транзакції: збій не «загубить» зустріч.
    record_id = db.create_zoom_record_once(
        [meeting_key, best["id"]],
        record_date,
        guess_type(info["duration"], info["is_breakout"]),
        zoom.email_to_name(info["host_email"]),
        zoom.file_local_name(best),
        record_time=record_time,
        source="zoom",
        source_json=meta,
        zoom_meeting_uuid=meeting_uuid,
        auto_detect=True,
        not_before=not_before,
    )
    if record_id is None:  # паралельний вебхук/поллер встиг першим
        return _wake_waiting_record(meeting_uuid, kind)
    log.info("Zoom (%s): у черзі запис #%s — %s", origin, record_id, info["topic"])
    return {"result": "queued", "record_id": record_id}


def cleanup_orphan_zoom_media() -> int:
    """
    Видаляє завантажені з Zoom медіафайли, що лишились після перерваної обробки
    (процес убито посеред завантаження/транскрипції). Викликати, поки задач немає.
    """
    removed = 0
    for path in list(UPLOAD_FOLDER.glob("zoom_*")) + list(UPLOAD_FOLDER.glob("*.part")):
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def seed_legacy_zoom_keys() -> int:
    """Позначає файли зі старих записів як оброблені (щоб поллер не дублював їх)."""
    count = 0
    for filename in db.zoom_record_filenames():
        file_id = zoom_file_id_from_filename(filename)
        if file_id and db.claim_zoom_key(file_id):
            count += 1
    return count


# ── Отримання транскрипції ────────────────────────────────────────────────────

def _zoom_transcript(record: dict) -> str:
    meta = source_meta(record)
    meeting_uuid = record.get("zoom_meeting_uuid") or meta.get("uuid")
    if meeting_uuid:
        meeting = zoom.get_meeting_recordings(meeting_uuid)
    else:
        file_id = zoom_file_id_from_filename(record.get("filename") or "")
        meeting = zoom.find_meeting_by_file_id(file_id) if file_id else None
    if not meeting:
        raise JobError("Запис не знайдено в Zoom (видалено з хмари або старший за 6 місяців)")

    info = zoom.meeting_info(meeting)
    if not info["uuid"]:
        info["uuid"] = meeting_uuid or ""
    best, kind = zoom.select_best_file(info["files"])
    now = db.utcnow()
    end = parse_iso(info["end_time"])
    end_naive = end.replace(tzinfo=None) if end else None
    wait_minutes = settings.get("zoom_transcript_wait_minutes")
    within_wait = end_naive is not None and now < end_naive + timedelta(minutes=wait_minutes)
    recheck = db.utcnow_iso(timedelta(minutes=TRANSCRIPT_RECHECK_MINUTES))

    if not best:
        if within_wait:
            raise Deferred(recheck, "Zoom ще обробляє файли запису")
        raise JobError("У Zoom-записі немає придатних файлів (транскрипції чи аудіо)")

    meta.update({k: info.get(k) for k in _META_KEYS if info.get(k) not in (None, "")})
    db.update_record(record["id"], zoom_meeting_uuid=info["uuid"] or None, source_json=meta,
                     filename=zoom.file_local_name(best), source="zoom")
    db.claim_zoom_key(best["id"])
    if info["uuid"]:
        db.claim_zoom_key(f"meeting:{info['uuid']}")

    if kind == "transcript":
        # Якщо запис у Zoom зупиняли й запускали знову — транскрипцій кілька; склеюємо по порядку.
        parts = sorted(
            (f for f in info["files"] if f["file_type"] == "TRANSCRIPT" and f["status"] == "completed"
             and f.get("download_url")),
            key=lambda f: f.get("recording_start") or "",
        ) or [best]
        return "\n".join(zoom.download_transcript(f["download_url"]) for f in parts)

    if within_wait:
        raise Deferred(recheck, "Очікуємо транскрипцію від Zoom")
    if not whisper_configured():
        raise JobError(
            "Zoom не створив транскрипцію для цього запису, а OPENAI_API_KEY не задано. "
            "Увімкніть у Zoom: Settings → Recording → Cloud recording → Audio transcript."
        )
    path = zoom.download_media(best["download_url"], zoom.file_local_name(best))
    try:
        return transcribe(path)
    finally:
        path.unlink(missing_ok=True)  # великі медіафайли не зберігаємо на диску


def obtain_transcript(record: dict) -> str:
    source = record_source(record)
    if source == "zoom":
        return _zoom_transcript(record)
    if source == "text":
        raise JobError("Транскрипція відсутня, а вихідного файлу немає — завантажте запис ще раз")
    path = upload_path(record.get("filename") or "")
    if path is None or not path.exists():
        raise JobError(
            "Файл запису не знайдено на сервері: Railway очищає диск при кожному деплої чи перезапуску. "
            "Завантажте файл ще раз."
        )
    ext = path.suffix.lower().lstrip(".")
    if ext in TEXT_EXTENSIONS:
        text = transcript_from_file_bytes(path.read_bytes(), ext)
        if not text.strip():
            raise JobError("Файл транскрипції порожній")
        return text
    return transcribe(path)


# ── Обробка запису ────────────────────────────────────────────────────────────

def _apply_detection(record: dict, text: str) -> str:
    fresh = db.get_record(record["id"]) or record
    if not fresh.get("auto_detect"):  # користувач уже вручну задав тип/імʼя
        return fresh.get("record_type") or "sales"
    meta = source_meta(record)
    result = detect_type_and_name(
        topic=meta.get("topic") or "",
        duration=int(meta.get("duration") or 0),
        is_breakout=bool(meta.get("is_breakout")),
        transcript=text,
        fallback_name=record.get("person_name") or "Невідомо",
    )
    name = result["person_name"]
    linked = db.get_linked_person_names()
    if linked and name not in linked:
        from services.transcript_text import speaker_stats
        speakers = speaker_stats(text)
        speaker_names = {s["speaker"] for s in speakers}
        total = sum(s["chars"] for s in speakers) or 1
        # Замінюємо лише «службове» імʼя (не учасник розмови: акаунт організатора, «Невідомо»…)
        # і лише на співробітника, який говорив суттєву частину часу — інакше тренера заняття
        # переписало б на менеджера, що говорить лише наприкінці.
        employee = next((s["speaker"] for s in speakers
                         if s["speaker"] in linked and s["chars"] / total >= 0.3), None)
        if name not in speaker_names and employee:
            name = employee
    # auto_detect=0: визначаємо один раз — повторні аналізи не «перекидають» запис між людьми.
    # Умова auto_detect = 1: якщо за час запиту до AI людина вже виправила тип/імʼя — не перетираємо.
    changed = db.execute(
        "UPDATE records SET record_type = ?, person_name = ?, auto_detect = 0, updated_at = ? "
        "WHERE id = ? AND auto_detect = 1",
        (result["record_type"], name, db.utcnow_iso(), record["id"]),
    )
    if not changed:
        return (db.get_record(record["id"]) or record).get("record_type") or "sales"
    result["person_name"] = name
    log.info("Запис #%s: %s / %s (%s)", record["id"], result["record_type"],
             result["person_name"], result.get("reason", ""))
    return result["record_type"]


def process_record(record: dict) -> str:
    """Обробляє запис, узятий з черги. Повертає done | deferred | retry | error."""
    record_id = record["id"]
    attempts = int(record.get("attempts") or 1)
    # record/job — у кожному рядку логу цієї задачі (і в журналі помилок).
    with applog.context(record=record_id, job=record.get("job_kind") or ""), _heartbeat(record_id):
        return _process(record, record_id, attempts)


def _process(record: dict, record_id: int, attempts: int) -> str:
    started = time.monotonic()
    log.info("Запис #%s: старт (%s, спроба %s, джерело %s)", record_id, record.get("job_kind"), attempts,
             record_source(record))
    try:
        if record.get("job_kind") in ("analyze", "reanalyze") and is_valid_transcript(record.get("transcription")):
            text = strip_error_suffix(record["transcription"])
        else:
            text = obtain_transcript(record)
            log.info("Запис #%s: транскрипція готова (%s символів, %.0f с)", record_id, len(text),
                     time.monotonic() - started)
            # Повтори після збою аналізу не повинні заново платити за транскрипцію.
            db.update_record(record_id, transcription=text, job_kind="analyze", locked_at=db.utcnow_iso())

        if record.get("auto_detect"):
            _apply_detection(record, text)
        # Тип беремо свіжий: користувач міг змінити його, поки йшла транскрипція.
        record_type = (db.get_record(record_id) or record).get("record_type") or "sales"

        db.update_record(record_id, status="analyzing", locked_at=db.utcnow_iso())
        log.info("Запис #%s: аналіз Claude (%s, %s символів)", record_id, record_type, len(text))
        result = analyze(record_type, text)
        extra = {}
        if record.get("job_kind") == "reanalyze":
            # масовий переаналіз не повинен потрапляти в щоденний звіт як «нові» дзвінки
            extra["analyzed_at"] = record.get("analyzed_at") or record.get("created_at")
        db.update_record(record_id, analysis=result, status="done", error_message=None,
                         locked_at=None, not_before=None, job_kind="analyze", **extra)
        current = db.get_record(record_id)
        if current and current.get("record_type") != record_type:  # тип змінили під час аналізу
            db.enqueue_record(record_id, "analyze")
        elif record.get("job_kind") != "reanalyze":  # масовий переаналіз не спамить сповіщеннями
            notify.notify_record_done(record_id)
        log.info("Запис #%s оброблено за %.0f с", record_id, time.monotonic() - started)
        return "done"

    except Deferred as d:
        db.update_record(record_id, status="queued", not_before=d.until, locked_at=None,
                         attempts=max(0, attempts - 1), error_message=None)
        log.info("Запис #%s відкладено до %s: %s", record_id, d.until, d)
        return "deferred"

    except Exception as e:  # noqa: BLE001 — будь-яка помилка має потрапити в запис
        message = str(e) or e.__class__.__name__
        if is_transient(e) and attempts < MAX_ATTEMPTS:
            delay = RETRY_DELAYS_MINUTES[min(attempts - 1, len(RETRY_DELAYS_MINUTES) - 1)]
            db.update_record(record_id, status="queued", locked_at=None,
                             not_before=db.utcnow_iso(timedelta(minutes=delay)),
                             error_message=f"Тимчасова помилка (спроба {attempts}/{MAX_ATTEMPTS}), "
                                           f"повторимо автоматично: {message}"[:2000])
            log.warning("Запис #%s: тимчасова помилка, повтор через %s хв: %s", record_id, delay, message)
            return "retry"
        log.exception("Запис #%s: помилка обробки", record_id)
        db.update_record(record_id, status="error", error_message=message[:2000], locked_at=None)
        return "error"


def run_pending_jobs(limit: int = 100, source: str | None = None) -> int:
    """Синхронно обробляє чергу (для CLI sync_zoom.py і тестів). Повертає кількість задач."""
    processed = 0
    while processed < limit:
        job = db.claim_next_job(source=source)
        if not job:
            break
        process_record(job)
        processed += 1
    return processed
