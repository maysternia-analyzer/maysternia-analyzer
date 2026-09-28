"""
Сповіщення: Telegram (підсумок аналізу, низька оцінка, щоденний звіт керівнику)
та вихідний вебхук (інтеграція з CRM / Make / Zapier). Помилки лише логуються.
"""
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests

import database as db
from services import settings, team, version
from services.analysis import main_score
from services.timeutil import LOCAL_TZ

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4000
HOT_LEAD_PERCENT = 60


def public_base_url() -> str:
    url = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if url:
        return url
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    return f"https://{domain}" if domain else ""


def record_link(record_id: int) -> str:
    base = public_base_url()
    return f"{base}/record/{record_id}" if base else ""


def _redact(text: str, token: str) -> str:
    return str(text).replace(token, "***") if token else str(text)


# ── Telegram ──────────────────────────────────────────────────────────────────

def telegram_configured() -> bool:
    return bool(settings.get("telegram_bot_token") and settings.chat_ids())


def send_telegram(text: str, chat_ids: list[str] | None = None) -> list[str]:
    """Надсилає HTML-повідомлення. Повертає список помилок (порожній — успіх)."""
    token = settings.get("telegram_bot_token")
    targets = chat_ids if chat_ids is not None else settings.chat_ids()
    if not token or not targets:
        return ["Telegram не налаштовано (токен бота або chat id)"]
    payload = {"text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if len(text) > TELEGRAM_LIMIT:
        # Обрізання HTML може розрізати тег чи сутність → Telegram відхилить усе повідомлення.
        plain = html.unescape(re.sub(r"<[^>]+>", "", text))
        payload = {"text": plain[:TELEGRAM_LIMIT] + "…", "disable_web_page_preview": True}
    errors = []
    for chat_id in targets:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, **payload},
                timeout=10,
            )
            if resp.status_code != 200:
                description = resp.json().get("description", resp.text[:200]) if resp.content else ""
                errors.append(f"{chat_id}: {description}")
        except (requests.RequestException, ValueError) as e:
            errors.append(f"{chat_id}: {_redact(e, token)}")
    for error in errors:
        log.warning("Telegram: %s", error)
    return errors


_error_alerts: list[float] = []
_error_alerts_lock = threading.Lock()
ERROR_ALERTS_PER_HOUR = 10


def alert_error(entry: dict) -> None:
    """Нова (не повторна) помилка з журналу → Telegram адміну. Не частіше 10 на годину на процес."""
    if not settings.get("notify_errors"):
        return
    now = time.monotonic()
    with _error_alerts_lock:
        _error_alerts[:] = [t for t in _error_alerts if now - t < 3600]
        if len(_error_alerts) >= ERROR_ALERTS_PER_HOUR:
            return
        _error_alerts.append(now)
    try:
        ctx = json.loads(entry.get("context") or "{}")
    except ValueError:
        ctx = {}
    where = " · ".join(f"{k}: {v}" for k, v in ctx.items() if k in ("req", "user", "record", "job"))
    base = public_base_url()
    link = f'\n<a href="{base}/admin/logs?group=errors">Відкрити журнал</a>' if base else ""
    text = (f"🛑 <b>Помилка</b> · Майстерня Аналізатор {version.label()}\n"
            f"{_esc(entry.get('message', ''))[:600]}"
            + (f"\n<i>{_esc(where)[:300]}</i>" if where else "") + link)
    send_telegram(text, settings.error_chat_ids())


def _esc(value) -> str:
    return html.escape(str(value or ""), quote=False)


def _esc_ai(value) -> str:
    """Текст від AI: прибираємо посилання (Telegram робить їх клікабельними) та екрануємо."""
    return _esc(re.sub(r"(?:https?://|www\.)\S+", "[посилання]", str(value or ""), flags=re.I))


