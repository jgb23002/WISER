#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from find_similar_storms import print_results  # noqa: E402

from wiser.agents.data_retrieval_processing_agent import DataRetrievalProcessingAgent
from wiser.agents.rag_knowledge_agent import RAGKnowledgeAgent
from wiser.agents.similarity_query_agent import SimilarityQueryAgent
from wiser.agents.storm_weather_analytics_agent import StormWeatherAnalyticsAgent
from wiser.agents.task_routing_agent import Route, TaskRoutingAgent
from wiser.database import connect, get_table_counts, has_required_tables
from wiser.rag import DEFAULT_RAG_SOURCES, format_excerpt

# ---------------------------------------------------------------------------
# Similarity query parsing (CLI-only; engine and agents stay string-free)
# ---------------------------------------------------------------------------

# User-facing aliases → WeatherData column names.
_ALIAS_TO_FEATURE: dict[str, str] = {
    "wind": "peakWSPD10",
    "windspeed": "peakWSPD10",
    "wind_speed": "peakWSPD10",
    "peakwspd10": "peakWSPD10",
    "precip": "sumAFWA_TOTPRECIP",
    "precipitation": "sumAFWA_TOTPRECIP",
    "sumafwa_totprecip": "sumAFWA_TOTPRECIP",
    "wgt5": "wgt5",
    "wgt9": "wgt9",
    "wgt13": "wgt13",
    "wgt18": "wgt18",
}

_PARAM_PATTERN = re.compile(
    r"(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(?P<value>[0-9]+(?:\.[0-9]*)?)"
)

# Connecticut counties in the current dataset (used for name detection only).
_CT_COUNTIES: tuple[str, ...] = (
    "Fairfield",
    "Hartford",
    "Litchfield",
    "Middlesex",
    "New Haven",
    "New London",
    "Tolland",
    "Windham",
)


def parse_similarity_params(query: str) -> dict[str, float]:
    """Extract feature=value pairs from a natural-language similarity query."""
    normalized = " ".join(query.split())
    params: dict[str, float] = {}
    for match in _PARAM_PATTERN.finditer(normalized):
        alias = match.group("name").lower()
        feature = _ALIAS_TO_FEATURE.get(alias)
        if feature is not None:
            params[feature] = float(match.group("value"))
    return params


def parse_county_name(query: str) -> str | None:
    """Extract county from explicit 'county <name>' phrasing."""
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


def parse_similarity_county(query: str, explicit_county: str | None = None) -> str | None:
    """Resolve county for a similarity query from --county or query text."""
    if explicit_county:
        return " ".join(word.capitalize() for word in explicit_county.strip().split())

    county = parse_county_name(query)
    if county:
        return county

    for known_county in _CT_COUNTIES:
        if re.search(rf"\b{re.escape(known_county)}\b", query, flags=re.IGNORECASE):
            return known_county

    match = re.search(
        r"\b(?:in|for)\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)\b",
        query,
        flags=re.IGNORECASE,
    )
    if match:
        candidate = match.group(1).strip()
        return " ".join(word.capitalize() for word in candidate.split())

    return None


def ensure_database(
    data_agent: DataRetrievalProcessingAgent,
    db_path: str | Path,
) -> sqlite3.Connection:
    conn = connect(db_path)
    if has_required_tables(conn):
        return conn
    conn.close()
    return data_agent.build_database(db_path)


def print_rows(rows: list[dict[str, object]]) -> None:
    if not rows:
        print("No results found.")
        return
    for row in rows:
        fields = ", ".join(f"{key}={value}" for key, value in row.items())
        print(f"  - {fields}")


# ---------------------------------------------------------------------------
# Route handler registry
# ---------------------------------------------------------------------------


@dataclass
class CliContext:
    query: str
    conn: sqlite3.Connection
    limit: int
    county: str | None
    data_agent: DataRetrievalProcessingAgent
    analytics_agent: StormWeatherAnalyticsAgent
    rag_agent: RAGKnowledgeAgent
    similarity_agent: SimilarityQueryAgent
    rebuild_rag: bool
    show_rag_context: bool


def handle_data_retrieval(ctx: CliContext) -> None:
    counts = get_table_counts(ctx.conn)
    print("Database refreshed/available with row counts:")
    for table_name, count in counts.items():
        print(f"  - {table_name}: {count}")


