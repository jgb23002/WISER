from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.agents.storm_reasoning_agent import (  # noqa: E402
    _SYSTEM_PROMPT,
    StormReasoningAgent,
    _add_generation_options,
    build_reasoning_prompt,
    normalized_ranking_divergence,
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


class TestSystemPromptStatesUnits(unittest.TestCase):
    """A live run mislabelled 30.67 mm as "30.67 inches" — a 26x error, printed
    directly under a report that said 1.21 in. The prompt named units for
    peakWSPD10 but not for precipitation."""

    def test_every_numeric_column_has_its_unit_stated(self):
        for column, unit in (
            ("peakWSPD10", "METRES PER SECOND"),
            ("sumAFWA_TOTPRECIP", "MILLIMETRES"),
        ):
            with self.subTest(column=column):
                self.assertIn(column, _SYSTEM_PROMPT)
                self.assertIn(unit, _SYSTEM_PROMPT)

    def test_prompt_warns_against_cross_unit_labelling(self):
        self.assertIn("never attach an imperial unit", _SYSTEM_PROMPT.lower())

    def test_prompt_names_the_imperial_companion_columns(self):
        self.assertIn("peak_wind_mph", _SYSTEM_PROMPT)
        self.assertIn("precip_in", _SYSTEM_PROMPT)

    def test_prompt_requires_superlatives_to_be_checked(self):
        lowered = _SYSTEM_PROMPT.lower()
        self.assertIn("superlative", lowered)
        self.assertIn("every row", lowered)

    def test_wgt_columns_are_described_as_gusts(self):
        """The schema evidence says wgt* cannot be hours above peakWSPD10."""
        self.assertIn("GUST", _SYSTEM_PROMPT)
        self.assertIn("not hours above peakWSPD10", _SYSTEM_PROMPT)


class TestNormalizedRankingDivergence(unittest.TestCase):
    """The shared raw-vs-normalized leader logic.

    Extracted from reason_locally so the storm comparison report can reuse it
    instead of duplicating it. tests/test_reasoning_characterization.py proves
    the extraction changed no reason_locally output.
    """

    def test_detects_a_flip_when_normalization_reorders_the_leaders(self):
        rows = [
            {"county": "Hartford", "outage": 4792, "outage_per_million_ohline": 816.4},
            {"county": "New Haven", "outage": 2958, "outage_per_million_ohline": 915.0},
        ]
        divergence = normalized_ranking_divergence(rows)

        self.assertIsNotNone(divergence)
        self.assertEqual(divergence.raw_metric, "outage")
        self.assertEqual(divergence.normalized_metric, "outage_per_million_ohline")
        self.assertEqual(divergence.raw_leader["county"], "Hartford")
        self.assertEqual(divergence.normalized_leader["county"], "New Haven")
        self.assertTrue(divergence.flipped)

    def test_no_flip_when_the_same_row_leads_both(self):
        rows = [
            {"county": "Fairfield", "outage": 600, "outage_per_million_ohline": 150.0},
            {"county": "Hartford", "outage": 400, "outage_per_million_ohline": 50.0},
        ]
        divergence = normalized_ranking_divergence(rows)

        self.assertIsNotNone(divergence)
        self.assertEqual(divergence.raw_leader["county"], "Fairfield")
        self.assertEqual(divergence.normalized_leader["county"], "Fairfield")
        self.assertFalse(divergence.flipped)

    def test_prefers_total_outage_over_outage_as_the_raw_metric(self):
        rows = [
            {
                "county": "Fairfield",
                "total_outage": 43475,
                "outage": 1,
                "outage_per_million_ohline": 8894.5,
            },
        ]
        self.assertEqual(normalized_ranking_divergence(rows).raw_metric, "total_outage")

    def test_returns_none_without_a_normalized_column(self):
        rows = [{"county": "Fairfield", "total_outage": 43475}]
        self.assertIsNone(normalized_ranking_divergence(rows))

    def test_returns_none_for_empty_rows(self):
        self.assertIsNone(normalized_ranking_divergence([]))

    def test_normalized_leader_without_a_raw_metric_does_not_claim_a_flip(self):
        rows = [
            {"county": "Fairfield", "outage_per_million_ohline": 8894.5},
            {"county": "Hartford", "outage_per_million_ohline": 6644.3},
        ]
        divergence = normalized_ranking_divergence(rows)

        self.assertEqual(divergence.normalized_leader["county"], "Fairfield")
        self.assertIsNone(divergence.raw_leader)
        self.assertFalse(divergence.flipped)

    def test_require_all_rejects_a_partially_measured_pair(self):
        """Naming one of two counties "higher when normalized" is not
        supportable when only one of them was measured."""
        rows = [
            {"county": "Hartford", "outage": 4792, "outage_per_million_ohline": 816.4},
            {"county": "New Haven", "outage": 2958},
        ]
        self.assertIsNone(normalized_ranking_divergence(rows, require_all=True))
        self.assertIsNotNone(normalized_ranking_divergence(rows))

    def test_require_all_rejects_a_null_normalized_value(self):
        rows = [
            {"county": "Hartford", "outage": 4792, "outage_per_million_ohline": 816.4},
            {"county": "New Haven", "outage": 2958, "outage_per_million_ohline": None},
        ]
        self.assertIsNone(normalized_ranking_divergence(rows, require_all=True))

    def test_ties_resolve_to_the_first_row(self):
        rows = [
            {"county": "Fairfield", "outage": 100, "outage_per_million_ohline": 50.0},
            {"county": "Hartford", "outage": 100, "outage_per_million_ohline": 50.0},
        ]
        divergence = normalized_ranking_divergence(rows)
        self.assertEqual(divergence.normalized_leader["county"], "Fairfield")
        self.assertFalse(divergence.flipped)

    def test_custom_metric_names_are_honoured(self):
        rows = [
            {"county": "Fairfield", "peak": 10, "per_mile": 1.0},
            {"county": "Hartford", "peak": 5, "per_mile": 9.0},
        ]
        divergence = normalized_ranking_divergence(
            rows, raw_metrics=("peak",), normalized_metrics=("per_mile",)
        )
        self.assertEqual(divergence.raw_leader["county"], "Fairfield")
        self.assertEqual(divergence.normalized_leader["county"], "Hartford")
        self.assertTrue(divergence.flipped)


if __name__ == "__main__":
    unittest.main()
