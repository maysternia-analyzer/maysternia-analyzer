"""Тести функцій рівня «SalesUp»: налаштування, чек-листи, сповіщення, команда, ролі, експорт."""
import csv
import hashlib
import hmac
import io
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

import database as db
from services import checklists, coaching, notify, pipeline, settings, team
from services.timeutil import LOCAL_TZ
from tests.conftest import (CSRF, login, make_user, post_json, sample_lesson_analysis,
                            sample_sales_analysis)


@pytest.fixture(autouse=True)
def fresh_settings():
    settings.invalidate_cache()
    yield
    settings.invalidate_cache()


def _done(person="Олена", kind="sales", day="2026-09-10", analysis=None, **fields):
    rid = db.create_record(day, kind, person, "", source="text", status="done", transcription="Олена: текст", **fields)
    db.update_record(rid, analysis=analysis or (sample_sales_analysis() if kind == "sales" else sample_lesson_analysis()))
    return rid


# ── Налаштування ──────────────────────────────────────────────────────────────

def test_settings_defaults_and_validation():
    config = settings.get_all()
    assert config["low_score_threshold"] == 50 and config["daily_digest_time"] == "19:00"
    settings.update({"low_score_threshold": "35", "telegram_chat_ids": "-1001234, 555666 @team_chat",
                     "notify_on_done": False, "daily_digest_time": "08:30"})
    config = settings.get_all()
    assert config["low_score_threshold"] == 35 and config["notify_on_done"] is False
    assert settings.chat_ids() == ["-1001234", "555666", "@team_chat"]
    for bad in ({"low_score_threshold": "101"}, {"daily_digest_time": "25:00"}, {"webhook_url": "http://x.com"},
                {"telegram_chat_ids": "abc"}, {"anthropic_model": "bad model!"}):
        with pytest.raises(settings.SettingsError):
            settings.update(bad)


def test_secret_settings_are_kept_unless_cleared():
    settings.update({"telegram_bot_token": "123:SECRET"})
    settings.update({"telegram_bot_token": ""})           # порожнє поле форми не затирає
    assert settings.get("telegram_bot_token") == "123:SECRET"
    settings.update({"telegram_bot_token": "", "clear_telegram_bot_token": True})
    assert settings.get("telegram_bot_token") == ""
    assert settings.mask("1234567890abcdefXYZ") == "••••XYZ" and settings.mask("short") == "••••"


def test_model_setting_overrides_env(monkeypatch):
    from services import llm
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-env-model")
    assert llm.model() == "claude-env-model"
    settings.update({"anthropic_model": "claude-sonnet-5"})
    assert llm.model() == "claude-sonnet-5"


# ── Чек-листи ─────────────────────────────────────────────────────────────────

def test_checklist_validation_and_keys():
    saved = checklists.save_checklist("sales", {"criteria": [
        {"key": "need_identified", "title": "Потреба", "description": "", "weight": "3"},
        {"key": "summary", "title": "Зарезервований ключ", "weight": 1},       # ключ зайнятий → новий
        {"key": "", "title": "  ", "weight": 1},                                 # порожній рядок → пропуск
        {"title": "Новий критерій", "weight": 1},
    ], "instructions": "  Скрипт  "})
    keys = [c["key"] for c in saved["criteria"]]
    assert keys[0] == "need_identified" and len(keys) == 3 and len(set(keys)) == 3
    assert all(k.startswith("c_") for k in keys[1:])
    assert saved["criteria"][0]["description"] == "Потреба" and saved["instructions"] == "Скрипт"
    assert checklists.get_checklist("sales")["criteria"][0]["weight"] == 3
    checklists.reset_checklist("sales")
    assert checklists.get_checklist("sales")["criteria"] == checklists.DEFAULTS["sales"]["criteria"]


@pytest.mark.parametrize("data,message", [
    ({"criteria": []}, "хоча б один"),
    ({"criteria": [{"title": "x", "weight": 11}]}, "від 1 до 10"),
    ({"criteria": [{"title": "x", "weight": "abc"}]}, "числом"),
    ({"criteria": [{"title": "x" * 121}]}, "задовга"),
    ({"criteria": [{"title": f"c{i}"} for i in range(21)]}, "Максимум"),
])
def test_checklist_validation_errors(data, message):
    with pytest.raises(checklists.ChecklistError, match=message):
        checklists.validate("sales", data)


# ── Сповіщення ────────────────────────────────────────────────────────────────

class FakeRequests:
    def __init__(self, status=200, body=None):
        self.calls = []
        self.status = status
        self.body = body or {"ok": True}

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        payload = json.dumps(self.body).encode()
        return SimpleNamespace(status_code=self.status, content=payload, text=payload.decode(),
                               json=lambda: self.body)


@pytest.fixture
def telegram(monkeypatch):
    fake = FakeRequests()
    monkeypatch.setattr(notify.requests, "post", fake.post)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://analyzer.example.com")
    settings.update({"telegram_bot_token": "111:TOKEN", "telegram_chat_ids": "-100500"})
    return fake


def test_telegram_message_escapes_html(telegram):
    rid = _done(person="<b>Хакер</b> & Co")
    notify.notify_record_done(rid)
    text = telegram.calls[0]["json"]["text"]
    assert "&lt;b&gt;Хакер&lt;/b&gt; &amp; Co" in text and "<b>Хакер" not in text
    assert f"https://analyzer.example.com/record/{rid}" in text
    assert telegram.calls[0]["json"]["parse_mode"] == "HTML"
    assert "111:TOKEN" in telegram.calls[0]["url"]


