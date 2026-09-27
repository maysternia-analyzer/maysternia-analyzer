"""
Майстерня Аналізатор — веб-застосунок (Flask).

Запуск у продакшні: gunicorn app:app (див. railway.toml).
Локально: python app.py → http://localhost:5050
"""
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import defaultdict
from datetime import date, timedelta
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request,  # noqa: E402
                   send_from_directory, url_for)
from flask_login import (LoginManager, UserMixin, current_user, login_required,  # noqa: E402
                         login_url, login_user, logout_user)
from werkzeug.exceptions import HTTPException  # noqa: E402
from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402
from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402

import database as db  # noqa: E402
from security import (LoginRateLimiter, csrf_protect, csrf_token, is_safe_next_url,  # noqa: E402
                      is_strong_secret, resolve_secret_key)
from services import background, health, pipeline, transcription, zoom  # noqa: E402
from services.analysis import LESSON_CRITERIA, SALES_CRITERIA, to_score  # noqa: E402
from services.insights import generate_insights  # noqa: E402
from services.poller import poll_once  # noqa: E402
from services.timeutil import today_local, utc_iso_to_local  # noqa: E402
from services.transcript_text import is_valid_transcript, strip_error_suffix, transcript_from_file_bytes  # noqa: E402

log = logging.getLogger("app")

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = BASE_DIR / "uploads"
UPLOAD_FOLDER.mkdir(exist_ok=True)
MEDIA_EXTENSIONS = {"mp4", "m4a", "mp3", "wav", "webm", "mov", "ogg", "flac"}
TEXT_EXTENSIONS = {"vtt", "txt"}
ALLOWED_EXTENSIONS = MEDIA_EXTENSIONS | TEXT_EXTENSIONS
MIN_PASSWORD_LENGTH = 8
MAX_TEXT_BYTES = 20 * 1024 * 1024
MAX_REQUEST_BYTES = 2 * 1024 * 1024          # будь-який запит, крім завантаження запису
MAX_UPLOAD_BYTES = 500 * 1024 * 1024         # завантаження запису (лише після входу)
MAX_WEBHOOK_BYTES = 1024 * 1024
ROLES = ("viewer", "admin")

db.init_db()

app = Flask(__name__)
# Railway працює за проксі: беремо реальні схему (https) та IP клієнта із заголовків.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

IS_PRODUCTION = os.environ.get("FORCE_SECURE_COOKIES") == "1" or any(
    os.environ.get(k) for k in ("RAILWAY_ENVIRONMENT_NAME", "RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID")
)
_secret_key, SECRET_KEY_SOURCE = resolve_secret_key()
app.config.update(
    SECRET_KEY=_secret_key,
    MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_SAMESITE="Lax",
    REMEMBER_COOKIE_SECURE=IS_PRODUCTION,
    REMEMBER_COOKIE_DURATION=timedelta(days=30),
)

login_limiter = LoginRateLimiter(max_failures=10, window=15 * 60)      # IP + email
email_limiter = LoginRateLimiter(max_failures=30, window=15 * 60)      # email з будь-яких IP
_DUMMY_PASSWORD_HASH = generate_password_hash("timing-equalizer")
_insights_lock = threading.Lock()


# ── Авторизація ───────────────────────────────────────────────────────────────

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Будь ласка, увійдіть, щоб продовжити"


def _credential_tag(password_hash: str) -> str:
    return hashlib.sha256((password_hash or "").encode()).hexdigest()[:16]


class User(UserMixin):
    def __init__(self, data: dict):
        self.id = data["id"]
        self.email = data["email"]
        self.name = data["name"]
        self.role = data["role"]
        self._active = bool(data.get("is_active", 1))
        self._tag = _credential_tag(data.get("password_hash", ""))

    def get_id(self):
        # Ідентифікатор сесії привʼязаний до пароля: після зміни пароля старі сесії
        # та remember-cookie перестають діяти.
        return f"{self.id}:{self._tag}"

    @property
    def is_active(self):
        return self._active

    def is_admin(self):
        return self.role == "admin"


