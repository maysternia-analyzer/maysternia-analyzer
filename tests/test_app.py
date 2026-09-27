import hashlib
import hmac
import io
import json

import pytest

import database as db
from tests.conftest import (CSRF, login, make_user, post_json, sample_lesson_analysis, sample_sales_analysis,
                            set_csrf)
from tests.test_zoom import WEBHOOK_OBJECT


def _record(**kwargs):
    params = dict(record_date="2026-09-01", record_type="sales", person_name="Олена", filename="")
    params.update(kwargs)
    return db.create_record(params.pop("record_date"), params.pop("record_type"), params.pop("person_name"),
                            params.pop("filename"), **params)


# ── Службові ──────────────────────────────────────────────────────────────────

def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200 and resp.json == {"ok": True, "db": True}


@pytest.mark.parametrize("path", ["/debug/zoom-state", "/debug/zoom-account", "/debug/ffmpeg-check",
                                  "/debug/delete-record/1", "/debug/process-transcript", "/debug/process-vtt"])
def test_unauthenticated_debug_endpoints_are_gone(client, path):
    assert client.get(path).status_code == 404
    assert client.post(path).status_code in (400, 404)


def test_security_headers(client):
    resp = client.get("/healthz")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"


# ── Авторизація ───────────────────────────────────────────────────────────────

def test_login_redirects_to_setup_when_no_users(client):
    assert client.get("/login").headers["Location"].endswith("/setup")


def test_setup_creates_first_admin(client):
    set_csrf(client)
    resp = client.post("/setup", data={"email": "boss@x.com", "name": "Бос", "password": "password123",
                                       "secret": "test-secret-key-that-is-long-enough", "csrf_token": CSRF})
    assert resp.status_code == 302
    assert db.get_user_by_email("boss@x.com")["role"] == "admin"
    assert client.get("/setup").headers["Location"].endswith("/login")  # повторно не працює


def test_setup_rejects_wrong_secret_and_short_password(client):
    set_csrf(client)
    resp = client.post("/setup", data={"email": "boss@x.com", "name": "Бос", "password": "password123",
                                       "secret": "wrong", "csrf_token": CSRF})
    assert "Невірний секретний ключ" in resp.get_data(as_text=True)
    resp = client.post("/setup", data={"email": "boss@x.com", "name": "Бос", "password": "123",
                                       "secret": "test-secret-key-that-is-long-enough", "csrf_token": CSRF})
    assert "щонайменше" in resp.get_data(as_text=True)
    assert db.count_users() == 0


def test_setup_disabled_with_public_secret(client, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "maysternia-secret-key-2024")
    set_csrf(client)
    resp = client.post("/setup", data={"email": "boss@x.com", "name": "Бос", "password": "password123",
                                       "secret": "maysternia-secret-key-2024", "csrf_token": CSRF})
    assert db.count_users() == 0
    assert "SECRET_KEY" in resp.get_data(as_text=True)


def test_login_success_and_safe_next(client):
    make_user()
    set_csrf(client)
    resp = client.post("/login?next=/sales", data={"email": "ADMIN@example.com ", "password": "password123",
                                                   "csrf_token": CSRF})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/sales")
    assert client.get("/").status_code == 200


@pytest.mark.parametrize("target", ["https://evil.com", "//evil.com", "/\\evil.com", "javascript:alert(1)"])
def test_login_blocks_open_redirect(client, target):
    make_user()
    set_csrf(client)
    resp = client.post(f"/login?next={target}", data={"email": "admin@example.com", "password": "password123",
                                                      "csrf_token": CSRF})
    assert resp.status_code == 302 and resp.headers["Location"] == "/"


def test_login_wrong_password_and_rate_limit(client):
    make_user()
    set_csrf(client)
    for _ in range(10):
        resp = client.post("/login", data={"email": "admin@example.com", "password": "bad", "csrf_token": CSRF})
        assert resp.status_code == 401
    resp = client.post("/login", data={"email": "admin@example.com", "password": "password123", "csrf_token": CSRF})
    assert resp.status_code == 429