def test_low_score_alert_only_mode(telegram):
    settings.update({"notify_on_done": False, "notify_low_score": True, "low_score_threshold": 50})
    good = _done(analysis=sample_sales_analysis(score=80))
    bad = _done(analysis=sample_sales_analysis(score=20))
    notify.notify_record_done(good)
    assert telegram.calls == []
    notify.notify_record_done(bad)
    assert "Низька оцінка" in telegram.calls[0]["json"]["text"]


def test_telegram_errors_are_reported_not_raised(monkeypatch):
    settings.update({"telegram_bot_token": "111:TOKEN", "telegram_chat_ids": "-100"})
    monkeypatch.setattr(notify.requests, "post",
                        FakeRequests(status=400, body={"ok": False, "description": "chat not found"}).post)
    assert notify.send_telegram("x") == ["-100: chat not found"]

    def boom(*a, **k):
        raise notify.requests.ConnectionError("https://api.telegram.org/bot111:TOKEN/sendMessage refused")

    monkeypatch.setattr(notify.requests, "post", boom)
    errors = notify.send_telegram("x")
    assert errors and "111:TOKEN" not in errors[0]   # токен не потрапляє в логи/інтерфейс
    assert notify.send_telegram("x", chat_ids=[]) == ["Telegram не налаштовано (токен бота або chat id)"]


