import sys
import unittest
from pathlib import Path

import psycopg

SRC_DIR = Path(__file__).parent.parent / "src"
TESTS_DIR = Path(__file__).parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from pg_harness import blank_conn, make_conn  # noqa: E402

from wiser.database import (  # noqa: E402
    DEFAULT_LOCK_TIMEOUT_MS,
    connect,
    initialize_schema,
    load_current_weather_rows,
    load_hourly_weather_rows,
    load_normalized_rows,
)


class TestDatabaseInitialization(unittest.TestCase):
    def test_connect_sets_lock_timeout(self):
        """The SQLite busy_timeout became a PostgreSQL lock_timeout.

        These are not the same setting renamed: SQLite locked the whole
        database file, so busy_timeout meant "how long to wait for the
        database". PostgreSQL locks rows and tables, so lock_timeout means
        "how long one statement waits for a lock". The old test also asserted
        PRAGMA foreign_keys == 1; PostgreSQL has no such switch, so that
        assertion is replaced by the enforcement test below.
        """
        conn = connect()
        try:
            # SHOW normalizes the value for display ("30s"); pg_settings keeps
            # it in the setting's base unit, which for lock_timeout is ms.
            lock_timeout = conn.execute(
                "SELECT setting FROM pg_settings WHERE name = 'lock_timeout'"
            ).fetchone()[0]
        finally:
            conn.close()

        self.assertEqual(int(lock_timeout), DEFAULT_LOCK_TIMEOUT_MS)

    def test_foreign_keys_are_always_enforced(self):
        conn = make_conn()
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                """
                INSERT INTO WeatherData(
                    stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                    wgt5, wgt9, wgt13, wgt18, outage
                ) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (9999999999, "Nowhere", 1.0, 1.0, 0, 0, 0, 0, 0),
            )
        conn.rollback()

    def test_initialize_schema_replaces_legacy_stormcounty_table(self):
        conn = blank_conn()
        conn.execute(
            """
            CREATE TABLE Storm (
                stormCode BIGINT PRIMARY KEY,
                state TEXT NOT NULL,
                eventDateTime TEXT NOT NULL,
                eventLength INTEGER NOT NULL
            );

            CREATE TABLE County (
                County VARCHAR(64) PRIMARY KEY,
                ohLine INTEGER NOT NULL
            );

            CREATE TABLE StormCounty (
                stormCode BIGINT NOT NULL,
                County VARCHAR(64) NOT NULL,
                PRIMARY KEY (stormCode, County),
                FOREIGN KEY (stormCode) REFERENCES Storm(stormCode),
                FOREIGN KEY (County) REFERENCES County(County)
            );
            """
        )
        conn.commit()

        initialize_schema(conn)

        # information_schema replaces sqlite_master, and it stores the folded
        # (lowercase) identifier -- so the comparison folds too.
        table_names = {
            str(row[0]).lower()
            for row in conn.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = current_schema()
                  AND table_type = 'BASE TABLE'
                """
            )
        }
        self.assertIn("storm", table_names)
        self.assertIn("county", table_names)
        self.assertIn("weatherdata", table_names)
        self.assertIn("externaloutageobservation", table_names)
        self.assertNotIn("stormcounty", table_names)

    def test_load_normalized_rows_seeds_regions_metrics_and_hourly_rows(self):
        conn = make_conn()
        load_normalized_rows(
            conn,
            [
                {
                    "stormCode": 2005100800,
                    "eventDateTime": "2005-10-08 00:00:00",
                    "eventLength": 48,
                }
            ],
            [{"county": "Fairfield", "state": "CT", "ohLine": 1500}],
            [
                {
                    "stormCode": 2005100800,
                    "county": "Fairfield",
                    "sumAFWA_TOTPRECIP": 25.5,
                    "peakWSPD10": 9.2,
                    "wgt5": 4,
                    "wgt9": 2,
                    "wgt13": 1,
                    "wgt18": 0,
                    "outage": 120,
                }
            ],
        )
        metric_count = conn.execute(
            "SELECT COUNT(*) FROM StormMetricDefinition"
        ).fetchone()[0]
        region_count = conn.execute(
            "SELECT COUNT(*) FROM RegionCountyMapping"
        ).fetchone()[0]

        load_hourly_weather_rows(
            conn,
            [
                {
                    "stormCode": 2005100800,
                    "county": "Fairfield",
                    "hourOffset": 0,
                    "metricKey": "humidity",
                    "metricValue": 84.0,
                    "observedAt": "2005-10-08 00:00:00",
                    "sourceColumn": "humidity_h00",
                },
                {
                    "stormCode": 2005100800,
                    "county": "Fairfield",
                    "hourOffset": 1,
                    "metricKey": "temperature",
                    "metricValue": 70.0,
                    "observedAt": "2005-10-08 01:00:00",
                    "sourceColumn": "temperature_h01",
                },
            ],
        )
        hourly_count = conn.execute(
            "SELECT COUNT(*) FROM HourlyWeatherObservation"
        ).fetchone()[0]
        temperature_metric = conn.execute(
            "SELECT displayName FROM StormMetricDefinition WHERE metricKey = %s",
            ("temperature",),
        ).fetchone()[0]
        peak_gust = conn.execute(
            "SELECT peakGUST10 FROM WeatherData WHERE stormCode = %s",
            (2005100800,),
        ).fetchone()[0]

        self.assertGreater(metric_count, 0)
        self.assertGreater(region_count, 0)
        self.assertEqual(hourly_count, 2)
        self.assertEqual(temperature_metric, "Temperature")
        self.assertIsNone(peak_gust)

    def test_load_current_weather_rows_stages_current_observations(self):
        conn = make_conn()
        conn.execute(
            "INSERT INTO County(county, state, ohLine) VALUES(%s, %s, %s)",
            ("Fairfield", "CT", 1500),
        )
        load_current_weather_rows(
            conn,
            [
                {
                    "sourceSystem": "mock_feed",
                    "sourceRecordId": "obs-1",
                    "observedAt": "2026-08-11 14:00:00",
                    "county": "Fairfield",
                    "state": "CT",
                    "peakWSPD10": 9.1,
                    "peakGUST10": 17.2,
                    "sumAFWA_TOTPRECIP": 12.5,
                    "humidity": 82.0,
                    "outage": 44,
                    "rawPayload": "{}",
                }
            ],
        )

        row = conn.execute(
            "SELECT peakGUST10, humidity, outage FROM CurrentWeatherObservation"
        ).fetchone()
        self.assertEqual(tuple(row), (17.2, 82.0, 44))


if __name__ == "__main__":
    unittest.main()
