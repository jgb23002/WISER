"""Tests for the storm-scoped handlers in wiser.handlers.

Handlers never print: every assertion is on the returned dataclass. No network
— reasoning runs in local-fallback mode via use_openai_summary=False.

No storm count or outage magnitude from data/wiser.db is hardcoded here; each
test seeds its own database and asserts on relationships.
"""

from __future__ import annotations

import contextlib
import io
import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).parent.parent
SRC_DIR = REPO_ROOT / "src"
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.agents.similarity_query_agent import SimilarityQueryAgent  # noqa: E402
from wiser.agents.storm_weather_analytics_agent import (  # noqa: E402
    StormWeatherAnalyticsAgent,
)
from wiser.agents.task_routing_agent import Route  # noqa: E402
from wiser.handlers import (  # noqa: E402
    ROUTE_HANDLERS,
    AnalyticsResult,
    SimilarityResult,
    handle_similarity_search,
    handle_storm_comparison,
    handle_storm_ranking,
    handle_storm_summary,
    looks_like_forecast,
    parse_forecast_params,
    parse_storm_tokens,
    turn_from_result,
)
from wiser.storm_reports import (  # noqa: E402
    REPORT_FORECAST_COMPARISON,
    REPORT_STORM_COMPARISON,
    REPORT_STORM_RANKING,
    REPORT_STORM_SUMMARY,
)

_SCHEMA_SQL = SCHEMA_PATH.read_text(encoding="utf-8")

_STORMS = [
    (2020080406, "2020-08-04 06:00:00", 48),
    (2011082800, "2011-08-28 00:00:00", 48),
    (2021090100, "2021-09-01 00:00:00", 48),
    (2021090112, "2021-09-01 12:00:00", 48),
]
_COUNTIES = [("Fairfield", "CT", 4_000_000), ("Hartford", "CT", 8_000_000)]
_WEATHER = [
    (2020080406, "Fairfield", 30.0, 9.5, 20, 8, 0, 0, 600),
    (2020080406, "Hartford", 25.0, 11.0, 18, 6, 1, 0, 400),
    (2011082800, "Fairfield", 50.0, 7.0, 10, 2, 0, 0, 200),
    (2011082800, "Hartford", 45.0, 6.0, 8, 1, 0, 0, 100),
    (2021090100, "Fairfield", 10.0, 5.0, 4, 0, 0, 0, 50),
    (2021090100, "Hartford", 12.0, 5.5, 5, 0, 0, 0, 60),
    (2021090112, "Fairfield", 8.0, 4.5, 3, 0, 0, 0, 40),
    (2021090112, "Hartford", 9.0, 4.8, 4, 0, 0, 0, 30),
]


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA_SQL)
    conn.executemany(
        "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(?, ?, ?)",
        _STORMS,
    )
    conn.executemany(
        "INSERT INTO County(county, state, ohLine) VALUES(?, ?, ?)", _COUNTIES
    )
    conn.executemany(
        "INSERT INTO WeatherData(stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,"
        " wgt5, wgt9, wgt13, wgt18, outage) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        _WEATHER,
    )
    conn.commit()
    return conn


