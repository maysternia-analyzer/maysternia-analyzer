"""
AI-аналіз усієї бази продажів та занять: портрет ЦА, топ потреб і заперечень,
закономірності успішних угод. Числові метрики рахуються точно в Python.
"""
import json

from services.analysis import to_score
from services.llm import call_json

MAX_SALES = 120
MAX_LESSONS = 80
SALES_PREVIEW_CHARS = 400
LESSON_PREVIEW_CHARS = 300
FIELD_CHARS = 300
MAX_PAYLOAD_CHARS = 150_000   # ≈ 75 тис. токенів — вартість і час одного запиту під контролем

_STR_LIST = {"type": "array", "items": {"type": "string"}}
_NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_NULLABLE_NUM = {"anyOf": [{"type": "number"}, {"type": "null"}]}


def _obj(properties: dict) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


INSIGHTS_SCHEMA = _obj({
    "summary": {"type": "string"},
    "sales_patterns": _obj({
        "success_factors": _STR_LIST,
        "failure_factors": _STR_LIST,
        "best_manager": _NULLABLE_STR,
        "best_manager_reason": {"type": "string"},
    }),
    "audience_portrait": _obj({
        "description": {"type": "string"},
        "age_range": {"type": "string"},
        "main_goals": _STR_LIST,
        "pain_points": _STR_LIST,
        "decision_factors": _STR_LIST,
    }),
    "top_needs": {"type": "array", "items": _obj({
        "need": {"type": "string"},
        "frequency": {"type": "string", "enum": ["висока", "середня", "низька"]},
        "description": {"type": "string"},
    })},
    "top_objections": {"type": "array", "items": _obj({
        "objection": {"type": "string"},
        "frequency": {"type": "string", "enum": ["висока", "середня", "низька"]},
        "how_to_handle": {"type": "string"},
    })},
    "lesson_insights": _obj({
        "best_trainer": _NULLABLE_STR,
        "best_trainer_reason": {"type": "string"},
        "common_strengths": _STR_LIST,
        "common_weaknesses": _STR_LIST,
    }),
    "company_recommendations": {"type": "array", "items": _obj({
        "priority": {"type": "string", "enum": ["висока", "середня"]},
        "area": {"type": "string"},
        "recommendation": {"type": "string"},
        "expected_impact": {"type": "string"},
    })},
})

SYSTEM = """Ти — бізнес-аналітик онлайн-школи "Майстерня скілів" (курс "Код Харизми", 15 000–30 000 грн).
Тобі дають стислі результати AI-аналізу дзвінків продажів і пробних занять та точні метрики.
Знайди закономірності, опиши портрет цільової аудиторії, топ потреб і заперечень клієнтів,
сильні та слабкі сторони тренерів і дай конкретні рекомендації компанії.
Спирайся лише на надані дані; не вигадуй цифр. Пиши українською."""


def _dict(value) -> dict:
    """Старі аналізи могли містити не словник там, де зараз словник."""
    return value if isinstance(value, dict) else {}


def _clip(value, limit: int = FIELD_CHARS) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values)) if values else None


def compute_metrics(sales: list[dict], lessons: list[dict]) -> dict:
    sold = [r for r in sales if r.get("sale_made") == 1]
    with_result = [r for r in sales if r.get("sale_made") in (0, 1)]
    amounts = [float(r["sale_amount"]) for r in sold if r.get("sale_amount")]
    return {
        "avg_checklist_score": _mean([to_score(_dict(r["analysis"]).get("checklist_score"), None) for r in sales]),
        "conversion_rate": round(len(sold) / len(with_result) * 100) if with_result else None,
        "total_revenue": round(sum(amounts)) if amounts else None,
        "avg_deal_size": round(sum(amounts) / len(amounts)) if amounts else None,
        "lesson_avg_score": _mean([to_score(_dict(r["analysis"]).get("overall_score"), None) for r in lessons]),
        "sales_count": len(sales),
        "lessons_count": len(lessons),
        "deals_closed": len(sold),
        "deals_with_result": len(with_result),
    }