def format_record_message(record: dict) -> str:
    a = record.get("analysis") or {}
    kind = record["record_type"]
    score = main_score(a, kind)
    icon = "🎓" if kind == "lesson" else "💰"
    lines = [f"{icon} <b>{_esc(record.get('person_name'))}</b> · {_esc(record.get('record_date'))} "
             f"{_esc(record.get('record_time'))}".rstrip()]
    if score is not None:
        mark = "🟢" if score >= 75 else "🟡" if score >= 50 else "🔴"
        lines.append(f"{mark} Оцінка: <b>{score}%</b>")
    if kind == "sales" and a.get("deal_chance"):
        lines.append(f"🎯 Шанс угоди: {_esc(a.get('deal_chance_percent'))}% ({_esc(a.get('deal_chance'))}), "
                     f"лід: {_esc(a.get('lead_temperature'))}")
        step = a.get("next_step") or {}
        if isinstance(step, dict):
            lines.append("➡️ Наступний крок: " + (_esc_ai(step.get("description")) if step.get("agreed")
                                                 else "не домовились"))
    if a.get("summary"):
        lines.append(f"\n{_esc_ai(a['summary'])}")
    risks = [r.get("category") for r in a.get("risks") or [] if isinstance(r, dict)]
    if risks:
        lines.append("⚠️ Ризики: " + _esc(", ".join(dict.fromkeys(risks))))
    link = record_link(record["id"])
    if link:
        lines.append(f'\n<a href="{html.escape(link)}">Відкрити запис</a>')
    return "\n".join(lines)


# ── Вебхук ────────────────────────────────────────────────────────────────────

def webhook_payload(record: dict) -> dict:
    a = record.get("analysis") or {}
    return {
        "event": "record.analyzed",
        "record": {
            "id": record["id"], "url": record_link(record["id"]), "type": record["record_type"],
            "person_name": record.get("person_name"), "trainer_name": record.get("trainer_name"),
            "date": record.get("record_date"), "time": record.get("record_time"),
            "score": main_score(a, record["record_type"]), "checklist_score": a.get("checklist_score"),
            "deal_chance": a.get("deal_chance"), "deal_chance_percent": a.get("deal_chance_percent"),
            "lead_temperature": a.get("lead_temperature"), "summary": a.get("summary"),
            "client": a.get("client"), "next_step": a.get("next_step"),
            "objections": a.get("objections"), "risks": a.get("risks"),
        },
    }


def _is_public_host(url: str) -> bool:
    """Вебхук не повинен ходити у внутрішню мережу сервера (localhost, приватні адреси)."""
    try:
        parsed = urlparse(url)
        port = parsed.port or 443
    except ValueError:  # «https://host:99999», «https://[::1/x» тощо
        return False
    if parsed.username or parsed.password or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, port, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return False
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if not address.is_global:  # приватні, loopback, link-local, CGNAT 100.64/10 тощо
            return False
    return bool(infos)


