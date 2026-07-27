"""Tests for wiser.storm_analysis.

Every test seeds its own in-memory database. Per the plan's decision on the
294-vs-312 discrepancy, NO test hardcodes a storm count, storm code, or outage
magnitude taken from data/wiser.db — assertions are on relationships (rank/
total consistency, ordering, aggregation identities) so the production dataset
can land without re-baselining.
"""

from __future__ import annotations

import ast
import sqlite3
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SRC_DIR = REPO_ROOT / "src"
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"
MODULE_PATH = SRC_DIR / "wiser" / "storm_analysis.py"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.storm_analysis import (  # noqa: E402
    METRIC_MAX_PRECIP,
    METRIC_MAX_WIND,
    METRIC_OUTAGE,
    RANKABLE_METRICS,
    StormAnalysisError,
    compare_forecast,
    compare_storms,
    date_to_storm_code_range,
    resolve_storm,
    storm_code_to_date,
    state_counties,
    storm_count,
    storm_ranking,
    storm_summary,
)

_SCHEMA_SQL = SCHEMA_PATH.read_text(encoding="utf-8")


def _make_conn(
    storms=None,
    counties=None,
    weather=None,
) -> sqlite3.Connection:
    """Seed a database. Defaults give three storms across two CT counties."""
    storms = storms if storms is not None else _DEFAULT_STORMS
    counties = counties if counties is not None else _DEFAULT_COUNTIES
    weather = weather if weather is not None else _DEFAULT_WEATHER

    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA_SQL)
    conn.executemany(
        "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(?, ?, ?)",
        storms,
    )
    conn.executemany(
        "INSERT INTO County(county, state, ohLine) VALUES(?, ?, ?)", counties
    )
    conn.executemany(
        "INSERT INTO WeatherData(stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,"
        " wgt5, wgt9, wgt13, wgt18, outage) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        weather,
    )
    conn.commit()
    return conn


# stormCode, eventDateTime, eventLength
_DEFAULT_STORMS = [
    (2020080406, "2020-08-04 06:00:00", 48),
    (2021090100, "2021-09-01 00:00:00", 48),
    (2021090112, "2021-09-01 12:00:00", 48),  # same date, different hour
]
_DEFAULT_COUNTIES = [
    ("Fairfield", "CT", 4_000_000),
    ("Hartford", "CT", 8_000_000),
]
# stormCode, county, precip, wind, wgt5, wgt9, wgt13, wgt18, outage
_DEFAULT_WEATHER = [
    (2020080406, "Fairfield", 30.0, 9.5, 20, 8, 0, 0, 600),
    (2020080406, "Hartford", 25.0, 11.0, 18, 6, 1, 0, 400),
    (2021090100, "Fairfield", 50.0, 7.0, 10, 0, 0, 0, 200),
    (2021090100, "Hartford", 45.0, 6.0, 8, 0, 0, 0, 100),
    (2021090112, "Fairfield", 10.0, 5.0, 4, 0, 0, 0, 50),
    (2021090112, "Hartford", 12.0, 5.5, 5, 0, 0, 0, 60),
]


class TestPortabilityGuard(unittest.TestCase):
    """Mechanical enforcement of the PostgreSQL rules in the plan's section 2.

    The checks run against the module's *SQL string literals*, extracted with
    ast, rather than the raw file text — otherwise prose in a docstring and
    Python constants like DEFAULT_STATE = "CT" trip assertions that are only
    ever meant to be about SQL.
    """

    @classmethod
    def setUpClass(cls):
        source = MODULE_PATH.read_text(encoding="utf-8")
        cls.source = source
        tree = ast.parse(source)
        sql_strings = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _looks_like_sql(node.value)
        ]
        self_check = "the guard found no SQL to inspect; the extractor is broken"
        assert sql_strings, self_check
        cls.sql = "\n".join(sql_strings)

    def test_no_sqlite_only_constructs_in_sql(self):
        upper = self.sql.upper()
        for banned in ("STRFTIME(", "PRAGMA", "AUTOINCREMENT", "SQLITE_MASTER"):
            with self.subTest(banned=banned):
                self.assertNotIn(
                    banned, upper, f"{banned} is SQLite-only; see the portability rules"
                )

    def test_no_round_of_a_float_in_sql(self):
        """PostgreSQL has no round(double precision, integer) overload."""
        self.assertNotIn(
            "ROUND(",
            self.sql.upper(),
            "round a float in the presentation layer, not in SQL",
        )

    def test_every_value_in_sql_is_a_bound_parameter(self):
        """No quoted literals: state, county, and storm codes all bind.

        This is what keeps `state` a parameter rather than a hardcoded 'CT'.
        """
        self.assertNotIn(
            "'",
            self.sql,
            "a quoted literal in SQL means a value was not bound as a parameter",
        )

    def test_does_not_import_sqlite3(self):
        self.assertNotIn("import sqlite3", self.source)

    def test_ranking_uses_portable_window_functions(self):
        self.assertIn("RANK() OVER", self.sql)
        self.assertIn("COUNT(*) OVER ()", self.sql)


