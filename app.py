"""
Майстерня Аналізатор — веб-застосунок (Flask).

Запуск у продакшні: gunicorn app:app (див. railway.toml).
Локально: python app.py → http://localhost:5050
"""
import csv
import hashlib
import hmac
import io
import json
import logging
import math
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

from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template, request,  # noqa: E402
                   send_from_directory, url_for)
from flask_login import (LoginManager, UserMixin, current_user, login_required,  # noqa: E402
                         login_url, login_user, logout_user)
from markupsafe import Markup, escape  # noqa: E402
from werkzeug.exceptions import HTTPException  # noqa: E402
from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402
from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402

import database as db  # noqa: E402
from security import (LoginRateLimiter, csrf_protect, csrf_token, is_safe_next_url,  # noqa: E402
                      is_strong_secret, resolve_secret_key)
from services import (background, checklists, coaching, health, notify, pipeline, team,  # noqa: E402
                      transcription, zoom)
from services import settings as app_settings  # noqa: E402
from services.analysis import (LESSON_CRITERIA, SALES_CRITERIA, main_score,  # noqa: E402
                               present_criteria)
from services.insights import generate_insights  # noqa: E402
from services.poller import poll_once  # noqa: E402
from services.timeutil import today_local, utc_iso_to_local  # noqa: E402
from services.transcript_text import (is_valid_transcript, split_timecode, strip_error_suffix,  # noqa: E402
                                     talk_stats, transcript_from_file_bytes)

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
ROLES = db.ROLES
ROLE_LABELS = {"admin": "Адміністратор", "viewer": "Керівник", "manager": "Менеджер / тренер"}
PER_PAGE = 50

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
search_limiter = LoginRateLimiter(max_failures=30, window=60)          # 30 пошуків за хвилину на користувача
_search_slots = threading.BoundedSemaphore(2)                          # важкі LIKE-запити — не більше 2 одночасно
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
        self.person_name = (data.get("person_name") or "").strip()
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

    def is_manager(self):
        """Менеджер/тренер бачить лише власні записи й не може нічого змінювати."""
        return self.role == "manager"

    def is_staff(self):
        return self.role in ("admin", "viewer")


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


def staff_required(view):
    """Адміністратор або керівник (не менеджер з обмеженим доступом)."""
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_staff():
            abort(403)
        return view(*args, **kwargs)
    return wrapped


NO_PERSON = "__немає_привʼязки__"   # імʼя, якого не буває в записах (без NUL — PostgreSQL їх не приймає)


def scope_person() -> str | None:
    """Для керівника/адміна — None (без обмежень); для решти — імʼя, за яким фільтруються записи."""
    if current_user.is_authenticated and current_user.is_staff():
        return None
    if current_user.is_authenticated and current_user.is_manager() and current_user.person_name:
        return current_user.person_name
    return NO_PERSON


def manager_hides_transcript() -> bool:
    return scope_person() is not None and not app_settings.get("manager_can_see_transcript")


def can_view_record(record: dict) -> bool:
    person = scope_person()
    return person is None or record.get("person_name") == person


def _contains_nul(value) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_contains_nul(k) or _contains_nul(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_contains_nul(v) for v in value)
    return False


@app.before_request
def _reject_nul_bytes():
    # PostgreSQL не приймає рядки з NUL — відповідаємо 400, а не 500.
    if "\x00" in request.path or any("\x00" in v for _, v in request.args.items(multi=True)):
        abort(400)


@app.before_request
def _upload_limit():
    # Великі файли приймаємо лише від авторизованих користувачів на сторінці завантаження.
    if (request.endpoint == "upload" and request.method == "POST" and current_user.is_authenticated
            and current_user.is_staff()):
        request.max_content_length = MAX_UPLOAD_BYTES