def test_webhook_payload_and_signature(monkeypatch):
    fake = FakeRequests()
    monkeypatch.setattr(notify.requests, "post", fake.post)
    monkeypatch.setattr(notify, "_is_public_host", lambda url: True)
    settings.update({"webhook_url": "https://hooks.example.com/x", "webhook_secret": "s3cret"})
    rid = _done()
    notify.notify_record_done(rid)
    call = fake.calls[0]
    body = call["data"]
    timestamp = call["headers"]["X-Maysternia-Timestamp"]
    expected = hmac.new(b"s3cret", timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    assert call["headers"]["X-Maysternia-Signature"] == "sha256=" + expected
    payload = json.loads(body)
    assert payload["event"] == "record.analyzed" and payload["record"]["id"] == rid
    assert payload["record"]["objections"][0]["category"] == "ціна"
    assert call["allow_redirects"] is False


def test_daily_digest_sent_once_after_time(telegram):
    settings.update({"daily_digest_enabled": True, "daily_digest_time": "19:00"})
    today = datetime.now(LOCAL_TZ).date().isoformat()
    _done(day=today, analysis=sample_sales_analysis(score=20))
    _done(person="Ігор", day=today, analysis=sample_sales_analysis(score=90))
    early = datetime.now(LOCAL_TZ).replace(hour=18, minute=59)
    late = early.replace(hour=19, minute=1)
    assert notify.send_daily_digest_if_due(early) is False
    assert notify.send_daily_digest_if_due(late) is True
    assert notify.send_daily_digest_if_due(late) is False      # вдруге того ж дня — ні
    text = telegram.calls[0]["json"]["text"]
    assert "Звіт за" in text and "Ігор — 90%" in text and "Нижче 50%" in text and "ціна ×2" in text


def test_digest_empty_day():
    assert notify.build_daily_digest("2000-01-01") is None


# ── Конвеєр: сповіщення та масовий переаналіз ─────────────────────────────────

def test_pipeline_notifies_except_bulk_reanalysis(monkeypatch):
    sent = []
    monkeypatch.setattr(pipeline, "analyze", lambda t, text: sample_sales_analysis())
    monkeypatch.setattr(pipeline.notify, "notify_record_done", lambda rid: sent.append(rid))
    rid = db.create_record("2026-09-01", "sales", "Олена", "", source="text", job_kind="analyze", transcription="т")
    pipeline.process_record(db.claim_next_job())
    assert sent == [rid]
    assert db.enqueue_reanalysis("sales", "2026-09-01", "2026-09-01") == 1
    pipeline.process_record(db.claim_next_job())
    assert sent == [rid]                                       # переаналіз — без сповіщень
    assert db.get_record(rid)["status"] == "done"


def test_bulk_reanalysis_selects_only_records_with_transcript():
    ok = _done(day="2026-09-05")
    db.create_record("2026-09-05", "sales", "X", "a.m4a", status="error")         # без транскрипції
    _done(day="2026-08-01")                                                        # поза періодом
    _done(kind="lesson", day="2026-09-05")                                          # інший тип
    assert db.count_reanalysis_candidates("sales", "2026-09-01", "2026-09-30") == 1
    assert db.enqueue_reanalysis("sales", "2026-09-01", "2026-09-30") == 1
    assert db.get_record(ok)["job_kind"] == "reanalyze"


# ── Командна аналітика ────────────────────────────────────────────────────────

def test_team_rating_trend_and_rates():
    for score, day in ((80, "2026-09-10"), (60, "2026-09-11")):
        _done(analysis=sample_sales_analysis(score=score), day=day)
    _done(analysis=sample_sales_analysis(score=40), day="2026-08-10")
    _done(person="Мирослава", kind="lesson", day="2026-09-10", analysis=sample_lesson_analysis(90))
    current = db.get_all_records(date_from="2026-09-01", date_to="2026-09-30")
    previous = db.get_all_records(date_from="2026-08-02", date_to="2026-08-31")
    assert team.previous_period("2026-09-01", "2026-09-30") == ("2026-08-02", "2026-08-31")
    rating = {p["name"]: p for p in team.people_rating(current, previous)}
    assert rating["Олена"]["avg_score"] == 70 and rating["Олена"]["trend"] == 30
    assert rating["Мирослава"]["avg_score"] == 90 and rating["Мирослава"]["trend"] is None
    rates = team.criteria_rates(current, "sales")
    assert rates[0]["rate"] == 0 and rates[-1]["title"] == "Виявив потребу клієнта" and rates[-1]["rate"] == 100
    assert team.objection_stats(current)[0] == {"category": "ціна", "count": 2, "handled_rate": 0, "example": "Дорого"}
    assert team.risk_stats(current)[0]["count"] == 2


def test_person_profile_and_sparkline():
    for score, day in ((50, "2026-09-01"), (90, "2026-09-02")):
        _done(analysis=sample_sales_analysis(score=score), day=day)
    profile = team.person_profile(db.get_all_records(person_name="Олена"))
    assert [p["score"] for p in profile["series"]] == [50, 90]
    points = team.sparkline_points(profile["series"])
    assert points[0]["x"] < points[1]["x"] and points[0]["y"] > points[1]["y"]
    assert profile["phrases"][0]["phrase"].startswith("Давайте")


def test_coaching_plan_is_generated_and_cached(monkeypatch):
    _done()
    seen = {}

    def fake_call_json(system, user, schema, max_tokens=0):
        seen["user"] = user
        return {"summary": "Добре", "strengths": ["емпатія"], "growth_areas": [], "focus_next_week": "ціна",
                "phrases": ["фраза"]}

    monkeypatch.setattr(coaching, "call_json", fake_call_json)
    plan = coaching.generate("Олена", db.get_all_records(person_name="Олена"))
    assert plan["based_on"] == 1 and coaching.get_cached("Олена")["focus_next_week"] == "ціна"
    assert "Виявив потребу клієнта" not in json.loads(seen["user"].split("\n", 1)[1].rsplit("\n", 1)[0])["calls"][0]["failed"]
    with pytest.raises(ValueError):
        coaching.generate("Ніхто", [])


# ── Веб: ролі, налаштування, чек-листи, експорт, пошук ────────────────────────

@pytest.fixture
def manager_client(client):
    user = make_user(email="m@x.com", role="manager", name="Олена К")
    db.update_user(user["id"], person_name="Олена")
    user = db.get_user_by_email("m@x.com")
    login(client, user)
    return client


def test_manager_sees_only_own_records(manager_client):
    own = _done(person="Олена")
    other = _done(person="Ігор")
    html = manager_client.get("/").get_data(as_text=True)
    assert f"/record/{own}" in html and f"/record/{other}" not in html
    assert manager_client.get(f"/record/{own}").status_code == 200
    assert manager_client.get(f"/record/{other}").status_code == 404
    assert manager_client.get(f"/record/{other}/status").status_code == 404
    sales_html = manager_client.get("/sales?manager=Ігор").get_data(as_text=True)
    assert f"/record/{own}" in sales_html and f"/record/{other}" not in sales_html
    assert manager_client.get("/person?name=Олена").status_code == 200
    assert manager_client.get("/person?name=Ігор").status_code == 404
    csv_text = manager_client.get("/export.csv?person=Ігор").get_data(as_text=True)
    assert "Ігор" not in csv_text and "Олена" in csv_text
    assert "Ігор" not in manager_client.get("/search?q=текст").get_data(as_text=True)


def test_manager_cannot_modify_or_see_staff_pages(manager_client):
    own = _done(person="Олена")
    for path in ("/team", "/analytics", "/upload", "/admin/settings", "/admin/checklists/sales"):
        assert manager_client.get(path).status_code == 403, path
    for url, body in ((f"/record/{own}/comment", {"comment": "x"}), (f"/record/{own}/sale_result", {"sale_made": True}),
                      (f"/record/{own}/reanalyze", {}), (f"/record/{own}/meta", {"record_type": "sales", "person_name": "x"}),
                      ("/person/coach?name=Олена", {}), ("/analytics/generate", {})):
        assert post_json(manager_client, url, body).status_code == 403, url
    html = manager_client.get(f"/record/{own}").get_data(as_text=True)
    assert "save-comment-btn" not in html and "sale-result-btn" not in html and "Мій профіль" in html


def test_manager_transcript_visibility_setting(manager_client):
    own = _done(person="Олена")
    assert "Олена: текст" in manager_client.get(f"/record/{own}").get_data(as_text=True)
    settings.update({"manager_can_see_transcript": False})
    assert "Олена: текст" not in manager_client.get(f"/record/{own}").get_data(as_text=True)


def test_unlinked_manager_sees_nothing(client):
    login(client, make_user(email="n@x.com", role="manager"))
    _done(person="Олена")
    assert "/record/" not in client.get("/").get_data(as_text=True)


def test_admin_creates_manager_requires_person_name(admin_client):
    form = {"email": "m2@x.com", "name": "М", "password": "password123", "role": "manager", "csrf_token": CSRF}
    admin_client.post("/admin/users/create", data=form)
    assert db.get_user_by_email("m2@x.com") is None
    admin_client.post("/admin/users/create", data={**form, "person_name": "Олена"})
    user = db.get_user_by_email("m2@x.com")
    assert user["role"] == "manager" and user["person_name"] == "Олена"
    admin_client.post(f"/admin/users/{user['id']}/person", data={"person_name": "Ігор", "csrf_token": CSRF})
    assert db.get_user_by_id(user["id"])["person_name"] == "Ігор"


def test_settings_page_saves_and_validates(admin_client):
    form = {"csrf_token": CSRF, "company_context": "Школа X", "anthropic_model": "custom",
            "anthropic_model_custom": "claude-opus-5", "low_score_threshold": "40", "telegram_chat_ids": "-100",
            "daily_digest_time": "18:30", "webhook_url": "", "zoom_min_duration_minutes": "5",
            "zoom_transcript_wait_minutes": "60", "notify_on_done": "on", "daily_digest_enabled": "on"}
    resp = admin_client.post("/admin/settings", data=form, follow_redirects=True)
    assert "збережено" in resp.get_data(as_text=True)
    config = settings.get_all()
    assert (config["company_context"], config["anthropic_model"], config["low_score_threshold"]) == \
        ("Школа X", "claude-opus-5", 40)
    assert config["notify_on_done"] and not config["notify_low_score"] and config["daily_digest_enabled"]
    resp = admin_client.post("/admin/settings", data={**form, "low_score_threshold": "abc"}, follow_redirects=True)
    assert "ціле число" in resp.get_data(as_text=True)
    assert settings.get("low_score_threshold") == 40


def test_settings_test_buttons(admin_client, telegram):
    resp = admin_client.post("/admin/settings/test-telegram", data={"csrf_token": CSRF}, follow_redirects=True)
    assert "надіслано" in resp.get_data(as_text=True)
    resp = admin_client.post("/admin/settings/digest-now", data={"csrf_token": CSRF, "day": "2000-01-01"},
                             follow_redirects=True)
    assert "немає проаналізованих" in resp.get_data(as_text=True)
    resp = admin_client.post("/admin/settings/test-webhook", data={"csrf_token": CSRF}, follow_redirects=True)
    assert "не налаштовано" in resp.get_data(as_text=True)


def test_checklist_editor_flow(admin_client):
    form = {"csrf_token": CSRF, "criterion_key": ["need_identified", ""],
            "criterion_title": ["Потреба", "Назвав ціну"], "criterion_description": ["опис", ""],
            "criterion_weight": ["2", "1"], "instructions": "Скрипт"}
    resp = admin_client.post("/admin/checklists/sales", data=form, follow_redirects=True)
    assert "збережено" in resp.get_data(as_text=True)
    saved = checklists.get_checklist("sales")
    assert [c["title"] for c in saved["criteria"]] == ["Потреба", "Назвав ціну"] and saved["criteria"][0]["weight"] == 2
    bad = admin_client.post("/admin/checklists/sales", data={**form, "criterion_weight": ["99", "1"]})
    assert "від 1 до 10" in bad.get_data(as_text=True) and 'value="99"' in bad.get_data(as_text=True)
    _done(day="2026-09-05")
    resp = admin_client.post("/admin/checklists/sales/reanalyze",
                             data={"csrf_token": CSRF, "date_from": "2026-09-01", "date_to": "2026-09-30"},
                             follow_redirects=True)
    assert "переаналіз: 1" in resp.get_data(as_text=True)
    admin_client.post("/admin/checklists/sales/reset", data={"csrf_token": CSRF})
    assert checklists.get_checklist("sales")["criteria"][0]["title"] == "Виявив потребу клієнта"
    assert admin_client.get("/admin/checklists/unknown").status_code == 404


def test_export_csv_format_and_injection_guard(admin_client):
    _done(person="=HYPERLINK(\"http://evil\")", day="2026-09-01")
    resp = admin_client.get("/export.csv")
    assert resp.mimetype == "text/csv" and "attachment" in resp.headers["Content-Disposition"]
    text = resp.get_data(as_text=True)
    assert text.startswith("﻿")
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"))
    assert rows[0][0] == "ID" and rows[1][4].startswith("'=")      # формула знешкоджена
    assert rows[1][7] == "60" and "не домовились" in rows[1][12]


def test_record_page_renders_timecodes_and_new_sections(admin_client):
    rid = _done()
    db.update_record(rid, transcription="[00:01:05] Олена: Це дорого\n[00:02:00] Клієнт: так")
    html = admin_client.get(f"/record/{rid}").get_data(as_text=True)
    assert 'data-t="65"' in html and 'data-t="00:01:05"' in html
    for text in ("Заперечення клієнта", "Ризики угоди", "Ключові моменти", "Що варто було сказати",
                 "Порівняймо з вартістю", "Наступний крок"):
        assert text in html, text


def test_person_coach_endpoint(admin_client, monkeypatch):
    _done()
    monkeypatch.setattr(coaching, "call_json", lambda *a, **k: {"summary": "План", "strengths": [], "growth_areas": [],
                                                              "focus_next_week": "Фокус", "phrases": []})
    assert post_json(admin_client, "/person/coach?name=Олена").json == {"ok": True}
    assert "Фокус" in admin_client.get("/person?name=Олена").get_data(as_text=True)
    assert post_json(admin_client, "/person/coach?name=Нікого").status_code == 404


def test_search_escapes_like_wildcards(admin_client):
    _done()
    assert "Олена" in admin_client.get("/search?q=текст").get_data(as_text=True)
    assert "Знайдено: 0" in admin_client.get("/search?q=%25%25").get_data(as_text=True)


def test_manager_cannot_download_foreign_audio(manager_client):
    import app as app_module
    for name, person in (("own-audio.mp3", "Олена"), ("foreign-audio.mp3", "Ігор")):
        (app_module.UPLOAD_FOLDER / name).write_bytes(b"ID3")
        db.create_record("2026-09-01", "sales", person, name, source="upload", status="done")
    try:
        assert manager_client.get("/uploads/own-audio.mp3").status_code == 200
        assert manager_client.get("/uploads/foreign-audio.mp3").status_code == 404
    finally:
        for name in ("own-audio.mp3", "foreign-audio.mp3"):
            (app_module.UPLOAD_FOLDER / name).unlink(missing_ok=True)


def test_corrupted_stored_settings_fall_back_to_defaults():
    db.set_setting(settings.SETTINGS_KEY, json.dumps({"low_score_threshold": "abc", "daily_digest_time": "99:99",
                                                       "notify_on_done": False, "unknown": 1}))
    settings.invalidate_cache()
    config = settings.get_all()
    assert config["low_score_threshold"] == 50 and config["daily_digest_time"] == "19:00"
    assert config["notify_on_done"] is False and "unknown" not in config


def test_long_telegram_message_is_sent_as_plain_text(telegram):
    notify.send_telegram("<b>Звіт</b> &amp; " + "х" * 5000)
    sent = telegram.calls[0]["json"]
    assert "parse_mode" not in sent and sent["text"].startswith("Звіт & ") and len(sent["text"]) <= notify.TELEGRAM_LIMIT + 1


def test_webhook_refuses_internal_addresses(monkeypatch):
    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: pytest.fail("не має надсилати"))
    for address in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "::1"):
        monkeypatch.setattr(notify.socket, "getaddrinfo", lambda *a, _ip=address, **k: [(None, None, None, "", (_ip, 443))])
        settings.update({"webhook_url": "https://internal.example.com/hook"})
        assert "публічною" in notify.post_webhook({"event": "test"})
    settings.update({"webhook_url": "https://user:pass@example.com/hook"})
    assert "публічною" in notify.post_webhook({"event": "test"})


