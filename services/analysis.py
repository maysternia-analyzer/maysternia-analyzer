"""
AI-аналіз транскрипції за налаштовуваним чек-листом.

Промпт і JSON-схема будуються з поточного чек-листа (services/checklists.py) та опису
компанії (налаштування). Результат — «картка дзвінка»: оцінка за критеріями з цитатами
й таймкодами, резюме, ключові моменти, фрази для коучингу; для продажів — шанс угоди,
дані клієнта, заперечення, ризики, наступний крок.
"""
from services import checklists, settings
from services.checklists import weighted_score
from services.llm import call_json

# Claude Sonnet має контекст 200k+ токенів; ~2 символи кирилиці ≈ 1 токен.
# Реальні дані: 2-годинне заняття ≈ 47 тис. символів, 8.5-годинна зустріч ≈ 87 тис.
# (безперервна мова — до ~50 тис./год), тож ріжемо лише екстремальні випадки (~6 год мови).
MAX_TRANSCRIPT_CHARS = 300_000

LEVELS = ["Високий", "Середній", "Низький"]
TEMPERATURES = ["Гарячий", "Теплий", "Холодний"]
OBJECTION_CATEGORIES = ["ціна", "немає часу", "треба подумати", "порадитись з кимось",
                        "сумнів у результаті", "не зараз / пізніше", "інше"]
RISK_CATEGORIES = ["немає наступного кроку", "не озвучено ціну", "не виявлено потребу",
                   "сумнів у результаті", "фінансові обмеження", "немає терміновості",
                   "клієнт не зацікавлений", "інше"]

# Сумісність зі старим кодом/тестами: стандартні критерії як (ключ, назва).
LESSON_CRITERIA = [(c["key"], c["title"]) for c in checklists.DEFAULTS["lesson"]["criteria"]]
SALES_CRITERIA = [(c["key"], c["title"]) for c in checklists.DEFAULTS["sales"]["criteria"]]


# ── JSON-схеми ────────────────────────────────────────────────────────────────

def _object(properties: dict) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_STR_LIST = {"type": "array", "items": _STR}
_CRITERION = _object({"result": _BOOL, "comment": _STR, "quote": _STR, "time": _STR})
_MOMENTS = {"type": "array", "items": _object({"time": _STR, "quote": _STR, "comment": _STR, "positive": _BOOL})}
_COACHING = {"type": "array", "items": _object({"situation": _STR, "phrase": _STR})}


def build_schema(kind: str, checklist: dict) -> dict:
    common = {
        "summary": _STR,
        "criteria": _object({c["key"]: _CRITERION for c in checklist["criteria"]}),
        "key_moments": _MOMENTS,
        "coaching_phrases": _COACHING,
    }
    if kind == "lesson":
        return _object({
            **common,
            "overall_score": {"type": "integer"},
            "engagement_level": {"type": "string", "enum": LEVELS},
            "strengths": _STR,
            "improvements": _STR,
        })
    return _object({
        **common,
        "deal_chance": {"type": "string", "enum": LEVELS},
        "deal_chance_percent": {"type": "integer"},
        "lead_temperature": {"type": "string", "enum": TEMPERATURES},
        "client": _object({"name": _STR, "goal": _STR, "pains": _STR_LIST, "questions": _STR_LIST,
                           "budget": _STR}),
        "objections": {"type": "array", "items": _object({
            "text": _STR, "category": {"type": "string", "enum": OBJECTION_CATEGORIES},
            "handled": _BOOL, "manager_response": _STR, "better_response": _STR})},
        "risks": {"type": "array", "items": _object({
            "category": {"type": "string", "enum": RISK_CATEGORIES}, "description": _STR})},
        "next_step": _object({"agreed": _BOOL, "description": _STR, "deadline": _STR}),
        "top_mistakes": _STR_LIST,
        "recommendations": _STR,
        "next_contact_script": _STR,
    })


# ── Промпти ───────────────────────────────────────────────────────────────────

_TRANSCRIPT_NOTE = (
    "Транскрипція Zoom має формат «[ГГ:ХХ:СС] Спікер: репліка» («Audio shared by …» — звук, яким "
    "поділились з компʼютера). Розпізнаний з аудіо текст може бути без імен спікерів — тоді "
    "визначай ролі за змістом. Читай транскрипцію ПОВНІСТЮ від початку до кінця."
)

