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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Sequence, Union

import psycopg

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
from wiser import units
from wiser.storm_analysis import (
    DEFAULT_STATE,
    MONTH_NAMES,
    PeriodSpec,
    StormAnalysisError,
    ThresholdSpec,
    all_period,
    compare_forecast,
    compare_storms,
    current_threshold_analysis,
    current_weather_available,
    current_weather_group_analysis,
    month_by_month_analysis,
    month_period,
    other_period,
    region_group_analysis,
    resolve_storm,
    same_date_siblings,
    season_by_season_analysis,
    season_period,
    state_counties,
    storm_group_analysis,
    storm_count,
    storm_ranking,
    storm_summary,
    threshold_analysis,
)
from wiser.storm_reports import (
    DATASET_VINTAGE_NOTE,
    comparison_map_panels,
    REPORT_FORECAST_COMPARISON,
    REPORT_STORM_GROUP_ANALYSIS,
    REPORT_STORM_COMPARISON,
    REPORT_STORM_RANKING,
    REPORT_STORM_SUMMARY,
    REPORT_THRESHOLD_ANALYSIS,
    render_forecast_comparison,
    render_storm_group_analysis,
    render_storm_comparison,
    display_rows,
    render_storm_ranking,
    render_storm_summary,
    render_threshold_analysis,
)
from wiser.visualization import county_metric_rows

# Shared defaults; the CLI argparse flags and the Streamlit sidebar both
# reference these so interface defaults cannot drift apart.
DEFAULT_INPUT_CSV = "data/testdata.csv"
# The database is now a server, not a file. None means "resolve from
# $DATABASE_URL, else the local Docker Compose instance".
DEFAULT_DSN: str | None = None
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
    "compare July storms with February storms",
    "month-by-month storm outages",
    "regional storm analysis",
    "which storms had wind above 40mph?",
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
    conn: psycopg.Connection
    dsn: str | None
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
    # Set only by the storm-scoped routes: a rendered report block and which
    # report produced it. Interfaces show the block above the rows.
    report: str | None = None
    report_kind: str | None = None
    # rows with human labels and rounded values, for display. `rows` keeps
    # machine column names and full precision for maps and reasoning.
    display_rows: list[dict[str, object]] | None = None
    # Two or more county maps meant to be read together, already sharing
    # one colour scale. Surfaces that can show maps render them side by side.
    map_panels: list[object] | None = None
    charts: list["ChartSpec"] = field(default_factory=list)


@dataclass
class ChartSpec:
    title: str
    kind: str  # "bar" | "line" | "histogram" | "scatter"
    rows: list[dict[str, object]]
    x: str
    y: str
    color: str | None = None
    x_label: str | None = None
    y_label: str | None = None


@dataclass
class SimilarityResult:
    ok: bool
    county: str | None = None
    params: dict[str, float] = field(default_factory=dict)
    rows: list[dict[str, object]] = field(default_factory=list)
    total_historical: int = 0
    notices: list[Notice] = field(default_factory=list)
    # Populated only when the query reads as a forecast comparison. Without a
    # forecast cue these stay None and the result is identical to before the
    # storm-scoped work landed.
    report: str | None = None
    report_kind: str | None = None
    display_rows: list[dict[str, object]] | None = None


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
    dsn: str | None = None,
) -> psycopg.Connection:
    conn = connect(dsn)
    if has_required_tables(conn):
        return conn
    conn.close()
    return data_agent.build_database(dsn)


