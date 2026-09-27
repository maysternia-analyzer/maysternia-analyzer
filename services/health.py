"""Перевірка інтеграцій для сторінки «Система» (лише для адміністратора)."""
import os

from services import llm, transcription, zoom


def config_summary() -> dict:
    return {
        "zoom": zoom.is_configured(),
        "zoom_webhook_secret": bool(zoom.webhook_secret()),
        "anthropic": llm.is_configured(),
        "anthropic_model": llm.model(),
        "openai": transcription.is_configured(),
        "ffmpeg": transcription.ffmpeg_path(),
        "database": "PostgreSQL" if os.environ.get("DATABASE_URL") else "SQLite",
    }


def _check(fn) -> dict:
    try:
        return {"ok": True, "detail": fn()}
    except Exception as e:  # показуємо адміну текст помилки як є
        return {"ok": False, "detail": str(e)[:300]}


def check_zoom() -> dict:
    def run():
        zoom.get_access_token(force_refresh=True)
        return "OAuth-токен отримано"
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
