"""Журнал подій: маскування секретів, групування, контекст, сторінка «Журнал», аудит, JS-помилки, версія."""
import logging
from pathlib import Path

import pytest

import database as db
import app as app_module  # імпорт застосунку налаштовує журнал (applog.setup)
from services import applog, background, notify, pipeline, settings, version, zoom
from tests.conftest import CSRF, login, make_user, post_json

log = logging.getLogger("test.journal")


@pytest.fixture(autouse=True)
def fresh_settings():
    settings.invalidate_cache()
    yield
    settings.invalidate_cache()


def _logs(group=None):
    return db.list_logs(group, limit=100)


def test_redact_masks_tokens_keys_and_passwords():
    # Фейкові токени складаємо з частин, щоб сканери секретів не приймали тест за витік.
    fake_anthropic = "sk-" + "ant-api03-" + "AAAABBBBCCCCDDDD"
    fake_github = "gh" + "p_" + "0123456789abcdefghij" + "ABCDEFGHIJ012345"
    fake_telegram = "123456789" + ":" + "AAHdqTcvCH1vGWJx" + "fSeofSAs0K5PALDsaw"
    text = ("GET https://zoom.us/rec/download/x?access_token=eyJabc.def-123&x=1 "
            f"Authorization: Bearer abcdefghijk123 key {fake_anthropic} "
            "postgresql://postgres:SuperSecret@db.railway.internal:5432/railway "
            f"https://api.telegram.org/bot{fake_telegram}/sendMessage "
            f"{fake_github} password=hunter2")
    masked = applog.redact(text)
    for secret in ("eyJabc", "abcdefghijk123", "AAAABBBBCCCC", "SuperSecret", "AAHdqTcvCH1v", "0123456789abcdefghij",
                   "hunter2"):
        assert secret not in masked, secret
    assert "db.railway.internal" in masked and "zoom.us/rec/download" in masked


def test_errors_are_grouped_with_counter_and_context():
    for record_id in (1, 2, 3):
        with applog.context(record=record_id):
            try:
                raise ValueError(f"поганий JSON у записі {record_id}")
            except ValueError:
                log.exception("Запис #%s: помилка обробки", record_id)
    rows = _logs("errors")
    assert len(rows) == 1 and rows[0]["count"] == 3
    row = rows[0]
    assert row["message"] == "Запис #3: помилка обробки" and row["context"]["record"] == "3"
    assert "ValueError: поганий JSON у записі 3" in row["details"]      # найсвіжіший traceback
    log.error("Зовсім інша помилка")
    assert len(_logs("errors")) == 2


def test_info_logs_stay_out_of_journal_but_audit_and_warnings_go_in():
    log.info("звичайна інформація")
    logging.getLogger(applog.AUDIT).info("admin@x.com: Зміна налаштувань")
    log.warning("Щось підозріле, token=abcdef123456")
    messages = [r["message"] for r in _logs("all")]
    assert "звичайна інформація" not in messages
    assert "admin@x.com: Зміна налаштувань" in messages
    assert "Щось підозріле, token=***" in messages                    # секрет замасковано і в БД


def test_prune_and_clear_logs():
    log.error("стара помилка")
    db.execute("UPDATE app_logs SET last_at = '2020-01-01T00:00:00+00:00'")
    log.error("нова помилка")
    assert db.prune_logs(days=30) == 1
    assert [r["message"] for r in _logs("all")] == ["нова помилка"]
    assert db.clear_logs() == 1 and _logs("all") == []


def test_async_writer_flushes_queue():
    handler = applog.db_handler()
    handler.sync = False
    try:
        log.error("асинхронна помилка")
        assert handler.queue or _logs("errors")
        applog.flush()
        assert _logs("errors")[0]["message"] == "асинхронна помилка"
    finally:
        handler.sync = True


def test_unhandled_error_is_journaled_with_request_context(admin_client, monkeypatch):
    monkeypatch.setattr(db, "list_records", lambda *a, **k: 1 / 0)
    resp = admin_client.get("/")
    assert resp.status_code == 500 and resp.headers.get("X-Request-ID")
    row = _logs("errors")[0]
    assert row["message"] == "Необроблена помилка: GET /" and "ZeroDivisionError" in row["details"]
    assert row["context"]["rid"] == resp.headers["X-Request-ID"] and row["context"]["user"] == "admin@example.com"