def build_context(
    query: str,
    *,
    input_csv: str = DEFAULT_INPUT_CSV,
    dsn: str | None = DEFAULT_DSN,
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
    conn = ensure_database(data_agent, dsn)
    return HandlerContext(
        query=query,
        conn=conn,
        dsn=dsn,
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
    ctx.conn = ctx.data_agent.build_database(ctx.dsn)
    return DataRetrievalResult(table_counts=get_table_counts(ctx.conn))


def handle_weather_analytics(ctx: HandlerContext) -> AnalyticsResult:
    sponsor_result = _sponsor_group_analytics(ctx)
    if sponsor_result is not None:
        return sponsor_result

    agent = SqlAiAgent(model=ctx.sql_model, dry_run=ctx.dry_run_sql)
    # getattr: interface tests fake the context with SimpleNamespace objects
    # that may predate the history field.
    history = getattr(ctx, "history", None)

    if ctx.dry_run_sql:
        result = agent.run(ctx.query, ctx.conn, limit=ctx.limit, history=history)
        return AnalyticsResult(dry_run_prompt=result["sql"])

    # Reasoning is attached after the try/except so a reasoning-layer failure is
    # never misreported as an SQL agent error, nor discards a valid SQL result.
    analytics = None
    try:
        result = agent.run(ctx.query, ctx.conn, limit=ctx.limit, history=history)
        analytics = AnalyticsResult(
            sql=result["sql"],
            rows=result["rows"],
            answer=result["answer"],
        )
    except SqlValidationError as exc:
        warning = f"SQL AI Agent validation failed ({exc}) — falling back to fixed queries"
    except Exception as exc:  # noqa: BLE001
        warning = (
            f"SQL AI Agent error ({type(exc).__name__}: {exc}) — falling back to fixed queries"
        )

    if analytics is not None:
        return _attach_reasoning(ctx, analytics)

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
    try:
        reasoning = reasoning_agent.run(
            ctx.query,
            sql=result.sql,
            rows=result.rows,
            use_openai=getattr(ctx, "use_openai_summary", True),
        )
    except Exception as exc:  # noqa: BLE001 - reasoning is additive; keep the rows
        result.notices.append(
            Notice(
                f"Storm reasoning unavailable ({type(exc).__name__}: {exc}).",
                level="warning",
            )
        )
        return result

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

    # An empty match (e.g. a storm code absent from the database) is a miss, not a
    # hit — otherwise it short-circuits every branch below and returns a bare table.
    comparison_rows = _reasoning_comparison_rows(ctx)
    if comparison_rows:
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


def _sponsor_group_analytics(ctx: HandlerContext) -> AnalyticsResult | None:
    """Deterministic month/season/region/threshold analytics.

    These sponsor-driven questions should work without an OpenAI key and should
    return chart-ready rows for Streamlit.
    """
    if getattr(ctx, "conn", None) is None:
        return None

    text = ctx.query.lower()
    if not _looks_like_sponsor_group_query(text):
        return None

    threshold, threshold_notices = _parse_threshold(ctx.query)
    periods = _parse_periods(ctx.query)
    regions = _parse_regions(ctx.query)
    primary_region = regions[0] if len(regions) == 1 else None
    county_scope = parse_storm_county(ctx) if not primary_region and len(regions) < 2 else None
    period = periods[0] if periods else all_period()
    today = date.today()
    has_current_month = current_weather_available(
        ctx.conn, year=today.year, month=today.month
    )

    if threshold is not None:
        try:
            if _should_use_current_threshold(
                threshold,
                periods,
                has_current_month=has_current_month,
                today=today,
            ):
                analysis = current_threshold_analysis(
                    ctx.conn,
                    threshold,
                    year=today.year,
                    month=today.month,
                    region=primary_region,
                    county=county_scope,
                    limit=ctx.limit,
                )
            else:
                analysis = threshold_analysis(
                    ctx.conn,
                    threshold,
                    period=period,
                    region=primary_region,
                    county=county_scope,
                    limit=ctx.limit,
                )
            report = render_threshold_analysis(analysis)
        except StormAnalysisError as exc:
            return AnalyticsResult(
                rows=[],
                notices=[*threshold_notices, Notice(str(exc), level="error")],
                report_kind=REPORT_THRESHOLD_ANALYSIS,
            )
        return AnalyticsResult(
            rows=report.rows,
            display_rows=display_rows(report),
            report=report.text,
            report_kind=report.kind,
            answer=(
                f"{analysis.matched_rows:,} county-storm rows exceeded "
                f"{threshold.label} > {threshold.display_threshold}."
            ),
            notices=threshold_notices,
            charts=_threshold_charts(report.rows, threshold),
        )
    if threshold_notices and _looks_like_threshold_query(text):
        return AnalyticsResult(
            rows=[],
            notices=threshold_notices,
            report_kind=REPORT_THRESHOLD_ANALYSIS,
        )

    try:
        if len(regions) >= 2:
            analysis = storm_group_analysis(
                ctx.conn,
                (all_period(),),
                title="Regional storm comparison",
                group_by="region",
                region=regions[0],
            )
            rows = []
            distribution = []
            for region in regions:
                if _should_use_current_group((period,), today=today, has_current_month=has_current_month):
                    regional = current_weather_group_analysis(
                        ctx.conn,
                        (period,),
                        current_year=today.year,
                        current_month=today.month,
                        title="Regional storm comparison",
                        group_by="region",
                        region=region,
                        county=county_scope,
                    )
                else:
                    regional = storm_group_analysis(
                        ctx.conn,
                        (period,),
                        title="Regional storm comparison",
                        group_by="region",
                        region=region,
                        county=county_scope,
                    )
                if regional.rows:
                    row = dict(regional.rows[0])
                    row["group"] = region
                    row["region"] = region
                    rows.append(row)
                distribution.extend(regional.distribution_rows)
            analysis = _replace_group_rows(
                analysis,
                title="Regional storm comparison",
                rows=rows,
                distribution_rows=distribution,
                group_by="region",
            )
        elif _requests_region_rollup(text) and not primary_region:
            analysis = region_group_analysis(ctx.conn, period=period)
        elif _requests_season_trend(text) and not periods:
            analysis = season_by_season_analysis(
                ctx.conn, region=primary_region, county=county_scope
            )
        elif _requests_month_trend(text) and not periods:
            analysis = month_by_month_analysis(
                ctx.conn, region=primary_region, county=county_scope
            )
        else:
            selected_periods = periods
            if len(selected_periods) == 1 and _requests_comparison_baseline(text):
                selected_periods = [
                    selected_periods[0],
                    other_period(selected_periods[0]),
                ]
            if not selected_periods:
                selected_periods = [all_period()]
            if _should_use_current_group(
                tuple(selected_periods),
                today=today,
                has_current_month=has_current_month,
            ):
                analysis = current_weather_group_analysis(
                    ctx.conn,
                    tuple(selected_periods),
                    current_year=today.year,
                    current_month=today.month,
                    title=_group_title(selected_periods),
                    group_by=selected_periods[0].kind,
                    region=primary_region,
                    county=county_scope,
                )
            else:
                analysis = storm_group_analysis(
                    ctx.conn,
                    tuple(selected_periods),
                    title=_group_title(selected_periods),
                    group_by=selected_periods[0].kind,
                    region=primary_region,
                    county=county_scope,
                )
        report = render_storm_group_analysis(analysis)
    except StormAnalysisError as exc:
        return AnalyticsResult(
            rows=[],
            notices=[Notice(str(exc), level="error")],
            report_kind=REPORT_STORM_GROUP_ANALYSIS,
        )

    notices = _group_notices(ctx.query, analysis)
    return AnalyticsResult(
        rows=report.rows,
        display_rows=display_rows(report),
        report=report.text,
        report_kind=report.kind,
        answer=report.text.split("\n", 1)[0],
        notices=notices,
        charts=_group_charts(analysis),
    )


_MONTH_ALIASES = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "februrary": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}
_SEASON_ALIASES = {
    "winter": "winter",
    "spring": "spring",
    "summer": "summer",
    "fall": "fall",
    "autumn": "fall",
}
_REGION_ALIASES = {
    "north": "Northern CT",
    "northern": "Northern CT",
    "northern ct": "Northern CT",
    "south": "Southern CT",
    "southern": "Southern CT",
    "southern ct": "Southern CT",
    "east": "Eastern CT",
    "eastern": "Eastern CT",
    "eastern ct": "Eastern CT",
    "west": "Western CT",
    "western": "Western CT",
    "western ct": "Western CT",
    "central": "Central CT",
    "central ct": "Central CT",
}
_THRESHOLD_METRICS = {
    "wind": ("peakWSPD10", "Peak sustained wind", units.SPEED),
    "winds": ("peakWSPD10", "Peak sustained wind", units.SPEED),
    "gust": ("peakGUST10", "Peak wind gust", units.SPEED),
    "gusts": ("peakGUST10", "Peak wind gust", units.SPEED),
    "precip": ("sumAFWA_TOTPRECIP", "Precipitation", units.DEPTH),
    "precipitation": ("sumAFWA_TOTPRECIP", "Precipitation", units.DEPTH),
    "rain": ("sumAFWA_TOTPRECIP", "Precipitation", units.DEPTH),
    "rainfall": ("sumAFWA_TOTPRECIP", "Precipitation", units.DEPTH),
    "outage": ("outage", "Outages", units.COUNT),
    "outages": ("outage", "Outages", units.COUNT),
    "humidity": ("humidity", "Humidity", units.COUNT),
}
_THRESHOLD_PATTERN = re.compile(
    r"\b(?P<metric>wind|winds|gust|gusts|precipitation|precip|rainfall|rain|"
    r"outages?|humidity)\b"
    r"(?:\s+\w+){0,4}?\s*"
    r"(?:above|over|greater\s+than|more\s+than|exceed(?:s|ed|ing)?|>=|>)\s*"
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>mph|m/s|ms|mps|mm|in|inch|inches|%)?",
    re.IGNORECASE,
)