_CRITERIA_NOTE = (
    "Для кожного критерію в полі criteria: result — виконано чи ні; comment — конкретне пояснення "
    "з прикладом або чому відсутній; quote — дослівна цитата з транскрипції (до 200 символів) або "
    "порожній рядок; time — таймкод ГГ:ХХ:СС з транскрипції, де це відбувається, або порожній рядок."
)

_COMMON_FIELDS = (
    "summary — 1–2 речення загального висновку.\n"
    "key_moments — 3–8 найважливіших моментів розмови: time (таймкод ГГ:ХХ:СС або \"\"), quote "
    "(дослівна цитата до 200 символів), comment (чому це важливо), positive (добре це чи погано).\n"
)


def _criteria_block(checklist: dict) -> str:
    lines = [f"- {c['key']}: {c['title']} — {c['description']}" for c in checklist["criteria"]]
    block = "Критерії чек-листа:\n" + "\n".join(lines) + "\n" + _CRITERIA_NOTE
    if checklist.get("instructions"):
        block += f"\n\nДодаткові інструкції керівника:\n{checklist['instructions']}"
    return block


def build_system(kind: str, checklist: dict, company_context: str) -> str:
    context = f"Контекст компанії:\n{company_context}\n\n"
    if kind == "lesson":
        return (
            "Ти — експерт з аналізу якості пробних занять онлайн-школи.\n\n" + context
            + f"Заняття веде {checklist.get('role', 'тренер')}, учасники — потенційні студенти.\n"
            + _TRANSCRIPT_NOTE + "\n\n" + _criteria_block(checklist) + "\n\n"
            + "Інші поля:\n" + _COMMON_FIELDS
            + "overall_score — ціле 0–100: 90–100 всі пункти виконані відмінно; 75–89 більшість "
              "пунктів виконані добре; 50–74 є суттєві недоліки; нижче 50 критичні проблеми.\n"
            + "engagement_level — залученість учасників: Високий / Середній / Низький.\n"
            + "strengths — 2–3 конкретні сильні сторони тренера з прикладами.\n"
            + "improvements — 2–3 конкретні рекомендації, що покращити.\n"
            + "coaching_phrases — 2–5 порад: situation (ситуація на занятті) і phrase (що тренеру варто "
              "сказати чи зробити — готова фраза).\n"
            + "Пиши українською."
        )
    return (
        "Ти — експерт з аналізу дзвінків продажів.\n\n" + context
        + f"Розмову веде {checklist.get('role', 'менеджер продажів')} з потенційним клієнтом.\n"
        + _TRANSCRIPT_NOTE + " Оцінюй весь дзвінок, не тільки початок.\n\n"
        + _criteria_block(checklist) + "\n\n"
        + "Інші поля:\n" + _COMMON_FIELDS
        + "deal_chance — Високий / Середній / Низький; deal_chance_percent — реалістична оцінка 0–100.\n"
        + "lead_temperature — Гарячий (готовий купити), Теплий (зацікавлений, але є сумніви), "
          "Холодний (не зацікавлений).\n"
        + "client — що відомо про клієнта з розмови: name (імʼя або \"\"), goal (мета навчання), pains "
          "(болі), questions (запитання клієнта), budget (що сказав про гроші/ціну або \"\").\n"
        + "objections — кожне заперечення клієнта: text (як сказав клієнт), category, handled (чи "
          "менеджер його відпрацював), manager_response (що відповів менеджер), better_response (як "
          "краще відповісти — готова фраза).\n"
        + "risks — ризики, через які угода може не відбутися: category і description.\n"
        + "next_step — agreed (чи домовились про конкретний наступний крок), description (що саме), "
          "deadline (коли — як сказано в розмові, або \"\").\n"
        + "top_mistakes — до 3 помилок менеджера, кожна з прикладом.\n"
        + "coaching_phrases — 2–5 порад: situation (момент розмови) і phrase (що менеджеру варто "
          "було сказати — готова фраза).\n"
        + "recommendations — 3–4 конкретні рекомендації на наступний дзвінок.\n"
        + "next_contact_script — готовий текст наступного повідомлення або дзвінка клієнту.\n"
        + "Пиши українською."
    )


# ── Нормалізація (захист від null/рядків/виходу за межі у відповіді) ────────────

def to_score(value, default: int | None = 0) -> int | None:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return default