def test_login_requires_csrf(client):
    make_user()
    resp = client.post("/login", data={"email": "admin@example.com", "password": "password123"})
    assert resp.status_code == 400


def test_inactive_user_cannot_login_and_session_is_revoked(client):
    user = make_user(active=False)
    set_csrf(client)
    resp = client.post("/login", data={"email": "admin@example.com", "password": "password123", "csrf_token": CSRF})
    assert resp.status_code == 403 and "заблоковано" in resp.get_data(as_text=True)
    wrong = client.post("/login", data={"email": "admin@example.com", "password": "bad", "csrf_token": CSRF})
    assert wrong.status_code == 401 and "заблоковано" not in wrong.get_data(as_text=True)  # без пароля не видаємо
    login(client, user)  # навіть зі старою сесією доступу немає
    assert client.get("/").status_code == 302


def test_pages_require_login(client):
    make_user()
    for path in ("/", "/lessons", "/sales", "/stats", "/analytics", "/upload", "/admin/system"):
        resp = client.get(path)
        assert resp.status_code == 302 and "/login" in resp.headers["Location"], path


def test_logout_requires_post(admin_client, client):
    assert admin_client.get("/logout").status_code == 302
    assert admin_client.get("/").status_code == 200          # GET не розлогінює (захист від CSRF)
    assert admin_client.post("/logout").status_code == 400   # без CSRF-токена
    assert admin_client.post("/logout", data={"csrf_token": CSRF}).status_code == 302
    assert admin_client.get("/").status_code == 302
    anon = client.application.test_client()
    assert anon.get("/logout").headers["Location"].endswith("/login")  # без петлі ?next=/logout


def test_password_change_revokes_existing_sessions(client, flask_app):
    viewer = make_user(email="v@x.com", role="viewer")
    login(client, viewer)
    assert client.get("/").status_code == 200
    from werkzeug.security import generate_password_hash
    db.update_user(viewer["id"], password_hash=generate_password_hash("new-password-1"))
    assert client.get("/").status_code == 302   # стара сесія/remember-cookie більше не діє
    with client.session_transaction() as sess:
        sess["_user_id"] = str(viewer["id"])    # старий формат без мітки пароля
    assert client.get("/").status_code == 302


def test_ajax_without_session_gets_json_401(client):
    make_user()
    rid = _record()
    set_csrf(client)
    resp = post_json(client, f"/record/{rid}/comment", {"comment": "x"})
    assert resp.status_code == 401 and resp.json["ok"] is False
    assert client.get(f"/record/{rid}/status").status_code == 401


def test_request_size_limits(client, admin_client, monkeypatch):
    make_user(email="x@x.com")
    big = "a" * (3 * 1024 * 1024)
    anon = client.application.test_client()
    assert anon.post("/login", data={"email": big}).status_code == 413
    assert anon.post("/zoom/webhook", data=big, content_type="application/json").status_code == 413
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    resp = _upload(admin_client, "big.m4a", b"\x00\x00\x00\x20ftypM4A " + b"\x00" * (3 * 1024 * 1024))
    assert resp.status_code == 302   # авторизованим — до 500 МБ
    rec = db.get_record(int(resp.headers["Location"].rsplit("/", 1)[1]))
    import app as app_module
    (app_module.UPLOAD_FOLDER / rec["filename"]).unlink()


def test_login_unknown_email_and_email_limiter(client):
    make_user()
    set_csrf(client)
    assert client.post("/login", data={"email": "nobody@x.com", "password": "x", "csrf_token": CSRF}).status_code == 401
    import app as app_module
    for _ in range(30):
        app_module.email_limiter.record_failure("email:admin@example.com")
    resp = client.post("/login", data={"email": "admin@example.com", "password": "password123", "csrf_token": CSRF})
    assert resp.status_code == 429


