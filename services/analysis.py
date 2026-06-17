import os
import json
import re
import anthropic

LESSON_PROMPT = """Ти — експерт з аналізу якості онлайн-навчання для школи "Майстерня скілів".

Проаналізуй транскрипцію пробного заняття. Відповідай ТІЛЬКИ валідним JSON без markdown.
Всі текстові поля — максимум 2 речення, стисло.

Транскрипція:
{transcription}

JSON формат (суворо дотримуйся):
{{
  "greeting": {{"result": true, "comment": "..."}},
  "safe_atmosphere": {{"result": true, "comment": "..."}},
  "structure_explained": {{"result": true, "comment": "..."}},
  "practical_exercises": {{"result": true, "comment": "..."}},
  "feedback_received": {{"result": true, "comment": "..."}},
  "transition_to_manager": {{"result": true, "comment": "..."}},
  "overall_score": 75,
  "engagement_level": "Середній",
  "strengths": "...",
  "improvements": "...",
  "summary": "..."
}}"""

SALES_PROMPT = """Ти — експерт з аналізу продажів для школи "Майстерня скілів".
Курс "Код Харизми" 15 000–30 000 грн. Відповідай ТІЛЬКИ валідним JSON без markdown.
Всі текстові поля — максимум 2 речення, стисло.

Транскрипція:
{transcription}

JSON формат (суворо дотримуйся):
{{
  "need_identified": {{"result": true, "details": "..."}},
  "presentation_done": {{"result": true, "comment": "..."}},
  "objections_handled": {{"result": true, "comment": "..."}},
  "urgency_used": {{"result": true, "comment": "..."}},
  "next_step_offered": {{"result": true, "comment": "..."}},
  "deal_chance": "Середній",
  "deal_chance_percent": 60,
  "lead_temperature": "Теплий",
  "checklist_score": 60,
  "next_contact_script": "...",
  "top_mistakes": ["...", "...", "..."],
  "recommendations": "..."
}}"""


def _clean_json(raw: str) -> str:
    """Strip markdown fences and extract first JSON object."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"```$", "", raw).strip()
    # Extract first complete JSON object
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        return match.group(0)
    return raw


def _call_claude(prompt: str) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = _clean_json(message.content[0].text)
    return json.loads(raw)


def analyze(record_type: str, transcription: str) -> dict:
    # Trim very long transcriptions to avoid hitting context limits
    if len(transcription) > 12000:
        transcription = transcription[:12000] + "\n[транскрипція скорочена]"

    if record_type == "lesson":
        return _call_claude(LESSON_PROMPT.format(transcription=transcription))
    return _call_claude(SALES_PROMPT.format(transcription=transcription))
