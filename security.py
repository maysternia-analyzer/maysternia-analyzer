"""Безпека веб-застосунку: секретний ключ, CSRF, обмеження спроб входу, безпечні редіректи."""
import hmac
import logging
import os
import secrets
import threading
import time
from collections import OrderedDict, deque
from urllib.parse import urlparse

from flask import abort, jsonify, request, session

import database as db

log = logging.getLogger(__name__)

# Значення, що є в публічному репозиторії або були дефолтними, — не можна використовувати.
PUBLIC_SECRET_KEYS = {"maysternia-secret-key-2024", "maysternia-dev-key", "change-me", ""}
CSRF_SESSION_KEY = "_csrf_token"
CSRF_HEADER = "X-CSRFToken"
CSRF_FIELD = "csrf_token"


def is_strong_secret(value: str | None) -> bool:
    value = (value or "").strip()
    return len(value) >= 16 and value not in PUBLIC_SECRET_KEYS and not value.startswith("change-me")


def resolve_secret_key() -> tuple[str, str]:
    """
    Ключ підпису сесій. Якщо SECRET_KEY не задано або він публічно відомий,
    генеруємо випадковий і зберігаємо в БД (спільний для всіх процесів).
    Повертає (ключ, джерело).
    """
    env_key = os.environ.get("SECRET_KEY", "")
    if is_strong_secret(env_key):
        return env_key.strip(), "env"
    log.warning("SECRET_KEY не задано або він публічно відомий — використовуємо згенерований ключ із БД")
    return db.set_setting_if_absent("flask_secret_key", secrets.token_hex(32)), "database"


# ── CSRF ──────────────────────────────────────────────────────────────────────

def csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def csrf_protect(exempt_endpoints: set[str]) -> None:
    """before_request: перевіряє токен для всіх змінюючих запитів."""
    if request.method in ("GET", "HEAD", "OPTIONS") or request.endpoint in exempt_endpoints:
        return
    expected = session.get(CSRF_SESSION_KEY)
    sent = request.headers.get(CSRF_HEADER) or request.form.get(CSRF_FIELD)
    if expected and sent and hmac.compare_digest(expected, sent):
        return
    log.warning("CSRF: відхилено %s %s", request.method, request.path)
    if request.is_json or request.headers.get(CSRF_HEADER) is not None:
        response = jsonify(ok=False, error="Сесія застаріла — оновіть сторінку")
        response.status_code = 400
        abort(response)
    abort(400, description="Сесія застаріла або форма недійсна — оновіть сторінку й спробуйте ще раз.")


# ── Редіректи ─────────────────────────────────────────────────────────────────

def is_safe_next_url(target: str | None) -> bool:
    """Дозволяємо лише відносні шляхи цього сайту (захист від open redirect)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return False
    parsed = urlparse(target)
    return not parsed.scheme and not parsed.netloc


# ── Обмеження спроб входу ─────────────────────────────────────────────────────

class LoginRateLimiter:
    """
    Не більше max_failures невдалих спроб за window секунд на ключ.
    При переповненні витісняються лише незаблоковані ключі (найдавніші першими), тож перебір
    тисяч вигаданих email не скидає блокування атакованого акаунта.
    """

    def __init__(self, max_failures: int = 10, window: int = 900, max_keys: int = 10_000):
        self.max_failures = max_failures
        self.window = window
        self.max_keys = max_keys
        self._failures: OrderedDict[str, deque] = OrderedDict()
        self._lock = threading.Lock()

    def _trim(self, key: str, now: float) -> deque:
        attempts = self._failures.get(key)
        if attempts is None:
            attempts = self._failures[key] = deque()
        self._failures.move_to_end(key)
        while attempts and now - attempts[0] > self.window:
            attempts.popleft()
        return attempts

    def allowed(self, key: str) -> bool:
        with self._lock:
            now = time.monotonic()
            attempts = self._failures.get(key)
            if attempts is None:
                return True
            while attempts and now - attempts[0] > self.window:
                attempts.popleft()
            return len(attempts) < self.max_failures

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._trim(key, now).append(now)
            if len(self._failures) > self.max_keys:
                self._evict(now)

    def _evict(self, now: float) -> None:
        for key in list(self._failures):  # від найдавніше використаних
            attempts = self._failures[key]
            while attempts and now - attempts[0] > self.window:
                attempts.popleft()
            if len(attempts) < self.max_failures:
                del self._failures[key]
                if len(self._failures) <= self.max_keys:
                    return

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
