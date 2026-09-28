"""
Налаштування застосунку, що змінюються з адмінки («Налаштування»).

Зберігаються одним JSON у таблиці app_settings (ключ "settings"); значення за
замовчуванням — з changeable-констант нижче та змінних середовища. Кешуються на
кілька секунд, щоб не читати БД на кожен виклик.
"""
import json
import os
import re
import threading
import time

import database as db

SETTINGS_KEY = "settings"
CACHE_SECONDS = 10

DEFAULT_COMPANY_CONTEXT = (
    "Онлайн-школа «Майстерня скілів» навчає харизмі, впевненості та публічним виступам. "
    "Основний курс — «Код Харизми», вартість 15 000–30 000 грн. Пробне заняття веде тренер "
    "у групі потенційних студентів; після нього менеджер продажів спілкується з клієнтом "
    "один на один і пропонує курс."
)

AI_MODELS = [
    ("claude-sonnet-4-6", "Claude Sonnet 4.6 — поточна (рекомендовано)"),
    ("claude-sonnet-5", "Claude Sonnet 5 — новіша, дешевша за токен, «думає» (довше)"),
    ("claude-opus-5", "Claude Opus 5 — найглибший аналіз, дорожча"),
    ("claude-haiku-4-5", "Claude Haiku 4.5 — найдешевша, простіший аналіз"),
]


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# Тип кожного поля визначає валідацію; значення — дефолт.
FIELDS = {
    "company_context": ("text", DEFAULT_COMPANY_CONTEXT),
    "anthropic_model": ("model", ""),
    "low_score_threshold": ("int:0:100", 50),
    "telegram_bot_token": ("secret", ""),
    "telegram_chat_ids": ("chat_ids", ""),
    "notify_on_done": ("bool", True),
    "notify_low_score": ("bool", True),
    "daily_digest_enabled": ("bool", False),
    "daily_digest_time": ("time", "19:00"),
    "webhook_url": ("url", ""),
    "webhook_secret": ("secret", ""),
    "zoom_min_duration_minutes": ("int:0:600", _env_int("ZOOM_MIN_DURATION_MINUTES", 3)),
    "zoom_transcript_wait_minutes": ("int:0:1440", _env_int("ZOOM_TRANSCRIPT_WAIT_MINUTES", 180)),
    "manager_can_see_transcript": ("bool", True),
    "notify_errors": ("bool", False),
    "error_chat_ids": ("chat_ids", ""),
}

_lock = threading.Lock()
_cache = {"at": 0.0, "data": None}


class SettingsError(ValueError):
    pass


LABELS = {
    "low_score_threshold": "Поріг низької оцінки",
    "zoom_min_duration_minutes": "Мінімальна тривалість запису Zoom",
    "zoom_transcript_wait_minutes": "Очікування транскрипції Zoom",
}


def _stored() -> dict:
    raw = db.get_setting(SETTINGS_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def get_all() -> dict:
    with _lock:
        if _cache["data"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS:
            return dict(_cache["data"])
    data = {key: default for key, (_, default) in FIELDS.items()}
    for key, value in _stored().items():
        if key not in FIELDS:
            continue
        try:  # пошкоджене/застаріле значення в БД не повинне ламати застосунок
            data[key] = _validate(key, value)
        except SettingsError:
            pass
    with _lock:
        _cache.update(at=time.monotonic(), data=data)
    return dict(data)


def get(key: str):
    return get_all()[key]


def invalidate_cache() -> None:
    with _lock:
        _cache.update(at=0.0, data=None)


def _validate(key: str, value):
    kind = FIELDS[key][0]
    if kind == "bool":
        return value in (True, "1", "on", "true", 1)
    if kind.startswith("int"):
        _, low, high = kind.split(":")
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            raise SettingsError(f"«{LABELS.get(key, key)}»: потрібне ціле число")
        if not int(low) <= number <= int(high):
            raise SettingsError(f"«{LABELS.get(key, key)}»: значення має бути від {low} до {high}")
        return number
    value = "" if value is None else str(value).strip()
    if kind == "text":
        if len(value) > 5000:
            raise SettingsError("Опис компанії завеликий (максимум 5000 символів)")
        return value or DEFAULT_COMPANY_CONTEXT
    if kind == "model":
        if value and not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{2,60}", value):
            raise SettingsError("Некоректна назва моделі")
        return value
    if kind == "time":
        value = value or FIELDS[key][1]
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
            raise SettingsError("Час має бути у форматі ГГ:ХХ")
        return value
    if kind == "url":
        if value and not re.fullmatch(r"https://[^\s]{4,500}", value):
            raise SettingsError("Адреса вебхука має починатися з https://")
        return value
    if kind == "chat_ids":
        ids = [part.strip() for part in re.split(r"[,\s]+", value) if part.strip()]
        for chat_id in ids:
            if not re.fullmatch(r"-?\d{3,20}|@[A-Za-z0-9_]{4,64}", chat_id):
                raise SettingsError(f"Некоректний Telegram chat id: {chat_id}")
        return ", ".join(ids)
    if kind == "secret":
        if len(value) > 500:
            raise SettingsError("Занадто довге значення")
        return value
    raise SettingsError(f"Невідомий тип поля {key}")


def update(changes: dict) -> dict:
    """Перевіряє та зберігає зміни. Секрети з порожнім значенням не затирають збережені."""
    stored = _stored()
    for key, value in changes.items():
        if key not in FIELDS:
            continue
        if FIELDS[key][0] == "secret" and value in (None, "") and not changes.get(f"clear_{key}"):
            continue  # поле пароля порожнє — лишаємо як було
        stored[key] = _validate(key, value)
    db.set_setting(SETTINGS_KEY, json.dumps(stored, ensure_ascii=False))
    invalidate_cache()
    return get_all()


def chat_ids() -> list[str]:
    return [c.strip() for c in (get("telegram_chat_ids") or "").split(",") if c.strip()]


def error_chat_ids() -> list[str]:
    """Куди слати помилки системи: окремі chat id або, якщо не задані, основні."""
    return [c.strip() for c in (get("error_chat_ids") or "").split(",") if c.strip()] or chat_ids()


def mask(secret: str) -> str:
    if not secret:
        return ""
    return "••••" + secret[-3:] if len(secret) > 16 else "••••"
