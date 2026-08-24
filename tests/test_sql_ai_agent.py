"""
Tests for SqlAiAgent: dry_run, SQL validation, LLM pipeline, CLI fallback,
and CLI flag behaviour.

All tests are hermetic: LLM calls are mocked via unittest.mock.patch and no
network requests are made, regardless of whether OPENAI_API_KEY or a
repo-root .env is present.  DB tests use a temp-file or in-memory SQLite
database populated from sql/schema.sql, matching the pattern in test_schema.py.
"""

from __future__ import annotations

import io
import os
import sys

import psycopg
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent
SRC_DIR = REPO_ROOT / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"

TESTS_DIR = Path(__file__).parent

for _p in (str(SRC_DIR), str(SCRIPTS_DIR), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pg_harness import make_conn  # noqa: E402

from wiser.agents.sql_ai_agent import (  # noqa: E402
    SQL_CACHE_MAX_ENTRIES,
    SqlAiAgent,
    SqlValidationError,
    _add_generation_options,
    _clean_sql,
    _load_schema_ddl,
    _make_answer,
    _validate_sql,
    clear_sql_cache,
    sql_cache_stats,
)
from wiser.conversation import ConversationTurn  # noqa: E402
from wiser.agents.storm_weather_analytics_agent import StormWeatherAnalyticsAgent  # noqa: E402
from wiser.handlers import handle_weather_analytics  # noqa: E402
from wiser_cli import render_result  # noqa: E402

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_SCHEMA_SQL = SCHEMA_PATH.read_text(encoding="utf-8")

_STORM_ROW = (2012102806, "2012-10-28 06:00:00", 72)
_COUNTY_FAIRFIELD = ("Fairfield", 4887830)
_COUNTY_HARTFORD = ("Hartford", 5869550)
_WD_FAIRFIELD = (2012102806, "Fairfield", 150.5, 12.3, 20, 8, 2, 0, 350)


def _make_memory_conn() -> psycopg.Connection:
    """In-memory DB — suitable for tests that do NOT call _execute_ro."""
    conn = make_conn()
    conn.commit()
    conn.execute("INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(%s, %s, %s)", _STORM_ROW)
    conn.execute("INSERT INTO County(county, ohLine) VALUES(%s, %s)", _COUNTY_FAIRFIELD)
    conn.execute("INSERT INTO County(county, ohLine) VALUES(%s, %s)", _COUNTY_HARTFORD)
    conn.execute(
        "INSERT INTO WeatherData(stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,"
        " wgt5, wgt9, wgt13, wgt18, outage) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        _WD_FAIRFIELD,
    )
    conn.commit()
    return conn


def _make_file_conn() -> psycopg.Connection:
    """Alias kept for call-site compatibility.

    Under SQLite this returned a temp-*file* database because ``_execute_ro``
    reopened the file via a ``file:...?mode=ro`` URI and therefore needed a
    real path. PostgreSQL enforces read-only inside a transaction on the same
    connection, so this is now just ``_make_memory_conn``.
    """
    return _make_memory_conn()


def _make_ctx(query: str, conn: psycopg.Connection, **overrides) -> SimpleNamespace:
    """Minimal HandlerContext-compatible namespace for handler tests."""
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
        use_sql_cache=False,
        history=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# 1. Schema loading
# ---------------------------------------------------------------------------


class TestLoadSchemaDdl(unittest.TestCase):
    def setUp(self):
        self.ddl = _load_schema_ddl(SCHEMA_PATH)

    def test_contains_all_three_create_table_blocks(self):
        for table in ("Storm", "County", "WeatherData"):
            self.assertIn(f"CREATE TABLE {table}", self.ddl)

    def test_strips_pragma_statements(self):
        self.assertNotIn("PRAGMA", self.ddl)

    def test_strips_drop_table_statements(self):
        self.assertNotIn("DROP TABLE", self.ddl)

    def test_includes_create_index_statements(self):
        self.assertIn("CREATE INDEX", self.ddl)

    def test_nonempty_and_reasonable_length(self):
        self.assertGreater(len(self.ddl), 200)


# ---------------------------------------------------------------------------
# 2. _clean_sql
# ---------------------------------------------------------------------------


class TestCleanSql(unittest.TestCase):
    def test_strips_sql_fence_with_newline(self):
        self.assertEqual(_clean_sql("```sql\nSELECT 1;\n```"), "SELECT 1;")

    def test_strips_plain_fence_with_newline(self):
        self.assertEqual(_clean_sql("```\nSELECT 1;\n```"), "SELECT 1;")

    def test_strips_leading_and_trailing_whitespace(self):
        self.assertEqual(_clean_sql("  SELECT 1;  "), "SELECT 1;")

    def test_clean_sql_unchanged(self):
        sql = "SELECT stormCode FROM Storm LIMIT 5;"
        self.assertEqual(_clean_sql(sql), sql)

    def test_fence_without_newline(self):
        result = _clean_sql("```sql SELECT 1;```")
        self.assertNotIn("```", result)
        self.assertIn("SELECT", result)

    # With conversation history in the prompt, the model sometimes echoes the
    # transcript format ("SQL: ...\nAnswer: ..."). These pin the unwrapping.

    def test_strips_sql_label_prefix(self):
        self.assertEqual(_clean_sql("SQL: SELECT 1;"), "SELECT 1;")

    def test_strips_sql_label_case_insensitive(self):
        self.assertEqual(_clean_sql("sql: SELECT 1;"), "SELECT 1;")

    def test_strips_answer_trailer(self):
        raw = "SQL: SELECT 1;\nAnswer: Found 3 results."
        self.assertEqual(_clean_sql(raw), "SELECT 1;")

    def test_strips_multiline_answer_trailer(self):
        raw = "SELECT 1;\nAnswer: Found 3 results.\nMore prose here."
        self.assertEqual(_clean_sql(raw), "SELECT 1;")

    def test_strips_sql_label_inside_fence(self):
        self.assertEqual(_clean_sql("```sql\nSQL: SELECT 1;\n```"), "SELECT 1;")

    def test_strips_sql_label_outside_fence(self):
        self.assertEqual(_clean_sql("SQL: ```sql\nSELECT 1;\n```"), "SELECT 1;")

    def test_answer_in_string_literal_is_preserved(self):
        sql = "SELECT 'Answer: yes' AS label FROM Storm;"
        self.assertEqual(_clean_sql(sql), sql)


# ---------------------------------------------------------------------------
# 2b. _make_answer — previews feed conversation history for anaphora
# ---------------------------------------------------------------------------


class TestMakeAnswer(unittest.TestCase):
    def test_empty_rows(self):
        self.assertEqual(_make_answer([]), "No results found.")

    def test_single_scalar_names_the_value(self):
        self.assertEqual(
            _make_answer([{"total_outage": 350}]), "The total_outage is 350."
        )

    def test_single_row_includes_values(self):
        answer = _make_answer([{"stormCode": 2012102806, "outage": 350}])
        self.assertIn("stormCode=2012102806", answer)
        self.assertIn("outage=350", answer)
        self.assertTrue(answer.startswith("Found 1 matching record"))

    def test_few_rows_all_previewed(self):
        rows = [{"county": "Hartford"}, {"county": "Fairfield"}]
        answer = _make_answer(rows)
        self.assertTrue(answer.startswith("Found 2 results"))
        self.assertIn("county=Hartford", answer)
        self.assertIn("county=Fairfield", answer)

    def test_many_rows_preview_capped_at_three(self):
        rows = [{"n": i} for i in range(10)]
        answer = _make_answer(rows)
        self.assertTrue(answer.startswith("Found 10 results; top 3:"))
        self.assertIn("n=2", answer)
        self.assertNotIn("n=3", answer)


# ---------------------------------------------------------------------------
# 3. _validate_sql — SELECT passes, write statements raise
# ---------------------------------------------------------------------------


class TestValidateSql(unittest.TestCase):
    def test_valid_select_passes(self):
        _validate_sql("SELECT stormCode FROM Storm LIMIT 5;")

    def test_select_with_join_passes(self):
        _validate_sql(
            "SELECT w.outage FROM WeatherData w "
            "JOIN Storm s ON s.stormCode = w.stormCode LIMIT 10;"
        )

    def test_insert_raises(self):
        with self.assertRaises(SqlValidationError):
            _validate_sql("INSERT INTO Storm VALUES(1, '2012-01-01', 24);")

    def test_drop_raises(self):
        with self.assertRaises(SqlValidationError):
            _validate_sql("DROP TABLE Storm;")

    def test_delete_raises(self):
        with self.assertRaises(SqlValidationError):
            _validate_sql("DELETE FROM Storm WHERE stormCode = 1;")

    def test_update_raises(self):
        with self.assertRaises(SqlValidationError):
            _validate_sql("UPDATE Storm SET eventLength = 48 WHERE stormCode = 1;")

    def test_alter_raises(self):
        with self.assertRaises(SqlValidationError):
            _validate_sql("ALTER TABLE Storm ADD COLUMN newcol TEXT;")

    def test_create_raises(self):
        with self.assertRaises(SqlValidationError):
            _validate_sql("CREATE TABLE foo (id INTEGER);")

    def test_multiple_statements_raise(self):
        with self.assertRaises(SqlValidationError):
            _validate_sql("SELECT 1; DROP TABLE Storm;")

    def test_cte_select_passes(self):
        _validate_sql(
            "WITH totals AS (SELECT county, SUM(outage) AS t FROM WeatherData"
            " GROUP BY county) SELECT county, t FROM totals ORDER BY t DESC;"
        )


# ---------------------------------------------------------------------------
# 4. dry_run=True — prompt returned, no HTTP call made
# ---------------------------------------------------------------------------


class TestDryRun(unittest.TestCase):
    def setUp(self):
        self.conn = _make_memory_conn()
        self.agent = SqlAiAgent(dry_run=True)

    def tearDown(self):
        # rollback, not close: the harness owns this shared connection, and
        # closing it makes the next reset_schema pay a fresh TCP+auth.
        self.conn.rollback()

    def test_returns_prompt_in_sql_field(self):
        result = self.agent.run("top storms by outage", self.conn)
        self.assertIn("[SYSTEM]", result["sql"])
        self.assertIn("[USER]", result["sql"])

    def test_dry_run_flag_is_true_in_result(self):
        result = self.agent.run("anything", self.conn)
        self.assertTrue(result["dry_run"])

    def test_rows_is_empty_list(self):
        result = self.agent.run("anything", self.conn)
        self.assertEqual(result["rows"], [])

    def test_answer_is_empty_string(self):
        result = self.agent.run("anything", self.conn)
        self.assertEqual(result["answer"], "")

    def test_no_http_call_made(self):
        with patch("wiser.agents.sql_ai_agent.urlopen") as mock_open:
            self.agent.run("test query", self.conn)
        mock_open.assert_not_called()

    def test_prompt_contains_schema_ddl(self):
        result = self.agent.run("any query", self.conn)
        self.assertIn("Storm", result["sql"])
        self.assertIn("WeatherData", result["sql"])

    def test_prompt_contains_user_query(self):
        query = "What is the total outage for Fairfield?"
        result = self.agent.run(query, self.conn)
        self.assertIn(query, result["sql"])


class TestOpenAIRequestOptions(unittest.TestCase):
    def test_gpt5_models_use_max_completion_tokens(self):
        body = {"model": "gpt-5.5", "messages": []}

        _add_generation_options(body, "gpt-5.5", max_tokens=512, temperature=0)

        self.assertEqual(body["max_completion_tokens"], 512)
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("temperature", body)

    def test_legacy_chat_models_keep_max_tokens(self):
        body = {"model": "gpt-4o-mini", "messages": []}

        _add_generation_options(body, "gpt-4o-mini", max_tokens=512, temperature=0)

        self.assertEqual(body["max_tokens"], 512)
        self.assertEqual(body["temperature"], 0)
        self.assertNotIn("max_completion_tokens", body)


# ---------------------------------------------------------------------------
# 5. Full pipeline — 8 query types with mocked LLM
# ---------------------------------------------------------------------------

_EXAMPLE_QUERIES: list[tuple[str, str]] = [
    (
        "What is the total outage count for Fairfield county?",
        "SELECT SUM(outage) AS total_outage FROM WeatherData WHERE LOWER(county) = 'fairfield';",
    ),
    (
        "Which storms had wind above 9 m/s in Hartford?",
        "SELECT w.stormCode, w.peakWSPD10, w.outage FROM WeatherData w"
        " WHERE LOWER(w.county) = 'hartford' AND w.wgt9 > 0"
        " ORDER BY w.peakWSPD10 DESC LIMIT 10;",
    ),
    (
        "What were the top 5 storms by outage in New Haven?",
        "SELECT w.stormCode, SUM(w.outage) AS total_outage FROM WeatherData w"
        " WHERE LOWER(w.county) = 'new haven'"
        " GROUP BY w.stormCode ORDER BY total_outage DESC LIMIT 5;",
    ),
    (
        "What was the outage count for storm 2012102806?",
        "SELECT w.county, w.outage FROM WeatherData w"
        " JOIN Storm s ON s.stormCode = w.stormCode WHERE s.stormCode = 2012102806;",
    ),
    (
        "How many storms happened between 2010 and 2015?",
        "SELECT COUNT(DISTINCT s.stormCode) AS storm_count FROM Storm s"
        " WHERE EXTRACT(YEAR FROM s.eventDateTime) BETWEEN 2010 AND 2015;",
    ),
    (
        "Which county had more outages, Fairfield or Hartford?",
        "SELECT county, SUM(outage) AS total_outage FROM WeatherData"
        " WHERE LOWER(county) IN ('fairfield', 'hartford')"
        " GROUP BY county ORDER BY total_outage DESC;",
    ),
    (
        "Which storms hit more than one county?",
        "SELECT stormCode, COUNT(DISTINCT county) AS county_count FROM WeatherData"
        " GROUP BY stormCode HAVING COUNT(DISTINCT county) > 1"
        " ORDER BY county_count DESC LIMIT 10;",
    ),
    (
        "Which storms lasted longer than 48 hours?",
        "SELECT s.stormCode, s.eventDateTime, s.eventLength FROM Storm s"
        " WHERE s.eventLength > 48 ORDER BY s.eventLength DESC LIMIT 10;",
    ),
]


class TestMockedLlmPipeline(unittest.TestCase):
    """Full pipeline: LLM mocked to return specific SQL for each query type."""

    def setUp(self):
        self.conn = _make_file_conn()
        self.agent = SqlAiAgent()
        # patch.dict restores any pre-existing key on stop, so a real
        # OPENAI_API_KEY in the environment survives this test class.
        env_patcher = patch.dict("os.environ", {"OPENAI_API_KEY": "test-key-not-real"})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def tearDown(self):
        # The harness owns the shared connection and rebuilds the schema on the
        # next make_conn(); there is no file to unlink.
        self.conn.rollback()

    def _run_with_sql(self, query_text: str, sql: str) -> dict:
        with patch("wiser.agents.sql_ai_agent._call_chat_llm", return_value=sql):
            return self.agent.run(query_text, self.conn)

    def _assert_valid_result(self, result: dict, expected_sql: str) -> None:
        self.assertEqual(result["sql"], expected_sql)
        self.assertIsInstance(result["rows"], list)
        self.assertIsInstance(result["answer"], str)
        self.assertFalse(result["dry_run"])
        for row in result["rows"]:
            self.assertIsInstance(row, dict)

    # --- 8 query types ---

    def test_aggregation_query(self):
        q, sql = _EXAMPLE_QUERIES[0]
        self._assert_valid_result(self._run_with_sql(q, sql), sql)

    def test_filter_query(self):
        q, sql = _EXAMPLE_QUERIES[1]
        self._assert_valid_result(self._run_with_sql(q, sql), sql)

    def test_ranking_query(self):
        q, sql = _EXAMPLE_QUERIES[2]
        self._assert_valid_result(self._run_with_sql(q, sql), sql)

    def test_join_query(self):
        q, sql = _EXAMPLE_QUERIES[3]
        self._assert_valid_result(self._run_with_sql(q, sql), sql)

    def test_date_range_query(self):
        q, sql = _EXAMPLE_QUERIES[4]
        self._assert_valid_result(self._run_with_sql(q, sql), sql)

    def test_comparison_query(self):
        q, sql = _EXAMPLE_QUERIES[5]
        self._assert_valid_result(self._run_with_sql(q, sql), sql)

    def test_multi_county_query(self):
        q, sql = _EXAMPLE_QUERIES[6]
        self._assert_valid_result(self._run_with_sql(q, sql), sql)

    def test_duration_query(self):
        q, sql = _EXAMPLE_QUERIES[7]
        self._assert_valid_result(self._run_with_sql(q, sql), sql)

    # --- Result shape ---

    def test_result_has_all_required_keys(self):
        q, sql = _EXAMPLE_QUERIES[0]
        result = self._run_with_sql(q, sql)
        for key in ("sql", "rows", "answer", "dry_run"):
            self.assertIn(key, result)

    def test_dry_run_false_in_live_call(self):
        q, sql = _EXAMPLE_QUERIES[0]
        self.assertFalse(self._run_with_sql(q, sql)["dry_run"])

    # --- Fence stripping before validation ---

    def test_markdown_fences_stripped_before_validation(self):
        q, sql = _EXAMPLE_QUERIES[0]
        fenced = f"```sql\n{sql}\n```"
        result = self._run_with_sql(q, fenced)
        self.assertEqual(result["sql"], sql)

    # --- Validation failures ---

    def test_empty_llm_response_raises_validation_error(self):
        with patch("wiser.agents.sql_ai_agent._call_chat_llm", return_value=""):
            with self.assertRaises(SqlValidationError):
                self.agent.run("test query", self.conn)

    def test_llm_returns_insert_raises_validation_error(self):
        with patch(
            "wiser.agents.sql_ai_agent._call_chat_llm",
            return_value="INSERT INTO Storm VALUES(1, '2020-01-01', 10);",
        ):
            with self.assertRaises(SqlValidationError):
                self.agent.run("test query", self.conn)

    def test_llm_returns_drop_raises_validation_error(self):
        with patch(
            "wiser.agents.sql_ai_agent._call_chat_llm",
            return_value="DROP TABLE Storm;",
        ):
            with self.assertRaises(SqlValidationError):
                self.agent.run("test query", self.conn)

    # --- API key guard ---

    def test_missing_api_key_raises_validation_error(self):
        # load_dotenv must be neutralised too: with a real repo-root .env it
        # would repopulate OPENAI_API_KEY into the cleared environment and
        # this test would make a live (unmocked) OpenAI call.
        with patch.dict("os.environ", {}, clear=True), \
                patch("wiser.agents.sql_ai_agent.load_dotenv", return_value=0), \
                patch("wiser.agents.sql_ai_agent.urlopen") as mock_open:
            with self.assertRaises(SqlValidationError):
                self.agent.run("top storms", self.conn)
        mock_open.assert_not_called()


# ---------------------------------------------------------------------------
# 6. CLI: handle_weather_analytics falls back on SqlValidationError
# ---------------------------------------------------------------------------


class TestCliHandlerFallback(unittest.TestCase):
    def setUp(self):
        self.conn = _make_memory_conn()

    def tearDown(self):
        # rollback, not close: the harness owns this shared connection, and
        # closing it makes the next reset_schema pay a fresh TCP+auth.
        self.conn.rollback()

    def _capture(self, query: str, side_effect, **ctx_overrides) -> str:
        ctx = _make_ctx(query, self.conn, **ctx_overrides)
        with patch(
            "wiser.agents.sql_ai_agent.SqlAiAgent.run",
            side_effect=side_effect,
        ):
            result = handle_weather_analytics(ctx)
        buf = io.StringIO()
        with redirect_stdout(buf):
            render_result(result)
        return buf.getvalue()

    def test_warning_printed_on_validation_error(self):
        output = self._capture(
            "top storms by outage",
            SqlValidationError("mock failure"),
        )
        self.assertIn("Warning", output)

    def test_fallback_message_mentions_fixed_queries(self):
        output = self._capture(
            "top storms by outage",
            SqlValidationError("mock failure"),
        )
        self.assertIn("fixed queries", output)

    def test_top_storms_fallback_pattern_fires(self):
        output = self._capture(
            "top storms by outage",
            SqlValidationError("mock failure"),
        )
        self.assertIn("Top", output)

    def test_top_counties_fallback_pattern_fires(self):
        output = self._capture(
            "top counties by outage",
            SqlValidationError("mock failure"),
        )
        self.assertIn("Top", output)

    def test_generic_exception_also_triggers_fallback(self):
        output = self._capture(
            "top storms by outage",
            RuntimeError("network timeout"),
        )
        self.assertIn("Warning", output)
        self.assertIn("fixed queries", output)


# ---------------------------------------------------------------------------
# 7. CLI flags: --dry-run-sql and --show-sql
# ---------------------------------------------------------------------------


class TestCliFlags(unittest.TestCase):
    def setUp(self):
        self.conn = _make_memory_conn()

    def tearDown(self):
        # rollback, not close: the harness owns this shared connection, and
        # closing it makes the next reset_schema pay a fresh TCP+auth.
        self.conn.rollback()

    def _render(self, result, **render_kwargs) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            render_result(result, **render_kwargs)
        return buf.getvalue()

    def test_dry_run_sql_prints_prompt_header(self):
        ctx = _make_ctx("top storms", self.conn, dry_run_sql=True)
        with patch("wiser.agents.sql_ai_agent.urlopen") as mock_open:
            result = handle_weather_analytics(ctx)
        mock_open.assert_not_called()
        self.assertIn("dry-run prompt", self._render(result))

    def test_dry_run_sql_output_contains_system_section(self):
        ctx = _make_ctx("top storms", self.conn, dry_run_sql=True)
        result = handle_weather_analytics(ctx)
        self.assertIn("[SYSTEM]", self._render(result))

    def test_show_sql_true_prints_sql_label(self):
        ctx = _make_ctx("top storms", self.conn)
        test_sql = "SELECT stormCode FROM Storm LIMIT 5;"
        with patch(
            "wiser.agents.sql_ai_agent.SqlAiAgent.run",
            return_value={"sql": test_sql, "rows": [], "answer": "", "dry_run": False},
        ):
            result = handle_weather_analytics(ctx)
        output = self._render(result, show_sql=True)
        self.assertIn("SQL:", output)
        self.assertIn(test_sql, output)

    def test_show_sql_false_omits_sql_label(self):
        ctx = _make_ctx("top storms", self.conn)
        test_sql = "SELECT stormCode FROM Storm LIMIT 5;"
        with patch(
            "wiser.agents.sql_ai_agent.SqlAiAgent.run",
            return_value={"sql": test_sql, "rows": [], "answer": "", "dry_run": False},
        ):
            result = handle_weather_analytics(ctx)
        self.assertNotIn("SQL:", self._render(result, show_sql=False))

    def test_answer_printed_when_nonempty(self):
        ctx = _make_ctx("top storms", self.conn)
        with patch(
            "wiser.agents.sql_ai_agent.SqlAiAgent.run",
            return_value={
                "sql": "SELECT 1;",
                "rows": [],
                "answer": "Found 0 results.",
                "dry_run": False,
            },
        ):
            result = handle_weather_analytics(ctx)
        self.assertIn("Found 0 results.", self._render(result))


class TestGeneratedSqlCache(unittest.TestCase):
    """The cache reuses the LLM call, never the rows."""

    SQL = "SELECT county FROM WeatherData LIMIT 1;"

    def setUp(self):
        self.conn = _make_file_conn()
        clear_sql_cache()
        self.addCleanup(clear_sql_cache)
        env_patcher = patch.dict("os.environ", {"OPENAI_API_KEY": "test-key-not-real"})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def tearDown(self):
        self.conn.rollback()

    def test_repeated_question_calls_the_llm_once(self):
        agent = SqlAiAgent(use_cache=True)
        with patch(
            "wiser.agents.sql_ai_agent._call_chat_llm", return_value=self.SQL
        ) as mock_llm:
            first = agent.run("which counties?", self.conn)
            second = agent.run("which counties?", self.conn)
        self.assertEqual(mock_llm.call_count, 1)
        self.assertEqual(first["sql"], second["sql"])
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])

    def test_cache_is_off_by_default(self):
        agent = SqlAiAgent()
        with patch(
            "wiser.agents.sql_ai_agent._call_chat_llm", return_value=self.SQL
        ) as mock_llm:
            agent.run("which counties?", self.conn)
            result = agent.run("which counties?", self.conn)
        self.assertEqual(mock_llm.call_count, 2)
        self.assertFalse(result["cached"])

    def test_rows_are_re_executed_on_a_cache_hit(self):
        """A hit must re-query the database, not replay stale rows."""
        agent = SqlAiAgent(use_cache=True)
        with patch("wiser.agents.sql_ai_agent._call_chat_llm", return_value=self.SQL):
            agent.run("which counties?", self.conn)
            with patch(
                "wiser.agents.sql_ai_agent._execute_ro", return_value=[{"county": "live"}]
            ) as mock_exec:
                second = agent.run("which counties?", self.conn)
        mock_exec.assert_called_once()
        self.assertEqual(second["rows"], [{"county": "live"}])

    def test_differing_history_is_a_different_entry(self):
        agent = SqlAiAgent(use_cache=True)
        turn = ConversationTurn(question="q", sql=self.SQL, answer="a")
        with patch(
            "wiser.agents.sql_ai_agent._call_chat_llm", return_value=self.SQL
        ) as mock_llm:
            agent.run("which counties?", self.conn)
            agent.run("which counties?", self.conn, history=(turn,))
        self.assertEqual(mock_llm.call_count, 2)

    def test_differing_model_is_a_different_entry(self):
        with patch(
            "wiser.agents.sql_ai_agent._call_chat_llm", return_value=self.SQL
        ) as mock_llm:
            SqlAiAgent(model="gpt-4o-mini", use_cache=True).run("q?", self.conn)
            SqlAiAgent(model="gpt-4o", use_cache=True).run("q?", self.conn)
        self.assertEqual(mock_llm.call_count, 2)

    def test_cache_is_shared_across_instances(self):
        with patch(
            "wiser.agents.sql_ai_agent._call_chat_llm", return_value=self.SQL
        ) as mock_llm:
            SqlAiAgent(use_cache=True).run("which counties?", self.conn)
            SqlAiAgent(use_cache=True).run("which counties?", self.conn)
        self.assertEqual(mock_llm.call_count, 1)

    def test_invalid_sql_is_never_cached(self):
        agent = SqlAiAgent(use_cache=True)
        with patch(
            "wiser.agents.sql_ai_agent._call_chat_llm", return_value="DROP TABLE Storm;"
        ):
            with self.assertRaises(SqlValidationError):
                agent.run("drop it", self.conn)
        self.assertEqual(sql_cache_stats()["entries"], 0)

    def test_entries_are_capped(self):
        agent = SqlAiAgent(use_cache=True)
        with patch("wiser.agents.sql_ai_agent._call_chat_llm", return_value=self.SQL):
            for index in range(SQL_CACHE_MAX_ENTRIES + 10):
                agent.run(f"question number {index}?", self.conn)
        self.assertEqual(sql_cache_stats()["entries"], SQL_CACHE_MAX_ENTRIES)

    def test_stats_track_hits_and_misses(self):
        agent = SqlAiAgent(use_cache=True)
        with patch("wiser.agents.sql_ai_agent._call_chat_llm", return_value=self.SQL):
            agent.run("which counties?", self.conn)
            agent.run("which counties?", self.conn)
        stats = sql_cache_stats()
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 1)

    def test_dry_run_never_caches(self):
        agent = SqlAiAgent(dry_run=True, use_cache=True)
        result = agent.run("which counties?", self.conn)
        self.assertFalse(result["cached"])
        self.assertEqual(sql_cache_stats()["entries"], 0)


if __name__ == "__main__":
    unittest.main()
