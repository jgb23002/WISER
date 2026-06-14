from __future__ import annotations

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"
REQUIRED_TABLES = frozenset({"Storm", "County", "WeatherData"})


def connect(db_path: str | Path, *, create: bool = True) -> sqlite3.Connection:
    path = Path(db_path)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.is_file():
        raise FileNotFoundError(f"Database not found at {path}")
    return sqlite3.connect(path)


def has_required_tables(conn: sqlite3.Connection) -> bool:
    table_names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    return REQUIRED_TABLES.issubset(table_names)


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
) -> None:
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
            :wgt5,
            :wgt9,
            :wgt13,
            :wgt18,
            :outage
        )
        """,
        weather_data_rows,
    )
    conn.commit()


def get_table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    result: dict[str, int] = {}
    for table_name in ("Storm", "County", "WeatherData"):
        count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        result[table_name] = int(count)
    return result
