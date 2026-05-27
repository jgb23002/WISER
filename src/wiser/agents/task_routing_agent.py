from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Route(str, Enum):
    DATA_RETRIEVAL = "data_retrieval"
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

    def route(self, query: str) -> RouteDecision:
        text = query.lower()
        # Analytics before data keywords so queries like "top counties from database"
        # are not misrouted by incidental words such as "database".
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