# ── CSRF ──────────────────────────────────────────────────────────────────────

def test_json_post_without_csrf_is_rejected(admin_client):
    rid = _record()
    resp = admin_client.post(f"/record/{rid}/comment", json={"comment": "x"})
    assert resp.status_code == 400
    resp = admin_client.post(f"/record/{rid}/comment", json={"comment": "x"}, headers={"X-CSRFToken": "wrong"})
    assert resp.status_code == 400 and resp.json["ok"] is False
    assert post_json(admin_client, f"/record/{rid}/comment", {"comment": "ok"}).json == {"ok": True}


def test_form_post_without_csrf_is_rejected(admin_client):
    rid = _record()
    assert admin_client.post(f"/record/{rid}/delete").status_code == 400
    assert db.get_record(rid)


# ── Сторінки ──────────────────────────────────────────────────────────────────

def _seed_varied_records():
    ids = {}
    ids["lesson"] = _record(record_type="lesson", person_name="Мирослава", status="done")
    db.update_record(ids["lesson"], analysis=sample_lesson_analysis(), transcription="Мирослава: привіт")
    ids["sale"] = _record(status="done", trainer_name="Мирослава")
    db.update_record(ids["sale"], analysis=sample_sales_analysis(chance="Високий"), transcription="Олена: день")
    db.update_sale_result(ids["sale"], True, 25000)
    ids["legacy_nulls"] = _record(status="done")
    db.update_record(ids["legacy_nulls"], analysis_json=json.dumps(
        {"checklist_score": None, "deal_chance": "Високий", "deal_chance_percent": None,
         "need_identified": None, "top_mistakes": None}))
    ids["queued"] = _record(status="queued", source="zoom", zoom_meeting_uuid="u1",
                            not_before=db.utcnow_iso(__import__("datetime").timedelta(minutes=10)))
    ids["processing"] = _record(status="processing")
    ids["legacy_error"] = _record(status="error", filename="zoom_1.vtt")
    db.update_record(ids["legacy_error"], transcription="[ПОМИЛКА VTT]: 401")
    ids["analysis_error"] = _record(status="error", source="text")
    db.update_record(ids["analysis_error"], transcription="Олена: текст\n[ПОМИЛКА аналізу]: overloaded")
    ids["stale"] = _record(record_type="lesson", status="done")
    db.update_record(ids["stale"], analysis=sample_sales_analysis(), transcription="т")
    ids["empty_name"] = _record(person_name="", status="done", record_type="lesson")
    db.update_record(ids["empty_name"], analysis=sample_lesson_analysis(10))
    return ids


def test_all_pages_render_with_varied_data(admin_client):
    ids = _seed_varied_records()
    for path in ("/", "/?type=sales", "/?type=bogus&date_from=xx", "/?page=2", "/?page=abc", "/lessons", "/sales",
                 "/sales?trainer=Мирослава", "/team", "/team?period=all", "/team?date_from=2026-08-01&date_to=2026-09-30",
                 "/person?name=Олена", "/person?name=Мирослава", "/search?q=день", "/search?q=x", "/analytics", "/upload",
                 "/admin/users", "/admin/system", "/admin/settings", "/admin/checklists/sales",
                 "/admin/checklists/lesson", "/export.csv", "/export.csv?type=sales"):
        resp = admin_client.get(path)
        assert resp.status_code == 200, path
    assert admin_client.get("/stats").status_code == 302
    for name, rid in ids.items():
        resp = admin_client.get(f"/record/{rid}")
        assert resp.status_code == 200, name
    html = admin_client.get(f"/record/{ids['queued']}").get_data(as_text=True)
    assert "Очікуємо транскрипцію від Zoom" in html
    html = admin_client.get(f"/record/{ids['analysis_error']}").get_data(as_text=True)
    assert "Проаналізувати повторно" in html and "[ПОМИЛКА аналізу]" not in html
    html = admin_client.get(f"/record/{ids['stale']}").get_data(as_text=True)
    assert "іншого типу" in html
    html = admin_client.get("/sales").get_data(as_text=True)
    assert "25 000" in html


