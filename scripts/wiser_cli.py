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
from wiser.rag import DEFAULT_RAG_SOURCES, format_excerpt


def ensure_database(
    data_agent: DataRetrievalProcessingAgent,
    db_path: str | Path,
) -> sqlite3.Connection:
    conn = connect(db_path)
    if has_required_tables(conn):
        return conn
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
            rag_answer = rag_agent.answer(conn, args.query, limit=args.limit)
            if not rag_answer.results:
                print("No RAG context found for this query.")
                return

            print("Grounded answer:")
            print(rag_answer.answer)
            print()
            print("Sources:")
            for position, result in enumerate(rag_answer.results, start=1):
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
