"""
Tests for conversational memory: multi-turn prompt construction in SqlAiAgent,
history trimming, sticky routing (resolve_route), the turn_from_result caller
contract, and history threading through handle_weather_analytics.

All tests are hermetic: LLM calls are mocked via unittest.mock.patch and no
network requests are made. DB tests use a temp-file SQLite database populated
from sql/schema.sql, matching the pattern in test_sql_ai_agent.py.
"""

from __future__ import annotations

import io
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).parent.parent
SRC_DIR = REPO_ROOT / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"

for _p in (str(SRC_DIR), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from wiser.agents.sql_ai_agent import (  # noqa: E402
    _SYSTEM_PROMPT,
    SqlAiAgent,
    _build_messages,
    _build_user_message,
)
from wiser.agents.task_routing_agent import Route, RouteDecision  # noqa: E402
from wiser.conversation import (  # noqa: E402
    MAX_HISTORY_TURNS,
    ConversationTurn,
    resolve_route,
)
from wiser.handlers import (  # noqa: E402
    AnalyticsResult,
    SimilarityResult,
    UnknownRouteResult,
    handle_weather_analytics,
    turn_from_result,
)

import wiser_cli  # noqa: E402

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_SCHEMA_SQL = SCHEMA_PATH.read_text(encoding="utf-8")
_SCHEMA_DDL = "CREATE TABLE Storm (stormCode BIGINT PRIMARY KEY);"

_TURN_1 = ConversationTurn(
    question="What is the total outage count for Fairfield county?",
    sql="SELECT SUM(outage) AS total_outage FROM WeatherData WHERE LOWER(county) = 'fairfield';",
    answer="The total_outage is 350.",
)
_TURN_2 = ConversationTurn(
    question="Which storms had wind above 9 m/s?",
    sql="SELECT stormCode FROM WeatherData WHERE wgt9 > 0 LIMIT 10;",
    answer="Found 3 results.",
)


def _make_file_conn() -> tuple[sqlite3.Connection, Path]:
    """Temp-file DB — required for tests that call _execute_ro (needs a real path)."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tmp_path = Path(tmp)
    conn = sqlite3.connect(str(tmp_path))
    conn.executescript(_SCHEMA_SQL)
    conn.execute(
        "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(?, ?, ?)",
        (2012102806, "2012-10-28 06:00:00", 72),
    )
    conn.execute("INSERT INTO County(county, ohLine) VALUES(?, ?)", ("Fairfield", 4887830))
    conn.execute("INSERT INTO County(county, ohLine) VALUES(?, ?)", ("Hartford", 5869550))
    conn.execute(
        "INSERT INTO WeatherData(stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,"
        " wgt5, wgt9, wgt13, wgt18, outage) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (2012102806, "Fairfield", 150.5, 12.3, 20, 8, 2, 0, 350),
    )
    conn.commit()
    return conn, tmp_path


def _make_ctx(query: str, conn: sqlite3.Connection, **overrides) -> SimpleNamespace:
    """Minimal HandlerContext-compatible namespace for handler tests."""
    defaults = dict(
        query=query,
        conn=conn,
        db_path=":memory:",
        limit=5,
        county=None,
        data_agent=None,
        analytics_agent=None,
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


# ---------------------------------------------------------------------------
# 1. _build_messages — multi-turn prompt construction
# ---------------------------------------------------------------------------


class TestBuildMessages(unittest.TestCase):
    def test_two_turns_produce_six_messages_in_order(self):
        messages = _build_messages(
            _SCHEMA_DDL, "what about Hartford?", limit=10, history=(_TURN_1, _TURN_2)
        )
        self.assertEqual(len(messages), 6)
        self.assertEqual(
            [m["role"] for m in messages],
            ["system", "user", "assistant", "user", "assistant", "user"],
        )

    def test_prior_turn_formatting(self):
        messages = _build_messages(
            _SCHEMA_DDL, "what about Hartford?", limit=10, history=(_TURN_1,)
        )
        self.assertEqual(messages[1]["content"], f"Question: {_TURN_1.question}")
        self.assertEqual(
            messages[2]["content"], f"SQL: {_TURN_1.sql}\nAnswer: {_TURN_1.answer}"
        )

    def test_schema_only_in_final_message(self):
        messages = _build_messages(
            _SCHEMA_DDL, "what about Hartford?", limit=10, history=(_TURN_1, _TURN_2)
        )
        for message in messages[:-1]:
            self.assertNotIn("CREATE TABLE", message["content"].replace(_SYSTEM_PROMPT, ""))
        self.assertIn("CREATE TABLE", messages[-1]["content"])
        self.assertIn("Default row limit: 10", messages[-1]["content"])
        self.assertIn("Question: what about Hartford?", messages[-1]["content"])

    def test_no_history_matches_legacy_two_message_prompt(self):
        for history in (None, ()):
            messages = _build_messages(_SCHEMA_DDL, "top storms", limit=5, history=history)
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[0]["content"], _SYSTEM_PROMPT)
            self.assertEqual(
                messages[1]["content"],
                _build_user_message(_SCHEMA_DDL, "top storms", limit=5),
            )

    def test_history_trimmed_to_max_turns(self):
        turns = tuple(
            ConversationTurn(question=f"q{i}", sql=f"SELECT {i};", answer=f"a{i}")
            for i in range(MAX_HISTORY_TURNS + 2)
        )
        messages = _build_messages(_SCHEMA_DDL, "next", limit=10, history=turns)
        self.assertEqual(len(messages), 1 + 2 * MAX_HISTORY_TURNS + 1)
        joined = "\n".join(m["content"] for m in messages)
        self.assertNotIn("Question: q0", joined)
        self.assertNotIn("Question: q1", joined)
        self.assertIn("Question: q2", joined)
        self.assertIn(f"Question: q{MAX_HISTORY_TURNS + 1}", joined)


# ---------------------------------------------------------------------------
# 2. SqlAiAgent.run — history reaches the LLM call; two-turn pipeline
# ---------------------------------------------------------------------------


class TestRunWithHistory(unittest.TestCase):
    def setUp(self):
        self.conn, self.tmp_path = _make_file_conn()
        # LIFO: close the connection first, then unlink (Windows locks open files).
        self.addCleanup(lambda: self.tmp_path.unlink(missing_ok=True))
        self.addCleanup(self.conn.close)
        env_patcher = patch.dict("os.environ", {"OPENAI_API_KEY": "test-key-not-real"})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def test_history_appears_in_llm_messages(self):
        sql = "SELECT SUM(outage) AS total_outage FROM WeatherData WHERE LOWER(county) = 'hartford';"
        with patch(
            "wiser.agents.sql_ai_agent._call_chat_llm", return_value=sql
        ) as mock_llm:
            SqlAiAgent().run(
                "what about Hartford?", self.conn, limit=10, history=(_TURN_1,)
            )
        messages = mock_llm.call_args.kwargs["messages"]
        self.assertEqual(len(messages), 4)
        self.assertEqual(messages[1]["content"], f"Question: {_TURN_1.question}")
        self.assertIn(_TURN_1.sql, messages[2]["content"])

    def test_two_turn_pipeline_feeds_first_sql_into_second_call(self):
        sql1 = "SELECT SUM(outage) AS total_outage FROM WeatherData WHERE LOWER(county) = 'fairfield';"
        sql2 = "SELECT SUM(outage) AS total_outage FROM WeatherData WHERE LOWER(county) = 'hartford';"
        agent = SqlAiAgent()
        with patch(
            "wiser.agents.sql_ai_agent._call_chat_llm", side_effect=[sql1, sql2]
        ) as mock_llm:
            first = agent.run("total outages for Fairfield?", self.conn, limit=10)
            turn = ConversationTurn(
                question="total outages for Fairfield?",
                sql=first["sql"],
                answer=first["answer"],
            )
            second = agent.run(
                "what about Hartford?", self.conn, limit=10, history=(turn,)
            )
        self.assertEqual(first["sql"], sql1)
        self.assertEqual(second["sql"], sql2)
        second_messages = mock_llm.call_args_list[1].kwargs["messages"]
        joined = "\n".join(m["content"] for m in second_messages)
        self.assertIn(sql1, joined)

    def test_dry_run_with_history_shows_assistant_blocks(self):
        agent = SqlAiAgent(dry_run=True)
        result = agent.run("what about Hartford?", self.conn, history=(_TURN_1,))
        self.assertTrue(result["dry_run"])
        self.assertIn("[ASSISTANT]", result["sql"])
        self.assertIn(_TURN_1.sql, result["sql"])

    def test_dry_run_without_history_has_no_assistant_block(self):
        agent = SqlAiAgent(dry_run=True)
        result = agent.run("top storms", self.conn)
        self.assertNotIn("[ASSISTANT]", result["sql"])
        self.assertIn("[SYSTEM]", result["sql"])
        self.assertIn("[USER]", result["sql"])


# ---------------------------------------------------------------------------
# 3. resolve_route — sticky routing
# ---------------------------------------------------------------------------


class TestResolveRoute(unittest.TestCase):
    def test_unknown_after_analytics_sticks_to_analytics(self):
        decision = resolve_route(
            RouteDecision(route=Route.UNKNOWN, reason="No keyword matched."),
            Route.WEATHER_ANALYTICS,
        )
        self.assertIs(decision.route, Route.WEATHER_ANALYTICS)
        self.assertIn("sticky", decision.reason.lower())

    def test_unknown_with_no_previous_route_stays_unknown(self):
        decision = resolve_route(
            RouteDecision(route=Route.UNKNOWN, reason="No keyword matched."), None
        )
        self.assertIs(decision.route, Route.UNKNOWN)

    def test_unknown_after_non_analytics_route_stays_unknown(self):
        decision = resolve_route(
            RouteDecision(route=Route.UNKNOWN, reason="No keyword matched."),
            Route.RAG_KNOWLEDGE,
        )
        self.assertIs(decision.route, Route.UNKNOWN)

    def test_non_unknown_decision_passes_through_unchanged(self):
        original = RouteDecision(route=Route.RAG_KNOWLEDGE, reason="Matched RAG keywords.")
        self.assertIs(resolve_route(original, Route.WEATHER_ANALYTICS), original)


# ---------------------------------------------------------------------------
# 4. turn_from_result — caller contract
# ---------------------------------------------------------------------------


class TestTurnFromResult(unittest.TestCase):
    def test_successful_analytics_result_becomes_turn(self):
        result = AnalyticsResult(sql="SELECT 1;", rows=[{"x": 1}], answer="The x is 1.")
        turn = turn_from_result("what is x?", result)
        self.assertEqual(
            turn,
            ConversationTurn(question="what is x?", sql="SELECT 1;", answer="The x is 1."),
        )

    def test_fallback_result_returns_none(self):
        result = AnalyticsResult(used_fallback=True, fallback_title="Top storms:", rows=[])
        self.assertIsNone(turn_from_result("top storms", result))

    def test_dry_run_result_returns_none(self):
        result = AnalyticsResult(dry_run_prompt="[SYSTEM]\n...")
        self.assertIsNone(turn_from_result("top storms", result))

    def test_non_analytics_results_return_none(self):
        self.assertIsNone(turn_from_result("q", SimilarityResult(ok=True)))
        self.assertIsNone(turn_from_result("q", UnknownRouteResult()))


# ---------------------------------------------------------------------------
# 5. handle_weather_analytics — history threads through to the agent
# ---------------------------------------------------------------------------


class TestHandlerHistoryThreading(unittest.TestCase):
    def test_handler_passes_ctx_history_to_agent_run(self):
        ctx = _make_ctx("what about Hartford?", None, history=(_TURN_1,))
        with patch("wiser.handlers.SqlAiAgent") as mock_agent_cls:
            mock_agent_cls.return_value.run.return_value = {
                "sql": "SELECT 1;",
                "rows": [],
                "answer": "No results found.",
                "dry_run": False,
            }
            result = handle_weather_analytics(ctx)
        self.assertEqual(result.sql, "SELECT 1;")
        run_kwargs = mock_agent_cls.return_value.run.call_args.kwargs
        self.assertEqual(run_kwargs["history"], (_TURN_1,))

    def test_handler_tolerates_context_without_history_field(self):
        ctx = _make_ctx("top storms", None)
        del ctx.history  # simulate a legacy fake context
        with patch("wiser.handlers.SqlAiAgent") as mock_agent_cls:
            mock_agent_cls.return_value.run.return_value = {
                "sql": "SELECT 1;",
                "rows": [],
                "answer": "No results found.",
                "dry_run": False,
            }
            handle_weather_analytics(ctx)
        run_kwargs = mock_agent_cls.return_value.run.call_args.kwargs
        self.assertIsNone(run_kwargs["history"])


# ---------------------------------------------------------------------------
# 6. CLI REPL — history and sticky routing carry across turns
# ---------------------------------------------------------------------------


class TestCliRepl(unittest.TestCase):
    def _run_main(self, argv: list[str], input_effects) -> tuple[MagicMock, MagicMock]:
        """Run wiser_cli.main() with mocked input/dispatch; return the mocks."""
        analytics = AnalyticsResult(
            sql="SELECT SUM(outage) FROM WeatherData;",
            rows=[{"total": 350}],
            answer="Found 1 matching record.",
        )
        fake_ctx = SimpleNamespace(conn=MagicMock())
        with (
            patch.object(sys, "argv", ["wiser_cli.py", *argv]),
            patch("builtins.input", side_effect=input_effects),
            patch.object(wiser_cli, "build_context", return_value=fake_ctx) as mock_build,
            patch.object(wiser_cli, "dispatch_route", return_value=analytics) as mock_dispatch,
            redirect_stdout(io.StringIO()),
        ):
            wiser_cli.main()
        return mock_build, mock_dispatch

    def test_second_turn_receives_first_turns_history(self):
        mock_build, mock_dispatch = self._run_main(
            ["--interactive"],
            ["top counties by outage", "what about Hartford?", "exit"],
        )
        self.assertEqual(mock_build.call_count, 2)
        self.assertEqual(mock_build.call_args_list[0].kwargs["history"], ())
        second_history = mock_build.call_args_list[1].kwargs["history"]
        self.assertEqual(len(second_history), 1)
        self.assertEqual(second_history[0].question, "top counties by outage")
        self.assertEqual(second_history[0].sql, "SELECT SUM(outage) FROM WeatherData;")

    def test_unknown_follow_up_sticks_to_analytics_route(self):
        _, mock_dispatch = self._run_main(
            ["--interactive"],
            ["top counties by outage", "what about Hartford?", "exit"],
        )
        routes = [call.args[0] for call in mock_dispatch.call_args_list]
        self.assertEqual(routes, [Route.WEATHER_ANALYTICS, Route.WEATHER_ANALYTICS])

    def test_clear_resets_history(self):
        mock_build, _ = self._run_main(
            ["--interactive"],
            ["top counties by outage", "clear", "what about Hartford?", "exit"],
        )
        # After clear the follow-up routes to UNKNOWN and carries no history.
        self.assertEqual(mock_build.call_args_list[1].kwargs["history"], ())

    def test_eof_terminates_cleanly(self):
        mock_build, _ = self._run_main(["--interactive"], EOFError())
        self.assertEqual(mock_build.call_count, 0)

    def test_positional_query_seeds_first_repl_turn(self):
        mock_build, _ = self._run_main(
            ["top counties by outage", "--interactive"],
            ["exit"],
        )
        self.assertEqual(mock_build.call_count, 1)
        self.assertEqual(mock_build.call_args.args[0], "top counties by outage")

    def test_failed_turn_keeps_session_alive_and_adds_no_history(self):
        analytics = AnalyticsResult(
            sql="SELECT 1;", rows=[], answer="No results found."
        )
        fake_ctx = SimpleNamespace(conn=MagicMock())
        with (
            patch.object(sys, "argv", ["wiser_cli.py", "--interactive"]),
            patch("builtins.input", side_effect=["top storms", "top counties", "exit"]),
            patch.object(wiser_cli, "build_context", return_value=fake_ctx) as mock_build,
            patch.object(
                wiser_cli,
                "dispatch_route",
                side_effect=[RuntimeError("boom"), analytics],
            ),
            redirect_stdout(io.StringIO()) as buf,
        ):
            wiser_cli.main()
        self.assertEqual(mock_build.call_count, 2)
        self.assertIn("Error: RuntimeError: boom", buf.getvalue())
        self.assertEqual(mock_build.call_args_list[1].kwargs["history"], ())


if __name__ == "__main__":
    unittest.main()