def test_dashboard_stats_are_computed(admin_client):
    _seed_varied_records()
    import app as app_module
    stats = app_module.build_stats(db.get_all_records())
    assert stats["sold"] == 1 and stats["revenue"] == 25000
    assert stats["in_progress"] == 2 and stats["errors"] == 2
    assert stats["ai_high_share"] == 100 and stats["conversion_real"] == 100
    assert dict(stats["by_person"])["Олена"] == 30   # (60 + 0) / 2 — null не ламає статистику


def test_record_404_and_status_json(admin_client):
    assert admin_client.get("/record/999").status_code == 404
    rid = _record(status="queued", not_before=db.utcnow_iso(__import__("datetime").timedelta(minutes=5)))
    data = admin_client.get(f"/record/{rid}/status").json
    assert data["status"] == "queued" and data["waiting"] is True
    assert admin_client.get("/record/999/status").status_code == 404


def test_viewer_has_no_admin_access(viewer_client):
    assert viewer_client.get("/admin/users").status_code == 403
    assert viewer_client.get("/admin/system").status_code == 403
    rid = _record()
    resp = viewer_client.post(f"/record/{rid}/delete", data={"csrf_token": CSRF})
    assert resp.status_code == 403 and db.get_record(rid)
    html = viewer_client.get(f"/record/{rid}").get_data(as_text=True)
    assert "Видалити" not in html


# ── Дії з записом ─────────────────────────────────────────────────────────────

def test_sale_result_flow(admin_client):
    rid = _record()
    url = f"/record/{rid}/sale_result"
    assert post_json(admin_client, url, {"sale_made": True, "sale_amount": "15000"}).json["ok"]
    assert post_json(admin_client, url, {"sale_made": True}).json["ok"]   # клік «Продано» не стирає суму
    assert db.get_record(rid)["sale_amount"] == 15000
    assert post_json(admin_client, url, {"sale_made": None, "sale_amount": None}).json["ok"]
    rec = db.get_record(rid)
    assert rec["sale_made"] is None and rec["sale_amount"] is None
    assert post_json(admin_client, url, {"sale_made": "так"}).status_code == 400
    assert post_json(admin_client, url, {"sale_made": True, "sale_amount": -1}).status_code == 400
    assert post_json(admin_client, url, {"sale_made": True, "sale_amount": "abc"}).status_code == 400
    assert post_json(admin_client, "/record/999/sale_result", {"sale_made": True}).status_code == 404
    # Некоректне тіло запиту не повинне давати 500
    resp = admin_client.post(url, data="not json", headers={"X-CSRFToken": CSRF, "Content-Type": "application/json"})
    assert resp.status_code == 400


def test_meta_update_triggers_reanalysis_on_type_change(admin_client):
    rid = _record(status="done", transcription="Олена: текст", auto_detect=True)
    db.update_record(rid, analysis=sample_sales_analysis())
    resp = post_json(admin_client, f"/record/{rid}/meta", {"record_type": "sales", "person_name": " Олена К "})
    assert resp.json == {"ok": True, "reanalyzing": False}
    resp = post_json(admin_client, f"/record/{rid}/meta", {"record_type": "lesson", "person_name": "Олена К"})
    assert resp.json == {"ok": True, "reanalyzing": True}
    rec = db.get_record(rid)
    assert (rec["record_type"], rec["person_name"], rec["status"], rec["job_kind"], rec["auto_detect"]) == \
        ("lesson", "Олена К", "queued", "analyze", 0)
    assert post_json(admin_client, f"/record/{rid}/meta", {"record_type": "webinar", "person_name": "x"}).status_code == 400
    assert post_json(admin_client, f"/record/{rid}/meta", {"record_type": "sales", "person_name": " "}).status_code == 400


