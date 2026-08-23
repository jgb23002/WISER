"""WISER Streamlit interface.

Run from the WISER directory:

    streamlit run streamlit_app.py

Like the CLI, this app contains no routing, dispatch, or SQL-generation
logic: it routes with TaskRoutingAgent and consumes the shared handler
layer in wiser.handlers, rendering the structured results with Streamlit
widgets. The OpenAI key is read exactly the way the CLI reads it (repo-root
.env or the environment, via wiser.openai_summarizer).
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import streamlit as st
import streamlit.components.v1 as components

REPO_ROOT = Path(__file__).resolve().parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.agents.task_routing_agent import Route, TaskRoutingAgent
from wiser.conversation import resolve_route
from wiser.database import connect, has_required_tables, resolve_dsn
from wiser.evaluation import evaluate_routing_cases, evaluate_sql_ground_truth
from wiser.handlers import (
    DEFAULT_DSN,
    DEFAULT_INPUT_CSV,
    DEFAULT_LIMIT,
    EXAMPLE_QUERIES,
    AnalyticsResult,
    DataRetrievalResult,
    HandlerResult,
    Notice,
    RagKnowledgeResult,
    SimilarityResult,
    UnknownRouteResult,
    build_context,
    dispatch_route,
    turn_from_result,
)
from wiser.integrations.eagleeye import parse_eagleeye_csv
from wiser.integrations.external_outage import (
    load_external_observations,
    recent_external_observations,
)
from wiser.integrations.national_grid import parse_national_grid_csv
from wiser.openai_summarizer import openai_config
from wiser.rag import format_excerpt
from wiser.visualization import (
    COUNTY_HEATMAP_METRICS,
    CT_LEGACY_COUNTY_SOURCE_URL,
    PANEL_MAP_HEIGHT,
    PANEL_MAP_WIDTH,
    available_storm_options,
    county_metric_rows,
    load_county_geojson,
    metric_display_name,
    render_county_heatmap_svg,
)

ROUTE_COLORS = {
    Route.DATA_RETRIEVAL: "green",
    Route.WEATHER_ANALYTICS: "orange",
    Route.SIMILARITY_SEARCH: "blue",
    Route.RAG_KNOWLEDGE: "violet",
    Route.UNKNOWN: "red",
}

_NOTICE_RENDERERS = {"error": st.error, "warning": st.warning, "info": st.info}
ROUTING_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "task_routing_cases.json"
SQL_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "sql_ai_ground_truth.json"
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"
COMBINED_DOC_PATH = REPO_ROOT / "docs" / "WISER_DOCUMENTATION.md"
EXTERNAL_SAMPLE_FEEDS = {
    "EagleEye": (
        REPO_ROOT / "tests" / "fixtures" / "eagleeye_sample.csv",
        parse_eagleeye_csv,
    ),
    "National Grid": (
        REPO_ROOT / "tests" / "fixtures" / "national_grid_sample.csv",
        parse_national_grid_csv,
    ),
}
APP_STATE_VERSION = "2026-07-20-openai-token-map-v2"


def _reset_conversation_state() -> None:
    st.session_state["messages"] = []
    st.session_state["sql_history"] = []
    st.session_state["last_route"] = None


def _ensure_conversation_state() -> None:
    st.session_state.setdefault("messages", [])
    st.session_state.setdefault("sql_history", [])
    st.session_state.setdefault("last_route", None)

    if st.session_state.get("app_state_version") != APP_STATE_VERSION:
        st.session_state["app_state_version"] = APP_STATE_VERSION
        _reset_conversation_state()


def _render_notices(notices: list[Notice]) -> None:
    for notice in notices:
        _NOTICE_RENDERERS.get(notice.level, st.info)(notice.text)


def _render_sources(cited_results) -> None:
    if not cited_results:
        return
    with st.expander(f"Sources ({len(cited_results)})", expanded=True):
        for position, result in enumerate(cited_results, start=1):
            st.markdown(
                f"**{position}. `{result.source_path}#chunk-{result.chunk_index}`** "
                f"(score={result.score:.2f})"
            )
            st.caption(format_excerpt(result.content, max_chars=420))


def _render_data_retrieval(result: DataRetrievalResult) -> None:
    st.success("Database rebuilt from CSV.")
    st.subheader("Row counts")
    st.dataframe(
        [
            {"table": table_name, "rows": count}
            for table_name, count in result.table_counts.items()
        ],
        hide_index=True,
        width="stretch",
    )


def _render_analytics(
    result: AnalyticsResult,
    *,
    show_sql: bool,
    show_reasoning_prompt: bool,
) -> None:
    if result.dry_run_prompt is not None:
        st.subheader("SQL AI Agent dry-run prompt")
        st.caption("No API call was made.")
        st.code(result.dry_run_prompt, language="text")
        return

    _render_notices(result.notices)

    # Storm-scoped routes render a report block above the table and heat map.
    # The table uses display_rows — human labels and rounded values — while the
    # heat map keeps the machine-named rows it needs.
    if result.report is not None:
        _render_storm_report(result)
        if result.display_rows:
            st.dataframe(result.display_rows, hide_index=True, width="stretch")
        _render_result_charts(result.charts)
        _render_analytics_heatmap(result)
        _render_reasoning(
            result,
            show_reasoning_prompt=show_reasoning_prompt,
        )
        return

    if result.used_fallback:
        st.caption("Answered with fixed fallback queries (SQL AI Agent unavailable).")
        if result.fallback_title is not None:
            st.subheader(result.fallback_title.rstrip(":"))
            _render_rows(result.rows or [])
            _render_analytics_heatmap(result)
        _render_reasoning(
            result,
            show_reasoning_prompt=show_reasoning_prompt,
        )
        return

    st.caption("Answered by the SQL AI Agent (LLM-generated SQL, read-only execution).")
    if result.answer:
        st.markdown(f"**{result.answer}**")
    if show_sql and result.sql:
        st.code(result.sql, language="sql")
    _render_rows(result.rows or [])
    _render_analytics_heatmap(result)
    _render_reasoning(
        result,
        show_reasoning_prompt=show_reasoning_prompt,
    )


_REPORT_TITLES = {
    "storm_summary": "Storm summary",
    "storm_ranking": "Storm ranking",
    "storm_comparison": "Storm comparison",
    "forecast_comparison": "Forecast comparison",
    "storm_group_analysis": "Storm group analysis",
    "threshold_analysis": "Threshold analysis",
}


def _render_storm_report(result) -> None:
    """Show a storm report block.

    Rendered as preformatted text, not markdown: the reports use column
    alignment and a leading-space indent that markdown would collapse.
    """
    st.subheader(_REPORT_TITLES.get(result.report_kind, "Storm report"))
    st.code(result.report, language="text")


def _render_reasoning(
    result: AnalyticsResult,
    *,
    show_reasoning_prompt: bool,
) -> None:
    if result.reasoning:
        st.subheader("Analytical reasoning")
        st.markdown(result.reasoning)
        if result.reasoning_mode:
            st.caption(f"Reasoning mode: {result.reasoning_mode}")
        if show_reasoning_prompt and result.reasoning_prompt:
            with st.expander("Reasoning prompt"):
                st.code(result.reasoning_prompt, language="text")


def _render_map_panels(result: AnalyticsResult) -> bool:
    """Render a set of county maps side by side. Returns True if it drew any.

    Every panel is drawn with the SAME colour range, computed across all of
    them. Scaled independently, a storm with a tenth of the outages would paint
    just as red as the larger one and invert the comparison being made.
    """
    panels = getattr(result, "map_panels", None)
    if not panels:
        return False

    st.subheader("County heat maps")
    st.caption(
        "Both maps share one colour scale, so the same shade means the same "
        "number of outages in each. The legend marks the values the bands run "
        "between; they widen toward the top end, so a moderately smaller storm "
        "keeps some of its own contrast. A storm orders of magnitude smaller "
        "will read as uniformly pale — that is the comparison, not a missing "
        "layer, and the caption above each map gives its own peak. Hatched "
        "counties have no data, as distinct from a measured zero."
    )
    columns = st.columns(len(panels))
    for column, panel in zip(columns, panels):
        with column:
            st.markdown(f"**{panel.title}**")
            st.caption(panel.subtitle)
            try:
                svg = render_county_heatmap_svg(
                    _county_geojson(),
                    panel.rows,
                    panel.metric,
                    title=panel.title,
                    value_range=panel.value_range,
                    width=PANEL_MAP_WIDTH,
                    height=PANEL_MAP_HEIGHT,
                )
            except Exception as exc:  # noqa: BLE001 - the table remains the source of truth
                st.warning(f"Map unavailable: {type(exc).__name__}: {exc}")
                continue
            _render_svg(svg, height=_SVG_PANEL_HEIGHT)
    return True


def _render_analytics_heatmap(result: AnalyticsResult) -> None:
    if _render_map_panels(result):
        return
    rows = result.rows or []
    metric_key = _heatmap_metric_for_result(result)
    if metric_key is None:
        return
    try:
        svg = render_county_heatmap_svg(
            _county_geojson(),
            rows,
            metric_key,
            title=metric_display_name(metric_key),
        )
    except Exception as exc:  # noqa: BLE001 - table is still the source of truth
        st.warning(f"County heat map unavailable for this result: {type(exc).__name__}: {exc}")
        return
    st.subheader("County heat map")
    _render_svg(svg)


def _heatmap_metric_for_result(result: AnalyticsResult) -> str | None:
    rows = result.rows or []
    if not _has_columns(rows, "county"):
        return None

    title = (result.fallback_title or "").lower()
    if "normalized" in title and _has_columns(rows, "outage_per_million_ohline"):
        return "outage_per_million_ohline"
    if "wind" in title and _has_columns(rows, "max_peak_wind"):
        return "max_peak_wind"

    for metric_key in (
        "total_outage",
        "outage_per_million_ohline",
        "peak_outage",
        "avg_outage",
        "max_peak_wind",
        "avg_peak_wind",
    ):
        if _has_columns(rows, metric_key):
            return metric_key
    return None


def _render_rows(rows: list[dict[str, object]]) -> None:
    if not rows:
        st.info("No results found.")
        return
    st.dataframe(rows, hide_index=True, width="stretch")
    _render_result_chart(rows)


def _render_result_chart(rows: list[dict[str, object]]) -> None:
    if _has_columns(rows, "county", "total_outage"):
        _render_bar_chart(
            _chart_rows(rows, "county", "total_outage"), "County", "Total outages"
        )
    elif _has_columns(rows, "stormCode", "total_outage"):
        _render_bar_chart(
            _chart_rows(rows, "stormCode", "total_outage"), "Storm", "Total outages"
        )
    elif _has_columns(rows, "stormCode", "outage"):
        _render_bar_chart(
            _chart_rows(rows, "stormCode", "outage"), "Storm", "Outages"
        )


def _render_result_charts(charts: list[object]) -> None:
    if not charts:
        return
    st.subheader("Charts")
    for chart in charts:
        rows = getattr(chart, "rows", [])
        if len(rows) < 2:
            continue
        with st.container(border=True):
            st.markdown(f"**{chart.title}**")
            if chart.kind == "bar":
                st.bar_chart(
                    rows,
                    x=chart.x,
                    y=chart.y,
                    color=getattr(chart, "color", None),
                    x_label=chart.x_label,
                    y_label=chart.y_label,
                )
            elif chart.kind == "line":
                st.line_chart(
                    rows,
                    x=chart.x,
                    y=chart.y,
                    color=getattr(chart, "color", None),
                    x_label=chart.x_label,
                    y_label=chart.y_label,
                )
            elif chart.kind == "scatter":
                st.scatter_chart(
                    rows,
                    x=chart.x,
                    y=chart.y,
                    color=getattr(chart, "color", None),
                    x_label=chart.x_label,
                    y_label=chart.y_label,
                )
            elif chart.kind == "histogram":
                _render_histogram(chart)


def _render_histogram(chart) -> None:
    tooltip = [{"field": chart.x, "type": "quantitative"}]
    if chart.color:
        tooltip.append({"field": chart.color, "type": "nominal"})
    st.vega_lite_chart(
        chart.rows,
        {
            "mark": "bar",
            "encoding": {
                "x": {
                    "bin": True,
                    "field": chart.x,
                    "type": "quantitative",
                    "title": chart.x_label,
                },
                "y": {
                    "aggregate": "count",
                    "type": "quantitative",
                    "title": chart.y_label,
                },
                "color": (
                    {
                        "field": chart.color,
                        "type": "nominal",
                        "title": chart.color.capitalize(),
                    }
                    if chart.color
                    else {"value": "#4f46e5"}
                ),
                "tooltip": tooltip,
            },
        },
    )


def _render_bar_chart(
    chart_rows: list[dict[str, object]],
    x_label: str,
    y_label: str,
) -> None:
    # A bar chart of one bar says nothing; a single-storm view is a table and a
    # map, not a chart.
    if len(chart_rows) < 2:
        return
    st.bar_chart(
        chart_rows, x="label", y="value", x_label=x_label, y_label=y_label
    )


def _has_columns(rows: list[dict[str, object]], *columns: str) -> bool:
    if not rows:
        return False
    return all(column in rows[0] for column in columns)


def _chart_rows(
    rows: list[dict[str, object]],
    label_key: str,
    value_key: str,
) -> list[dict[str, object]]:
    chart_rows: list[dict[str, object]] = []
    for row in rows:
        value = row.get(value_key)
        if isinstance(value, (int, float)):
            chart_rows.append({"label": str(row.get(label_key)), "value": value})
    return chart_rows


def _render_similarity(result: SimilarityResult) -> None:
    if not result.ok:
        _render_notices(result.notices)
        return
    _render_notices(result.notices)
    if result.report is not None:
        _render_storm_report(result)
        if result.display_rows:
            st.dataframe(result.display_rows, hide_index=True, width="stretch")
        return
    params = ", ".join(f"{name}={value:g}" for name, value in result.params.items())
    st.subheader(f"Similar storms — {result.county}")
    st.caption(
        f"Query features: {params} · "
        f"{result.total_historical} historical storms in {result.county} · "
        "multi-dimensional Z-score similarity"
    )
    if not result.rows:
        st.info(f"No historical data found for county: {result.county}")
        return
    st.dataframe(
        result.rows,
        hide_index=True,
        width="stretch",
        column_config={
            "similarity": st.column_config.ProgressColumn(
                "similarity", min_value=0.0, max_value=1.0, format="%.4f"
            ),
        },
    )
    if _has_columns(result.rows, "stormCode", "similarity"):
        _render_bar_chart(
            _chart_rows(result.rows, "stormCode", "similarity"),
            "Storm",
            "Similarity",
        )


def _render_rag(result: RagKnowledgeResult, *, show_rag_context: bool) -> None:
    summary = result.index_summary
    st.caption(
        f"RAG index ready: {summary.total_documents} documents, "
        f"{summary.total_chunks} chunks ({summary.documents_indexed} refreshed)."
    )
    if result.external_summary is not None:
        web_summary = result.external_summary
        st.caption(
            f"External storm context: {web_summary.total_documents} documents, "
            f"{web_summary.total_chunks} chunks "
            f"({web_summary.documents_indexed} refreshed"
            f"{', ' + str(len(web_summary.failures)) + ' failed' if web_summary.failures else ''}) · "
            f"similarity backend: {result.backend_status}"
        )
        if web_summary.failures:
            with st.expander(f"Web source failures ({len(web_summary.failures)})"):
                for failure in web_summary.failures:
                    st.markdown(f"- {failure}")

    _render_notices(result.notices)

    if not result.has_results:
        st.info("No RAG context found for this query.")
        return

    if result.used_external:
        st.subheader("Storm impact summary")
        st.caption(f"Summary mode: {result.summary_mode}")
    else:
        st.subheader("Grounded answer")
    st.markdown(result.answer)
    _render_sources(result.cited_results)

    if show_rag_context:
        with st.expander("Augmented prompt/context"):
            st.code(result.context, language="text")


def _render_unknown(result: UnknownRouteResult) -> None:
    st.warning("No route matched this query. Try one of these:")
    for suggestion in result.suggestions:
        st.markdown(f"- `{suggestion}`")


@st.cache_data(show_spinner=False)
def _evaluation_metrics() -> tuple[dict[str, object], dict[str, object]]:
    routing = evaluate_routing_cases(ROUTING_FIXTURE)
    sql = evaluate_sql_ground_truth(SQL_FIXTURE, schema_path=SCHEMA_PATH)
    return (
        {
            "name": routing.name,
            "passed": routing.passed,
            "total": routing.total,
            "failed": routing.failed,
            "pass_rate": routing.pass_rate,
        },
        {
            "name": sql.name,
            "passed": sql.passed,
            "total": sql.total,
            "failed": sql.failed,
            "pass_rate": sql.pass_rate,
        },
    )


def _redacted_dsn(dsn: str) -> str:
    """Hide the password before a DSN reaches the browser or the transcript.

    The sidebar value and the load-confirmation message both end up in
    screenshots and shared session history.
    """
    parsed = urlsplit(dsn)
    if not parsed.password:
        return dsn
    userinfo = f"{parsed.username}:***@" if parsed.username else "***@"
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit(
        (parsed.scheme, f"{userinfo}{host}", parsed.path, parsed.query, parsed.fragment)
    )


def _render_demo_status(dsn: str | None) -> None:
    # _evaluation_metrics() runs the SQL ground truth, which now needs a
    # reachable server and CREATE SCHEMA rights. Under SQLite it ran against a
    # temp file and could not fail this way. Every sibling panel in this file
    # already degrades to a warning; this one must too.
    try:
        routing, sql = _evaluation_metrics()
    except Exception as exc:  # noqa: BLE001 - keep presentation panel resilient
        st.warning(f"Evaluation metrics unavailable: {type(exc).__name__}: {exc}")
        _render_external_preview(dsn, limit=8)
        return
    col1, col2, col3 = st.columns(3)
    col1.metric(
        "Routing ground truth",
        f"{routing['passed']}/{routing['total']}",
        f"{routing['pass_rate'] * 100:.0f}%",
    )
    col2.metric(
        "SQL ground truth",
        f"{sql['passed']}/{sql['total']}",
        f"{sql['pass_rate'] * 100:.0f}%",
    )
    col3.metric(
        "External CSV feeds",
        f"{len(EXTERNAL_SAMPLE_FEEDS)} ready",
        "staging enabled",
    )

    st.caption(
        "Presentation-ready demo surface: routed queries, semantic SQL evaluation, "
        "EagleEye/National Grid CSV staging, consolidated docs, and result charts."
    )
    st.markdown(f"Documentation: `{COMBINED_DOC_PATH.relative_to(REPO_ROOT)}`")

    with st.expander("Current PostgreSQL staging preview", expanded=False):
        _render_external_preview(dsn, limit=8)


def _render_external_feed_loader(dsn: str | None) -> None:
    feed_names = tuple(EXTERNAL_SAMPLE_FEEDS)
    selected_feed = st.selectbox("Sample external feed", options=("All", *feed_names))
    if st.button("Load sample feed", width="stretch"):
        feeds_to_load = feed_names if selected_feed == "All" else (selected_feed,)
        summaries = []
        conn = connect(dsn)
        try:
            for feed_name in feeds_to_load:
                sample_path, parser = EXTERNAL_SAMPLE_FEEDS[feed_name]
                observations = parser(sample_path)
                summaries.append(load_external_observations(conn, observations))
        finally:
            conn.close()

        loaded = sum(summary.rows_loaded for summary in summaries)
        st.success(
            "Loaded "
            f"{loaded} external outage observations into "
            f"`{_redacted_dsn(resolve_dsn(dsn))}`."
        )

    _render_external_preview(dsn, limit=10)


def _render_external_preview(dsn: str | None, *, limit: int) -> None:
    # There is no database file to stat now; an unreachable server or an
    # unbuilt schema surfaces through the except branch below instead.
    try:
        conn = connect(dsn)
        try:
            rows = recent_external_observations(conn, limit=limit)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - keep presentation panel resilient
        st.warning(f"External staging preview unavailable: {type(exc).__name__}: {exc}")
        return

    if not rows:
        st.info("No external outage observations staged yet.")
        return
    st.dataframe(rows, hide_index=True, width="stretch")
    if _has_columns(rows, "sourceRecordId", "outageCount"):
        _render_bar_chart(
            _chart_rows(rows, "sourceRecordId", "outageCount"),
            "Source record",
            "Outages",
        )


@st.cache_data(show_spinner=False)
def _county_geojson() -> dict[str, object]:
    return load_county_geojson()


def _render_county_heatmaps(dsn: str | None) -> None:
    st.subheader("County heat maps")
    try:
        probe = connect(dsn)
        try:
            ready = has_required_tables(probe)
        finally:
            probe.close()
    except Exception as exc:  # noqa: BLE001 - keep presentation panel resilient
        st.warning(f"County heat map unavailable: {type(exc).__name__}: {exc}")
        return
    if not ready:
        st.info("Run scripts/bootstrap_wiser.py before rendering county heat maps.")
        return

    try:
        conn = connect(dsn)
        try:
            storm_options = available_storm_options(conn, limit=100)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - keep presentation panel resilient
        st.warning(f"County heat map unavailable: {type(exc).__name__}: {exc}")
        return

    metric_key = st.selectbox(
        "Map metric",
        options=tuple(COUNTY_HEATMAP_METRICS),
        format_func=metric_display_name,
    )

    storm_by_code = {row["stormCode"]: row for row in storm_options}
    storm_scope = st.selectbox(
        "Storm scope",
        options=(None, *storm_by_code),
        format_func=lambda code: (
            "All storms"
            if code is None
            else f"{code} · {storm_by_code[code]['eventDateTime']} · {storm_by_code[code]['total_outage']:,} outages"
        ),
    )

    try:
        conn = connect(dsn)
        try:
            rows = county_metric_rows(conn, metric_key, storm_code=storm_scope)
        finally:
            conn.close()
        geojson = _county_geojson()
        title_suffix = "all storms" if storm_scope is None else f"storm {storm_scope}"
        svg = render_county_heatmap_svg(
            geojson,
            rows,
            metric_key,
            title=f"{metric_display_name(metric_key)} · {title_suffix}",
        )
    except Exception as exc:  # noqa: BLE001 - keep presentation panel resilient
        st.warning(f"County heat map unavailable: {type(exc).__name__}: {exc}")
        return

    _render_svg(svg)
    st.dataframe(rows, hide_index=True, width="stretch")
    st.caption(
        "Boundary source: State of Connecticut CT Counties legacy 8-county layer. "
        f"{CT_LEGACY_COUNTY_SOURCE_URL}"
    )


_SVG_FRAME_HEIGHT = 620
#: A comparison panel's frame must equal PANEL_MAP_HEIGHT. preserveAspectRatio
#: scales by min(frame_w/viewBox_w, frame_h/viewBox_h), so any frame shorter
#: than the viewBox shrinks the whole drawing — text included — and no font
#: size can compensate for a transform applied after it.
_SVG_PANEL_HEIGHT = PANEL_MAP_HEIGHT

# The SVG is served to the iframe as an HTML document. Two things have to be
# right or the map breaks:
#   charset — without it the browser decodes the data URI as latin-1 and every
#     non-ASCII character in the map (the "·" separator, county labels) renders
#     as mojibake. This is separate from any console codepage issue.
#   sizing  — the SVG carries width:100%;height:auto, so in a wide column its
#     height grows past the fixed iframe and the bottom of the map is clipped.
#     Pinning it to 100% of the frame and letting viewBox scale it keeps the
#     whole map visible at any width.
_SVG_FRAME_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>County heat map</title>
<style>
  html, body {{ margin: 0; padding: 0; height: 100%; background: transparent; }}
  svg {{ display: block; width: 100% !important; height: 100% !important; }}
</style></head>
<body>{svg}</body></html>"""


