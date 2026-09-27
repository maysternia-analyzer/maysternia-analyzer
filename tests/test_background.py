import fcntl
import json
import threading

import database as db
from services import background


def test_only_one_leader_holds_the_lock(tmp_path, monkeypatch):
    lock_path = tmp_path / "worker.lock"
    monkeypatch.setattr(background, "LOCK_PATH", str(lock_path))
    monkeypatch.setattr(background, "_lock_file", None)
    assert background._acquire_lock()
    # Інший «процес» (інший file description) не може взяти lock
    other = open(lock_path, "a+")
    try:
        try:
            fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            acquired = False
        assert not acquired
    finally:
        other.close()
        background._lock_file.close()


def test_run_job_releases_slot_even_on_crash(monkeypatch):
    def boom(job):
        raise RuntimeError("crash")

    monkeypatch.setattr(background.pipeline, "process_record", boom)
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    background._active.add(42)
    background._run_job({"id": 42}, slots)
    assert 42 not in background._active
    assert slots.acquire(blocking=False)


def test_start_is_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("BACKGROUND_JOBS", "0")
    monkeypatch.setattr(background, "_started", False)
    background.start()
    assert background._started is False


def test_worker_status_roundtrip():
    assert background.worker_status() is None
    db.set_setting(background.WORKER_STATUS_KEY, json.dumps({"pid": 1, "active": [3]}))
    assert background.worker_status() == {"pid": 1, "active": [3]}
    db.set_setting(background.WORKER_STATUS_KEY, "not json")
    assert background.worker_status() is None


def test_login_limiter_lru_does_not_reset_target():
    from security import LoginRateLimiter
    limiter = LoginRateLimiter(max_failures=3, window=900, max_keys=50)
    for _ in range(3):
        limiter.record_failure("victim")
    assert not limiter.allowed("victim")
    for i in range(500):                   # спроба «витіснити» лічильник сміттєвими ключами
        limiter.record_failure(f"junk{i}")
    assert not limiter.allowed("victim")   # заблокований ключ не витісняється
    assert len(limiter._failures) <= 51    # памʼять обмежена
