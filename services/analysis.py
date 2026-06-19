import os
import json
import re
import anthropic

LESSON_PROMPT = """Ти — експерт з аналізу якості пробних занять онлайн-школи "Майстерня скілів".
Школа навчає харизмі та публічним виступам. Курс називається "Код Харизми".
Пробне заняття веде викладач-тренер, учасники — потенційні студенти.

ВАЖЛИВО: Читай транскрипцію ПОВНІСТЮ від початку до кінця. Не роби висновків на основі часткового аналізу.
Якщо елемент є в будь-якій частині заняття — він вважається виконаним.
Відповідай ТІЛЬКИ валідним JSON без markdown-блоків.

Транскрипція пробного заняття:
{transcription}

Критерії оцінки:
- greeting: тренер привітався, представився, познайомився з учасниками
- safe_atmosphere: створив психологічно безпечну, дружню атмосферу (жарти, підтримка, компліменти)
- structure_explained: пояснив план/структуру заняття на початку
- practical_exercises: проводив практичні вправи з харизми (не просто теорія, а виконання вправ учасниками)
- feedback_received: отримував зворотній зв'язок від учасників під час або після вправ
- transition_to_manager: наприкінці заняття логічно перейшов до менеджера / запропонував продовжити навчання / передав слово для обговорення курсу

Шкала overall_score (0-100):
- 90-100: всі пункти виконані відмінно
- 75-89: більшість пунктів виконані добре
- 50-74: є суттєві недоліки
- нижче 50: критичні проблеми

JSON формат (суворо):
{{
  "greeting": {{"result": true/false, "comment": "конкретний приклад з транскрипції або пояснення чому відсутній"}},
  "safe_atmosphere": {{"result": true/false, "comment": "конкретний приклад з транскрипції або пояснення чому відсутній"}},
  "structure_explained": {{"result": true/false, "comment": "конкретний приклад з транскрипції або пояснення чому відсутній"}},
  "practical_exercises": {{"result": true/false, "comment": "конкретний приклад вправи з транскрипції або пояснення чому відсутній"}},
  "feedback_received": {{"result": true/false, "comment": "конкретний приклад з транскрипції або пояснення чому відсутній"}},
  "transition_to_manager": {{"result": true/false, "comment": "конкретний приклад з транскрипції або пояснення чому відсутній"}},
  "overall_score": 75,
  "engagement_level": "Високий/Середній/Низький",
  "strengths": "2-3 конкретні сильні сторони тренера з прикладами",
  "improvements": "2-3 конкретні рекомендації що покращити",
  "summary": "1-2 речення загального висновку про заняття"
}}"""

SALES_PROMPT = """Ти — експерт з аналізу дзвінків продажів онлайн-школи "Майстерня скілів".
Школа продає курс "Код Харизми" — навчання харизмі та публічним виступам. Вартість: 15 000–30 000 грн.
Менеджер продажів спілкується з потенційним клієнтом після пробного заняття.

ВАЖЛИВО: Читай транскрипцію ПОВНІСТЮ від початку до кінця. Оцінюй весь дзвінок, не тільки початок.
Відповідай ТІЛЬКИ валідним JSON без markdown-блоків.

Транскрипція дзвінка продажів:
{transcription}

Критерії оцінки:
- need_identified: менеджер з'ясував потреби, болі, цілі клієнта
- presentation_done: презентував курс з вигодами для конкретного клієнта
- objections_handled: відпрацював заперечення (ціна, час, сумніви)
- urgency_used: використав дедлайн, обмежену кількість місць або інший тригер терміновості
- next_step_offered: запропонував конкретний наступний крок (оплата, зустріч, дзвінок)

deal_chance_percent: реалістична оцінка 0-100 на основі всього дзвінка
lead_temperature: Гарячий (готовий купити), Теплий (зацікавлений але є сумніви), Холодний (не зацікавлений)

JSON формат (суворо):
{{
  "need_identified": {{"result": true/false, "details": "конкретні потреби клієнта які виявив менеджер"}},
  "presentation_done": {{"result": true/false, "comment": "що саме презентував і як пов'язав з потребами"}},
  "objections_handled": {{"result": true/false, "comment": "які заперечення були і як відпрацював"}},
  "urgency_used": {{"result": true/false, "comment": "який тригер терміновості використав або чому не використав"}},
  "next_step_offered": {{"result": true/false, "comment": "який конкретний наступний крок запропонував"}},
  "deal_chance": "Високий/Середній/Низький",
  "deal_chance_percent": 60,
  "lead_temperature": "Гарячий/Теплий/Холодний",
  "checklist_score": 60,
  "next_contact_script": "готовий скрипт наступного повідомлення або дзвінка менеджеру",
  "top_mistakes": ["помилка 1 з прикладом", "помилка 2 з прикладом", "помилка 3 з прикладом"],
  "recommendations": "3-4 конкретні рекомендації що змінити в наступному дзвінку"
}}"""


def _clean_json(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"```$", "", raw).strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        return match.group(0)
    return raw


def _call_claude(prompt: str) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    message = client.messages.create(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = _clean_json(message.content[0].text)
    return json.loads(raw)


def analyze(record_type: str, transcription: str) -> dict:
    # Claude Sonnet supports 200k context — only trim truly extreme transcriptions
    if len(transcription) > 150000:
        # Split into beginning + end (keep both for context)
        half = 70000
        transcription = (
            transcription[:half]
            + "\n\n[... середина транскрипції скорочена для економії токенів ...]\n\n"
            + transcription[-half:]
        )

    if record_type == "lesson":
        return _call_claude(LESSON_PROMPT.format(transcription=transcription))
    return _call_claude(SALES_PROMPT.format(transcription=transcription))