def handle_weather_analytics(ctx: CliContext) -> None:
    text = ctx.query.lower()
    county = parse_county_name(ctx.query)

    if "top storm" in text:
        print(f"Top {ctx.limit} storms by outage:")
        print_rows(
            ctx.analytics_agent.top_storms_by_outage_from_db(ctx.conn, limit=ctx.limit)
        )
        return

    if county:
        print(f"Top {ctx.limit} storm events for county {county}:")
        print_rows(
            ctx.analytics_agent.county_storm_history_from_db(
                ctx.conn,
                county=county,
                limit=ctx.limit,
            )
        )
        return

    print(f"Top {ctx.limit} counties by outage:")
    print_rows(
        ctx.analytics_agent.county_outage_summary_from_db(ctx.conn, limit=ctx.limit)
    )


def handle_similarity_search(ctx: CliContext) -> None:
    query_params = parse_similarity_params(ctx.query)
    if not query_params:
        print(
            "No numeric feature parameters found. "
            "Use key=value pairs such as wind=9.2, precip=45."
        )
        print(
            "Supported aliases: wind, precip, wgt5, wgt9, wgt13, wgt18 "
            "(and full column names)."
        )
        return

    county = parse_similarity_county(ctx.query, ctx.county)
    if not county:
        print(
            "County required. Include a county name in the query "
            "or pass --county Fairfield."
        )
        return

    if "peakWSPD10" in query_params and query_params["peakWSPD10"] <= 0:
        print("Wind speed must be positive (> 0).")
        return

    total = ctx.conn.execute(
        "SELECT COUNT(*) FROM WeatherData WHERE LOWER(county) = LOWER(?)",
        (county,),
    ).fetchone()[0]

    results = ctx.similarity_agent.find_similar(
        ctx.conn, county, query_params, limit=ctx.limit
    )
    print_results(county, query_params, results, total_historical=total)


def handle_rag_knowledge(ctx: CliContext) -> None:
    summary = ctx.rag_agent.ensure_index(ctx.conn, rebuild=ctx.rebuild_rag)
    print(
        "RAG index ready: "
        f"{summary.total_documents} documents, {summary.total_chunks} chunks "
        f"({summary.documents_indexed} refreshed)."
    )
    rag_answer = ctx.rag_agent.answer(ctx.conn, ctx.query, limit=ctx.limit)
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

    if ctx.show_rag_context:
        print()
        print("Augmented prompt/context:")
        print(rag_answer.context)


def handle_unknown(ctx: CliContext) -> None:
    print("Unknown route. Try queries like:")
    print("  - build database from csv")
    print("  - top counties by outage")
    print("  - top storms by outage")
    print("  - history for county Hartford")
    print("  - find storms similar to wind=9.2, precip=45 in Fairfield")
    print("  - explain the storm similarity algorithm")


RouteHandler = Callable[[CliContext], None]

ROUTE_HANDLERS: dict[Route, RouteHandler] = {
    Route.DATA_RETRIEVAL: handle_data_retrieval,
    Route.WEATHER_ANALYTICS: handle_weather_analytics,
    Route.SIMILARITY_SEARCH: handle_similarity_search,
    Route.RAG_KNOWLEDGE: handle_rag_knowledge,
}

DEFAULT_ROUTE_HANDLER: RouteHandler = handle_unknown


def dispatch_route(decision_route: Route, ctx: CliContext) -> None:
    """Invoke the registered handler for a route decision."""
    handler = ROUTE_HANDLERS.get(decision_route, DEFAULT_ROUTE_HANDLER)
    handler(ctx)


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
    args = parser.parse_args()

    routing_agent = TaskRoutingAgent()
    data_agent = DataRetrievalProcessingAgent(args.input)
    analytics_agent = StormWeatherAnalyticsAgent(data_agent)
    similarity_agent = SimilarityQueryAgent()
    rag_sources = tuple(args.rag_source) if args.rag_source else DEFAULT_RAG_SOURCES
    rag_agent = RAGKnowledgeAgent(rag_sources)

    decision = routing_agent.route(args.query)
    print(f"Route: {decision.route.value} ({decision.reason})")

    conn: sqlite3.Connection | None = None
    try:
        conn = ensure_database(data_agent, args.db)
        ctx = CliContext(
            query=args.query,
            conn=conn,
            limit=args.limit,
            county=args.county,
            data_agent=data_agent,
            analytics_agent=analytics_agent,
            rag_agent=rag_agent,
            similarity_agent=similarity_agent,
            rebuild_rag=args.rebuild_rag,
            show_rag_context=args.show_rag_context,
        )
        dispatch_route(decision.route, ctx)
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    main()
