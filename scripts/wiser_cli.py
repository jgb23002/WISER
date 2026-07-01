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

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from find_similar_storms import print_results  # noqa: E402

from wiser.agents.task_routing_agent import TaskRoutingAgent
from wiser.handlers import (
    DEFAULT_DB_PATH,
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
)
from wiser.rag import RagResult, format_excerpt

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


def _render_analytics(result: AnalyticsResult, *, show_sql: bool) -> None:
    if result.dry_run_prompt is not None:
        print("--- SQL AI Agent dry-run prompt ---")
        print(result.dry_run_prompt)
        return

    for notice in result.notices:
        print(notice.text)

    if result.used_fallback:
        if result.fallback_title is not None:
            print(result.fallback_title)
            print_rows(result.rows or [])
        return

    if show_sql:
        print(f"SQL: {result.sql}")
    if result.answer:
        print(result.answer)
    print_rows(result.rows or [])


def _render_similarity(result: SimilarityResult) -> None:
    if not result.ok:
        for notice in result.notices:
            print(notice.text)
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
            f"({web_summary.documents_indexed} refreshed, "
            f"{len(web_summary.failures)} failed)."
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
) -> None:
    """Print a handler result exactly as the CLI has always formatted it."""
    if isinstance(result, DataRetrievalResult):
        _render_data_retrieval(result)
    elif isinstance(result, AnalyticsResult):
        _render_analytics(result, show_sql=show_sql)
    elif isinstance(result, SimilarityResult):
        _render_similarity(result)
    elif isinstance(result, RagKnowledgeResult):
        _render_rag_knowledge(result, show_rag_context=show_rag_context)
    elif isinstance(result, UnknownRouteResult):
        _render_unknown(result)
    else:  # pragma: no cover - defensive
        raise TypeError(f"Unhandled result type: {type(result).__name__}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="WISER command-line agent demo.")
    parser.add_argument("query", help="Natural language query for the routing agent.")
    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT_CSV,
        help="Path to the source CSV file.",
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help="Path to the SQLite database.",
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
        help="Use the local extractive impact summary even if OPENAI_API_KEY is set.",
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
    args = parser.parse_args()

    routing_agent = TaskRoutingAgent()
    decision = routing_agent.route(args.query)
    print(f"Route: {decision.route.value} ({decision.reason})")

    ctx = build_context(
        args.query,
        input_csv=args.input,
        db_path=args.db,
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
    )
    try:
        result = dispatch_route(decision.route, ctx)
        render_result(
            result,
            show_sql=args.show_sql,
            show_rag_context=args.show_rag_context,
        )
    finally:
        # Handlers may replace ctx.conn (e.g. a database rebuild); close
        # whichever connection is current.
        ctx.conn.close()


if __name__ == "__main__":
    main()
