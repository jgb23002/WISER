from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path)


def initialize_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;

        DROP TABLE IF EXISTS StormCounty;
        DROP TABLE IF EXISTS County;
        DROP TABLE IF EXISTS Storm;

        CREATE TABLE Storm (
            stormCode INTEGER PRIMARY KEY,
            state TEXT NOT NULL,
            eventDateTime TEXT NOT NULL,
            eventLength INTEGER NOT NULL
        );

        CREATE TABLE County (
            County TEXT PRIMARY KEY,
            ohLine INTEGER NOT NULL
        );

        CREATE TABLE StormCounty (
            stormCode INTEGER NOT NULL,
            County TEXT NOT NULL,
            sumAFWA_TOTPRECIP REAL NOT NULL,
            peakWSPD10 REAL NOT NULL,
            wgt5 INTEGER NOT NULL,
            wgt9 INTEGER NOT NULL,
            wgt13 INTEGER NOT NULL,
            wgt18 INTEGER NOT NULL,
            outage INTEGER NOT NULL,
            PRIMARY KEY (stormCode, County),
            FOREIGN KEY (stormCode) REFERENCES Storm (stormCode),
            FOREIGN KEY (County) REFERENCES County (County)
        );

        CREATE INDEX idx_storm_county_outage ON StormCounty(outage);
        CREATE INDEX idx_storm_county_county ON StormCounty(County);
        """
    )
    conn.commit()


def load_normalized_rows(
    conn: sqlite3.Connection,
    storm_rows: list[dict[str, object]],
    county_rows: list[dict[str, object]],
    storm_county_rows: list[dict[str, object]],
) -> None:
    conn.executemany(
        """
        INSERT INTO Storm(stormCode, state, eventDateTime, eventLength)
        VALUES(:stormCode, :state, :eventDateTime, :eventLength)
        """,
        storm_rows,
    )
    conn.executemany(
        """
        INSERT INTO County(County, ohLine)
        VALUES(:County, :ohLine)
        """,
        county_rows,
    )
    conn.executemany(
        """
        INSERT INTO StormCounty(
            stormCode,
            County,
            sumAFWA_TOTPRECIP,
            peakWSPD10,
            wgt5,
            wgt9,
            wgt13,
            wgt18,
            outage
        ) VALUES(
            :stormCode,
            :County,
            :sumAFWA_TOTPRECIP,
            :peakWSPD10,
            :wgt5,
            :wgt9,
            :wgt13,
            :wgt18,
            :outage
        )
        """,
        storm_county_rows,
    )
    conn.commit()


def get_table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    result: dict[str, int] = {}
    for table_name in ("Storm", "County", "StormCounty"):
        count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        result[table_name] = int(count)
    return result