def test_pipeline_errors_carry_record_id(monkeypatch):
    monkeypatch.setattr(pipeline, "analyze", lambda t, text: (_ for _ in ()).throw(KeyError("criteria")))
    rid = db.create_record("2026-09-01", "sales", "Олена", "", source="text", job_kind="analyze",
                           transcription="Олена: добрий день\nКлієнт: так")
    assert pipeline.process_record(db.claim_next_job()) == "error"
    row = _logs("errors")[0]
    assert row["context"]["record"] == str(rid) and row["context"]["job"] == "analyze"
    assert "KeyError" in row["details"]


def test_audit_trail_for_admin_actions_and_logins(client):
    user = make_user()
    resp = client.post("/login", data={"email": "admin@example.com", "password": "wrong", "csrf_token": CSRF})
    assert resp.status_code in (400, 401)  # без CSRF-сесії — 400, з нею — 401
    login(client, user)
    resp = client.post("/admin/settings", data={
        "csrf_token": CSRF, "company_context": "Школа", "low_score_threshold": "45", "daily_digest_time": "19:00",
        "zoom_min_duration_minutes": "3", "zoom_transcript_wait_minutes": "180"})
    assert resp.status_code == 302
    client.post("/logout", data={"csrf_token": CSRF})
    messages = [r["message"] for r in _logs("actions")]
    assert "admin@example.com: Зміна налаштувань" in messages
    assert "admin@example.com: Вихід із системи" in messages


def test_failed_login_is_audited(client):
    make_user()
    client.get("/login")
    with client.session_transaction() as sess:
        sess["_csrf_token"] = CSRF
    client.post("/login", data={"email": "admin@example.com", "password": "wrong", "csrf_token": CSRF})
    assert any(r["message"] == "Невдалий вхід: admin@example.com (невірний пароль)" for r in _logs("actions"))


def test_logs_page_filters_search_and_clear(admin_client):
    log.error("Помилка Zoom API 500")
    log.warning("Повільна сторінка: /team — 4.2 с")
    logging.getLogger(applog.AUDIT).info("admin@example.com: Зміна чек-листа (sales)")
    html = admin_client.get("/admin/logs").get_data(as_text=True)
    assert "Помилка Zoom API 500" in html and "Повільна сторінка" in html and "Зміна чек-листа" not in html
    html = admin_client.get("/admin/logs?group=actions").get_data(as_text=True)
    assert "Зміна чек-листа" in html and "Помилка Zoom" not in html
    html = admin_client.get("/admin/logs?group=all&q=zoom").get_data(as_text=True)
    assert "Помилка Zoom API 500" in html and "Повільна сторінка" not in html
    assert admin_client.post("/admin/logs/clear", data={"csrf_token": CSRF}).status_code == 302
    assert [r["message"] for r in _logs("all")] == ["admin@example.com: Очищення журналу"]


def test_logs_page_is_admin_only(client):
    login(client, make_user(email="v@x.com", role="viewer"))
    assert client.get("/admin/logs").status_code == 403
    assert client.post("/admin/logs/clear", data={"csrf_token": CSRF}).status_code == 403


def test_error_badge_in_sidebar(admin_client):
    assert "nav-badge" not in admin_client.get("/team").get_data(as_text=True)
    log.error("щось зламалось")
    app_module._error_badge.update(at=0.0)
    assert 'class="nav-badge"' in admin_client.get("/team").get_data(as_text=True)


def test_client_js_errors_are_journaled_and_rate_limited(admin_client):
    payload = {"message": "TypeError: x is undefined", "source": "/static/js/main.js", "line": 10, "column": 5,
               "stack": "at foo (main.js:10:5)", "url": "https://app/record/1"}
    assert post_json(admin_client, "/api/client-error", payload).json == {"ok": True}
    row = _logs("warnings")[0]
    assert row["logger"] == applog.CLIENT and row["message"] == "JS: TypeError: x is undefined"
    assert "main.js:10:5" in row["details"] and "https://app/record/1" in row["details"]
    for _ in range(30):
        post_json(admin_client, "/api/client-error", {"message": "Інша помилка"})
    assert db.list_logs("warnings", q="інша")[0]["count"] == 19      # ліміт 20 за 10 хв на користувача
    assert not any(r["logger"] == "audit" for r in _logs("all"))     # службовий запит не «дія користувача»


