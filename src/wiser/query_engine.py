from __future__ import annotations

import sqlite3


def top_counties_by_outage(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            County,
            COUNT(*) AS storms_observed,
            SUM(outage) AS total_outage,
            ROUND(AVG(outage), 2) AS avg_outage,
            MAX(outage) AS peak_outage
        FROM StormCounty
        GROUP BY County
        ORDER BY total_outage DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "County": row[0],
            "storms_observed": int(row[1]),
            "total_outage": int(row[2]),
            "avg_outage": float(row[3]),
            "peak_outage": int(row[4]),
        }
        for row in rows
    ]


def top_storms_by_outage(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            s.stormCode,
            s.state,
            s.eventDateTime,
            s.eventLength,
            SUM(sc.outage) AS total_outage
        FROM Storm s
        JOIN StormCounty sc ON sc.stormCode = s.stormCode
        GROUP BY s.stormCode, s.state, s.eventDateTime, s.eventLength
        ORDER BY total_outage DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "stormCode": int(row[0]),
            "state": row[1],
            "eventDateTime": row[2],
            "eventLength": int(row[3]),
            "total_outage": int(row[4]),
        }
        for row in rows
    ]


def county_storm_history(
    conn: sqlite3.Connection,
    county: str,
    limit: int = 10,
) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            s.stormCode,
            s.eventDateTime,
            sc.sumAFWA_TOTPRECIP,
            sc.peakWSPD10,
            sc.outage
        FROM StormCounty sc
        JOIN Storm s ON s.stormCode = sc.stormCode
        WHERE lower(sc.County) = lower(?)
        ORDER BY sc.outage DESC, s.eventDateTime DESC
        LIMIT ?
        """,
        (county, limit),
    ).fetchall()
    return [
        {
            "stormCode": int(row[0]),
            "eventDateTime": row[1],
            "sumAFWA_TOTPRECIP": float(row[2]),
            "peakWSPD10": float(row[3]),
            "outage": int(row[4]),
        }
        for row in rows
    ]