@app.before_request
def _reject_nul_in_body():
    # Окремий хук ПІСЛЯ _upload_limit: читання форми до нього обрізало б великі файли лімітом 2 МБ.
    if request.method != "POST" or request.endpoint == "zoom_webhook":
        return
    if request.mimetype in ("application/x-www-form-urlencoded", "multipart/form-data"):
        if any("\x00" in v for _, v in request.form.items(multi=True)):
            abort(400)
    elif request.is_json and _contains_nul(request.get_json(silent=True)):
        abort(400)


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
            "now_utc": db.utcnow_iso(), "main_score": main_score, "checklist_summary": checklist_summary,
            "ROLE_LABELS": ROLE_LABELS}


def checklist_summary(analysis, kind: str) -> tuple[int, int]:
    """(виконано, всього) критеріїв — для компактного стовпця у списках."""
    items = present_criteria(analysis, kind) if analysis else []
    return sum(1 for c in items if c["result"]), len(items)


# Доступні й у макросах, імпортованих без контексту.
app.jinja_env.globals.update(checklist_summary=checklist_summary, main_score=main_score)


@app.template_filter("localtime")
def _localtime_filter(value, fmt="%Y-%m-%d %H:%M"):
    return utc_iso_to_local(value, fmt) if value else ""


@app.template_filter("highlight")
def _highlight_filter(text, query):
    """Безпечне підсвічування збігів: усе екранується, <mark> додаємо самі."""
    text, query = str(text or ""), str(query or "")
    if not query:
        return escape(text)
    parts = re.split(f"({re.escape(query)})", text, flags=re.IGNORECASE)
    return Markup("").join(Markup("<mark>{}</mark>").format(p) if i % 2 else escape(p)
                           for i, p in enumerate(parts))


@app.template_filter("plural")
def _plural_filter(n, one, few, many):
    """Українські множини: 1 запис, 2 записи, 5 записів."""
    n = abs(int(n or 0))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


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
    if not record or not can_view_record(record):
        abort(404)  # чужий запис для менеджера виглядає як неіснуючий
    return record


_PAGER_KEYS = ("type", "person", "manager", "trainer", "date_from", "date_to", "q", "period", "name")


def paginate(items: list, per_page: int = PER_PAGE) -> tuple[list, dict]:
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    pages = max(1, math.ceil(len(items) / per_page))
    page = min(page, pages)
    args = {k: v for k, v in request.args.items() if k in _PAGER_KEYS}
    args.update(request.view_args or {})

    def link(number):
        return url_for(request.endpoint, page=number, **args) if 1 <= number <= pages else None

    return items[(page - 1) * per_page: page * per_page], {
        "page": page, "pages": pages, "total": len(items),
        "prev_url": link(page - 1), "next_url": link(page + 1),
    }


def _date_filters() -> tuple[str, str]:
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    return (date_from if _valid_date(date_from) else "", date_to if _valid_date(date_to) else "")


def build_stats(records: list[dict]) -> dict:
    """Зведені показники для дашбордів з «легких» колонок (без розбору JSON аналізу)."""
    def analyzed(r):
        return r.get("analysis_kind") == r["record_type"] and r.get("score") is not None

    sales = [r for r in records if r["record_type"] == "sales" and analyzed(r)]
    lessons = [r for r in records if r["record_type"] == "lesson" and analyzed(r)]
    sold = [r for r in records if r["record_type"] == "sales" and r.get("sale_made") == 1]
    not_sold = [r for r in records if r["record_type"] == "sales" and r.get("sale_made") == 0]
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
        "not_sold": len(not_sold),
        "revenue": sum(float(r["sale_amount"] or 0) for r in sold),
        "by_person": [],
        "by_trainer": [],
    }
    if sold or not_sold:  # реальна конверсія — лише серед записів з відміченим результатом
        stats["conversion_real"] = round(len(sold) / (len(sold) + len(not_sold)) * 100)
    if sales:
        high = sum(1 for r in sales if r.get("deal_chance") == "Високий")
        stats["ai_high_share"] = round(high / len(sales) * 100)
    for key, rows, avg_key in (("by_person", sales, "avg_sales_score"), ("by_trainer", lessons, "avg_lesson_score")):
        if not rows:
            continue
        scores = defaultdict(list)
        for r in rows:
            scores[r["person_name"]].append(r["score"])
        stats[key] = sorted(((p, round(sum(v) / len(v))) for p, v in scores.items()),
                            key=lambda x: x[1], reverse=True)
        all_scores = [x for v in scores.values() for x in v]
        stats[avg_key] = round(sum(all_scores) / len(all_scores))
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
            if user_data and password_ok:  # пароль правильний — можна сказати, що акаунт заблоковано
                error, status = "Акаунт заблоковано — зверніться до адміністратора", 403
            else:
                error, status = "Невірний email або пароль", 401
    return render_template("login.html", error=error, email=request.form.get("email", "")), status


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
    return render_template("admin_users.html", users=db.get_all_users(), min_password=MIN_PASSWORD_LENGTH,
                           people=db.get_person_names(), roles=ROLES)


