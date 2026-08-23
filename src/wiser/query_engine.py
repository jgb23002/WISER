from __future__ import annotations

from wiser.storm_analysis import DbConnection


def county_historical_count(conn: DbConnection, county: str) -> int:
    """Return the number of WeatherData rows for a county."""
    return conn.execute(
        "SELECT COUNT(*) FROM WeatherData WHERE LOWER(county) = LOWER(%s)",
        (county,),
    ).fetchone()[0]


def top_counties_by_outage(
    conn: DbConnection, limit: int = 10
) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            county,
            COUNT(*) AS storms_observed,
            SUM(outage) AS total_outage,
            ROUND(AVG(outage)::numeric, 2) AS avg_outage,
            MAX(outage) AS peak_outage
        FROM WeatherData
        GROUP BY county
        ORDER BY total_outage DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "county": row[0],
            "storms_observed": int(row[1]),
            "total_outage": int(row[2]),
            "avg_outage": float(row[3]),
            "peak_outage": int(row[4]),
        }
        for row in rows
    ]


def top_storms_by_outage(
    conn: DbConnection, limit: int = 10
) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            s.stormCode,
            s.eventDateTime,
            s.eventLength,
            SUM(w.outage) AS total_outage
        FROM Storm s
        JOIN WeatherData w ON w.stormCode = s.stormCode
        GROUP BY s.stormCode, s.eventDateTime, s.eventLength
        ORDER BY total_outage DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "stormCode": int(row[0]),
            "eventDateTime": row[1],
            "eventLength": int(row[2]),
            "total_outage": int(row[3]),
        }
        for row in rows
    ]


def county_storm_history(
    conn: DbConnection,
    county: str,
    limit: int = 10,
) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
            s.stormCode,
            s.eventDateTime,
            w.sumAFWA_TOTPRECIP,
            w.peakWSPD10,
            w.outage
        FROM WeatherData w
        JOIN Storm s ON s.stormCode = w.stormCode
        WHERE lower(w.county) = lower(%s)
        ORDER BY w.outage DESC, s.eventDateTime DESC
        LIMIT %s
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