def sign_webhook(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def post_webhook(payload: dict) -> str | None:
    """
    POST JSON. Заголовки: X-Maysternia-Timestamp (unix-час) і
    X-Maysternia-Signature: sha256=HMAC(секрет, "<timestamp>.<тіло>") — отримувач може
    відкидати повторені/застарілі запити. Повертає помилку або None.
    """
    url = settings.get("webhook_url")
    if not url:
        return "Вебхук не налаштовано"
    if not _is_public_host(url):
        return "Адреса вебхука має бути публічною (не localhost / внутрішня мережа, без логіна в URL)"
    body = json.dumps(payload, ensure_ascii=False).encode()
    timestamp = str(int(time.time()))
    headers = {"Content-Type": "application/json", "User-Agent": "maysternia-analyzer",
               "X-Maysternia-Timestamp": timestamp}
    secret = settings.get("webhook_secret")
    if secret:
        headers["X-Maysternia-Signature"] = sign_webhook(secret, timestamp, body)
    try:
        resp = requests.post(url, data=body, headers=headers, timeout=10, allow_redirects=False)
    except requests.RequestException as e:
        log.warning("Вебхук: помилка зʼєднання (%s)", type(e).__name__)
        return f"Помилка зʼєднання ({type(e).__name__})"
    if resp.status_code >= 300:
        log.warning("Вебхук відповів %s", resp.status_code)
        return f"Сервер відповів {resp.status_code}"
    return None


# ── Після аналізу запису ──────────────────────────────────────────────────────

def notify_record_done(record_id: int) -> None:
    """Викликається конвеєром після успішного аналізу. Ніколи не кидає виключень."""
    try:
        record = db.get_record(record_id)
        if not record or not record.get("analysis"):
            return
        config = settings.get_all()
        a = record["analysis"]
        score = main_score(a, record["record_type"])
        alerts = []
        if score is not None and score < config["low_score_threshold"]:
            alerts.append("🚨 <b>Низька оцінка</b>")
        step = a.get("next_step") if isinstance(a.get("next_step"), dict) else {}
        if (record["record_type"] == "sales" and (a.get("deal_chance_percent") or 0) >= HOT_LEAD_PERCENT
                and step and not step.get("agreed")):
            alerts.append("🔥 <b>Гарячий лід без наступного кроку</b> — зв'яжіться з клієнтом сьогодні")
        alerts = alerts if config["notify_low_score"] else []
        if telegram_configured() and (config["notify_on_done"] or alerts):
            send_telegram("\n".join(alerts + [format_record_message(record)]))
        if config["webhook_url"]:
            post_webhook(webhook_payload(record))
    except Exception:
        log.exception("Сповіщення для запису #%s не надіслано", record_id)


# ── Щоденний звіт ─────────────────────────────────────────────────────────────

DIGEST_SENT_AT_KEY = "digest_last_sent_at"
DIGEST_MAX_WINDOW_HOURS = 48


def build_digest(records: list[dict], title: str) -> str | None:
    """Текст звіту за набором записів. None — якщо проаналізованих немає."""
    records = [r for r in records if r.get("analysis")]
    if not records:
        return None
    sales = [r for r in records if r["record_type"] == "sales"]
    lessons = [r for r in records if r["record_type"] == "lesson"]
    lines = [f"📊 <b>{_esc(title)}</b>",
             f"Проаналізовано: {len(records)} (занять {len(lessons)}, продажів {len(sales)})"]
    high = sum(1 for r in sales if r["analysis"].get("deal_chance") == "Високий")
    if sales:
        lines.append(f"Високий шанс угоди: {high} з {len(sales)}")
    rating = team.people_rating(records)
    if rating:
        lines.append("\n<b>Рейтинг</b>")
        for p in rating[:10]:
            lines.append(f"• {_esc(p['name'])} — {p['avg_score']}% ({p['calls']})")
    threshold = settings.get("low_score_threshold")
    low = [r for r in records if (main_score(r["analysis"], r["record_type"]) or 0) < threshold]
    if low:
        lines.append(f"\n🔴 <b>Нижче {threshold}%</b>")
        for r in low[:8]:
            link = record_link(r["id"])
            label = f"{_esc(r['person_name'])} — {main_score(r['analysis'], r['record_type'])}%"
            lines.append(f'• <a href="{html.escape(link)}">{label}</a>' if link else f"• {label}")
    weak = [c for c in team.criteria_rates(records, "sales") + team.criteria_rates(records, "lesson")
            if c["rate"] < 60][:5]
    if weak:
        lines.append("\n<b>Слабкі місця</b>")
        lines += [f"• {_esc(c['title'])} — {c['rate']}%" for c in weak]
    objections = team.objection_stats(records)[:5]
    if objections:
        lines.append("\n<b>Заперечення</b>: " + _esc(", ".join(f"{o['category']} ×{o['count']}" for o in objections)))
    return "\n".join(lines)


def build_daily_digest(day: str) -> str | None:
    """Звіт за конкретну дату запису (кнопка «Надіслати звіт за день»)."""
    return build_digest(db.get_all_records(date_from=day, date_to=day), f"Звіт за {day}")


def send_daily_digest_if_due(now: datetime | None = None) -> bool:
    """
    Щоденний звіт після заданого часу: усе, що проаналізовано з моменту попереднього звіту
    (тож вечірні дзвінки потрапляють у наступний). Один раз на день навіть за кількох
    процесів/контейнерів — атомарне «захоплення» ключа дня в БД. True — звіт відправлено.
    """
    config = settings.get_all()
    if not config["daily_digest_enabled"] or not telegram_configured():
        return False
    now = now or datetime.now(LOCAL_TZ)
    today = now.date().isoformat()
    if now.strftime("%H:%M") < config["daily_digest_time"]:
        return False
    claim = f"{os.getpid()}:{time.time()}"
    if db.set_setting_if_absent(f"digest:{today}", claim) != claim:
        return False  # сьогодні вже надіслав інший процес або цей
    now_utc = db.utcnow_iso()
    oldest = db.utcnow_iso(-timedelta(hours=DIGEST_MAX_WINDOW_HOURS))
    since = max(db.get_setting(DIGEST_SENT_AT_KEY) or "", oldest)
    text = build_digest(db.get_records_analyzed_since(since), f"Звіт за {today}")
    if text is None:
        db.set_setting(DIGEST_SENT_AT_KEY, now_utc)
        return True
    errors = send_telegram(text)
    if not errors:  # при помилці наступний звіт охопить і ці записи
        db.set_setting(DIGEST_SENT_AT_KEY, now_utc)
    return not errors


def is_valid_webhook_url(url: str) -> bool:
    return bool(re.fullmatch(r"https://[^\s]{4,500}", url or ""))
