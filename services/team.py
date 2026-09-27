"""
Командна аналітика (детерміновано, без AI): рейтинг людей, динаміка, «карта навичок»
(частка виконання кожного критерію), заперечення та ризики за категоріями.
"""
from collections import Counter, defaultdict
from datetime import date, timedelta

from services.analysis import main_score, present_criteria, to_score


def _mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values)) if values else None


def previous_period(date_from: str, date_to: str) -> tuple[str, str] | None:
    """Попередній період такої ж довжини (для тренду)."""
    try:
        start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    except (TypeError, ValueError):
        return None
    length = (end - start).days + 1
    if length <= 0:
        return None
    prev_end = start - timedelta(days=1)
    return (prev_end - timedelta(days=length - 1)).isoformat(), prev_end.isoformat()


def _analyzed(records):
    # Не лише status=done: невдалий переаналіз (error/черга) не повинен прибирати попередній аналіз.
    return [r for r in records if r.get("analysis")]


def criteria_rates(records, kind: str) -> list[dict]:
    """Частка виконання кожного критерію (слабкі місця — першими)."""
    totals: dict[str, list] = {}
    titles: dict[str, str] = {}
    for r in _analyzed(records):
        if r["record_type"] != kind:
            continue
        for c in present_criteria(r["analysis"], kind):
            entry = totals.setdefault(c["key"], [0, 0])
            entry[0] += 1 if c["result"] else 0
            entry[1] += 1
            titles.setdefault(c["key"], c["title"])  # записи від нових до старих — лишаємо найсвіжішу назву
    rates = [{"key": k, "title": titles[k], "passed": p, "total": t, "rate": round(p / t * 100)}
             for k, (p, t) in totals.items() if t]
    return sorted(rates, key=lambda x: x["rate"])


def objection_stats(records) -> list[dict]:
    counts, handled = Counter(), Counter()
    examples: dict[str, str] = {}
    for r in _analyzed(records):
        for o in r["analysis"].get("objections") or []:
            if not isinstance(o, dict):
                continue
            category = o.get("category") or "інше"
            counts[category] += 1
            handled[category] += 1 if o.get("handled") else 0
            examples.setdefault(category, o.get("text") or "")
    return [{"category": c, "count": n, "handled_rate": round(handled[c] / n * 100), "example": examples[c]}
            for c, n in counts.most_common()]


def risk_stats(records) -> list[dict]:
    counts = Counter()
    for r in _analyzed(records):
        # одна категорія ризику рахується один раз на запис
        categories = {risk.get("category") or "інше" for risk in r["analysis"].get("risks") or []
                      if isinstance(risk, dict)}
        counts.update(categories)
    return [{"category": c, "count": n} for c, n in counts.most_common()]


def people_rating(records, previous_records=None) -> list[dict]:
    """Рейтинг людей: кількість, середні оцінки, продажі, тренд до попереднього періоду."""
    def collect(rows):
        people = defaultdict(lambda: {"sales": [], "lessons": [], "deal": [], "sold": 0, "revenue": 0.0,
                                      "with_result": 0})
        for r in rows:
            name = r.get("person_name") or "Невідомо"
            person = people[name]
            if r["record_type"] == "sales":
                if r.get("sale_made") in (0, 1):
                    person["with_result"] += 1
                if r.get("sale_made") == 1:
                    person["sold"] += 1
                    person["revenue"] += float(r.get("sale_amount") or 0)
            if not r.get("analysis"):
                continue
            score = main_score(r["analysis"], r["record_type"])
            if r["record_type"] == "sales":
                person["sales"].append(score)
                person["deal"].append(to_score(r["analysis"].get("deal_chance_percent"), None))
            else:
                person["lessons"].append(score)
        return people

    current = collect(records)
    previous = collect(previous_records or [])
    rating = []
    for name, p in current.items():
        scores = p["sales"] + p["lessons"]
        if not scores and not p["with_result"]:
            continue  # лише записи з помилками — у рейтингу нічого показати
        avg = _mean(scores)
        prev = previous.get(name)
        prev_avg = _mean((prev["sales"] + prev["lessons"])) if prev else None
        rating.append({
            "name": name,
            "calls": len(scores),
            "sales_count": len(p["sales"]),
            "lessons_count": len(p["lessons"]),
            "avg_score": avg,
            "avg_sales_score": _mean(p["sales"]),
            "avg_lesson_score": _mean(p["lessons"]),
            "avg_deal_pct": _mean(p["deal"]),
            "sold": p["sold"],
            "with_result": p["with_result"],
            "conversion": round(p["sold"] / p["with_result"] * 100) if p["with_result"] else None,
            "revenue": p["revenue"],
            "trend": (avg - prev_avg) if avg is not None and prev_avg is not None else None,
        })
    return sorted(rating, key=lambda x: (x["avg_score"] is None, -(x["avg_score"] or 0), -x["calls"]))


