"""Tests for the shared interface-agnostic handler layer (wiser.handlers).

Handlers must never print: every test asserts on the returned result
dataclasses only. LLM calls are mocked; no network requests are made.
"""

from __future__ import annotations

import contextlib
import io
import sys

import psycopg
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent
SRC_DIR = REPO_ROOT / "src"
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"

TESTS_DIR = Path(__file__).parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from pg_harness import executemany, make_conn, new_connection  # noqa: E402,F401

from wiser.agents.rag_knowledge_agent import RAGKnowledgeAgent  # noqa: E402
from wiser.agents.similarity_query_agent import SimilarityQueryAgent  # noqa: E402
from wiser.agents.sql_ai_agent import SqlValidationError  # noqa: E402
from wiser.agents.storm_weather_analytics_agent import StormWeatherAnalyticsAgent  # noqa: E402
from wiser.agents.task_routing_agent import Route  # noqa: E402
from wiser.external_sources import ExternalSourceIndexSummary  # noqa: E402
from wiser.rag import initialize_rag_schema  # noqa: E402
from wiser.handlers import (  # noqa: E402
    ROUTE_HANDLERS,
    AnalyticsResult,
    DataRetrievalResult,
    RagKnowledgeResult,
    SimilarityResult,
    UnknownRouteResult,
    dispatch_route,
    handle_data_retrieval,
    handle_rag_knowledge,
    handle_similarity_search,
    handle_unknown,
    handle_weather_analytics,
)

_SCHEMA_SQL = SCHEMA_PATH.read_text(encoding="utf-8")

_STORM_ROWS = [
    (2012102806, "2012-10-28 06:00:00", 72),
    (2011082800, "2011-08-28 00:00:00", 36),
]
_WEATHER_ROWS = [
    # stormCode, county, precip, wind, wgt5, wgt9, wgt13, wgt18, outage
    (2012102806, "Fairfield", 150.5, 12.3, 20, 8, 2, 0, 350),
    (2011082800, "Fairfield", 88.0, 9.1, 12, 3, 0, 0, 120),
]


