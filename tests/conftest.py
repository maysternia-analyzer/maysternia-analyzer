"""
Спільні фікстури тестів.

За замовчуванням тести працюють на тимчасовій SQLite. Щоб прогнати їх на PostgreSQL:
    TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/test_db pytest
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# /dev/shm (tmpfs) робить SQLite-тести швидшими на повільних дисках.
_TMP = Path(tempfile.mkdtemp(prefix="maysternia-tests-",
                             dir="/dev/shm" if os.path.isdir("/dev/shm") else None))

# Середовище задаємо ДО імпорту застосунку (модулі читають його при імпорті).
for key in ("ZOOM_ACCOUNT_ID", "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET", "ZOOM_WEBHOOK_SECRET",
            "OPENAI_API_KEY", "ANTHROPIC_MODEL", "DATABASE_URL", "RAILWAY_ENVIRONMENT_NAME",
            "RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID"):
    os.environ.pop(key, None)
os.environ.update({
    "BACKGROUND_JOBS": "0",
    "SECRET_KEY": "test-secret-key-that-is-long-enough",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "SQLITE_PATH": str(_TMP / "test.db"),
    "WORKER_LOCK_PATH": str(_TMP / "worker.lock"),
    "LOG_LEVEL": "WARNING",
})
if os.environ.get("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]

import database as db  # noqa: E402

db.init_db()

TABLES = ("records", "users", "insights_cache", "zoom_processed", "webhook_log", "app_settings")


@pytest.fixture(autouse=True)
def clean_db():
    """Кожен тест стартує з порожніх таблиць (ключ сесій зберігаємо)."""
    for table in TABLES:
        if table == "app_settings":
            db.execute("DELETE FROM app_settings WHERE key != 'flask_secret_key'")
        else:
            db.execute(f"DELETE FROM {table}")
    yield


@pytest.fixture
def zoom_env(monkeypatch):
    monkeypatch.setenv("ZOOM_ACCOUNT_ID", "acc")
    monkeypatch.setenv("ZOOM_CLIENT_ID", "cid")
    monkeypatch.setenv("ZOOM_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ZOOM_WEBHOOK_SECRET", "whsecret")
    from services import zoom
    zoom._token_cache.update(token="cached-token", expires_at=10**12)
    yield
    zoom._token_cache.update(token=None, expires_at=0)


@pytest.fixture
def flask_app():
    import app as app_module
    app_module.app.config.update(TESTING=True)
    app_module.login_limiter._failures.clear()
    app_module.email_limiter._failures.clear()
    app_module._bad_signature_logged_at[0] = 0.0
    return app_module.app


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


CSRF = "test-csrf-token"


def make_user(email="admin@example.com", password="password123", role="admin", name="Адмін", active=True):
    from werkzeug.security import generate_password_hash
    db.create_user(email, name, generate_password_hash(password), role)
    user = db.get_user_by_email(email)
    if not active:
        db.update_user(user["id"], is_active=0)
    return db.get_user_by_email(email)


def session_user_id(user):
    """Ідентифікатор сесії Flask-Login: id + мітка поточного пароля."""
    import app as app_module
    return f"{user['id']}:{app_module._credential_tag(user['password_hash'])}"


def login(client, user):
    """Логін через сесію + CSRF-токен у сесії."""
    with client.session_transaction() as sess:
        sess["_user_id"] = session_user_id(user)
        sess["_fresh"] = True
        sess["_csrf_token"] = CSRF


def set_csrf(client):
    with client.session_transaction() as sess:
        sess["_csrf_token"] = CSRF


def post_json(client, url, data=None):
    return client.post(url, json=data or {}, headers={"X-CSRFToken": CSRF})


@pytest.fixture
def admin_client(client):
    user = make_user()
    login(client, user)
    client.user = user
    return client


@pytest.fixture
def viewer_client(client):
    user = make_user(email="viewer@example.com", role="viewer", name="Глядач")
    login(client, user)
    client.user = user
    return client


def sample_lesson_analysis(score=80):
    from services.analysis import normalize_lesson
    return normalize_lesson({
        "greeting": {"result": True, "comment": "Привіталась"},
        "overall_score": score, "engagement_level": "Високий",
        "strengths": "Добре", "improvements": "Краще", "summary": "Ок",
    })


def sample_sales_analysis(score=60, chance="Середній"):
    from services.analysis import normalize_sales
    result = normalize_sales({
        "need_identified": {"result": True, "details": "хоче впевненості"},
        "deal_chance": chance, "deal_chance_percent": 55, "lead_temperature": "Теплий",
        "top_mistakes": ["помилка"], "recommendations": "рекомендація", "next_contact_script": "скрипт",
        "summary": "Менеджер виявив потребу, але не закрив угоду.",
        "objections": [{"text": "Дорого", "category": "ціна", "handled": False, "manager_response": "…",
                        "better_response": "Порівняймо з вартістю…"}],
        "risks": [{"category": "немає наступного кроку", "description": "не домовились"}],
        "next_step": {"agreed": False, "description": "", "deadline": ""},
        "key_moments": [{"time": "00:01:05", "quote": "Це дорого", "comment": "заперечення", "positive": False}],
        "coaching_phrases": [{"situation": "ціна", "phrase": "Давайте порахуємо вартість одного заняття"}],
    })
    result["checklist_score"] = score  # у тестах оцінку задаємо напряму
    return result
