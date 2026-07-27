"""Characterization tests for reason_locally, pinned before the divergence
extraction in step 3.

These assert the EXACT current output of the local analytical fallback across
every branch it has. They were captured from the implementation as it stood at
commit 10657ce, before `normalized_ranking_divergence` was factored out.

Their whole purpose is to prove the extraction changed no behaviour. If one of
these fails after a refactor, the refactor was not behaviour-preserving —
fix the refactor, do not re-baseline the expected string. Changing an expected
value here is only correct when the wording is being deliberately changed, and
that should be an obvious, separate, intentional commit.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.agents.storm_reasoning_agent import reason_locally  # noqa: E402

_CAVEAT = (
    "- Caveat: this explanation uses only returned database fields; causal claims"
    " need additional evidence such as population, vegetation, asset condition,"
    " and restoration logs."
)

# ---------------------------------------------------------------------------
# Fixtures: (query, rows, expected output)
# ---------------------------------------------------------------------------

_WHY_FULL_ROWS = [
    {
        "county": "Hartford",
        "stormCode": 2020080406,
        "outage": 4792,
        "peakWSPD10": 9.35,
        "sumAFWA_TOTPRECIP": 25.88,
        "wgt5": 18,
        "wgt9": 6,
        "wgt13": 1,
        "wgt18": 0,
        "ohLine": 5869550,
        "outage_per_million_ohline": 816.4,
    },
    {
        "county": "New Haven",
        "stormCode": 2020080406,
        "outage": 2958,
        "peakWSPD10": 9.95,
        "sumAFWA_TOTPRECIP": 31.08,
        "wgt5": 16,
        "wgt9": 5,
        "wgt13": 2,
        "wgt18": 0,
        "ohLine": 3233197,
        "outage_per_million_ohline": 915.0,
    },
]

_WHY_NO_NORM_ROWS = [
    {"county": "Hartford", "outage": 4792, "peakWSPD10": 9.35},
    {"county": "New Haven", "outage": 2958, "peakWSPD10": 9.95},
]

_NORMALIZE_ROWS = [
    {
        "county": "Fairfield",
        "total_outage": 43475,
        "ohLine": 4887830,
        "outage_per_million_ohline": 8894.5,
    },
    {
        "county": "Hartford",
        "total_outage": 39000,
        "ohLine": 5869550,
        "outage_per_million_ohline": 6644.3,
    },
]

_TOTALS_ROWS = [
    {"county": "Fairfield", "total_outage": 43475},
    {"county": "Hartford", "total_outage": 39000},
]

_WIND_MAX_ROWS = [
    {"county": "Fairfield", "max_peak_wind": 13.1, "total_outage": 100},
    {"county": "Hartford", "max_peak_wind": 12.0, "total_outage": 900},
]

_NULL_METRIC_ROWS = [
    {"county": "Fairfield", "total_outage": 43475},
    {"county": "Hartford", "total_outage": None},
]


class TestReasonLocallyCharacterization(unittest.TestCase):
    maxDiff = None

    def test_why_query_with_exposure_and_normalized_columns(self):
        """The full divergence branch: inversion pair, ohLine exposure, and
        the raw-vs-normalized leader flip."""
        expected = "\n".join(
            [
                "- Strongest measured signal: Hartford / storm 2020080406 ranks highest"
                " by outage with 4,792.",
                "- Next highest is New Haven / storm 2020080406 at 2,958, a gap of 1,834.",
                "- The rows support a non-linear outage relationship: Hartford / storm"
                " 2020080406 has 4,792 outages with 9.35 m/s wind, while New Haven /"
                " storm 2020080406 has 2,958 outages despite 9.95 m/s wind. Exposure"
                " may matter: ohLine is 5,869,550 versus 3,233,197. Normalized by"
                " ohLine, New Haven / storm 2020080406 is higher at 915, so the"
                " exposure-adjusted interpretation differs from the raw outage ranking.",
                _CAVEAT,
            ]
        )
        self.assertEqual(
            reason_locally(
                "for storm 2020080406, why did Hartford have more outages than"
                " New Haven despite lower wind?",
                _WHY_FULL_ROWS,
            ),
            expected,
        )

    def test_why_query_without_exposure_or_normalized_columns(self):
        expected = "\n".join(
            [
                "- Strongest measured signal: Hartford ranks highest by outage with 4,792.",
                "- Next highest is New Haven at 2,958, a gap of 1,834.",
                "- The rows support a non-linear outage relationship: Hartford has 4,792"
                " outages with 9.35 m/s wind, while New Haven has 2,958 outages despite"
                " 9.95 m/s wind.",
                _CAVEAT,
            ]
        )
        self.assertEqual(
            reason_locally(
                "why did Hartford have more outages than New Haven despite lower wind?",
                _WHY_NO_NORM_ROWS,
            ),
            expected,
        )

    def test_normalize_query_uses_the_normalized_leader_branch(self):
        expected = "\n".join(
            [
                "- Strongest measured signal: Fairfield ranks highest by"
                " outage_per_million_ohline with 8,894.50.",
                "- Next highest is Hartford at 6,644.30, a gap of 2,250.20.",
                "- Normalized view: Fairfield is highest by outage_per_million_ohline at"
                " 8,894.50, which can change the interpretation from raw outage totals.",
                _CAVEAT,
            ]
        )
        self.assertEqual(
            reason_locally(
                "normalize county outages by overhead line exposure", _NORMALIZE_ROWS
            ),
            expected,
        )

    def test_population_query_states_the_schema_gap(self):
        expected = "\n".join(
            [
                "- Strongest measured signal: Fairfield ranks highest by total_outage"
                " with 43,475.",
                "- Next highest is Hartford at 39,000, a gap of 4,475.",
                "- Population is not present in the current WISER schema, so a"
                " per-capita answer would need an added population table or input values.",
                _CAVEAT,
            ]
        )
        self.assertEqual(
            reason_locally("normalize outages by population", _TOTALS_ROWS), expected
        )

    def test_wind_query_reports_the_outage_ranking_divergence(self):
        expected = "\n".join(
            [
                "- Strongest measured signal: Fairfield ranks highest by max_peak_wind"
                " with 13.10.",
                "- Next highest is Hartford at 12, a gap of 1.10.",
                "- Wind and outage rankings diverge: Hartford leads by total_outage with"
                " 900, not Fairfield.",
                _CAVEAT,
            ]
        )
        self.assertEqual(
            reason_locally("which county had the highest maximum wind?", _WIND_MAX_ROWS),
            expected,
        )

    def test_plain_ranking_query(self):
        expected = "\n".join(
            [
                "- Strongest measured signal: Fairfield ranks highest by total_outage"
                " with 43,475.",
                "- Next highest is Hartford at 39,000, a gap of 4,475.",
                _CAVEAT,
            ]
        )
        self.assertEqual(
            reason_locally("top counties by outage", _TOTALS_ROWS), expected
        )

    def test_null_metric_omits_the_gap(self):
        expected = "\n".join(
            [
                "- Strongest measured signal: Fairfield ranks highest by total_outage"
                " with 43,475.",
                "- Next highest is Hartford at not reported.",
                _CAVEAT,
            ]
        )
        self.assertEqual(
            reason_locally("top counties by outage", _NULL_METRIC_ROWS), expected
        )

    def test_empty_rows(self):
        self.assertEqual(
            reason_locally("top counties by outage", []),
            "No result rows were returned, so there is not enough evidence to analyze.",
        )


if __name__ == "__main__":
    unittest.main()
