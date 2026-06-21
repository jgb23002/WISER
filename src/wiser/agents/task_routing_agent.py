from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Route(str, Enum):
    DATA_RETRIEVAL = "data_retrieval"
    RAG_KNOWLEDGE = "rag_knowledge"
    SIMILARITY_SEARCH = "similarity_search"
    WEATHER_ANALYTICS = "weather_analytics"
    UNKNOWN = "unknown"


@dataclass
class RouteDecision:
    route: Route
    reason: str


class TaskRoutingAgent:
    DATA_KEYWORDS = {
        "load",
        "retrieve",
        "raw",
        "csv",
        "schema",
        "table",
        "database",
        "db",
        "sqlite",
        "ingest",
        "normalize",
    }
    ANALYTICS_KEYWORDS = {
        "analyze",
        "analysis",
        "predict",
        "outage",
        "storm",
        "weather",
        "compare",
        "trend",
        "top",
        "county",
        "counties",
    }
    RAG_KEYWORDS = {
        "algorithm",
        "context",
        "describe",
        "docs",
        "document",
        "documentation",
        "explain",
        "how does",
        "hurricane",
        "impact",
        "knowledge",
        "method",
        "ncei",
        "nhc",
        "prompt",
        "rag",
        "reference",
        "steps",
        "summarize",
    }
    # Bare "similarity" is intentionally excluded from RAG_KEYWORDS so numeric
    # queries like "find storm similarity wind=9.2" are not swallowed by RAG.
    # Explanation queries still match via "explain", "how does", "algorithm", etc.
    SIMILARITY_KEYWORDS = {
        "similar",
        "find similar",
        "similar to",
        "rank storms",
    }
    # Matches wind=, precip=, wgt5=, etc. — same aliases the CLI parser accepts.
    _NUMERIC_PARAM_PATTERN = re.compile(
        r"\b(?:wind|windspeed|wind_speed|peakwspd10|precip|precipitation|"
        r"sumafwa_totprecip|wgt\d{1,2})\s*=",
        re.IGNORECASE,
    )

    def route(self, query: str) -> RouteDecision:
        text = query.lower()
        # RAG first: explanation queries match via "explain", "how does",
        # "algorithm", etc.  Numeric param queries are excluded so
        # "find storm similarity wind=9.2" reaches SIMILARITY_SEARCH below.
        if any(keyword in text for keyword in self.RAG_KEYWORDS) and not (
            self._NUMERIC_PARAM_PATTERN.search(query)
        ):
            return RouteDecision(
                route=Route.RAG_KNOWLEDGE,
                reason="Matched documentation/RAG knowledge keywords.",
            )
        # SIMILARITY_SEARCH before ANALYTICS: keyword or numeric-param match.
        if any(keyword in text for keyword in self.SIMILARITY_KEYWORDS) or (
            self._NUMERIC_PARAM_PATTERN.search(query)
        ):
            return RouteDecision(
                route=Route.SIMILARITY_SEARCH,
                reason="Matched similarity search keywords.",
            )
        # Analytics before data: "top counties from database" is analytics,
        # not a data-load operation.
        if any(keyword in text for keyword in self.ANALYTICS_KEYWORDS):
            return RouteDecision(
                route=Route.WEATHER_ANALYTICS,
                reason="Matched weather/outage analytics keywords.",
            )
        if any(keyword in text for keyword in self.DATA_KEYWORDS):
            return RouteDecision(
                route=Route.DATA_RETRIEVAL,
                reason="Matched data/database-oriented keywords.",
            )
        return RouteDecision(
            route=Route.UNKNOWN,
            reason="No strong keyword match; needs manual decision.",
        )
