#!/usr/bin/env python3
"""WISER command-line interface.

All routing, dispatch, and handler logic lives in wiser.handlers (shared with
every other interface); this script only parses arguments and renders the
structured handler results as text.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from find_similar_storms import print_results  # noqa: E402

from wiser.agents.task_routing_agent import Route, TaskRoutingAgent
from wiser.conversation import ConversationTurn, resolve_route
from wiser.database import connect
from wiser.integrations.eagleeye import parse_eagleeye_csv
from wiser.integrations.external_outage import (
    load_external_observations,
    recent_external_observations,
)
from wiser.integrations.national_grid import parse_national_grid_csv
from wiser.handlers import (
    DEFAULT_DSN,
    DEFAULT_INPUT_CSV,
    DEFAULT_LIMIT,
    DEFAULT_MAX_LINKED_PDFS,
    AnalyticsResult,
    DataRetrievalResult,
    HandlerResult,
    RagKnowledgeResult,
    SimilarityResult,
    UnknownRouteResult,
    build_context,
    dispatch_route,
    turn_from_result,
)
from wiser.evaluation import (  # noqa: E402
    evaluate_routing_cases,
    evaluate_sql_ground_truth,
    format_evaluation_summary,
)
from wiser.rag import RagResult, format_excerpt

DEFAULT_ROUTING_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "task_routing_cases.json"
DEFAULT_SQL_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "sql_ai_ground_truth.json"
DEFAULT_SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"
EXTERNAL_SOURCE_PARSERS = {
    "eagleeye": parse_eagleeye_csv,
    "national-grid": parse_national_grid_csv,
}

# ---------------------------------------------------------------------------
# Result renderers (the only printing layer)
# ---------------------------------------------------------------------------


def print_rows(rows: list[dict[str, object]]) -> None:
    if not rows:
        print("No results found.")
        return
    for row in rows:
        fields = ", ".join(f"{key}={value}" for key, value in row.items())
        print(f"  - {fields}")


def load_external_source(
    *,
    dsn: str | None,
    source: str,
    file_path: str,
    preview_limit: int,
) -> None:
    parser = EXTERNAL_SOURCE_PARSERS[source]
    observations = parser(file_path)

    conn = connect(dsn)
    try:
        summary = load_external_observations(conn, observations)
        preview_rows = recent_external_observations(
            conn,
            source_system=summary.source_system,
            limit=preview_limit,
        )
    finally:
        conn.close()

    print("External outage observations loaded:")
    print(f"  - source: {summary.source_system}")
    print(f"  - file: {file_path}")
    print(f"  - rows parsed: {summary.rows_seen}")
    print(f"  - rows loaded: {summary.rows_loaded}")
    print(f"  - rows updated: {summary.rows_updated}")
    print(f"  - source table count: {summary.table_count}")
    if preview_rows:
        print("Recent loaded observations:")
        print_rows(preview_rows)


def _print_sources(cited_results: tuple[RagResult, ...]) -> None:
    print("Sources:")
    for position, result in enumerate(cited_results, start=1):
        print(
            f"{position}. {result.source_path}#chunk-{result.chunk_index} "
            f"(score={result.score:.2f})"
        )
        print(f"   {format_excerpt(result.content, max_chars=420)}")


def _render_data_retrieval(result: DataRetrievalResult) -> None:
    print("Database rebuilt from CSV with row counts:")
    for table_name, count in result.table_counts.items():
        print(f"  - {table_name}: {count}")


def _render_analytics(
    result: AnalyticsResult,
    *,
    show_sql: bool,
    show_reasoning_prompt: bool,
) -> None:
    if result.dry_run_prompt is not None:
        print("--- SQL AI Agent dry-run prompt ---")
        print(result.dry_run_prompt)
        return

    for notice in result.notices:
        print(notice.text)

    # Storm-scoped routes render a report block. The block already contains
    # every figure in a laid-out form, so the rows are not printed again here —
    # dumping machine column names and full-precision floats underneath it only
    # buries the answer. Richer surfaces still get result.display_rows.
    if result.report is not None:
        print(result.report)
        _print_reasoning(result, show_prompt=show_reasoning_prompt)
        return

    # A storm route that produced only notices (unknown or ambiguous storm).
    # The notices above are the whole answer; "No results found." under them
    # adds nothing.
    if result.report_kind is not None:
        return

    if result.used_fallback:
        if result.fallback_title is not None:
            print(result.fallback_title)
            print_rows(result.rows or [])
            _print_reasoning(result, show_prompt=show_reasoning_prompt)
        return

    if show_sql:
        print(f"SQL: {result.sql}")
    if result.answer:
        print(result.answer)
    print_rows(result.rows or [])
    _print_reasoning(result, show_prompt=show_reasoning_prompt)


def _print_reasoning(
    result: AnalyticsResult,
    *,
    show_prompt: bool,
) -> None:
    if not result.reasoning:
        return
    print("Analytical reasoning:")
    print(result.reasoning)
    if result.reasoning_mode:
        print(f"Reasoning mode: {result.reasoning_mode}")
    if show_prompt and result.reasoning_prompt:
        print("Reasoning prompt:")
        print(result.reasoning_prompt)


def _render_similarity(result: SimilarityResult) -> None:
    if not result.ok:
        for notice in result.notices:
            print(notice.text)
        return
    for notice in result.notices:
        print(notice.text)
    if result.report is not None:
        # As above: the forecast report already lays out the analogues.
        print(result.report)
        return
    print_results(
        result.county,
        result.params,
        result.rows,
        total_historical=result.total_historical,
    )


def _render_rag_knowledge(result: RagKnowledgeResult, *, show_rag_context: bool) -> None:
    summary = result.index_summary
    print(
        "RAG index ready: "
        f"{summary.total_documents} documents, {summary.total_chunks} chunks "
        f"({summary.documents_indexed} refreshed)."
    )

    if result.external_summary is not None:
        web_summary = result.external_summary
        print(
            "External storm context ready: "
            f"{web_summary.total_documents} documents, "
            f"{web_summary.total_chunks} chunks "
            f"({web_summary.documents_indexed} refreshed"
            f"{', ' + str(len(web_summary.failures)) + ' failed' if web_summary.failures else ''})."
        )
        print(f"Similarity backend: {result.backend_status}")
        for failure in web_summary.failures[:3]:
            print(f"  - Web source failed: {failure}")

    if result.used_external:
        print(f"Summary mode: {result.summary_mode}")
        print("Storm impact summary:")
        print(result.answer)
        print()
        _print_sources(result.cited_results)
        if show_rag_context:
            print()
            print("Augmented prompt/context:")
            print(result.context)
        return

    if not result.has_results:
        print("No RAG context found for this query.")
        return

    print("Grounded answer:")
    print(result.answer)
    print()
    _print_sources(result.cited_results)

    if show_rag_context:
        print()
        print("Augmented prompt/context:")
        print(result.context)


def _render_unknown(result: UnknownRouteResult) -> None:
    print("Unknown route. Try queries like:")
    for suggestion in result.suggestions:
        print(f"  - {suggestion}")


def render_result(
    result: HandlerResult,
    *,
    show_sql: bool = False,
    show_rag_context: bool = False,
    show_reasoning_prompt: bool = False,
) -> None:
    """Print a handler result exactly as the CLI has always formatted it."""
    if isinstance(result, DataRetrievalResult):
        _render_data_retrieval(result)
    elif isinstance(result, AnalyticsResult):
        _render_analytics(
            result,
            show_sql=show_sql,
            show_reasoning_prompt=show_reasoning_prompt,
        )
    elif isinstance(result, SimilarityResult):
        _render_similarity(result)
    elif isinstance(result, RagKnowledgeResult):
        _render_rag_knowledge(result, show_rag_context=show_rag_context)
    elif isinstance(result, UnknownRouteResult):
        _render_unknown(result)
    else:  # pragma: no cover - defensive
        raise TypeError(f"Unhandled result type: {type(result).__name__}")


# ---------------------------------------------------------------------------
# Query execution (shared by one-shot and interactive modes)
# ---------------------------------------------------------------------------


def run_query_once(
    query: str,
    args: argparse.Namespace,
    routing_agent: TaskRoutingAgent,
    *,
    history: Sequence[ConversationTurn] = (),
    last_route: Route | None = None,
    pooled: bool = False,
) -> tuple[ConversationTurn | None, Route | None]:
    """Route, dispatch, and render one query.

    Returns (turn to append to the conversation history or None, updated
    last route for sticky follow-up routing). ``pooled`` reuses a process
    pool for the connection — worthwhile for the REPL, pointless for a
    one-shot invocation.
    """
    decision = resolve_route(routing_agent.route(query), last_route)
    print(f"Route: {decision.route.value} ({decision.reason})")

    ctx = build_context(
        query,
        input_csv=args.input,
        dsn=args.dsn,
        limit=args.limit,
        county=args.county,
        rag_sources=args.rag_source or None,
        rebuild_rag=args.rebuild_rag,
        web_sources=args.web_source or None,
        crawl_web_sources=args.crawl_web_sources,
        include_linked_pdfs=not args.no_linked_pdfs,
        max_linked_pdfs=args.max_linked_pdfs,
        similarity_backend=args.similarity_backend,
        openai_model=args.openai_model,
        use_openai_summary=not args.no_openai_summary,
        sql_model=args.sql_model,
        dry_run_sql=args.dry_run_sql,
        history=tuple(history),
        pooled=pooled,
    )
    try:
        result = dispatch_route(decision.route, ctx)
        render_result(
            result,
            show_sql=args.show_sql,
            show_rag_context=args.show_rag_context,
            show_reasoning_prompt=args.show_reasoning_prompt,
        )
    finally:
        # Handlers may replace ctx.conn (e.g. a database rebuild); close
        # whichever connection is current.
        ctx.conn.close()

    new_last_route = decision.route if decision.route is not Route.UNKNOWN else last_route
    return turn_from_result(query, result), new_last_route


_REPL_EXIT_WORDS = {"exit", "quit", "q"}


def run_repl(
    args: argparse.Namespace,
    routing_agent: TaskRoutingAgent,
    *,
    initial_query: str | None = None,
) -> None:
    """Interactive multi-turn session; follow-ups reuse conversation context."""
    print("WISER interactive session. Type 'exit' to leave, 'clear' to reset the conversation.")
    history: list[ConversationTurn] = []
    last_route: Route | None = None
    pending = initial_query

    while True:
        if pending is not None:
            query, pending = pending, None
        else:
            try:
                query = input("wiser> ")
            except (EOFError, KeyboardInterrupt):
                print()
                return
        query = query.strip()
        if not query:
            continue
        if query.lower() in _REPL_EXIT_WORDS:
            return
        if query.lower() == "clear":
            history.clear()
            last_route = None
            print("Conversation cleared.")
            continue

        try:
            turn, last_route = run_query_once(
                query,
                args,
                routing_agent,
                history=history,
                last_route=last_route,
                pooled=True,
            )
        except Exception as exc:  # noqa: BLE001 — a failed turn must not kill the session
            print(f"Error: {type(exc).__name__}: {exc}")
            continue
        if turn is not None:
            history.append(turn)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _use_utf8_stdout() -> None:
    """Print UTF-8 regardless of the console codepage.

    Reports contain em dashes and other non-ASCII. On a Windows console still
    running cp1252 (Git Bash, some CI shells) printing one raises
    UnicodeEncodeError and takes the whole command down. Reconfiguring here
    fixes the rendering; errors="replace" makes it degrade to a placeholder
    instead of crashing if the stream cannot be reconfigured at all.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):  # pragma: no cover - stream not seekable
            pass


