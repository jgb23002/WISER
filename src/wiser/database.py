from __future__ import annotations

import os
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"

# Table names are written in camelCase for readability, but PostgreSQL folds
# unquoted identifiers to lowercase, so introspection against
# information_schema must compare against the folded form.
#
# Introspection also has to target current_schema() rather than a literal
# 'public': every DML statement here uses unqualified table names, which
# resolve through search_path. The test harness and the evaluation/smoke
# fixtures all pin search_path to a private schema, so asking about
# 'public' would report on a different database than the one being written.
REQUIRED_TABLES = frozenset(
    {
        "Storm",
        "County",
        "WeatherData",
        "RegionCountyMapping",
        "StormMetricDefinition",
        "HourlyWeatherObservation",
        "CurrentWeatherObservation",
    }
)
_REQUIRED_TABLES_FOLDED = frozenset(name.lower() for name in REQUIRED_TABLES)

DEFAULT_DSN = "postgresql://wiser:wiser123@localhost:5433/wiser"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 30.0
# PostgreSQL blocks on row/table locks rather than whole-file locks, so the
# SQLite busy_timeout is replaced by lock_timeout: how long a statement waits
# for a lock before giving up.
DEFAULT_LOCK_TIMEOUT_MS = int(DEFAULT_CONNECT_TIMEOUT_SECONDS * 1000)

DEFAULT_REGION_COUNTY_ROWS: tuple[dict[str, object], ...] = (
    {"region": "Northern CT", "county": "Hartford", "state": "CT"},
    {"region": "Northern CT", "county": "Litchfield", "state": "CT"},
    {"region": "Northern CT", "county": "Tolland", "state": "CT"},
    {"region": "Northern CT", "county": "Windham", "state": "CT"},
    {"region": "Southern CT", "county": "Fairfield", "state": "CT"},
    {"region": "Southern CT", "county": "Middlesex", "state": "CT"},
    {"region": "Southern CT", "county": "New Haven", "state": "CT"},
    {"region": "Southern CT", "county": "New London", "state": "CT"},
    {"region": "Eastern CT", "county": "New London", "state": "CT"},
    {"region": "Eastern CT", "county": "Tolland", "state": "CT"},
    {"region": "Eastern CT", "county": "Windham", "state": "CT"},
    {"region": "Western CT", "county": "Fairfield", "state": "CT"},
    {"region": "Western CT", "county": "Litchfield", "state": "CT"},
    {"region": "Central CT", "county": "Hartford", "state": "CT"},
    {"region": "Central CT", "county": "Middlesex", "state": "CT"},
    {"region": "Central CT", "county": "New Haven", "state": "CT"},
)

DEFAULT_METRIC_DEFINITION_ROWS: tuple[dict[str, object], ...] = (
    {
        "metricKey": "outage",
        "displayName": "Outages",
        "storedUnit": "trouble spots",
        "higherIsMoreSevere": 1,
        "sourceColumn": "outage",
    },
    {
        "metricKey": "peakWSPD10",
        "displayName": "Peak sustained wind",
        "storedUnit": "m/s",
        "higherIsMoreSevere": 1,
        "sourceColumn": "peakWSPD10",
    },
    {
        "metricKey": "peakGUST10",
        "displayName": "Peak wind gust",
        "storedUnit": "m/s",
        "higherIsMoreSevere": 1,
        "sourceColumn": "peakGUST10",
    },
    {
        "metricKey": "sumAFWA_TOTPRECIP",
        "displayName": "Total precipitation",
        "storedUnit": "mm",
        "higherIsMoreSevere": 1,
        "sourceColumn": "sumAFWA_TOTPRECIP",
    },
    {
        "metricKey": "wgt5",
        "displayName": "Gust hours above 5 m/s",
        "storedUnit": "hours",
        "higherIsMoreSevere": 1,
        "sourceColumn": "wgt5",
    },
    {
        "metricKey": "wgt9",
        "displayName": "Gust hours above 9 m/s",
        "storedUnit": "hours",
        "higherIsMoreSevere": 1,
        "sourceColumn": "wgt9",
    },
    {
        "metricKey": "wgt13",
        "displayName": "Gust hours above 13 m/s",
        "storedUnit": "hours",
        "higherIsMoreSevere": 1,
        "sourceColumn": "wgt13",
    },
    {
        "metricKey": "wgt18",
        "displayName": "Gust hours above 18 m/s",
        "storedUnit": "hours",
        "higherIsMoreSevere": 1,
        "sourceColumn": "wgt18",
    },
    {
        "metricKey": "humidity",
        "displayName": "Humidity",
        "storedUnit": "%",
        "higherIsMoreSevere": 1,
        "sourceColumn": None,
    },
)