@app.route("/admin/users/create", methods=["POST"])
@admin_required
def admin_create_user():
    email = request.form.get("email", "").strip().lower()
    name = request.form.get("name", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "viewer")
    person_name = request.form.get("person_name", "").strip()[:120]
    if not email or "@" not in email or not name:
        flash("Вкажіть коректний email та імʼя", "error")
    elif len(password) < MIN_PASSWORD_LENGTH:
        flash(f"Пароль має містити щонайменше {MIN_PASSWORD_LENGTH} символів", "error")
    elif role not in ROLES:
        flash("Невідома роль", "error")
    elif role == "manager" and not person_name:
        flash("Для менеджера вкажіть імʼя, під яким він фігурує в записах", "error")
    elif db.create_user(email, name, generate_password_hash(password), role, person_name=person_name):
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
    elif role == "manager" and not (user.get("person_name") or "").strip():
        flash("Спочатку вкажіть «Імʼя в записах» — інакше менеджер не побачить жодного запису", "error")
    else:
        db.update_user(user_id, role=role)
        flash("Роль змінено", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/person", methods=["POST"])
@admin_required
def admin_change_person(user_id):
    if not db.get_user_by_id(user_id):
        abort(404)
    db.update_user(user_id, person_name=request.form.get("person_name", "").strip()[:120])
    flash("Привʼязку до імені в записах змінено", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/people/rename", methods=["POST"])
@admin_required
def admin_rename_person():
    old = request.form.get("old_name", "").strip()
    new = request.form.get("new_name", "").strip()[:120]
    if not old or not new or old == new:
        flash("Вкажіть поточне і нове імʼя (різні)", "error")
    else:
        count = db.rename_person(old, new)
        flash(f"«{old}» → «{new}»: оновлено записів і привʼязок: {count}", "success")
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
        min_duration=app_settings.get("zoom_min_duration_minutes"),
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


_TEXT_SETTINGS = ("company_context", "anthropic_model", "low_score_threshold", "telegram_bot_token",
                  "telegram_chat_ids", "daily_digest_time", "webhook_url", "webhook_secret",
                  "zoom_min_duration_minutes", "zoom_transcript_wait_minutes")
_BOOL_SETTINGS = ("notify_on_done", "notify_low_score", "daily_digest_enabled", "manager_can_see_transcript")


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        form = request.form
        changes = {key: form.get(key, "") for key in _TEXT_SETTINGS}
        changes.update({key: form.get(key) == "on" for key in _BOOL_SETTINGS})
        if changes["anthropic_model"] == "custom":
            changes["anthropic_model"] = form.get("anthropic_model_custom", "").strip()
        if changes["anthropic_model"] and changes["anthropic_model"] != app_settings.get("anthropic_model"):
            from services import llm
            model_error = llm.validate_model(changes["anthropic_model"]) if llm.is_configured() else None
            if model_error:
                flash(model_error + " — модель не змінено", "error")
                changes.pop("anthropic_model")
        for secret in ("telegram_bot_token", "webhook_secret"):
            if form.get(f"clear_{secret}") == "on":
                changes[secret], changes[f"clear_{secret}"] = "", True
        try:
            app_settings.update(changes)
            flash("Налаштування збережено", "success")
            return redirect(url_for("admin_settings"))
        except app_settings.SettingsError as e:
            flash(str(e) + " — зміни не збережено, виправте поле й збережіть ще раз", "error")
            submitted = {**app_settings.get_all(), **{k: v for k, v in changes.items()
                                                    if not k.startswith("clear_") and k in app_settings.FIELDS
                                                    and app_settings.FIELDS[k][0] != "secret"}}
            return _render_settings(submitted), 400
    return _render_settings(app_settings.get_all())


def _render_settings(current: dict):
    stored = app_settings.get_all()
    return render_template(
        "settings.html", s=current, models=app_settings.AI_MODELS,
        known_models=[m for m, _ in app_settings.AI_MODELS], default_model=llm_default_model(),
        telegram_token_mask=app_settings.mask(stored["telegram_bot_token"]),
        webhook_secret_mask=app_settings.mask(stored["webhook_secret"]),
        public_url=notify.public_base_url(),
    )


def llm_default_model() -> str:
    from services import llm
    return os.environ.get("ANTHROPIC_MODEL", "").strip() or llm.DEFAULT_MODEL


@app.route("/admin/settings/test-telegram", methods=["POST"])
@admin_required
def admin_test_telegram():
    errors = notify.send_telegram("✅ Тестове повідомлення від «Майстерня Аналізатор». Сповіщення працюють.")
    flash("Telegram: повідомлення надіслано" if not errors else "Telegram: " + "; ".join(errors),
          "success" if not errors else "error")
    return redirect(url_for("admin_settings"))


@app.route("/admin/settings/test-webhook", methods=["POST"])
@admin_required
def admin_test_webhook():
    error = notify.post_webhook({"event": "test", "message": "Перевірка вебхука Майстерня Аналізатор"})
    flash("Вебхук: сервер прийняв тестовий запит" if not error else f"Вебхук: {error}",
          "success" if not error else "error")
    return redirect(url_for("admin_settings"))


@app.route("/admin/settings/digest-now", methods=["POST"])
@admin_required
def admin_digest_now():
    day = request.form.get("day", "") if _valid_date(request.form.get("day", "")) else today_local()
    text = notify.build_daily_digest(day)
    if not text:
        flash(f"За {day} немає проаналізованих записів — звіт порожній", "error")
    else:
        errors = notify.send_telegram(text)
        flash("Звіт надіслано в Telegram" if not errors else "Telegram: " + "; ".join(errors),
              "success" if not errors else "error")
    return redirect(url_for("admin_settings"))


@app.route("/admin/checklists")
@admin_required
def admin_checklists_index():
    return redirect(url_for("admin_checklist", kind="sales"))


def _checklist_from_form(form) -> dict:
    rows = zip(form.getlist("criterion_key"), form.getlist("criterion_title"),
               form.getlist("criterion_description"), form.getlist("criterion_weight"))
    return {"criteria": [{"key": k, "title": t, "description": d, "weight": w} for k, t, d, w in rows],
            "instructions": form.get("instructions", "")}


@app.route("/admin/checklists/<kind>", methods=["GET", "POST"])
@admin_required
def admin_checklist(kind):
    if kind not in checklists.KINDS:
        abort(404)
    checklist = checklists.get_checklist(kind)
    if request.method == "POST":
        submitted = _checklist_from_form(request.form)
        try:
            checklists.save_checklist(kind, submitted)
            flash("Чек-лист збережено. Нові записи аналізуватимуться за ним.", "success")
            return redirect(url_for("admin_checklist", kind=kind))
        except checklists.ChecklistError as e:
            flash(str(e), "error")
            checklist = {**checklist, **submitted}
    today = date.fromisoformat(today_local())
    default_from = (today - timedelta(days=29)).isoformat()
    return render_template(
        "checklist.html", kind=kind, checklist=checklist, kinds=checklists.KINDS,
        default_from=default_from, default_to=today.isoformat(),
        candidates=db.count_reanalysis_candidates(kind, default_from, today.isoformat()),
    )


@app.route("/admin/checklists/<kind>/count")
@admin_required
def admin_checklist_count(kind):
    if kind not in checklists.KINDS:
        abort(404)
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    count = db.count_reanalysis_candidates(kind, date_from if _valid_date(date_from) else None,
                                           date_to if _valid_date(date_to) else None)
    return jsonify(ok=True, count=min(count, db.REANALYSIS_BATCH_LIMIT), total=count,
                   limit=db.REANALYSIS_BATCH_LIMIT)


@app.route("/admin/checklists/<kind>/reset", methods=["POST"])
@admin_required
def admin_checklist_reset(kind):
    if kind not in checklists.KINDS:
        abort(404)
    checklists.reset_checklist(kind)
    flash("Відновлено стандартний чек-лист", "success")
    return redirect(url_for("admin_checklist", kind=kind))


@app.route("/admin/checklists/<kind>/reanalyze", methods=["POST"])
@admin_required
def admin_checklist_reanalyze(kind):
    if kind not in checklists.KINDS:
        abort(404)
    date_from = request.form.get("date_from", "")
    date_to = request.form.get("date_to", "")
    if ((date_from and not _valid_date(date_from)) or (date_to and not _valid_date(date_to))
            or (date_from and date_to and date_from > date_to)):
        flash("Некоректний період: перевірте дати «Від» і «До»", "error")
        return redirect(url_for("admin_checklist", kind=kind))
    count = db.enqueue_reanalysis(kind, date_from or None, date_to or None)
    background.wake()
    flash(f"Поставлено в чергу на переаналіз: {count} записів (максимум {db.REANALYSIS_BATCH_LIMIT} за раз). "
          f"Нові дзвінки обробляються першими, сповіщення для переаналізу не надсилаються.", "success")
    return redirect(url_for("admin_checklist", kind=kind))


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
    person_name = scope_person() or request.args.get("person", "").strip()
    date_from, date_to = _date_filters()
    records = db.list_records(record_type=record_type or None, person_name=person_name or None,
                              date_from=date_from or None, date_to=date_to or None)
    page_records, pager = paginate(records)
    return render_template(
        "index.html", records=page_records, pager=pager, stats=build_stats(records),
        people=[] if scope_person() else db.get_person_names(),
        filters={"type": record_type, "person": "" if scope_person() else person_name,
                 "date_from": date_from, "date_to": date_to},
    )


@app.route("/lessons")
@login_required
def lessons_page():
    person_name = scope_person() or request.args.get("person", "").strip()
    date_from, date_to = _date_filters()
    records = db.list_records(record_type="lesson", person_name=person_name or None,
                              date_from=date_from or None, date_to=date_to or None)
    page_records, pager = paginate(records)
    return render_template(
        "lessons.html", records=page_records, pager=pager,
        trainers=[] if scope_person() else db.get_person_names("lesson"),
        stats=build_stats(records),
        filters={"person": person_name, "date_from": date_from, "date_to": date_to},
    )


@app.route("/sales")
@login_required
def sales_page():
    manager = scope_person() or request.args.get("manager", "").strip()
    trainer = request.args.get("trainer", "").strip()
    date_from, date_to = _date_filters()
    records = db.list_records(record_type="sales", person_name=manager or None, trainer_name=trainer or None,
                              date_from=date_from or None, date_to=date_to or None)
    page_records, pager = paginate(records)
    return render_template(
        "sales.html", records=page_records, pager=pager,
        managers=[] if scope_person() else db.get_person_names("sales"),
        trainers=[] if scope_person() else db.get_trainer_names_from_sales(), stats=build_stats(records),
        filters={"manager": manager, "trainer": trainer, "date_from": date_from, "date_to": date_to},
    )


@app.route("/stats")
@login_required
def stats_page():
    return redirect(url_for("team_page"))


def _period() -> tuple[str, str, bool]:
    """Період звіту: з параметрів або останні 30 днів. Третє значення — «весь час»."""
    if request.args.get("period") == "all":
        return "", "", True
    date_from, date_to = _date_filters()
    if not date_from and not date_to:
        today = date.fromisoformat(today_local())
        return (today - timedelta(days=29)).isoformat(), today.isoformat(), False
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from  # переплутані дати — міняємо місцями
    return date_from, date_to, False


@app.route("/team")
@staff_required
def team_page():
    date_from, date_to, all_time = _period()
    record_type = request.args.get("type", "")
    record_type = record_type if record_type in db.RECORD_TYPES else ""
    records = db.get_all_records(record_type=record_type or None, date_from=date_from or None,
                                 date_to=date_to or None)
    previous = None
    prev = team.previous_period(date_from, date_to) if date_from and date_to else None
    if prev:
        previous = db.get_all_records(record_type=record_type or None, date_from=prev[0], date_to=prev[1])
    return render_template(
        "team.html", rating=team.people_rating(records, previous), stats=build_stats(records),
        lesson_criteria=team.criteria_rates(records, "lesson"), sales_criteria=team.criteria_rates(records, "sales"),
        objections=team.objection_stats(records), risks=team.risk_stats(records),
        calibration=team.ai_calibration(records),
        filters={"date_from": date_from, "date_to": date_to, "type": record_type, "all": all_time},
        has_previous=bool(prev),
    )


@app.route("/team/drill")
@staff_required
def team_drill():
    date_from, date_to, all_time = _period()
    kind = request.args.get("kind", "sales")
    kind = kind if kind in db.RECORD_TYPES else "sales"
    criterion = request.args.get("criterion", "")
    objection = request.args.get("objection", "")
    risk = request.args.get("risk", "")
    records = db.get_all_records(record_type=kind, date_from=date_from or None, date_to=date_to or None)
    items = team.drill_down(records, kind, criterion=criterion, objection=objection, risk=risk)
    title = ""
    if criterion:
        rates = {c["key"]: c["title"] for c in team.criteria_rates(records, kind)}
        title = f"Не виконано: «{rates.get(criterion, criterion)}»"
    elif objection:
        title = f"Заперечення: «{objection}»"
    elif risk:
        title = f"Ризик: «{risk}»"
    return render_template("drill.html", items=items, title=title or "Записи",
                           filters={"date_from": date_from, "date_to": date_to, "kind": kind, "all": all_time})


@app.route("/person")
@login_required
def person_page():
    name = request.args.get("name", "").strip()
    if not name or (scope_person() is not None and name != scope_person()):
        abort(404)
    records = db.get_all_records(person_name=name)
    if not records and scope_person() is None:
        abort(404)  # для менеджера — порожній профіль замість «сторінку не знайдено»
    profile = team.person_profile(records)
    return render_template(
        "person.html", name=name, profile=profile, points=team.sparkline_points(profile["series"]),
        records=records[:20], plan=coaching.get_cached(name),
    )


_coaching_lock = threading.Lock()


@app.route("/person/coach", methods=["POST"])
@staff_required
def person_coach():
    name = request.args.get("name", "").strip()
    records = db.get_all_records(person_name=name) if name else []
    if not records:
        return _json_error("Записів цієї людини немає", 404)
    if not _coaching_lock.acquire(blocking=False):
        return _json_error("План уже генерується — зачекайте хвилину", 409)
    try:
        coaching.generate(name, records)
    except ValueError as e:
        return _json_error(str(e))
    except Exception as e:
        log.exception("Не вдалося згенерувати план розвитку")
        return _json_error(f"Не вдалося згенерувати план: {e}", 500)
    finally:
        _coaching_lock.release()
    return jsonify(ok=True)


def _snippet(text: str, query: str, radius: int = 110) -> str:
    position = (text or "").lower().find(query.lower())
    if position < 0:
        return (text or "")[: radius * 2]
    start = max(0, position - radius)
    return ("…" if start else "") + text[start: position + len(query) + radius] + "…"


@app.route("/search")
@login_required
def search_page():
    query = request.args.get("q", "").strip()[:200]
    results, error = [], None
    if len(query) >= 2:
        if not search_limiter.allowed(str(current_user.id)):
            error = "Забагато пошукових запитів — зачекайте хвилину"
        elif not _search_slots.acquire(timeout=10):
            error = "Сервер зайнятий іншими пошуками — спробуйте за кілька секунд"
        else:
            search_limiter.record_failure(str(current_user.id))  # лічильник запитів, а не помилок
            try:
                hide = manager_hides_transcript()
                for r in db.search_records(query, person_name=scope_person(), include_transcript=not hide):
                    sources = [strip_error_suffix(r.get("transcription") or ""), r.get("summary") or "",
                               r.get("manager_comment") or ""]
                    source = next((t for t in sources if query.lower() in t.lower()), sources[1] or sources[0])
                    r["snippet"] = _snippet(source, query)
                    r.pop("transcription", None)
                    results.append(r)
            finally:
                _search_slots.release()
    return render_template("search.html", query=query, results=results, error=error)


def _csv_cell(value) -> str:
    """Захист від формул у Excel (CSV injection): =, +, -, @ на початку."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@app.route("/export.csv")
@login_required
def export_csv():
    record_type = request.args.get("type", "")
    record_type = record_type if record_type in db.RECORD_TYPES else None
    date_from, date_to = _date_filters()
    person = scope_person() or request.args.get("person", "").strip() or None
    trainer = request.args.get("trainer", "").strip() or None
    records = db.get_all_records(record_type=record_type, person_name=person, trainer_name=trainer,
                                 date_from=date_from or None, date_to=date_to or None)
    status_labels = {"done": "Готово", "error": "Помилка", "queued": "В черзі", "processing": "Обробка",
                     "analyzing": "Обробка"}
    buffer = io.StringIO()
    buffer.write("\ufeff")  # BOM — Excel коректно відкриває кирилицю
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["ID", "Дата", "Час", "Тип", "Імʼя", "Тренер", "Статус", "Оцінка, %", "Чек-ліст, %",
                     "Виконано критеріїв", "Шанс угоди, %", "Теплота ліда", "Наступний крок",
                     "Результат продажу", "Сума, грн", "Резюме", "Невиконані критерії", "Посилання"])
    for r in records:
        a = r.get("analysis") or {}
        items = present_criteria(a, r["record_type"]) if a else []
        step = a.get("next_step") if isinstance(a.get("next_step"), dict) else {}
        writer.writerow([_csv_cell(v) for v in (
            r["id"], r["record_date"], r.get("record_time"),
            "Заняття" if r["record_type"] == "lesson" else "Продаж",
            r["person_name"], r.get("trainer_name"), status_labels.get(r["status"], r["status"]),
            main_score(a, r["record_type"]) if a else "",
            a.get("checklist_score", ""), f"{sum(1 for c in items if c['result'])}/{len(items)}" if items else "",
            a.get("deal_chance_percent", ""), a.get("lead_temperature", ""),
            step.get("description", "") if step.get("agreed") else ("не домовились" if step else ""),
            {1: "Продано", 0: "Не продано"}.get(r.get("sale_made"), ""),
            f"{r['sale_amount']:g}" if r.get("sale_amount") and r.get("sale_made") == 1 else "",
            a.get("summary", ""), "; ".join(c["title"] for c in items if not c["result"]),
            notify.record_link(r["id"]) or url_for("record_detail", record_id=r["id"], _external=True),
        )])
    filename = f"maysternia-{today_local()}.csv"
    return Response(buffer.getvalue(), content_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.route("/analytics")
@staff_required
def analytics_page():
    date_from, date_to = _date_filters()
    return render_template("analytics.html", insights=db.get_insights(date_from, date_to),
                           date_from=date_from, date_to=date_to)


@app.route("/analytics/generate", methods=["POST"])
@staff_required
def generate_analytics():
    data = _json_body()
    date_from = data.get("date_from", "") if _valid_date(data.get("date_from", "")) else ""
    date_to = data.get("date_to", "") if _valid_date(data.get("date_to", "")) else ""
    if date_from and date_to and date_from > date_to:
        return _json_error("Дата «Від» пізніша за «До»")
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
@staff_required
def upload():
    whisper_ok = transcription.is_configured()
    if request.method == "GET":
        return render_template("upload.html", today=today_local(), form={}, whisper_ok=whisper_ok,
                               people=db.get_person_names())

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
                               whisper_ok=whisper_ok, people=db.get_person_names()), 400

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
    if manager_hides_transcript():
        abort(404)
    if scope_person() is not None:  # менеджер — лише файли власних записів
        owner = db.fetch_one("SELECT person_name FROM records WHERE filename = ? LIMIT 1", (filename,))
        if not owner or not can_view_record(owner):
            abort(404)
    return send_from_directory(UPLOAD_FOLDER, filename, conditional=True)


# ── Запис ─────────────────────────────────────────────────────────────────────

@app.route("/record/<int:record_id>")
@login_required
def record_detail(record_id):
    record = _record_or_404(record_id)
    has_transcript = is_valid_transcript(record.get("transcription"))
    show_transcript = has_transcript and (current_user.is_staff()
                                          or app_settings.get("manager_can_see_transcript"))
    transcript_lines = []
    if show_transcript:
        for line in strip_error_suffix(record["transcription"]).split("\n"):
            seconds, label, text = split_timecode(line)
            transcript_lines.append({"seconds": seconds, "label": label, "text": text})
    back = request.referrer or ""
    back_url = back if back.startswith(request.host_url) and "/record/" not in back else url_for("dashboard")
    return render_template(
        "record.html", record=record, has_transcript=has_transcript, back_url=back_url,
        talk=talk_stats(record["transcription"]) if show_transcript else None,
        media_url=None if manager_hides_transcript() else _media_url(record),
        transcript_lines=transcript_lines, show_transcript=show_transcript,
        criteria=present_criteria(record.get("analysis"), record["record_type"]) if record.get("analysis") else [],
        source=pipeline.record_source(record), meta=pipeline.source_meta(record),
    )


@app.route("/record/<int:record_id>/status")
@login_required
def record_status(record_id):
    record = _record_or_404(record_id)
    waiting = bool(record["status"] == "queued" and record.get("not_before")
                   and record["not_before"] > db.utcnow_iso())
    return jsonify(status=record["status"], error_message=record.get("error_message") or "",
                   waiting=waiting, not_before=utc_iso_to_local(record.get("not_before") or ""),
                   analysis_only=record.get("job_kind") in ("analyze", "reanalyze"))


@app.route("/record/<int:record_id>/sale_result", methods=["POST"])
@staff_required
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
@staff_required
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
@staff_required
def save_comment(record_id):
    _record_or_404(record_id)
    data = _json_body()
    if not isinstance(data.get("comment"), str):
        return _json_error("Не передано текст коментаря")
    comment = data["comment"][:10_000]
    db.update_comment(record_id, comment)
    return jsonify(ok=True)


@app.route("/record/<int:record_id>/reanalyze", methods=["POST"])
@staff_required
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