def _text(value, limit: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = "\n".join(str(v) for v in value if v)
    return str(value).strip()[:limit]


def _choice(value, allowed: list[str]) -> str:
    value = _text(value)
    for option in allowed:
        if value.lower().startswith(option.lower()[:4]):
            return option
    return ""


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    if isinstance(value, list):
        return value
    return [value] if value else []


_TIMECODE_RE = __import__("re").compile(r"(?<![\d:])(\d{1,2}):([0-5]\d)(?::([0-5]\d))?(?![\d:])")


def _timecode(value) -> str:
    """Таймкод ГГ:ХХ:СС (або ХХ:СС) з відповіді AI; некоректний («1:05:3», «12:75») — порожній."""
    match = _TIMECODE_RE.search(_text(value))
    if not match:
        return ""
    if match.group(3) is None:
        return f"00:{int(match.group(1)):02d}:{match.group(2)}"
    return f"{int(match.group(1)):02d}:{match.group(2)}:{match.group(3)}"


def normalize_criterion(raw) -> dict:
    if not isinstance(raw, dict):
        return {"result": bool(raw), "comment": "", "quote": "", "time": ""}
    return {
        "result": bool(raw.get("result")),
        "comment": _text(raw.get("comment") or raw.get("details"), 1500),
        "quote": _text(raw.get("quote"), 400),
        "time": _timecode(raw.get("time")),
    }


def _moments(raw) -> list[dict]:
    return [{"time": _timecode(m.get("time")), "quote": _text(m.get("quote"), 400),
             "comment": _text(m.get("comment"), 600), "positive": bool(m.get("positive"))}
            for m in map(_dict, _list(raw)) if m.get("quote") or m.get("comment")][:10]


def _phrases(raw) -> list[dict]:
    return [{"situation": _text(p.get("situation"), 400), "phrase": _text(p.get("phrase"), 600)}
            for p in map(_dict, _list(raw)) if p.get("phrase")][:8]


def normalize(kind: str, raw: dict, checklist: dict) -> dict:
    raw = _dict(raw)
    criteria_raw = _dict(raw.get("criteria"))
    criteria = {c["key"]: normalize_criterion(criteria_raw.get(c["key"])) for c in checklist["criteria"]}
    result = {
        "_kind": kind,
        "_criteria": [{"key": c["key"], "title": c["title"], "weight": int(c.get("weight") or 1)}
                      for c in checklist["criteria"]],
        "criteria": criteria,
        "checklist_score": weighted_score(criteria, checklist["criteria"]),
        "summary": _text(raw.get("summary"), 1500),
        "key_moments": _moments(raw.get("key_moments")),
        "coaching_phrases": _phrases(raw.get("coaching_phrases")),
    }
    if kind == "lesson":
        result.update(
            overall_score=to_score(raw.get("overall_score")),
            engagement_level=_choice(raw.get("engagement_level"), LEVELS),
            strengths=_text(raw.get("strengths")),
            improvements=_text(raw.get("improvements")),
        )
        return result
    client = _dict(raw.get("client"))
    next_step = _dict(raw.get("next_step"))
    result.update(
        deal_chance=_choice(raw.get("deal_chance"), LEVELS),
        deal_chance_percent=to_score(raw.get("deal_chance_percent")),
        lead_temperature=_choice(raw.get("lead_temperature"), TEMPERATURES),
        client={"name": _text(client.get("name"), 120), "goal": _text(client.get("goal"), 600),
                "pains": [_text(p, 300) for p in _list(client.get("pains")) if p][:8],
                "questions": [_text(q, 300) for q in _list(client.get("questions")) if q][:10],
                "budget": _text(client.get("budget"), 400)},
        objections=[{"text": _text(o.get("text"), 400),
                     "category": _choice(o.get("category"), OBJECTION_CATEGORIES) or "інше",
                     "handled": bool(o.get("handled")),
                     "manager_response": _text(o.get("manager_response"), 600),
                     "better_response": _text(o.get("better_response"), 600)}
                    for o in map(_dict, _list(raw.get("objections"))) if o.get("text")][:10],
        risks=[{"category": _choice(r.get("category"), RISK_CATEGORIES) or "інше",
                "description": _text(r.get("description"), 500)}
               for r in map(_dict, _list(raw.get("risks"))) if r.get("description") or r.get("category")][:8],
        next_step={"agreed": bool(next_step.get("agreed")), "description": _text(next_step.get("description"), 500),
                   "deadline": _text(next_step.get("deadline"), 120)},
        top_mistakes=[_text(m, 600) for m in _list(raw.get("top_mistakes")) if _text(m)][:5],
        recommendations=_text(raw.get("recommendations")),
        next_contact_script=_text(raw.get("next_contact_script")),
    )
    return result


def normalize_lesson(raw: dict) -> dict:
    """Нормалізація за стандартним чек-листом (підтримує і старий плоский формат)."""
    raw = _dict(raw)
    checklist = checklists.DEFAULTS["lesson"]
    if "criteria" not in raw:
        raw = {**raw, "criteria": {c["key"]: raw.get(c["key"]) for c in checklist["criteria"]}}
    return normalize("lesson", raw, checklist)


def normalize_sales(raw: dict) -> dict:
    raw = _dict(raw)
    checklist = checklists.DEFAULTS["sales"]
    if "criteria" not in raw:
        raw = {**raw, "criteria": {c["key"]: raw.get(c["key"]) for c in checklist["criteria"]}}
    return normalize("sales", raw, checklist)


# ── Представлення для шаблонів (нові та старі записи) ─────────────────────────

def present_criteria(analysis: dict | None, kind: str) -> list[dict]:
    """Список критеріїв з результатами: з нового формату (знімок) або зі старого (плоскі ключі)."""
    analysis = _dict(analysis)
    if isinstance(analysis.get("criteria"), dict):
        snapshot = analysis.get("_criteria")
        if not isinstance(snapshot, list) or not snapshot:
            titles = checklists.default_titles(kind)
            snapshot = [{"key": k, "title": titles.get(k, k), "weight": 1} for k in analysis["criteria"]]
        return [{"key": c.get("key"), "title": c.get("title") or c.get("key"),
                 "weight": int(c.get("weight") or 1),
                 **normalize_criterion(analysis["criteria"].get(c.get("key")))}
                for c in snapshot if isinstance(c, dict) and c.get("key")]
    titles = checklists.default_titles(kind)
    return [{"key": key, "title": title, "weight": 1, **normalize_criterion(analysis.get(key))}
            for key, title in titles.items() if key in analysis]


def light_fields(analysis: dict, kind: str) -> dict:
    """Поля для колонок records (списки/статистика без розбору JSON)."""
    items = present_criteria(analysis, kind)
    return {
        "analysis_kind": kind,
        "score": main_score(analysis, kind),
        "checklist_done": sum(1 for c in items if c["result"]),
        "checklist_total": len(items),
        "summary": _text(analysis.get("summary"), 500) or None,
        "deal_chance": _choice(analysis.get("deal_chance"), LEVELS) or None,
        "deal_chance_percent": to_score(analysis.get("deal_chance_percent"), None),
        "lead_temperature": _choice(analysis.get("lead_temperature"), TEMPERATURES) or None,
        "engagement_level": _choice(analysis.get("engagement_level"), LEVELS) or None,
    }


def main_score(analysis: dict | None, kind: str) -> int | None:
    """Головна оцінка запису: для занять — загальна оцінка AI, для продажів — чек-ліст."""
    analysis = _dict(analysis)
    value = analysis.get("overall_score") if kind == "lesson" else analysis.get("checklist_score")
    if value is None and kind == "lesson":
        value = analysis.get("checklist_score")
    return to_score(value, None)


# ── Аналіз ────────────────────────────────────────────────────────────────────

def prepare_transcript(transcription: str) -> tuple[str, bool]:
    """Обрізає лише надзвичайно довгі транскрипції (початок + кінець). Повертає (текст, обрізано?)."""
    if len(transcription) <= MAX_TRANSCRIPT_CHARS:
        return transcription, False
    marker = "\n\n[... середина транскрипції скорочена через надзвичайну довжину ...]\n\n"
    half = (MAX_TRANSCRIPT_CHARS - len(marker)) // 2
    return transcription[:half] + marker + transcription[-half:], True


def analyze(record_type: str, transcription: str) -> dict:
    kind = record_type if record_type in checklists.KINDS else "sales"
    checklist = checklists.get_checklist(kind)
    text, truncated = prepare_transcript(transcription)
    system = build_system(kind, checklist, settings.get("company_context"))
    user = f"Транскрипція:\n<transcript>\n{text}\n</transcript>"
    result = normalize(kind, call_json(system, user, build_schema(kind, checklist), max_tokens=32000), checklist)
    if truncated:
        result["_truncated"] = True
    return result
