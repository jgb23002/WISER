import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.database import (  # noqa: E402
    DEFAULT_SQLITE_BUSY_TIMEOUT_MS,
    connect,
    initialize_schema,
    load_current_weather_rows,
    load_hourly_weather_rows,
    load_normalized_rows,
)


class TestDatabaseInitialization(unittest.TestCase):
    def test_connect_sets_busy_timeout_and_foreign_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "wiser.db"
            conn = connect(db_path)
            try:
                busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
                foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(int(busy_timeout), DEFAULT_SQLITE_BUSY_TIMEOUT_MS)
        self.assertEqual(int(foreign_keys), 1)

    def test_initialize_schema_replaces_legacy_stormcounty_table(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(
                """
                PRAGMA foreign_keys = ON;

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
                    PRIMARY KEY (stormCode, County),
                    FOREIGN KEY (stormCode) REFERENCES Storm(stormCode),
                    FOREIGN KEY (County) REFERENCES County(County)
                );
                """
            )
            conn.commit()

            initialize_schema(conn)

            table_names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertIn("Storm", table_names)
            self.assertIn("County", table_names)
            self.assertIn("WeatherData", table_names)
            self.assertIn("ExternalOutageObservation", table_names)
            self.assertNotIn("StormCounty", table_names)
        finally:
            conn.close()

    def test_load_normalized_rows_seeds_regions_metrics_and_hourly_rows(self):
        conn = sqlite3.connect(":memory:")
        try:
            initialize_schema(conn)
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
                    }
                ],
            )
            hourly_count = conn.execute(
                "SELECT COUNT(*) FROM HourlyWeatherObservation"
            ).fetchone()[0]
            temperature_metric = conn.execute(
                "SELECT displayName FROM StormMetricDefinition WHERE metricKey = ?",
                ("temperature",),
            ).fetchone()[0]
            peak_gust = conn.execute(
                "SELECT peakGUST10 FROM WeatherData WHERE stormCode = ?",
                (2005100800,),
            ).fetchone()[0]

            self.assertGreater(metric_count, 0)
            self.assertGreater(region_count, 0)
            self.assertEqual(hourly_count, 2)
            self.assertEqual(temperature_metric, "Temperature")
            self.assertIsNone(peak_gust)
        finally:
            conn.close()

    def test_load_current_weather_rows_stages_current_observations(self):
        conn = sqlite3.connect(":memory:")
        try:
            initialize_schema(conn)
            conn.execute(
                "INSERT INTO County(county, state, ohLine) VALUES(?, ?, ?)",
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
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