def _looks_like_sponsor_group_query(text: str) -> bool:
    return (
        _looks_like_threshold_query(text)
        or _requests_month_trend(text)
        or _requests_season_trend(text)
        or _requests_region_rollup(text)
        or "this month" in text
        or any(month in text for month in _MONTH_ALIASES)
        or any(season in text for season in _SEASON_ALIASES)
    ) and any(
        word in text
        for word in (
            "storm",
            "storms",
            "weather",
            "outage",
            "outages",
            "wind",
            "gust",
            "precip",
            "rain",
            "humidity",
        )
    )


def _looks_like_threshold_query(text: str) -> bool:
    return bool(_THRESHOLD_PATTERN.search(text)) or bool(
        re.search(
            r"\b(?:wind|gust|precip|rain|outage|humidity)\s*(?:>|>=)\s*\d",
            text,
        )
    )


def _requests_month_trend(text: str) -> bool:
    return any(
        phrase in text
        for phrase in (
            "month by month",
            "month-to-month",
            "month to month",
            "monthly",
            "by month",
        )
    )


def _requests_season_trend(text: str) -> bool:
    return any(
        phrase in text
        for phrase in (
            "season by season",
            "season-to-season",
            "season to season",
            "seasonal",
            "by season",
        )
    )


def _requests_region_rollup(text: str) -> bool:
    return any(
        phrase in text
        for phrase in (
            "regional",
            "by region",
            "region by region",
            "region-to-region",
            "northern",
            "southern",
            "eastern",
            "western",
            "central",
        )
    )


def _requests_comparison_baseline(text: str) -> bool:
    return any(
        phrase in text
        for phrase in (
            "this month",
            "more outages",
            "larger",
            "more intense",
            "than",
            "compare",
            "comparison",
        )
    )


def _should_use_current_group(
    periods: Sequence[PeriodSpec],
    *,
    today: date,
    has_current_month: bool,
) -> bool:
    return has_current_month and any(
        _is_current_month_period(period, today=today) for period in periods
    )


def _should_use_current_threshold(
    threshold: ThresholdSpec,
    periods: Sequence[PeriodSpec],
    *,
    today: date,
    has_current_month: bool,
) -> bool:
    if not has_current_month:
        return False
    if any(_is_current_month_period(period, today=today) for period in periods):
        return True
    return threshold.metric_key == "humidity"


def _is_current_month_period(period: PeriodSpec, *, today: date) -> bool:
    return (
        period.kind == "month"
        and period.month == today.month
        and period.year in (None, today.year)
    )


def _parse_periods(query: str) -> list[PeriodSpec]:
    text = query.lower()
    periods: list[PeriodSpec] = []
    if "this month" in text:
        periods.append(month_period(date.today().month))

    for match in re.finditer(
        r"\b("
        + "|".join(re.escape(name) for name in sorted(_MONTH_ALIASES, key=len, reverse=True))
        + r")\b",
        text,
    ):
        month = _MONTH_ALIASES[match.group(1)]
        _append_period(periods, month_period(month))

    for match in re.finditer(
        r"\b(winter|spring|summer|fall|autumn)\b",
        text,
    ):
        _append_period(periods, season_period(_SEASON_ALIASES[match.group(1)]))

    return periods


def _append_period(periods: list[PeriodSpec], period: PeriodSpec) -> None:
    if all(existing.key != period.key for existing in periods):
        periods.append(period)


