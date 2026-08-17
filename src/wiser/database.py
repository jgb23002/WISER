from __future__ import annotations

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"
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
DEFAULT_SQLITE_TIMEOUT_SECONDS = 30.0
DEFAULT_SQLITE_BUSY_TIMEOUT_MS = int(DEFAULT_SQLITE_TIMEOUT_SECONDS * 1000)

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


def connect(
    db_path: str | Path,
    *,
    create: bool = True,
    timeout: float = DEFAULT_SQLITE_TIMEOUT_SECONDS,
) -> sqlite3.Connection:
    path = Path(db_path)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.is_file():
        raise FileNotFoundError(f"Database not found at {path}")
    conn = sqlite3.connect(path, timeout=timeout)
    conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def has_required_tables(conn: sqlite3.Connection) -> bool:
    table_names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if not REQUIRED_TABLES.issubset(table_names):
        return False
    weather_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(WeatherData)").fetchall()
    }
    return "peakGUST10" in weather_columns


def open_weather_db(db_path: str | Path) -> sqlite3.Connection:
    """Open an existing WISER database or raise a clear error."""
    conn = connect(db_path, create=False)
    if not has_required_tables(conn):
        conn.close()
        raise sqlite3.OperationalError(
            "Database schema is incomplete. Run: python scripts/bootstrap_wiser.py"
        )
    return conn


def initialize_schema(conn: sqlite3.Connection) -> None:
    with SCHEMA_PATH.open("r", encoding="utf-8") as schema_file:
        conn.executescript(schema_file.read())
    conn.commit()


def load_normalized_rows(
    conn: sqlite3.Connection,
    storm_rows: list[dict[str, object]],
    county_rows: list[dict[str, object]],
    weather_data_rows: list[dict[str, object]],
    hourly_weather_rows: list[dict[str, object]] | None = None,
) -> None:
    weather_rows = [
        {**row, "peakGUST10": row.get("peakGUST10")}
        for row in weather_data_rows
    ]
    conn.executemany(
        """
        INSERT INTO Storm(stormCode, eventDateTime, eventLength)
        VALUES(:stormCode, :eventDateTime, :eventLength)
        """,
        storm_rows,
    )
    conn.executemany(
        """
        INSERT INTO County(county, state, ohLine)
        VALUES(:county, :state, :ohLine)
        """,
        county_rows,
    )
    conn.executemany(
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
            :stormCode,
            :county,
            :sumAFWA_TOTPRECIP,
            :peakWSPD10,
            :peakGUST10,
            :wgt5,
            :wgt9,
            :wgt13,
            :wgt18,
            :outage
        )
        """,
        weather_rows,
    )
    ensure_reference_data(conn)
    if hourly_weather_rows:
        load_hourly_weather_rows(conn, hourly_weather_rows)
    conn.commit()


def ensure_reference_data(conn: sqlite3.Connection) -> None:
    """Seed default metric definitions and CT regions when the tables exist."""
    if _table_exists(conn, "StormMetricDefinition"):
        conn.executemany(
            """
            INSERT OR IGNORE INTO StormMetricDefinition(
                metricKey,
                displayName,
                storedUnit,
                higherIsMoreSevere,
                sourceColumn
            ) VALUES(
                :metricKey,
                :displayName,
                :storedUnit,
                :higherIsMoreSevere,
                :sourceColumn
            )
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
        conn.executemany(
            """
            INSERT OR IGNORE INTO RegionCountyMapping(region, county, state)
            VALUES(:region, :county, :state)
            """,
            rows,
        )


def load_hourly_weather_rows(
    conn: sqlite3.Connection,
    hourly_weather_rows: list[dict[str, object]],
) -> None:
    """Load normalized hourly weather rows into the optional hourly table."""
    if not _table_exists(conn, "HourlyWeatherObservation"):
        raise sqlite3.OperationalError(
            "HourlyWeatherObservation is not present in this database schema"
        )
    ensure_reference_data(conn)
    _ensure_hourly_metric_definitions(conn, hourly_weather_rows)
    conn.executemany(
        """
        INSERT OR REPLACE INTO HourlyWeatherObservation(
            stormCode,
            county,
            hourOffset,
            metricKey,
            metricValue,
            observedAt,
            sourceColumn
        ) VALUES(
            :stormCode,
            :county,
            :hourOffset,
            :metricKey,
            :metricValue,
            :observedAt,
            :sourceColumn
        )
        """,
        hourly_weather_rows,
    )
    conn.commit()


def load_current_weather_rows(
    conn: sqlite3.Connection,
    current_weather_rows: list[dict[str, object]],
) -> None:
    """Load current/live weather observations into the staging table."""
    if not _table_exists(conn, "CurrentWeatherObservation"):
        raise sqlite3.OperationalError(
            "CurrentWeatherObservation is not present in this database schema"
        )
    conn.executemany(
        """
        INSERT OR REPLACE INTO CurrentWeatherObservation(
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
            :sourceSystem,
            :sourceRecordId,
            :observedAt,
            :county,
            :state,
            :peakWSPD10,
            :peakGUST10,
            :sumAFWA_TOTPRECIP,
            :humidity,
            :outage,
            :rawPayload
        )
        """,
        current_weather_rows,
    )
    conn.commit()


def _ensure_hourly_metric_definitions(
    conn: sqlite3.Connection,
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
    conn.executemany(
        """
        INSERT OR IGNORE INTO StormMetricDefinition(
            metricKey,
            displayName,
            storedUnit,
            higherIsMoreSevere,
            sourceColumn
        ) VALUES(
            :metricKey,
            :displayName,
            :storedUnit,
            :higherIsMoreSevere,
            :sourceColumn
        )
        """,
        rows,
    )


def _metric_display_name(metric_key: str) -> str:
    return metric_key.replace("_", " ").strip().title() or metric_key


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?",
        ("table", table_name),
    ).fetchone()
    return row is not None


def get_table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    result: dict[str, int] = {}
    for table_name in ("Storm", "County", "WeatherData"):
        count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        result[table_name] = int(count)
    return result
