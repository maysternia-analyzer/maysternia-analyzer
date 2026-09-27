import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from services import analysis, detection, insights, llm
from tests.conftest import sample_lesson_analysis, sample_sales_analysis


def _api_error(cls, status, message="boom"):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(message, response=httpx2.Response(status, request=request), body=None)


def _response(text, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


class FakeMessages:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def fake_claude(monkeypatch):
    def install(*results):
        messages = FakeMessages(*results)
        monkeypatch.setattr(llm, "client", lambda: SimpleNamespace(messages=messages))
        return messages
    return install


# ── llm ───────────────────────────────────────────────────────────────────────

def test_call_json_uses_structured_outputs(fake_claude):
    messages = fake_claude(_response('{"a": 1}'))
    assert llm.call_json("sys", "user", {"type": "object"}) == {"a": 1}
    call = messages.calls[0]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["model"] == llm.DEFAULT_MODEL
    assert call["system"] == "sys"


def test_call_json_falls_back_when_structured_outputs_unsupported(fake_claude):
    messages = fake_claude(_api_error(anthropic.BadRequestError, 400, "output_config.format: not supported"),
                           _response('```json\n{"a": 2}\n```'))
    assert llm.call_json("sys", "user", {}) == {"a": 2}
    assert "output_config" not in messages.calls[1]


def test_call_json_other_bad_request_is_permanent(fake_claude):
    fake_claude(_api_error(anthropic.BadRequestError, 400, "prompt is too long"))
    with pytest.raises(llm.LLMError) as err:
        llm.call_json("s", "u", {})
    assert not err.value.transient


@pytest.mark.parametrize("cls,status,transient", [
    (anthropic.RateLimitError, 429, True),
    (anthropic.InternalServerError, 500, True),
    (anthropic.OverloadedError, 529, True),
    (anthropic.AuthenticationError, 401, False),
    (anthropic.NotFoundError, 404, False),
])
def test_call_json_error_mapping(fake_claude, cls, status, transient):
    fake_claude(_api_error(cls, status))
    with pytest.raises(llm.LLMError) as err:
        llm.call_json("s", "u", {})
    assert err.value.transient is transient


def test_call_json_max_tokens_and_refusal(fake_claude):
    fake_claude(_response('{"a": ', stop_reason="max_tokens"))
    with pytest.raises(llm.LLMError, match="обрізана") as err:
        llm.call_json("s", "u", {})
    assert not err.value.transient  # повтор з тим самим входом обріжеться знову
    fake_claude(_response("", stop_reason="refusal"))
    with pytest.raises(llm.LLMError, match="відмовився"):
        llm.call_json("s", "u", {})


def test_call_json_invalid_json_is_transient(fake_claude):
    fake_claude(_response("не json"))
    with pytest.raises(llm.LLMError) as err:
        llm.call_json("s", "u", {})
    assert err.value.transient


def test_call_json_connection_errors_are_transient(fake_claude):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    for error in (anthropic.APIConnectionError(request=request), anthropic.APITimeoutError(request=request)):
        fake_claude(error)
        with pytest.raises(llm.LLMError) as err:
            llm.call_json("s", "u", {})
        assert err.value.transient


def test_call_json_through_real_sdk(monkeypatch):
    """Справжній SDK з підміненим HTTP-транспортом: перевіряємо реальне тіло запиту."""
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 2:
            return httpx2.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": '{"ok": true}'}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    real = anthropic.Anthropic(api_key="sk-ant-test", max_retries=0,
                               http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    monkeypatch.setattr(llm, "client", lambda: real)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
              "additionalProperties": False}
    assert llm.call_json("система", "запит", schema, max_tokens=123) == {"ok": True}
    assert seen[0]["output_config"] == {"format": {"type": "json_schema", "schema": schema}}
    assert seen[0]["system"] == "система" and seen[0]["max_tokens"] == 123
    with pytest.raises(llm.LLMError) as err:
        llm.call_json("s", "u", schema)
    assert err.value.transient