def test_ai_links_are_removed_from_telegram(telegram):
    analysis = sample_sales_analysis()
    analysis["summary"] = "Клієнт просить оплатити тут: https://evil.example/pay або www.evil.example"
    notify.notify_record_done(_done(analysis=analysis))
    text = telegram.calls[0]["json"]["text"]
    assert "evil.example" not in text and "[посилання]" in text


def test_nul_bytes_in_params_are_rejected(admin_client):
    assert admin_client.get("/?person=a%00b").status_code == 400
    assert admin_client.get("/search?q=%00%00").status_code == 400


def test_search_hides_transcript_for_manager_when_disabled(manager_client):
    rid = _done(person="Олена")
    db.update_record(rid, transcription="Олена: секретна фраза клієнта")
    html = manager_client.get("/search?q=секретна").get_data(as_text=True)
    assert "<mark>секретна</mark> фраза клієнта" in html            # збіг підсвічено
    settings.update({"manager_can_see_transcript": False})
    html = manager_client.get("/search?q=секретна").get_data(as_text=True)
    assert "фраза клієнта" not in html and "Знайдено: 0" in html


def test_detection_runs_once_and_prefers_linked_employees(monkeypatch):
    make_user(email="emp@x.com", role="manager", name="Мирослава")
    db.update_user(db.get_user_by_email("emp@x.com")["id"], person_name="Мирослава")
    # Детекція повернула «службове» імʼя (не учасник розмови) → беремо активного співробітника
    monkeypatch.setattr(pipeline, "detect_type_and_name",
                        lambda **k: {"record_type": "lesson", "person_name": "H40904875", "reason": ""})
    monkeypatch.setattr(pipeline, "analyze", lambda t, text: sample_lesson_analysis())
    transcript = "\n".join(["Мирослава: вправа на голос " * 5, "Анна: так", "Мирослава: молодці " * 5, "Анна: дякую"])
    rid = db.create_record("2026-09-01", "lesson", "Невідомо", "", source="text", job_kind="analyze",
                           auto_detect=True, transcription=transcript)
    pipeline.process_record(db.claim_next_job())
    rec = db.get_record(rid)
    assert rec["person_name"] == "Мирослава" and rec["auto_detect"] == 0
    monkeypatch.setattr(pipeline, "detect_type_and_name", lambda **k: pytest.fail("повторне визначення"))
    db.enqueue_reanalysis("lesson", None, None)
    pipeline.process_record(db.claim_next_job())
    assert db.get_record(rid)["person_name"] == "Мирослава"