def _looks_like_sql(text: str) -> bool:
    upper = text.upper()
    return "SELECT" in upper and "FROM" in upper


class TestStormCodeArithmetic(unittest.TestCase):
    def test_storm_code_to_date(self):
        self.assertEqual(storm_code_to_date(2020080406), "2020-08-04")
        self.assertEqual(storm_code_to_date(2005100800), "2005-10-08")

    def test_date_to_storm_code_range_is_half_open(self):
        lower, upper = date_to_storm_code_range("2020-08-04")
        self.assertEqual((lower, upper), (2020080400, 2020080500))
        self.assertTrue(lower <= 2020080406 < upper)
        self.assertFalse(lower <= 2020080506 < upper)

    def test_bad_date_raises(self):
        for bad in ("2020/08/04", "not-a-date", "2020-08"):
            with self.subTest(bad=bad):
                with self.assertRaises(StormAnalysisError):
                    date_to_storm_code_range(bad)


class TestResolveStorm(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def test_resolves_a_storm_code(self):
        resolution = resolve_storm(self.conn, "2020080406")
        self.assertTrue(resolution.ok)
        self.assertEqual(resolution.storm.storm_code, 2020080406)
        self.assertEqual(resolution.storm.date, "2020-08-04")

    def test_resolves_an_unambiguous_date(self):
        resolution = resolve_storm(self.conn, "2020-08-04")
        self.assertTrue(resolution.ok)
        self.assertEqual(resolution.storm.storm_code, 2020080406)

    def test_a_date_naming_two_storms_is_ambiguous_not_arbitrary(self):
        resolution = resolve_storm(self.conn, "2021-09-01")
        self.assertTrue(resolution.ambiguous)
        self.assertFalse(resolution.ok)
        self.assertIsNone(resolution.storm)
        self.assertEqual(
            [match.storm_code for match in resolution.matches],
            [2021090100, 2021090112],
        )

    def test_unknown_storm_is_missing_not_an_error(self):
        resolution = resolve_storm(self.conn, "1999010100")
        self.assertTrue(resolution.missing)
        self.assertIsNone(resolution.storm)


class TestStormSummary(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def test_statewide_total_equals_the_sum_of_county_rows(self):
        summary = storm_summary(self.conn, 2020080406)
        self.assertEqual(
            summary.statewide_outage, sum(row.outage for row in summary.counties)
        )

    def test_max_wind_and_precip_pick_the_right_county(self):
        summary = storm_summary(self.conn, 2020080406)
        self.assertEqual(summary.max_wind.county, "Hartford")
        self.assertEqual(summary.max_precip.county, "Fairfield")

    def test_argmax_ties_break_deterministically_by_county_name(self):
        conn = _make_conn(
            weather=[
                (2020080406, "Fairfield", 30.0, 9.0, 1, 0, 0, 0, 10),
                (2020080406, "Hartford", 30.0, 9.0, 1, 0, 0, 0, 10),
            ]
        )
        self.addCleanup(conn.close)
        first = storm_summary(conn, 2020080406).max_wind.county
        second = storm_summary(conn, 2020080406).max_wind.county
        self.assertEqual(first, second)
        self.assertEqual(first, "Fairfield")

    def test_top_outage_counties_are_ordered_and_capped_at_three(self):
        summary = storm_summary(self.conn, 2020080406)
        values = [entry.value for entry in summary.top_outage_counties]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertLessEqual(len(summary.top_outage_counties), 3)

    def test_normalization_uses_ohline_and_can_reorder_the_raw_ranking(self):
        """Fairfield leads on raw outage; Hartford has twice the line length,
        so the normalized ranking must be computed, not assumed."""
        summary = storm_summary(self.conn, 2020080406)
        by_county = {row.county: row for row in summary.counties}
        self.assertAlmostEqual(
            by_county["Fairfield"].outage_per_million_ohline,
            600 * 1_000_000.0 / 4_000_000,
        )
        self.assertEqual(summary.top_normalized_counties[0].county, "Fairfield")

    def test_county_scope_filters_to_one_county(self):
        summary = storm_summary(self.conn, 2020080406, county="Hartford")
        self.assertEqual(len(summary.counties), 1)
        self.assertEqual(summary.counties[0].county, "Hartford")
        self.assertEqual(summary.statewide_outage, 400)
        self.assertEqual(summary.scope_label, "Hartford County")

    def test_phase_two_fields_are_absent_never_zero(self):
        summary = storm_summary(self.conn, 2020080406)
        self.assertIsNone(summary.storm_type)
        self.assertIsNone(summary.peak_snowfall_mm)
        self.assertIsNone(summary.wind_gust_ms)

    def test_threshold_count_uses_the_configured_column(self):
        summary = storm_summary(self.conn, 2020080406)
        self.assertEqual(summary.counties_above_threshold, 2)  # both have wgt9 > 0
        wgt13_scoped = storm_summary(
            self.conn, 2020080406, threshold_column="wgt13"
        )
        self.assertEqual(wgt13_scoped.counties_above_threshold, 1)

    def test_top_outage_share_is_a_fraction_of_the_total(self):
        summary = storm_summary(self.conn, 2020080406)
        self.assertAlmostEqual(summary.top_outage_share, 600 / 1000)

    def test_unknown_storm_raises(self):
        with self.assertRaises(StormAnalysisError):
            storm_summary(self.conn, 1999010100)


class TestStormRanking(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def test_rank_and_total_are_always_consistent(self):
        ranking = storm_ranking(self.conn, 2020080406)
        expected_total = storm_count(self.conn)
        for metric in ranking.metrics:
            with self.subTest(metric=metric.metric):
                self.assertGreaterEqual(metric.rank, 1)
                self.assertLessEqual(metric.rank, metric.total)
                self.assertEqual(metric.total, expected_total)

    def test_ordering_follows_the_seeded_magnitudes(self):
        """The storm with the larger statewide outage ranks strictly better."""
        best = storm_ranking(self.conn, 2020080406).metric(METRIC_OUTAGE)
        middle = storm_ranking(self.conn, 2021090100).metric(METRIC_OUTAGE)
        worst = storm_ranking(self.conn, 2021090112).metric(METRIC_OUTAGE)
        self.assertLess(best.rank, middle.rank)
        self.assertLess(middle.rank, worst.rank)

    def test_state_scope_aggregates_counties_before_ranking(self):
        """SUM outage, MAX wind, MAX precip — the plan's stated aggregation."""
        ranking = storm_ranking(self.conn, 2020080406)
        summary = storm_summary(self.conn, 2020080406)
        self.assertEqual(
            ranking.metric(METRIC_OUTAGE).value, float(summary.statewide_outage)
        )
        self.assertEqual(
            ranking.metric(METRIC_MAX_WIND).value,
            max(row.peak_wind_ms for row in summary.counties),
        )
        self.assertEqual(
            ranking.metric(METRIC_MAX_PRECIP).value,
            max(row.precip_mm for row in summary.counties),
        )

    def test_county_scope_ranks_within_that_county_only(self):
        """Hartford's 2021090112 row (60) beats its 2021090100 row (100)? No —
        but the county ordering must follow the county's own values, not the
        statewide ones."""
        conn = _make_conn(
            weather=[
                # Statewide, storm A leads. Within Hartford, storm B leads.
                (2020080406, "Fairfield", 10.0, 5.0, 1, 0, 0, 0, 900),
                (2020080406, "Hartford", 10.0, 5.0, 1, 0, 0, 0, 10),
                (2021090100, "Fairfield", 10.0, 5.0, 1, 0, 0, 0, 10),
                (2021090100, "Hartford", 10.0, 5.0, 1, 0, 0, 0, 500),
            ],
            storms=_DEFAULT_STORMS[:2],
        )
        self.addCleanup(conn.close)

        statewide = storm_ranking(conn, 2020080406).metric(METRIC_OUTAGE)
        in_hartford = storm_ranking(conn, 2020080406, county="Hartford").metric(
            METRIC_OUTAGE
        )
        self.assertEqual(statewide.rank, 1)
        self.assertEqual(in_hartford.rank, 2)
        self.assertEqual(in_hartford.value, 10.0)

    def test_ties_share_a_rank_and_report_the_tie_group(self):
        conn = _make_conn(
            storms=_DEFAULT_STORMS[:2],
            weather=[
                (2020080406, "Fairfield", 10.0, 5.0, 1, 0, 0, 0, 100),
                (2021090100, "Fairfield", 10.0, 5.0, 1, 0, 0, 0, 100),
            ],
        )
        self.addCleanup(conn.close)
        first = storm_ranking(conn, 2020080406).metric(METRIC_OUTAGE)
        second = storm_ranking(conn, 2021090100).metric(METRIC_OUTAGE)
        self.assertEqual(first.rank, second.rank)
        self.assertTrue(first.tied)
        self.assertEqual(first.tie_count, 2)

    def test_percentile_is_derived_from_rank_and_total(self):
        ranking = storm_ranking(self.conn, 2020080406)
        metric = ranking.metric(METRIC_OUTAGE)
        self.assertAlmostEqual(metric.percentile, metric.rank / metric.total * 100.0)

    def test_state_parameter_is_honoured_not_decorative(self):
        """Seed two states; a CT-scoped rank must ignore the other state."""
        conn = _make_conn(
            counties=[
                ("Fairfield", "CT", 4_000_000),
                ("Providence", "RI", 4_000_000),
            ],
            weather=[
                (2020080406, "Fairfield", 10.0, 5.0, 1, 0, 0, 0, 100),
                (2020080406, "Providence", 99.0, 20.0, 9, 9, 9, 9, 9999),
                (2021090100, "Fairfield", 10.0, 5.0, 1, 0, 0, 0, 50),
            ],
            storms=_DEFAULT_STORMS[:2],
        )
        self.addCleanup(conn.close)

        ct = storm_ranking(conn, 2020080406, state="CT").metric(METRIC_OUTAGE)
        self.assertEqual(ct.value, 100.0, "RI rows must not enter a CT aggregate")
        self.assertEqual(ct.total, 2)

        ri = storm_ranking(conn, 2020080406, state="RI").metric(METRIC_OUTAGE)
        self.assertEqual(ri.value, 9999.0)
        self.assertEqual(ri.total, 1)

    def test_duration_is_not_a_rankable_metric(self):
        """eventLength is a fixed analysis window; ranking it is meaningless."""
        self.assertNotIn("duration", RANKABLE_METRICS)
        with self.assertRaises(StormAnalysisError) as caught:
            storm_ranking(self.conn, 2020080406, metrics=["duration"])
        self.assertIn("duration", str(caught.exception))

    def test_metrics_can_be_requested_selectively(self):
        ranking = storm_ranking(self.conn, 2020080406, metrics=[METRIC_MAX_WIND])
        self.assertEqual([m.metric for m in ranking.metrics], [METRIC_MAX_WIND])

    def test_storm_absent_from_the_scope_raises(self):
        conn = _make_conn(
            counties=[("Fairfield", "CT", 4_000_000), ("Hartford", "CT", 8_000_000)],
        )
        self.addCleanup(conn.close)
        with self.assertRaises(StormAnalysisError):
            storm_ranking(conn, 2020080406, county="Litchfield")


class TestCompareStorms(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def test_deltas_are_a_minus_b(self):
        comparison = compare_storms(self.conn, 2020080406, 2021090100)
        outage = next(d for d in comparison.deltas if d.metric == METRIC_OUTAGE)
        self.assertEqual(outage.value_a, 1000.0)
        self.assertEqual(outage.value_b, 300.0)
        self.assertEqual(outage.difference, 700.0)

    def test_percent_change_is_relative_to_storm_b(self):
        comparison = compare_storms(self.conn, 2020080406, 2021090100)
        outage = next(d for d in comparison.deltas if d.metric == METRIC_OUTAGE)
        self.assertAlmostEqual(outage.percent_change, 700.0 / 300.0 * 100.0)

    def test_percent_change_is_none_when_the_baseline_is_zero(self):
        conn = _make_conn(
            storms=_DEFAULT_STORMS[:2],
            weather=[
                (2020080406, "Fairfield", 10.0, 5.0, 1, 0, 0, 0, 100),
                (2021090100, "Fairfield", 10.0, 5.0, 1, 0, 0, 0, 0),
            ],
        )
        self.addCleanup(conn.close)
        comparison = compare_storms(conn, 2020080406, 2021090100)
        outage = next(d for d in comparison.deltas if d.metric == METRIC_OUTAGE)
        self.assertIsNone(outage.percent_change)
        self.assertEqual(outage.difference, 100.0)

    def test_threshold_row_uses_wgt9_not_wgt13(self):
        comparison = compare_storms(self.conn, 2020080406, 2021090100)
        threshold = next(
            d for d in comparison.deltas if d.metric == "counties_above_threshold"
        )
        self.assertIn("9+", threshold.label)
        self.assertEqual(threshold.value_a, 2.0)
        self.assertEqual(threshold.value_b, 0.0)

    def test_comparison_carries_both_rankings(self):
        comparison = compare_storms(self.conn, 2020080406, 2021090100)
        self.assertEqual(comparison.ranking_a.storm.storm_code, 2020080406)
        self.assertEqual(comparison.ranking_b.storm.storm_code, 2021090100)
        self.assertEqual(
            comparison.ranking_a.total_storms, comparison.ranking_b.total_storms
        )


class _RecordingSimilarityAgent:
    """Captures find_similar calls so delegation can be asserted."""

    def __init__(self, results=None):
        self.calls = []
        self._results = results

    def find_similar(self, conn, county, query_params, limit=5):
        self.calls.append((county, dict(query_params), limit))
        if self._results is not None:
            return self._results
        from wiser.agents.similarity_query_agent import SimilarityQueryAgent

        return SimilarityQueryAgent().find_similar(
            conn, county, query_params, limit=limit
        )


class TestCompareForecast(unittest.TestCase):
    """Mode B — a forecast storm against the historical record."""

    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def test_delegates_retrieval_to_the_similarity_agent(self):
        """Mode B is the existing similarity search with a different report,
        not a reimplementation of ranking."""
        agent = _RecordingSimilarityAgent()
        compare_forecast(
            self.conn,
            {"peakWSPD10": 12.07},
            county="Fairfield",
            similarity_agent=agent,
            limit=3,
        )
        self.assertEqual(len(agent.calls), 1)
        county, params, limit = agent.calls[0]
        self.assertEqual(county, "Fairfield")
        self.assertEqual(params, {"peakWSPD10": 12.07})
        self.assertEqual(limit, 3)

    def test_forecast_carries_no_outage_of_its_own(self):
        """Outage is an outcome; a forecast supplies only conditions."""
        comparison = compare_forecast(
            self.conn, {"peakWSPD10": 12.07}, county="Fairfield"
        )
        self.assertFalse(hasattr(comparison, "outage"))
        self.assertFalse(hasattr(comparison, "statewide_outage"))
        self.assertNotIn("outage", comparison.params)

    def test_position_is_where_the_forecast_would_rank(self):
        """Seeded max winds are 11.0, 7.0, 5.5 statewide. A 12.07 m/s forecast
        exceeds all of them, so it would rank first."""
        comparison = compare_forecast(self.conn, {"peakWSPD10": 12.07})
        wind = next(p for p in comparison.positions if p.metric == METRIC_MAX_WIND)
        self.assertEqual(wind.rank, 1)
        self.assertEqual(wind.total, storm_count(self.conn))

    def test_position_ranks_below_stronger_historical_storms(self):
        comparison = compare_forecast(self.conn, {"peakWSPD10": 6.0})
        wind = next(p for p in comparison.positions if p.metric == METRIC_MAX_WIND)
        # Two seeded storms have a higher statewide max wind (11.0 and 7.0).
        self.assertEqual(wind.rank, 3)

    def test_positions_cover_only_the_supplied_parameters(self):
        wind_only = compare_forecast(self.conn, {"peakWSPD10": 9.0})
        self.assertEqual(
            [p.metric for p in wind_only.positions], [METRIC_MAX_WIND]
        )

        precip_only = compare_forecast(self.conn, {"sumAFWA_TOTPRECIP": 30.0})
        self.assertEqual(
            [p.metric for p in precip_only.positions], [METRIC_MAX_PRECIP]
        )

    def test_threshold_only_input_still_finds_analogues_without_positions(self):
        comparison = compare_forecast(self.conn, {"wgt5": 12}, county="Fairfield")
        self.assertEqual(comparison.positions, ())
        self.assertTrue(comparison.analogue_groups)
        self.assertEqual(comparison.total_storms, storm_count(self.conn))

    def test_analogues_carry_real_recorded_outages(self):
        comparison = compare_forecast(
            self.conn, {"peakWSPD10": 9.4}, county="Fairfield", limit=5
        )
        analogues = comparison.analogue_groups[0].analogues
        self.assertTrue(analogues)
        seeded_outages = {600, 200, 50}
        for analogue in analogues:
            with self.subTest(storm=analogue.storm_code):
                self.assertIn(analogue.outage, seeded_outages)
                self.assertEqual(analogue.date, storm_code_to_date(analogue.storm_code))

    def test_statewide_groups_analogues_per_county(self):
        """Z-scores are computed within a county, so scores from different
        counties are not comparable and must not be merged into one ranking."""
        comparison = compare_forecast(self.conn, {"peakWSPD10": 9.0}, limit=2)
        self.assertIsNone(comparison.county_scope)
        self.assertEqual(
            [group.county for group in comparison.analogue_groups],
            list(state_counties(self.conn)),
        )
        for group in comparison.analogue_groups:
            with self.subTest(county=group.county):
                self.assertTrue(
                    all(a.county == group.county for a in group.analogues),
                    "an analogue must stay in the county it was scored against",
                )

    def test_county_scope_queries_only_that_county(self):
        agent = _RecordingSimilarityAgent()
        compare_forecast(
            self.conn, {"peakWSPD10": 9.0}, county="Hartford", similarity_agent=agent
        )
        self.assertEqual([call[0] for call in agent.calls], ["Hartford"])

    def test_statewide_scope_queries_every_county_in_the_state(self):
        agent = _RecordingSimilarityAgent()
        compare_forecast(self.conn, {"peakWSPD10": 9.0}, similarity_agent=agent)
        self.assertEqual(
            [call[0] for call in agent.calls], list(state_counties(self.conn))
        )

    def test_state_parameter_scopes_the_county_list(self):
        conn = _make_conn(
            counties=[
                ("Fairfield", "CT", 4_000_000),
                ("Providence", "RI", 4_000_000),
            ],
            weather=[
                (2020080406, "Fairfield", 10.0, 5.0, 1, 0, 0, 0, 100),
                (2020080406, "Providence", 99.0, 20.0, 9, 9, 9, 9, 9999),
            ],
            storms=_DEFAULT_STORMS[:1],
        )
        self.addCleanup(conn.close)
        agent = _RecordingSimilarityAgent()
        compare_forecast(conn, {"peakWSPD10": 9.0}, state="RI", similarity_agent=agent)
        self.assertEqual([call[0] for call in agent.calls], ["Providence"])

    def test_empty_parameters_raise(self):
        with self.assertRaises(StormAnalysisError):
            compare_forecast(self.conn, {})

    def test_unknown_state_raises_rather_than_returning_empty(self):
        with self.assertRaises(StormAnalysisError):
            compare_forecast(self.conn, {"peakWSPD10": 9.0}, state="ZZ")

    def test_counties_without_history_are_skipped_not_rendered_empty(self):
        agent = _RecordingSimilarityAgent(results=[])
        comparison = compare_forecast(
            self.conn, {"peakWSPD10": 9.0}, similarity_agent=agent
        )
        self.assertEqual(comparison.analogue_groups, ())


if __name__ == "__main__":
    unittest.main()
