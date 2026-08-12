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
    OVERLAP_EXPLANATION,
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

    def test_summary_flags_a_same_date_sibling(self):
        result = handle_storm_summary(
            _make_ctx("summarize storm 2021090100", self.conn)
        )
        warnings = [n.text for n in result.notices if n.level == "warning"]
        note = next(w for w in warnings if "shares its date" in w)

        self.assertIn("2021090112", note)
        self.assertIn("only part of the event", note)
        # The summary still renders; nothing is withheld or merged.
        self.assertIsNotNone(result.report)

    def test_summary_is_silent_for_a_unique_date(self):
        result = handle_storm_summary(
            _make_ctx("summarize storm 2020080406", self.conn)
        )
        warnings = [n.text for n in result.notices if n.level == "warning"]
        self.assertFalse([w for w in warnings if "shares its date" in w])

    def test_disambiguation_explains_the_overlap_before_asking_a_choice(self):
        """Without this the user picks arbitrarily between what may be two
        records of the same weather system."""
        result = handle_storm_summary(
            _make_ctx("storm summary for 2021-09-01", self.conn)
        )
        message = _errors(result)[0]

        self.assertIn("48-hour window", message)
        self.assertIn("one weather system recorded twice", message)
        self.assertIn("may not be a meaningful choice", message)
        # And still offers the runnable options.
        self.assertIn("storm summary for 2021090100", message)

    def test_all_three_messages_share_one_overlap_explanation(self):
        """One source of truth, so the three places cannot drift apart."""
        summary = handle_storm_summary(
            _make_ctx("summarize storm 2021090100", self.conn)
        )
        ranking = handle_storm_ranking(
            _make_ctx("how does storm 2021090100 rank", self.conn)
        )
        ambiguous = handle_storm_summary(
            _make_ctx("storm summary for 2021-09-01", self.conn)
        )
        texts = [
            next(n.text for n in summary.notices if "shares its date" in n.text),
            next(n.text for n in ranking.notices if "shares its date" in n.text),
            _errors(ambiguous)[0],
        ]
        for text in texts:
            with self.subTest(text=text[:40]):
                self.assertIn(OVERLAP_EXPLANATION, text)

    def test_ambiguous_options_are_ready_to_run_queries(self):
        """The user should be able to copy a line, not retype the question."""
        query = "storm summary for 2021-09-01"
        result = handle_storm_summary(_make_ctx(query, self.conn))
        message = _errors(result)[0]

        self.assertIn("storm summary for 2021090100", message)
        self.assertIn("storm summary for 2021090112", message)
        self.assertIn("(2021-09-01 00:00)", message)

    def test_a_suggested_requery_actually_resolves(self):
        result = handle_storm_summary(
            _make_ctx("storm summary for 2021-09-01", self.conn)
        )
        suggestion = next(
            line.strip().split("   ")[0]
            for line in _errors(result)[0].splitlines()
            if "2021090100" in line
        )
        rerun = handle_storm_summary(_make_ctx(suggestion, self.conn))
        self.assertIsNotNone(rerun.report)
        self.assertEqual(rerun.report_kind, REPORT_STORM_SUMMARY)

    def test_a_mid_sentence_token_keeps_the_sentence_intact(self):
        result = handle_storm_ranking(
            _make_ctx("how does the storm on 2021-09-01 rank", self.conn)
        )
        message = _errors(result)[0]
        self.assertIn("how does the storm on 2021090100 rank", message)

    def test_display_rows_carry_labels_not_metric_keys(self):
        result = handle_storm_summary(
            _make_ctx("summarize storm 2020080406", self.conn)
        )
        self.assertTrue(result.display_rows)
        self.assertIn("County", result.display_rows[0])
        self.assertNotIn("peakWSPD10", result.display_rows[0])
        # The machine-named rows survive for maps and the reasoning layer.
        self.assertIn("peakWSPD10", result.rows[0])

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

    def test_same_date_sibling_is_flagged_on_the_ranking(self):
        """A same-date pair sits at adjacent ranks with near-identical outage
        figures, which reads as a duplicate-row bug unless it is named."""
        result = handle_storm_ranking(
            _make_ctx("how does storm 2021090100 rank", self.conn)
        )
        warnings = [n.text for n in result.notices if n.level == "warning"]
        note = next(w for w in warnings if "shares its date" in w)

        self.assertIn("2021090112", note)
        self.assertIn("2021-09-01 12:00", note)
        self.assertIn("48-hour", note)
        # Both storms stay in the ranking; nothing is deduplicated.
        self.assertIsNotNone(result.report)

    def test_the_sibling_notice_is_symmetric(self):
        result = handle_storm_ranking(
            _make_ctx("how does storm 2021090112 rank", self.conn)
        )
        warnings = [n.text for n in result.notices if n.level == "warning"]
        self.assertTrue(any("2021090100" in w for w in warnings))

    def test_no_sibling_notice_for_a_storm_with_a_unique_date(self):
        result = handle_storm_ranking(
            _make_ctx("how does storm 2020080406 rank", self.conn)
        )
        warnings = [n.text for n in result.notices if n.level == "warning"]
        self.assertFalse([w for w in warnings if "shares its date" in w])


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
        self.assertEqual(len(result.rows), 5)

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
                "how does an upcoming storm with wind=9.2ms compare in Fairfield",
                self.conn,
            )
        )
        self.assertIsNotNone(forecast.report)
        self.assertEqual(forecast.report_kind, REPORT_FORECAST_COMPARISON)
        self.assertEqual(forecast.rows, plain.rows)
        self.assertEqual(forecast.params, plain.params)
        self.assertEqual(forecast.county, plain.county)

    def test_statewide_forecast_needs_no_county(self):
        """A forecast compared against the whole record is legitimately
        statewide; compare_forecast groups analogues per county. Requiring a
        county here made the query error instead of rendering."""
        result = handle_similarity_search(
            _make_ctx(
                "how does an upcoming storm with wind=27mph compare to the record",
                self.conn,
            )
        )
        self.assertTrue(result.ok)
        self.assertIsNone(result.county)
        self.assertIsNotNone(result.report)
        self.assertEqual(result.report_kind, REPORT_FORECAST_COMPARISON)

    def test_plain_similarity_without_a_county_still_errors(self):
        result = handle_similarity_search(
            _make_ctx("find storms similar to wind=9.2", self.conn)
        )
        self.assertFalse(result.ok)
        self.assertIn("County required", _errors(result)[0])

    def test_forecast_units_are_honoured_on_the_similarity_route(self):
        """parse_similarity_params drops the suffix, so "wind=27mph" would be
        read as 27 m/s — 60 mph, a fivefold error."""
        result = handle_similarity_search(
            _make_ctx(
                "how does an upcoming storm with wind=27mph compare to the record",
                self.conn,
            )
        )
        self.assertAlmostEqual(result.params["peakWSPD10"], 12.070, places=2)
        self.assertIn("27.0 mph", result.report)
        self.assertNotIn("60.4 mph", result.report)

    def test_a_bare_forecast_number_is_rejected_on_this_route_too(self):
        result = handle_similarity_search(
            _make_ctx(
                "how does an upcoming storm with wind=27 compare to the record",
                self.conn,
            )
        )
        self.assertFalse(result.ok)
        self.assertIn("has no unit", _errors(result)[0])

    def test_plain_similarity_keeps_the_unit_free_convention(self):
        result = handle_similarity_search(
            _make_ctx("find storms similar to wind=9.2 in Fairfield", self.conn)
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.params["peakWSPD10"], 9.2)

    def test_forecast_report_keeps_the_not_a_prediction_line(self):
        result = handle_similarity_search(
            _make_ctx(
                "how does an upcoming storm with wind=9.2ms compare in Fairfield",
                self.conn,
            )
        )
        self.assertIn("NOT a prediction", " ".join(result.report.split()))


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
