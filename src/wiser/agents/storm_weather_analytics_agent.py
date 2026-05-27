from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from wiser.agents.data_retrieval_processing_agent import DataRetrievalProcessingAgent
from wiser.query_engine import (
    county_storm_history,
    top_counties_by_outage,
    top_storms_by_outage,
)


@dataclass
class StormWeatherAnalyticsAgent:
    data_agent: DataRetrievalProcessingAgent

    def county_outage_summary_from_db(
        self,
        conn: sqlite3.Connection,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        return top_counties_by_outage(conn, limit=limit)

    def top_storms_by_outage_from_db(
        self,
        conn: sqlite3.Connection,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        return top_storms_by_outage(conn, limit=limit)

    def county_storm_history_from_db(
        self,
        conn: sqlite3.Connection,
        county: str,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        return county_storm_history(conn, county=county, limit=limit)
