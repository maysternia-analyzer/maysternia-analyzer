import json
import sqlite3
from datetime import timedelta

import pytest

import database as db
from tests.conftest import sample_lesson_analysis, sample_sales_analysis


def _new(**kwargs):
    params = dict(record_date="2026-09-01", record_type="sales", person_name="Олена", filename="f.m4a")
    params.update(kwargs)
    return db.create_record(params.pop("record_date"), params.pop("record_type"),
                            params.pop("person_name"), params.pop("filename"), **params)


def test_init_db_is_idempotent():
    db.init_db()
    db.init_db()
    assert db.fetch_one("SELECT COUNT(*) AS n FROM records")["n"] == 0


@pytest.mark.skipif(db.USE_POSTGRES, reason="перевірка міграції старої SQLite-схеми")
def test_migration_adds_missing_columns_to_legacy_sqlite(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE records (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            record_date TEXT NOT NULL, record_time TEXT DEFAULT '', record_type TEXT NOT NULL,
            person_name TEXT NOT NULL, filename TEXT, transcription TEXT, analysis_json TEXT,
            manager_comment TEXT, status TEXT DEFAULT 'pending');
        INSERT INTO records (created_at, record_date, record_type, person_name, filename, transcription, status)
            VALUES ('2026-06-01T10:00:00', '2026-06-01', 'sales', 'Old', 'zoom_1.vtt', 'Готовий текст', 'done');
        INSERT INTO records (created_at, record_date, record_type, person_name, filename, transcription, status)
            VALUES ('2026-06-01T10:00:00', '2026-06-01', 'sales', 'Old', 'zoom_2.vtt', '[ПОМИЛКА]: x', 'error');
    """)
    conn.close()
    monkeypatch.setattr(db, "SQLITE_PATH", path)
    db.init_db()
    rows = db.fetch_all("SELECT id, job_kind, trainer_name, sale_made, error_message FROM records ORDER BY id")
    assert rows[0]["job_kind"] == "analyze"   # є транскрипція → при повторі лише аналіз
    assert rows[1]["job_kind"] == "full"
    assert rows[0]["sale_made"] is None


def test_create_get_update_record_and_analysis_kind():
    rid = _new(record_type="lesson")
    db.update_record(rid, analysis=sample_lesson_analysis(), status="done")
    rec = db.get_record(rid)
    assert rec["analysis"]["overall_score"] == 80
    assert rec["updated_at"]
    # Змінили тип — аналіз для заняття не показуємо як аналіз продажу
    db.update_record(rid, record_type="sales")
    rec = db.get_record(rid)
    assert rec["analysis"] is None and rec["analysis_stale"]


def test_update_record_rejects_unknown_columns():
    rid = _new()
    with pytest.raises(ValueError):
        db.update_record(rid, bogus=1)


def test_corrupted_analysis_json_does_not_crash():
    rid = _new()
    db.update_record(rid, analysis_json="{not json")
    assert db.get_record(rid)["analysis"] is None
    assert db.get_all_records()[0]["analysis"] is None


def test_get_all_records_filters_and_excludes_transcription():
    a = _new(person_name="Олена", record_date="2026-09-01", transcription="довгий текст")
    _new(person_name="Ігор", record_date="2026-09-05", record_type="lesson", trainer_name="")
    _new(person_name="Олена", record_date="2026-09-10", trainer_name="Мирослава")
    assert len(db.get_all_records(person_name="Олена")) == 2
    assert len(db.get_all_records(record_type="lesson")) == 1
    assert len(db.get_all_records(date_from="2026-09-02", date_to="2026-09-06")) == 1
    assert len(db.get_all_records(trainer_name="Мирослава")) == 1
    rows = db.get_all_records()
    assert "transcription" not in rows[0]
    assert [r["record_date"] for r in rows] == ["2026-09-10", "2026-09-05", "2026-09-01"]
    preview = db.get_all_records(transcript_preview_chars=6)
    assert next(r for r in preview if r["id"] == a)["transcription"] == "довгий"


def test_person_and_trainer_names_skip_empty():
    _new(person_name="")
    _new(person_name="Олена", trainer_name="Тренер")
    _new(person_name="Ігор", record_type="lesson")
    assert db.get_person_names() == ["Ігор", "Олена"]
    assert db.get_person_names("lesson") == ["Ігор"]
    assert db.get_trainer_names_from_sales() == ["Тренер"]


def test_sale_result_reset_sets_null():
    rid = _new()
    db.update_sale_result(rid, True, 20000)
    assert db.get_record(rid)["sale_made"] == 1
    assert db.get_record(rid)["sale_amount"] == 20000
    db.update_sale_result(rid, None)   # старий код записував 0 («Не продано») замість скидання
    rec = db.get_record(rid)
    assert rec["sale_made"] is None and rec["sale_amount"] is None
    db.update_sale_result(rid, False, 500)
    rec = db.get_record(rid)
    assert rec["sale_made"] == 0 and rec["sale_amount"] is None


def test_claim_next_job_is_exclusive_and_respects_not_before():
    first = _new()
    later = _new(not_before=db.utcnow_iso(timedelta(hours=1)))
    job = db.claim_next_job()
    assert job["id"] == first and job["status"] == "processing" and job["attempts"] == 1
    assert db.claim_next_job() is None  # другий запис ще відкладений
    db.update_record(later, not_before=db.utcnow_iso(timedelta(seconds=-1)))
    assert db.claim_next_job()["id"] == later


def test_claim_next_job_source_filter():
    _new(source="upload")
    zoom_id = _new(source="zoom")
    assert db.claim_next_job(source="zoom")["id"] == zoom_id
    assert db.claim_next_job(source="zoom") is None


def test_requeue_stale_jobs_and_max_attempts():
    stale = _new()
    db.claim_next_job()
    db.update_record(stale, locked_at=db.utcnow_iso(timedelta(minutes=-30)))
    fresh = _new()
    db.claim_next_job()
    assert db.requeue_stale_jobs(5, max_attempts=3) == (1, 0)
    assert db.get_record(stale)["status"] == "queued"
    assert db.get_record(fresh)["status"] == "processing"

    db.update_record(stale, status="processing", attempts=3, locked_at=db.utcnow_iso(timedelta(minutes=-30)))
    assert db.requeue_stale_jobs(5, max_attempts=3) == (0, 1)
    assert db.get_record(stale)["status"] == "error"
    assert "Повторити" in db.get_record(stale)["error_message"]


def test_requeue_legacy_rows_without_locked_at_use_longer_threshold():
    rid = _new(status="analyzing")
    db.execute("UPDATE records SET updated_at = ?, locked_at = NULL WHERE id = ?",
               (db.utcnow_iso(timedelta(minutes=-10)), rid))
    assert db.requeue_stale_jobs(5, legacy_minutes=15) == (0, 0)
    db.execute("UPDATE records SET updated_at = ? WHERE id = ?", (db.utcnow_iso(timedelta(minutes=-20)), rid))
    assert db.requeue_stale_jobs(5, legacy_minutes=15) == (1, 0)


def test_enqueue_resets_error_and_attempts():
    rid = _new(status="error")
    db.update_record(rid, error_message="boom", attempts=3)
    db.enqueue_record(rid, "analyze")
    rec = db.get_record(rid)
    assert (rec["status"], rec["job_kind"], rec["error_message"], rec["attempts"]) == ("queued", "analyze", None, 0)


def test_heartbeat_updates_locked_at():
    rid = _new()
    db.heartbeat([rid])
    assert db.get_record(rid)["locked_at"]
    db.heartbeat([])  # без помилок


def test_zoom_keys_claim_once():
    assert db.claim_zoom_key("meeting:abc")
    assert not db.claim_zoom_key("meeting:abc")
    assert db.is_zoom_file_processed("meeting:abc")
    assert db.any_zoom_key_processed(["x", "meeting:abc"])
    assert not db.any_zoom_key_processed([])
    assert db.release_zoom_key("meeting:abc") == 1
    assert not db.is_zoom_file_processed("meeting:abc")


def test_webhook_logs_return_dicts_and_prune():
    # Старий get_webhook_logs падав на PostgreSQL (dict(tuple)) → /debug/zoom-state віддавав 500
    for i in range(5):
        db.log_webhook("recording.completed", "queued", f"деталі {i}")
    logs = db.get_webhook_logs(3)
    assert [log["details"] for log in logs] == ["деталі 4", "деталі 3", "деталі 2"]
    assert set(logs[0]) == {"id", "received_at", "event", "status", "details"}
    assert db.prune_webhook_logs(keep=2) == 3
    assert len(db.get_webhook_logs(100)) == 2


def test_settings_upsert_and_if_absent():
    assert db.set_setting_if_absent("k", "one") == "one"
    assert db.set_setting_if_absent("k", "two") == "one"
    db.set_setting("k", "three")
    assert db.get_setting("k") == "three"
    assert db.get_setting("missing") is None


def test_insights_cache_roundtrip():
    db.save_insights({"summary": "перший"}, "2026-01-01", "")
    db.save_insights({"summary": "другий"}, "2026-01-01", "")
    cached = db.get_insights("2026-01-01", "")
    assert cached["data"]["summary"] == "другий"
    assert db.fetch_one("SELECT COUNT(*) AS n FROM insights_cache")["n"] == 1
    assert db.get_insights("", "") is None


def test_users_crud_and_admin_count():
    assert db.create_user("A@Example.com ", "Адмін", "hash", "admin")
    assert not db.create_user("a@example.com", "Дубль", "hash")
    user = db.get_user_by_email(" A@EXAMPLE.COM")
    assert user and user["email"] == "a@example.com"
    assert db.count_users() == 1 and db.count_active_admins() == 1
    db.update_user(user["id"], is_active=0)
    assert db.count_active_admins() == 0
    db.delete_user(user["id"])
    assert db.count_users() == 0


def test_delete_record_returns_row():
    rid = _new(filename="abc.m4a")
    assert db.delete_record(rid)["filename"] == "abc.m4a"
    assert db.get_record(rid) is None
    assert db.delete_record(rid) is None


def test_sql_placeholder_conversion_escapes_percent(monkeypatch):
    monkeypatch.setattr(db, "USE_POSTGRES", True)
    assert db._sql("SELECT * FROM t WHERE a LIKE 'zoom_%' AND b = ?") == \
        "SELECT * FROM t WHERE a LIKE 'zoom_%%' AND b = %s"


def test_zoom_record_filenames_like_query():
    _new(filename="zoom_123.vtt")
    _new(filename="manual.m4a")
    assert db.zoom_record_filenames() == ["zoom_123.vtt"]


def test_count_by_status_and_meeting_lookup():
    rid = _new(zoom_meeting_uuid="uuid-1", status="done")
    _new(status="error")
    assert db.count_records_by_status() == {"done": 1, "error": 1}
    assert db.find_record_by_meeting("uuid-1")["id"] == rid
    assert db.find_record_by_meeting("nope") is None
    # source_json серіалізується
    db.update_record(rid, source_json={"topic": "Т"})
    assert json.loads(db.get_record(rid)["source_json"]) == {"topic": "Т"}
    db.update_record(rid, analysis=sample_sales_analysis())
    assert db.get_record(rid)["analysis"]["_kind"] == "sales"


def test_legacy_analysis_values_are_coerced():
    rid = _new(record_type="lesson", status="done")
    db.update_record(rid, analysis_json=json.dumps({"overall_score": "85", "greeting": True,
                                                    "top_mistakes": "одна", "engagement_level": "Високий"}))
    a = db.get_record(rid)["analysis"]
    assert a["overall_score"] == 85 and a["greeting"] == {"result": True, "comment": ""}
    assert a["top_mistakes"] == ["одна"]


def test_enqueue_does_not_touch_running_jobs():
    rid = _new()
    db.claim_next_job()
    assert db.enqueue_record(rid, "analyze") is False
    assert db.get_record(rid)["status"] == "processing"
    db.update_record(rid, status="error")
    assert db.enqueue_record(rid, "analyze") is True


def test_create_zoom_record_once_is_atomic():
    first = db.create_zoom_record_once(["meeting:u", "file-1"], "2026-09-01", "sales", "X", "zoom_file-1.vtt",
                                       source="zoom", zoom_meeting_uuid="u")
    assert first and db.is_zoom_file_processed("file-1")
    assert db.create_zoom_record_once(["meeting:u", "file-2"], "2026-09-01", "sales", "X", "f") is None
    assert not db.is_zoom_file_processed("file-2")
    assert db.fetch_one("SELECT COUNT(*) AS n FROM records")["n"] == 1


@pytest.mark.skipif(not db.USE_POSTGRES, reason="пул зʼєднань лише для PostgreSQL")
def test_postgres_pool_reuses_connections():
    pids = {db.fetch_one("SELECT pg_backend_pid() AS pid")["pid"] for _ in range(5)}
    assert len(pids) == 1   # раніше minconn=0 → кожен запит відкривав нове зʼєднання
