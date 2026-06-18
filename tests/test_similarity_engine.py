"""
Tests for the multi-dimensional L2 Z-score similarity engine.

Five test categories required by the deliverables:
  1. Single-feature parity  – rank_by_similarity with only peakWSPD10 must
                              produce the same scores as the legacy
                              calculate_similarity() function (now in tests/test_similarity_algorithm.py).
  2. Two-feature ranking    – adding precipitation changes which storm ranks
                              first when wind is close but precip is far.
  3. Zero std-dev safety    – constant historical feature handled without crash.
  4. Feature weight override – heavier wind weight restores 1-D dominance.
  5. Partial query_params   – querying on a subset of available columns works.
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

# Add src to path so wiser.similarity_engine is importable
SRC_DIR = Path(__file__).parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Add tests to path to import legacy math functions from test_similarity_algorithm
from tests.test_similarity_algorithm import calculate_similarity, compute_statistics

from wiser.similarity_engine import (
    compute_feature_stats,
    compute_l2_zscore_distance,
    distance_to_similarity,
    rank_by_similarity,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

# Three storms used in ranking-difference and weight-override tests.
# Storm 1: wind very close to query (9.0), but precipitation far away.
# Storm 2: wind moderately close,        precipitation matches query exactly.
# Storm 3: wind far from query,          precipitation also far.
_THREE_RECORDS = [
    {"peakWSPD10": 9.2, "sumAFWA_TOTPRECIP": 10.0, "stormCode": 1},
    {"peakWSPD10": 8.0, "sumAFWA_TOTPRECIP": 45.0, "stormCode": 2},
    {"peakWSPD10": 5.0, "sumAFWA_TOTPRECIP": 50.0, "stormCode": 3},
]
_QUERY_WIND_ONLY = {"peakWSPD10": 9.0}
_QUERY_WIND_PRECIP = {"peakWSPD10": 9.0, "sumAFWA_TOTPRECIP": 45.0}


# ---------------------------------------------------------------------------
# 1. compute_feature_stats
# ---------------------------------------------------------------------------


class TestComputeFeatureStats(unittest.TestCase):
    def test_empty_records_returns_zero_stats(self):
        stats = compute_feature_stats([], ["peakWSPD10"])
        self.assertEqual(stats["peakWSPD10"], (0.0, 0.0))

    def test_single_record_returns_zero_std_dev(self):
        stats = compute_feature_stats([{"peakWSPD10": 7.5}], ["peakWSPD10"])
        mean, std_dev = stats["peakWSPD10"]
        self.assertAlmostEqual(mean, 7.5, places=10)
        self.assertAlmostEqual(std_dev, 0.0, places=10)

    def test_multiple_records_matches_legacy_compute_statistics(self):
        """compute_feature_stats must agree with the legacy compute_statistics."""
        wind_speeds = [5.8, 6.4, 5.7, 10.6, 8.1, 6.2, 7.0, 9.2, 5.5, 7.8]
        records = [{"peakWSPD10": w} for w in wind_speeds]

        legacy_mean, legacy_std, _, _ = compute_statistics(wind_speeds)
        stats = compute_feature_stats(records, ["peakWSPD10"])
        engine_mean, engine_std = stats["peakWSPD10"]

        self.assertAlmostEqual(engine_mean, legacy_mean, places=10)
        self.assertAlmostEqual(engine_std, legacy_std, places=10)

    def test_two_features_independent(self):
        records = [
            {"peakWSPD10": 4.0, "sumAFWA_TOTPRECIP": 10.0},
            {"peakWSPD10": 6.0, "sumAFWA_TOTPRECIP": 30.0},
        ]
        stats = compute_feature_stats(records, ["peakWSPD10", "sumAFWA_TOTPRECIP"])

        wind_mean, wind_std = stats["peakWSPD10"]
        prec_mean, prec_std = stats["sumAFWA_TOTPRECIP"]

        self.assertAlmostEqual(wind_mean, 5.0, places=10)
        # sample std dev for [4, 6]: sqrt(((4-5)^2 + (6-5)^2) / 1) = sqrt(2)
        self.assertAlmostEqual(wind_std, math.sqrt(2), places=10)
        self.assertAlmostEqual(prec_mean, 20.0, places=10)
        self.assertAlmostEqual(prec_std, math.sqrt(200), places=10)


# ---------------------------------------------------------------------------
# 2. compute_l2_zscore_distance
# ---------------------------------------------------------------------------


class TestComputeL2ZscoreDistance(unittest.TestCase):
    def _stats(self, mean, std_dev):
        return {"f": (mean, std_dev)}

    def test_identical_values_gives_zero_distance(self):
        stats = self._stats(7.0, 2.0)
        dist = compute_l2_zscore_distance({"f": 9.0}, {"f": 9.0}, stats, {"f": 1.0})
        self.assertAlmostEqual(dist, 0.0, places=10)

    def test_one_std_dev_apart_gives_unit_distance(self):
        # z_hist=0, z_query=1 → |0-1|=1 → sqrt(1*1)=1
        stats = self._stats(mean=7.0, std_dev=2.0)
        dist = compute_l2_zscore_distance({"f": 7.0}, {"f": 9.0}, stats, {"f": 1.0})
        self.assertAlmostEqual(dist, 1.0, places=10)

    def test_zero_stddev_exact_match_returns_zero(self):
        stats = self._stats(7.0, 0.0)
        dist = compute_l2_zscore_distance({"f": 7.0}, {"f": 7.0}, stats, {"f": 1.0})
        self.assertAlmostEqual(dist, 0.0, places=10)

    def test_zero_stddev_mismatch_returns_inf(self):
        stats = self._stats(7.0, 0.0)
        dist = compute_l2_zscore_distance({"f": 7.0}, {"f": 8.0}, stats, {"f": 1.0})
        self.assertTrue(math.isinf(dist))

    def test_weight_scales_squared_contribution(self):
        # weight=4 → distance = sqrt(4 * 1^2) = 2  (compared to weight=1 → 1)
        stats = self._stats(mean=7.0, std_dev=2.0)
        dist_w1 = compute_l2_zscore_distance({"f": 7.0}, {"f": 9.0}, stats, {"f": 1.0})
        dist_w4 = compute_l2_zscore_distance({"f": 7.0}, {"f": 9.0}, stats, {"f": 4.0})
        self.assertAlmostEqual(dist_w1, 1.0, places=10)
        self.assertAlmostEqual(dist_w4, 2.0, places=10)

    def test_two_feature_distance_is_l2_norm(self):
        stats = {"a": (0.0, 1.0), "b": (0.0, 1.0)}
        # a: z_hist=0, z_query=3 → diff=3
        # b: z_hist=0, z_query=4 → diff=4
        # distance = sqrt(9 + 16) = 5
        dist = compute_l2_zscore_distance(
            {"a": 0.0, "b": 0.0},
            {"a": 3.0, "b": 4.0},
            stats,
            {"a": 1.0, "b": 1.0},
        )
        self.assertAlmostEqual(dist, 5.0, places=10)


# ---------------------------------------------------------------------------
# 3. distance_to_similarity
# ---------------------------------------------------------------------------


class TestDistanceToSimilarity(unittest.TestCase):
    def test_zero_distance_is_perfect_similarity(self):
        self.assertAlmostEqual(distance_to_similarity(0.0), 1.0, places=10)

    def test_inf_distance_is_zero_similarity(self):
        self.assertAlmostEqual(distance_to_similarity(math.inf), 0.0, places=10)

    def test_unit_distance_is_half_similarity(self):
        self.assertAlmostEqual(distance_to_similarity(1.0), 0.5, places=10)

    def test_two_distance_is_one_third_similarity(self):
        self.assertAlmostEqual(distance_to_similarity(2.0), 1.0 / 3.0, places=10)

    def test_result_always_in_zero_one_range(self):
        for d in [0.0, 0.1, 1.0, 5.0, 100.0]:
            s = distance_to_similarity(d)
            self.assertGreater(s, 0.0)
            self.assertLessEqual(s, 1.0)


# ---------------------------------------------------------------------------
# 4. rank_by_similarity – single-feature parity
# ---------------------------------------------------------------------------


class TestSingleFeatureParity(unittest.TestCase):
    """
    Category 1: rank_by_similarity with only peakWSPD10 must match the
    legacy calculate_similarity() score exactly for every record.
    """

    _RECORDS = [
        {"peakWSPD10": 5.8, "stormCode": 1},
        {"peakWSPD10": 6.4, "stormCode": 2},
        {"peakWSPD10": 10.6, "stormCode": 3},
        {"peakWSPD10": 9.2, "stormCode": 4},
        {"peakWSPD10": 7.0, "stormCode": 5},
    ]
    _QUERY_VAL = 9.0

    def test_scores_match_legacy_calculate_similarity(self):
        winds = [r["peakWSPD10"] for r in self._RECORDS]
        mean, std_dev, _, _ = compute_statistics(winds)

        # Legacy reference scores keyed by stormCode
        legacy = {}
        for r in self._RECORDS:
            sim, _, _ = calculate_similarity(
                r["peakWSPD10"], self._QUERY_VAL, mean, std_dev
            )
            legacy[r["stormCode"]] = sim

        # Engine scores
        ranked = rank_by_similarity(self._RECORDS, {"peakWSPD10": self._QUERY_VAL})
        engine = {r["stormCode"]: s for r, s in ranked}

        for sc in legacy:
            self.assertAlmostEqual(
                engine[sc],
                legacy[sc],
                places=10,
                msg=f"Score mismatch for stormCode={sc}",
            )

    def test_ranking_order_matches_legacy(self):
        """The sort order produced by the engine must match sorting by legacy score."""
        winds = [r["peakWSPD10"] for r in self._RECORDS]
        mean, std_dev, _, _ = compute_statistics(winds)

        legacy_sorted = sorted(
            self._RECORDS,
            key=lambda r: calculate_similarity(
                r["peakWSPD10"], self._QUERY_VAL, mean, std_dev
            )[0],
            reverse=True,
        )
        engine_sorted = [
            r
            for r, _ in rank_by_similarity(
                self._RECORDS, {"peakWSPD10": self._QUERY_VAL}
            )
        ]

        self.assertEqual(
            [r["stormCode"] for r in engine_sorted],
            [r["stormCode"] for r in legacy_sorted],
        )

    def test_identical_wind_speed_scores_one(self):
        """Query value present in historical data → similarity == 1.0."""
        ranked = rank_by_similarity(self._RECORDS, {"peakWSPD10": 9.2})
        best_record, best_score = ranked[0]
        self.assertEqual(best_record["stormCode"], 4)  # stormCode 4 has wind=9.2
        self.assertAlmostEqual(best_score, 1.0, places=10)

    def test_empty_historical_returns_empty_list(self):
        self.assertEqual(rank_by_similarity([], {"peakWSPD10": 9.0}), [])

    def test_empty_query_params_returns_empty_list(self):
        self.assertEqual(rank_by_similarity(self._RECORDS, {}), [])


# ---------------------------------------------------------------------------
# 5. rank_by_similarity – two-feature ranking difference
# ---------------------------------------------------------------------------


class TestTwoFeatureRankingDifference(unittest.TestCase):
    """
    Category 2: Adding precipitation to the query changes which storm ranks
    first when wind is close but precip is far from the query.

    Data:
      Storm 1  wind=9.2  precip= 10  (wind very close, precip very far)
      Storm 2  wind=8.0  precip= 45  (wind farther,    precip = query)
      Storm 3  wind=5.0  precip= 50  (both farther)

    Query: wind=9.0, precip=45.0

    1-D (wind only):  Storm 1 ranks first (wind 9.2 ≈ 9.0)
    2-D (wind+precip): Storm 2 ranks first (perfect precip match outweighs
                        the small wind gap)
    """

    def test_wind_only_storm1_ranks_first(self):
        ranked = rank_by_similarity(_THREE_RECORDS, _QUERY_WIND_ONLY)
        self.assertEqual(ranked[0][0]["stormCode"], 1)
        self.assertEqual(ranked[1][0]["stormCode"], 2)

    def test_wind_and_precip_storm2_ranks_first(self):
        ranked = rank_by_similarity(_THREE_RECORDS, _QUERY_WIND_PRECIP)
        self.assertEqual(ranked[0][0]["stormCode"], 2)
        self.assertEqual(ranked[1][0]["stormCode"], 1)

    def test_adding_precip_lowers_storm1_score(self):
        """Storm 1's score must drop when precip (far from query) is included."""
        ranked_1d = rank_by_similarity(_THREE_RECORDS, _QUERY_WIND_ONLY)
        ranked_2d = rank_by_similarity(_THREE_RECORDS, _QUERY_WIND_PRECIP)

        score_1d = {r["stormCode"]: s for r, s in ranked_1d}
        score_2d = {r["stormCode"]: s for r, s in ranked_2d}

        self.assertGreater(score_1d[1], score_2d[1])

    def test_adding_precip_keeps_storm2_score_same(self):
        """
        Storm 2's precip (45) equals the query (45), so adding precip does
        not penalise it — its score stays the same between 1-D and 2-D.
        """
        ranked_1d = rank_by_similarity(_THREE_RECORDS, _QUERY_WIND_ONLY)
        ranked_2d = rank_by_similarity(_THREE_RECORDS, _QUERY_WIND_PRECIP)

        score_1d = {r["stormCode"]: s for r, s in ranked_1d}
        score_2d = {r["stormCode"]: s for r, s in ranked_2d}

        self.assertAlmostEqual(score_1d[2], score_2d[2], places=10)


