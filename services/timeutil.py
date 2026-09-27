"""Робота з часом: Zoom повертає UTC, у дашборді показуємо локальний час школи."""
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    LOCAL_TZ = ZoneInfo(os.environ.get("APP_TIMEZONE", "Europe/Kyiv"))
except ZoneInfoNotFoundError:  # немає tzdata в системі
    LOCAL_TZ = timezone.utc


def parse_iso(value: str) -> datetime | None:
    """ISO-рядок → aware datetime (рядки без зони вважаємо UTC)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def zoom_start_to_local(start_time: str) -> tuple[str, str]:
    """'2026-07-29T15:44:03Z' → ('2026-07-29', '18:44') у локальному часовому поясі."""
    dt = parse_iso(start_time)
    if dt is None:
        return datetime.now(LOCAL_TZ).date().isoformat(), ""
    local = dt.astimezone(LOCAL_TZ)
    return local.date().isoformat(), local.strftime("%H:%M")


def utc_iso_to_local(value: str, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Службові мітки часу (UTC) → локальний час для відображення."""
    dt = parse_iso(value)
    if dt is None:
        return value or ""
    return dt.astimezone(LOCAL_TZ).strftime(fmt)


def today_local() -> str:
    return datetime.now(LOCAL_TZ).date().isoformat()