def _parse_regions(query: str) -> list[str]:
    text = query.lower()
    regions: list[str] = []
    for alias, region in sorted(_REGION_ALIASES.items(), key=lambda item: -len(item[0])):
        if re.search(rf"\b{re.escape(alias)}\b", text) and region not in regions:
            regions.append(region)
    return regions


def _parse_threshold(query: str) -> tuple[ThresholdSpec | None, list[Notice]]:
    match = _THRESHOLD_PATTERN.search(query)
    if not match:
        return None, []
    metric_key, label, dimension = _THRESHOLD_METRICS[match.group("metric").lower()]
    raw = f"{match.group('value')}{match.group('unit') or ''}"
    notices: list[Notice] = []
    if metric_key == "humidity":
        unit = (match.group("unit") or "%").strip()
        if unit != "%":
            return None, [
                Notice("Humidity thresholds should be percentages, such as humidity > 80%.", level="error")
            ]
        threshold = float(match.group("value"))
    elif dimension == units.COUNT:
        threshold = float(match.group("value"))
    else:
        try:
            threshold = units.parse_measurement(raw, dimension=dimension)
        except units.AmbiguousUnitError as exc:
            return None, [Notice(str(exc), level="error")]
        except (units.UnknownUnitError, ValueError) as exc:
            return None, [Notice(str(exc), level="error")]

    comparison_metric = None
    display_threshold = _threshold_display(metric_key, threshold)
    if metric_key == "peakGUST10":
        comparison_metric = _gust_threshold_column(threshold)
        display_threshold = _threshold_display(metric_key, threshold)

    return (
        ThresholdSpec(
            metric_key=metric_key,
            label=label,
            stored_threshold=threshold,
            display_threshold=display_threshold,
            comparison_metric=comparison_metric,
        ),
        notices,
    )


def _gust_threshold_column(threshold_ms: float) -> str | None:
    for supported in (5, 9, 13, 18):
        if threshold_ms <= supported:
            return f"wgt{supported}"
    return None


def _threshold_display(metric_key: str, threshold: float) -> str:
    if metric_key in {"peakWSPD10", "peakGUST10"}:
        return units.format_mph(threshold)
    if metric_key == "sumAFWA_TOTPRECIP":
        return units.format_inches(threshold)
    if metric_key == "humidity":
        return f"{threshold:g}%"
    return units.format_count(threshold)


def _group_title(periods: Sequence[PeriodSpec]) -> str:
    if len(periods) >= 2:
        return "Storm group comparison: " + " vs ".join(
            period.label for period in periods
        )
    if periods and periods[0].kind == "baseline":
        return "Storm group comparison"
    return "Storm group analysis"


def _replace_group_rows(
    analysis,
    *,
    title: str,
    rows: list[dict[str, object]],
    distribution_rows: list[dict[str, object]],
    group_by: str,
):
    from dataclasses import replace

    return replace(
        analysis,
        title=title,
        group_by=group_by,
        rows=tuple(rows),
        distribution_rows=tuple(distribution_rows),
    )


def _group_notices(query: str, analysis) -> list[Notice]:
    notices: list[Notice] = []
    text = query.lower()
    if "this month" in text:
        month_name = MONTH_NAMES[date.today().month - 1]
        if _analysis_uses_current_weather(analysis):
            notices.append(
                Notice(
                    f"Using staged current-weather observations for {month_name} "
                    f"{date.today().year} from CurrentWeatherObservation."
                )
            )
        else:
            notices.append(
                Notice(
                    f"Interpreting 'this month' as calendar month {month_name} "
                    "across the historical WISER dataset; no current-month "
                    "weather observations are loaded in this database."
                )
            )
    if analysis.group_by == "region":
        notices.append(
            Notice(
                "Regional analysis uses WISER default CT county groupings. "
                "Sponsor service territories need territory mapping data."
            )
        )
    if "humidity" in text and not _analysis_has_metric(analysis, "avg_humidity"):
        notices.append(
            Notice(
                "Humidity is registered for the upcoming mock dataset, but no "
                "humidity observations are present in the current database.",
                level="warning",
            )
        )
    return notices


def _analysis_uses_current_weather(analysis) -> bool:
    return any(row.get("source") == "current_weather" for row in analysis.rows)


def _analysis_has_metric(analysis, metric: str) -> bool:
    return any(isinstance(row.get(metric), (int, float)) for row in analysis.rows)


def _group_charts(analysis) -> list[ChartSpec]:
    charts: list[ChartSpec] = []
    rows = list(analysis.rows)
    if len(rows) >= 2:
        charts.extend(
            [
                ChartSpec(
                    title="Outages by group",
                    kind="bar",
                    rows=rows,
                    x="group",
                    y="total_outage",
                    x_label=analysis.group_by.capitalize(),
                    y_label="Total outages",
                ),
                ChartSpec(
                    title="Storm count by group",
                    kind="bar",
                    rows=rows,
                    x="group",
                    y="storm_count",
                    x_label=analysis.group_by.capitalize(),
                    y_label="Storm count",
                ),
                ChartSpec(
                    title="Peak wind by group",
                    kind="bar",
                    rows=rows,
                    x="group",
                    y="peak_wind_ms",
                    x_label=analysis.group_by.capitalize(),
                    y_label="Peak sustained wind (m/s)",
                ),
            ]
        )
        if any(isinstance(row.get("peak_gust_ms"), (int, float)) for row in rows):
            charts.append(
                ChartSpec(
                    title="Peak gust by group",
                    kind="bar",
                    rows=rows,
                    x="group",
                    y="peak_gust_ms",
                    x_label=analysis.group_by.capitalize(),
                    y_label="Peak wind gust (m/s)",
                )
            )
        if any(isinstance(row.get("max_humidity"), (int, float)) for row in rows):
            charts.append(
                ChartSpec(
                    title="Humidity by group",
                    kind="bar",
                    rows=rows,
                    x="group",
                    y="max_humidity",
                    x_label=analysis.group_by.capitalize(),
                    y_label="Maximum humidity (%)",
                )
            )
    distribution_rows = list(analysis.distribution_rows)
    if len(distribution_rows) >= 2:
        charts.append(
            ChartSpec(
                title="Storm outage distribution",
                kind="histogram",
                rows=distribution_rows,
                x="outage",
                y="count",
                color="group",
                x_label="Storm outage total",
                y_label="Storm count",
            )
        )
    if len(rows) >= 2 and analysis.group_by in {"month", "season"}:
        charts.append(
            ChartSpec(
                title="Outage trend",
                kind="line",
                rows=rows,
                x="group",
                y="total_outage",
                x_label=analysis.group_by.capitalize(),
                y_label="Total outages",
            )
        )
    return charts