def test_trainer_is_not_reassigned_to_linked_manager_speaking_briefly(monkeypatch):
    """Регресія: заняття не повинно переходити менеджеру, який говорить лише під час передачі."""
    make_user(email="mgr@x.com", role="manager", name="Олена")
    db.update_user(db.get_user_by_email("mgr@x.com")["id"], person_name="Олена")
    monkeypatch.setattr(pipeline, "detect_type_and_name",
                        lambda **k: {"record_type": "lesson", "person_name": "Ірина Тренер", "reason": ""})
    monkeypatch.setattr(pipeline, "analyze", lambda t, text: sample_lesson_analysis())
    transcript = "\n".join(["Ірина Тренер: вправа " * 30, "Учасник: так", "Ірина Тренер: ще вправа " * 30,
                            "Олена: Вітаю, я менеджер, розкажу про курс"])
    rid = db.create_record("2026-09-01", "lesson", "Невідомо", "", source="text", job_kind="analyze",
                           auto_detect=True, transcription=transcript)
    pipeline.process_record(db.claim_next_job())
    assert db.get_record(rid)["person_name"] == "Ірина Тренер"


def test_bulk_reanalysis_runs_after_new_jobs_and_keeps_analysis_visible(admin_client):
    old = _done(day="2026-09-01")
    db.enqueue_reanalysis("sales", None, None)
    fresh = db.create_record("2026-09-20", "sales", "Новий", "", source="text", job_kind="analyze", transcription="т")
    assert db.claim_next_job()["id"] == fresh          # нові дзвінки — першими
    assert db.get_record(old)["status"] == "queued"
    html = admin_client.get(f"/record/{old}").get_data(as_text=True)
    assert "Заперечення клієнта" in html and "оновлюється" in html   # аналіз видно під час переаналізу
    rating = team.people_rating(db.get_all_records())
    assert next(p for p in rating if p["name"] == "Олена")["calls"] == 1


