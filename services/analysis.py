"""AI-аналіз транскрипції за чек-лістом: пробне заняття (тренер) або продаж (менеджер)."""
from services.llm import call_json

# Claude Sonnet має контекст 200k+ токенів; ~2 символи кирилиці ≈ 1 токен.
# Реальні дані: 2-годинне заняття ≈ 47 тис. символів, 8.5-годинна зустріч ≈ 87 тис.
# (безперервна мова — до ~50 тис./год), тож ріжемо лише екстремальні випадки (~6 год мови).
MAX_TRANSCRIPT_CHARS = 300_000

LEVELS = ["Високий", "Середній", "Низький"]
TEMPERATURES = ["Гарячий", "Теплий", "Холодний"]

LESSON_CRITERIA = [
    ("greeting", "Привітав та представився"),
    ("safe_atmosphere", "Створив безпечну атмосферу"),
    ("structure_explained", "Пояснив структуру заняття"),
    ("practical_exercises", "Практичні вправи з харизми"),
    ("feedback_received", "Отримав зворотній звʼязок"),
    ("transition_to_manager", "Логічний перехід до менеджера"),
]
SALES_CRITERIA = [
    ("need_identified", "Виявив потребу клієнта"),
    ("presentation_done", "Зробив презентацію курсу"),
    ("objections_handled", "Опрацював заперечення"),
    ("urgency_used", "Використав urgency / терміновість"),
    ("next_step_offered", "Запропонував наступний крок"),
]

LESSON_SYSTEM = """Ти — експерт з аналізу якості пробних занять онлайн-школи "Майстерня скілів".
Школа навчає харизмі та публічним виступам. Курс називається "Код Харизми".
Пробне заняття веде викладач-тренер, учасники — потенційні студенти.

Транскрипція Zoom має формат «Спікер: репліка» («Audio shared by …» — звук, яким поділились з компʼютера);
розпізнаний з аудіо текст може бути без імен спікерів — тоді визначай ролі за змістом.
Читай транскрипцію ПОВНІСТЮ від початку до кінця. Не роби висновків на основі часткового аналізу.
Якщо елемент є в будь-якій частині заняття — він вважається виконаним.

Критерії оцінки (для кожного: result — виконано чи ні, comment — конкретний приклад з транскрипції або пояснення, чому відсутній):
- greeting: тренер привітався, представився, познайомився з учасниками
- safe_atmosphere: створив психологічно безпечну, дружню атмосферу (жарти, підтримка, компліменти)
- structure_explained: пояснив план/структуру заняття на початку
- practical_exercises: проводив практичні вправи з харизми (не просто теорія, а виконання вправ учасниками)
- feedback_received: отримував зворотній зв'язок від учасників під час або після вправ
- transition_to_manager: наприкінці заняття логічно перейшов до менеджера / запропонував продовжити навчання / передав слово для обговорення курсу

overall_score (ціле 0-100):
- 90-100: всі пункти виконані відмінно
- 75-89: більшість пунктів виконані добре
- 50-74: є суттєві недоліки
- нижче 50: критичні проблеми

engagement_level — залученість учасників: Високий / Середній / Низький.
strengths — 2-3 конкретні сильні сторони тренера з прикладами.
improvements — 2-3 конкретні рекомендації, що покращити.
summary — 1-2 речення загального висновку про заняття.
Пиши українською."""

SALES_SYSTEM = """Ти — експерт з аналізу дзвінків продажів онлайн-школи "Майстерня скілів".
Школа продає курс "Код Харизми" — навчання харизмі та публічним виступам. Вартість: 15 000–30 000 грн.
Менеджер продажів спілкується з потенційним клієнтом після пробного заняття.

Транскрипція Zoom має формат «Спікер: репліка»; розпізнаний з аудіо текст може бути без імен —
тоді визначай, хто менеджер, а хто клієнт, за змістом.
Читай транскрипцію ПОВНІСТЮ від початку до кінця. Оцінюй весь дзвінок, не тільки початок.

Критерії оцінки (result — виконано чи ні):
- need_identified: менеджер з'ясував потреби, болі, цілі клієнта (details — конкретні потреби клієнта, які виявив менеджер)
- presentation_done: презентував курс з вигодами для конкретного клієнта (comment — що саме презентував і як пов'язав з потребами)
- objections_handled: відпрацював заперечення — ціна, час, сумніви (comment — які заперечення були і як відпрацював)
- urgency_used: використав дедлайн, обмежену кількість місць або інший тригер терміновості (comment — який тригер або чому не використав)
- next_step_offered: запропонував конкретний наступний крок — оплата, зустріч, дзвінок (comment — який саме)

deal_chance — Високий / Середній / Низький; deal_chance_percent — реалістична оцінка 0-100 на основі всього дзвінка.
lead_temperature: Гарячий (готовий купити), Теплий (зацікавлений, але є сумніви), Холодний (не зацікавлений).
checklist_score — ціле 0-100, наскільки менеджер виконав чек-ліст.
next_contact_script — готовий скрипт наступного повідомлення або дзвінка менеджеру.
top_mistakes — до 3 помилок, кожна з прикладом з транскрипції.
recommendations — 3-4 конкретні рекомендації, що змінити в наступному дзвінку.
Пиши українською."""