def _threshold_charts(
    rows: list[dict[str, object]],
    threshold: ThresholdSpec,
) -> list[ChartSpec]:
    if len(rows) < 2:
        return []
    return [
        ChartSpec(
            title="Threshold exceedances by county",
            kind="bar",
            rows=_count_rows(rows, "county"),
            x="label",
            y="count",
            x_label="County",
            y_label="Matching rows",
        ),
        ChartSpec(
            title="Weather metric vs outages",
            kind="scatter",
            rows=rows,
            x="metric_value",
            y="outage",
            color="county",
            x_label=threshold.label,
            y_label="Outages",
        ),
    ]


def _count_rows(rows: list[dict[str, object]], key: str) -> list[dict[str, object]]:
    counts: dict[str, int] = {}
    for row in rows:
        label = str(row.get(key))
        counts[label] = counts.get(label, 0) + 1
    return [
        {"label": label, "count": count}
        for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


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
        placeholders = ", ".join("%s" for _ in counties)
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
                ROUND(w.peakWSPD10::numeric, 2) AS peakWSPD10,
                ROUND(w.sumAFWA_TOTPRECIP::numeric, 2) AS sumAFWA_TOTPRECIP,
                w.wgt5,
                w.wgt9,
                w.wgt13,
                w.wgt18,
                c.ohLine,
                ROUND((w.outage * 1000000.0 / c.ohLine)::numeric, 2)
                    AS outage_per_million_ohline
            FROM WeatherData w
            JOIN County c ON LOWER(c.county) = LOWER(w.county)
            WHERE w.stormCode = %s
              AND LOWER(w.county) IN ({placeholders})
            ORDER BY w.outage DESC
            """,
            params,
        ).fetchall()
        return [_comparison_row(row) for row in rows]

    placeholders = ", ".join("%s" for _ in counties)
    rows = ctx.conn.execute(
        f"""
        SELECT
            w.county,
            SUM(w.outage) AS total_outage,
            ROUND(AVG(w.outage)::numeric, 2) AS avg_outage,
            MAX(w.outage) AS peak_outage,
            ROUND(AVG(w.peakWSPD10)::numeric, 2) AS avg_peak_wind,
            ROUND(MAX(w.peakWSPD10)::numeric, 2) AS max_peak_wind,
            c.ohLine,
            ROUND((SUM(w.outage) * 1000000.0 / c.ohLine)::numeric, 2)
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


# ---------------------------------------------------------------------------
# Storm-scoped handlers
# ---------------------------------------------------------------------------

# Words that mean "this is about a storm that has not happened yet". They never
# change the route on their own; they only select a report.
FORECAST_CUES: tuple[str, ...] = (
    "upcoming",
    "forecast",
    "forecasted",
    "expected",
    "projected",
    "incoming",
    "predicted",
)

# Feature column → the dimension its value is measured in, for unit parsing.
_FEATURE_DIMENSIONS: dict[str, str] = {
    "peakWSPD10": units.SPEED,
    "sumAFWA_TOTPRECIP": units.DEPTH,
    "wgt5": units.COUNT,
    "wgt9": units.COUNT,
    "wgt13": units.COUNT,
    "wgt18": units.COUNT,
}

# alias=value with the unit attached: wind=27mph, precip=1.2in, wgt5=12.
#
# The unit must touch the number. Allowing a space would make "wind=12 to storm
# 2020080406" read "to" as a unit, and worse, "precip=1.2 in Fairfield" would
# read the preposition as inches. An attached suffix is also exactly what the
# unit-convention decision specified.
_FORECAST_PARAM_PATTERN = re.compile(
    r"(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"
    r"(?P<value>[0-9]+(?:\.[0-9]*)?)(?P<unit>[a-zA-Z/\"']*)"
)

_ISO_DATE_PATTERN = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_STORM_CODE_PATTERN = re.compile(r"\b(\d{10})\b")
_US_DATE_PATTERN = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_MONTH_NAME_PATTERN = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october"
    r"|november|december)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
    re.IGNORECASE,
)
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


# Every storm carries the same fixed 48-hour analysis window, so two storms
# entered 6 or 12 hours apart overlap by 36-42 hours. Stated in one place
# because three separate messages depend on it: the ranking note, the summary
# note, and the disambiguation prompt.
# Written as a standalone sentence so it reads correctly wherever it is
# dropped in; callers append their own consequence after an em dash.
OVERLAP_EXPLANATION = (
    "Every storm carries the same fixed 48-hour window, so a same-date pair "
    "overlaps and may be one weather system recorded twice"
)