def _render_svg(svg, *, height: int = _SVG_FRAME_HEIGHT):
    document = _SVG_FRAME_TEMPLATE.format(svg=svg)
    encoded = base64.b64encode(document.encode("utf-8")).decode("ascii")
    components.iframe(
        f"data:text/html;charset=utf-8;base64,{encoded}",
        height=height,
    )


def render_result(
    result: HandlerResult,
    *,
    show_sql: bool,
    show_rag_context: bool,
    show_reasoning_prompt: bool,
) -> None:
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
        _render_rag(result, show_rag_context=show_rag_context)
    elif isinstance(result, UnknownRouteResult):
        _render_unknown(result)
    else:  # pragma: no cover - defensive
        st.error(f"Unhandled result type: {type(result).__name__}")


def _set_query(query: str) -> None:
    # st.chat_input cannot be pre-filled; example buttons queue a pending turn.
    st.session_state["pending_query"] = query


def _render_route_line(route: Route, reason: str) -> None:
    route_color = ROUTE_COLORS.get(route, "gray")
    st.markdown(f"**Route:** :{route_color}[{route.value}] — {reason}")


def main() -> None:
    st.set_page_config(page_title="WISER", page_icon="🌩️", layout="wide")
    st.title("🌩️ WISER — Weather Integrated Storm Event Response")
    st.caption(
        "Natural-language queries routed across four agents: data retrieval, "
        "SQL analytics, storm similarity, and RAG knowledge."
    )
    _ensure_conversation_state()

    # Same key-loading path as the CLI: repo-root .env or the environment.
    llm_config = openai_config()
    if not llm_config.api_key_present:
        st.info(
            "**OPENAI_API_KEY is not configured** (repo-root `.env` or environment). "
            "Analytics will use fixed fallback queries and impact summaries will be "
            "local extractive. Everything still works — no crash, no API calls.",
            icon="🔑",
        )

    with st.sidebar:
        st.header("Settings")
        # The widget shows a password-redacted DSN because its value lands in
        # screenshots and shared session history. The redacted string is not a
        # usable connection URL, so an untouched field maps back to the real
        # one; anything the user actually types is passed through as-is.
        _resolved_dsn = resolve_dsn(DEFAULT_DSN)
        _dsn_input = st.text_input(
            "PostgreSQL connection URL",
            value=_redacted_dsn(_resolved_dsn),
            help="Defaults to $DATABASE_URL, else the local Docker Compose instance.",
        )
        dsn = (
            _resolved_dsn
            if _dsn_input == _redacted_dsn(_resolved_dsn)
            else _dsn_input
        )
        input_csv = st.text_input("Source CSV", value=DEFAULT_INPUT_CSV)
        limit = st.slider("Result limit", min_value=1, max_value=50, value=DEFAULT_LIMIT)
        county = st.text_input(
            "County override (similarity)",
            value="",
            help="Used when the query itself does not name a county.",
        )

        st.subheader("SQL AI Agent")
        dry_run_sql = st.checkbox(
            "Dry-run SQL",
            value=False,
            help="Show the prompt that would be sent to the LLM without making an API call.",
        )
        show_sql = st.checkbox("Show generated SQL", value=True)
        show_reasoning_prompt = st.checkbox("Show reasoning prompt", value=False)

        st.subheader("RAG / external context")
        show_rag_context = st.checkbox("Show augmented context", value=False)
        rebuild_rag = st.checkbox("Rebuild RAG index", value=False)
        crawl_web_sources = st.checkbox(
            "Crawl external web sources",
            value=False,
            help="Fetch NOAA/NHC/NCEI pages before RAG retrieval (network).",
        )
        use_openai_summary = not st.checkbox(
            "Disable OpenAI summaries/reasoning",
            value=False,
            help="Force local extractive summaries and local analytical reasoning even if a key is set.",
        )
        similarity_backend = st.selectbox(
            "Text similarity backend", options=("auto", "faiss", "exact")
        )

        st.caption(
            f"OpenAI key: {'configured' if llm_config.api_key_present else 'not set'} · "
            f"model: {llm_config.model}",
            help="Set WISER_OPENAI_MODEL in the repo-root .env to change the model.",
        )
        if st.button(
            "Clear conversation",
            key="clear_conversation",
            width="stretch",
            disabled=not bool(st.session_state["messages"]),
            help="Clear saved chat results and SQL follow-up history.",
        ):
            _reset_conversation_state()
            st.rerun()

    routing_agent = TaskRoutingAgent()

    status_tab, map_tab, external_tab = st.tabs(
        ("Demo status", "County heat maps", "External outage staging")
    )
    with status_tab:
        _render_demo_status(dsn)
    with map_tab:
        _render_county_heatmaps(dsn)
    with external_tab:
        _render_external_feed_loader(dsn)

    st.markdown("**Example queries**")
    st.caption(
        "Colored by the agent each query routes to: "
        + " · ".join(
            f":{color}[{route.value}]"
            for route, color in ROUTE_COLORS.items()
            if route is not Route.UNKNOWN
        )
    )
    example_columns = st.columns(min(len(EXAMPLE_QUERIES), 4))
    for position, example in enumerate(EXAMPLE_QUERIES):
        example_color = ROUTE_COLORS.get(routing_agent.route(example).route, "gray")
        with example_columns[position % len(example_columns)]:
            st.button(
                f":{example_color}[{example}]",
                key=f"example_{position}",
                on_click=_set_query,
                args=(example,),
                width="stretch",
            )

    # Replay the transcript on every rerun.
    for message in st.session_state["messages"]:
        with st.chat_message(message["role"]):
            if message["role"] == "user":
                st.markdown(message["content"])
            elif "error" in message:
                st.error(message["error"])
            else:
                _render_route_line(message["route"], message["reason"])
                render_result(
                    message["result"],
                    show_sql=show_sql,
                    show_rag_context=show_rag_context,
                    show_reasoning_prompt=show_reasoning_prompt,
                )

    query = st.chat_input("e.g. top counties by outage") or st.session_state.pop(
        "pending_query", None
    )
    if not query or not query.strip():
        return
    query = query.strip()

    st.session_state["messages"].append({"role": "user", "content": query})
    with st.chat_message("user"):
        st.markdown(query)

    decision = resolve_route(
        routing_agent.route(query), st.session_state["last_route"]
    )
    with st.chat_message("assistant"):
        _render_route_line(decision.route, decision.reason)
        try:
            with st.spinner("Running agents..."):
                ctx = build_context(
                    query,
                    input_csv=input_csv,
                    dsn=dsn,
                    limit=limit,
                    county=county.strip() or None,
                    rebuild_rag=rebuild_rag,
                    crawl_web_sources=crawl_web_sources,
                    similarity_backend=similarity_backend,
                    use_openai_summary=use_openai_summary,
                    dry_run_sql=dry_run_sql,
                    history=tuple(st.session_state["sql_history"]),
                )
                try:
                    result = dispatch_route(decision.route, ctx)
                finally:
                    # Handlers may replace ctx.conn (database rebuild); close
                    # whichever connection is current.
                    ctx.conn.close()
        except Exception as exc:  # noqa: BLE001 - surface anything to the UI
            error_text = f"{type(exc).__name__}: {exc}"
            st.error(error_text)
            st.session_state["messages"].append(
                {"role": "assistant", "error": error_text}
            )
            return

        render_result(
            result,
            show_sql=show_sql,
            show_rag_context=show_rag_context,
            show_reasoning_prompt=show_reasoning_prompt,
        )

    st.session_state["messages"].append(
        {
            "role": "assistant",
            "route": decision.route,
            "reason": decision.reason,
            "result": result,
        }
    )
    turn = turn_from_result(query, result)
    if turn is not None:
        st.session_state["sql_history"].append(turn)
    if decision.route is not Route.UNKNOWN:
        st.session_state["last_route"] = decision.route


main()