@login_manager.user_loader
def load_user(user_id):
    raw_id, _, tag = str(user_id).partition(":")
    try:
        data = db.get_user_by_id(int(raw_id))
    except (TypeError, ValueError):
        return None
    # Заблокований користувач або змінений пароль — доступ втрачається одразу.
    if not data or not data.get("is_active"):
        return None
    if not tag or not hmac.compare_digest(tag, _credential_tag(data["password_hash"])):
        return None
    return User(data)


@login_manager.unauthorized_handler
def _unauthorized():
    if _wants_json():
        return jsonify(ok=False, error="Сесія завершилась — оновіть сторінку та увійдіть знову"), 401
    flash(login_manager.login_message, "message")
    return redirect(login_url("login", next_url=request.url))


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin():
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@app.before_request
def _upload_limit():
    # Великі файли приймаємо лише від авторизованих користувачів на сторінці завантаження.
    if request.endpoint == "upload" and request.method == "POST" and current_user.is_authenticated:
        request.max_content_length = MAX_UPLOAD_BYTES


@app.before_request
def _csrf():
    csrf_protect(exempt_endpoints={"zoom_webhook", "healthz", "static"})


@app.after_request
def _security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.context_processor
def _template_globals():
    return {"csrf_token": csrf_token, "LESSON_CRITERIA": LESSON_CRITERIA, "SALES_CRITERIA": SALES_CRITERIA,
            "now_utc": db.utcnow_iso()}


@app.template_filter("localtime")
def _localtime_filter(value, fmt="%Y-%m-%d %H:%M"):
    return utc_iso_to_local(value, fmt) if value else ""


@app.template_filter("money")
def _money_filter(value):
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


# ── Допоміжне ─────────────────────────────────────────────────────────────────

def _valid_date(value: str) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        date.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


def _valid_time(value: str) -> bool:
    return bool(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value or ""))


def _json_body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _json_error(message: str, status: int = 400):
    return jsonify(ok=False, error=message), status


def _record_or_404(record_id: int) -> dict:
    record = db.get_record(record_id)
    if not record:
        abort(404)
    return record


def _date_filters() -> tuple[str, str]:
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    return (date_from if _valid_date(date_from) else "", date_to if _valid_date(date_to) else "")


def build_stats(records: list[dict]) -> dict:
    """Зведені показники для дашбордів (розраховані стійко до неповних даних)."""
    sales = [r for r in records if r["record_type"] == "sales" and r.get("analysis")]
    lessons = [r for r in records if r["record_type"] == "lesson" and r.get("analysis")]
    sold = [r for r in records if r["record_type"] == "sales" and r.get("sale_made") == 1]
    stats = {
        "total": len(records),
        "lessons_total": sum(1 for r in records if r["record_type"] == "lesson"),
        "sales_total": sum(1 for r in records if r["record_type"] == "sales"),
        "analyzed": len(sales) + len(lessons),
        "sales_analyzed": len(sales),
        "lessons_analyzed": len(lessons),
        "errors": sum(1 for r in records if r["status"] == "error"),
        "in_progress": sum(1 for r in records if r["status"] in db.ACTIVE_STATUSES),
        "sold": len(sold),
        "not_sold": sum(1 for r in records if r["record_type"] == "sales" and r.get("sale_made") == 0),
        "revenue": sum(float(r["sale_amount"] or 0) for r in sold),
        "by_person": [],
        "by_trainer": [],
    }
    if sales:
        high = sum(1 for r in sales if r["analysis"].get("deal_chance") == "Високий")
        stats["conversion"] = round(high / len(sales) * 100)
        scores = defaultdict(list)
        for r in sales:
            scores[r["person_name"]].append(to_score(r["analysis"].get("checklist_score")))
        stats["by_person"] = sorted(((p, round(sum(v) / len(v))) for p, v in scores.items()),
                                    key=lambda x: x[1], reverse=True)
        all_scores = [s for v in scores.values() for s in v]
        stats["avg_sales_score"] = round(sum(all_scores) / len(all_scores))
    if lessons:
        scores = defaultdict(list)
        for r in lessons:
            scores[r["person_name"]].append(to_score(r["analysis"].get("overall_score")))
        stats["by_trainer"] = sorted(((p, round(sum(v) / len(v))) for p, v in scores.items()),
                                     key=lambda x: x[1], reverse=True)
        all_scores = [s for v in scores.values() for s in v]
        stats["avg_lesson_score"] = round(sum(all_scores) / len(all_scores))
    return stats


