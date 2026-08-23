"""Regression tests for the six code-review fixes committed in 7fd6fe4.

That commit shipped six production fixes with no test coverage. Each test
class below pins exactly one of them, so a future change that reintroduces
the defect fails here rather than in a demo.

The fixes, in commit order:
  1. Duplicate heat map render        — streamlit_app._render_svg
  2. Reasoning failure destroying     — handlers.handle_weather_analytics
     valid SQL results                  handlers._attach_reasoning
  3. TypeError on SQL NULLs           — storm_reasoning_agent._as_float
  4. Empty comparison treated as hit  — handlers._weather_analytics_fallback
  5. Routing misroutes                — task_routing_agent._looks_like_data_management
  6. External outage counting         — external_outage.load_external_observations

No network calls. Every test runs under `python -m unittest`; pytest is not
installed in this environment.
"""

from __future__ import annotations

import ast
import sys

import psycopg
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent
SRC_DIR = REPO_ROOT / "src"
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"
STREAMLIT_APP_PATH = REPO_ROOT / "streamlit_app.py"

TESTS_DIR = Path(__file__).parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from pg_harness import executemany, make_conn, new_connection  # noqa: E402,F401

from wiser.agents.sql_ai_agent import SqlValidationError  # noqa: E402
from wiser.agents.storm_reasoning_agent import (  # noqa: E402
    _format_value,
    _lower_wind_higher_outage_observation,
    reason_locally,
)
from wiser.agents.storm_weather_analytics_agent import (  # noqa: E402
    StormWeatherAnalyticsAgent,
)
from wiser.agents.task_routing_agent import Route, TaskRoutingAgent  # noqa: E402
from wiser.handlers import (  # noqa: E402
    AnalyticsResult,
    _attach_reasoning,
    _weather_analytics_fallback,
    handle_weather_analytics,
)
from wiser.integrations.external_outage import (  # noqa: E402
    ExternalOutageObservation,
    external_observation_count,
    load_external_observations,
)

_SCHEMA_SQL = SCHEMA_PATH.read_text(encoding="utf-8")

_STORM_ROWS = [
    (2012102806, "2012-10-28 06:00:00", 48),
    (2011082800, "2011-08-28 00:00:00", 48),
]
_WEATHER_ROWS = [
    # stormCode, county, precip, wind, wgt5, wgt9, wgt13, wgt18, outage
    (2012102806, "Fairfield", 150.5, 12.3, 20, 8, 2, 0, 350),
    (2012102806, "Hartford", 120.0, 10.1, 14, 5, 1, 0, 500),
    (2011082800, "Fairfield", 88.0, 9.1, 12, 3, 0, 0, 120),
    (2011082800, "Hartford", 70.0, 8.4, 9, 2, 0, 0, 200),
]
_COUNTY_ROWS = [("Fairfield", 4887830), ("Hartford", 5869550)]


def _make_conn() -> psycopg.Connection:
    conn = make_conn()
    executemany(conn, 
        "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(%s, %s, %s)",
        _STORM_ROWS,
    )
    executemany(conn, "INSERT INTO County(county, ohLine) VALUES(%s, %s)", _COUNTY_ROWS)
    executemany(conn, 
        "INSERT INTO WeatherData(stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,"
        " wgt5, wgt9, wgt13, wgt18, outage) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        _WEATHER_ROWS,
    )
    conn.commit()
    return conn


