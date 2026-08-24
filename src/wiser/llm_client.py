"""Shared OpenAI HTTP transport for every LLM-bearing module.

Three modules (SQL generation, storm reasoning, impact summaries) previously
carried their own copy of the same urllib request/error ladder and the same
model-capability switches. This module holds the single implementation; each
caller keeps its own thin wrapper so its public exception type — and the
seam its tests patch — stays unchanged.

Retry policy, deliberately conservative:
- HTTP 429/5xx: retried with backoff, honouring Retry-After when sane.
- Connection-level failures that fail fast (refused/reset): retried.
- Timeouts: NOT retried. A 30 s timeout retried twice would triple the
  user-visible stall for a network that is simply down.
- 4xx other than 429, and malformed response bodies: not retried.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

MAX_RETRIES = 2
_BACKOFF_SECONDS = (0.5, 1.0)
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRY_AFTER_SECONDS = 10.0

UrlOpener = Callable[..., Any]


class LlmRequestError(RuntimeError):
    """The OpenAI call failed (after any retries) or returned a bad payload."""


class LlmUnavailableError(LlmRequestError):
    """Transport-level failure: the API endpoint could not be reached at all.

    Callers use this to skip follow-up LLM calls in the same request — a
    second 30 s attempt against an unreachable network only doubles the stall.
    """


def uses_completion_token_limit(model: str) -> bool:
    normalized = model.lower()
    return normalized.startswith(("gpt-5", "o1", "o3", "o4"))


def add_generation_options(
    body: dict[str, Any],
    model: str,
    *,
    max_tokens: int,
    temperature: float,
) -> None:
    if uses_completion_token_limit(model):
        body["max_completion_tokens"] = max_tokens
        return
    body["max_tokens"] = max_tokens
    body["temperature"] = temperature


def _retry_delay(attempt: int, exc: Exception) -> float:
    delay = _BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)]
    if isinstance(exc, HTTPError):
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        if retry_after:
            try:
                delay = max(delay, min(float(retry_after), _MAX_RETRY_AFTER_SECONDS))
            except ValueError:
                pass
    return delay


def _is_fast_connection_failure(exc: URLError) -> bool:
    reason = getattr(exc, "reason", None)
    return isinstance(reason, (ConnectionRefusedError, ConnectionResetError))


def post_json(
    url: str,
    body: dict[str, Any],
    *,
    api_key: str,
    timeout: int = 30,
    opener: UrlOpener | None = None,
) -> dict[str, Any]:
    """POST a JSON body with bearer auth and return the parsed JSON response.

    Raises LlmUnavailableError for transport failures, LlmRequestError for
    HTTP errors and malformed payloads. HTTPError detail text is preserved in
    the message exactly as the per-module clients formatted it.
    """
    data = json.dumps(body).encode("utf-8")
    open_url = opener or urlopen
    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES + 1):
        request = Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with open_url(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code not in _RETRYABLE_STATUS or attempt >= MAX_RETRIES:
                # read() only on the raise path: a retried error's body is
                # never reported, and reading it here would exhaust a mock
                # error object re-raised across attempts.
                detail = exc.read().decode("utf-8", errors="ignore")
                raise LlmRequestError(
                    f"OpenAI request failed with HTTP {exc.code}: {detail}"
                ) from exc
            last_error = exc
            time.sleep(_retry_delay(attempt, exc))
        except TimeoutError as exc:
            raise LlmUnavailableError(f"OpenAI request failed: {exc}") from exc
        except URLError as exc:
            if _is_fast_connection_failure(exc) and attempt < MAX_RETRIES:
                last_error = exc
                time.sleep(_retry_delay(attempt, exc))
                continue
            raise LlmUnavailableError(f"OpenAI request failed: {exc}") from exc
        except OSError as exc:
            raise LlmUnavailableError(f"OpenAI request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise LlmRequestError(f"OpenAI request failed: {exc}") from exc

    raise LlmRequestError(f"OpenAI request failed after retries: {last_error}")


def post_chat(
    *,
    messages: list[dict[str, str]],
    model: str,
    api_key: str,
    max_tokens: int,
    temperature: float,
    timeout: int = 30,
    opener: UrlOpener | None = None,
) -> str:
    """Chat-completions call returning the first choice's content string."""
    body: dict[str, Any] = {"model": model, "messages": messages}
    add_generation_options(body, model, max_tokens=max_tokens, temperature=temperature)
    payload = post_json(
        OPENAI_CHAT_URL, body, api_key=api_key, timeout=timeout, opener=opener
    )
    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmRequestError(f"Unexpected OpenAI response shape: {exc}") from exc
