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

    def stream(self, **kwargs):
        response = self.create(**kwargs)
        self.calls[-1]["_streamed"] = True

        class _Stream:
            def __enter__(self_inner):
                return SimpleNamespace(get_final_message=lambda: response)

            def __exit__(self_inner, *exc):
                return False
        return _Stream()


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
                                        "strengths": ["a", "b"], "summary": None, "key_moments": "bad",
                                        "coaching_phrases": [{"phrase": "так"}, None]})
    assert result["criteria"]["greeting"] == {"result": False, "comment": "", "quote": "", "time": ""}
    assert result["overall_score"] == 86 and result["checklist_score"] == 0
    assert result["key_moments"] == [] and result["coaching_phrases"] == [{"situation": "", "phrase": "так"}]
    assert result["engagement_level"] == "Високий"
    assert result["strengths"] == "a\nb"
    assert result["summary"] == ""
    assert result["_kind"] == "lesson"
    assert [c["key"] for c in result["_criteria"]] == [k for k, _ in analysis.LESSON_CRITERIA]


def test_normalize_sales_clamps_and_defaults():
    result = analysis.normalize_sales({"checklist_score": 150, "deal_chance_percent": -5, "deal_chance": None,
                                       "top_mistakes": "одна помилка", "objections": [{"text": "Дорого", "category": "ЦІНА!"}],
                                       "risks": [{"category": "щось нове", "description": "x"}], "next_step": "bad",
                                       "need_identified": {"result": 1, "details": "потреба", "time": "1:05"}})
    assert result["checklist_score"] == 20        # рахується з критеріїв (1 з 5), а не береться від AI
    assert result["deal_chance_percent"] == 0 and result["deal_chance"] == ""
    assert result["top_mistakes"] == ["одна помилка"]
    assert result["criteria"]["need_identified"] == {"result": True, "comment": "потреба", "quote": "", "time": "00:01:05"}
    assert result["objections"][0]["category"] == "ціна" and result["risks"][0]["category"] == "інше"
    assert result["next_step"] == {"agreed": False, "description": "", "deadline": ""}
    assert result["_kind"] == "sales"


def test_weighted_checklist_score_and_custom_checklist(monkeypatch):
    checklist = {"criteria": [{"key": "a", "title": "A", "weight": 3, "description": "a"},
                              {"key": "b", "title": "B", "weight": 1, "description": "b"}]}
    result = analysis.normalize("sales", {"criteria": {"a": {"result": True}, "b": {"result": False}}}, checklist)
    assert result["checklist_score"] == 75
    assert result["_criteria"] == [{"key": "a", "title": "A", "weight": 3}, {"key": "b", "title": "B", "weight": 1}]
    items = analysis.present_criteria(result, "sales")
    assert [(c["title"], c["result"], c["weight"]) for c in items] == [("A", True, 3), ("B", False, 1)]


def test_present_criteria_for_legacy_flat_analysis():
    legacy = {"need_identified": {"result": True, "details": "потреба"}, "presentation_done": {"result": False},
              "checklist_score": 60}
    items = analysis.present_criteria(legacy, "sales")
    assert [c["title"] for c in items] == ["Виявив потребу клієнта", "Зробив презентацію курсу"]
    assert items[0]["comment"] == "потреба"
    assert analysis.main_score(legacy, "sales") == 60
    assert analysis.main_score({"overall_score": "77"}, "lesson") == 77
    assert analysis.main_score({}, "sales") is None


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


