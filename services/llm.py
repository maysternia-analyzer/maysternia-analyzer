"""Спільний клієнт Anthropic: виклик Claude з гарантовано валідним JSON (structured outputs)."""
import json
import logging
import os
import re
import threading
import time

import anthropic

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"


class LLMError(RuntimeError):
    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


def model() -> str:
    """Модель: з налаштувань адмінки → ANTHROPIC_MODEL → за замовчуванням."""
    try:
        from services import settings
        chosen = settings.get("anthropic_model")
    except Exception:  # БД недоступна — не блокуємо аналіз
        chosen = ""
    return chosen or os.environ.get("ANTHROPIC_MODEL", "").strip() or DEFAULT_MODEL


def is_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


_client = None
_client_lock = threading.Lock()


def client() -> anthropic.Anthropic:
    global _client
    if not is_configured():
        raise LLMError("ANTHROPIC_API_KEY не задано — AI-аналіз недоступний")
    with _client_lock:
        if _client is None:
            _client = anthropic.Anthropic(
                api_key=os.environ["ANTHROPIC_API_KEY"],
                # Довга відповідь може генеруватись хвилинами, але зʼєднання — лише секунди.
                timeout=anthropic.Timeout(600, connect=10),
                max_retries=2,
            )
        return _client


def validate_model(model_id: str) -> str | None:
    """Перевіряє, що модель існує. Повертає текст помилки або None (у т.ч. якщо перевірити не вдалося)."""
    try:
        client().with_options(timeout=15, max_retries=0).models.retrieve(model_id)
    except anthropic.NotFoundError:
        return f"Модель «{model_id}» не знайдено в Anthropic — перевірте назву"
    except Exception as e:  # мережа / ключ — не блокуємо збереження
        log.warning("Не вдалося перевірити модель %s: %s", model_id, e)
    return None


def extract_json(raw: str) -> dict:
    """Дістає JSON-обʼєкт з тексту (на випадок відповіді без structured outputs)."""
    raw = (raw or "").strip()
    raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end < start:
        raise ValueError("JSON не знайдено у відповіді")
    data = json.loads(raw[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("Очікувався JSON-обʼєкт")
    return data


def _is_structured_output_unsupported(error: anthropic.BadRequestError) -> bool:
    message = str(error).lower()
    return any(word in message for word in ("output_config", "json_schema", "structured output"))


STREAM_THRESHOLD = 16000  # довші відповіді отримуємо стрімінгом (без ризику HTTP-таймаутів)


_TRANSIENT_STREAM_ERRORS = ("overloaded_error", "api_error", "rate_limit_error")


def _send(api, **kwargs):
    if kwargs["max_tokens"] <= STREAM_THRESHOLD:
        return api.messages.create(**kwargs)
    try:
        with api.messages.stream(**kwargs) as stream:
            return stream.get_final_message()
    except anthropic.APIStatusError as e:
        if e.status_code != 200:
            raise
        # Подія помилки прийшла вже після HTTP 200 (посеред стріму): визначаємо тип з тіла.
        error = e.body.get("error") if isinstance(e.body, dict) else None
        error_type = error.get("type") if isinstance(error, dict) else None
        raise LLMError(f"Anthropic: {error_type or e.message}",
                       transient=error_type in _TRANSIENT_STREAM_ERRORS) from e
    except anthropic.APIError:
        raise
    except Exception as e:  # обрив зʼєднання під час читання стріму (помилки транспорту httpx)
        raise LLMError(f"Зʼєднання з Anthropic обірвалося: {e}", transient=True) from e


def call_json(system: str, user: str, schema: dict, max_tokens: int = 16000) -> dict:
    """
    Запит до Claude, що повертає dict за JSON-схемою. Новіші моделі (Sonnet 5, Opus 5) за
    замовчуванням «думають» — це входить у max_tokens, тож для великих відповідей беріть запас.
    """
    kwargs = dict(model=model(), max_tokens=max_tokens, system=system,
                  messages=[{"role": "user", "content": user}])
    api = client()
    started = time.monotonic()
    try:
        try:
            resp = _send(api, **kwargs, output_config={"format": {"type": "json_schema", "schema": schema}})
        except anthropic.BadRequestError as e:
            if not _is_structured_output_unsupported(e):
                raise
            log.warning("Модель %s не підтримує structured outputs — звичайний режим", model())
            kwargs["system"] = system + "\n\nВідповідай ТІЛЬКИ валідним JSON-обʼєктом без markdown."
            resp = _send(api, **kwargs)
    except anthropic.AuthenticationError as e:
        raise LLMError("Ключ Anthropic недійсний — оновіть ANTHROPIC_API_KEY") from e
    except anthropic.PermissionDeniedError as e:
        raise LLMError(f"Anthropic: немає доступу ({e.message})") from e
    except anthropic.NotFoundError as e:
        raise LLMError(f"Модель {model()} недоступна — оберіть іншу в «Налаштуваннях»") from e
    except anthropic.RateLimitError as e:
        raise LLMError("Anthropic: перевищено ліміт запитів, повторимо пізніше", transient=True) from e
    except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
        raise LLMError(f"Anthropic API недоступний: {e}", transient=True) from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"Anthropic API помилка {e.status_code}: {e.message}",
                       transient=e.status_code >= 500) from e

    usage = getattr(resp, "usage", None)
    log.info("Claude %s: %.1f с, токени: вхід %s, вихід %s, stop=%s", kwargs["model"], time.monotonic() - started,
             getattr(usage, "input_tokens", "?"), getattr(usage, "output_tokens", "?"), resp.stop_reason)
    if resp.stop_reason == "refusal":
        raise LLMError("Claude відмовився обробляти цей текст")
    if resp.stop_reason == "max_tokens":
        # Повтор з тим самим входом обріжеться знову — не витрачаємо гроші на повтори.
        raise LLMError("Відповідь Claude обрізана через ліміт токенів")
    text = "".join(block.text for block in resp.content if block.type == "text")
    try:
        return extract_json(text)
    except ValueError as e:
        raise LLMError(f"Claude повернув невалідний JSON: {e}", transient=True) from e