def _make_ctx(query: str, conn: sqlite3.Connection, **overrides) -> SimpleNamespace:
    defaults = dict(
        query=query,
        conn=conn,
        db_path=":memory:",
        limit=5,
        county=None,
        data_agent=None,
        analytics_agent=StormWeatherAnalyticsAgent(),
        rag_agent=None,
        similarity_agent=SimilarityQueryAgent(),
        rebuild_rag=False,
        web_sources=(),
        crawl_web_sources=False,
        include_linked_pdfs=False,
        max_linked_pdfs=0,
        similarity_backend="auto",
        openai_model=None,
        use_openai_summary=False,
        sql_model=None,
        dry_run_sql=False,
        history=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@contextlib.contextmanager
def _assert_no_stdout(test: unittest.TestCase):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield
    test.assertEqual(buf.getvalue(), "", "handler printed to stdout")


def _errors(result) -> list[str]:
    return [n.text for n in result.notices if n.level == "error"]


class TestParsing(unittest.TestCase):
    def test_storm_tokens_accept_looser_dates_than_the_router(self):
        self.assertEqual(parse_storm_tokens("storm 2020080406"), ["2020080406"])
        self.assertEqual(parse_storm_tokens("the 2020-08-04 storm"), ["2020-08-04"])
        self.assertEqual(parse_storm_tokens("the 8/4/2020 storm"), ["2020-08-04"])
        self.assertEqual(
            parse_storm_tokens("the storm on August 4, 2020"), ["2020-08-04"]
        )

    def test_storm_tokens_are_deduplicated_in_order(self):
        self.assertEqual(
            parse_storm_tokens("compare 2020080406 and 2011082800 and 2020080406"),
            ["2020080406", "2011082800"],
        )

    def test_forecast_params_require_explicit_units(self):
        params, notices = parse_forecast_params("wind=27mph precip=1.2in")
        self.assertAlmostEqual(params["peakWSPD10"], 12.070, places=2)
        self.assertAlmostEqual(params["sumAFWA_TOTPRECIP"], 30.48, places=2)
        self.assertEqual(notices, [])

    def test_bare_number_is_rejected_with_both_readings(self):
        params, notices = parse_forecast_params("wind=12")
        self.assertNotIn("peakWSPD10", params)
        self.assertEqual(len(notices), 1)
        message = notices[0].text
        self.assertIn("mph", message)
        self.assertIn("26.8", message)  # 12 m/s
        self.assertIn("5.36", message)  # 12 mph

    def test_threshold_hours_stay_dimensionless(self):
        params, notices = parse_forecast_params("wgt5=12")
        self.assertEqual(params["wgt5"], 12.0)
        self.assertEqual(notices, [])

    def test_forecast_cue_detection(self):
        self.assertTrue(looks_like_forecast("an upcoming storm"))
        self.assertTrue(looks_like_forecast("forecast for tomorrow"))
        self.assertFalse(looks_like_forecast("storm summary for 2020080406"))


class TestHandleStormSummary(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def test_returns_a_report_with_rows(self):
        with _assert_no_stdout(self):
            result = handle_storm_summary(
                _make_ctx("summarize storm 2020080406", self.conn)
            )
        self.assertIsInstance(result, AnalyticsResult)
        self.assertEqual(result.report_kind, REPORT_STORM_SUMMARY)
        self.assertIn("Storm date:", result.report)
        self.assertEqual(len(result.rows), 2)

    def test_attaches_reasoning_over_county_shaped_rows(self):
        result = handle_storm_summary(
            _make_ctx("summarize storm 2020080406", self.conn)
        )
        self.assertTrue(result.reasoning)
        self.assertIn("local analytical fallback", result.reasoning_mode)

    def test_county_scope_is_honoured(self):
        result = handle_storm_summary(
            _make_ctx("storm summary for Hartford in 2020080406", self.conn)
        )
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0]["county"], "Hartford")

    def test_unknown_storm_returns_a_notice_not_an_exception(self):
        with _assert_no_stdout(self):
            result = handle_storm_summary(
                _make_ctx("summarize storm 1999010100", self.conn)
            )
        self.assertEqual(result.rows, [])
        self.assertIsNone(result.report)
        self.assertTrue(_errors(result))

    def test_ambiguous_date_lists_candidates_and_picks_none(self):
        result = handle_storm_summary(
            _make_ctx("storm summary for 2021-09-01", self.conn)
        )
        self.assertIsNone(result.report)
        message = _errors(result)[0]
        self.assertIn("2021090100", message)
        self.assertIn("2021090112", message)

    def test_missing_identifier_explains_how_to_name_a_storm(self):
        result = handle_storm_summary(_make_ctx("storm summary please", self.conn))
        self.assertIn("No storm identifier", _errors(result)[0])