def test_analyze_uses_configured_checklist_and_context(monkeypatch):
    from services import checklists, settings
    checklists.save_checklist("sales", {"criteria": [
        {"title": "Назвав ціну", "description": "озвучив вартість курсу", "weight": 2},
        {"key": "need_identified", "title": "Потреба", "description": "зʼясував потребу", "weight": 1}],
        "instructions": "Скрипт: привітання → діагностика → ціна"})
    settings.update({"company_context": "Школа танців «Тест», курс 5000 грн"})
    seen = []

    def fake_call_json(system, user, schema, max_tokens=16000):
        seen.append((system, schema))
        keys = list(schema["properties"]["criteria"]["properties"])
        return {"criteria": {keys[0]: {"result": True, "quote": "Курс коштує 5000", "time": "00:02:10"}},
                "overall_score": 70, "deal_chance": "Високий"}

    monkeypatch.setattr(analysis, "call_json", fake_call_json)
    sales = analysis.analyze("sales", "текст")
    system, schema = seen[0]
    assert "Школа танців «Тест»" in system and "Назвав ціну" in system and "діагностика → ціна" in system
    assert "need_identified" in schema["properties"]["criteria"]["properties"]
    assert "objections" in schema["properties"]
    assert sales["checklist_score"] == 67          # вага 2 з 3
    assert sales["criteria"][sales["_criteria"][0]["key"]]["time"] == "00:02:10"
    lesson = analysis.analyze("lesson", "текст")
    assert lesson["overall_score"] == 70 and "objections" not in seen[1][1]["properties"]


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
    from services import checklists, coaching
    schemas = [analysis.build_schema(kind, checklists.DEFAULTS[kind]) for kind in checklists.KINDS]
    for schema in schemas + [detection.DETECT_SCHEMA, insights.INSIGHTS_SCHEMA, coaching.SCHEMA]:
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
    first = payload["sales"][0]
    assert "transcript_preview" not in first            # є резюме — сирий фрагмент не потрібен
    assert first["summary"] and first["objections"][0]["category"] == "ціна"
    assert first["checklist"]["Виявив потребу клієнта"] is True
    legacy = {"record_type": "sales", "analysis": {"checklist_score": 50}, "person_name": "X",
              "record_date": "2026-09-01", "transcription": "т" * 5000}
    assert len(insights._sale_summary(legacy)["transcript_preview"]) == insights.SALES_PREVIEW_CHARS + 1


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


def test_large_responses_are_streamed(fake_claude):
    messages = fake_claude(_response('{"a": 1}'), _response('{"b": 2}'))
    assert llm.call_json("s", "u", {}, max_tokens=32000) == {"a": 1}
    assert messages.calls[0].get("_streamed") and messages.calls[0]["max_tokens"] == 32000
    assert llm.call_json("s", "u", {}, max_tokens=4096) == {"b": 2}
    assert not messages.calls[1].get("_streamed")


def test_validate_model(monkeypatch):
    def retrieve(model_id):
        if model_id == "claude-bad":
            raise _api_error(anthropic.NotFoundError, 404, "not found")
        if model_id == "offline":
            raise RuntimeError("offline")
        return SimpleNamespace(id=model_id)

    fake = SimpleNamespace(with_options=lambda **k: SimpleNamespace(models=SimpleNamespace(retrieve=retrieve)))
    monkeypatch.setattr(llm, "client", lambda: fake)
    assert llm.validate_model("claude-sonnet-5") is None
    assert "не знайдено" in llm.validate_model("claude-bad")
    assert llm.validate_model("offline") is None


def _sse(*events):
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


_MESSAGE_START = ("message_start", {"type": "message_start", "message": {
    "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-4-6", "content": [],
    "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}})


class _BrokenStream(httpx2.SyncByteStream):
    def __iter__(self):
        yield _sse(_MESSAGE_START)
        raise httpx2.ReadError("connection reset")


@pytest.mark.parametrize("error_type, transient", [("overloaded_error", True), ("api_error", True),
                                                   ("invalid_request_error", False)])
def test_stream_error_events_are_classified(monkeypatch, error_type, transient):
    """Помилка посеред стріму приходить після HTTP 200 — тип визначаємо з події."""
    def handler(request):
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=_sse(
            _MESSAGE_START, ("error", {"type": "error", "error": {"type": error_type, "message": "x"}})))

    real = anthropic.Anthropic(api_key="sk-ant-test", max_retries=0,
                               http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    monkeypatch.setattr(llm, "client", lambda: real)
    with pytest.raises(llm.LLMError) as err:
        llm.call_json("s", "u", {}, max_tokens=32000)
    assert err.value.transient is transient and error_type in str(err.value)


def test_stream_connection_drop_is_transient(monkeypatch):
    def handler(request):
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=_BrokenStream())

    real = anthropic.Anthropic(api_key="sk-ant-test", max_retries=0,
                               http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    monkeypatch.setattr(llm, "client", lambda: real)
    with pytest.raises(llm.LLMError) as err:
        llm.call_json("s", "u", {}, max_tokens=32000)
    assert err.value.transient