def _criterion_schema(text_field: str = "comment") -> dict:
    return {
        "type": "object",
        "properties": {"result": {"type": "boolean"}, text_field: {"type": "string"}},
        "required": ["result", text_field],
        "additionalProperties": False,
    }


def _object(properties: dict) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


LESSON_SCHEMA = _object({
    **{key: _criterion_schema() for key, _ in LESSON_CRITERIA},
    "overall_score": {"type": "integer"},
    "engagement_level": {"type": "string", "enum": LEVELS},
    "strengths": {"type": "string"},
    "improvements": {"type": "string"},
    "summary": {"type": "string"},
})

SALES_SCHEMA = _object({
    "need_identified": _criterion_schema("details"),
    **{key: _criterion_schema() for key, _ in SALES_CRITERIA[1:]},
    "deal_chance": {"type": "string", "enum": LEVELS},
    "deal_chance_percent": {"type": "integer"},
    "lead_temperature": {"type": "string", "enum": TEMPERATURES},
    "checklist_score": {"type": "integer"},
    "next_contact_script": {"type": "string"},
    "top_mistakes": {"type": "array", "items": {"type": "string"}},
    "recommendations": {"type": "string"},
})


# ── Нормалізація (захист від null/рядків/виходу за межі у відповіді) ────────────

def to_score(value, default: int = 0) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return default


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(str(v) for v in value if v)
    return str(value).strip()


def _choice(value, allowed: list[str]) -> str:
    value = _text(value)
    for option in allowed:
        if value.lower().startswith(option.lower()[:4]):
            return option
    return ""


def _criterion(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    comment = _text(raw.get("comment") or raw.get("details"))
    return {"result": bool(raw.get("result")), "comment": comment}


def normalize_lesson(raw: dict) -> dict:
    result = {key: _criterion(raw.get(key)) for key, _ in LESSON_CRITERIA}
    result.update(
        overall_score=to_score(raw.get("overall_score")),
        engagement_level=_choice(raw.get("engagement_level"), LEVELS),
        strengths=_text(raw.get("strengths")),
        improvements=_text(raw.get("improvements")),
        summary=_text(raw.get("summary")),
        _kind="lesson",
    )
    return result


def normalize_sales(raw: dict) -> dict:
    result = {key: _criterion(raw.get(key)) for key, _ in SALES_CRITERIA}
    result["need_identified"]["details"] = result["need_identified"]["comment"]
    mistakes = raw.get("top_mistakes")
    if isinstance(mistakes, str):
        mistakes = [mistakes]
    result.update(
        deal_chance=_choice(raw.get("deal_chance"), LEVELS),
        deal_chance_percent=to_score(raw.get("deal_chance_percent")),
        lead_temperature=_choice(raw.get("lead_temperature"), TEMPERATURES),
        checklist_score=to_score(raw.get("checklist_score")),
        next_contact_script=_text(raw.get("next_contact_script")),
        top_mistakes=[_text(m) for m in (mistakes or []) if _text(m)][:5],
        recommendations=_text(raw.get("recommendations")),
        _kind="sales",
    )
    return result


def prepare_transcript(transcription: str) -> tuple[str, bool]:
    """Обрізає лише надзвичайно довгі транскрипції (початок + кінець). Повертає (текст, обрізано?)."""
    if len(transcription) <= MAX_TRANSCRIPT_CHARS:
        return transcription, False
    marker = "\n\n[... середина транскрипції скорочена через надзвичайну довжину ...]\n\n"
    half = (MAX_TRANSCRIPT_CHARS - len(marker)) // 2
    return transcription[:half] + marker + transcription[-half:], True


def analyze(record_type: str, transcription: str) -> dict:
    text, truncated = prepare_transcript(transcription)
    user = f"Транскрипція:\n<transcript>\n{text}\n</transcript>"
    if record_type == "lesson":
        result = normalize_lesson(call_json(LESSON_SYSTEM, user, LESSON_SCHEMA))
    else:
        result = normalize_sales(call_json(SALES_SYSTEM, user, SALES_SCHEMA))
    if truncated:
        result["_truncated"] = True
    return result
