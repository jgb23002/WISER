from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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


def load_dotenv(
    path: str | Path | None = None,
    *,
    override: bool = False,
) -> int:
    dotenv_path = Path(path) if path is not None else DEFAULT_DOTENV_PATH
    if not dotenv_path.is_file():
        return 0

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
) -> str:
    load_dotenv()
    resolved_api_key = api_key or os.environ.get("OPENAI_API_KEY")
    if not resolved_api_key:
        raise OpenAISummarizationError("OPENAI_API_KEY is not configured.")
    if not results:
        raise OpenAISummarizationError("No retrieved context was provided.")

    resolved_model = model or os.environ.get("WISER_OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
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

    data = json.dumps(request_body).encode("utf-8")
    request = Request(
        OPENAI_RESPONSES_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {resolved_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        open_url = opener or urlopen
        with open_url(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise OpenAISummarizationError(
            f"OpenAI request failed with HTTP {exc.code}: {detail}"
        ) from exc
    except (OSError, URLError, json.JSONDecodeError) as exc:
        raise OpenAISummarizationError(f"OpenAI request failed: {exc}") from exc

    output_text = _extract_output_text(payload)
    if not output_text:
        raise OpenAISummarizationError(_missing_output_text_message(payload))
    return output_text.strip()


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
