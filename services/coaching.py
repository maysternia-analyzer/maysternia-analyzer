"""AI-план розвитку для конкретного тренера/менеджера на основі його останніх аналізів."""
import json

import database as db
from services import settings
from services.analysis import main_score, present_criteria
from services.llm import call_json
from services.team import criteria_rates

MAX_RECORDS = 25
CACHE_PREFIX = "coach:"

_STR = {"type": "string"}


def _obj(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


SCHEMA = _obj({
    "summary": _STR,
    "strengths": {"type": "array", "items": _STR},
    "growth_areas": {"type": "array", "items": _obj({"skill": _STR, "evidence": _STR, "exercise": _STR})},
    "focus_next_week": _STR,
    "phrases": {"type": "array", "items": _STR},
})


def cache_key(name: str) -> str:
    return CACHE_PREFIX + name[:200]


def get_cached(name: str) -> dict | None:
    raw = db.get_setting(cache_key(name))
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def generate(name: str, records: list[dict]) -> dict:
    analyzed = [r for r in records if r.get("analysis")][:MAX_RECORDS]
    if not analyzed:
        raise ValueError("Немає проаналізованих записів для цієї людини")
    calls = []
    for r in analyzed:
        a = r["analysis"]
        calls.append({
            "date": r.get("record_date"), "type": r["record_type"],
            "score": main_score(a, r["record_type"]),
            "failed": [c["title"] for c in present_criteria(a, r["record_type"]) if not c["result"]],
            "summary": (a.get("summary") or "")[:300],
            "mistakes": [str(m)[:200] for m in (a.get("top_mistakes") or [])[:3]],
        })
    rates = criteria_rates(analyzed, "sales") + criteria_rates(analyzed, "lesson")
    system = (
        "Ти — коуч з продажів і публічних виступів. Контекст компанії:\n"
        + settings.get("company_context")
        + "\n\nНа основі результатів AI-аналізу останніх розмов людини склади персональний план розвитку: "
          "summary (2–3 речення про рівень і динаміку), strengths (2–4 сильні сторони), growth_areas "
          "(2–4 зони росту: skill, evidence — з чого це видно, exercise — конкретна вправа), "
          "focus_next_week (один головний фокус на тиждень), phrases (3–6 готових фраз для роботи). "
          "Спирайся лише на дані. Пиши українською, звертайся до людини на «ти»."
    )
    user = json.dumps({"name": name, "criteria_pass_rates": [{"criterion": r["title"], "rate": r["rate"]}
                                                             for r in rates], "calls": calls},
                      ensure_ascii=False)
    plan = call_json(system, f"<data>\n{user}\n</data>", SCHEMA, max_tokens=8000)
    plan["generated_at"] = db.utcnow_iso()
    plan["based_on"] = len(analyzed)
    db.set_setting(cache_key(name), json.dumps(plan, ensure_ascii=False))
    return plan
