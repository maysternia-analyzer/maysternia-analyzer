"""Перевірка інтеграцій для сторінки «Система» (лише для адміністратора)."""
import os

from services import llm, transcription, version, zoom


def config_summary() -> dict:
    return {
        "zoom": zoom.is_configured(),
        "zoom_webhook_secret": bool(zoom.webhook_secret()),
        "anthropic": llm.is_configured(),
        "anthropic_model": llm.model(),
        "openai": transcription.is_configured(),
        "ffmpeg": transcription.ffmpeg_path(),
        "database": "PostgreSQL" if os.environ.get("DATABASE_URL") else "SQLite",
        "version": version.label(),
    }


def _check(fn) -> dict:
    try:
        return {"ok": True, "detail": fn()}
    except Exception as e:  # показуємо адміну текст помилки як є
        return {"ok": False, "detail": str(e)[:300]}


def check_zoom() -> dict:
    def run():
        from datetime import datetime, timedelta, timezone
        zoom.get_access_token(force_refresh=True)
        today = datetime.now(timezone.utc).date()
        meetings = zoom.list_recordings(today - timedelta(days=90), today)
        last = max((m.get("start_time") or "" for m in meetings), default="")
        scope = ("записи всіх організаторів акаунта" if zoom.account_wide() else
                 "лише записи власника Zoom-застосунку (щоб бачити всіх організаторів, додайте застосунку "
                 "scope cloud_recording:read:list_account_recordings:admin)")
        latest = f"останній запис {last[:10]}" if last else "за 90 днів хмарних записів немає"
        return f"OAuth-токен отримано; доступ: {scope}; записів за 90 днів: {len(meetings)}, {latest}"
    return _check(run) if zoom.is_configured() else {"ok": False, "detail": "ZOOM_* не задані"}


def check_anthropic() -> dict:
    def run():
        info = llm.client().with_options(timeout=20, max_retries=0).models.retrieve(llm.model())
        return f"модель {info.id} доступна"
    return _check(run) if llm.is_configured() else {"ok": False, "detail": "ANTHROPIC_API_KEY не задано"}


def check_openai() -> dict:
    def run():
        from openai import OpenAI
        OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=20, max_retries=0).models.retrieve(transcription.MODEL)
        return f"ключ дійсний, модель {transcription.MODEL} доступна"
    return _check(run) if transcription.is_configured() else {"ok": False, "detail": "OPENAI_API_KEY не задано"}


def run_all_checks() -> dict:
    return {"Zoom": check_zoom(), "Anthropic (Claude)": check_anthropic(), "OpenAI (Whisper)": check_openai()}
