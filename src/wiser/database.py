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

        DROP TABLE IF EXISTS WeatherData;
        DROP TABLE IF EXISTS County;
        DROP TABLE IF EXISTS Storm;

        CREATE TABLE Storm (
            stormCode BIGINT PRIMARY KEY,
            eventDateTime TIMESTAMP NOT NULL,
            eventLength INTEGER NOT NULL,
            UNIQUE (eventDateTime),
            CHECK (eventLength > 0)
        );

        CREATE TABLE County (
            county VARCHAR(64) PRIMARY KEY,
            ohLine BIGINT NOT NULL,
            CHECK (ohLine > 0)
        );

        CREATE TABLE WeatherData (
            stormCode BIGINT NOT NULL,
            county VARCHAR(64) NOT NULL,
            sumAFWA_TOTPRECIP REAL NOT NULL,
            peakWSPD10 REAL NOT NULL,
            wgt5 INTEGER NOT NULL,
            wgt9 INTEGER NOT NULL,
            wgt13 INTEGER NOT NULL,
            wgt18 INTEGER NOT NULL,
            outage INTEGER NOT NULL,
            PRIMARY KEY (stormCode, county),
            FOREIGN KEY (stormCode) REFERENCES Storm(stormCode),
            FOREIGN KEY (county) REFERENCES County(county),
            CHECK (sumAFWA_TOTPRECIP >= 0),
            CHECK (peakWSPD10 > 0),
            CHECK (wgt5 >= 0),
            CHECK (wgt9 >= 0),
            CHECK (wgt13 >= 0),
            CHECK (wgt18 >= 0),
            CHECK (outage >= 0)
        );

        CREATE INDEX idx_weatherdata_outage ON WeatherData(outage DESC);
        CREATE INDEX idx_weatherdata_county ON WeatherData(county);
        CREATE INDEX idx_storm_datetime ON Storm(eventDateTime);
        """
    )
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
        INSERT INTO County(county, ohLine)
        VALUES(:County, :ohLine)
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
        weather_data_rows,
    )
    conn.commit()


def get_table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    result: dict[str, int] = {}
    for table_name in ("Storm", "County", "WeatherData"):
        count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        result[table_name] = int(count)
    return result