def _make_conn() -> psycopg.Connection:
    conn = make_conn()
    for storm in _STORM_ROWS:
        conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(%s, %s, %s)",
            storm,
        )
    conn.execute("INSERT INTO County(county, ohLine) VALUES(%s, %s)", ("Fairfield", 4887830))
    for row in _WEATHER_ROWS:
        conn.execute(
            "INSERT INTO WeatherData(stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,"
            " wgt5, wgt9, wgt13, wgt18, outage) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            row,
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
    """Handlers must be print-free; fail the test if anything hits stdout."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield
    test.assertEqual(buf.getvalue(), "", "handler printed to stdout")


# ---------------------------------------------------------------------------
# Data retrieval
# ---------------------------------------------------------------------------


class TestHandleDataRetrieval(unittest.TestCase):
    def test_rebuilds_and_returns_table_counts(self):
        old_conn = _make_conn()
        # A second *distinct* connection into the same seeded schema.
        # _make_conn() rebuilds the schema and hands back the shared
        # connection, so calling it twice would return the same object
        # and wipe the seed data.
        new_conn = new_connection()

        class FakeDataAgent:
            def build_database(self, dsn=None):
                return new_conn

        ctx = _make_ctx("build database from csv", old_conn, data_agent=FakeDataAgent())
        with _assert_no_stdout(self):
            result = handle_data_retrieval(ctx)

        self.assertIsInstance(result, DataRetrievalResult)
        self.assertEqual(
            result.table_counts,
            {"Storm": 2, "County": 1, "WeatherData": 2},
        )
        # The old connection was closed and replaced on the context.
        self.assertIs(ctx.conn, new_conn)
        # psycopg reports use of a closed connection as OperationalError.
        with self.assertRaises(psycopg.OperationalError):
            old_conn.execute("SELECT 1")
        new_conn.close()


# ---------------------------------------------------------------------------
# Weather analytics
# ---------------------------------------------------------------------------


class TestHandleWeatherAnalytics(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()

    def tearDown(self):
        self.conn.close()

    def _insert_county_weather(
        self,
        county: str,
        *,
        oh_line: int,
        outage: int,
        wind: float,
        storm_code: int = 2012102806,
    ) -> None:
        self.conn.execute(
            "INSERT INTO County(county, ohLine) VALUES(%s, %s)",
            (county, oh_line),
        )
        self.conn.execute(
            "INSERT INTO WeatherData(stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,"
            " wgt5, wgt9, wgt13, wgt18, outage) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (storm_code, county, 45.0, wind, 4, 1, 0, 0, outage),
        )
        self.conn.commit()

    def _insert_storm_weather(
        self,
        storm_code: int,
        event_datetime: str,
        county: str,
        *,
        outage: int,
        wind: float,
        precip: float,
        wgt9: int = 0,
        wgt13: int = 0,
    ) -> None:
        self.conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(%s, %s, %s)",
            (storm_code, event_datetime, 48),
        )
        self.conn.execute(
            "INSERT INTO WeatherData(stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,"
            " wgt5, wgt9, wgt13, wgt18, outage) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (storm_code, county, precip, wind, wgt9, wgt9, wgt13, 0, outage),
        )
        self.conn.commit()

    def test_llm_success_populates_sql_rows_answer(self):
        payload = {
            "sql": "SELECT 1;",
            "rows": [{"x": 1}],
            "answer": "The x is 1.",
            "dry_run": False,
        }
        ctx = _make_ctx("total outage for Fairfield", self.conn)
        with patch("wiser.agents.sql_ai_agent.SqlAiAgent.run", return_value=payload):
            with _assert_no_stdout(self):
                result = handle_weather_analytics(ctx)
        self.assertIsInstance(result, AnalyticsResult)
        self.assertFalse(result.used_fallback)
        self.assertEqual(result.sql, "SELECT 1;")
        self.assertEqual(result.rows, [{"x": 1}])
        self.assertEqual(result.answer, "The x is 1.")
        self.assertEqual(result.notices, [])

    def test_dry_run_returns_prompt_without_llm_call(self):
        ctx = _make_ctx("top storms", self.conn, dry_run_sql=True)
        with patch("wiser.agents.sql_ai_agent.urlopen") as mock_open:
            with _assert_no_stdout(self):
                result = handle_weather_analytics(ctx)
        mock_open.assert_not_called()
        self.assertIn("[SYSTEM]", result.dry_run_prompt)
        self.assertIsNone(result.sql)

    def test_validation_error_falls_back_with_warning_notice(self):
        ctx = _make_ctx("top storms by outage", self.conn)
        with patch(
            "wiser.agents.sql_ai_agent.SqlAiAgent.run",
            side_effect=SqlValidationError("mock failure"),
        ):
            with _assert_no_stdout(self):
                result = handle_weather_analytics(ctx)
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.fallback_title, "Top 5 storms by outage:")
        self.assertEqual(len(result.rows), 2)
        self.assertEqual(result.notices[0].level, "warning")
        self.assertIn("falling back to fixed queries", result.notices[0].text)

    def test_generic_exception_also_falls_back(self):
        ctx = _make_ctx("top counties by outage", self.conn)
        with patch(
            "wiser.agents.sql_ai_agent.SqlAiAgent.run",
            side_effect=RuntimeError("network timeout"),
        ):
            result = handle_weather_analytics(ctx)
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.fallback_title, "Top 5 counties by outage:")
        self.assertIn("RuntimeError", result.notices[0].text)

    def test_county_history_fallback(self):
        ctx = _make_ctx("history for county Fairfield", self.conn)
        with patch(
            "wiser.agents.sql_ai_agent.SqlAiAgent.run",
            side_effect=SqlValidationError("mock failure"),
        ):
            result = handle_weather_analytics(ctx)
        self.assertEqual(
            result.fallback_title, "Top 5 storm events for county Fairfield:"
        )
        self.assertEqual(len(result.rows), 2)

    def test_unsupported_fallback_query_returns_guidance_notice(self):
        ctx = _make_ctx("compare outage trends", self.conn)
        with patch(
            "wiser.agents.sql_ai_agent.SqlAiAgent.run",
            side_effect=SqlValidationError("mock failure"),
        ):
            result = handle_weather_analytics(ctx)
        self.assertTrue(result.used_fallback)
        self.assertIsNone(result.fallback_title)
        self.assertIsNone(result.rows)
        self.assertEqual(len(result.notices), 2)
        self.assertIn("I can't answer this", result.notices[1].text)

    def test_affected_most_fallback_returns_reasoning_rows(self):
        ctx = _make_ctx("which county was affected the most overall?", self.conn)
        with patch(
            "wiser.agents.sql_ai_agent.SqlAiAgent.run",
            side_effect=SqlValidationError("mock failure"),
        ):
            result = handle_weather_analytics(ctx)

        self.assertTrue(result.used_fallback)
        self.assertEqual(result.fallback_title, "County impact summary:")
        self.assertEqual(result.rows[0]["county"], "Fairfield")
        self.assertIn("total_outage", result.rows[0])
        self.assertIn("Fairfield", result.reasoning)

    def test_population_normalization_fallback_uses_line_exposure(self):
        ctx = _make_ctx("normalize outages by population", self.conn)
        with patch(
            "wiser.agents.sql_ai_agent.SqlAiAgent.run",
            side_effect=SqlValidationError("mock failure"),
        ):
            result = handle_weather_analytics(ctx)

        self.assertTrue(result.used_fallback)
        self.assertIn("normalized", result.fallback_title)
        self.assertIn("outage_per_million_ohline", result.rows[0])
        self.assertIn("Population is not in the current WISER schema", result.notices[1].text)
        self.assertIn("Population is not present", result.reasoning)

    def test_highest_wind_fallback_returns_wind_and_outage_summary(self):
        ctx = _make_ctx(
            "which county had the highest maximum wind and did it also have the highest outages?",
            self.conn,
        )
        with patch(
            "wiser.agents.sql_ai_agent.SqlAiAgent.run",
            side_effect=SqlValidationError("mock failure"),
        ):
            result = handle_weather_analytics(ctx)

        self.assertTrue(result.used_fallback)
        self.assertEqual(result.fallback_title, "County wind and outage summary:")
        self.assertIn("max_peak_wind", result.rows[0])
        self.assertIn("total_outage", result.rows[0])

    def test_why_despite_fallback_returns_county_comparison(self):
        self._insert_county_weather(
            "Hartford",
            oh_line=5869550,
            outage=500,
            wind=9.0,
        )
        ctx = _make_ctx(
            "for storm 2012102806, why did Hartford have more outages than "
            "Fairfield despite lower wind?",
            self.conn,
        )
        with patch(
            "wiser.agents.sql_ai_agent.SqlAiAgent.run",
            side_effect=SqlValidationError("mock failure"),
        ):
            result = handle_weather_analytics(ctx)

        self.assertTrue(result.used_fallback)
        self.assertEqual(result.fallback_title, "County comparison for analytical reasoning:")
        self.assertEqual(result.rows[0]["county"], "Hartford")
        self.assertEqual(result.rows[0]["stormCode"], 2012102806)
        self.assertIn("non-linear outage relationship", result.reasoning)

    def test_sponsor_month_comparison_bypasses_sql_ai(self):
        self._insert_storm_weather(
            2026020100,
            "2026-02-01 00:00:00",
            "Fairfield",
            outage=50,
            wind=5.0,
            precip=10.0,
        )
        self._insert_storm_weather(
            2026070100,
            "2026-07-01 00:00:00",
            "Fairfield",
            outage=500,
            wind=12.0,
            precip=40.0,
            wgt9=4,
            wgt13=1,
        )
        ctx = _make_ctx(
            "how does storms in July and Februrary relate to storms",
            self.conn,
        )

        with patch("wiser.agents.sql_ai_agent.SqlAiAgent.run") as mock_sql:
            result = handle_weather_analytics(ctx)

        mock_sql.assert_not_called()
        self.assertEqual(result.report_kind, "storm_group_analysis")
        self.assertIn("July", result.report)
        self.assertIn("February", result.report)
        self.assertTrue(result.display_rows)
        self.assertTrue(any(chart.kind == "histogram" for chart in result.charts))

    def test_sponsor_threshold_query_returns_matches_and_charts(self):
        ctx = _make_ctx("which storms had wind above 20mph in Fairfield?", self.conn)

        result = handle_weather_analytics(ctx)

        self.assertEqual(result.report_kind, "threshold_analysis")
        self.assertEqual(result.rows[0]["stormCode"], 2012102806)
        self.assertEqual(result.rows[0]["county"], "Fairfield")
        self.assertEqual({row["county"] for row in result.rows}, {"Fairfield"})
        self.assertTrue(any(chart.kind == "scatter" for chart in result.charts))

    def test_sponsor_region_comparison_uses_default_regions(self):
        self._insert_county_weather(
            "Hartford",
            oh_line=5869550,
            outage=500,
            wind=9.0,
        )
        ctx = _make_ctx("compare northern and southern storm outages", self.conn)

        result = handle_weather_analytics(ctx)

        self.assertEqual(result.report_kind, "storm_group_analysis")
        groups = {row["group"] for row in result.rows}
        self.assertIn("Northern CT", groups)
        self.assertIn("Southern CT", groups)
        self.assertTrue(result.charts)

    def test_this_month_query_uses_current_weather_when_loaded(self):
        today = date.today()
        observed_at = f"{today.year:04d}-{today.month:02d}-11 14:00:00"
        self.conn.execute(
            """
            INSERT INTO CurrentWeatherObservation(
                sourceSystem,
                sourceRecordId,
                observedAt,
                county,
                state,
                peakWSPD10,
                peakGUST10,
                sumAFWA_TOTPRECIP,
                humidity,
                outage,
                rawPayload
            ) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                "mock_feed",
                "obs-1",
                observed_at,
                "Fairfield",
                "CT",
                9.0,
                17.0,
                10.0,
                82.0,
                44,
                "{}",
            ),
        )
        self.conn.commit()
        ctx = _make_ctx("does the weather this month caused more outages?", self.conn)

        result = handle_weather_analytics(ctx)

        self.assertEqual(result.report_kind, "storm_group_analysis")
        self.assertEqual(result.rows[0]["source"], "current_weather")
        self.assertEqual(result.rows[0]["total_outage"], 44)
        self.assertTrue(
            any("Using staged current-weather observations" in notice.text for notice in result.notices)
        )

    def test_humidity_threshold_uses_current_weather_when_loaded(self):
        today = date.today()
        observed_at = f"{today.year:04d}-{today.month:02d}-11 14:00:00"
        self.conn.execute(
            """
            INSERT INTO CurrentWeatherObservation(
                sourceSystem,
                sourceRecordId,
                observedAt,
                county,
                state,
                peakWSPD10,
                peakGUST10,
                sumAFWA_TOTPRECIP,
                humidity,
                outage,
                rawPayload
            ) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                "mock_feed",
                "obs-1",
                observed_at,
                "Fairfield",
                "CT",
                9.0,
                17.0,
                10.0,
                82.0,
                44,
                "{}",
            ),
        )
        self.conn.commit()
        ctx = _make_ctx("which storms had humidity above 80%?", self.conn)

        result = handle_weather_analytics(ctx)

        self.assertEqual(result.report_kind, "threshold_analysis")
        self.assertEqual(result.rows[0]["source"], "current_weather")
        self.assertEqual(result.rows[0]["metric_value"], 82.0)

    def test_humidity_threshold_returns_current_dataset_gap(self):
        ctx = _make_ctx("which storms had humidity above 80%?", self.conn)

        result = handle_weather_analytics(ctx)

        self.assertEqual(result.report_kind, "threshold_analysis")
        self.assertEqual(result.rows, [])
        self.assertEqual(result.notices[0].level, "error")
        self.assertIn("humidity observations", result.notices[0].text)


# ---------------------------------------------------------------------------
# Similarity search
# ---------------------------------------------------------------------------


class TestHandleSimilaritySearch(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()

    def tearDown(self):
        self.conn.close()

    def test_happy_path_returns_ranked_rows(self):
        ctx = _make_ctx(
            "find storms similar to wind=9.2, precip=45 in Fairfield", self.conn
        )
        with _assert_no_stdout(self):
            result = handle_similarity_search(ctx)
        self.assertIsInstance(result, SimilarityResult)
        self.assertTrue(result.ok)
        self.assertEqual(result.county, "Fairfield")
        self.assertEqual(
            result.params, {"peakWSPD10": 9.2, "sumAFWA_TOTPRECIP": 45.0}
        )
        self.assertEqual(result.total_historical, 2)
        self.assertEqual(len(result.rows), 2)
        self.assertIn("similarity", result.rows[0])

    def test_explicit_county_override(self):
        ctx = _make_ctx("find storms similar to wind=9.2", self.conn, county="fairfield")
        result = handle_similarity_search(ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.county, "Fairfield")

    def test_missing_params_is_error(self):
        ctx = _make_ctx("find similar storms", self.conn)
        with _assert_no_stdout(self):
            result = handle_similarity_search(ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.rows, [])
        self.assertEqual(result.notices[0].level, "error")
        self.assertIn("No numeric feature parameters found", result.notices[0].text)

    def test_missing_county_is_error(self):
        ctx = _make_ctx("find storms similar to wind=9.2", self.conn)
        result = handle_similarity_search(ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.notices[0].level, "error")
        self.assertIn("County required", result.notices[0].text)

    def test_zero_wind_is_error(self):
        ctx = _make_ctx("find storms similar to wind=0 in Fairfield", self.conn)
        result = handle_similarity_search(ctx)
        self.assertFalse(result.ok)
        self.assertIn("Wind speed must be positive", result.notices[0].text)


# ---------------------------------------------------------------------------
# RAG knowledge
# ---------------------------------------------------------------------------


class TestHandleRagKnowledge(unittest.TestCase):
    def setUp(self):
        self.conn = make_conn()
        self.tmpdir = tempfile.TemporaryDirectory()
        docs_dir = Path(self.tmpdir.name) / "docs"
        docs_dir.mkdir()
        (docs_dir / "algorithm.md").write_text(
            "# Similarity Algorithm\n\n"
            "The similarity algorithm standardizes each weather feature to "
            "Z-scores and ranks historical storms by weighted L2 distance.\n",
            encoding="utf-8",
        )
        self.rag_agent = RAGKnowledgeAgent(sources=("docs",), root=self.tmpdir.name)

    def tearDown(self):
        self.conn.close()
        self.tmpdir.cleanup()

    def test_local_answer_populates_result(self):
        ctx = _make_ctx(
            "explain the similarity algorithm", self.conn, rag_agent=self.rag_agent
        )
        with _assert_no_stdout(self):
            result = handle_rag_knowledge(ctx)
        self.assertIsInstance(result, RagKnowledgeResult)
        self.assertTrue(result.has_results)
        self.assertFalse(result.used_external)
        self.assertIsNone(result.external_summary)
        self.assertGreaterEqual(result.index_summary.total_documents, 1)
        self.assertTrue(result.answer)
        self.assertTrue(result.cited_results)
        # Context is always populated; showing it is a render-time decision.
        self.assertTrue(result.context)

    def test_no_matching_context_sets_has_results_false(self):
        ctx = _make_ctx(
            "explain quixotic zymurgy", self.conn, rag_agent=self.rag_agent
        )
        result = handle_rag_knowledge(ctx)
        self.assertFalse(result.has_results)
        self.assertFalse(result.used_external)

    def _index_external_chunk(self):
        initialize_rag_schema(self.conn)
        cursor = self.conn.execute(
            "INSERT INTO RagDocument(path, title, checksum, indexedAt)"
            " VALUES(%s, %s, %s,"
            "        to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'))"
            " RETURNING id",
            ("https://example.gov/report", "Storm Report", "abc123"),
        )
        document_id = cursor.fetchone()[0]
        self.conn.execute(
            "INSERT INTO RagChunk(documentId, chunkIndex, content, tokenCount)"
            " VALUES(%s, %s, %s, %s)",
            (
                document_id,
                0,
                "Tropical Storm Isaias caused widespread power outages and "
                "wind damage across Connecticut counties in August 2020.",
                16,
            ),
        )
        self.conn.commit()

    def test_impact_query_reuses_indexed_external_context(self):
        self._index_external_chunk()
        ctx = _make_ctx(
            "impact of the new storm in Connecticut",
            self.conn,
            rag_agent=self.rag_agent,
        )
        with patch("wiser.handlers.index_external_sources") as mock_crawl:
            result = handle_rag_knowledge(ctx)
        mock_crawl.assert_not_called()
        self.assertTrue(result.used_external)
        self.assertTrue(result.has_results)
        self.assertIsNone(result.external_summary)
        self.assertTrue(
            any("previously indexed" in notice.text for notice in result.notices)
        )

    def test_impact_query_crawls_when_no_external_context(self):
        ctx = _make_ctx(
            "impact of the new storm in Connecticut",
            self.conn,
            rag_agent=self.rag_agent,
        )
        empty_summary = ExternalSourceIndexSummary(
            documents_seen=0,
            documents_indexed=0,
            chunks_indexed=0,
            total_documents=0,
            total_chunks=0,
            failures=(),
        )
        with patch(
            "wiser.handlers.index_external_sources", return_value=empty_summary
        ) as mock_crawl:
            result = handle_rag_knowledge(ctx)
        mock_crawl.assert_called_once()
        self.assertIs(result.external_summary, empty_summary)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


class TestDispatch(unittest.TestCase):
    def test_registry_covers_all_concrete_routes(self):
        # Derived from Route rather than frozen as a list: the invariant is
        # "every route except UNKNOWN has a handler", so adding a route should
        # fail here until it is wired up, not require editing this expectation.
        expected = set(Route) - {Route.UNKNOWN}
        self.assertEqual(set(ROUTE_HANDLERS.keys()), expected)

    def test_unknown_route_returns_suggestions(self):
        ctx = _make_ctx("hello world", None)
        result = dispatch_route(Route.UNKNOWN, ctx)
        self.assertIsInstance(result, UnknownRouteResult)
        self.assertTrue(result.suggestions)

    def test_handle_unknown_directly(self):
        result = handle_unknown(_make_ctx("anything", None))
        self.assertIn("top counties by outage", result.suggestions)

    def test_dispatch_returns_handler_result(self):
        conn = _make_conn()
        try:
            ctx = _make_ctx(
                "find storms similar to wind=9.2 in Fairfield", conn
            )
            result = dispatch_route(Route.SIMILARITY_SEARCH, ctx)
            self.assertIsInstance(result, SimilarityResult)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
