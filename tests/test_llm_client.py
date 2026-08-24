"""Tests for the shared OpenAI transport (wiser.llm_client).

The client is exercised entirely through fake openers — no network, no
sleeps beyond the patched-out backoff.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

REPO_ROOT = Path(__file__).parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser import llm_client  # noqa: E402
from wiser.llm_client import (  # noqa: E402
    LlmRequestError,
    LlmUnavailableError,
    post_chat,
    post_json,
)


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _http_error(code: int, detail: str = "err", headers=None) -> HTTPError:
    return HTTPError(
        url="https://api.openai.com/v1/chat/completions",
        code=code,
        msg="error",
        hdrs=headers,
        fp=io.BytesIO(detail.encode("utf-8")),
    )


def _chat_payload(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


class _SequenceOpener:
    """An opener that raises or returns each queued item in order."""

    def __init__(self, *items):
        self.items = list(items)
        self.calls = 0

    def __call__(self, request, timeout=None):
        self.calls += 1
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(item)


class TestRetryPolicy(unittest.TestCase):
    def test_429_then_success_is_retried(self):
        opener = _SequenceOpener(_http_error(429), _chat_payload("SELECT 1"))
        with patch.object(llm_client.time, "sleep") as mock_sleep:
            content = post_chat(
                messages=[{"role": "user", "content": "q"}],
                model="gpt-4o-mini",
                api_key="k",
                max_tokens=10,
                temperature=0,
                opener=opener,
            )
        self.assertEqual(content, "SELECT 1")
        self.assertEqual(opener.calls, 2)
        mock_sleep.assert_called_once()

    def test_500_then_success_is_retried(self):
        opener = _SequenceOpener(_http_error(500), _chat_payload("ok"))
        with patch.object(llm_client.time, "sleep"):
            content = post_chat(
                messages=[{"role": "user", "content": "q"}],
                model="gpt-4o-mini",
                api_key="k",
                max_tokens=10,
                temperature=0,
                opener=opener,
            )
        self.assertEqual(content, "ok")
        self.assertEqual(opener.calls, 2)

    def test_401_is_never_retried(self):
        opener = _SequenceOpener(_http_error(401, "bad key"))
        with patch.object(llm_client.time, "sleep") as mock_sleep:
            with self.assertRaises(LlmRequestError) as caught:
                post_json(
                    llm_client.OPENAI_CHAT_URL,
                    {"model": "m"},
                    api_key="k",
                    opener=opener,
                )
        self.assertEqual(opener.calls, 1)
        mock_sleep.assert_not_called()
        self.assertIn("HTTP 401", str(caught.exception))
        self.assertIn("bad key", str(caught.exception))

    def test_retries_are_bounded(self):
        opener = _SequenceOpener(
            _http_error(500), _http_error(500), _http_error(500), _http_error(500)
        )
        with patch.object(llm_client.time, "sleep"):
            with self.assertRaises(LlmRequestError):
                post_json(
                    llm_client.OPENAI_CHAT_URL,
                    {"model": "m"},
                    api_key="k",
                    opener=opener,
                )
        self.assertEqual(opener.calls, llm_client.MAX_RETRIES + 1)

    def test_timeout_is_not_retried_and_maps_to_unavailable(self):
        opener = _SequenceOpener(TimeoutError("timed out"))
        with patch.object(llm_client.time, "sleep") as mock_sleep:
            with self.assertRaises(LlmUnavailableError):
                post_json(
                    llm_client.OPENAI_CHAT_URL,
                    {"model": "m"},
                    api_key="k",
                    opener=opener,
                )
        self.assertEqual(opener.calls, 1)
        mock_sleep.assert_not_called()

    def test_dns_failure_maps_to_unavailable_without_retry(self):
        opener = _SequenceOpener(URLError(OSError("getaddrinfo failed")))
        with patch.object(llm_client.time, "sleep") as mock_sleep:
            with self.assertRaises(LlmUnavailableError):
                post_json(
                    llm_client.OPENAI_CHAT_URL,
                    {"model": "m"},
                    api_key="k",
                    opener=opener,
                )
        self.assertEqual(opener.calls, 1)
        mock_sleep.assert_not_called()

    def test_connection_refused_is_retried(self):
        opener = _SequenceOpener(
            URLError(ConnectionRefusedError()), _chat_payload("ok")
        )
        with patch.object(llm_client.time, "sleep"):
            content = post_chat(
                messages=[{"role": "user", "content": "q"}],
                model="gpt-4o-mini",
                api_key="k",
                max_tokens=10,
                temperature=0,
                opener=opener,
            )
        self.assertEqual(content, "ok")
        self.assertEqual(opener.calls, 2)

    def test_retry_after_header_is_honoured_but_capped(self):
        headers = {"Retry-After": "3"}
        exc = _http_error(429, headers=headers)
        delay = llm_client._retry_delay(0, exc)
        self.assertEqual(delay, 3.0)
        exc_large = _http_error(429, headers={"Retry-After": "600"})
        self.assertEqual(
            llm_client._retry_delay(0, exc_large),
            llm_client._MAX_RETRY_AFTER_SECONDS,
        )

    def test_malformed_response_shape_raises_request_error(self):
        opener = _SequenceOpener({"unexpected": True})
        with self.assertRaises(LlmRequestError):
            post_chat(
                messages=[{"role": "user", "content": "q"}],
                model="gpt-4o-mini",
                api_key="k",
                max_tokens=10,
                temperature=0,
                opener=opener,
            )


class TestGenerationOptions(unittest.TestCase):
    def test_completion_token_models(self):
        body = {"model": "gpt-5.5", "messages": []}
        llm_client.add_generation_options(body, "gpt-5.5", max_tokens=99, temperature=0)
        self.assertEqual(body["max_completion_tokens"], 99)
        self.assertNotIn("temperature", body)

    def test_legacy_models(self):
        body = {"model": "gpt-4o-mini", "messages": []}
        llm_client.add_generation_options(
            body, "gpt-4o-mini", max_tokens=99, temperature=0.5
        )
        self.assertEqual(body["max_tokens"], 99)
        self.assertEqual(body["temperature"], 0.5)


class TestUnavailableSkipsSecondCall(unittest.TestCase):
    """The handler must not attempt reasoning when SQL generation proved the
    endpoint unreachable — the double-30 s-timeout regression."""

    def test_sql_agent_maps_unavailable(self):
        from wiser.agents.sql_ai_agent import (
            SqlAgentUnavailableError,
            _call_chat_llm,
        )

        def dead_opener(request, timeout=None):
            raise TimeoutError("timed out")

        with patch("wiser.agents.sql_ai_agent.urlopen", dead_opener):
            with self.assertRaises(SqlAgentUnavailableError):
                _call_chat_llm(
                    messages=[{"role": "user", "content": "q"}],
                    model="gpt-4o-mini",
                    api_key="k",
                )


if __name__ == "__main__":
    unittest.main()
