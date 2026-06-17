import os
import json
import re
import anthropic

DETECT_PROMPT = """Ти аналізуєш запис онлайн-школи "Майстерня скілів" (розвиток харизми).

Контекст:
- "Пробне заняття" = тренер проводить групове заняття з вправами з харизми/впевненості
- "Продаж" = менеджер один на один з клієнтом, обговорює курс "Код Харизми" за 15 000–30 000 грн

Дані зустрічі з Zoom:
Тема: {topic}
Тривалість: {duration} хв
Тип кімнати: {room_type}

Початок транскрипції (перші 2 хвилини):
{transcript_preview}

Відповідай ТІЛЬКИ валідним JSON:
{{
  "record_type": "lesson" або "sales",
  "person_name": "Ім'я тренера або менеджера (хто веде)",
  "confidence": 0-100,
  "reason": "коротке пояснення (1 речення)"
}}"""


def detect_type_and_name(topic: str, duration: int, is_breakout: bool,
                          host_name: str, transcript_preview: str) -> dict:
    room_type = "Breakout Room (індивідуальна кімната)" if is_breakout else "Головна кімната (групова)"

    # Fast heuristic — breakout room is almost always sales
    if is_breakout and duration <= 60:
        return {
            "record_type": "sales",
            "person_name": host_name,
            "confidence": 90,
            "reason": "Breakout room короткої тривалості — індивідуальний продаж",
        }

    # Main room long duration — almost always lesson
    if not is_breakout and duration >= 60:
        return {
            "record_type": "lesson",
            "person_name": host_name,
            "confidence": 90,
            "reason": "Головна кімната тривалого заняття",
        }

    # Ambiguous — ask Claude
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    preview = transcript_preview[:2000] if transcript_preview else "транскрипція недоступна"
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{"role": "user", "content": DETECT_PROMPT.format(
            topic=topic, duration=duration,
            room_type=room_type, transcript_preview=preview,
        )}],
    )
    raw = message.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"```$", "", raw).strip()
    result = json.loads(raw)
    # Fallback name to host if AI returns empty
    if not result.get("person_name"):
        result["person_name"] = host_name
    return result