def _sale_summary(r: dict) -> dict:
    a = _dict(r["analysis"])
    need = _dict(a.get("need_identified"))
    mistakes = a.get("top_mistakes") if isinstance(a.get("top_mistakes"), list) else []
    return {
        "date": r.get("record_date", ""),
        "manager": r.get("person_name", ""),
        "trainer": r.get("trainer_name", ""),
        "checklist_score": a.get("checklist_score"),
        "deal_chance": a.get("deal_chance"),
        "lead_temperature": a.get("lead_temperature"),
        "sale_made": {1: True, 0: False}.get(r.get("sale_made")),
        "sale_amount": r.get("sale_amount"),
        **{k: _dict(a.get(k)).get("result") for k in (
            "need_identified", "presentation_done", "objections_handled", "urgency_used", "next_step_offered")},
        "client_needs": _clip(need.get("details") or need.get("comment")),
        "objections": _clip(_dict(a.get("objections_handled")).get("comment")),
        "top_mistakes": [_clip(m, 200) for m in mistakes[:3]],
        "transcript_preview": _clip(r.get("transcription"), SALES_PREVIEW_CHARS),
    }


def _lesson_summary(r: dict) -> dict:
    a = _dict(r["analysis"])
    return {
        "date": r.get("record_date", ""),
        "trainer": r.get("person_name", ""),
        "overall_score": a.get("overall_score"),
        "engagement_level": a.get("engagement_level"),
        "strengths": _clip(a.get("strengths")),
        "improvements": _clip(a.get("improvements")),
        "transcript_preview": _clip(r.get("transcription"), LESSON_PREVIEW_CHARS),
    }


def _build_payload(metrics: dict, sales: list, lessons: list) -> tuple[str, int, int]:
    """JSON для Claude в межах MAX_PAYLOAD_CHARS: за потреби лишаємо найсвіжіші записи."""
    n_sales, n_lessons = min(len(sales), MAX_SALES), min(len(lessons), MAX_LESSONS)
    while True:
        payload = json.dumps({
            "metrics": metrics,
            "sales": [_sale_summary(r) for r in sales[:n_sales]],
            "lessons": [_lesson_summary(r) for r in lessons[:n_lessons]],
        }, ensure_ascii=False)
        if len(payload) <= MAX_PAYLOAD_CHARS or (n_sales <= 5 and n_lessons <= 5):
            return payload, n_sales, n_lessons
        n_sales, n_lessons = max(5, int(n_sales * 0.8)), max(5, int(n_lessons * 0.8))


def generate_insights(records: list) -> dict:
    """records — записи з полями analysis, sale_made, sale_amount, transcription (превʼю)."""
    sales = [r for r in records if r.get("record_type") == "sales" and r.get("analysis")]
    lessons = [r for r in records if r.get("record_type") == "lesson" and r.get("analysis")]
    if not sales and not lessons:
        return empty_insights()

    metrics = compute_metrics(sales, lessons)
    payload, n_sales, n_lessons = _build_payload(metrics, sales, lessons)
    note = ""
    if n_sales < len(sales) or n_lessons < len(lessons):
        note = (f"\nУ вибірці наведено {n_sales} найсвіжіших продажів із {len(sales)} "
                f"та {n_lessons} занять із {len(lessons)}; метрики пораховано по всіх.")
    user = f"Дані для аналізу (JSON):{note}\n<data>\n{payload}\n</data>"
    data = call_json(SYSTEM, user, INSIGHTS_SCHEMA, max_tokens=16000)

    # Точні цифри — з бази, а не від моделі.
    for key in ("sales_patterns", "lesson_insights"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    data["sales_patterns"].update(
        avg_checklist_score=metrics["avg_checklist_score"],
        conversion_rate=metrics["conversion_rate"],
        total_revenue=metrics["total_revenue"],
        avg_deal_size=metrics["avg_deal_size"],
    )
    data["lesson_insights"]["avg_score"] = metrics["lesson_avg_score"]
    data["metrics"] = metrics
    return data


def empty_insights() -> dict:
    return {
        "summary": "Недостатньо даних для аналізу. Додайте більше проаналізованих записів.",
        "sales_patterns": {"success_factors": [], "failure_factors": [], "best_manager": None,
                           "best_manager_reason": "", "avg_checklist_score": None,
                           "conversion_rate": None, "total_revenue": None, "avg_deal_size": None},
        "audience_portrait": {"description": "", "age_range": "", "main_goals": [],
                              "pain_points": [], "decision_factors": []},
        "top_needs": [],
        "top_objections": [],
        "lesson_insights": {"avg_score": None, "best_trainer": None, "best_trainer_reason": "",
                            "common_strengths": [], "common_weaknesses": []},
        "company_recommendations": [],
        "metrics": {},
    }
