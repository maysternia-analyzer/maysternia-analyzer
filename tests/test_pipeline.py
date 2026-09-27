import copy
from datetime import timedelta

import pytest

import database as db
from services import pipeline, zoom
from tests.conftest import sample_lesson_analysis, sample_sales_analysis
from tests.test_zoom import WEBHOOK_OBJECT


@pytest.fixture
def fake_ai(monkeypatch):
    calls = {"analyze": [], "detect": []}

    def analyze(record_type, text):
        calls["analyze"].append((record_type, text))
        return sample_lesson_analysis() if record_type == "lesson" else sample_sales_analysis()

    def detect(**kwargs):
        calls["detect"].append(kwargs)
        return {"record_type": "lesson", "person_name": "Myroslava", "reason": "test"}

    monkeypatch.setattr(pipeline, "analyze", analyze)
    monkeypatch.setattr(pipeline, "detect_type_and_name", detect)
    return calls


def _meeting(**overrides):
    obj = copy.deepcopy(WEBHOOK_OBJECT)
    obj.update(overrides)
    return obj


def _process_next():
    job = db.claim_next_job()
    assert job, "у черзі немає задачі"
    return pipeline.process_record(job)


# ── Надходження з Zoom ────────────────────────────────────────────────────────

def test_ingest_creates_single_record_per_meeting():
    info = zoom.meeting_info(_meeting())
    result = pipeline.ingest_zoom_meeting(info, "recording.completed")
    assert result["result"] == "queued"
    rec = db.get_record(result["record_id"])
    assert rec["source"] == "zoom" and rec["auto_detect"] == 1
    assert rec["zoom_meeting_uuid"] == WEBHOOK_OBJECT["uuid"]
    assert rec["filename"] == "zoom_vtt-1.vtt"
    assert (rec["record_date"], rec["record_time"]) == ("2026-07-29", "18:44")  # UTC → Київ
    assert rec["not_before"] is None  # транскрипція вже є — обробляємо одразу
    # Повторний вебхук, поллер і transcript_completed не створюють дублікатів
    assert pipeline.ingest_zoom_meeting(info, "poller")["result"] == "duplicate"
    assert pipeline.ingest_zoom_meeting(info, "recording.transcript_completed")["result"] == "duplicate"
    assert db.fetch_one("SELECT COUNT(*) AS n FROM records")["n"] == 1


def test_ingest_media_only_waits_and_transcript_event_wakes_it():
    no_vtt = _meeting(recording_files=[f for f in WEBHOOK_OBJECT["recording_files"] if f["file_type"] != "TRANSCRIPT"])
    first = pipeline.ingest_zoom_meeting(zoom.meeting_info(no_vtt), "recording.completed")
    rec = db.get_record(first["record_id"])
    assert rec["filename"] == "zoom_m4a-1.m4a" and rec["not_before"] > db.utcnow_iso()
    # Раніше тут створювався другий запис (інший file_id) → дублікат зустрічі
    woken = pipeline.ingest_zoom_meeting(zoom.meeting_info(_meeting()), "recording.transcript_completed")
    assert woken == {"result": "woken", "record_id": first["record_id"]}
    assert db.get_record(first["record_id"])["not_before"] is None
    assert db.fetch_one("SELECT COUNT(*) AS n FROM records")["n"] == 1


def test_ingest_respects_legacy_processed_file_ids():
    db.claim_zoom_key("m4a-1")  # старий код позначав окремі файли
    result = pipeline.ingest_zoom_meeting(zoom.meeting_info(_meeting()), "poller")
    assert result["result"] == "duplicate"
    assert db.is_zoom_file_processed(f"meeting:{WEBHOOK_OBJECT['uuid']}")
    assert db.fetch_one("SELECT COUNT(*) AS n FROM records")["n"] == 0


