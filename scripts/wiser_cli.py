#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.agents.data_retrieval_processing_agent import DataRetrievalProcessingAgent
from wiser.agents.rag_knowledge_agent import RAGKnowledgeAgent
from wiser.agents.storm_weather_analytics_agent import StormWeatherAnalyticsAgent
from wiser.agents.task_routing_agent import Route, TaskRoutingAgent
from wiser.database import connect, get_table_counts, has_required_tables
from wiser.external_sources import (
    DEFAULT_STORM_CONTEXT_URLS,
    answer_storm_impact,
    index_external_sources,
    should_use_external_storm_context,
    similarity_backend_status,
)
from wiser.openai_summarizer import openai_config
from wiser.rag import DEFAULT_RAG_SOURCES, format_excerpt


def ensure_database(
    data_agent: DataRetrievalProcessingAgent,
    db_path: str | Path,
) -> sqlite3.Connection:
    conn = connect(db_path)
    try:
        if has_required_tables(conn):
            return conn
    except sqlite3.OperationalError:
        conn.close()
        raise
    conn.close()
    return data_agent.build_database(db_path)


def parse_county_name(query: str) -> str | None:
    # Accept patterns like:
    # - "show county Hartford"
    # - "history for county New Haven"
    match = re.search(
        r"\bcounty\s+((?:[a-zA-Z]+(?:\s+[a-zA-Z]+)*))(?:\s+limit\b|\s*$)",
        query,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    county = match.group(1).strip()
    if not county:
        return None
    return " ".join(word.capitalize() for word in county.split())


def print_rows(rows: list[dict[str, object]]) -> None:
    if not rows:
        print("No results found.")
        return
    for row in rows:
        fields = ", ".join(f"{key}={value}" for key, value in row.items())
        print(f"  - {fields}")


def main() -> None:
    parser = argparse.ArgumentParser(description="WISER command-line agent demo.")
    parser.add_argument("query", help="Natural language query for the routing agent.")
    parser.add_argument(
        "--input",
        default="data/testdata.csv",
        help="Path to the source CSV file.",
    )
    parser.add_argument(
        "--db",
        default="data/wiser.db",
        help="Path to the SQLite database.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Limit for top-N queries.",
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
        default=50,
        help="Maximum linked PDFs to crawl from each source page (default: 50).",
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
    args = parser.parse_args()

    routing_agent = TaskRoutingAgent()
    data_agent = DataRetrievalProcessingAgent(args.input)
    analytics_agent = StormWeatherAnalyticsAgent(data_agent)
    rag_sources = tuple(args.rag_source) if args.rag_source else DEFAULT_RAG_SOURCES
    rag_agent = RAGKnowledgeAgent(rag_sources)

    decision = routing_agent.route(args.query)
    print(f"Route: {decision.route.value} ({decision.reason})")

    conn: sqlite3.Connection | None = None
    try:
        conn = ensure_database(data_agent, args.db)

        if decision.route == Route.DATA_RETRIEVAL:
            counts = get_table_counts(conn)
            print("Database refreshed/available with row counts:")
            for table_name, count in counts.items():
                print(f"  - {table_name}: {count}")
            return

        if decision.route == Route.WEATHER_ANALYTICS:
            text = args.query.lower()
            county = parse_county_name(args.query)

            if "top storm" in text:
                print(f"Top {args.limit} storms by outage:")
                print_rows(
                    analytics_agent.top_storms_by_outage_from_db(conn, limit=args.limit)
                )
                return

            if county:
                print(f"Top {args.limit} storm events for county {county}:")
                print_rows(
                    analytics_agent.county_storm_history_from_db(
                        conn,
                        county=county,
                        limit=args.limit,
                    )
                )
                return

            print(f"Top {args.limit} counties by outage:")
            print_rows(
                analytics_agent.county_outage_summary_from_db(conn, limit=args.limit)
            )
            return

        if decision.route == Route.RAG_KNOWLEDGE:
            summary = rag_agent.ensure_index(conn, rebuild=args.rebuild_rag)
            print(
                "RAG index ready: "
                f"{summary.total_documents} documents, {summary.total_chunks} chunks "
                f"({summary.documents_indexed} refreshed)."
            )

            use_external_context = should_use_external_storm_context(args.query)
            crawl_external_context = args.crawl_web_sources or use_external_context
            web_sources = (
                tuple(args.web_source) if args.web_source else DEFAULT_STORM_CONTEXT_URLS
            )
            if crawl_external_context:
                web_summary = index_external_sources(
                    conn,
                    web_sources,
                    rebuild=args.rebuild_rag,
                    include_linked_pdfs=not args.no_linked_pdfs,
                    max_linked_pdfs=args.max_linked_pdfs,
                )
                print(
                    "External storm context ready: "
                    f"{web_summary.total_documents} documents, "
                    f"{web_summary.total_chunks} chunks "
                    f"({web_summary.documents_indexed} refreshed, "
                    f"{len(web_summary.failures)} failed)."
                )
                print(f"Similarity backend: {similarity_backend_status()}")
                for failure in web_summary.failures[:3]:
                    print(f"  - Web source failed: {failure}")

            if use_external_context:
                impact_answer = answer_storm_impact(
                    conn,
                    args.query,
                    limit=args.limit,
                    backend=args.similarity_backend,
                    use_openai=not args.no_openai_summary,
                    openai_model=args.openai_model,
                    source_urls=web_sources,
                )
                if impact_answer.results:
                    llm_config = openai_config(args.openai_model)
                    if impact_answer.used_openai:
                        print(f"Summary mode: OpenAI Responses API ({llm_config.model})")
                    elif impact_answer.openai_error:
                        print(
                            "Summary mode: local extractive "
                            f"(OpenAI fallback: {impact_answer.openai_error})"
                        )
                    elif args.no_openai_summary:
                        print("Summary mode: local extractive")
                    elif not llm_config.api_key_present:
                        print("Summary mode: local extractive (OPENAI_API_KEY not set)")
                    else:
                        print("Summary mode: local extractive")
                    print("Storm impact summary:")
                    print(impact_answer.answer)
                    print()
                    print("Sources:")
                    for position, result in enumerate(
                        impact_answer.cited_results,
                        start=1,
                    ):
                        print(
                            f"{position}. {result.source_path}#chunk-{result.chunk_index} "
                            f"(score={result.score:.2f})"
                        )
                        print(f"   {format_excerpt(result.content, max_chars=420)}")

                    if args.show_rag_context:
                        print()
                        print("Augmented prompt/context:")
                        print(impact_answer.context)
                    return

            rag_answer = rag_agent.answer(conn, args.query, limit=args.limit)
            if not rag_answer.results:
                print("No RAG context found for this query.")
                return

            print("Grounded answer:")
            print(rag_answer.answer)
            print()
            print("Sources:")
            for position, result in enumerate(rag_answer.cited_results, start=1):
                print(
                    f"{position}. {result.source_path}#chunk-{result.chunk_index} "
                    f"(score={result.score:.2f})"
                )
                print(f"   {format_excerpt(result.content, max_chars=420)}")

            if args.show_rag_context:
                print()
                print("Augmented prompt/context:")
                print(rag_answer.context)
            return

        print("Unknown route. Try queries like:")
        print("  - build database from csv")
        print("  - top counties by outage")
        print("  - top storms by outage")
        print("  - history for county Hartford")
        print("  - explain the storm similarity algorithm")
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    main()
