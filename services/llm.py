"""Спільний клієнт Anthropic: виклик Claude з гарантовано валідним JSON (structured outputs)."""
import json
import logging
import os
import re
import threading

import anthropic

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"


class LLMError(RuntimeError):
    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


def model() -> str:
    return os.environ.get("ANTHROPIC_MODEL", "").strip() or DEFAULT_MODEL


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


def call_json(system: str, user: str, schema: dict, max_tokens: int = 16000) -> dict:
    """Запит до Claude, що повертає dict за JSON-схемою."""
    kwargs = dict(model=model(), max_tokens=max_tokens, system=system,
                  messages=[{"role": "user", "content": user}])
    api = client()
    try:
        try:
            resp = api.messages.create(
                **kwargs, output_config={"format": {"type": "json_schema", "schema": schema}}
            )
        except anthropic.BadRequestError as e:
            if not _is_structured_output_unsupported(e):
                raise
            log.warning("Модель %s не підтримує structured outputs — звичайний режим", model())
            kwargs["system"] = system + "\n\nВідповідай ТІЛЬКИ валідним JSON-обʼєктом без markdown."
            resp = api.messages.create(**kwargs)
    except anthropic.AuthenticationError as e:
        raise LLMError("Ключ Anthropic недійсний — оновіть ANTHROPIC_API_KEY") from e
    except anthropic.PermissionDeniedError as e:
        raise LLMError(f"Anthropic: немає доступу ({e.message})") from e
    except anthropic.NotFoundError as e:
        raise LLMError(f"Модель {model()} недоступна — перевірте ANTHROPIC_MODEL") from e
    except anthropic.RateLimitError as e:
        raise LLMError("Anthropic: перевищено ліміт запитів, повторимо пізніше", transient=True) from e
    except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
        raise LLMError(f"Anthropic API недоступний: {e}", transient=True) from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"Anthropic API помилка {e.status_code}: {e.message}",
                       transient=e.status_code >= 500) from e

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