def test_client_error_requires_login(client):
    assert client.post("/api/client-error", json={"message": "x"}).status_code in (302, 400, 401)


def test_new_error_alerts_telegram_once_per_group(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send_telegram", lambda text, chat_ids=None: sent.append((text, chat_ids)) or [])
    settings.update({"telegram_bot_token": "1:T", "telegram_chat_ids": "-100", "notify_errors": True,
                     "error_chat_ids": "555"})
    notify._error_alerts.clear()
    applog.db_handler().on_new_error = notify.alert_error
    with applog.context(record=7):
        log.error("Claude недоступний")
        log.error("Claude недоступний")                                # повтор — лише лічильник
    log.warning("просто попередження")
    assert len(sent) == 1 and sent[0][1] == ["555"]
    assert "Claude недоступний" in sent[0][0] and version.label() in sent[0][0] and "record: 7" in sent[0][0]
    settings.update({"notify_errors": False})
    log.error("ще одна нова помилка")
    assert len(sent) == 1


def test_version_is_shown_in_sidebar_login_and_system(admin_client, client):
    expected = "v" + (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
    assert version.label() == expected
    assert 'class="app-version"' in admin_client.get("/").get_data(as_text=True)
    assert expected in admin_client.get("/").get_data(as_text=True)
    assert expected in admin_client.get("/admin/system").get_data(as_text=True)


def test_version_on_login_page(client):
    make_user()
    assert version.label() in client.get("/login").get_data(as_text=True)


def test_memory_usage_is_reported():
    assert background.memory_mb() > 0


def test_zoom_lists_account_wide_recordings_when_scope_granted(monkeypatch, zoom_env):
    paths = []
    monkeypatch.setattr(zoom, "_api_get", lambda path, params=None: paths.append(path) or {"meetings": []})
    monkeypatch.setattr(zoom, "get_access_token", lambda force_refresh=False: "t")
    from datetime import date
    monkeypatch.setitem(zoom._token_cache, "scopes", "cloud_recording:read:list_user_recordings:admin")
    zoom.list_recordings(date(2026, 9, 1), date(2026, 9, 10))
    monkeypatch.setitem(zoom._token_cache, "scopes",
                        "cloud_recording:read:list_user_recordings:admin cloud_recording:read:list_account_recordings:admin")
    zoom.list_recordings(date(2026, 9, 1), date(2026, 9, 10))
    assert paths == ["/users/me/recordings", "/accounts/me/recordings"]
    monkeypatch.setattr(zoom, "ZOOM_USER", "trainer@school.com")          # явний користувач — лише він
    zoom.list_recordings(date(2026, 9, 1), date(2026, 9, 10))
    assert paths[-1] == "/users/trainer@school.com/recordings"


def test_gunicorn_worker_crashes_are_moved_into_journal(tmp_path):
    path = tmp_path / "gunicorn-errors.log"
    path.write_text("2026-09-28 10:00:01 ERROR Worker (pid:123) was sent SIGKILL! Perhaps out of memory?\n"
                    "2026-09-28 10:05:09 CRITICAL WORKER TIMEOUT (pid:456)\n", encoding="utf-8")
    assert applog.ingest_server_errors(str(path)) == 2
    assert not path.exists() and applog.ingest_server_errors(str(path)) == 0   # файл забрано один раз
    messages = [r["message"] for r in _logs("errors")]
    assert any("Perhaps out of memory" in m for m in messages) and any("WORKER TIMEOUT" in m for m in messages)


def test_gunicorn_config_hooks(tmp_path, monkeypatch):
    import importlib.util
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location("gconf", Path(__file__).resolve().parent.parent / "gunicorn.conf.py")
    gconf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gconf)
    gconf.worker_abort(SimpleNamespace(pid=999))
    row = _logs("errors")[0]
    assert row["level"] == "CRITICAL" and "pid=999" in row["message"] and "MainThread" in row["details"]
    path = tmp_path / "errors.log"
    path.write_text("2026-09-28 11:00:00 ERROR Worker (pid:7) exited with code 1\n", encoding="utf-8")
    monkeypatch.setattr(gconf, "GUNICORN_ERRORS_FILE", str(path))
    gconf.post_worker_init(SimpleNamespace(pid=1000))
    assert any("exited with code 1" in r["message"] for r in _logs("errors"))