def _media_url(record: dict) -> str | None:
    """Посилання на аудіо/відео, якщо файл ще є на диску."""
    filename = record.get("filename") or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in MEDIA_EXTENSIONS:
        return None
    path = pipeline.upload_path(filename)
    if path is None or not path.exists():
        return None
    return url_for("serve_upload", filename=filename)


# ── Службові маршрути ─────────────────────────────────────────────────────────

@app.route("/healthz")
def healthz():
    try:
        db.fetch_one("SELECT 1 AS ok")
    except Exception as e:
        log.error("healthz: БД недоступна: %s", e)
        return jsonify(ok=False, db=False), 503
    return jsonify(ok=True, db=True)


# ── Вхід / налаштування ───────────────────────────────────────────────────────

@app.route("/setup", methods=["GET", "POST"])
def setup():
    """Створення першого адміністратора (лише коли користувачів ще немає)."""
    if db.count_users() > 0:
        return redirect(url_for("login"))
    setup_secret = os.environ.get("SECRET_KEY", "")
    setup_allowed = is_strong_secret(setup_secret)
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")
        secret = request.form.get("secret", "")
        if not setup_allowed:
            error = ("Задайте унікальний SECRET_KEY (від 16 символів) у змінних середовища "
                     "або створіть адміністратора командою python create_admin.py")
        elif not hmac.compare_digest(secret.encode(), setup_secret.strip().encode()):
            error = "Невірний секретний ключ"
        elif not email or "@" not in email or not name:
            error = "Вкажіть коректний email та імʼя"
        elif len(password) < MIN_PASSWORD_LENGTH:
            error = f"Пароль має містити щонайменше {MIN_PASSWORD_LENGTH} символів"
        elif db.create_user(email, name, generate_password_hash(password), role="admin"):
            flash("Адміністратора створено — увійдіть", "success")
            return redirect(url_for("login"))
        else:
            error = "Не вдалося створити користувача"
    return render_template("setup.html", error=error, setup_allowed=setup_allowed)


@app.route("/login", methods=["GET", "POST"])
def login():
    if db.count_users() == 0:
        return redirect(url_for("setup"))
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    error, status = None, 200
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        limiter_key, email_key = f"{request.remote_addr}|{email}", f"email:{email}"
        if not login_limiter.allowed(limiter_key) or not email_limiter.allowed(email_key):
            error, status = "Забагато невдалих спроб. Спробуйте через 15 хвилин.", 429
        else:
            user_data = db.get_user_by_email(email)
            # Хеш перевіряємо завжди: час відповіді не видає, чи існує такий email.
            password_ok = check_password_hash(
                user_data["password_hash"] if user_data else _DUMMY_PASSWORD_HASH, password)
            if user_data and user_data.get("is_active") and password_ok:
                login_limiter.reset(limiter_key)
                login_user(User(user_data), remember=True)
                target = request.args.get("next")
                return redirect(target if is_safe_next_url(target) else url_for("dashboard"))
            login_limiter.record_failure(limiter_key)
            email_limiter.record_failure(email_key)
            error, status = "Невірний email або пароль", 401
    return render_template("login.html", error=error), status


@app.route("/logout", methods=["GET", "POST"])
def logout():
    # Вихід лише через POST з CSRF-токеном (GET-посилання з чужого сайту не розлогінить).
    if request.method == "POST":
        logout_user()
    return redirect(url_for("login"))


# ── Адміністрування користувачів ──────────────────────────────────────────────

def _is_last_active_admin(user: dict) -> bool:
    return user["role"] == "admin" and bool(user["is_active"]) and db.count_active_admins() <= 1


@app.route("/admin/users")
@admin_required
def admin_users():
    return render_template("admin_users.html", users=db.get_all_users(), min_password=MIN_PASSWORD_LENGTH)