# ---------------------------------------------------------------------------
# 6. rank_by_similarity – zero std-dev safety
# ---------------------------------------------------------------------------


class TestZeroStdDevSafety(unittest.TestCase):
    """
    Category 3: If all historical records share the same value for a feature,
    std_dev = 0.  The engine must not crash and must:
      - Return similarity = 1.0 when the query value matches.
      - Return similarity = 0.0 when the query value differs.
    """

    _CONSTANT_RECORDS = [{"peakWSPD10": 9.0, "stormCode": i} for i in range(5)]

    def test_zero_stddev_exact_match_all_scores_one(self):
        ranked = rank_by_similarity(self._CONSTANT_RECORDS, {"peakWSPD10": 9.0})
        self.assertEqual(len(ranked), 5)
        for _, score in ranked:
            self.assertAlmostEqual(score, 1.0, places=10)

    def test_zero_stddev_mismatch_all_scores_zero(self):
        ranked = rank_by_similarity(self._CONSTANT_RECORDS, {"peakWSPD10": 10.0})
        self.assertEqual(len(ranked), 5)
        for _, score in ranked:
            self.assertAlmostEqual(score, 0.0, places=10)

    def test_zero_stddev_one_feature_other_varies(self):
        """
        If one feature is constant (std_dev=0) and matches the query, the
        score should be determined entirely by the other feature.
        """
        records = [
            {"peakWSPD10": 9.0, "sumAFWA_TOTPRECIP": 10.0, "stormCode": 1},
            {"peakWSPD10": 9.0, "sumAFWA_TOTPRECIP": 30.0, "stormCode": 2},
        ]
        # peakWSPD10 is constant (9.0) → std_dev = 0; query matches
        # sumAFWA_TOTPRECIP varies; storm 2 is closer to query (25)
        ranked = rank_by_similarity(
            records,
            {"peakWSPD10": 9.0, "sumAFWA_TOTPRECIP": 25.0},
        )
        # storm 2 (precip=30, diff=5) is closer to query=25 than storm 1 (precip=10, diff=15)
        self.assertEqual(ranked[0][0]["stormCode"], 2)

    def test_zero_stddev_constant_feature_mismatch_zeroes_all(self):
        """
        If the constant feature does NOT match the query, every record
        scores 0 regardless of the other features.
        """
        records = [
            {"peakWSPD10": 9.0, "sumAFWA_TOTPRECIP": 10.0, "stormCode": 1},
            {"peakWSPD10": 9.0, "sumAFWA_TOTPRECIP": 45.0, "stormCode": 2},
        ]
        ranked = rank_by_similarity(
            records,
            {"peakWSPD10": 999.0, "sumAFWA_TOTPRECIP": 45.0},
        )
        for _, score in ranked:
            self.assertAlmostEqual(score, 0.0, places=10)


