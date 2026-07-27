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
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.agents.sql_ai_agent import SqlValidationError  # noqa: E402
from wiser.agents.task_routing_agent import Route  # noqa: E402
from wiser.handlers import (  # noqa: E402
    ROUTE_HANDLERS,
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
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "external.db"
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
                    "--db",
                    str(db_path),
                ],
            ), redirect_stdout(output):
                main()

        text = output.getvalue()
        self.assertIn("External outage observations loaded", text)
        self.assertIn("source: EagleEye", text)
        self.assertIn("rows parsed: 2", text)


if __name__ == "__main__":
    unittest.main()