def executemany(conn: psycopg.Connection, sql: str, rows) -> None:
    """Run a bulk statement, closing the cursor afterwards.

    psycopg puts ``executemany`` on the cursor, not the connection, so the
    obvious translation of ``conn.executemany(...)`` is
    ``conn.cursor().executemany(...)`` -- which leaks a cursor at every call
    site. This is that translation, done once.
    """
    with conn.cursor() as cursor:
        cursor.executemany(sql, rows)


def resolve_dsn(dsn: str | None = None) -> str:
    """Return the connection string to use.

    Precedence: explicit argument, then ``DATABASE_URL``, then the local
    Docker Compose default.
    """
    return dsn or os.environ.get("DATABASE_URL") or DEFAULT_DSN


def connect(
    dsn: str | None = None,
    *,
    timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
) -> psycopg.Connection:
    # libpq reads connect_timeout=0 as "wait forever", so a sub-second
    # timeout must not truncate to zero -- round up to one second instead.
    conn = psycopg.connect(
        resolve_dsn(dsn), connect_timeout=max(1, int(round(timeout)))
    )
    conn.execute(f"SET lock_timeout = {int(timeout * 1000)}")
    conn.commit()
    return conn


def has_required_tables(conn: psycopg.Connection) -> bool:
    table_names = {
        str(row[0]).lower()
        for row in conn.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'
            """
        ).fetchall()
    }
    if not _REQUIRED_TABLES_FOLDED.issubset(table_names):
        return False
    weather_columns = {
        str(row[0]).lower()
        for row in conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = %s
            """,
            ("weatherdata",),
        ).fetchall()
    }
    return "peakgust10" in weather_columns


def open_weather_db(dsn: str | None = None) -> psycopg.Connection:
    """Open an existing WISER database or raise a clear error."""
    conn = connect(dsn)
    if not has_required_tables(conn):
        conn.close()
        raise psycopg.OperationalError(
            "Database schema is incomplete. Run: python scripts/bootstrap_wiser.py"
        )
    return conn


def initialize_schema(conn: psycopg.Connection) -> None:
    # psycopg sends a parameterless query over the simple protocol, which
    # accepts the multi-statement schema file in one round trip. This is the
    # replacement for sqlite3's executescript().
    with SCHEMA_PATH.open("r", encoding="utf-8") as schema_file:
        conn.execute(schema_file.read())
    conn.commit()


def load_normalized_rows(
    conn: psycopg.Connection,
    storm_rows: list[dict[str, object]],
    county_rows: list[dict[str, object]],
    weather_data_rows: list[dict[str, object]],
    hourly_weather_rows: list[dict[str, object]] | None = None,
) -> None:
    weather_rows = [
        {**row, "peakGUST10": row.get("peakGUST10")}
        for row in weather_data_rows
    ]
    executemany(
        conn,
        """
        INSERT INTO Storm(stormCode, eventDateTime, eventLength)
        VALUES(%(stormCode)s, %(eventDateTime)s, %(eventLength)s)
        """,
        storm_rows,
    )
    executemany(
        conn,
        """
        INSERT INTO County(county, state, ohLine)
        VALUES(%(county)s, %(state)s, %(ohLine)s)
        """,
        county_rows,
    )
    executemany(
        conn,
        """
        INSERT INTO WeatherData(
            stormCode,
            county,
            sumAFWA_TOTPRECIP,
            peakWSPD10,
            peakGUST10,
            wgt5,
            wgt9,
            wgt13,
            wgt18,
            outage
        ) VALUES(
            %(stormCode)s,
            %(county)s,
            %(sumAFWA_TOTPRECIP)s,
            %(peakWSPD10)s,
            %(peakGUST10)s,
            %(wgt5)s,
            %(wgt9)s,
            %(wgt13)s,
            %(wgt18)s,
            %(outage)s
        )
        """,
        weather_rows,
    )
    ensure_reference_data(conn)
    if hourly_weather_rows:
        load_hourly_weather_rows(conn, hourly_weather_rows)
    conn.commit()


def ensure_reference_data(conn: psycopg.Connection) -> None:
    """Seed default metric definitions and CT regions when the tables exist."""
    if _table_exists(conn, "StormMetricDefinition"):
        executemany(
        conn,
            """
            INSERT INTO StormMetricDefinition(
                metricKey,
                displayName,
                storedUnit,
                higherIsMoreSevere,
                sourceColumn
            ) VALUES(
                %(metricKey)s,
                %(displayName)s,
                %(storedUnit)s,
                %(higherIsMoreSevere)s,
                %(sourceColumn)s
            )
            ON CONFLICT DO NOTHING
            """,
            DEFAULT_METRIC_DEFINITION_ROWS,
        )
    if _table_exists(conn, "RegionCountyMapping"):
        available_counties = {
            str(row[0]).lower()
            for row in conn.execute("SELECT county FROM County").fetchall()
        }
        rows = [
            row
            for row in DEFAULT_REGION_COUNTY_ROWS
            if str(row["county"]).lower() in available_counties
        ]
        executemany(
        conn,
            """
            INSERT INTO RegionCountyMapping(region, county, state)
            VALUES(%(region)s, %(county)s, %(state)s)
            ON CONFLICT DO NOTHING
            """,
            rows,
        )