def test_ingest_skips_short_and_empty_meetings():
    assert pipeline.ingest_zoom_meeting(zoom.meeting_info(_meeting(duration=0)), "poller")["result"] == "skipped_short"
    assert pipeline.ingest_zoom_meeting(zoom.meeting_info(_meeting(recording_files=[])), "poller")["result"] == "no_files"
    assert pipeline.ingest_zoom_meeting(zoom.meeting_info(_meeting(uuid="")), "poller")["result"] == "no_uuid"
    assert db.fetch_one("SELECT COUNT(*) AS n FROM records")["n"] == 0


def test_seed_legacy_zoom_keys():
    db.create_record("2026-06-01", "sales", "X", "zoom_file123.m4a", source="zoom", status="done")
    db.create_record("2026-06-01", "sales", "X", "zoom_manual_1700000000.vtt", status="done")
    db.create_record("2026-06-01", "sales", "X", "upload.m4a", status="done")
    assert pipeline.seed_legacy_zoom_keys() == 1
    assert db.is_zoom_file_processed("file123")
    assert pipeline.seed_legacy_zoom_keys() == 0


def test_record_source_inference_for_legacy_rows():
    assert pipeline.record_source({"filename": "zoom_abc.vtt"}) == "zoom"
    assert pipeline.record_source({"filename": "zoom_manual_1.vtt"}) == "text"
    assert pipeline.record_source({"filename": "Запис.m4a"}) == "upload"
    assert pipeline.record_source({"source": "text", "filename": "zoom_abc.vtt"}) == "text"
    assert pipeline.zoom_file_id_from_filename("zoom_abc.def.vtt") == "abc.def"


def test_upload_path_blocks_traversal():
    assert pipeline.upload_path("../app.py") is None
    assert pipeline.upload_path("sub/x.m4a") is None
    assert pipeline.upload_path("") is None
    assert pipeline.upload_path("ok.m4a").name == "ok.m4a"


# ── Обробка ───────────────────────────────────────────────────────────────────

def test_process_zoom_transcript_end_to_end(monkeypatch, fake_ai, zoom_env):
    pipeline.ingest_zoom_meeting(zoom.meeting_info(_meeting()), "recording.completed")
    monkeypatch.setattr(zoom, "get_meeting_recordings", lambda uuid: _meeting())
    monkeypatch.setattr(zoom, "download_transcript", lambda url: "Myroslava: Привіт усім")
    assert _process_next() == "done"
    rec = db.get_all_records()[0]
    rec = db.get_record(rec["id"])
    assert rec["status"] == "done" and rec["transcription"] == "Myroslava: Привіт усім"
    assert (rec["record_type"], rec["person_name"]) == ("lesson", "Myroslava")
    assert rec["analysis"]["_kind"] == "lesson"
    assert rec["job_kind"] == "analyze" and rec["locked_at"] is None
    assert fake_ai["detect"][0]["duration"] == 132


def test_process_zoom_media_defers_while_waiting_for_transcript(monkeypatch, fake_ai, zoom_env):
    no_vtt = _meeting(recording_files=[f for f in WEBHOOK_OBJECT["recording_files"] if f["file_type"] != "TRANSCRIPT"])
    rid = pipeline.ingest_zoom_meeting(zoom.meeting_info(no_vtt), "recording.completed")["record_id"]
    db.update_record(rid, not_before=None)
    now = db.utcnow()
    fresh = copy.deepcopy(no_vtt)
    for f in fresh["recording_files"]:
        f["recording_end"] = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(zoom, "get_meeting_recordings", lambda uuid: fresh)
    monkeypatch.setattr(zoom, "download_media", lambda *a: pytest.fail("ще рано качати аудіо"))
    assert _process_next() == "deferred"
    rec = db.get_record(rid)
    assert rec["status"] == "queued" and rec["not_before"] > db.utcnow_iso() and rec["attempts"] == 0