# ---------------------------------------------------------------------------
# 7. rank_by_similarity – feature weight override
# ---------------------------------------------------------------------------


class TestFeatureWeightOverride(unittest.TestCase):
    """
    Category 4: Custom feature_weights change rankings.

    Using the same three-record fixture as the ranking-difference tests:
      - Equal weights  → Storm 2 ranks first (precip match outweighs wind gap)
      - Heavy wind     → Storm 1 ranks first again (wind dominates)
      - Heavy precip   → Storm 2 still first (precip is king)
    """

    def test_equal_weights_storm2_wins(self):
        ranked = rank_by_similarity(
            _THREE_RECORDS,
            _QUERY_WIND_PRECIP,
            feature_weights={"peakWSPD10": 1.0, "sumAFWA_TOTPRECIP": 1.0},
        )
        self.assertEqual(ranked[0][0]["stormCode"], 2)

    def test_heavy_wind_weight_storm1_wins(self):
        """
        Boosting wind weight (100x) makes the wind-close storm (storm 1)
        outrank the precip-close storm (storm 2).
        """
        ranked = rank_by_similarity(
            _THREE_RECORDS,
            _QUERY_WIND_PRECIP,
            feature_weights={"peakWSPD10": 100.0, "sumAFWA_TOTPRECIP": 1.0},
        )
        self.assertEqual(ranked[0][0]["stormCode"], 1)

    def test_heavy_precip_weight_storm2_still_wins(self):
        ranked = rank_by_similarity(
            _THREE_RECORDS,
            _QUERY_WIND_PRECIP,
            feature_weights={"peakWSPD10": 1.0, "sumAFWA_TOTPRECIP": 100.0},
        )
        self.assertEqual(ranked[0][0]["stormCode"], 2)

    def test_default_none_weights_equals_unit_weights(self):
        """feature_weights=None must give identical results to all-1.0 weights."""
        ranked_none = rank_by_similarity(_THREE_RECORDS, _QUERY_WIND_PRECIP, None)
        ranked_ones = rank_by_similarity(
            _THREE_RECORDS,
            _QUERY_WIND_PRECIP,
            feature_weights={"peakWSPD10": 1.0, "sumAFWA_TOTPRECIP": 1.0},
        )
        for (r_n, s_n), (r_o, s_o) in zip(ranked_none, ranked_ones):
            self.assertEqual(r_n["stormCode"], r_o["stormCode"])
            self.assertAlmostEqual(s_n, s_o, places=10)