def test_whisper_paragraphs_are_not_speakers():
    from services.transcript_text import speaker_stats
    whisper = "\n".join(["[00:00:00] Добрий день усім, починаємо заняття.", "[00:00:31] Завдання: скажіть фразу голосно.",
                         "[00:01:02] Молодці, тепер наступна вправа.", "[00:01:40] Дякую всім за участь."])
    assert speaker_stats(whisper) == []


def test_timecode_parsing_is_strict():
    from services.analysis import _timecode
    assert _timecode("1:05") == "00:01:05" and _timecode("[01:02:03]") == "01:02:03"
    assert _timecode("1:05:3") == "" and _timecode("12:75") == "" and _timecode(None) == ""


def test_long_monologue_gets_timecode_every_minute():
    from services.transcript_text import parse_vtt
    cues = "\n\n".join(f"{i}\n00:{i:02d}:00.000 --> 00:{i:02d}:05.000\nТренер: частина {i}" for i in range(0, 4))
    lines = parse_vtt("WEBVTT\n\n" + cues).split("\n")
    assert [line[:10] for line in lines] == ["[00:00:00]", "[00:01:00]", "[00:02:00]", "[00:03:00]"]


def test_switch_to_manager_requires_person_name(admin_client):
    user = make_user(email="v2@x.com", role="viewer")
    admin_client.post(f"/admin/users/{user['id']}/role", data={"role": "manager", "csrf_token": CSRF})
    assert db.get_user_by_id(user["id"])["role"] == "viewer"


# ── UX-виправлення та нові функції (раунд рев'ю 2) ────────────────────────────

def test_light_columns_are_filled_and_backfilled():
    rid = _done()
    rec = db.list_records()[0]
    assert rec["has_analysis"] and rec["score"] == 60 and rec["checklist_total"] == 5 and rec["summary"]
    assert rec["analysis_kind"] == "sales" and rec["analyzed_at"]
    db.execute("UPDATE records SET analysis_kind = NULL, score = NULL WHERE id = ?", (rid,))
    assert db.backfill_light_columns() == 1
    assert db.list_records()[0]["score"] == 60
    db.update_record(rid, record_type="lesson")          # тип змінили — аналіз не підходить
    assert db.list_records()[0]["has_analysis"] is False


def test_hot_lead_without_next_step_alert(telegram):
    settings.update({"notify_on_done": False, "notify_low_score": True})
    hot = sample_sales_analysis(score=90, chance="Високий")
    hot["deal_chance_percent"] = 80
    notify.notify_record_done(_done(analysis=hot))
    assert "Гарячий лід без наступного кроку" in telegram.calls[0]["json"]["text"]


def test_digest_includes_records_analyzed_since_last_digest(telegram):
    settings.update({"daily_digest_enabled": True, "daily_digest_time": "19:00"})
    evening = datetime.now(LOCAL_TZ).replace(hour=20, minute=0)
    _done(person="Вчорашній", day="2026-01-01")          # дата запису стара, але проаналізовано щойно
    assert notify.send_daily_digest_if_due(evening) is True
    assert "Вчорашній" in telegram.calls[0]["json"]["text"]
    assert db.get_setting(notify.DIGEST_SENT_AT_KEY)


def test_digest_failure_keeps_records_for_next_time(monkeypatch):
    settings.update({"daily_digest_enabled": True, "telegram_bot_token": "111:T", "telegram_chat_ids": "-100"})
    monkeypatch.setattr(notify.requests, "post", FakeRequests(status=500, body={"description": "down"}).post)
    _done()
    assert notify.send_daily_digest_if_due(datetime.now(LOCAL_TZ).replace(hour=23, minute=0)) is False
    assert db.get_setting(notify.DIGEST_SENT_AT_KEY) is None   # наступний звіт охопить ці записи


