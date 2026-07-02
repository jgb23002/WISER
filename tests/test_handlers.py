"""Tests for the shared interface-agnostic handler layer (wiser.handlers).

Handlers must never print: every test asserts on the returned result
dataclasses only. LLM calls are mocked; no network requests are made.
"""

from __future__ import annotations

import contextlib
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent
SRC_DIR = REPO_ROOT / "src"
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.agents.rag_knowledge_agent import RAGKnowledgeAgent  # noqa: E402
from wiser.agents.similarity_query_agent import SimilarityQueryAgent  # noqa: E402
from wiser.agents.sql_ai_agent import SqlValidationError  # noqa: E402
from wiser.agents.storm_weather_analytics_agent import StormWeatherAnalyticsAgent  # noqa: E402
from wiser.agents.task_routing_agent import Route  # noqa: E402
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


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA_SQL)
    for storm in _STORM_ROWS:
        conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(?, ?, ?)",
            storm,
        )
    conn.execute("INSERT INTO County(county, ohLine) VALUES(?, ?)", ("Fairfield", 4887830))
    for row in _WEATHER_ROWS:
        conn.execute(
            "INSERT INTO WeatherData(stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,"
            " wgt5, wgt9, wgt13, wgt18, outage) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
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
        new_conn = _make_conn()

        class FakeDataAgent:
            def build_database(self, db_path):
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
        with self.assertRaises(sqlite3.ProgrammingError):
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
        self.conn = sqlite3.connect(":memory:")
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


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


class TestDispatch(unittest.TestCase):
    def test_registry_covers_all_concrete_routes(self):
        expected = {
            Route.DATA_RETRIEVAL,
            Route.WEATHER_ANALYTICS,
            Route.SIMILARITY_SEARCH,
            Route.RAG_KNOWLEDGE,
        }
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