def main() -> None:
    _use_utf8_stdout()
    parser = argparse.ArgumentParser(description="WISER command-line agent demo.")
    parser.add_argument(
        "query",
        nargs="?",
        help="Natural language query for the routing agent.",
    )
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help=(
            "Start an interactive multi-turn session; follow-up questions "
            "reuse the conversation context. A positional query becomes the "
            "first turn."
        ),
    )
    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT_CSV,
        help="Path to the source CSV file.",
    )
    parser.add_argument(
        "--dsn",
        default=DEFAULT_DSN,
        help=(
            "PostgreSQL connection URL "
            "(default: $DATABASE_URL, else the local Docker Compose instance)."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Limit for top-N queries.",
    )
    parser.add_argument(
        "--county",
        default=None,
        help="County name for similarity search (optional if present in query).",
    )
    parser.add_argument(
        "--rag-source",
        action="append",
        default=[],
        help=(
            "File or directory to include in the RAG index. "
            "Repeat this flag to add multiple sources."
        ),
    )
    parser.add_argument(
        "--rebuild-rag",
        action="store_true",
        help="Rebuild the local RAG index before retrieving context.",
    )
    parser.add_argument(
        "--show-rag-context",
        action="store_true",
        help="Print the full augmented context after the grounded RAG answer.",
    )
    parser.add_argument(
        "--web-source",
        action="append",
        default=[],
        help=(
            "External NOAA/NHC/NCEI URL to crawl for storm-impact context. "
            "Repeat this flag to add multiple sources."
        ),
    )
    parser.add_argument(
        "--crawl-web-sources",
        action="store_true",
        help="Crawl configured external web/PDF sources before RAG retrieval.",
    )
    parser.add_argument(
        "--max-linked-pdfs",
        type=int,
        default=DEFAULT_MAX_LINKED_PDFS,
        help=(
            "Maximum linked PDFs to crawl from each source page "
            f"(default: {DEFAULT_MAX_LINKED_PDFS})."
        ),
    )
    parser.add_argument(
        "--no-linked-pdfs",
        action="store_true",
        help="Index source pages only and skip PDFs linked from those pages.",
    )
    parser.add_argument(
        "--similarity-backend",
        choices=("auto", "faiss", "exact"),
        default="auto",
        help="Text similarity backend for external impact summaries.",
    )
    parser.add_argument(
        "--openai-model",
        default=None,
        help=(
            "OpenAI model for generated impact summaries "
            "(default: WISER_OPENAI_MODEL or gpt-5.5)."
        ),
    )
    parser.add_argument(
        "--no-openai-summary",
        action="store_true",
        help=(
            "Use local extractive summaries and local analytical reasoning even "
            "if OPENAI_API_KEY is set."
        ),
    )
    parser.add_argument(
        "--sql-model",
        default=None,
        help=(
            "OpenAI model for SQL generation only "
            "(default: WISER_OPENAI_MODEL or gpt-4o-mini)."
        ),
    )
    parser.add_argument(
        "--show-sql",
        action="store_true",
        help="Print the generated SQL query alongside the results.",
    )
    parser.add_argument(
        "--dry-run-sql",
        action="store_true",
        help=(
            "Print the prompt that would be sent to the SQL AI Agent, "
            "then exit without making an API call."
        ),
    )
    parser.add_argument(
        "--show-reasoning-prompt",
        action="store_true",
        help="Print the analytical reasoning prompt built from the SQL result rows.",
    )
    parser.add_argument(
        "--evaluate-routing",
        action="store_true",
        help="Run the task-routing ground-truth fixture and print pass/fail metrics.",
    )
    parser.add_argument(
        "--evaluate-sql",
        action="store_true",
        help=(
            "Run the SQL AI ground-truth fixture against its deterministic "
            "SQLite seed database."
        ),
    )
    parser.add_argument(
        "--routing-fixture",
        default=str(DEFAULT_ROUTING_FIXTURE),
        help="Path to the routing ground-truth fixture.",
    )
    parser.add_argument(
        "--sql-fixture",
        default=str(DEFAULT_SQL_FIXTURE),
        help="Path to the SQL AI ground-truth fixture.",
    )
    parser.add_argument(
        "--load-external",
        action="store_true",
        help="Load an EagleEye or National Grid CSV into the external outage staging table.",
    )
    parser.add_argument(
        "--external-source",
        choices=tuple(EXTERNAL_SOURCE_PARSERS),
        help="External outage source name for --load-external.",
    )
    parser.add_argument(
        "--external-file",
        help="CSV file to load for --load-external.",
    )
    parser.add_argument(
        "--external-preview-limit",
        type=int,
        default=5,
        help="Number of recently loaded external rows to print.",
    )
    args = parser.parse_args()

    if args.load_external:
        if not args.external_source:
            parser.error("--external-source is required with --load-external")
        if not args.external_file:
            parser.error("--external-file is required with --load-external")
        load_external_source(
            dsn=args.dsn,
            source=args.external_source,
            file_path=args.external_file,
            preview_limit=args.external_preview_limit,
        )
        return

    if args.evaluate_routing or args.evaluate_sql:
        summaries = []
        if args.evaluate_routing:
            summaries.append(evaluate_routing_cases(args.routing_fixture))
        if args.evaluate_sql:
            summaries.append(
                evaluate_sql_ground_truth(
                    args.sql_fixture,
                    schema_path=DEFAULT_SCHEMA_PATH,
                )
            )
        for summary in summaries:
            print(format_evaluation_summary(summary))
        if any(summary.failed for summary in summaries):
            raise SystemExit(1)
        return

    if not args.query and not args.interactive:
        parser.error("query is required unless --interactive or an evaluation flag is used")

    routing_agent = TaskRoutingAgent()

    if args.interactive:
        run_repl(args, routing_agent, initial_query=args.query)
        return

    run_query_once(args.query, args, routing_agent)


if __name__ == "__main__":
    main()