def load_hourly_weather_rows(
    conn: psycopg.Connection,
    hourly_weather_rows: list[dict[str, object]],
) -> None:
    """Load normalized hourly weather rows into the optional hourly table."""
    if not _table_exists(conn, "HourlyWeatherObservation"):
        raise psycopg.OperationalError(
            "HourlyWeatherObservation is not present in this database schema"
        )
    ensure_reference_data(conn)
    _ensure_hourly_metric_definitions(conn, hourly_weather_rows)
    # SQLite's INSERT OR REPLACE deleted and reinserted the row; every column
    # is listed here, so updating all non-key columns is equivalent.
    executemany(
        conn,
        """
        INSERT INTO HourlyWeatherObservation(
            stormCode,
            county,
            hourOffset,
            metricKey,
            metricValue,
            observedAt,
            sourceColumn
        ) VALUES(
            %(stormCode)s,
            %(county)s,
            %(hourOffset)s,
            %(metricKey)s,
            %(metricValue)s,
            %(observedAt)s,
            %(sourceColumn)s
        )
        ON CONFLICT (stormCode, county, hourOffset, metricKey) DO UPDATE SET
            metricValue = EXCLUDED.metricValue,
            observedAt = EXCLUDED.observedAt,
            sourceColumn = EXCLUDED.sourceColumn
        """,
        hourly_weather_rows,
    )
    conn.commit()


def load_current_weather_rows(
    conn: psycopg.Connection,
    current_weather_rows: list[dict[str, object]],
) -> None:
    """Load current/live weather observations into the staging table."""
    if not _table_exists(conn, "CurrentWeatherObservation"):
        raise psycopg.OperationalError(
            "CurrentWeatherObservation is not present in this database schema"
        )
    executemany(
        conn,
        """
        INSERT INTO CurrentWeatherObservation(
            sourceSystem,
            sourceRecordId,
            observedAt,
            county,
            state,
            peakWSPD10,
            peakGUST10,
            sumAFWA_TOTPRECIP,
            humidity,
            outage,
            rawPayload
        ) VALUES(
            %(sourceSystem)s,
            %(sourceRecordId)s,
            %(observedAt)s,
            %(county)s,
            %(state)s,
            %(peakWSPD10)s,
            %(peakGUST10)s,
            %(sumAFWA_TOTPRECIP)s,
            %(humidity)s,
            %(outage)s,
            %(rawPayload)s
        )
        ON CONFLICT (sourceSystem, sourceRecordId, observedAt, county) DO UPDATE SET
            state = EXCLUDED.state,
            peakWSPD10 = EXCLUDED.peakWSPD10,
            peakGUST10 = EXCLUDED.peakGUST10,
            sumAFWA_TOTPRECIP = EXCLUDED.sumAFWA_TOTPRECIP,
            humidity = EXCLUDED.humidity,
            outage = EXCLUDED.outage,
            rawPayload = EXCLUDED.rawPayload
        """,
        current_weather_rows,
    )
    conn.commit()


def _ensure_hourly_metric_definitions(
    conn: psycopg.Connection,
    hourly_weather_rows: list[dict[str, object]],
) -> None:
    if not _table_exists(conn, "StormMetricDefinition"):
        return
    rows = [
        {
            "metricKey": str(metric_key),
            "displayName": _metric_display_name(str(metric_key)),
            "storedUnit": "unknown",
            "higherIsMoreSevere": 1,
            "sourceColumn": None,
        }
        for metric_key in sorted(
            {row["metricKey"] for row in hourly_weather_rows}
        )
    ]
    executemany(
        conn,
        """
        INSERT INTO StormMetricDefinition(
            metricKey,
            displayName,
            storedUnit,
            higherIsMoreSevere,
            sourceColumn
        ) VALUES(
            %(metricKey)s,
            %(displayName)s,
            %(storedUnit)s,
            %(higherIsMoreSevere)s,
            %(sourceColumn)s
        )
        ON CONFLICT DO NOTHING
        """,
        rows,
    )


def _metric_display_name(metric_key: str) -> str:
    return metric_key.replace("_", " ").strip().title() or metric_key


def _table_exists(conn: psycopg.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = current_schema() AND table_name = %s
        """,
        (table_name.lower(),),
    ).fetchone()
    return row is not None


def get_table_counts(conn: psycopg.Connection) -> dict[str, int]:
    result: dict[str, int] = {}
    for table_name in ("Storm", "County", "WeatherData"):
        count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        result[table_name] = int(count)
    return result