def looks_like_forecast(query: str) -> bool:
    """True when the query is about a storm that has not happened yet."""
    text = query.lower()
    return any(
        re.search(rf"\b{re.escape(cue)}\b", text) for cue in FORECAST_CUES
    )


def parse_storm_tokens(query: str) -> list[str]:
    """Storm codes and dates named in the query, normalised and deduplicated.

    Accepts looser date forms than the router does. The router stays strict to
    avoid false positives; by the time a query reaches a handler the intent is
    settled, so a friendlier parse is safe and a failure can be explained.
    """
    tokens: list[str] = [m.group(1) for m in _STORM_CODE_PATTERN.finditer(query)]
    tokens.extend(m.group(1) for m in _ISO_DATE_PATTERN.finditer(query))
    tokens.extend(
        f"{int(m.group(3)):04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        for m in _US_DATE_PATTERN.finditer(query)
    )
    tokens.extend(
        f"{int(m.group(3)):04d}-{_MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"
        for m in _MONTH_NAME_PATTERN.finditer(query)
    )

    seen: set[str] = set()
    ordered: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            ordered.append(token)
    return ordered


def parse_forecast_params(query: str) -> tuple[dict[str, float], list[Notice]]:
    """Parse unit-suffixed forecast parameters into stored (metric) units.

    Bare numbers are rejected rather than guessed: 12 m/s and 12 mph differ by
    a factor of five, both are in range for this dataset, and the error is
    largest at exactly the values people are most likely to type.
    """
    params: dict[str, float] = {}
    notices: list[Notice] = []

    for match in _FORECAST_PARAM_PATTERN.finditer(" ".join(query.split())):
        feature = FEATURE_ALIASES.get(match.group("name").lower())
        if feature is None:
            continue
        dimension = _FEATURE_DIMENSIONS.get(feature, units.COUNT)
        raw = f"{match.group('value')}{match.group('unit')}"
        try:
            params[feature] = units.parse_measurement(raw, dimension=dimension)
        except units.AmbiguousUnitError as exc:
            notices.append(Notice(str(exc), level="error"))
        except (units.UnknownUnitError, ValueError) as exc:
            notices.append(Notice(str(exc), level="error"))

    return params, notices


def parse_storm_county(ctx: HandlerContext, state: str = DEFAULT_STATE) -> str | None:
    """County named in the query, matched against the counties in this state.

    Strict on purpose: unlike the similarity parser this has no free-text
    fallback, because a storm query's trailing words are far more likely to be
    a date or a metric than a county name.
    """
    if ctx.county:
        return ctx.county
    try:
        candidates = state_counties(ctx.conn, state=state)
    except Exception:  # noqa: BLE001 - county scoping is optional
        candidates = _CT_COUNTIES
    for county in candidates:
        if re.search(rf"\b{re.escape(county)}\b", ctx.query, flags=re.IGNORECASE):
            return county
    return None


def _resolve_single_storm(
    ctx: HandlerContext,
    token: str,
) -> tuple[int | None, list[Notice]]:
    """Resolve one token, turning ambiguity and misses into notices."""
    try:
        resolution = resolve_storm(ctx.conn, token)
    except StormAnalysisError as exc:
        return None, [Notice(str(exc), level="error")]

    if resolution.ok:
        return resolution.storm.storm_code, []

    if resolution.ambiguous:
        # Rendered as ready-to-run queries rather than a prose list: the user
        # would otherwise have to retype the whole question around the code.
        options = "\n".join(
            f"  {_requery_text(ctx.query, token, match.storm_code)}"
            f"   ({_clock_time(match.event_datetime)})"
            for match in resolution.matches
        )
        # The candidates are, by construction, storms sharing a calendar date —
        # so they are the same-date siblings, and picking between them may not
        # be a meaningful choice. Saying so here is worth more than saying it
        # after the fact: without it the user picks one arbitrarily.
        return None, [
            Notice(
                f"{token} matches more than one storm. {OVERLAP_EXPLANATION} "
                "— picking one of these may not be a meaningful choice. "
                f"Re-ask with the full stormCode:\n{options}",
                level="error",
            )
        ]

    return None, [
        Notice(f"No storm in the database matches {token!r}.", level="error")
    ]


def _requery_text(query: str, token: str, storm_code: int) -> str:
    """The user's own question with the ambiguous token swapped for a code.

    Substituting in place rather than rebuilding the sentence: the token can
    sit anywhere, and removing it from "how does the storm on 2022-03-31 rank"
    leaves "how does the storm on rank".
    """
    return " ".join(query.replace(token, str(storm_code)).split())


def _clock_time(event_datetime: str) -> str:
    """'2021-09-01 12:00:00' → '2021-09-01 12:00'."""
    text = str(event_datetime).replace("T", " ")
    return text[:16] if len(text) >= 16 else text


def _storm_notice_result(notices: list[Notice], kind: str) -> AnalyticsResult:
    return AnalyticsResult(rows=[], notices=notices, report_kind=kind)


def _missing_storm_result(kind: str) -> AnalyticsResult:
    return _storm_notice_result(
        [
            Notice(
                "No storm identifier found. Name a storm by code (2020080406) "
                "or by date (2020-08-04).",
                level="error",
            )
        ],
        kind,
    )


