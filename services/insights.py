"""
AI-аналіз всієї бази продажів та занять.
Формує портрет ЦА, топ потреб, заперечень, закономірності успішних угод.
"""
import json
import os
import anthropic


def _client():
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def generate_insights(records: list) -> dict:
    """
    Аналізує всі записи та повертає інсайти.
    records — list of dicts із полями: record_type, person_name, trainer_name,
              sale_made, sale_amount, analysis (dict), transcription (str, optional)
    """
    sales = [r for r in records if r["record_type"] == "sales" and r.get("analysis")]
    lessons = [r for r in records if r["record_type"] == "lesson" and r.get("analysis")]

    if not sales and not lessons:
        return _empty_insights()

    # Build compact summaries for Claude
    sales_summaries = []
    for r in sales:
        a = r.get("analysis", {})
        summary = {
            "date": r.get("record_date", ""),
            "manager": r.get("person_name", ""),
            "trainer": r.get("trainer_name", ""),
            "checklist_score": a.get("checklist_score"),
            "deal_chance": a.get("deal_chance"),
            "deal_chance_pct": a.get("deal_chance_percent"),
            "lead_temperature": a.get("lead_temperature"),
            "sale_made": r.get("sale_made"),
            "sale_amount": r.get("sale_amount"),
            "need_identified": a.get("need_identified", {}).get("result"),
            "presentation_done": a.get("presentation_done", {}).get("result"),
            "objections_handled": a.get("objections_handled", {}).get("result"),
            "urgency_used": a.get("urgency_used", {}).get("result"),
            "next_step_offered": a.get("next_step_offered", {}).get("result"),
            "top_mistakes": a.get("top_mistakes", []),
            "recommendations": a.get("recommendations", ""),
            "next_contact_script": a.get("next_contact_script", ""),
            "transcript_preview": (r.get("transcription") or "")[:1500],
        }
        sales_summaries.append(summary)

    lesson_summaries = []
    for r in lessons:
        a = r.get("analysis", {})
        lesson_summaries.append({
            "date": r.get("record_date", ""),
            "trainer": r.get("person_name", ""),
            "overall_score": a.get("overall_score"),
            "engagement_level": a.get("engagement_level"),
            "strengths": a.get("strengths", ""),
            "improvements": a.get("improvements", ""),
            "transcript_preview": (r.get("transcription") or "")[:1000],
        })

    prompt = f"""Ти — бізнес-аналітик онлайн школи "Майстерня скілів".

У тебе є дані про {len(sales)} продажів та {len(lessons)} пробних занять.

ДАНІ ПРОДАЖІВ:
{json.dumps(sales_summaries, ensure_ascii=False, indent=2)}

ДАНІ ЗАНЯТЬ:
{json.dumps(lesson_summaries, ensure_ascii=False, indent=2)}

Проведи глибокий аналіз та поверни JSON з такою структурою (БЕЗ markdown, тільки JSON):

{{
  "summary": "загальний висновок в 2-3 реченнях про стан продажів та занять",

  "sales_patterns": {{
    "success_factors": ["фактор 1 що є в успішних угодах", "фактор 2", ...],
    "failure_factors": ["що не спрацьовує в неуспішних", ...],
    "best_manager": "ім'я або null",
    "best_manager_reason": "чому",
    "avg_checklist_score": число,
    "conversion_rate": число від 0 до 100,
    "total_revenue": загальна сума успішних угод або null,
    "avg_deal_size": середня сума угоди або null
  }},

  "audience_portrait": {{
    "description": "опис портрету типового клієнта",
    "age_range": "орієнтовний вік",
    "main_goals": ["ціль 1", "ціль 2", ...],
    "pain_points": ["біль 1", "біль 2", ...],
    "decision_factors": ["що впливає на рішення купити", ...]
  }},

  "top_needs": [
    {{"need": "потреба", "frequency": "висока/середня/низька", "description": "деталі"}},
    ...
  ],

  "top_objections": [
    {{"objection": "заперечення", "frequency": "висока/середня/низька", "how_to_handle": "як краще відпрацювати"}},
    ...
  ],

  "lesson_insights": {{
    "avg_score": число або null,
    "best_trainer": "ім'я або null",
    "best_trainer_reason": "чому",
    "common_strengths": ["сильна сторона 1", ...],
    "common_weaknesses": ["слабка сторона 1", ...]
  }},

  "company_recommendations": [
    {{"priority": "висока/середня", "area": "сфера", "recommendation": "рекомендація", "expected_impact": "очікуваний ефект"}},
    ...
  ]
}}"""

    client = _client()
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        raw = raw.rsplit("```", 1)[0]
    return json.loads(raw)


def _empty_insights():
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
    }