def _make_ctx(query: str, conn: psycopg.Connection, **overrides) -> SimpleNamespace:
    defaults = dict(
        query=query,
        conn=conn,
        dsn=None,
        limit=5,
        county=None,
        data_agent=None,
        analytics_agent=StormWeatherAnalyticsAgent(),
        rag_agent=None,
        similarity_agent=None,
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


def _observation(record_id: str, *, outage: int = 5) -> ExternalOutageObservation:
    return ExternalOutageObservation(
        source_system="eagleeye",
        source_record_id=record_id,
        observed_at="2024-01-01T00:00:00Z",
        county="Fairfield",
        service_territory=None,
        outage_count=outage,
        customer_count=None,
        restoration_status=None,
        raw_payload="{}",
    )


# ---------------------------------------------------------------------------
# Fix 1 — duplicate heat map render
# ---------------------------------------------------------------------------


class TestNoDuplicateHeatmapRender(unittest.TestCase):
    """streamlit_app._render_svg embedded the same iframe twice.

    Asserted at the source level with ast: importing streamlit_app executes
    main() at module scope, and the defect was a literally duplicated call,
    so counting the calls in the function body pins it exactly. This also
    keeps the test running when streamlit is not installed.
    """

    @staticmethod
    def _render_svg_node() -> ast.FunctionDef:
        tree = ast.parse(STREAMLIT_APP_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_render_svg":
                return node
        raise AssertionError("streamlit_app._render_svg not found")

    def test_render_svg_embeds_exactly_one_iframe(self):
        iframe_calls = [
            node
            for node in ast.walk(self._render_svg_node())
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "iframe"
        ]
        self.assertEqual(
            len(iframe_calls),
            1,
            "_render_svg must embed the heat map exactly once; a second"
            " components.iframe call renders the map twice.",
        )

    def test_render_svg_encodes_once(self):
        encode_calls = [
            node
            for node in ast.walk(self._render_svg_node())
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "b64encode"
        ]
        self.assertEqual(len(encode_calls), 1)


# ---------------------------------------------------------------------------
# Fix 2 — a reasoning failure must not destroy a valid SQL result
# ---------------------------------------------------------------------------


class _BoomReasoningAgent:
    """Stands in for StormReasoningAgent and always fails."""

    def __init__(self, *args, **kwargs):
        pass

    def run(self, *args, **kwargs):
        raise RuntimeError("reasoning backend exploded")


class TestReasoningFailureKeepsSqlResult(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def test_attach_reasoning_keeps_rows_and_adds_warning(self):
        result = AnalyticsResult(
            sql="SELECT 1",
            rows=[{"county": "Fairfield", "total_outage": 350}],
            answer="one row",
        )
        ctx = _make_ctx("why did Fairfield lead?", self.conn)

        with patch(
            "wiser.handlers.StormReasoningAgent", _BoomReasoningAgent
        ):
            attached = _attach_reasoning(ctx, result)

        self.assertEqual(attached.sql, "SELECT 1")
        self.assertEqual(len(attached.rows), 1)
        self.assertEqual(attached.answer, "one row")
        self.assertEqual(attached.reasoning, "")
        self.assertTrue(
            any("reasoning unavailable" in n.text.lower() for n in attached.notices),
            f"expected a reasoning-unavailable notice, got {attached.notices}",
        )

    def test_reasoning_error_is_not_reported_as_a_sql_agent_error(self):
        """The regression: the reasoning call used to sit inside the SQL
        try/except, so a reasoning failure was caught by `except Exception`,
        discarded the valid SQL result, and silently ran the fallback chain."""

        class FakeSqlAgent:
            def __init__(self, *args, **kwargs):
                pass

            def run(self, *args, **kwargs):
                return {
                    "sql": "SELECT county, SUM(outage) FROM WeatherData GROUP BY county",
                    "rows": [{"county": "Hartford", "total_outage": 700}],
                    "answer": "Hartford leads.",
                    "dry_run": False,
                }

        ctx = _make_ctx("which county was affected the most?", self.conn)
        with patch("wiser.handlers.SqlAiAgent", FakeSqlAgent):
            with patch(
                "wiser.handlers.StormReasoningAgent", _BoomReasoningAgent
            ):
                result = handle_weather_analytics(ctx)

        self.assertFalse(
            result.used_fallback,
            "a reasoning failure must not trigger the SQL fallback chain",
        )
        self.assertEqual(result.answer, "Hartford leads.")
        self.assertEqual(result.rows, [{"county": "Hartford", "total_outage": 700}])
        self.assertFalse(
            any("SQL AI Agent error" in n.text for n in result.notices),
            "a reasoning failure must not be reported as an SQL agent error",
        )

    def test_sql_failure_still_falls_back(self):
        """Guard the other direction: the fix must not have broken the real
        fallback path."""

        class FailingSqlAgent:
            def __init__(self, *args, **kwargs):
                pass

            def run(self, *args, **kwargs):
                raise SqlValidationError("no api key")

        ctx = _make_ctx("top counties by outage", self.conn)
        with patch("wiser.handlers.SqlAiAgent", FailingSqlAgent):
            result = handle_weather_analytics(ctx)

        self.assertTrue(result.used_fallback)
        self.assertTrue(any("falling back" in n.text for n in result.notices))


# ---------------------------------------------------------------------------
# Fix 3 — TypeError on SQL NULLs
# ---------------------------------------------------------------------------


class TestNullToleranceInReasoning(unittest.TestCase):
    def test_as_float_handles_none_and_defaults(self):
        # Imported inside the test, not at module scope: reverting the fix
        # removes this private helper, and a module-level import would turn
        # every behavioural test below into an ImportError instead of letting
        # them fail on the TypeError that is the actual defect.
        from wiser.agents.storm_reasoning_agent import _as_float

        self.assertEqual(_as_float(None), float("-inf"))
        self.assertEqual(_as_float(None, 0.0), 0.0)
        self.assertEqual(_as_float(3), 3.0)
        self.assertEqual(_as_float("not a number"), float("-inf"))

    def test_format_value_reports_none_explicitly(self):
        self.assertEqual(_format_value(None), "not reported")
        self.assertNotEqual(_format_value(None), "0")

    def test_reason_locally_survives_null_metric(self):
        """A LEFT JOIN aggregate can emit NULL; sorting used to raise
        TypeError on float(None)."""
        rows = [
            {"county": "Fairfield", "total_outage": 350, "peakWSPD10": 12.3},
            {"county": "Hartford", "total_outage": None, "peakWSPD10": 10.1},
        ]
        answer = reason_locally("which county was affected the most?", rows)
        self.assertIn("Fairfield", answer)
        self.assertIsInstance(answer, str)

    def test_gap_sentence_omitted_when_a_value_is_null(self):
        rows = [
            {"county": "Fairfield", "total_outage": 350},
            {"county": "Hartford", "total_outage": None},
        ]
        answer = reason_locally("top counties by outage", rows)
        self.assertIn("Next highest", answer)
        self.assertNotIn(
            "a gap of",
            answer,
            "a gap is only meaningful when both sides are measured values",
        )

    def test_lower_wind_higher_outage_survives_null_outage(self):
        rows = [
            {"county": "Fairfield", "outage": None, "peakWSPD10": 12.3},
            {"county": "Hartford", "outage": 500, "peakWSPD10": 10.1},
        ]
        # Must not raise; the observation itself may or may not be found.
        _lower_wind_higher_outage_observation(rows)


# ---------------------------------------------------------------------------
# Fix 4 — an empty comparison is a miss, not a hit
# ---------------------------------------------------------------------------


class TestEmptyComparisonIsNotAHit(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def test_absent_storm_code_does_not_short_circuit_the_fallback_chain(self):
        """`if comparison_rows is not None` treated an empty list as a hit,
        returning a bare empty table instead of trying the other branches."""
        ctx = _make_ctx(
            "for storm 1999010100, why did Hartford have more outages than Fairfield?",
            self.conn,
        )
        result = _weather_analytics_fallback(ctx)

        self.assertNotEqual(
            result.fallback_title,
            "County comparison for analytical reasoning:",
            "an empty comparison must not be returned as the answer",
        )

    def test_present_storm_code_still_produces_the_comparison(self):
        ctx = _make_ctx(
            "for storm 2012102806, why did Hartford have more outages than Fairfield?",
            self.conn,
        )
        result = _weather_analytics_fallback(ctx)

        self.assertEqual(
            result.fallback_title, "County comparison for analytical reasoning:"
        )
        self.assertEqual(len(result.rows), 2)


# ---------------------------------------------------------------------------
# Fix 5 — routing misroutes
# ---------------------------------------------------------------------------


class TestRoutingHardening(unittest.TestCase):
    def setUp(self):
        self.agent = TaskRoutingAgent()

    def test_crawl_does_not_match_raw_as_a_substring(self):
        """Whole-word matching: "crawl" contains "raw"."""
        decision = self.agent.route("crawl the external storm sources")
        self.assertIsNot(
            decision.route,
            Route.DATA_RETRIEVAL,
            'substring matching would read "crawl" as the "raw" artifact keyword',
        )

    def test_normalize_with_database_as_a_source_stays_analytics(self):
        for query in (
            "normalize outages by population using the database",
            "normalize county outages by overhead line exposure",
        ):
            with self.subTest(query=query):
                self.assertIs(
                    self.agent.route(query).route,
                    Route.WEATHER_ANALYTICS,
                    "naming the database as a source is not an ETL request",
                )

    def test_data_action_needs_a_data_artifact(self):
        """A bare action verb is not ETL without a named artifact."""
        self.assertIs(
            self.agent.route("biggest outage load in Fairfield").route,
            Route.WEATHER_ANALYTICS,
        )
        self.assertIs(
            self.agent.route("load the raw storm csv").route,
            Route.DATA_RETRIEVAL,
        )

    def test_normalize_on_a_raw_artifact_is_still_etl(self):
        self.assertIs(
            self.agent.route("normalize the weather csv").route,
            Route.DATA_RETRIEVAL,
        )

    def test_matches_word_is_whole_word(self):
        self.assertTrue(TaskRoutingAgent._matches_word("load the raw csv", ("raw",)))
        self.assertFalse(TaskRoutingAgent._matches_word("crawl the sources", ("raw",)))
        self.assertFalse(TaskRoutingAgent._matches_word("database", ("db",)))


# ---------------------------------------------------------------------------
# Fix 6 — external outage counting
# ---------------------------------------------------------------------------


class TestExternalOutageCounting(unittest.TestCase):
    def setUp(self):
        self.conn = make_conn()
        self.addCleanup(self.conn.close)

    def test_first_load_counts_every_row_as_inserted(self):
        summary = load_external_observations(
            self.conn, [_observation("a"), _observation("b")]
        )
        self.assertEqual(summary.rows_seen, 2)
        self.assertEqual(summary.rows_loaded, 2)
        self.assertEqual(summary.rows_updated, 0)
        self.assertEqual(summary.table_count, 2)

    def test_reloading_the_same_batch_inserts_nothing(self):
        """rows_loaded used to report len(observations) unconditionally, so a
        pure-duplicate reload claimed to have loaded rows it only refreshed."""
        batch = [_observation("a"), _observation("b")]
        load_external_observations(self.conn, batch)
        summary = load_external_observations(self.conn, batch)

        self.assertEqual(summary.rows_seen, 2)
        self.assertEqual(summary.rows_loaded, 0)
        self.assertEqual(summary.rows_updated, 2)
        self.assertEqual(summary.table_count, 2)
        self.assertEqual(external_observation_count(self.conn), 2)

    def test_partial_overlap_splits_inserted_and_updated(self):
        load_external_observations(self.conn, [_observation("a")])
        summary = load_external_observations(
            self.conn, [_observation("a", outage=9), _observation("b")]
        )
        self.assertEqual(summary.rows_loaded, 1)
        self.assertEqual(summary.rows_updated, 1)
        self.assertEqual(external_observation_count(self.conn), 2)

    def test_empty_batch_reports_zeros_not_the_table_total(self):
        load_external_observations(self.conn, [_observation("a")])
        summary = load_external_observations(self.conn, [])

        self.assertEqual(summary.source_system, "")
        self.assertEqual(summary.rows_seen, 0)
        self.assertEqual(summary.rows_loaded, 0)
        self.assertEqual(summary.rows_updated, 0)
        self.assertEqual(
            summary.table_count,
            0,
            "an empty batch has no source to scope a count to; reporting the"
            " table-wide total would attribute other sources' rows to it",
        )

    def test_duplicate_keys_within_one_batch_count_once(self):
        summary = load_external_observations(
            self.conn, [_observation("a"), _observation("a", outage=9)]
        )
        self.assertEqual(summary.rows_seen, 2)
        self.assertEqual(summary.rows_loaded, 1)
        self.assertEqual(summary.rows_updated, 0)
        self.assertEqual(external_observation_count(self.conn), 1)


if __name__ == "__main__":
    unittest.main()
