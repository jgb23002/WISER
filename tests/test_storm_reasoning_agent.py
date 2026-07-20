from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.agents.storm_reasoning_agent import (  # noqa: E402
    StormReasoningAgent,
    _add_generation_options,
    build_reasoning_prompt,
    reason_locally,
)


class TestStormReasoningAgent(unittest.TestCase):
    def test_prompt_contains_question_sql_and_rows(self):
        prompt = build_reasoning_prompt(
            "Which county was affected most?",
            sql="SELECT county, SUM(outage) AS total_outage FROM WeatherData GROUP BY county;",
            rows=[
                {"county": "Fairfield", "total_outage": 100},
                {"county": "Hartford", "total_outage": 50},
            ],
        )

        self.assertIn("Which county was affected most?", prompt)
        self.assertIn("Validated SQL", prompt)
        self.assertIn("Fairfield", prompt)
        self.assertIn("total_outage", prompt)

    def test_local_reasoning_ranks_county_by_outage(self):
        answer = reason_locally(
            "Which county was affected the most overall?",
            [
                {"county": "Fairfield", "total_outage": 100},
                {"county": "Hartford", "total_outage": 50},
            ],
        )

        self.assertIn("Fairfield ranks highest", answer)
        self.assertIn("gap of 50", answer)

    def test_local_reasoning_handles_lower_wind_higher_outage(self):
        answer = reason_locally(
            "Why did Hartford have more outages despite lower wind?",
            [
                {
                    "county": "Hartford",
                    "outage": 500,
                    "peakWSPD10": 9.0,
                    "ohLine": 2000,
                },
                {
                    "county": "New Haven",
                    "outage": 300,
                    "peakWSPD10": 10.0,
                    "ohLine": 1000,
                },
            ],
        )

        self.assertIn("non-linear outage relationship", answer)
        self.assertIn("Exposure may matter", answer)

    def test_local_reasoning_does_not_invent_population(self):
        answer = reason_locally(
            "Normalize outage by population.",
            [
                {
                    "county": "Fairfield",
                    "total_outage": 100,
                    "outage_per_million_ohline": 10.5,
                }
            ],
        )

        self.assertIn("Population is not present", answer)

    def test_local_reasoning_compares_wind_and_outage_rankings(self):
        answer = reason_locally(
            "Which county had the highest max wind, and did it also have the highest outages?",
            [
                {
                    "county": "Windham",
                    "max_peak_wind": 13.1,
                    "total_outage": 100,
                },
                {
                    "county": "Fairfield",
                    "max_peak_wind": 11.6,
                    "total_outage": 500,
                },
            ],
        )

        self.assertIn("Windham ranks highest by max_peak_wind", answer)
        self.assertIn("Wind and outage rankings diverge", answer)

    def test_run_can_force_local_mode(self):
        result = StormReasoningAgent().run(
            "Which county was affected most?",
            sql="SELECT county, SUM(outage) AS total_outage FROM WeatherData GROUP BY county;",
            rows=[{"county": "Fairfield", "total_outage": 100}],
            use_openai=False,
        )

        self.assertEqual(result.mode, "local analytical fallback")
        self.assertIn("Fairfield", result.answer)
        self.assertIn("SQL result rows", result.prompt)

    def test_gpt5_reasoning_request_uses_max_completion_tokens(self):
        body = {"model": "gpt-5.5", "messages": []}

        _add_generation_options(body, "gpt-5.5", max_tokens=700, temperature=0.2)

        self.assertEqual(body["max_completion_tokens"], 700)
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("temperature", body)

    def test_legacy_reasoning_request_keeps_temperature(self):
        body = {"model": "gpt-4o-mini", "messages": []}

        _add_generation_options(body, "gpt-4o-mini", max_tokens=700, temperature=0.2)

        self.assertEqual(body["max_tokens"], 700)
        self.assertEqual(body["temperature"], 0.2)
        self.assertNotIn("max_completion_tokens", body)


if __name__ == "__main__":
    unittest.main()