def test_team_drill_down_and_calibration(admin_client):
    rid = _done(day="2026-09-10")
    db.update_sale_result(rid, True, 1000)
    records = db.get_all_records()
    assert team.ai_calibration(records) == [{"level": "Середній", "sold": 1, "total": 1, "rate": 100}]
    html = admin_client.get("/team/drill?kind=sales&criterion=presentation_done&date_from=2026-09-01&date_to=2026-09-30").get_data(as_text=True)
    assert "Не виконано" in html and f"/record/{rid}" in html
    html = admin_client.get("/team/drill?kind=sales&objection=ціна&date_from=2026-09-01&date_to=2026-09-30").get_data(as_text=True)
    assert "Дорого" in html and "Порівняймо" in html
    team_html = admin_client.get("/team?date_from=2026-09-30&date_to=2026-09-01").get_data(as_text=True)
    assert "2026-09-01 — 2026-09-30" in team_html and "Точність прогнозу AI" in team_html


def test_rename_person_merges_everywhere(admin_client):
    rid = _done(person="Myroslava", kind="lesson")
    sale = _done(person="Олена", trainer_name="Myroslava")
    user = make_user(email="t@x.com", role="manager")
    db.update_user(user["id"], person_name="Myroslava")
    admin_client.post("/admin/people/rename", data={"old_name": "Myroslava", "new_name": "Мирослава", "csrf_token": CSRF})
    assert db.get_record(rid)["person_name"] == "Мирослава" and db.get_record(sale)["trainer_name"] == "Мирослава"
    assert db.get_user_by_id(user["id"])["person_name"] == "Мирослава"


def test_talk_stats_on_record_page(admin_client):
    rid = _done()
    db.update_record(rid, transcription="[00:00:00] Олена: Як справи? Що цікавить?\n[00:00:20] Ірина: Курс\n"
                                        "[00:00:30] Олена: Розповім детально про курс\n[00:02:00] Ірина: Дорого")
    html = admin_client.get(f"/record/{rid}").get_data(as_text=True)
    assert "Аналітика мовлення" in html and "Найдовший монолог: Олена — 1 хв 30 с" in html


def test_settings_error_keeps_user_input(admin_client):
    resp = admin_client.post("/admin/settings", data={"csrf_token": CSRF, "company_context": "Новий опис",
                                                      "telegram_chat_ids": "abc", "low_score_threshold": "50",
                                                      "daily_digest_time": "", "zoom_min_duration_minutes": "3",
                                                      "zoom_transcript_wait_minutes": "180"})
    html = resp.get_data(as_text=True)
    assert resp.status_code == 400 and "Новий опис" in html and "chat id" in html
    ok = admin_client.post("/admin/settings", data={"csrf_token": CSRF, "company_context": "Новий опис",
                                                    "low_score_threshold": "50", "daily_digest_time": "",
                                                    "zoom_min_duration_minutes": "3", "zoom_transcript_wait_minutes": "180"})
    assert ok.status_code == 302 and settings.get("daily_digest_time") == "19:00"


def test_manager_without_records_sees_empty_profile(client):
    user = make_user(email="new@x.com", role="manager")
    db.update_user(user["id"], person_name="Новенька")
    login(client, db.get_user_by_email("new@x.com"))
    html = client.get("/person?name=Новенька").get_data(as_text=True)
    assert "Проаналізованих записів поки немає" in html
    assert "Завантажте запис" not in client.get("/").get_data(as_text=True)


def test_checklist_duplicate_titles_rejected():
    with pytest.raises(checklists.ChecklistError, match="повторюється"):
        checklists.validate("sales", {"criteria": [{"title": "Ціна"}, {"title": "ціна"}]})


def test_checklist_count_endpoint_and_reversed_range(admin_client):
    _done(day="2026-09-05")
    data = admin_client.get("/admin/checklists/sales/count?date_from=2026-09-01&date_to=2026-09-30").json
    assert data["count"] == 1 and data["limit"] == db.REANALYSIS_BATCH_LIMIT
    resp = admin_client.post("/admin/checklists/sales/reanalyze", data={"csrf_token": CSRF, "date_from": "2026-09-30",
                                                                         "date_to": "2026-09-01"}, follow_redirects=True)
    assert "Некоректний період" in resp.get_data(as_text=True)


def test_export_trainer_filter_and_labels(admin_client):
    _done(person="Олена", trainer_name="Андрій")
    _done(person="Ігор", trainer_name="Мирослава")
    text = admin_client.get("/export.csv?type=sales&trainer=Андрій").get_data(as_text=True)
    assert "Олена" in text and "Ігор" not in text and "Готово" in text
    assert admin_client.get("/export.csv").headers["Content-Type"] == "text/csv; charset=utf-8"


def test_plural_filter():
    import app as app_module
    assert [app_module._plural_filter(n, "запис", "записи", "записів") for n in (1, 2, 5, 11, 21, 22, 112)] == \
        ["запис", "записи", "записів", "записів", "запис", "записи", "записів"]


# ── Регресії фінального ревью ─────────────────────────────────────────────────