@app.route("/admin/users/create", methods=["POST"])
@admin_required
def admin_create_user():
    email = request.form.get("email", "").strip().lower()
    name = request.form.get("name", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "viewer")
    if not email or "@" not in email or not name:
        flash("Вкажіть коректний email та імʼя", "error")
    elif len(password) < MIN_PASSWORD_LENGTH:
        flash(f"Пароль має містити щонайменше {MIN_PASSWORD_LENGTH} символів", "error")
    elif role not in ROLES:
        flash("Невідома роль", "error")
    elif db.create_user(email, name, generate_password_hash(password), role):
        flash(f"Користувача {email} створено", "success")
    else:
        flash(f"Користувач з email {email} вже існує", "error")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_user(user_id):
    user = db.get_user_by_id(user_id)
    if not user or user_id == current_user.id:
        flash("Не можна змінити статус власного акаунта", "error")
    elif user["is_active"] and _is_last_active_admin(user):
        flash("Не можна заблокувати останнього адміністратора", "error")
    else:
        db.update_user(user_id, is_active=0 if user["is_active"] else 1)
        flash("Статус користувача змінено", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
@admin_required
def admin_change_role(user_id):
    user = db.get_user_by_id(user_id)
    role = request.form.get("role", "")
    if not user or user_id == current_user.id or role not in ROLES:
        flash("Роль змінити не можна", "error")
    elif role != "admin" and _is_last_active_admin(user):
        flash("Має залишитися хоча б один адміністратор", "error")
    else:
        db.update_user(user_id, role=role)
        flash("Роль змінено", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    user = db.get_user_by_id(user_id)
    if not user or user_id == current_user.id:
        flash("Не можна видалити власний акаунт", "error")
    elif _is_last_active_admin(user):
        flash("Не можна видалити останнього адміністратора", "error")
    else:
        db.delete_user(user_id)
        flash("Користувача видалено", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/password", methods=["POST"])
@admin_required
def admin_change_password(user_id):
    password = request.form.get("password", "")
    if not db.get_user_by_id(user_id):
        abort(404)
    if len(password) < MIN_PASSWORD_LENGTH:
        flash(f"Пароль має містити щонайменше {MIN_PASSWORD_LENGTH} символів", "error")
    else:
        db.update_user(user_id, password_hash=generate_password_hash(password))
        flash("Пароль змінено", "success")
    return redirect(url_for("admin_users"))


# ── Адміністрування системи ───────────────────────────────────────────────────

@app.route("/admin/system")
@admin_required
def admin_system():
    return render_template(
        "system.html",
        config=health.config_summary(),
        secret_source=SECRET_KEY_SOURCE,
        worker=background.worker_status(),
        status_counts=db.count_records_by_status(),
        webhook_logs=db.get_webhook_logs(50),
        webhook_url=url_for("zoom_webhook", _external=True),
    )


@app.route("/admin/system/check", methods=["POST"])
@admin_required
def admin_system_check():
    for name, result in health.run_all_checks().items():
        flash(f"{name}: {'✅' if result['ok'] else '❌'} {result['detail']}",
              "success" if result["ok"] else "error")
    return redirect(url_for("admin_system"))


@app.route("/admin/system/poll", methods=["POST"])
@admin_required
def admin_system_poll():
    try:
        days = max(1, min(int(request.form.get("days", "3")), 180))
    except ValueError:
        days = 3
    try:
        result = poll_once(days)
        background.wake()
        flash(f"Zoom за {days} дн.: зустрічей {result.get('meetings', 0)}, нових у черзі "
              f"{result.get('queued', 0)}, вже оброблених {result.get('duplicate', 0)}, "
              f"закоротких {result.get('skipped_short', 0)}", "success")
    except Exception as e:
        flash(f"Помилка перевірки Zoom: {e}", "error")
    return redirect(url_for("admin_system"))


@app.route("/admin/webhook-logs")
@admin_required
def admin_webhook_logs():
    return jsonify(db.get_webhook_logs(200))


# ── Сторінки ──────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def dashboard():
    record_type = request.args.get("type", "")
    record_type = record_type if record_type in db.RECORD_TYPES else ""
    person_name = request.args.get("person", "").strip()
    date_from, date_to = _date_filters()
    records = db.get_all_records(record_type=record_type or None, person_name=person_name or None,
                                 date_from=date_from or None, date_to=date_to or None)
    return render_template(
        "index.html", records=records, stats=build_stats(records), people=db.get_person_names(),
        filters={"type": record_type, "person": person_name, "date_from": date_from, "date_to": date_to},
    )


@app.route("/lessons")
@login_required
def lessons_page():
    person_name = request.args.get("person", "").strip()
    date_from, date_to = _date_filters()
    records = db.get_all_records(record_type="lesson", person_name=person_name or None,
                                 date_from=date_from or None, date_to=date_to or None)
    return render_template(
        "lessons.html", records=records, trainers=db.get_person_names("lesson"),
        stats=build_stats(records),
        filters={"person": person_name, "date_from": date_from, "date_to": date_to},
    )


@app.route("/sales")
@login_required
def sales_page():
    manager = request.args.get("manager", "").strip()
    trainer = request.args.get("trainer", "").strip()
    date_from, date_to = _date_filters()
    records = db.get_all_records(record_type="sales", person_name=manager or None, trainer_name=trainer or None,
                                 date_from=date_from or None, date_to=date_to or None)
    return render_template(
        "sales.html", records=records, managers=db.get_person_names("sales"),
        trainers=db.get_trainer_names_from_sales(), stats=build_stats(records),
        filters={"manager": manager, "trainer": trainer, "date_from": date_from, "date_to": date_to},
    )


@app.route("/stats")
@login_required
def stats_page():
    done = [r for r in db.get_all_records() if r["status"] == "done" and r.get("analysis")]
    people: dict[str, dict] = {}
    for r in sorted(done, key=lambda x: (x["record_date"] or "", x.get("record_time") or "")):
        person = people.setdefault(r["person_name"] or "Невідомо", {
            "name": r["person_name"] or "Невідомо", "sales": [], "lessons": []})
        a = r["analysis"]
        if r["record_type"] == "sales":
            person["sales"].append({"id": r["id"], "date": r["record_date"] or "",
                                    "score": to_score(a.get("checklist_score")),
                                    "chance": a.get("deal_chance", ""),
                                    "pct": to_score(a.get("deal_chance_percent"))})
        else:
            person["lessons"].append({"id": r["id"], "date": r["record_date"] or "",
                                      "score": to_score(a.get("overall_score")),
                                      "engagement": a.get("engagement_level", "")})
    for person in people.values():
        sales, lessons = person["sales"], person["lessons"]
        person["avg_sales_score"] = round(sum(s["score"] for s in sales) / len(sales)) if sales else None
        person["avg_deal_pct"] = round(sum(s["pct"] for s in sales) / len(sales)) if sales else None
        person["high_chance"] = sum(1 for s in sales if s["chance"] == "Високий")
        person["avg_lesson_score"] = round(sum(l["score"] for l in lessons) / len(lessons)) if lessons else None
        person["total"] = len(sales) + len(lessons)
    managers = sorted(people.values(), key=lambda p: p["total"], reverse=True)
    return render_template("stats.html", managers=managers, total_records=len(done))


@app.route("/analytics")
@login_required
def analytics_page():
    date_from, date_to = _date_filters()
    return render_template("analytics.html", insights=db.get_insights(date_from, date_to),
                           date_from=date_from, date_to=date_to)


@app.route("/analytics/generate", methods=["POST"])
@login_required
def generate_analytics():
    data = _json_body()
    date_from = data.get("date_from", "") if _valid_date(data.get("date_from", "")) else ""
    date_to = data.get("date_to", "") if _valid_date(data.get("date_to", "")) else ""
    if not _insights_lock.acquire(blocking=False):
        return _json_error("Аналіз уже генерується — зачекайте хвилину", 409)
    try:
        records = db.get_all_records(date_from=date_from or None, date_to=date_to or None,
                                     transcript_preview_chars=1200)
        result = generate_insights(records)
        db.save_insights(result, date_from, date_to)
        return jsonify(ok=True)
    except Exception as e:
        log.exception("Помилка генерації аналітики")
        return _json_error(f"Не вдалося згенерувати аналіз: {e}", 500)
    finally:
        _insights_lock.release()


# ── Завантаження запису ───────────────────────────────────────────────────────

@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    whisper_ok = transcription.is_configured()
    if request.method == "GET":
        return render_template("upload.html", today=today_local(), form={}, whisper_ok=whisper_ok)

    form = request.form
    file = request.files.get("file")
    record_type = form.get("record_type", "")
    person_name = form.get("person_name", "").strip()[:120]
    trainer_name = form.get("trainer_name", "").strip()[:120] if record_type == "sales" else ""
    record_date = form.get("record_date", "").strip()
    record_time = form.get("record_time", "").strip()
    ext = file.filename.rsplit(".", 1)[-1].lower() if file and file.filename and "." in file.filename else ""

    error = None
    if not file or not file.filename:
        error = "Файл не вибрано"
    elif ext not in ALLOWED_EXTENSIONS:
        error = "Непідтримуваний формат файлу. Дозволено: " + ", ".join(sorted(ALLOWED_EXTENSIONS))
    elif record_type not in db.RECORD_TYPES:
        error = "Оберіть тип запису"
    elif not person_name:
        error = "Введіть імʼя тренера або менеджера"
    elif not _valid_date(record_date):
        error = "Вкажіть коректну дату"
    elif record_time and not _valid_time(record_time):
        error = "Вкажіть коректний час (ГГ:ХХ)"
    elif ext in MEDIA_EXTENSIONS and not whisper_ok:
        error = ("Транскрипція аудіо зараз недоступна (не задано OPENAI_API_KEY). "
                 "Завантажте готову транскрипцію з Zoom у форматі VTT або TXT.")

    transcript = None
    if not error and ext in TEXT_EXTENSIONS:
        raw = file.read(MAX_TEXT_BYTES + 1)
        if len(raw) > MAX_TEXT_BYTES:
            error = "Файл транскрипції завеликий (максимум 20 МБ)"
        else:
            transcript = transcript_from_file_bytes(raw, ext)
            if not transcript.strip():
                error = "Файл транскрипції порожній"
    if error:
        return render_template("upload.html", error=error, today=today_local(), form=form,
                               whisper_ok=whisper_ok), 400

    common = dict(record_time=record_time, trainer_name=trainer_name,
                  source_json={"original_name": file.filename[:200]})
    if transcript is not None:
        record_id = db.create_record(record_date, record_type, person_name, "", source="text",
                                     job_kind="analyze", transcription=transcript, **common)
    else:
        stored_name = f"{uuid.uuid4().hex}.{ext}"
        file.save(UPLOAD_FOLDER / stored_name)
        record_id = db.create_record(record_date, record_type, person_name, stored_name,
                                     source="upload", job_kind="full", **common)
    background.wake()
    return redirect(url_for("record_detail", record_id=record_id))


@app.route("/uploads/<path:filename>")
@login_required
def serve_upload(filename):
    if pipeline.upload_path(filename) is None:
        abort(404)
    return send_from_directory(UPLOAD_FOLDER, filename, conditional=True)


# ── Запис ─────────────────────────────────────────────────────────────────────

@app.route("/record/<int:record_id>")
@login_required
def record_detail(record_id):
    record = _record_or_404(record_id)
    has_transcript = is_valid_transcript(record.get("transcription"))
    return render_template(
        "record.html", record=record, media_url=_media_url(record), has_transcript=has_transcript,
        transcript=strip_error_suffix(record["transcription"]) if has_transcript else "",
        source=pipeline.record_source(record), meta=pipeline.source_meta(record),
    )


@app.route("/record/<int:record_id>/status")
@login_required
def record_status(record_id):
    record = _record_or_404(record_id)
    waiting = bool(record["status"] == "queued" and record.get("not_before")
                   and record["not_before"] > db.utcnow_iso())
    return jsonify(status=record["status"], error_message=record.get("error_message") or "",
                   waiting=waiting, not_before=utc_iso_to_local(record.get("not_before") or ""))


@app.route("/record/<int:record_id>/sale_result", methods=["POST"])
@login_required
def save_sale_result(record_id):
    record = _record_or_404(record_id)
    data = _json_body()
    if "sale_made" not in data:
        return _json_error("Не вказано результат продажу (sale_made)")
    sale_made = data.get("sale_made")
    if sale_made not in (True, False, None):
        return _json_error("Некоректне значення sale_made")
    if "sale_amount" in data:
        amount = data.get("sale_amount")
        if amount in (None, ""):
            amount = None
        else:
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                return _json_error("Сума має бути числом")
            if not 0 <= amount <= 100_000_000:
                return _json_error("Некоректна сума")
    else:
        amount = record.get("sale_amount")  # клік «Продано» не стирає вже введену суму
    db.update_sale_result(record_id, sale_made, amount)
    return jsonify(ok=True)


@app.route("/record/<int:record_id>/meta", methods=["POST"])
@login_required
def update_meta(record_id):
    record = _record_or_404(record_id)
    data = _json_body()
    record_type = data.get("record_type")
    person_name = data.get("person_name")
    person_name = person_name.strip()[:120] if isinstance(person_name, str) else ""
    if record_type not in db.RECORD_TYPES:
        return _json_error("Некоректний тип запису")
    if not person_name:
        return _json_error("Імʼя не може бути порожнім")
    db.update_record(record_id, record_type=record_type, person_name=person_name, auto_detect=0)
    reanalyzing = False
    # Аналіз іншого типу не підходить — перезапускаємо його автоматично.
    if (record_type != record["record_type"] and is_valid_transcript(record.get("transcription"))
            and record["status"] not in db.ACTIVE_STATUSES):
        reanalyzing = db.enqueue_record(record_id, "analyze")
        background.wake()
    return jsonify(ok=True, reanalyzing=reanalyzing)


@app.route("/record/<int:record_id>/comment", methods=["POST"])
@login_required
def save_comment(record_id):
    _record_or_404(record_id)
    data = _json_body()
    if not isinstance(data.get("comment"), str):
        return _json_error("Не передано текст коментаря")
    comment = data["comment"][:10_000]
    db.update_comment(record_id, comment)
    return jsonify(ok=True)


@app.route("/record/<int:record_id>/reanalyze", methods=["POST"])
@login_required
def reanalyze(record_id):
    """mode: auto (аналіз, якщо є транскрипція, інакше повна обробка) | analyze | full."""
    record = _record_or_404(record_id)
    if record["status"] in ("processing", "analyzing"):
        return _json_error("Запис уже обробляється — зачекайте", 409)
    mode = _json_body().get("mode", "auto")
    has_transcript = is_valid_transcript(record.get("transcription"))
    if mode == "analyze" or (mode == "auto" and has_transcript):
        if not has_transcript:
            return _json_error("Транскрипції немає — потрібна повна обробка")
        job_kind = "analyze"
    else:
        source = pipeline.record_source(record)
        if source == "text":
            return _json_error("Вихідного файлу немає — завантажте транскрипцію ще раз")
        if source == "zoom" and not zoom.is_configured():
            return _json_error("Zoom не налаштовано на сервері")
        if source == "upload":
            path = pipeline.upload_path(record.get("filename") or "")
            if path is None or not path.exists():
                return _json_error("Файл не знайдено на сервері (Railway очищає диск при перезапуску). "
                                   "Завантажте запис ще раз.")
        job_kind = "full"
    if not db.enqueue_record(record_id, job_kind):
        return _json_error("Запис уже обробляється — зачекайте", 409)
    background.wake()
    return jsonify(ok=True, action=job_kind)


@app.route("/record/<int:record_id>/delete", methods=["POST"])
@admin_required
def delete_record(record_id):
    row = db.delete_record(record_id)
    if not row:
        abort(404)
    if row.get("source") != "zoom":
        path = pipeline.upload_path(row.get("filename") or "")
        if path is not None:
            path.unlink(missing_ok=True)
    flash("Запис видалено", "success")
    return redirect(url_for("dashboard"))


# ── Zoom Webhook ──────────────────────────────────────────────────────────────

ZOOM_EVENTS = ("recording.completed", "recording.transcript_completed")


_PLAIN_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,128}")
_bad_signature_logged_at = [0.0]


def _log_bad_signature(event: str, reason: str) -> None:
    """Не частіше разу на хвилину: сміттєві запити не повинні витісняти справжні записи журналу."""
    now = time.monotonic()
    if now - _bad_signature_logged_at[0] >= 60:
        _bad_signature_logged_at[0] = now
        db.log_webhook(event, "error_bad_signature", f"Відхилено: {reason}")


@app.route("/zoom/webhook", methods=["POST"])
def zoom_webhook():
    if (request.content_length or 0) > MAX_WEBHOOK_BYTES:
        return jsonify(error="payload too large"), 413
    body = request.get_data(cache=True)
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return jsonify(error="invalid json"), 400
    event = str(data.get("event") or "unknown")[:100]
    timestamp = request.headers.get("x-zm-request-timestamp", "")
    signature = request.headers.get("x-zm-signature", "")

    if not zoom.webhook_secret():
        db.log_webhook(event, "error_config", "ZOOM_WEBHOOK_SECRET не задано на сервері")
        return jsonify(error="webhook secret not configured"), 503

    # Підпис перевіряємо для ВСІХ подій, включно з url_validation: інакше відповідь на
    # валідацію (HMAC від довільного токена) дозволяє підробити підпис будь-якої події.
    if not zoom.verify_webhook_signature(body, timestamp, signature):
        _log_bad_signature(event, "немає заголовків підпису" if not (timestamp and signature)
                           else "підпис не збігається")
        return jsonify(error="invalid signature"), 401

    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    if event == "endpoint.url_validation":
        plain_token = payload.get("plainToken")
        if not isinstance(plain_token, str) or not _PLAIN_TOKEN.fullmatch(plain_token):
            return jsonify(error="plainToken required"), 400
        db.log_webhook(event, "validated", "Zoom перевірив URL вебхука")
        return jsonify(zoom.url_validation_response(plain_token))

    if event not in ZOOM_EVENTS:
        db.log_webhook(event, "ignored", "Подія не обробляється")
        return jsonify(ok=True)

    info = zoom.meeting_info(payload.get("object"))
    try:
        result = pipeline.ingest_zoom_meeting(info, origin=event)
    except Exception as e:
        log.exception("Вебхук Zoom: помилка")
        db.log_webhook(event, "error", f"{info['topic']} | {e}")
        return jsonify(error="internal error"), 500  # Zoom повторить доставку
    db.log_webhook(event, result["result"],
                   f"{info['topic']} | {info['start_time']} | {info['duration']} хв | "
                   f"uuid={info['uuid']} | record_id={result.get('record_id')}")
    background.wake()
    return jsonify(ok=True, **result)


# ── Помилки ───────────────────────────────────────────────────────────────────

def _wants_json() -> bool:
    return bool(
        request.is_json
        or request.headers.get("X-CSRFToken")
        or request.path.startswith("/zoom/")
        or request.path.endswith("/status")
        or request.path == "/admin/webhook-logs"
    )


@app.errorhandler(HTTPException)
def _http_error(error):
    if _wants_json():
        return jsonify(ok=False, error=error.description), error.code
    messages = {
        403: "Недостатньо прав для цієї дії.",
        404: "Сторінку не знайдено.",
        413: "Файл або запит завеликий (запис — до 500 МБ).",
    }
    return render_template("error.html", code=error.code,
                           message=messages.get(error.code, error.description)), error.code


@app.errorhandler(Exception)
def _unhandled_error(error):
    log.exception("Необроблена помилка: %s %s", request.method, request.path)
    if _wants_json():
        return _json_error("Внутрішня помилка сервера", 500)
    return render_template("error.html", code=500,
                           message="Внутрішня помилка сервера. Спробуйте ще раз."), 500


# ── Запуск ────────────────────────────────────────────────────────────────────

background.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    log.info("Майстерня Аналізатор → http://localhost:%s", port)
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=port, debug=False, use_reloader=False)