def test_process_zoom_media_without_openai_errors_after_wait(monkeypatch, fake_ai, zoom_env):
    no_vtt = _meeting(recording_files=[f for f in WEBHOOK_OBJECT["recording_files"] if f["file_type"] != "TRANSCRIPT"])
    rid = pipeline.ingest_zoom_meeting(zoom.meeting_info(no_vtt), "recording.completed")["record_id"]
    db.update_record(rid, not_before=None)
    monkeypatch.setattr(zoom, "get_meeting_recordings", lambda uuid: no_vtt)  # зустріч давно завершилась
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert _process_next() == "error"
    assert "Audio transcript" in db.get_record(rid)["error_message"]


def test_process_zoom_media_transcribes_and_deletes_file(monkeypatch, fake_ai, zoom_env, tmp_path):
    no_vtt = _meeting(recording_files=[f for f in WEBHOOK_OBJECT["recording_files"] if f["file_type"] != "TRANSCRIPT"])
    rid = pipeline.ingest_zoom_meeting(zoom.meeting_info(no_vtt), "recording.completed")["record_id"]
    db.update_record(rid, not_before=None)
    media = tmp_path / "zoom_m4a-1.m4a"
    media.write_bytes(b"x")
    monkeypatch.setattr(zoom, "get_meeting_recordings", lambda uuid: no_vtt)
    monkeypatch.setattr(zoom, "download_media", lambda url, name: media)
    monkeypatch.setattr(pipeline, "whisper_configured", lambda: True)
    monkeypatch.setattr(pipeline, "transcribe", lambda path: "Олена: текст")
    assert _process_next() == "done"
    assert not media.exists()
    assert db.get_record(rid)["transcription"] == "Олена: текст"


def test_process_zoom_deleted_meeting_is_permanent_error(monkeypatch, fake_ai, zoom_env):
    rid = pipeline.ingest_zoom_meeting(zoom.meeting_info(_meeting()), "recording.completed")["record_id"]
    monkeypatch.setattr(zoom, "get_meeting_recordings", lambda uuid: None)
    assert _process_next() == "error"
    assert "не знайдено в Zoom" in db.get_record(rid)["error_message"]


def test_legacy_zoom_record_resolved_by_file_id(monkeypatch, fake_ai, zoom_env):
    rid = db.create_record("2026-07-29", "sales", "Невідомо", "zoom_vtt-1.vtt", source="", auto_detect=False)
    monkeypatch.setattr(zoom, "find_meeting_by_file_id", lambda file_id: _meeting() if file_id == "vtt-1" else None)
    monkeypatch.setattr(zoom, "download_transcript", lambda url: "Олена: Добрий день")
    assert _process_next() == "done"
    rec = db.get_record(rid)
    assert rec["zoom_meeting_uuid"] == WEBHOOK_OBJECT["uuid"] and rec["source"] == "zoom"
    assert fake_ai["detect"] == []  # тип не визначаємо автоматично, бо auto_detect=0


def test_transient_errors_retry_then_fail(monkeypatch, fake_ai, zoom_env):
    rid = pipeline.ingest_zoom_meeting(zoom.meeting_info(_meeting()), "recording.completed")["record_id"]

    def flaky(uuid):
        raise zoom.ZoomError("503", transient=True)

    monkeypatch.setattr(zoom, "get_meeting_recordings", flaky)
    for attempt in (1, 2):
        assert _process_next() == "retry"
        rec = db.get_record(rid)
        assert rec["status"] == "queued" and rec["attempts"] == attempt
        assert "Тимчасова помилка" in rec["error_message"]
        db.update_record(rid, not_before=None)
    assert _process_next() == "error"
    assert db.get_record(rid)["status"] == "error"