def test_comment_is_saved_and_limited(admin_client):
    rid = _record()
    post_json(admin_client, f"/record/{rid}/comment", {"comment": "x" * 20000})
    assert len(db.get_record(rid)["manager_comment"]) == 10000


def test_reanalyze_modes(admin_client, zoom_env, tmp_path, monkeypatch):
    with_text = _record(status="error", transcription="Олена: текст", source="text")
    assert post_json(admin_client, f"/record/{with_text}/reanalyze").json == {"ok": True, "action": "analyze"}
    assert db.get_record(with_text)["status"] == "queued"

    busy = _record(status="processing")
    assert post_json(admin_client, f"/record/{busy}/reanalyze").status_code == 409

    no_file = _record(status="error", source="upload", filename="missing.m4a")
    resp = post_json(admin_client, f"/record/{no_file}/reanalyze")
    assert resp.status_code == 400 and "Railway" in resp.json["error"]

    zoom_rec = _record(status="error", source="zoom", filename="zoom_1.vtt")
    db.update_record(zoom_rec, transcription="[ПОМИЛКА]: 401")
    assert post_json(admin_client, f"/record/{zoom_rec}/reanalyze").json["action"] == "full"

    text_only = _record(status="error", source="text")
    assert post_json(admin_client, f"/record/{text_only}/reanalyze").status_code == 400
    assert post_json(admin_client, f"/record/{with_text}/reanalyze", {"mode": "analyze"}).json["ok"]


def test_delete_record_removes_uploaded_file(admin_client):
    import app as app_module
    path = app_module.UPLOAD_FOLDER / "test-delete.m4a"
    path.write_bytes(b"x")
    rid = _record(filename="test-delete.m4a", source="upload")
    resp = admin_client.post(f"/record/{rid}/delete", data={"csrf_token": CSRF})
    assert resp.status_code == 302
    assert db.get_record(rid) is None and not path.exists()
    assert admin_client.post(f"/record/{rid}/delete", data={"csrf_token": CSRF}).status_code == 404


def test_small_bad_inputs_do_not_500(admin_client):
    assert admin_client.get("/uploads/x%00.mp3").status_code in (400, 404)
    rid = _record()
    assert post_json(admin_client, f"/record/{rid}/meta", {"record_type": "sales", "person_name": 5}).status_code == 400
    assert post_json(admin_client, f"/record/{rid}/meta", {"record_type": ["sales"], "person_name": "x"}).status_code == 400
    assert admin_client.get("/?date_from=20260101").status_code == 200
    import app as app_module
    assert not app_module._valid_date("20260927") and not app_module._valid_date("2026W391")
    assert app_module._valid_date("2026-09-27")


def test_reanalyze_race_with_running_job_returns_409(admin_client):
    rid = _record(status="done", transcription="Олена: текст")
    original = db.enqueue_record
    import app as app_module

    def claimed_meanwhile(record_id, job_kind="full", not_before=None):
        db.update_record(record_id, status="processing")   # воркер встиг узяти задачу
        return original(record_id, job_kind, not_before)

    app_module.db.enqueue_record = claimed_meanwhile
    try:
        assert post_json(admin_client, f"/record/{rid}/reanalyze").status_code == 409
    finally:
        app_module.db.enqueue_record = original
    assert db.get_record(rid)["status"] == "processing"


def test_uploads_require_login_and_block_traversal(client, admin_client):
    import app as app_module
    (app_module.UPLOAD_FOLDER / "test-serve.mp3").write_bytes(b"ID3data")
    try:
        assert admin_client.get("/uploads/test-serve.mp3").data == b"ID3data"
        assert admin_client.get("/uploads/../app.py").status_code == 404
        assert admin_client.get("/uploads/%2e%2e/app.py").status_code == 404
        anon = app_module.app.test_client()
        assert anon.get("/uploads/test-serve.mp3").status_code == 302
    finally:
        (app_module.UPLOAD_FOLDER / "test-serve.mp3").unlink(missing_ok=True)


