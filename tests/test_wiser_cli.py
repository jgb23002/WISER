"""Tests for similarity query parsing, the route registry, and CLI rendering."""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
SRC_DIR = Path(__file__).parent.parent / "src"
FIXTURE_DIR = Path(__file__).parent / "fixtures"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
TESTS_DIR = Path(__file__).parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from pg_harness import make_conn  # noqa: E402

from wiser.agents.sql_ai_agent import SqlValidationError  # noqa: E402
from wiser.agents.task_routing_agent import Route  # noqa: E402
from wiser.handlers import (  # noqa: E402
    ROUTE_HANDLERS,
    AnalyticsResult,
    Notice,
    SimilarityResult,
    handle_weather_analytics,
    parse_similarity_county,
    parse_similarity_params,
)
from wiser_cli import main, render_result  # noqa: E402


class TestParseSimilarityParams(unittest.TestCase):
    def test_parses_comma_separated_pairs(self):
        params = parse_similarity_params(
            "find storms similar to wind=9.2, precip=45"
        )
        self.assertEqual(params["peakWSPD10"], 9.2)
        self.assertEqual(params["sumAFWA_TOTPRECIP"], 45.0)

    def test_parses_multiline_query(self):
        query = "Find storms similar to:\nwind=9.2\nprecip=45"
        params = parse_similarity_params(query)
        self.assertEqual(params["peakWSPD10"], 9.2)
        self.assertEqual(params["sumAFWA_TOTPRECIP"], 45.0)

    def test_parses_wgt_aliases(self):
        params = parse_similarity_params("similar to wgt5=10, wgt9=3")
        self.assertEqual(params["wgt5"], 10.0)
        self.assertEqual(params["wgt9"], 3.0)

    def test_ignores_unknown_wgt_aliases(self):
        self.assertEqual(parse_similarity_params("similar to wgt99=3"), {})

    def test_ignores_unknown_aliases(self):
        params = parse_similarity_params("similar to temp=72, wind=9.0")
        self.assertEqual(params, {"peakWSPD10": 9.0})

    def test_empty_when_no_params(self):
        self.assertEqual(parse_similarity_params("find similar storms"), {})


class TestParseSimilarityCounty(unittest.TestCase):
    def test_explicit_county_flag(self):
        county = parse_similarity_county(
            "find storms similar to wind=9.2", explicit_county="fairfield"
        )
        self.assertEqual(county, "Fairfield")

    def test_detects_known_county_in_query(self):
        county = parse_similarity_county(
            "find storms similar to wind=9.2 in Hartford"
        )
        self.assertEqual(county, "Hartford")

    def test_detects_two_word_county(self):
        county = parse_similarity_county(
            "storms similar to wind=6.5 for New Haven"
        )
        self.assertEqual(county, "New Haven")

    def test_county_keyword_pattern(self):
        county = parse_similarity_county(
            "similar to wind=8.0 for county Litchfield"
        )
        self.assertEqual(county, "Litchfield")


class TestRouteRegistry(unittest.TestCase):
    def test_all_routes_have_handlers_except_unknown(self):
        # Derived from Route so a newly added route fails here until wired up.
        expected = set(Route) - {Route.UNKNOWN}
        self.assertEqual(set(ROUTE_HANDLERS.keys()), expected)

    def test_similarity_search_handler_registered(self):
        self.assertIn(Route.SIMILARITY_SEARCH, ROUTE_HANDLERS)
        self.assertTrue(callable(ROUTE_HANDLERS[Route.SIMILARITY_SEARCH]))


class TestWeatherAnalyticsHandler(unittest.TestCase):
    def test_unknown_analytics_query_does_not_fall_back_to_top_counties(self):
        class FailingAnalyticsAgent:
            def county_outage_summary_from_db(self, conn, limit=10):
                raise AssertionError("unexpected top-counties fallback")

        ctx = SimpleNamespace(
            query="How many storms had outage > 500 in 2020?",
            conn=None,
            limit=10,
            analytics_agent=FailingAnalyticsAgent(),
            sql_model=None,
            dry_run_sql=False,
        )

        # Force the SQL AI agent to fail so the fixed-query fallback runs;
        # this test asserts the fallback's behaviour for unsupported queries.
        with patch(
            "wiser.agents.sql_ai_agent.SqlAiAgent.run",
            side_effect=SqlValidationError("forced failure for fallback test"),
        ):
            result = handle_weather_analytics(ctx)

        output = io.StringIO()
        with redirect_stdout(output):
            render_result(result)

        self.assertIn(
            "I can't answer this with the current analytics agent yet",
            output.getvalue(),
        )
        self.assertIn("top counties by outage", output.getvalue())


