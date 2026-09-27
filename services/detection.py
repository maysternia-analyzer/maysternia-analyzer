"""Визначення типу Zoom-запису (заняття / продаж) та імені тренера чи менеджера."""
import logging

from services import llm
from services.transcript_text import IGNORED_SPEAKERS, speaker_stats

log = logging.getLogger(__name__)

DETECT_SYSTEM = """Ти аналізуєш запис онлайн-школи "Майстерня скілів" (розвиток харизми).

Контекст:
- "Пробне заняття" (lesson) = тренер проводить групове заняття з вправами з харизми/впевненості
- "Продаж" (sales) = менеджер один на один з клієнтом, обговорює курс "Код Харизми" за 15 000–30 000 грн

Визнач тип запису та імʼя людини, яка веде зустріч (тренер для lesson, менеджер для sales).
Імʼя бери зі списку спікерів, як воно записане в Zoom. Не використовуй назву акаунта організатора
(наприклад, "Код Харизми") як імʼя ведучого."""

DETECT_SCHEMA = {
    "type": "object",
    "properties": {
        "record_type": {"type": "string", "enum": ["lesson", "sales"]},
        "person_name": {"type": "string"},
        "confidence": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["record_type", "person_name", "confidence", "reason"],
    "additionalProperties": False,
}


def _heuristic_type(duration: int, is_breakout: bool) -> str | None:
    if is_breakout and duration <= 60:
        return "sales"   # коротка breakout-кімната — індивідуальний продаж
    if not is_breakout and duration >= 60:
        return "lesson"  # довга головна кімната — групове заняття
    return None


def guess_type(duration: int, is_breakout: bool) -> str:
    return _heuristic_type(duration, is_breakout) or ("sales" if duration < 60 else "lesson")


def detect_type_and_name(topic: str, duration: int, is_breakout: bool,
                         transcript: str, fallback_name: str = "Невідомо") -> dict:
    """Повертає {record_type, person_name, reason}. Ніколи не кидає виключень."""
    speakers = speaker_stats(transcript)
    top_speaker = speakers[0]["speaker"] if speakers else ""
    heuristic = _heuristic_type(duration, is_breakout)

    if heuristic and top_speaker:
        return {"record_type": heuristic, "person_name": top_speaker,
                "reason": "евристика за тривалістю/типом кімнати; імʼя — найактивніший спікер"}

    # Неоднозначний тип або немає імен спікерів (текст Whisper) — питаємо Claude.
    if llm.is_configured():
        speaker_lines = "\n".join(
            f"- {s['speaker']}: {s['chars']} символів, {s['turns']} реплік" for s in speakers[:10]
        ) or "(імена спікерів недоступні — визнач імʼя ведучого з тексту, якщо він представився)"
        hint = f"\nЙмовірний тип за тривалістю: {heuristic}" if heuristic else ""
        user = (
            f"Тема: {topic}\nТривалість: {duration} хв\n"
            f"Тип кімнати: {'Breakout Room (індивідуальна)' if is_breakout else 'Головна кімната'}{hint}\n\n"
            f"Спікери (скільки говорили):\n{speaker_lines}\n\n"
            f"Початок транскрипції:\n<transcript>\n{(transcript or '')[:6000]}\n</transcript>"
        )
        try:
            # Запас max_tokens: новіші моделі за замовчуванням «думають», і це входить у ліміт.
            result = llm.call_json(DETECT_SYSTEM, user, DETECT_SCHEMA, max_tokens=4096)
            name = (result.get("person_name") or "").strip()
            if not name or name.lower() in IGNORED_SPEAKERS:
                name = top_speaker or fallback_name
            record_type = heuristic or result.get("record_type")
            if record_type not in ("lesson", "sales"):
                record_type = guess_type(duration, is_breakout)
            return {"record_type": record_type, "person_name": name[:120],
                    "reason": (result.get("reason") or "")[:300]}
        except Exception as e:  # визначення не повинне ламати обробку
            log.warning("Визначення типу через Claude не вдалося: %s", e)

    return {"record_type": guess_type(duration, is_breakout),
            "person_name": top_speaker or fallback_name,
            "reason": "запасна евристика"}
