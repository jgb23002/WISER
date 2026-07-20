"""Interface-agnostic route handlers returning structured results.

This module is the single implementation of the route → handler dispatch
used by every WISER interface (CLI, Streamlit, ...). Handlers never print;
they return one of the result dataclasses below and each interface renders
them however it likes. Natural-language parsing for similarity queries also
lives here so the engine and agents stay string-free.

No interface-specific concerns (argparse, Streamlit widgets) belong here.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence, Union

from wiser.agents.data_retrieval_processing_agent import DataRetrievalProcessingAgent
from wiser.agents.rag_knowledge_agent import RAGKnowledgeAgent
from wiser.agents.similarity_query_agent import SimilarityQueryAgent
from wiser.agents.sql_ai_agent import SqlAiAgent, SqlValidationError
from wiser.agents.storm_reasoning_agent import StormReasoningAgent
from wiser.agents.storm_weather_analytics_agent import StormWeatherAnalyticsAgent
from wiser.agents.task_routing_agent import Route
from wiser.cli_helpers import FEATURE_ALIASES, validate_wind_positive
from wiser.conversation import ConversationTurn
from wiser.database import connect, get_table_counts, has_required_tables
from wiser.external_sources import (
    DEFAULT_STORM_CONTEXT_URLS,
    ExternalSourceIndexSummary,
    answer_storm_impact,
    has_external_context,
    index_external_sources,
    should_use_external_storm_context,
    similarity_backend_status,
)
from wiser.openai_summarizer import openai_config
from wiser.query_engine import county_historical_count
from wiser.rag import DEFAULT_RAG_SOURCES, RagIndexSummary, RagResult
from wiser.visualization import county_metric_rows

# Shared defaults; the CLI argparse flags and the Streamlit sidebar both
# reference these so interface defaults cannot drift apart.
DEFAULT_INPUT_CSV = "data/testdata.csv"
DEFAULT_DB_PATH = "data/wiser.db"
DEFAULT_LIMIT = 10
DEFAULT_MAX_LINKED_PDFS = 50

EXAMPLE_QUERIES: tuple[str, ...] = (
    "build database from csv",
    "which county was affected the most overall?",
    "for storm 2020080406, why did Hartford have more outages than New Haven despite lower wind?",
    "top counties by outage",
    "normalize county outages by overhead line exposure",
    "top storms by outage",
    "history for county Hartford",
    "find storms similar to wind=9.2, precip=45 in Fairfield",
    "explain the storm similarity algorithm",
    "impact of the new storm in Connecticut",
)

# ---------------------------------------------------------------------------
# Similarity query parsing (interface layer; engine and agents stay string-free)
# ---------------------------------------------------------------------------

# User-facing aliases → WeatherData column names.
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
        feature = FEATURE_ALIASES.get(alias)
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
    """Resolve county for a similarity query from an explicit value or query text."""
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


# ---------------------------------------------------------------------------
# Context and result types
# ---------------------------------------------------------------------------


@dataclass
class HandlerContext:
    """Everything a handler needs to answer a query.

    Render-only options (show SQL, show RAG context) are deliberately NOT
    here: they never change what a handler computes, only how an interface
    displays the result.
    """

    query: str
    conn: sqlite3.Connection
    db_path: str
    limit: int
    county: str | None
    data_agent: DataRetrievalProcessingAgent
    analytics_agent: StormWeatherAnalyticsAgent
    rag_agent: RAGKnowledgeAgent
    similarity_agent: SimilarityQueryAgent
    rebuild_rag: bool
    web_sources: tuple[str, ...]
    crawl_web_sources: bool
    include_linked_pdfs: bool
    max_linked_pdfs: int
    similarity_backend: str
    openai_model: str | None
    use_openai_summary: bool
    sql_model: str | None = None
    dry_run_sql: bool = False
    history: tuple[ConversationTurn, ...] | None = None


@dataclass
class Notice:
    """A status/guidance line. The CLI prints the text verbatim; richer
    interfaces may use the level to pick a warning/error presentation."""

    text: str
    level: str = "info"  # "info" | "warning" | "error"


@dataclass
class DataRetrievalResult:
    table_counts: dict[str, int]


@dataclass
class AnalyticsResult:
    sql: str | None = None
    rows: list[dict[str, object]] | None = None
    answer: str = ""
    reasoning: str = ""
    reasoning_prompt: str | None = None
    reasoning_mode: str | None = None
    dry_run_prompt: str | None = None
    used_fallback: bool = False
    fallback_title: str | None = None
    notices: list[Notice] = field(default_factory=list)


@dataclass
class SimilarityResult:
    ok: bool
    county: str | None = None
    params: dict[str, float] = field(default_factory=dict)
    rows: list[dict[str, object]] = field(default_factory=list)
    total_historical: int = 0
    notices: list[Notice] = field(default_factory=list)


@dataclass
class RagKnowledgeResult:
    index_summary: RagIndexSummary
    external_summary: ExternalSourceIndexSummary | None = None
    backend_status: str | None = None
    used_external: bool = False
    summary_mode: str | None = None
    answer: str = ""
    cited_results: tuple[RagResult, ...] = ()
    context: str = ""
    has_results: bool = False
    notices: list[Notice] = field(default_factory=list)


@dataclass
class UnknownRouteResult:
    suggestions: tuple[str, ...] = EXAMPLE_QUERIES


HandlerResult = Union[
    DataRetrievalResult,
    AnalyticsResult,
    SimilarityResult,
    RagKnowledgeResult,
    UnknownRouteResult,
]


# ---------------------------------------------------------------------------
# Database bootstrap
# ---------------------------------------------------------------------------


def ensure_database(
    data_agent: DataRetrievalProcessingAgent,
    db_path: str | Path,
) -> sqlite3.Connection:
    conn = connect(db_path)
    if has_required_tables(conn):
        return conn
    conn.close()
    return data_agent.build_database(db_path)


def build_context(
    query: str,
    *,
    input_csv: str = DEFAULT_INPUT_CSV,
    db_path: str = DEFAULT_DB_PATH,
    limit: int = DEFAULT_LIMIT,
    county: str | None = None,
    rag_sources: Sequence[str] | None = None,
    rebuild_rag: bool = False,
    web_sources: Sequence[str] | None = None,
    crawl_web_sources: bool = False,
    include_linked_pdfs: bool = True,
    max_linked_pdfs: int = DEFAULT_MAX_LINKED_PDFS,
    similarity_backend: str = "auto",
    openai_model: str | None = None,
    use_openai_summary: bool = True,
    sql_model: str | None = None,
    dry_run_sql: bool = False,
    history: Sequence[ConversationTurn] | None = None,
) -> HandlerContext:
    """Construct agents, open (or bootstrap) the database, and bundle a context.

    The caller owns ctx.conn and must close it — note handlers may replace it
    (a database rebuild), so close whichever connection is current.
    """
    data_agent = DataRetrievalProcessingAgent(input_csv)
    rag_agent = RAGKnowledgeAgent(
        tuple(rag_sources) if rag_sources else DEFAULT_RAG_SOURCES
    )
    conn = ensure_database(data_agent, db_path)
    return HandlerContext(
        query=query,
        conn=conn,
        db_path=db_path,
        limit=limit,
        county=county,
        data_agent=data_agent,
        analytics_agent=StormWeatherAnalyticsAgent(),
        rag_agent=rag_agent,
        similarity_agent=SimilarityQueryAgent(),
        rebuild_rag=rebuild_rag,
        web_sources=(
            tuple(web_sources) if web_sources else DEFAULT_STORM_CONTEXT_URLS
        ),
        crawl_web_sources=crawl_web_sources,
        include_linked_pdfs=include_linked_pdfs,
        max_linked_pdfs=max_linked_pdfs,
        similarity_backend=similarity_backend,
        openai_model=openai_model,
        use_openai_summary=use_openai_summary,
        sql_model=sql_model,
        dry_run_sql=dry_run_sql,
        history=tuple(history) if history else None,
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def handle_data_retrieval(ctx: HandlerContext) -> DataRetrievalResult:
    # Rebuild from CSV even when the database already exists — schema.sql
    # drops and recreates the tables, so this is a true refresh.
    ctx.conn.close()
    ctx.conn = ctx.data_agent.build_database(ctx.db_path)
    return DataRetrievalResult(table_counts=get_table_counts(ctx.conn))


def handle_weather_analytics(ctx: HandlerContext) -> AnalyticsResult:
    agent = SqlAiAgent(model=ctx.sql_model, dry_run=ctx.dry_run_sql)
    # getattr: interface tests fake the context with SimpleNamespace objects
    # that may predate the history field.
    history = getattr(ctx, "history", None)

    if ctx.dry_run_sql:
        result = agent.run(ctx.query, ctx.conn, limit=ctx.limit, history=history)
        return AnalyticsResult(dry_run_prompt=result["sql"])

    try:
        result = agent.run(ctx.query, ctx.conn, limit=ctx.limit, history=history)
        analytics = AnalyticsResult(
            sql=result["sql"],
            rows=result["rows"],
            answer=result["answer"],
        )
        return _attach_reasoning(ctx, analytics)
    except SqlValidationError as exc:
        warning = f"SQL AI Agent validation failed ({exc}) — falling back to fixed queries"
    except Exception as exc:  # noqa: BLE001
        warning = (
            f"SQL AI Agent error ({type(exc).__name__}: {exc}) — falling back to fixed queries"
        )

    fallback = _weather_analytics_fallback(ctx)
    fallback.notices.insert(0, Notice(f"[Warning: {warning}]", level="warning"))
    return _attach_reasoning(ctx, fallback)


def _attach_reasoning(
    ctx: HandlerContext,
    result: AnalyticsResult,
) -> AnalyticsResult:
    if result.dry_run_prompt is not None or not result.rows:
        return result

    reasoning_agent = StormReasoningAgent(
        model=getattr(ctx, "openai_model", None) or getattr(ctx, "sql_model", None)
    )
    reasoning = reasoning_agent.run(
        ctx.query,
        sql=result.sql,
        rows=result.rows,
        use_openai=getattr(ctx, "use_openai_summary", True),
    )
    result.reasoning = reasoning.answer
    result.reasoning_prompt = reasoning.prompt
    result.reasoning_mode = reasoning.mode
    if reasoning.openai_error:
        result.notices.append(
            Notice(
                f"OpenAI reasoning failed ({reasoning.openai_error}); using local analytical fallback.",
                level="warning",
            )
        )
    return result


def _weather_analytics_fallback(ctx: HandlerContext) -> AnalyticsResult:
    text = ctx.query.lower()
    county = parse_county_name(ctx.query)

    comparison_rows = _reasoning_comparison_rows(ctx)
    if comparison_rows is not None:
        return AnalyticsResult(
            used_fallback=True,
            fallback_title="County comparison for analytical reasoning:",
            rows=comparison_rows,
        )

    if "top storm" in text:
        return AnalyticsResult(
            used_fallback=True,
            fallback_title=f"Top {ctx.limit} storms by outage:",
            rows=ctx.analytics_agent.top_storms_by_outage_from_db(
                ctx.conn, limit=ctx.limit
            ),
        )

    if "normal" in text or "per capita" in text or "per-capita" in text:
        notices: list[Notice] = []
        if "population" in text or "per capita" in text or "per-capita" in text:
            notices.append(
                Notice(
                    "Population is not in the current WISER schema; showing "
                    "outages normalized by overhead-line exposure instead."
                )
            )
        return AnalyticsResult(
            used_fallback=True,
            fallback_title="County outages normalized by overhead-line exposure:",
            rows=county_metric_rows(
                ctx.conn,
                "outage_per_million_ohline",
            )[: ctx.limit],
            notices=notices,
        )

    if "affected" in text and ("county" in text or "counties" in text):
        return AnalyticsResult(
            used_fallback=True,
            fallback_title="County impact summary:",
            rows=county_metric_rows(ctx.conn, "total_outage")[: ctx.limit],
        )

    if (
        "wind" in text
        and ("county" in text or "counties" in text)
        and any(keyword in text for keyword in ("highest", "maximum", "max", "peak"))
    ):
        return AnalyticsResult(
            used_fallback=True,
            fallback_title="County wind and outage summary:",
            rows=county_metric_rows(ctx.conn, "max_peak_wind")[: ctx.limit],
        )

    if "top" in text and ("county" in text or "counties" in text):
        return AnalyticsResult(
            used_fallback=True,
            fallback_title=f"Top {ctx.limit} counties by outage:",
            rows=ctx.analytics_agent.county_outage_summary_from_db(
                ctx.conn, limit=ctx.limit
            ),
        )

    if county:
        return AnalyticsResult(
            used_fallback=True,
            fallback_title=f"Top {ctx.limit} storm events for county {county}:",
            rows=ctx.analytics_agent.county_storm_history_from_db(
                ctx.conn,
                county=county,
                limit=ctx.limit,
            ),
        )

    return AnalyticsResult(
        used_fallback=True,
        notices=[
            Notice(
                "I can't answer this with the current analytics agent yet. "
                "Try a supported query like 'top counties by outage', "
                "'top storms by outage', or 'history for county Hartford'.",
                level="warning",
            )
        ],
    )


def _reasoning_comparison_rows(ctx: HandlerContext) -> list[dict[str, object]] | None:
    text = ctx.query.lower()
    if not ("why" in text or "despite" in text or "compare" in text):
        return None

    counties = [
        county
        for county in _CT_COUNTIES
        if re.search(rf"\b{re.escape(county)}\b", ctx.query, flags=re.IGNORECASE)
    ]
    if len(counties) < 2:
        return None

    storm_match = re.search(r"\b(\d{10})\b", ctx.query)
    if storm_match:
        placeholders = ", ".join("?" for _ in counties)
        params: list[object] = [
            int(storm_match.group(1)),
            *[county.lower() for county in counties],
        ]
        rows = ctx.conn.execute(
            f"""
            SELECT
                w.county,
                w.stormCode,
                w.outage,
                ROUND(w.peakWSPD10, 2) AS peakWSPD10,
                ROUND(w.sumAFWA_TOTPRECIP, 2) AS sumAFWA_TOTPRECIP,
                w.wgt5,
                w.wgt9,
                w.wgt13,
                w.wgt18,
                c.ohLine,
                ROUND(w.outage * 1000000.0 / c.ohLine, 2)
                    AS outage_per_million_ohline
            FROM WeatherData w
            JOIN County c ON LOWER(c.county) = LOWER(w.county)
            WHERE w.stormCode = ?
              AND LOWER(w.county) IN ({placeholders})
            ORDER BY w.outage DESC
            """,
            params,
        ).fetchall()
        return [_comparison_row(row) for row in rows]

    placeholders = ", ".join("?" for _ in counties)
    rows = ctx.conn.execute(
        f"""
        SELECT
            w.county,
            SUM(w.outage) AS total_outage,
            ROUND(AVG(w.outage), 2) AS avg_outage,
            MAX(w.outage) AS peak_outage,
            ROUND(AVG(w.peakWSPD10), 2) AS avg_peak_wind,
            ROUND(MAX(w.peakWSPD10), 2) AS max_peak_wind,
            c.ohLine,
            ROUND(SUM(w.outage) * 1000000.0 / c.ohLine, 2)
                AS outage_per_million_ohline
        FROM WeatherData w
        JOIN County c ON LOWER(c.county) = LOWER(w.county)
        WHERE LOWER(w.county) IN ({placeholders})
        GROUP BY w.county, c.ohLine
        ORDER BY total_outage DESC
        """,
        [county.lower() for county in counties],
    ).fetchall()
    return [_aggregate_comparison_row(row) for row in rows]


def _comparison_row(row: Sequence[object]) -> dict[str, object]:
    return {
        "county": row[0],
        "stormCode": int(row[1]),
        "outage": int(row[2]),
        "peakWSPD10": float(row[3]),
        "sumAFWA_TOTPRECIP": float(row[4]),
        "wgt5": int(row[5]),
        "wgt9": int(row[6]),
        "wgt13": int(row[7]),
        "wgt18": int(row[8]),
        "ohLine": int(row[9]),
        "outage_per_million_ohline": float(row[10]),
    }


def _aggregate_comparison_row(row: Sequence[object]) -> dict[str, object]:
    return {
        "county": row[0],
        "total_outage": int(row[1]),
        "avg_outage": float(row[2]),
        "peak_outage": int(row[3]),
        "avg_peak_wind": float(row[4]),
        "max_peak_wind": float(row[5]),
        "ohLine": int(row[6]),
        "outage_per_million_ohline": float(row[7]),
    }


def handle_similarity_search(ctx: HandlerContext) -> SimilarityResult:
    query_params = parse_similarity_params(ctx.query)
    if not query_params:
        return SimilarityResult(
            ok=False,
            notices=[
                Notice(
                    "No numeric feature parameters found. "
                    "Use key=value pairs such as wind=9.2, precip=45.",
                    level="error",
                ),
                Notice(
                    "Supported aliases: wind, precip, wgt5, wgt9, wgt13, wgt18 "
                    "(and full column names)."
                ),
            ],
        )

    county = parse_similarity_county(ctx.query, ctx.county)
    if not county:
        return SimilarityResult(
            ok=False,
            params=query_params,
            notices=[
                Notice(
                    "County required. Include a county name in the query "
                    "or pass --county Fairfield.",
                    level="error",
                )
            ],
        )

    if err := validate_wind_positive(query_params):
        return SimilarityResult(
            ok=False,
            county=county,
            params=query_params,
            notices=[Notice(err, level="error")],
        )

    total = county_historical_count(ctx.conn, county)
    results = ctx.similarity_agent.find_similar(
        ctx.conn, county, query_params, limit=ctx.limit
    )
    return SimilarityResult(
        ok=True,
        county=county,
        params=query_params,
        rows=results,
        total_historical=total,
    )


def handle_rag_knowledge(ctx: HandlerContext) -> RagKnowledgeResult:
    index_summary = ctx.rag_agent.ensure_index(ctx.conn, rebuild=ctx.rebuild_rag)
    result = RagKnowledgeResult(index_summary=index_summary)

    use_external_context = should_use_external_storm_context(ctx.query)
    # Crawling NOAA/NHC/NCEI sources downloads dozens of PDFs and takes
    # minutes, so reuse the indexed chunks unless the caller explicitly asked
    # for a crawl/rebuild or nothing external has been indexed yet.
    need_crawl = ctx.crawl_web_sources or (
        use_external_context
        and (ctx.rebuild_rag or not has_external_context(ctx.conn))
    )
    if need_crawl:
        result.external_summary = index_external_sources(
            ctx.conn,
            ctx.web_sources,
            rebuild=ctx.rebuild_rag,
            include_linked_pdfs=ctx.include_linked_pdfs,
            max_linked_pdfs=ctx.max_linked_pdfs,
        )
        result.backend_status = similarity_backend_status()
    elif use_external_context:
        result.notices.append(
            Notice(
                "Using previously indexed external storm context; "
                "enable the web-source crawl to refresh it."
            )
        )

    if use_external_context:
        impact_answer = answer_storm_impact(
            ctx.conn,
            ctx.query,
            limit=ctx.limit,
            backend=ctx.similarity_backend,
            use_openai=ctx.use_openai_summary,
            openai_model=ctx.openai_model,
            source_urls=ctx.web_sources,
        )
        if impact_answer.results:
            llm_config = openai_config(ctx.openai_model)
            if impact_answer.used_openai:
                summary_mode = f"OpenAI Responses API ({llm_config.model})"
            elif impact_answer.openai_error:
                summary_mode = (
                    f"local extractive (OpenAI fallback: {impact_answer.openai_error})"
                )
            elif not ctx.use_openai_summary:
                summary_mode = "local extractive"
            elif not llm_config.api_key_present:
                summary_mode = "local extractive (OPENAI_API_KEY not set)"
            else:
                summary_mode = "local extractive"

            result.used_external = True
            result.summary_mode = summary_mode
            result.answer = impact_answer.answer
            result.cited_results = impact_answer.cited_results
            result.context = impact_answer.context
            result.has_results = True
            return result

    rag_answer = ctx.rag_agent.answer(ctx.conn, ctx.query, limit=ctx.limit)
    result.answer = rag_answer.answer
    result.cited_results = rag_answer.cited_results
    result.context = rag_answer.context
    result.has_results = bool(rag_answer.results)
    return result


def handle_unknown(ctx: HandlerContext) -> UnknownRouteResult:
    return UnknownRouteResult()


# ---------------------------------------------------------------------------
# Route handler registry
# ---------------------------------------------------------------------------

RouteHandler = Callable[[HandlerContext], HandlerResult]

ROUTE_HANDLERS: dict[Route, RouteHandler] = {
    Route.DATA_RETRIEVAL: handle_data_retrieval,
    Route.WEATHER_ANALYTICS: handle_weather_analytics,
    Route.SIMILARITY_SEARCH: handle_similarity_search,
    Route.RAG_KNOWLEDGE: handle_rag_knowledge,
}

DEFAULT_ROUTE_HANDLER: RouteHandler = handle_unknown


def dispatch_route(decision_route: Route, ctx: HandlerContext) -> HandlerResult:
    """Invoke the registered handler for a route decision and return its result."""
    handler = ROUTE_HANDLERS.get(decision_route, DEFAULT_ROUTE_HANDLER)
    return handler(ctx)


def turn_from_result(query: str, result: HandlerResult) -> ConversationTurn | None:
    """The turn a surface should append to its LLM history, or None.

    Only successful LLM-generated analytics answers become history: fallback
    answers, dry-run prompts, errors, and non-analytics routes return None.
    UI transcripts still display those turns; they just don't feed the prompt.
    """
    if (
        isinstance(result, AnalyticsResult)
        and result.sql
        and not result.used_fallback
        and result.dry_run_prompt is None
    ):
        return ConversationTurn(question=query, sql=result.sql, answer=result.answer)
    return None