def test_bulk_reanalysis_keeps_analyzed_at_out_of_daily_digest(monkeypatch):
    rid = _done(day="2026-08-01")
    old_iso = "2026-08-01T10:00:00+00:00"
    db.execute("UPDATE records SET analyzed_at = ? WHERE id = ?", (old_iso, rid))
    monkeypatch.setattr(pipeline, "analyze", lambda t, text: sample_sales_analysis(score=90))
    monkeypatch.setattr(pipeline.notify, "notify_record_done", lambda rid: pytest.fail("без сповіщень"))
    since = db.utcnow_iso()
    assert db.enqueue_reanalysis("sales", None, None) == 1
    pipeline.process_record(db.claim_next_job())
    rec = db.get_record(rid)
    assert rec["score"] == 90 and rec["analyzed_at"] == old_iso
    assert db.get_records_analyzed_since(since) == []      # щоденний звіт не заллє старими дзвінками


def test_detection_does_not_overwrite_manual_edit_made_during_ai_call(monkeypatch):
    rid = db.create_record("2026-09-01", "lesson", "Невідомо", "", source="text", job_kind="analyze",
                           auto_detect=True, transcription="Ірина: вправа\nАнна: так")

    def detect(**kwargs):  # поки AI визначав, людина вручну виправила запис
        db.update_record(rid, record_type="sales", person_name="Ручне Імʼя", auto_detect=0)
        return {"record_type": "lesson", "person_name": "Ірина", "reason": ""}

    monkeypatch.setattr(pipeline, "detect_type_and_name", detect)
    monkeypatch.setattr(pipeline, "analyze", lambda t, text: sample_sales_analysis())
    pipeline.process_record(db.claim_next_job())
    rec = db.get_record(rid)
    assert (rec["record_type"], rec["person_name"], rec["status"]) == ("sales", "Ручне Імʼя", "done")


def test_all_time_team_drill_links_keep_period(admin_client):
    rid = _done(day="2025-01-15")                        # поза типовим періодом 30 днів
    html = admin_client.get("/team?period=all").get_data(as_text=True)
    assert "/team/drill?" in html and "period=all" in html
    drill = admin_client.get("/team/drill?kind=sales&criterion=presentation_done&period=all").get_data(as_text=True)
    assert f"/record/{rid}" in drill and "весь час" in drill and "/team?period=all" in drill


def test_webhook_host_check_handles_bad_urls_and_non_global_ranges(monkeypatch):
    assert notify._is_public_host("https://example.com:99999/hook") is False
    assert notify._is_public_host("https://[::1/hook") is False
    for address, public in (("100.64.0.1", False), ("192.0.2.10", False), ("fe80::1%eth0", False),
                            ("93.184.216.34", True)):
        monkeypatch.setattr(notify.socket, "getaddrinfo", lambda *a, _ip=address, **k: [(None, None, None, "", (_ip, 443))])
        assert notify._is_public_host("https://hook.example.com/x") is public, address


def test_nul_bytes_in_form_and_json_bodies_are_rejected(admin_client):
    rid = _done()
    resp = admin_client.post("/admin/checklists/sales", data={
        "csrf_token": CSRF, "criterion_key": ["a", "b"], "criterion_title": ["Добре", "Погано\x00"],
        "criterion_description": ["", ""], "criterion_weight": ["1", "1"], "instructions": ""})
    assert resp.status_code == 400
    assert post_json(admin_client, f"/record/{rid}/comment", {"comment": "x\x00y"}).status_code == 400
    assert post_json(admin_client, f"/record/{rid}/comment", {"comment": "ok"}).status_code == 200


def test_large_uploads_only_for_staff(manager_client):
    big = io.BytesIO(b"x" * (3 * 1024 * 1024))
    resp = manager_client.post("/upload", data={"csrf_token": CSRF, "file": (big, "call.txt")},
                               content_type="multipart/form-data")
    assert resp.status_code == 413


def test_coaching_uses_records_with_analysis_during_reanalysis(monkeypatch):
    rid = _done(person="Олена")
    db.enqueue_reanalysis("sales", None, None)
    assert db.get_record(rid)["status"] == "queued"
    monkeypatch.setattr(coaching, "call_json", lambda *a, **k: {
        "summary": "s", "strengths": [], "growth_areas": [], "focus_next_week": "f", "phrases": []})
    plan = coaching.generate("Олена", db.get_all_records(person_name="Олена"))
    assert plan["based_on"] == 1


def test_rename_person_moves_cached_coaching_plan(admin_client):
    _done(person="Myroslava")
    db.set_setting(coaching.cache_key("Myroslava"), json.dumps({"summary": "план"}))
    db.rename_person("Myroslava", "Мирослава")
    assert coaching.get_cached("Мирослава") == {"summary": "план"}
    assert coaching.get_cached("Myroslava") is None


def test_large_upload_by_admin_is_not_cut_by_default_limit(admin_client):
    body = ("Олена: Добрий день, розкажу про курс.\nКлієнт: Дорого.\n" * 40000).encode("utf-16")
    assert len(body) > 3 * 1024 * 1024
    resp = admin_client.post("/upload", data={
        "csrf_token": CSRF, "record_type": "sales", "person_name": "Олена", "record_date": "2026-09-20",
        "file": (io.BytesIO(body), "call.txt")}, content_type="multipart/form-data")
    assert resp.status_code == 302
    rec = db.get_record(int(resp.headers["Location"].rsplit("/", 1)[-1]))
    assert rec["transcription"].startswith("Олена: Добрий день") and "\x00" not in rec["transcription"]