def test_analysis_failure_keeps_transcript_and_retry_skips_transcription(monkeypatch, zoom_env):
    rid = pipeline.ingest_zoom_meeting(zoom.meeting_info(_meeting()), "recording.completed")["record_id"]
    monkeypatch.setattr(zoom, "get_meeting_recordings", lambda uuid: _meeting())
    downloads = []
    monkeypatch.setattr(zoom, "download_transcript", lambda url: downloads.append(url) or "Олена: текст")
    monkeypatch.setattr(pipeline, "detect_type_and_name",
                        lambda **k: {"record_type": "sales", "person_name": "Олена", "reason": ""})

    from services.llm import LLMError

    def overloaded(record_type, text):
        raise LLMError("overloaded", transient=True)

    monkeypatch.setattr(pipeline, "analyze", overloaded)
    assert _process_next() == "retry"
    rec = db.get_record(rid)
    # Старий код затирав транскрипцію повідомленням про помилку
    assert rec["transcription"] == "Олена: текст" and rec["job_kind"] == "analyze"

    monkeypatch.setattr(pipeline, "analyze", lambda record_type, text: sample_sales_analysis())
    db.update_record(rid, not_before=None)
    assert _process_next() == "done"
    assert len(downloads) == 1


def test_process_uploaded_text_and_missing_file(fake_ai):
    rid = db.create_record("2026-09-01", "sales", "Олена", "", source="text", job_kind="analyze",
                           transcription="Олена: Добрий день")
    assert _process_next() == "done"
    assert fake_ai["analyze"][-1] == ("sales", "Олена: Добрий день")
    assert fake_ai["detect"] == []

    rid2 = db.create_record("2026-09-01", "lesson", "Мирослава", "gone.m4a", source="upload")
    assert _process_next() == "error"
    assert "Railway" in db.get_record(rid2)["error_message"]
    assert db.get_record(rid)["status"] == "done"


def test_process_uploaded_vtt_file(fake_ai, monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "UPLOAD_FOLDER", tmp_path)
    (tmp_path / "abc.vtt").write_text("WEBVTT\n\n00:01.000 --> 00:02.000\nОлена: Привіт\n", encoding="utf-8")
    rid = db.create_record("2026-09-01", "sales", "Олена", "abc.vtt", source="upload")
    assert _process_next() == "done"
    assert db.get_record(rid)["transcription"] == "Олена: Привіт"


def test_legacy_error_suffix_is_stripped_on_reanalyze(fake_ai):
    db.create_record("2026-09-01", "sales", "Олена", "zoom_x.vtt", source="zoom", job_kind="analyze",
                     transcription="Олена: текст\n[ПОМИЛКА аналізу]: timeout")
    assert _process_next() == "done"
    assert fake_ai["analyze"][-1][1] == "Олена: текст"


def test_manual_type_change_during_processing_wins(monkeypatch, fake_ai, zoom_env):
    rid = pipeline.ingest_zoom_meeting(zoom.meeting_info(_meeting()), "recording.completed")["record_id"]
    monkeypatch.setattr(zoom, "get_meeting_recordings", lambda uuid: _meeting())

    def download(url):
        # користувач вручну змінив тип, поки йшла транскрипція
        db.update_record(rid, record_type="sales", person_name="Олена", auto_detect=0)
        return "Олена: текст"

    monkeypatch.setattr(zoom, "download_transcript", download)
    assert _process_next() == "done"
    rec = db.get_record(rid)
    assert (rec["record_type"], rec["person_name"]) == ("sales", "Олена")
    assert fake_ai["detect"] == [] and fake_ai["analyze"][-1][0] == "sales"


def test_run_pending_jobs_processes_queue(fake_ai):
    for i in range(3):
        db.create_record("2026-09-01", "sales", f"M{i}", "", source="text", job_kind="analyze", transcription="т")
    assert pipeline.run_pending_jobs() == 3
    assert db.count_records_by_status() == {"done": 3}


# ── Регресії з код-рев'ю ──────────────────────────────────────────────────────

def test_failed_record_creation_does_not_lose_meeting(monkeypatch):
    info = zoom.meeting_info(_meeting())
    monkeypatch.setattr(pipeline, "zoom_start_to_local", lambda start: (None, ""))  # NOT NULL → помилка INSERT
    with pytest.raises(Exception):
        pipeline.ingest_zoom_meeting(info, "recording.completed")
    assert not db.is_zoom_file_processed(f"meeting:{WEBHOOK_OBJECT['uuid']}")  # транзакцію відкочено
    monkeypatch.undo()
    assert pipeline.ingest_zoom_meeting(info, "recording.completed")["result"] == "queued"  # повтор Zoom спрацює