class TestCliEvaluationFlags(unittest.TestCase):
    def _run_cli(self, *args: str) -> str:
        output = io.StringIO()
        with patch.object(sys, "argv", ["wiser_cli.py", *args]), \
                redirect_stdout(output):
            main()
        return output.getvalue()

    def test_evaluate_routing_prints_metrics(self):
        output = self._run_cli("--evaluate-routing")

        self.assertIn("routing:", output)
        self.assertIn("100.0%", output)

    def test_evaluate_sql_prints_metrics(self):
        output = self._run_cli("--evaluate-sql")

        self.assertIn("sql:", output)
        self.assertIn("100.0%", output)


class TestCliExternalLoad(unittest.TestCase):
    def test_load_external_eagleeye_prints_summary(self):
        # There is no temp database file to point at any more; the CLI writes
        # into the test schema that pg_harness pins via DATABASE_URL.
        make_conn()
        output = io.StringIO()
        with patch.object(
            sys,
            "argv",
            [
                "wiser_cli.py",
                "--load-external",
                "--external-source",
                "eagleeye",
                "--external-file",
                str(FIXTURE_DIR / "eagleeye_sample.csv"),
            ],
        ), redirect_stdout(output):
            main()

        text = output.getvalue()
        self.assertIn("External outage observations loaded", text)
        self.assertIn("source: EagleEye", text)
        self.assertIn("rows parsed: 2", text)


class TestStormReportRendering(unittest.TestCase):
    """The report block prints above the rows; results without one are
    byte-identical to what the CLI printed before storm-scoped routes existed."""

    @staticmethod
    def _render(result) -> str:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            render_result(result)
        return buffer.getvalue()

    def test_report_replaces_the_raw_row_dump(self):
        """The block already lays out every figure; printing machine column
        names and full-precision floats underneath only buries the answer."""
        result = AnalyticsResult(
            report="Storm date:        2020-08-04\nStatewide outages:  21,557",
            report_kind="storm_summary",
            rows=[{"county": "Fairfield", "outage_per_million_ohline": 1214.2402661}],
            answer="ignored while a report is present",
        )
        output = self._render(result)

        self.assertIn("Storm date:", output)
        self.assertNotIn("outage_per_million_ohline", output)
        self.assertNotIn("1214.2402661", output)

    def test_a_storm_route_with_only_notices_prints_no_row_placeholder(self):
        result = AnalyticsResult(
            report=None,
            report_kind="storm_summary",
            rows=[],
            notices=[Notice("2021-09-01 matches more than one storm.", level="error")],
        )
        output = self._render(result)

        self.assertIn("matches more than one storm", output)
        self.assertNotIn("No results found", output)

    def test_notices_print_before_the_report(self):
        result = AnalyticsResult(
            report="Storm date: 2020-08-04",
            report_kind="storm_summary",
            rows=[],
            notices=[Notice("2021-09-01 matches more than one storm.", level="error")],
        )
        output = self._render(result)
        self.assertLess(
            output.index("matches more than one storm"), output.index("Storm date:")
        )

    def test_a_result_without_a_report_is_unchanged(self):
        with_report_field = AnalyticsResult(
            sql="SELECT 1", rows=[{"county": "Fairfield", "total_outage": 10}],
            answer="one row",
        )
        output = self._render(with_report_field)
        self.assertIn("one row", output)
        self.assertIn("Fairfield", output)
        self.assertNotIn("Storm date", output)

    def test_similarity_forecast_report_prints_above_the_table(self):
        result = SimilarityResult(
            ok=True,
            county="Fairfield",
            params={"peakWSPD10": 9.2},
            rows=[
                {
                    "stormCode": 2020080406,
                    "similarity": 0.9,
                    "eventDateTime": "2020-08-04 06:00:00",
                    "peakWSPD10": 9.2,
                    "outage": 5935,
                }
            ],
            total_historical=294,
            report="Forecast Comparison: upcoming storm vs historical record",
            report_kind="forecast_comparison",
        )
        output = self._render(result)
        self.assertIn("Forecast Comparison:", output)
        # The forecast report lays out its own analogue table, so the raw
        # similarity table is not printed underneath it.
        self.assertNotIn("Similarity Analysis", output)


if __name__ == "__main__":
    unittest.main()