def score_series(records) -> list[dict]:
    """Динаміка оцінок у часі (від старих до нових) для графіка."""
    series = []
    for r in _analyzed(records):
        score = main_score(r["analysis"], r["record_type"])
        if score is not None:
            series.append({"date": r.get("record_date") or "", "score": score, "id": r["id"],
                           "type": r["record_type"]})
    return sorted(series, key=lambda x: (x["date"], x["id"]))


def ai_calibration(records) -> list[dict]:
    """Наскільки прогноз AI («шанс угоди») збігається з реальним результатом продажу."""
    groups = {level: [0, 0] for level in ("Високий", "Середній", "Низький")}
    for r in _analyzed(records):
        if r["record_type"] != "sales" or r.get("sale_made") not in (0, 1):
            continue
        level = r["analysis"].get("deal_chance")
        if level in groups:
            groups[level][1] += 1
            groups[level][0] += 1 if r["sale_made"] == 1 else 0
    return [{"level": level, "sold": sold, "total": total, "rate": round(sold / total * 100)}
            for level, (sold, total) in groups.items() if total]


def drill_down(records, kind: str, criterion: str = "", objection: str = "", risk: str = "") -> list[dict]:
    """Записи, де критерій не виконано / було заперечення чи ризик певної категорії — з цитатами."""
    items = []
    for r in _analyzed(records):
        if r["record_type"] != kind:
            continue
        a = r["analysis"]
        if criterion:
            for c in present_criteria(a, kind):
                if c["key"] == criterion and not c["result"]:
                    items.append({"record": r, "text": c["comment"], "quote": c["quote"], "time": c["time"]})
        elif objection:
            for o in a.get("objections") or []:
                if isinstance(o, dict) and (o.get("category") or "інше") == objection:
                    items.append({"record": r, "text": o.get("better_response") or "", "quote": o.get("text") or "",
                                  "time": "", "handled": o.get("handled")})
        elif risk:
            for x in a.get("risks") or []:
                if isinstance(x, dict) and (x.get("category") or "інше") == risk:
                    items.append({"record": r, "text": x.get("description") or "", "quote": "", "time": ""})
                    break
    return items


def person_profile(records) -> dict:
    """Дані для профілю людини (records — лише її записи)."""
    analyzed = _analyzed(records)
    mistakes = []
    for r in analyzed:
        for m in r["analysis"].get("top_mistakes") or []:
            if isinstance(m, str) and m.strip():
                mistakes.append({"text": m, "id": r["id"], "date": r.get("record_date")})
    phrases = []
    for r in analyzed:
        for p in r["analysis"].get("coaching_phrases") or []:
            if isinstance(p, dict) and p.get("phrase"):
                phrases.append({**p, "id": r["id"]})
    rating = people_rating(records)
    return {
        "summary": rating[0] if rating else None,
        "series": score_series(records),
        "lesson_criteria": criteria_rates(records, "lesson"),
        "sales_criteria": criteria_rates(records, "sales"),
        "objections": objection_stats(records),
        "risks": risk_stats(records),
        "mistakes": mistakes[:15],
        "phrases": phrases[:10],
    }


def sparkline_points(series: list[dict], width: int = 600, height: int = 140, pad: int = 12) -> list[dict]:
    """Координати точок SVG-графіка (оцінки 0–100)."""
    if not series:
        return []
    step = (width - 2 * pad) / max(1, len(series) - 1)
    return [{**point, "x": round(pad + i * step, 1),
             "y": round(height - pad - (height - 2 * pad) * point["score"] / 100, 1)}
            for i, point in enumerate(series)]