def handle_storm_summary(ctx: HandlerContext) -> AnalyticsResult:
    """Capability 1 — a summary report for one storm."""
    tokens = parse_storm_tokens(ctx.query)
    if not tokens:
        return _missing_storm_result(REPORT_STORM_SUMMARY)

    storm_code, notices = _resolve_single_storm(ctx, tokens[0])
    if storm_code is None:
        return _storm_notice_result(notices, REPORT_STORM_SUMMARY)

    county = parse_storm_county(ctx)
    try:
        summary = storm_summary(ctx.conn, storm_code, county=county)
        report = render_storm_summary(
            summary, total_storms=storm_count(ctx.conn, state=summary.state)
        )
    except StormAnalysisError as exc:
        return _storm_notice_result(
            [Notice(str(exc), level="error")], REPORT_STORM_SUMMARY
        )

    notices.extend(
        _same_date_sibling_notices(
            ctx,
            storm_code,
            consequence=(
                "these figures may describe only part of the event, and the "
                "sibling storm reports a near-identical outage total."
            ),
        )
    )

    result = AnalyticsResult(
        rows=report.rows,
        display_rows=display_rows(report),
        report=report.text,
        report_kind=report.kind,
        answer=(
            f"Storm {storm_code} on {summary.storm.date}: "
            f"{summary.statewide_outage:,} outages across "
            f"{len(summary.counties)} counties."
        ),
        notices=notices,
    )
    # Reasoning is attached here and nowhere else in the storm routes: these
    # rows are county-shaped, which is what the reasoning layer reads. The
    # ranking and comparison reports already carry their interpretation,
    # computed deterministically.
    return _attach_reasoning(ctx, result)


def handle_storm_ranking(ctx: HandlerContext) -> AnalyticsResult:
    """Capability 2 — where a storm sits in the historical record."""
    tokens = parse_storm_tokens(ctx.query)
    if not tokens:
        return _missing_storm_result(REPORT_STORM_RANKING)

    storm_code, notices = _resolve_single_storm(ctx, tokens[0])
    if storm_code is None:
        return _storm_notice_result(notices, REPORT_STORM_RANKING)

    if re.search(r"\bdurations?\b|\blongest\b|\bhow long\b", ctx.query, re.IGNORECASE):
        notices.append(
            Notice(
                "Duration cannot be ranked: Storm.eventLength is a fixed "
                "analysis window, identical for every storm in the database.",
                level="warning",
            )
        )

    county = parse_storm_county(ctx)
    try:
        ranking = storm_ranking(ctx.conn, storm_code, county=county)
        report = render_storm_ranking(ranking)
    except StormAnalysisError as exc:
        return _storm_notice_result(
            [*notices, Notice(str(exc), level="error")], REPORT_STORM_RANKING
        )

    notices.extend(
        _same_date_sibling_notices(
            ctx,
            storm_code,
            consequence=(
                "expect adjacent ranks and near-identical outage figures for "
                "the pair."
            ),
        )
    )
    notices.append(Notice(DATASET_VINTAGE_NOTE))
    return AnalyticsResult(
        rows=report.rows,
        display_rows=display_rows(report),
        report=report.text,
        report_kind=report.kind,
        answer=report.text.split("\n", 1)[0],
        notices=notices,
    )


def _same_date_sibling_notices(
    ctx: HandlerContext,
    storm_code: int,
    *,
    consequence: str,
) -> list[Notice]:
    """Warn that a storm shares its calendar date with another.

    Such a pair sits at adjacent ranks with near-identical outage figures,
    which reads as a duplicate-row bug. Naming it turns "why are there two of
    these" into a stated data-quality observation. Nothing is deduplicated —
    that would be a data decision, and both rows are real.

    ``consequence`` states what the overlap means for the report at hand; the
    overlap itself is described by the shared OVERLAP_EXPLANATION so all three
    places that mention it cannot drift apart.
    """
    try:
        siblings = same_date_siblings(ctx.conn, storm_code)
    except Exception:  # noqa: BLE001 - an advisory notice must never break a report
        return []
    if not siblings:
        return []

    listed = ", ".join(
        f"{sibling.storm_code} ({_clock_time(sibling.event_datetime)})"
        for sibling in siblings
    )
    return [
        Notice(
            f"Data note: {storm_code} shares its date with {listed}. "
            f"{OVERLAP_EXPLANATION} — {consequence}",
            level="warning",
        )
    ]


def handle_storm_comparison(ctx: HandlerContext) -> AnalyticsResult:
    """Capability 3 — Mode A (two stored storms) or Mode B (a forecast)."""
    params, param_notices = parse_forecast_params(ctx.query)
    tokens = parse_storm_tokens(ctx.query)

    if params or looks_like_forecast(ctx.query):
        return _handle_forecast_comparison(ctx, params, param_notices, tokens)

    if len(tokens) < 2:
        return _storm_notice_result(
            [
                Notice(
                    "A storm comparison needs two storms. Name both by code "
                    "(2020080406) or date (2020-08-04), or describe a forecast "
                    "with parameters such as wind=27mph.",
                    level="error",
                )
            ],
            REPORT_STORM_COMPARISON,
        )

    notices: list[Notice] = []
    codes: list[int] = []
    for token in tokens[:2]:
        code, token_notices = _resolve_single_storm(ctx, token)
        notices.extend(token_notices)
        if code is not None:
            codes.append(code)

    if len(codes) < 2:
        return _storm_notice_result(notices, REPORT_STORM_COMPARISON)

    try:
        comparison = compare_storms(ctx.conn, codes[0], codes[1])
        report = render_storm_comparison(comparison)
    except StormAnalysisError as exc:
        return _storm_notice_result(
            [*notices, Notice(str(exc), level="error")], REPORT_STORM_COMPARISON
        )

    return AnalyticsResult(
        rows=report.rows,
        display_rows=display_rows(report),
        # Two county maps on one shared scale, for surfaces that can show them.
        map_panels=comparison_map_panels(comparison),
        report=report.text,
        report_kind=report.kind,
        answer=report.text.split("\n", 1)[0],
        notices=notices,
    )


