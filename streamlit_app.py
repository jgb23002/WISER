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

import sys
from pathlib import Path

import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.agents.task_routing_agent import Route, TaskRoutingAgent
from wiser.handlers import (
    DEFAULT_DB_PATH,
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
)
from wiser.openai_summarizer import openai_config
from wiser.rag import format_excerpt

ROUTE_LABELS = {
    Route.DATA_RETRIEVAL: ":green[data_retrieval]",
    Route.WEATHER_ANALYTICS: ":orange[weather_analytics]",
    Route.SIMILARITY_SEARCH: ":blue[similarity_search]",
    Route.RAG_KNOWLEDGE: ":violet[rag_knowledge]",
    Route.UNKNOWN: ":red[unknown]",
}

_NOTICE_RENDERERS = {"error": st.error, "warning": st.warning, "info": st.info}


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


def _render_analytics(result: AnalyticsResult, *, show_sql: bool) -> None:
    if result.dry_run_prompt is not None:
        st.subheader("SQL AI Agent dry-run prompt")
        st.caption("No API call was made.")
        st.code(result.dry_run_prompt, language="text")
        return

    _render_notices(result.notices)

    if result.used_fallback:
        st.caption("Answered with fixed fallback queries (SQL AI Agent unavailable).")
        if result.fallback_title is not None:
            st.subheader(result.fallback_title.rstrip(":"))
            _render_rows(result.rows or [])
        return

    st.caption("Answered by the SQL AI Agent (LLM-generated SQL, read-only execution).")
    if result.answer:
        st.markdown(f"**{result.answer}**")
    if show_sql and result.sql:
        st.code(result.sql, language="sql")
    _render_rows(result.rows or [])


def _render_rows(rows: list[dict[str, object]]) -> None:
    if not rows:
        st.info("No results found.")
        return
    st.dataframe(rows, hide_index=True, width="stretch")


def _render_similarity(result: SimilarityResult) -> None:
    if not result.ok:
        _render_notices(result.notices)
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
            f"({web_summary.documents_indexed} refreshed, "
            f"{len(web_summary.failures)} failed) · "
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


def render_result(
    result: HandlerResult,
    *,
    show_sql: bool,
    show_rag_context: bool,
) -> None:
    if isinstance(result, DataRetrievalResult):
        _render_data_retrieval(result)
    elif isinstance(result, AnalyticsResult):
        _render_analytics(result, show_sql=show_sql)
    elif isinstance(result, SimilarityResult):
        _render_similarity(result)
    elif isinstance(result, RagKnowledgeResult):
        _render_rag(result, show_rag_context=show_rag_context)
    elif isinstance(result, UnknownRouteResult):
        _render_unknown(result)
    else:  # pragma: no cover - defensive
        st.error(f"Unhandled result type: {type(result).__name__}")


def _set_query(query: str) -> None:
    st.session_state["query_input"] = query
    st.session_state["auto_run"] = True


def main() -> None:
    st.set_page_config(page_title="WISER", page_icon="🌩️", layout="wide")
    st.title("🌩️ WISER — Weather Impact Storm Event Reasoner")
    st.caption(
        "Natural-language queries routed across four agents: data retrieval, "
        "SQL analytics, storm similarity, and RAG knowledge."
    )

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
        db_path = st.text_input("SQLite database", value=DEFAULT_DB_PATH)
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
        sql_model = st.text_input(
            "SQL model", value="", help="Default: WISER_OPENAI_MODEL or gpt-4o-mini."
        )

        st.subheader("RAG / external context")
        show_rag_context = st.checkbox("Show augmented context", value=False)
        rebuild_rag = st.checkbox("Rebuild RAG index", value=False)
        crawl_web_sources = st.checkbox(
            "Crawl external web sources",
            value=False,
            help="Fetch NOAA/NHC/NCEI pages before RAG retrieval (network).",
        )
        use_openai_summary = not st.checkbox(
            "Disable OpenAI summaries",
            value=False,
            help="Force the local extractive impact summary even if a key is set.",
        )
        similarity_backend = st.selectbox(
            "Text similarity backend", options=("auto", "faiss", "exact")
        )
        openai_model = st.text_input(
            "Summary model", value="", help="Default: WISER_OPENAI_MODEL or gpt-5.5."
        )

        st.caption(
            f"OpenAI key: {'configured' if llm_config.api_key_present else 'not set'} · "
            f"model default: {llm_config.model}"
        )

    st.markdown("**Example queries**")
    example_columns = st.columns(min(len(EXAMPLE_QUERIES), 4))
    for position, example in enumerate(EXAMPLE_QUERIES):
        with example_columns[position % len(example_columns)]:
            st.button(
                example,
                key=f"example_{position}",
                on_click=_set_query,
                args=(example,),
                width="stretch",
            )

    query = st.text_input(
        "Query",
        key="query_input",
        placeholder="e.g. top counties by outage",
    )
    run_clicked = st.button("Run query", type="primary")
    auto_run = st.session_state.pop("auto_run", False)

    if not (run_clicked or auto_run):
        return
    if not query.strip():
        st.warning("Enter a query first.")
        return

    decision = TaskRoutingAgent().route(query)
    route_label = ROUTE_LABELS.get(decision.route, decision.route.value)
    st.markdown(f"**Route:** {route_label} — {decision.reason}")

    try:
        with st.spinner("Running agents..."):
            ctx = build_context(
                query,
                input_csv=input_csv,
                db_path=db_path,
                limit=limit,
                county=county.strip() or None,
                rebuild_rag=rebuild_rag,
                crawl_web_sources=crawl_web_sources,
                similarity_backend=similarity_backend,
                openai_model=openai_model.strip() or None,
                use_openai_summary=use_openai_summary,
                sql_model=sql_model.strip() or None,
                dry_run_sql=dry_run_sql,
            )
            try:
                result = dispatch_route(decision.route, ctx)
            finally:
                # Handlers may replace ctx.conn (database rebuild); close
                # whichever connection is current.
                ctx.conn.close()
    except Exception as exc:  # noqa: BLE001 - surface anything to the UI
        st.error(f"{type(exc).__name__}: {exc}")
        return

    render_result(result, show_sql=show_sql, show_rag_context=show_rag_context)


main()