# ── Завантаження ──────────────────────────────────────────────────────────────

def _upload(client, filename, content, **fields):
    data = {"record_type": "sales", "person_name": "Олена", "record_date": "2026-09-01",
            "record_time": "14:30", "trainer_name": "Мирослава", "csrf_token": CSRF}
    data.update(fields)
    data["file"] = (io.BytesIO(content), filename)
    return client.post("/upload", data=data, content_type="multipart/form-data")


def test_upload_vtt_with_cyrillic_name_is_analyzed_directly(admin_client):
    vtt = "WEBVTT\n\n00:01.000 --> 00:02.000\nОлена: Добрий день\n".encode("utf-8")
    resp = _upload(admin_client, "Запис дзвінка.vtt", vtt)
    assert resp.status_code == 302
    rec = db.get_record(int(resp.headers["Location"].rsplit("/", 1)[1]))
    assert rec["transcription"] == "[00:00:01] Олена: Добрий день"
    assert (rec["status"], rec["job_kind"], rec["source"]) == ("queued", "analyze", "text")
    assert (rec["record_time"], rec["trainer_name"]) == ("14:30", "Мирослава")
    assert json.loads(rec["source_json"])["original_name"] == "Запис дзвінка.vtt"


def test_upload_media_saved_with_safe_name(admin_client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    resp = _upload(admin_client, "Урок №1.m4a", b"\x00\x00\x00\x20ftypM4A ", record_type="lesson")
    assert resp.status_code == 302
    rec = db.get_record(int(resp.headers["Location"].rsplit("/", 1)[1]))
    # secure_filename("Урок №1.m4a") давав "1.m4a"/"m4a" без розширення → Whisper падав
    assert rec["filename"].endswith(".m4a") and len(rec["filename"]) == 36
    assert rec["trainer_name"] == ""   # тренер лише для продажів
    import app as app_module
    (app_module.UPLOAD_FOLDER / rec["filename"]).unlink()


def test_upload_media_without_openai_is_rejected(admin_client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    resp = _upload(admin_client, "a.m4a", b"data")
    assert resp.status_code == 400 and "VTT" in resp.get_data(as_text=True)
    assert db.count_records_by_status() == {}


@pytest.mark.parametrize("filename,fields,error", [
    ("a.exe", {}, "Непідтримуваний"),
    ("a.vtt", {"record_type": "webinar"}, "тип"),
    ("a.vtt", {"person_name": " "}, "імʼя"),
    ("a.vtt", {"record_date": "31-12-2026"}, "дату"),
    ("a.vtt", {"record_time": "25:99"}, "час"),
])
def test_upload_validation(admin_client, filename, fields, error):
    resp = _upload(admin_client, filename, b"WEBVTT", **fields)
    assert resp.status_code == 400 and error in resp.get_data(as_text=True)


def test_upload_empty_transcript(admin_client):
    resp = _upload(admin_client, "a.txt", b"   \n  ")
    assert resp.status_code == 400 and "порожній" in resp.get_data(as_text=True)


# ── Адміністрування ───────────────────────────────────────────────────────────

def test_admin_user_management(admin_client):
    form = {"email": "new@x.com", "name": "Новий", "password": "password123", "role": "viewer", "csrf_token": CSRF}
    admin_client.post("/admin/users/create", data=form)
    new = db.get_user_by_email("new@x.com")
    assert new and new["role"] == "viewer"
    admin_client.post("/admin/users/create", data=dict(form, email="short@x.com", password="123"))
    assert db.get_user_by_email("short@x.com") is None
    admin_client.post("/admin/users/create", data=dict(form, email="bad@x.com", role="root"))
    assert db.get_user_by_email("bad@x.com") is None

    admin_client.post(f"/admin/users/{new['id']}/role", data={"role": "admin", "csrf_token": CSRF})
    assert db.get_user_by_id(new["id"])["role"] == "admin"
    admin_client.post(f"/admin/users/{new['id']}/toggle", data={"csrf_token": CSRF})
    assert db.get_user_by_id(new["id"])["is_active"] == 0
    admin_client.post(f"/admin/users/{new['id']}/password", data={"password": "newpassword1", "csrf_token": CSRF})
    admin_client.post(f"/admin/users/{new['id']}/delete", data={"csrf_token": CSRF})
    assert db.get_user_by_id(new["id"]) is None


def test_admin_cannot_remove_self_or_last_admin(admin_client):
    me = admin_client.user
    admin_client.post(f"/admin/users/{me['id']}/toggle", data={"csrf_token": CSRF})
    admin_client.post(f"/admin/users/{me['id']}/delete", data={"csrf_token": CSRF})
    admin_client.post(f"/admin/users/{me['id']}/role", data={"role": "viewer", "csrf_token": CSRF})
    user = db.get_user_by_id(me["id"])
    assert user and user["is_active"] == 1 and user["role"] == "admin"


def test_admin_user_name_is_not_injected_into_js(admin_client):
    make_user(email="evil@x.com", role="viewer", name="x');alert(1);//")
    html = admin_client.get("/admin/users").get_data(as_text=True)
    # Раніше імʼя підставлялося в onsubmit="return confirm('…{{ u.name }}')" → XSS
    assert "onsubmit=" not in html and "onclick=" not in html
    assert "x&#39;);alert(1);//" in html and "x');alert(1)" not in html


def test_admin_system_poll(admin_client, zoom_env, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, "poll_once", lambda days: {"meetings": 2, "queued": 1, "duplicate": 1})
    resp = admin_client.post("/admin/system/poll", data={"days": "7", "csrf_token": CSRF}, follow_redirects=True)
    assert "нових у черзі 1" in resp.get_data(as_text=True)


def test_admin_system_check(admin_client, monkeypatch):
    from services import health
    monkeypatch.setattr(health, "run_all_checks", lambda: {"Zoom": {"ok": False, "detail": "немає ключів"}})
    resp = admin_client.post("/admin/system/check", data={"csrf_token": CSRF}, follow_redirects=True)
    assert "немає ключів" in resp.get_data(as_text=True)


def test_webhook_logs_endpoint(admin_client):
    db.log_webhook("recording.completed", "queued", "ok")
    assert admin_client.get("/admin/webhook-logs").json[0]["status"] == "queued"


# ── Аналітика ─────────────────────────────────────────────────────────────────

def test_analytics_generate(admin_client, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, "generate_insights", lambda records: {"summary": f"{len(records)} записів"})
    _record(status="done")
    resp = post_json(admin_client, "/analytics/generate", {"date_from": "2026-01-01", "date_to": "bad"})
    assert resp.json == {"ok": True}
    assert db.get_insights("2026-01-01", "")["data"]["summary"] == "1 записів"
    html = admin_client.get("/analytics?date_from=2026-01-01").get_data(as_text=True)
    assert "1 записів" in html


def test_analytics_generate_error_and_lock(admin_client, monkeypatch):
    import app as app_module

    def boom(records):
        raise RuntimeError("Claude недоступний")

    monkeypatch.setattr(app_module, "generate_insights", boom)
    resp = post_json(admin_client, "/analytics/generate")
    assert resp.status_code == 500 and "Claude недоступний" in resp.json["error"]
    app_module._insights_lock.acquire()
    try:
        assert post_json(admin_client, "/analytics/generate").status_code == 409
    finally:
        app_module._insights_lock.release()


# ── Zoom webhook ──────────────────────────────────────────────────────────────

def _signed_post(client, payload, secret="whsecret", timestamp="1700000000"):
    body = json.dumps(payload).encode()
    signature = "v0=" + hmac.new(secret.encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256).hexdigest()
    return client.post("/zoom/webhook", data=body, content_type="application/json",
                       headers={"x-zm-request-timestamp": timestamp, "x-zm-signature": signature})


def test_webhook_url_validation(client, zoom_env):
    resp = _signed_post(client, {"event": "endpoint.url_validation", "payload": {"plainToken": "abc"}})
    assert resp.json["plainToken"] == "abc"
    assert resp.json["encryptedToken"] == hmac.new(b"whsecret", b"abc", hashlib.sha256).hexdigest()
    assert _signed_post(client, {"event": "endpoint.url_validation", "payload": {}}).status_code == 400
    assert _signed_post(client, {"event": "endpoint.url_validation",
                                 "payload": {"plainToken": "v0:1:{\"x\":1}"}}).status_code == 400


def test_webhook_validation_cannot_be_used_as_signing_oracle(client, zoom_env):
    """Раніше непідписаний url_validation повертав HMAC довільного рядка = підпис будь-якої події."""
    forged = json.dumps({"event": "recording.completed", "payload": {"object": WEBHOOK_OBJECT}})
    oracle = client.post("/zoom/webhook", json={"event": "endpoint.url_validation",
                                                "payload": {"plainToken": f"v0:1700000000:{forged}"}})
    assert oracle.status_code == 401
    assert db.fetch_one("SELECT COUNT(*) AS n FROM records")["n"] == 0


def test_webhook_rejects_bad_signature(client, zoom_env):
    resp = _signed_post(client, {"event": "recording.completed"}, secret="wrong")
    assert resp.status_code == 401
    assert client.post("/zoom/webhook", json={"event": "recording.completed"}).status_code == 401
    assert db.get_webhook_logs()[0]["status"] == "error_bad_signature"


def test_webhook_without_secret_configured(client, monkeypatch):
    monkeypatch.delenv("ZOOM_WEBHOOK_SECRET", raising=False)
    assert client.post("/zoom/webhook", json={"event": "recording.completed"}).status_code == 503


def test_webhook_recording_completed_queues_once(client, zoom_env):
    payload = {"event": "recording.completed", "payload": {"object": WEBHOOK_OBJECT}}
    first = _signed_post(client, payload)
    assert first.status_code == 200 and first.json["result"] == "queued"
    second = _signed_post(client, payload)
    assert second.json["result"] == "duplicate"
    assert db.fetch_one("SELECT COUNT(*) AS n FROM records")["n"] == 1
    assert [log["status"] for log in db.get_webhook_logs()] == ["duplicate", "queued"]


def test_webhook_ignores_other_events(client, zoom_env):
    resp = _signed_post(client, {"event": "meeting.started", "payload": {}})
    assert resp.json == {"ok": True}
    assert db.get_webhook_logs()[0]["status"] == "ignored"


def test_webhook_malformed_payload_does_not_crash(client, zoom_env):
    resp = _signed_post(client, {"event": "recording.completed", "payload": {"object": {"recording_files": "bad"}}})
    assert resp.status_code == 200 and resp.json["result"] in ("no_uuid", "no_files")
    resp = _signed_post(client, {"event": "recording.completed",
                                 "payload": {"object": {**WEBHOOK_OBJECT, "recording_files": None, "duration": "x"}}})
    assert resp.status_code == 200
    assert _signed_post(client, [1, 2]).status_code == 400
    assert client.post("/zoom/webhook", data="not json", content_type="application/json").status_code == 400


def test_webhook_bad_signature_logging_is_throttled(client, zoom_env):
    for _ in range(5):
        _signed_post(client, {"event": "recording.completed"}, secret="wrong")
    assert len(db.get_webhook_logs()) == 1