class TestHandleStormRanking(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def test_returns_rank_rows_and_a_report(self):
        with _assert_no_stdout(self):
            result = handle_storm_ranking(
                _make_ctx("how does storm 2020080406 rank", self.conn)
            )
        self.assertEqual(result.report_kind, REPORT_STORM_RANKING)
        self.assertEqual(len(result.rows), 3)
        for row in result.rows:
            with self.subTest(metric=row["metric"]):
                self.assertLessEqual(row["rank"], row["total"])

    def test_duration_request_is_refused_not_silently_substituted(self):
        result = handle_storm_ranking(
            _make_ctx("how does storm 2020080406 rank by duration", self.conn)
        )
        warnings = [n.text for n in result.notices if n.level == "warning"]
        self.assertTrue(any("Duration cannot be ranked" in w for w in warnings))
        self.assertNotIn("duration", [row["metric"] for row in result.rows])

    def test_county_scope_ranks_within_the_county(self):
        result = handle_storm_ranking(
            _make_ctx("how does storm 2020080406 rank in Hartford", self.conn)
        )
        self.assertIn("Hartford County", result.report)

    def test_unknown_storm_returns_a_notice(self):
        result = handle_storm_ranking(
            _make_ctx("how does storm 1999010100 rank", self.conn)
        )
        self.assertTrue(_errors(result))
        self.assertIsNone(result.report)


class TestHandleStormComparisonModeA(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def test_two_storms_produce_a_comparison(self):
        with _assert_no_stdout(self):
            result = handle_storm_comparison(
                _make_ctx("compare storm 2020080406 and 2011082800", self.conn)
            )
        self.assertEqual(result.report_kind, REPORT_STORM_COMPARISON)
        self.assertIn("Storm Comparison:", result.report)
        self.assertEqual(len(result.rows), 4)

    def test_one_storm_gives_a_notice_not_a_half_report(self):
        result = handle_storm_comparison(
            _make_ctx("compare storm 2020080406", self.conn)
        )
        self.assertIsNone(result.report)
        self.assertEqual(result.rows, [])
        self.assertIn("needs two storms", _errors(result)[0])

    def test_one_unresolvable_storm_gives_a_notice_not_a_half_report(self):
        result = handle_storm_comparison(
            _make_ctx("compare storm 2020080406 and 1999010100", self.conn)
        )
        self.assertIsNone(result.report)
        self.assertTrue(_errors(result))


class TestHandleStormComparisonModeB(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def test_forecast_parameters_select_mode_b(self):
        with _assert_no_stdout(self):
            result = handle_storm_comparison(
                _make_ctx(
                    "compare an upcoming storm with wind=27mph to storm 2020080406",
                    self.conn,
                )
            )
        self.assertEqual(result.report_kind, REPORT_FORECAST_COMPARISON)
        self.assertIn("Forecast Comparison:", result.report)

    def test_named_reference_storm_appears_as_measured_history(self):
        result = handle_storm_comparison(
            _make_ctx(
                "compare an upcoming storm with wind=27mph to storm 2020080406",
                self.conn,
            )
        )
        self.assertIn("Named reference storm (actual)", result.report)
        self.assertIn("2020-08-04", result.report)

    def test_no_outage_appears_on_the_forecast_side(self):
        result = handle_storm_comparison(
            _make_ctx(
                "compare an upcoming storm with wind=27mph to storm 2020080406",
                self.conn,
            )
        )
        forecast_block = result.report.split("Named reference storm")[0]
        self.assertNotIn("outage", forecast_block.lower())

    def test_bare_number_blocks_mode_b_with_an_explanatory_notice(self):
        result = handle_storm_comparison(
            _make_ctx(
                "compare an upcoming storm with wind=12 to storm 2020080406", self.conn
            )
        )
        self.assertIsNone(result.report)
        self.assertIn("has no unit", _errors(result)[0])

    def test_forecast_without_parameters_asks_for_them(self):
        result = handle_storm_comparison(
            _make_ctx("compare an upcoming storm to storm 2020080406", self.conn)
        )
        self.assertIsNone(result.report)
        self.assertIn("explicit", _errors(result)[0])

    def test_unresolvable_reference_still_renders_the_forecast_half(self):
        result = handle_storm_comparison(
            _make_ctx(
                "compare an upcoming storm with wind=27mph to storm 1999010100",
                self.conn,
            )
        )
        self.assertEqual(result.report_kind, REPORT_FORECAST_COMPARISON)
        self.assertNotIn("Named reference storm", result.report)
        self.assertTrue(
            _errors(result), "the dropped reference must be reported, not silent"
        )

    def test_non_positive_wind_is_rejected(self):
        result = handle_storm_comparison(
            _make_ctx(
                "compare an upcoming storm with wind=0mph to storm 2020080406",
                self.conn,
            )
        )
        self.assertIsNone(result.report)
        self.assertTrue(_errors(result))


class TestSimilarityPresentationSwitch(unittest.TestCase):
    """Comparing a forecast to the whole record names no storm, so it stays on
    the similarity route. Only the presentation changes."""

    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def test_without_a_forecast_cue_the_result_is_unchanged(self):
        ctx = _make_ctx(
            "find storms similar to wind=9.2 in Fairfield", self.conn
        )
        result = handle_similarity_search(ctx)

        self.assertIsNone(result.report)
        self.assertIsNone(result.report_kind)
        # Identical to a result constructed without any storm-scoped work.
        self.assertEqual(
            result,
            SimilarityResult(
                ok=True,
                county=result.county,
                params=result.params,
                rows=result.rows,
                total_historical=result.total_historical,
            ),
        )

    def test_a_forecast_cue_attaches_a_report_without_changing_the_rows(self):
        plain = handle_similarity_search(
            _make_ctx("find storms similar to wind=9.2 in Fairfield", self.conn)
        )
        forecast = handle_similarity_search(
            _make_ctx(
                "how does an upcoming storm with wind=9.2 compare in Fairfield",
                self.conn,
            )
        )
        self.assertIsNotNone(forecast.report)
        self.assertEqual(forecast.report_kind, REPORT_FORECAST_COMPARISON)
        self.assertEqual(forecast.rows, plain.rows)
        self.assertEqual(forecast.params, plain.params)
        self.assertEqual(forecast.county, plain.county)

    def test_forecast_report_keeps_the_not_a_prediction_line(self):
        result = handle_similarity_search(
            _make_ctx(
                "how does an upcoming storm with wind=9.2 compare in Fairfield",
                self.conn,
            )
        )
        self.assertIn("NOT a prediction", result.report)


class TestRegistryAndHistory(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def test_every_storm_route_has_a_handler(self):
        for route in (
            Route.STORM_SUMMARY,
            Route.STORM_RANKING,
            Route.STORM_COMPARISON,
        ):
            with self.subTest(route=route):
                self.assertIn(route, ROUTE_HANDLERS)

    def test_storm_reports_do_not_enter_the_sql_conversation_history(self):
        """Storm reports carry no LLM-generated SQL, so they must not be fed
        back into the SQL agent's prompt."""
        result = handle_storm_summary(
            _make_ctx("summarize storm 2020080406", self.conn)
        )
        self.assertIsNone(turn_from_result("summarize storm 2020080406", result))


if __name__ == "__main__":
    unittest.main()
