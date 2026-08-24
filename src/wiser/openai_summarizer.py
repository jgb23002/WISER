from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from wiser import llm_client
from wiser.rag import RagResult, format_excerpt

DEFAULT_OPENAI_MODEL = "gpt-5.5"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DOTENV_PATH = REPO_ROOT / ".env"


class OpenAISummarizationError(RuntimeError):
    """Raised when the optional OpenAI summarization call cannot complete."""


@dataclass(frozen=True)
class OpenAIConfig:
    model: str
    api_key_present: bool


UrlOpener = Callable[..., Any]


# Paths already parsed this process. Every LLM call site funnels through this
# one loader, so without the guard the .env file is re-read from disk several
# times per request. override=True bypasses the guard (and refreshes it).
_dotenv_loaded_paths: set[str] = set()


def reset_dotenv_cache() -> None:
    """Forget which .env files were loaded (test hook)."""
    _dotenv_loaded_paths.clear()


def load_dotenv(
    path: str | Path | None = None,
    *,
    override: bool = False,
) -> int:
    dotenv_path = Path(path) if path is not None else DEFAULT_DOTENV_PATH
    if not override and str(dotenv_path) in _dotenv_loaded_paths:
        return 0
    if not dotenv_path.is_file():
        return 0
    _dotenv_loaded_paths.add(str(dotenv_path))

    loaded = 0
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not _is_valid_env_key(key):
            continue
        if not override and key in os.environ:
            continue
        os.environ[key] = _strip_env_value(value.strip())
        loaded += 1
    return loaded


def openai_config(model: str | None = None) -> OpenAIConfig:
    load_dotenv()
    return OpenAIConfig(
        model=model or os.environ.get("WISER_OPENAI_MODEL", DEFAULT_OPENAI_MODEL),
        api_key_present=bool(os.environ.get("OPENAI_API_KEY")),
    )


# Summary answer cache — same shape as the generated-SQL cache. Keyed on the
# full request body (model, instructions, prompt with all retrieved excerpts),
# so changed context changes the key. Opt-in via use_cache.

SUMMARY_CACHE_MAX_ENTRIES = 128

_summary_cache: "OrderedDict[str, str]" = OrderedDict()
_summary_cache_lock = threading.Lock()


def clear_summary_cache() -> None:
    """Drop every cached impact summary (test hook / schema-change hook)."""
    with _summary_cache_lock:
        _summary_cache.clear()


def _summary_cache_get(key: str) -> str | None:
    with _summary_cache_lock:
        if key in _summary_cache:
            _summary_cache.move_to_end(key)
            return _summary_cache[key]
        return None


def _summary_cache_put(key: str, answer: str) -> None:
    with _summary_cache_lock:
        _summary_cache[key] = answer
        _summary_cache.move_to_end(key)
        while len(_summary_cache) > SUMMARY_CACHE_MAX_ENTRIES:
            _summary_cache.popitem(last=False)


