from __future__ import annotations

from dataclasses import dataclass

from wiser.storm_analysis import DbConnection
from wiser.query_engine import (
    county_storm_history,
    top_counties_by_outage,
    top_storms_by_outage,
)


@dataclass
class StormWeatherAnalyticsAgent:
    def county_outage_summary_from_db(
        self,
        conn: DbConnection,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        return top_counties_by_outage(conn, limit=limit)

    def top_storms_by_outage_from_db(
        self,
        conn: DbConnection,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        return top_storms_by_outage(conn, limit=limit)

    def county_storm_history_from_db(
        self,
        conn: DbConnection,
        county: str,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        return county_storm_history(conn, county=county, limit=limit)