def test_late_transcript_retries_failed_record():
    no_vtt = _meeting(recording_files=[f for f in WEBHOOK_OBJECT["recording_files"] if f["file_type"] != "TRANSCRIPT"])
    rid = pipeline.ingest_zoom_meeting(zoom.meeting_info(no_vtt), "recording.completed")["record_id"]
    db.update_record(rid, status="error", error_message="немає транскрипції")
    result = pipeline.ingest_zoom_meeting(zoom.meeting_info(_meeting()), "recording.transcript_completed")
    assert result == {"result": "retried", "record_id": rid}
    assert db.get_record(rid)["status"] == "queued"


def test_type_change_during_analysis_requeues(monkeypatch, fake_ai):
    rid = db.create_record("2026-09-01", "sales", "Олена", "", source="text", job_kind="analyze", transcription="т")

    def analyze(record_type, text):
        db.update_record(rid, record_type="lesson")   # користувач змінив тип під час аналізу
        return sample_sales_analysis()

    monkeypatch.setattr(pipeline, "analyze", analyze)
    assert _process_next() == "done"
    rec = db.get_record(rid)
    assert rec["status"] == "queued" and rec["job_kind"] == "analyze"


def test_heartbeat_runs_during_long_job(monkeypatch, fake_ai):
    import time as _time
    monkeypatch.setattr(pipeline, "HEARTBEAT_SECONDS", 0.05)
    rid = db.create_record("2026-09-01", "sales", "Олена", "", source="text", job_kind="analyze", transcription="т")
    beats = []

    def slow_analyze(record_type, text):
        db.update_record(rid, locked_at="2000-01-01T00:00:00")
        _time.sleep(0.3)
        beats.append(db.get_record(rid)["locked_at"])
        return sample_sales_analysis()

    monkeypatch.setattr(pipeline, "analyze", slow_analyze)
    assert _process_next() == "done"
    assert beats[0] > "2000-01-01T00:00:00"   # heartbeat оновив мітку, поки йшов аналіз


def test_database_errors_are_transient():
    import sqlite3
    assert pipeline.is_transient(sqlite3.OperationalError("database is locked"))
    assert pipeline.is_transient(db.DatabaseBusyError("busy"))
    assert not pipeline.is_transient(ValueError("x"))


def test_cleanup_orphan_zoom_media(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "UPLOAD_FOLDER", tmp_path)
    for name in ("zoom_a.m4a", "zoom_b.m4a.part", "x.m4a.part", "user-upload.m4a"):
        (tmp_path / name).write_bytes(b"x")
    assert pipeline.cleanup_orphan_zoom_media() == 3
    assert [p.name for p in tmp_path.iterdir()] == ["user-upload.m4a"]


def test_multiple_transcript_segments_are_joined(monkeypatch, fake_ai, zoom_env):
    meeting = _meeting()
    second = dict(meeting["recording_files"][2], id="vtt-2", download_url="https://zoom.us/rec/download/vtt2",
                  recording_start="2026-07-29T17:00:00Z")
    meeting["recording_files"][2]["recording_start"] = "2026-07-29T15:45:00Z"
    meeting["recording_files"].insert(0, second)
    rid = pipeline.ingest_zoom_meeting(zoom.meeting_info(meeting), "recording.completed")["record_id"]
    monkeypatch.setattr(zoom, "get_meeting_recordings", lambda uuid: meeting)
    monkeypatch.setattr(zoom, "download_transcript", lambda url: "друга частина" if url.endswith("vtt2") else "перша частина")
    assert _process_next() == "done"
    assert db.get_record(rid)["transcription"] == "перша частина\nдруга частина"