def summarize_storm_impact_with_openai(
    query: str,
    results: Sequence[RagResult],
    *,
    api_key: str | None = None,
    model: str | None = None,
    max_bullets: int = 4,
    max_output_tokens: int = 700,
    timeout: int = 30,
    opener: UrlOpener | None = None,
    use_cache: bool = False,
) -> str:
    load_dotenv()
    resolved_api_key = api_key or os.environ.get("OPENAI_API_KEY")
    if not resolved_api_key:
        raise OpenAISummarizationError("OPENAI_API_KEY is not configured.")
    if not results:
        raise OpenAISummarizationError("No retrieved context was provided.")

    # WISER_SUMMARY_MODEL overrides just this client; WISER_OPENAI_MODEL is
    # the shared fallback for every LLM client.
    resolved_model = (
        model
        or os.environ.get("WISER_SUMMARY_MODEL")
        or os.environ.get("WISER_OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    )
    request_body = {
        "model": resolved_model,
        "instructions": (
            "You summarize weather and climate impact evidence for WISER. "
            "Use only the provided sources. Write short, decision-oriented bullets. "
            "Every bullet must end with one citation marker like [1]. "
            "Do not invent dates, locations, costs, deaths, or outage counts."
        ),
        "input": [
            {
                "role": "user",
                "content": _build_openai_prompt(
                    query,
                    results,
                    max_bullets=max_bullets,
                ),
            }
        ],
        "max_output_tokens": max_output_tokens,
    }
    if _supports_reasoning_effort(resolved_model):
        request_body["reasoning"] = {"effort": "low"}

    cache_key = None
    if use_cache:
        cache_key = hashlib.sha256(
            json.dumps(request_body, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        cached = _summary_cache_get(cache_key)
        if cached is not None:
            return cached

    try:
        payload = llm_client.post_json(
            OPENAI_RESPONSES_URL,
            request_body,
            api_key=resolved_api_key,
            timeout=timeout,
            opener=opener,
        )
    except llm_client.LlmRequestError as exc:
        raise OpenAISummarizationError(str(exc)) from exc

    output_text = _extract_output_text(payload)
    if not output_text:
        raise OpenAISummarizationError(_missing_output_text_message(payload))
    answer = output_text.strip()
    if cache_key is not None:
        _summary_cache_put(cache_key, answer)
    return answer


def _build_openai_prompt(
    query: str,
    results: Sequence[RagResult],
    *,
    max_bullets: int,
) -> str:
    lines = [
        f"Question: {query}",
        "",
        f"Return at most {max_bullets} bullets.",
        "Prefer impacts: wind damage, flooding, outages, deaths, costs, and infrastructure effects.",
        "",
        "Sources:",
    ]
    for position, result in enumerate(results, start=1):
        lines.append(
            f"[{position}] {result.title} | {result.source_path}#chunk-{result.chunk_index}"
        )
        lines.append(format_excerpt(result.content, max_chars=900))
        lines.append("")
    return "\n".join(lines).strip()


def _extract_output_text(payload: dict[str, Any]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    parts: list[str] = []
    for output_item in payload.get("output", []):
        if not isinstance(output_item, dict):
            continue
        parts.extend(_extract_text_from_content(output_item.get("content")))
        parts.extend(_extract_text_from_content(output_item.get("text")))
    return "\n".join(_dedupe_nonempty(parts)).strip()


def _extract_text_from_content(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            parts.extend(_extract_text_from_content(item))
        return parts
    if not isinstance(content, dict):
        return []

    parts: list[str] = []
    text = content.get("text")
    if isinstance(text, str):
        parts.append(text)
    elif isinstance(text, dict):
        value = text.get("value")
        if isinstance(value, str):
            parts.append(value)

    for key in ("content", "output_text"):
        value = content.get(key)
        if value is not text:
            parts.extend(_extract_text_from_content(value))
    return parts


def _dedupe_nonempty(parts: Sequence[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for part in parts:
        clean = part.strip()
        if not clean or clean in seen:
            continue
        unique.append(clean)
        seen.add(clean)
    return unique


def _supports_reasoning_effort(model: str) -> bool:
    return model.startswith(("gpt-5", "o1", "o3", "o4"))


def _missing_output_text_message(payload: dict[str, Any]) -> str:
    status = payload.get("status", "unknown")
    reason = ""
    incomplete_details = payload.get("incomplete_details")
    if isinstance(incomplete_details, dict):
        incomplete_reason = incomplete_details.get("reason")
        if incomplete_reason:
            reason = f", reason={incomplete_reason}"

    output_types: list[str] = []
    for output_item in payload.get("output", []):
        if isinstance(output_item, dict):
            output_types.append(str(output_item.get("type", "unknown")))
    output_summary = ", ".join(output_types) if output_types else "none"

    return (
        "OpenAI response did not include output text "
        f"(status={status}{reason}, output_types={output_summary})."
    )


def _is_valid_env_key(key: str) -> bool:
    return key.replace("_", "").isalnum() and not key[0].isdigit()


def _strip_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