def _handle_forecast_comparison(
    ctx: HandlerContext,
    params: dict[str, float],
    param_notices: list[Notice],
    tokens: list[str],
) -> AnalyticsResult:
    """Mode B — a forecast positioned against the historical record."""
    if not params:
        notices = param_notices or [
            Notice(
                "A forecast comparison needs weather parameters with explicit "
                "units, such as wind=27mph or precip=1.2in.",
                level="error",
            )
        ]
        return _storm_notice_result(notices, REPORT_FORECAST_COMPARISON)

    if err := validate_wind_positive(params):
        return _storm_notice_result(
            [*param_notices, Notice(err, level="error")], REPORT_FORECAST_COMPARISON
        )

    notices = list(param_notices)
    county = parse_storm_county(ctx)

    reference_summary = None
    reference_ranking = None
    if tokens:
        storm_code, reference_notices = _resolve_single_storm(ctx, tokens[0])
        if storm_code is None:
            # The forecast half still stands on its own; say what was dropped.
            notices.extend(reference_notices)
        else:
            try:
                reference_summary = storm_summary(ctx.conn, storm_code)
                reference_ranking = storm_ranking(ctx.conn, storm_code)
            except StormAnalysisError as exc:
                notices.append(Notice(str(exc), level="warning"))
                reference_summary = None
                reference_ranking = None

    if len(params) == 1 and "peakWSPD10" not in params:
        notices.append(
            Notice(
                "Only one non-wind parameter was supplied, so the match is "
                "constrained on that feature alone."
            )
        )

    try:
        # The analogue count is its own concern, not the general row limit:
        # eight counties at --limit 10 is eighty rows of table for one question.
        comparison = compare_forecast(ctx.conn, params, county=county)
        report = render_forecast_comparison(
            comparison,
            reference_summary=reference_summary,
            reference_ranking=reference_ranking,
        )
    except StormAnalysisError as exc:
        return _storm_notice_result(
            [*notices, Notice(str(exc), level="error")], REPORT_FORECAST_COMPARISON
        )

    return AnalyticsResult(
        rows=report.rows,
        display_rows=display_rows(
            report, forecast_wind_ms=params.get("peakWSPD10")
        ),
        report=report.text,
        report_kind=report.kind,
        answer=(
            "Forecast positioned against "
            f"{comparison.total_storms:,} historical storms; "
            "outages shown are historical actuals, not a prediction."
        ),
        notices=notices,
    )


def handle_similarity_search(ctx: HandlerContext) -> SimilarityResult:
    # A forecast query is parsed with unit awareness even on this route.
    # parse_similarity_params drops the suffix, so "wind=27mph" would be read
    # as 27 m/s — 60 mph, a fivefold error and exactly what requiring explicit
    # units exists to prevent. Plain similarity queries keep the historical
    # unit-free convention.
    unit_notices: list[Notice] = []
    if looks_like_forecast(ctx.query):
        query_params, unit_notices = parse_forecast_params(ctx.query)
        if unit_notices and not query_params:
            return SimilarityResult(ok=False, params={}, notices=unit_notices)
    else:
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
        # A forecast compared against the whole record is legitimately
        # statewide: compare_forecast groups analogues per county, so unlike a
        # plain similarity search it does not need one naming a county.
        if looks_like_forecast(ctx.query):
            return _statewide_forecast_result(ctx, query_params, unit_notices)
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
    result = SimilarityResult(
        ok=True,
        county=county,
        params=query_params,
        rows=results,
        total_historical=total,
        notices=list(unit_notices),
    )
    return _attach_forecast_report(ctx, result)


def _statewide_forecast_result(
    ctx: HandlerContext,
    params: dict[str, float],
    notices: list[Notice] | None = None,
) -> SimilarityResult:
    """A forecast positioned against the whole record, no county named."""
    try:
        comparison = compare_forecast(ctx.conn, params)
        report = render_forecast_comparison(comparison)
    except StormAnalysisError as exc:
        return SimilarityResult(
            ok=False,
            params=params,
            notices=[Notice(str(exc), level="error")],
        )

    return SimilarityResult(
        ok=True,
        county=None,
        params=params,
        notices=list(notices or []),
        rows=report.rows,
        total_historical=comparison.total_storms,
        report=report.text,
        report_kind=report.kind,
        display_rows=display_rows(
            report, forecast_wind_ms=params.get("peakWSPD10")
        ),
    )


def _attach_forecast_report(
    ctx: HandlerContext,
    result: SimilarityResult,
) -> SimilarityResult:
    """Render a forecast comparison over a similarity result, when asked.

    Comparing a forecast to the whole record names no storm, so it stays on the
    similarity route rather than fighting for a storm-scoped one. What changes
    is only the presentation: with a forecast cue the result gains a report
    block; without one it is returned exactly as before.
    """
    if not looks_like_forecast(ctx.query):
        return result

    try:
        comparison = compare_forecast(
            ctx.conn, result.params, county=result.county, limit=ctx.limit
        )
        report = render_forecast_comparison(comparison)
    except StormAnalysisError as exc:
        result.notices.append(
            Notice(f"Forecast comparison unavailable ({exc}).", level="warning")
        )
        return result

    result.report = report.text
    result.report_kind = report.kind
    result.display_rows = display_rows(
        report, forecast_wind_ms=result.params.get("peakWSPD10")
    )
    return result


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
    Route.STORM_SUMMARY: handle_storm_summary,
    Route.STORM_RANKING: handle_storm_ranking,
    Route.STORM_COMPARISON: handle_storm_comparison,
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
