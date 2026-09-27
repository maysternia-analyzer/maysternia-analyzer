"""
Фонові задачі.

У кожному процесі gunicorn стартує потік, але працює лише один «лідер»
(файловий lock у /tmp): воркер черги, поллер Zoom, heartbeat активних задач
і повернення в чергу записів, обробку яких перервав перезапуск.
Якщо процес-лідер завершиться, lock звільниться і його перехопить інший процес.
"""
import fcntl
import json
import logging
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import database as db
from services import notify, pipeline, zoom
from services.poller import poll_once

log = logging.getLogger(__name__)

LOCK_PATH = os.environ.get("WORKER_LOCK_PATH") or os.path.join(tempfile.gettempdir(), "maysternia-worker.lock")
CONCURRENCY = max(1, int(os.environ.get("JOB_CONCURRENCY", "2")))
POLL_INTERVAL_MINUTES = max(1, int(os.environ.get("ZOOM_POLL_INTERVAL_MINUTES", "5")))
QUEUE_CHECK_SECONDS = 3
MAINTENANCE_SECONDS = 60
STALE_MINUTES = 5
WORKER_STATUS_KEY = "worker_status"

_started = False
_lock_file = None
_active: set[int] = set()
_active_lock = threading.Lock()
_wake = threading.Event()
_poll_state = {"at": None, "result": None, "error": None}


def enabled() -> bool:
    return os.environ.get("BACKGROUND_JOBS", "1") != "0"


def wake() -> None:
    """Прискорює перевірку черги (діє, якщо лідер — цей же процес)."""
    _wake.set()


def start() -> None:
    global _started
    if _started or not enabled():
        return
    _started = True
    threading.Thread(target=_leader_main, name="bg-leader", daemon=True).start()


def _acquire_lock() -> bool:
    global _lock_file
    handle = open(LOCK_PATH, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _lock_file = handle  # тримаємо відкритим до кінця життя процесу
    return True


def _leader_main() -> None:
    while not _acquire_lock():
        time.sleep(15)
    log.info("Фонові задачі працюють у процесі pid=%s (паралельно задач: %s)", os.getpid(), CONCURRENCY)
    try:
        requeued, failed = db.requeue_stale_jobs(STALE_MINUTES, max_attempts=pipeline.MAX_ATTEMPTS)
        if requeued or failed:
            log.info("Після перезапуску: повернуто в чергу %s, позначено помилкою %s", requeued, failed)
        _backfill()
    except Exception:
        log.exception("Помилка стартового відновлення черги або заповнення показників")
    try:
        removed = pipeline.cleanup_orphan_zoom_media()
        if removed:
            log.info("Видалено %s незавершених медіафайлів Zoom", removed)
        if zoom.is_configured():
            seeded = pipeline.seed_legacy_zoom_keys()
            if seeded:
                log.info("Позначено %s файлів зі старих Zoom-записів як оброблені", seeded)
    except Exception:
        log.exception("Помилка стартових задач")

    threading.Thread(target=_maintenance_loop, name="bg-maintenance", daemon=True).start()
    if zoom.is_configured():
        threading.Thread(target=_poller_loop, name="bg-poller", daemon=True).start()
    else:
        log.info("Zoom-синхронізацію вимкнено: ZOOM_* ключі не задані")
    _job_loop()


def _backfill() -> None:
    filled = db.backfill_light_columns()
    if filled:
        log.info("Заповнено показники для %s записів старих версій", filled)


def _job_loop() -> None:
    slots = threading.BoundedSemaphore(CONCURRENCY)
    executor = ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="job")
    while True:
        slots.acquire()
        try:
            job = db.claim_next_job()
        except Exception:
            log.exception("Черга: помилка БД")
            job = None
        if job is None:
            slots.release()
            _wake.wait(QUEUE_CHECK_SECONDS)
            _wake.clear()
            continue
        with _active_lock:
            _active.add(job["id"])
        executor.submit(_run_job, job, slots)


def _run_job(job: dict, slots: threading.BoundedSemaphore) -> None:
    try:
        pipeline.process_record(job)
    except Exception:
        log.exception("Непередбачена помилка задачі #%s", job.get("id"))
    finally:
        with _active_lock:
            _active.discard(job["id"])
        slots.release()
        _wake.set()


def _maintenance_loop() -> None:
    iteration = 0
    while True:
        time.sleep(MAINTENANCE_SECONDS)
        iteration += 1
        try:
            with _active_lock:
                active = list(_active)
            # heartbeat кожної задачі оновлює сам pipeline.process_record
            requeued, failed = db.requeue_stale_jobs(STALE_MINUTES, max_attempts=pipeline.MAX_ATTEMPTS)
            if requeued or failed:
                log.warning("Перервані задачі: повернуто в чергу %s, позначено помилкою %s", requeued, failed)
            db.set_setting(WORKER_STATUS_KEY, json.dumps({
                "pid": os.getpid(), "at": db.utcnow_iso(), "active": active,
                "concurrency": CONCURRENCY, "last_poll_at": _poll_state["at"],
                "last_poll_result": _poll_state["result"], "last_poll_error": _poll_state["error"],
            }, ensure_ascii=False))
            notify.send_daily_digest_if_due()
            if iteration % 60 == 0:
                db.prune_webhook_logs()
            if iteration % 10 == 0:
                _backfill()  # записи, дописані старою версією під час деплою
        except Exception:
            log.exception("Помилка обслуговування черги")


def _poller_loop() -> None:
    time.sleep(10)
    while True:
        try:
            _poll_state["result"] = poll_once()
            _poll_state["error"] = None
        except Exception as e:
            _poll_state["error"] = str(e)[:300]
            log.warning("Поллер Zoom: %s", e)
        _poll_state["at"] = db.utcnow_iso()
        time.sleep(POLL_INTERVAL_MINUTES * 60)


def worker_status() -> dict | None:
    """Статус лідера з БД (видно з будь-якого процесу)."""
    raw = db.get_setting(WORKER_STATUS_KEY)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None