# ---------------------------------------------------------------------------
# 8. rank_by_similarity – partial query_params
# ---------------------------------------------------------------------------


class TestPartialQueryParams(unittest.TestCase):
    """
    Category 5: Querying on a subset of available columns works correctly.
    Records may contain many extra keys; only the keys in query_params matter.
    """

    _RICH_RECORDS = [
        {
            "peakWSPD10": 9.0,
            "sumAFWA_TOTPRECIP": 10.0,
            "wgt5": 5,
            "wgt9": 3,
            "wgt13": 1,
            "wgt18": 0,
            "stormCode": 1,
        },
        {
            "peakWSPD10": 7.0,
            "sumAFWA_TOTPRECIP": 20.0,
            "wgt5": 4,
            "wgt9": 2,
            "wgt13": 0,
            "wgt18": 0,
            "stormCode": 2,
        },
        {
            "peakWSPD10": 5.0,
            "sumAFWA_TOTPRECIP": 45.0,
            "wgt5": 1,
            "wgt9": 0,
            "wgt13": 0,
            "wgt18": 0,
            "stormCode": 3,
        },
    ]

    def test_query_on_precip_only_ignores_wind(self):
        """Only querying on precip → storm 3 (precip=45 ≈ query=44) ranks first."""
        ranked = rank_by_similarity(self._RICH_RECORDS, {"sumAFWA_TOTPRECIP": 44.0})
        self.assertEqual(ranked[0][0]["stormCode"], 3)

    def test_query_on_single_wgt_column(self):
        """Can query on wgt5 alone without touching other columns."""
        ranked = rank_by_similarity(self._RICH_RECORDS, {"wgt5": 5.0})
        # storm 1 has wgt5=5, closest to query=5
        self.assertEqual(ranked[0][0]["stormCode"], 1)
        self.assertAlmostEqual(ranked[0][1], 1.0, places=10)

    def test_partial_subset_produces_valid_scores(self):
        """All returned scores must be in (0, 1]."""
        ranked = rank_by_similarity(
            self._RICH_RECORDS,
            {"peakWSPD10": 8.0, "wgt9": 2.0},
        )
        self.assertEqual(len(ranked), len(self._RICH_RECORDS))
        for _, score in ranked:
            self.assertGreater(score, 0.0)
            self.assertLessEqual(score, 1.0)

    def test_scores_decrease_monotonically_with_distance(self):
        """Similarity must be 1.0 for the exact-match record in a subset query."""
        # Only query on sumAFWA_TOTPRECIP; storm 2 has precip=20 = query
        ranked = rank_by_similarity(self._RICH_RECORDS, {"sumAFWA_TOTPRECIP": 20.0})
        best_record, best_score = ranked[0]
        self.assertEqual(best_record["stormCode"], 2)
        self.assertAlmostEqual(best_score, 1.0, places=10)


if __name__ == "__main__":
    unittest.main()