def test_model_env_override(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5")
    assert llm.model() == "claude-sonnet-5"
    monkeypatch.setenv("ANTHROPIC_MODEL", "  ")
    assert llm.model() == llm.DEFAULT_MODEL


def test_client_requires_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(llm.LLMError):
        llm.client()


def test_extract_json():
    assert llm.extract_json('Ось результат: {"x": [1, 2]} кінець') == {"x": [1, 2]}
    with pytest.raises(ValueError):
        llm.extract_json("[]")


# ── analysis ─────────────────────────────────────────────────────────────────

def test_normalize_lesson_handles_garbage():
    result = analysis.normalize_lesson({"greeting": None, "overall_score": "85.6", "engagement_level": "високий рівень",
                                        "strengths": ["a", "b"], "summary": None})
    assert result["greeting"] == {"result": False, "comment": ""}
    assert result["overall_score"] == 86
    assert result["engagement_level"] == "Високий"
    assert result["strengths"] == "a\nb"
    assert result["summary"] == ""
    assert result["_kind"] == "lesson"
    assert set(k for k, _ in analysis.LESSON_CRITERIA) <= set(result)


def test_normalize_sales_clamps_and_defaults():
    result = analysis.normalize_sales({"checklist_score": 150, "deal_chance_percent": -5, "deal_chance": None,
                                       "top_mistakes": "одна помилка",
                                       "need_identified": {"result": 1, "details": "потреба"}})
    assert result["checklist_score"] == 100 and result["deal_chance_percent"] == 0
    assert result["deal_chance"] == ""
    assert result["top_mistakes"] == ["одна помилка"]
    assert result["need_identified"] == {"result": True, "comment": "потреба", "details": "потреба"}
    assert result["_kind"] == "sales"


def test_to_score():
    assert analysis.to_score(None) == 0
    assert analysis.to_score(None, None) is None
    assert analysis.to_score("abc", 5) == 5
    assert analysis.to_score(42.4) == 42


def test_prepare_transcript_truncates_only_huge_texts():
    text, cut = analysis.prepare_transcript("коротко")
    assert (text, cut) == ("коротко", False)
    huge = "а" * (analysis.MAX_TRANSCRIPT_CHARS + 10)
    text, cut = analysis.prepare_transcript(huge)
    assert cut and len(text) < len(huge) and "скорочена" in text


def test_analyze_routes_by_type(monkeypatch):
    seen = []

    def fake_call_json(system, user, schema, max_tokens=16000):
        seen.append((system, schema))
        return {"overall_score": 70} if schema is analysis.LESSON_SCHEMA else {"checklist_score": 40}

    monkeypatch.setattr(analysis, "call_json", fake_call_json)
    assert analysis.analyze("lesson", "текст")["overall_score"] == 70
    assert analysis.analyze("sales", "текст")["checklist_score"] == 40
    assert seen[0][0] is analysis.LESSON_SYSTEM and seen[1][0] is analysis.SALES_SYSTEM


def _assert_strict(schema, path="$"):
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            assert schema.get("additionalProperties") is False, path
            assert set(schema["required"]) == set(schema["properties"]), path
        for key, value in schema.items():
            _assert_strict(value, f"{path}.{key}")
    elif isinstance(schema, list):
        for i, item in enumerate(schema):
            _assert_strict(item, f"{path}[{i}]")


def test_schemas_are_strict_at_every_level():
    for schema in (analysis.LESSON_SCHEMA, analysis.SALES_SCHEMA, detection.DETECT_SCHEMA, insights.INSIGHTS_SCHEMA):
        _assert_strict(schema)


# ── detection ────────────────────────────────────────────────────────────────

TRANSCRIPT = "Код Харизми: Вітаю\nMyroslava: Привіт усім, я тренер. " + "Вправа. " * 50 + "\nАнна: Круто"


def test_detection_heuristic_uses_top_speaker_without_llm(monkeypatch):
    monkeypatch.setattr(llm, "call_json", lambda *a, **k: pytest.fail("LLM не потрібен"))
    result = detection.detect_type_and_name("Zoom Meeting", 130, False, TRANSCRIPT, "H40904875")
    assert result["record_type"] == "lesson"
    assert result["person_name"] == "Myroslava"  # раніше тут був email-нік хоста
    sales_call = "Олена: Добрий день\nКлієнт: Привіт\nОлена: Розкажіть про себе"
    assert detection.detect_type_and_name("x", 30, True, sales_call, "Host") == {
        "record_type": "sales", "person_name": "Олена",
        "reason": "евристика за тривалістю/типом кімнати; імʼя — найактивніший спікер"}


def test_detection_ambiguous_asks_claude(monkeypatch):
    monkeypatch.setattr(llm, "call_json", lambda *a, **k: {"record_type": "sales", "person_name": "Олена",
                                                           "confidence": 90, "reason": "1:1"})
    result = detection.detect_type_and_name("Meeting", 25, False, TRANSCRIPT, "Host")
    assert result == {"record_type": "sales", "person_name": "Олена", "reason": "1:1"}


def test_detection_rejects_host_account_name_and_bad_type(monkeypatch):
    monkeypatch.setattr(llm, "call_json", lambda *a, **k: {"record_type": "webinar", "person_name": "Код Харизми",
                                                           "confidence": 10, "reason": ""})
    result = detection.detect_type_and_name("Meeting", 25, False, TRANSCRIPT, "Host")
    assert result["person_name"] == "Myroslava"
    assert result["record_type"] == "sales"  # 25 хв → евристика


def test_detection_whisper_text_without_speakers_asks_claude_for_name(monkeypatch):
    whisper_text = "Добрий день усім. Сьогодні у нас план такий: розминка і вправи. Мене звати Оксана."
    asked = []

    def fake(system, user, schema, max_tokens=0):
        asked.append((user, max_tokens))
        return {"record_type": "sales", "person_name": "Оксана", "confidence": 80, "reason": "представилась"}

    monkeypatch.setattr(llm, "call_json", fake)
    result = detection.detect_type_and_name("Zoom", 120, False, whisper_text, "H40904875")
    assert result["person_name"] == "Оксана"
    assert result["record_type"] == "lesson"          # тип за евристикою не перебивається
    assert "Ймовірний тип за тривалістю: lesson" in asked[0][0]
    assert asked[0][1] >= 4096


def test_detection_whisper_text_without_llm_falls_back(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    text = "Добрий день усім. Сьогодні у нас план такий: розминка"
    result = detection.detect_type_and_name("Zoom", 120, False, text, "Host")
    assert result == {"record_type": "lesson", "person_name": "Host", "reason": "запасна евристика"}


def test_detection_survives_llm_failure(monkeypatch):
    def boom(*a, **k):
        raise llm.LLMError("down", transient=True)
    monkeypatch.setattr(llm, "call_json", boom)
    result = detection.detect_type_and_name("Meeting", 45, False, "", "Host")
    assert result["person_name"] == "Host" and result["record_type"] == "sales"


# ── insights ─────────────────────────────────────────────────────────────────

def _records():
    return [
        {"record_type": "sales", "analysis": sample_sales_analysis(80), "sale_made": 1, "sale_amount": 20000.0,
         "person_name": "Олена", "record_date": "2026-09-01", "transcription": "т" * 5000},
        {"record_type": "sales", "analysis": sample_sales_analysis(40), "sale_made": 0, "sale_amount": None,
         "person_name": "Ігор", "record_date": "2026-09-02"},
        {"record_type": "sales", "analysis": sample_sales_analysis(60), "sale_made": None,
         "person_name": "Ігор", "record_date": "2026-09-03"},
        {"record_type": "lesson", "analysis": sample_lesson_analysis(90), "person_name": "Мирослава",
         "record_date": "2026-09-01"},
        {"record_type": "lesson", "analysis": None, "person_name": "X", "record_date": "2026-09-01"},
    ]


def test_compute_metrics():
    sales = [r for r in _records() if r["record_type"] == "sales"]
    lessons = [r for r in _records() if r["record_type"] == "lesson" and r["analysis"]]
    m = insights.compute_metrics(sales, lessons)
    assert m["avg_checklist_score"] == 60
    assert m["conversion_rate"] == 50          # 1 з 2 записів з відміченим результатом
    assert m["total_revenue"] == 20000 and m["avg_deal_size"] == 20000
    assert m["lesson_avg_score"] == 90


def test_generate_insights_overrides_numbers_and_limits_payload(monkeypatch):
    captured = {}

    def fake_call_json(system, user, schema, max_tokens=16000):
        captured["user"] = user
        return {"summary": "ok", "sales_patterns": {"total_revenue": 999999, "conversion_rate": 99},
                "lesson_insights": {}}

    monkeypatch.setattr(insights, "call_json", fake_call_json)
    data = insights.generate_insights(_records())
    assert data["sales_patterns"]["total_revenue"] == 20000
    assert data["sales_patterns"]["conversion_rate"] == 50
    assert data["lesson_insights"]["avg_score"] == 90
    payload = json.loads(captured["user"].split("<data>\n", 1)[1].rsplit("\n</data>", 1)[0])
    assert len(payload["sales"][0]["transcript_preview"]) == insights.SALES_PREVIEW_CHARS + 1  # + «…»


def test_insights_payload_budget_keeps_newest(monkeypatch):
    monkeypatch.setattr(insights, "MAX_PAYLOAD_CHARS", 20_000)
    captured = {}
    monkeypatch.setattr(insights, "call_json", lambda s, u, schema, max_tokens=0: captured.setdefault("u", u) and {})
    big = [{"record_type": "sales", "analysis": sample_sales_analysis(), "person_name": f"M{i}",
            "record_date": f"2026-09-{i % 28 + 1:02d}", "transcription": "т" * 5000} for i in range(100)]
    data = insights.generate_insights(big)
    body = captured["u"].split("<data>\n", 1)[1].rsplit("\n</data>", 1)[0]
    assert len(body) <= 20_000
    assert json.loads(body)["sales"][0]["manager"] == "M0"
    assert "найсвіжіших" in captured["u"]
    assert data["metrics"]["sales_count"] == 100


def test_insights_tolerate_legacy_non_dict_values(monkeypatch):
    monkeypatch.setattr(insights, "call_json", lambda *a, **k: {"summary": "ok", "sales_patterns": "bad"})
    legacy = [{"record_type": "sales", "person_name": "X", "record_date": "2026-06-01",
               "analysis": {"checklist_score": "70", "need_identified": True, "objections_handled": "так",
                            "top_mistakes": "одна", "deal_chance": "Високий"}}]
    data = insights.generate_insights(legacy)
    assert data["sales_patterns"]["avg_checklist_score"] == 70


def test_generate_insights_empty():
    assert insights.generate_insights([])["top_needs"] == []
