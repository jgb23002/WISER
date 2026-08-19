import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SRC_DIR = Path(__file__).parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.openai_summarizer import (  # noqa: E402
    OpenAISummarizationError,
    load_dotenv,
    openai_config,
    summarize_storm_impact_with_openai,
)
from wiser.rag import RagResult  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class TestOpenAISummarizer(unittest.TestCase):
    def _sample_results(self):
        return [
            RagResult(
                source_path="https://example.test/report.pdf",
                title="Storm Report",
                chunk_index=0,
                score=0.9,
                content="Damaging winds and flooding caused outages.",
            )
        ]

    def test_load_dotenv_reads_key_values_without_overriding_existing_env(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "# local settings",
                        "OPENAI_API_KEY=from-file",
                        'WISER_OPENAI_MODEL="gpt-test"',
                        "INVALID LINE",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {"OPENAI_API_KEY": "from-shell"},
                clear=True,
            ):
                loaded = load_dotenv(env_path)
                config = openai_config()

                self.assertEqual(loaded, 1)
                self.assertEqual(config.model, "gpt-test")
                self.assertTrue(config.api_key_present)
                self.assertEqual(os.environ["OPENAI_API_KEY"], "from-shell")

    def test_summarize_storm_impact_with_openai_builds_responses_request(self):
        captured = {}

        def fake_opener(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["headers"] = dict(request.header_items())
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse(
                {
                    "output_text": (
                        "- Damaging winds and flooding were the main reported "
                        "impact signals. [1]"
                    )
                }
            )

        result = summarize_storm_impact_with_openai(
            "impact of the new storm",
            self._sample_results(),
            api_key="test-key",
            model="gpt-5.5",
            opener=fake_opener,
        )

        self.assertIn("Damaging winds", result)
        self.assertEqual(captured["url"], "https://api.openai.com/v1/responses")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(captured["body"]["model"], "gpt-5.5")
        self.assertEqual(captured["body"]["reasoning"], {"effort": "low"})
        self.assertEqual(captured["body"]["input"][0]["role"], "user")
        self.assertIn("Sources:", captured["body"]["input"][0]["content"])

    def test_summarize_storm_impact_reads_nested_output_message_text(self):
        def fake_opener(request, timeout):
            return FakeResponse(
                {
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "- Flooding and outages were the main impacts. [1]",
                                }
                            ],
                        }
                    ]
                }
            )

        result = summarize_storm_impact_with_openai(
            "impact",
            self._sample_results(),
            api_key="test-key",
            model="gpt-test",
            opener=fake_opener,
        )

        self.assertIn("Flooding and outages", result)

    def test_summarize_storm_impact_reads_nested_text_value(self):
        def fake_opener(request, timeout):
            return FakeResponse(
                {
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": {
                                        "value": (
                                            "- Wind damage was reported alongside "
                                            "flooding. [1]"
                                        )
                                    },
                                }
                            ],
                        }
                    ]
                }
            )

        result = summarize_storm_impact_with_openai(
            "impact",
            self._sample_results(),
            api_key="test-key",
            model="gpt-test",
            opener=fake_opener,
        )

        self.assertIn("Wind damage", result)

    def test_summarize_storm_impact_reports_response_shape_without_text(self):
        def fake_opener(request, timeout):
            return FakeResponse(
                {
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [{"type": "reasoning", "content": []}],
                }
            )

        with self.assertRaisesRegex(
            OpenAISummarizationError,
            "status=incomplete, reason=max_output_tokens, output_types=reasoning",
        ):
            summarize_storm_impact_with_openai(
                "impact",
                self._sample_results(),
                api_key="test-key",
                model="gpt-test",
                opener=fake_opener,
            )

    def test_summarize_storm_impact_requires_api_key(self):
        # summarize_storm_impact_with_openai() calls load_dotenv() itself, so
        # clearing os.environ is not enough: a developer .env holding a real
        # OPENAI_API_KEY would be read straight back in, and the call would
        # attempt a live API request instead of raising. Stub the loader so
        # this asserts what it means to -- "no key configured" -- regardless
        # of what is on disk.
        with patch.dict("os.environ", {}, clear=True), patch(
            "wiser.openai_summarizer.load_dotenv", return_value=0
        ):
            with self.assertRaises(OpenAISummarizationError):
                summarize_storm_impact_with_openai(
                    "impact",
                    self._sample_results(),
                )


if __name__ == "__main__":
    unittest.main()
