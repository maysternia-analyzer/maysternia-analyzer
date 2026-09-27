"""
Чек-листи (скрипти) оцінки: критерії, їх описи для AI та ваги.

Адміністратор редагує їх на сторінці «Чек-листи»; зберігаються в app_settings
(ключ "checklist:<kind>"). Кожен AI-аналіз зберігає знімок критеріїв, тож старі
записи відображаються коректно навіть після зміни чек-листа.
"""
import json
import re
import secrets

import database as db

KINDS = ("lesson", "sales")
MAX_CRITERIA = 20
# Ключі, що зайняті іншими полями аналізу — не можна використовувати для критеріїв.
RESERVED_KEYS = {
    "summary", "criteria", "overall_score", "checklist_score", "engagement_level", "strengths",
    "improvements", "deal_chance", "deal_chance_percent", "lead_temperature", "client", "objections",
    "risks", "next_step", "key_moments", "top_mistakes", "coaching_phrases", "recommendations",
    "next_contact_script", "result", "comment",
}

DEFAULTS = {
    "lesson": {
        "title": "Пробне заняття",
        "role": "тренер",
        "instructions": "Якщо елемент є в будь-якій частині заняття — він вважається виконаним.",
        "criteria": [
            {"key": "greeting", "title": "Привітав та представився", "weight": 1,
             "description": "тренер привітався, представився, познайомився з учасниками"},
            {"key": "safe_atmosphere", "title": "Створив безпечну атмосферу", "weight": 1,
             "description": "створив психологічно безпечну, дружню атмосферу (жарти, підтримка, компліменти)"},
            {"key": "structure_explained", "title": "Пояснив структуру заняття", "weight": 1,
             "description": "пояснив план/структуру заняття на початку"},
            {"key": "practical_exercises", "title": "Практичні вправи з харизми", "weight": 1,
             "description": "проводив практичні вправи з харизми (не просто теорія, а виконання вправ учасниками)"},
            {"key": "feedback_received", "title": "Отримав зворотній звʼязок", "weight": 1,
             "description": "отримував зворотний звʼязок від учасників під час або після вправ"},
            {"key": "transition_to_manager", "title": "Логічний перехід до менеджера", "weight": 1,
             "description": "наприкінці заняття логічно перейшов до менеджера / запропонував продовжити "
                            "навчання / передав слово для обговорення курсу"},
        ],
    },
    "sales": {
        "title": "Продаж менеджера",
        "role": "менеджер продажів",
        "instructions": "",
        "criteria": [
            {"key": "need_identified", "title": "Виявив потребу клієнта", "weight": 1,
             "description": "менеджер зʼясував потреби, болі, цілі клієнта"},
            {"key": "presentation_done", "title": "Зробив презентацію курсу", "weight": 1,
             "description": "презентував курс з вигодами для конкретного клієнта"},
            {"key": "objections_handled", "title": "Опрацював заперечення", "weight": 1,
             "description": "відпрацював заперечення (ціна, час, сумніви)"},
            {"key": "urgency_used", "title": "Використав urgency / терміновість", "weight": 1,
             "description": "використав дедлайн, обмежену кількість місць або інший тригер терміновості"},
            {"key": "next_step_offered", "title": "Запропонував наступний крок", "weight": 1,
             "description": "запропонував конкретний наступний крок (оплата, зустріч, дзвінок)"},
        ],
    },
}


class ChecklistError(ValueError):
    pass


def _setting_key(kind: str) -> str:
    return f"checklist:{kind}"


def get_checklist(kind: str) -> dict:
    """Поточний чек-лист (збережений адміністратором або стандартний)."""
    kind = kind if kind in KINDS else "sales"
    raw = db.get_setting(_setting_key(kind))
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("criteria"):
                return data
        except ValueError:
            pass
    return json.loads(json.dumps(DEFAULTS[kind]))  # глибока копія


def default_titles(kind: str) -> dict:
    return {c["key"]: c["title"] for c in DEFAULTS.get(kind, DEFAULTS["sales"])["criteria"]}


def validate(kind: str, data: dict) -> dict:
    if kind not in KINDS:
        raise ChecklistError("Невідомий тип чек-листа")
    criteria_in = data.get("criteria") or []
    if not isinstance(criteria_in, list) or not criteria_in:
        raise ChecklistError("Додайте хоча б один критерій")
    if len(criteria_in) > MAX_CRITERIA:
        raise ChecklistError(f"Максимум {MAX_CRITERIA} критеріїв")
    seen, criteria = set(), []
    for item in criteria_in:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        description = str(item.get("description") or "").strip()
        if not title:
            continue  # порожній рядок у формі — пропускаємо
        if len(title) > 120:
            raise ChecklistError(f"Назва критерію задовга: «{title[:40]}…»")
        if len(description) > 600:
            raise ChecklistError(f"Опис критерію «{title[:40]}» задовгий (максимум 600 символів)")
        try:
            weight = int(item.get("weight") or 1)
        except (TypeError, ValueError):
            raise ChecklistError(f"Вага критерію «{title[:40]}» має бути числом")
        if not 1 <= weight <= 10:
            raise ChecklistError(f"Вага критерію «{title[:40]}» — від 1 до 10")
        key = str(item.get("key") or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,39}", key) or key in RESERVED_KEYS or key in seen:
            key = "c_" + secrets.token_hex(3)
        seen.add(key)
        if any(c["title"].lower() == title.lower() for c in criteria):
            raise ChecklistError(f"Критерій «{title[:40]}» повторюється — назви мають бути унікальними")
        criteria.append({"key": key, "title": title, "description": description or title, "weight": weight})
    if not criteria:
        raise ChecklistError("Додайте хоча б один критерій з назвою")
    instructions = str(data.get("instructions") or "").strip()
    if len(instructions) > 3000:
        raise ChecklistError("Додаткові інструкції задовгі (максимум 3000 символів)")
    return {
        "title": DEFAULTS[kind]["title"],
        "role": DEFAULTS[kind]["role"],
        "instructions": instructions,
        "criteria": criteria,
        "updated_at": db.utcnow_iso(),
    }


def save_checklist(kind: str, data: dict) -> dict:
    checklist = validate(kind, data)
    db.set_setting(_setting_key(kind), json.dumps(checklist, ensure_ascii=False))
    return checklist


def reset_checklist(kind: str) -> dict:
    db.execute("DELETE FROM app_settings WHERE key = ?", (_setting_key(kind),))
    return get_checklist(kind)


def weighted_score(criteria_results: dict, criteria: list[dict]) -> int:
    """Частка виконаних критеріїв з урахуванням ваг, 0–100."""
    total = sum(max(1, int(c.get("weight") or 1)) for c in criteria)
    if not total:
        return 0
    passed = sum(max(1, int(c.get("weight") or 1)) for c in criteria
                 if (criteria_results.get(c["key"]) or {}).get("result"))
    return round(passed / total * 100)
