"""
Tests for WISER database schema validation and integrity.

This test suite validates:
1. Table structure and column definitions
2. Primary key and foreign key constraints
3. CHECK constraints
4. Unique constraints
5. Index creation
6. Data type correctness
7. Constraint enforcement with actual data
"""

import sqlite3
import unittest
from datetime import datetime
from pathlib import Path


class TestSchemaStructure(unittest.TestCase):
    """Test database schema table and column definitions."""

    @classmethod
    def setUpClass(cls):
        """Create in-memory database and load schema."""
        cls.conn = sqlite3.connect(":memory:")
        cls.conn.row_factory = sqlite3.Row

        # Load schema from file
        schema_path = Path(__file__).parent.parent / "sql" / "schema.sql"
        with open(schema_path, "r") as f:
            schema_sql = f.read()

        cls.conn.executescript(schema_sql)
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        """Close database connection."""
        cls.conn.close()

    def test_storm_table_exists(self):
        """Test that Storm table exists."""
        cursor = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='Storm'"
        )
        self.assertIsNotNone(cursor.fetchone())

    def test_county_table_exists(self):
        """Test that County table exists."""
        cursor = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='County'"
        )
        self.assertIsNotNone(cursor.fetchone())

    def test_weatherdata_table_exists(self):
        """Test that WeatherData table exists."""
        cursor = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='WeatherData'"
        )
        self.assertIsNotNone(cursor.fetchone())

    def test_external_outage_table_exists(self):
        """Test that ExternalOutageObservation table exists."""
        cursor = self.conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='ExternalOutageObservation'
            """
        )
        self.assertIsNotNone(cursor.fetchone())

    def test_sponsor_support_tables_exist(self):
        """Test that region, metric, and hourly support tables exist."""
        for table_name in (
            "RegionCountyMapping",
            "StormMetricDefinition",
            "HourlyWeatherObservation",
        ):
            with self.subTest(table=table_name):
                cursor = self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table_name,),
                )
                self.assertIsNotNone(cursor.fetchone())

    def test_storm_columns(self):
        """Test Storm table has correct columns."""
        cursor = self.conn.execute("PRAGMA table_info(Storm)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}

        self.assertIn("stormCode", columns)
        self.assertIn("eventDateTime", columns)
        self.assertIn("eventLength", columns)

        # Verify data types
        self.assertEqual(columns["stormCode"], "BIGINT")
        self.assertEqual(columns["eventDateTime"], "TIMESTAMP")
        self.assertEqual(columns["eventLength"], "INTEGER")

    def test_county_columns(self):
        """Test County table has correct columns."""
        cursor = self.conn.execute("PRAGMA table_info(County)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}

        self.assertIn("county", columns)
        self.assertIn("state", columns)
        self.assertIn("ohLine", columns)

        # Verify data types
        self.assertEqual(columns["county"], "VARCHAR(64)")
        self.assertEqual(columns["state"], "VARCHAR(2)")
        self.assertEqual(columns["ohLine"], "BIGINT")

    def test_weatherdata_columns(self):
        """Test WeatherData table has correct columns."""
        cursor = self.conn.execute("PRAGMA table_info(WeatherData)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}

        expected_columns = {
            "stormCode": "BIGINT",
            "county": "VARCHAR(64)",
            "sumAFWA_TOTPRECIP": "REAL",
            "peakWSPD10": "REAL",
            "wgt5": "INTEGER",
            "wgt9": "INTEGER",
            "wgt13": "INTEGER",
            "wgt18": "INTEGER",
            "outage": "INTEGER",
        }

        for col_name, col_type in expected_columns.items():
            self.assertIn(col_name, columns)
            self.assertEqual(columns[col_name], col_type)

    def test_external_outage_columns(self):
        """Test ExternalOutageObservation table has correct columns."""
        cursor = self.conn.execute("PRAGMA table_info(ExternalOutageObservation)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}

        expected_columns = {
            "id": "INTEGER",
            "sourceSystem": "TEXT",
            "sourceRecordId": "TEXT",
            "observedAt": "TIMESTAMP",
            "county": "VARCHAR(64)",
            "serviceTerritory": "TEXT",
            "outageCount": "INTEGER",
            "customerCount": "INTEGER",
            "restorationStatus": "TEXT",
            "rawPayload": "TEXT",
        }

        for col_name, col_type in expected_columns.items():
            self.assertIn(col_name, columns)
            self.assertEqual(columns[col_name], col_type)

    def test_hourly_weather_columns(self):
        """Test HourlyWeatherObservation supports long-form hourly metrics."""
        cursor = self.conn.execute("PRAGMA table_info(HourlyWeatherObservation)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}

        expected_columns = {
            "stormCode": "BIGINT",
            "county": "VARCHAR(64)",
            "hourOffset": "INTEGER",
            "metricKey": "TEXT",
            "metricValue": "REAL",
            "observedAt": "TIMESTAMP",
            "sourceColumn": "TEXT",
        }

        for col_name, col_type in expected_columns.items():
            self.assertIn(col_name, columns)
            self.assertEqual(columns[col_name], col_type)

    def test_region_and_metric_indexes_exist(self):
        cursor = self.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        index_names = {row[0] for row in cursor.fetchall()}

        self.assertIn("idx_region_county_state", index_names)
        self.assertIn("idx_hourly_weather_storm_county_hour", index_names)
        self.assertIn("idx_hourly_weather_metric", index_names)

    def test_storm_primary_key(self):
        """Test Storm has correct primary key."""
        cursor = self.conn.execute("PRAGMA table_info(Storm)")
        pk_columns = [row[1] for row in cursor.fetchall() if row[5] != 0]
        self.assertEqual(pk_columns, ["stormCode"])

    def test_county_primary_key(self):
        """Test County has correct primary key."""
        cursor = self.conn.execute("PRAGMA table_info(County)")
        pk_columns = [row[1] for row in cursor.fetchall() if row[5] != 0]
        self.assertEqual(pk_columns, ["county"])

    def test_weatherdata_primary_key(self):
        """Test WeatherData has composite primary key."""
        cursor = self.conn.execute("PRAGMA table_info(WeatherData)")
        pk_columns = sorted([row[1] for row in cursor.fetchall() if row[5] != 0])
        self.assertEqual(pk_columns, ["county", "stormCode"])

    def test_indexes_exist(self):
        """Test that all required indexes are created."""
        cursor = self.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        index_names = {row[0] for row in cursor.fetchall()}

        self.assertIn("idx_weatherdata_outage", index_names)
        self.assertIn("idx_weatherdata_county", index_names)
        self.assertIn("idx_storm_datetime", index_names)
        self.assertIn("idx_external_outage_source_time", index_names)
        self.assertIn("idx_external_outage_county", index_names)


class TestConstraints(unittest.TestCase):
    """Test constraint enforcement."""

    def setUp(self):
        """Create fresh in-memory database for each test."""
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

        # Load schema from file
        schema_path = Path(__file__).parent.parent / "sql" / "schema.sql"
        with open(schema_path, "r") as f:
            schema_sql = f.read()

        self.conn.executescript(schema_sql)
        self.conn.commit()

    def tearDown(self):
        """Close database connection."""
        self.conn.close()

    def test_insert_valid_storm(self):
        """Test inserting valid storm record."""
        self.conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(?, ?, ?)",
            (2005100800, "2005-10-08 00:00:00", 48),
        )
        self.conn.commit()

        cursor = self.conn.execute(
            "SELECT * FROM Storm WHERE stormCode = ?", (2005100800,)
        )
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["stormCode"], 2005100800)

    def test_storm_eventlength_check_constraint(self):
        """Test that eventLength must be > 0."""
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(?, ?, ?)",
                (2005100800, "2005-10-08 00:00:00", 0),
            )
            self.conn.commit()

    def test_storm_unique_datetime_constraint(self):
        """Test that eventDateTime must be unique."""
        # Insert first record
        self.conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(?, ?, ?)",
            (2005100800, "2005-10-08 00:00:00", 48),
        )
        self.conn.commit()

        # Try to insert duplicate datetime
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(?, ?, ?)",
                (2005100801, "2005-10-08 00:00:00", 48),
            )
            self.conn.commit()

    def test_county_primary_key_constraint(self):
        """Test that county name is unique primary key."""
        # Insert first record
        self.conn.execute(
            "INSERT INTO County(county, ohLine) VALUES(?, ?)", ("Fairfield", 4887830)
        )
        self.conn.commit()

        # Try to insert duplicate county
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO County(county, ohLine) VALUES(?, ?)",
                ("Fairfield", 5000000),
            )
            self.conn.commit()

    def test_county_ohline_check_constraint(self):
        """Test that ohLine must be > 0."""
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO County(county, ohLine) VALUES(?, ?)", ("Fairfield", 0)
            )
            self.conn.commit()

    def test_county_ohline_negative_check_constraint(self):
        """Test that ohLine must not be negative."""
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO County(county, ohLine) VALUES(?, ?)", ("Fairfield", -1000)
            )
            self.conn.commit()

    def test_insert_valid_weatherdata(self):
        """Test inserting valid weather data record."""
        # Insert prerequisite storm and county
        self.conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(?, ?, ?)",
            (2005100800, "2005-10-08 00:00:00", 48),
        )
        self.conn.execute(
            "INSERT INTO County(county, ohLine) VALUES(?, ?)", ("Fairfield", 4887830)
        )
        self.conn.commit()

        # Insert weather data
        self.conn.execute(
            """INSERT INTO WeatherData(
                stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                wgt5, wgt9, wgt13, wgt18, outage
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (2005100800, "Fairfield", 231.98, 5.79, 9, 0, 0, 0, 186),
        )
        self.conn.commit()

        cursor = self.conn.execute(
            "SELECT * FROM WeatherData WHERE stormCode = ? AND county = ?",
            (2005100800, "Fairfield"),
        )
        row = cursor.fetchone()
        self.assertIsNotNone(row)

    def test_weatherdata_foreign_key_storm(self):
        """Test that stormCode must exist in Storm table."""
        # Insert county but not storm
        self.conn.execute(
            "INSERT INTO County(county, ohLine) VALUES(?, ?)", ("Fairfield", 4887830)
        )
        self.conn.commit()

        # Try to insert weather data with non-existent storm
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """INSERT INTO WeatherData(
                    stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                    wgt5, wgt9, wgt13, wgt18, outage
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (2005100800, "Fairfield", 231.98, 5.79, 9, 0, 0, 0, 186),
            )
            self.conn.commit()

    def test_weatherdata_foreign_key_county(self):
        """Test that county must exist in County table."""
        # Insert storm but not county
        self.conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(?, ?, ?)",
            (2005100800, "2005-10-08 00:00:00", 48),
        )
        self.conn.commit()

        # Try to insert weather data with non-existent county
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """INSERT INTO WeatherData(
                    stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                    wgt5, wgt9, wgt13, wgt18, outage
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (2005100800, "Fairfield", 231.98, 5.79, 9, 0, 0, 0, 186),
            )
            self.conn.commit()

    def test_weatherdata_sumafwa_totprecip_check(self):
        """Test that sumAFWA_TOTPRECIP must be >= 0."""
        # Setup
        self.conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(?, ?, ?)",
            (2005100800, "2005-10-08 00:00:00", 48),
        )
        self.conn.execute(
            "INSERT INTO County(county, ohLine) VALUES(?, ?)", ("Fairfield", 4887830)
        )
        self.conn.commit()

        # Try to insert with negative precipitation
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """INSERT INTO WeatherData(
                    stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                    wgt5, wgt9, wgt13, wgt18, outage
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (2005100800, "Fairfield", -10.0, 5.79, 9, 0, 0, 0, 186),
            )
            self.conn.commit()

    def test_weatherdata_peakwspd10_check(self):
        """Test that peakWSPD10 must be > 0."""
        # Setup
        self.conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(?, ?, ?)",
            (2005100800, "2005-10-08 00:00:00", 48),
        )
        self.conn.execute(
            "INSERT INTO County(county, ohLine) VALUES(?, ?)", ("Fairfield", 4887830)
        )
        self.conn.commit()

        # Try to insert with zero wind speed
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """INSERT INTO WeatherData(
                    stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                    wgt5, wgt9, wgt13, wgt18, outage
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (2005100800, "Fairfield", 231.98, 0.0, 9, 0, 0, 0, 186),
            )
            self.conn.commit()

    def test_weatherdata_wgt_values_check(self):
        """Test that wgt5-18 values must be >= 0."""
        # Setup
        self.conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(?, ?, ?)",
            (2005100800, "2005-10-08 00:00:00", 48),
        )
        self.conn.execute(
            "INSERT INTO County(county, ohLine) VALUES(?, ?)", ("Fairfield", 4887830)
        )
        self.conn.commit()

        # Try with negative wgt5
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """INSERT INTO WeatherData(
                    stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                    wgt5, wgt9, wgt13, wgt18, outage
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (2005100800, "Fairfield", 231.98, 5.79, -1, 0, 0, 0, 186),
            )
            self.conn.commit()

    def test_weatherdata_outage_check(self):
        """Test that outage must be >= 0."""
        # Setup
        self.conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(?, ?, ?)",
            (2005100800, "2005-10-08 00:00:00", 48),
        )
        self.conn.execute(
            "INSERT INTO County(county, ohLine) VALUES(?, ?)", ("Fairfield", 4887830)
        )
        self.conn.commit()

        # Try with negative outage
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """INSERT INTO WeatherData(
                    stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                    wgt5, wgt9, wgt13, wgt18, outage
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (2005100800, "Fairfield", 231.98, 5.79, 9, 0, 0, 0, -10),
            )
            self.conn.commit()

    def test_weatherdata_composite_primary_key(self):
        """Test that (stormCode, county) composite key is enforced."""
        # Setup
        self.conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(?, ?, ?)",
            (2005100800, "2005-10-08 00:00:00", 48),
        )
        self.conn.execute(
            "INSERT INTO County(county, ohLine) VALUES(?, ?)", ("Fairfield", 4887830)
        )
        self.conn.commit()

        # Insert first weather data record
        self.conn.execute(
            """INSERT INTO WeatherData(
                stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                wgt5, wgt9, wgt13, wgt18, outage
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (2005100800, "Fairfield", 231.98, 5.79, 9, 0, 0, 0, 186),
        )
        self.conn.commit()

        # Try to insert duplicate composite key
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """INSERT INTO WeatherData(
                    stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                    wgt5, wgt9, wgt13, wgt18, outage
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (2005100800, "Fairfield", 100.0, 6.0, 10, 1, 0, 0, 200),
            )
            self.conn.commit()


class TestDataIntegrity(unittest.TestCase):
    """Test data integrity scenarios."""

    def setUp(self):
        """Create fresh in-memory database for each test."""
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

        # Load schema from file
        schema_path = Path(__file__).parent.parent / "sql" / "schema.sql"
        with open(schema_path, "r") as f:
            schema_sql = f.read()

        self.conn.executescript(schema_sql)
        self.conn.commit()

    def tearDown(self):
        """Close database connection."""
        self.conn.close()

    def test_insert_realistic_data(self):
        """Test inserting realistic weather data from testdata.csv."""
        # Sample data from testdata.csv
        storm_data = [
            (2005100800, "2005-10-08 00:00:00", 48),
            (2005101512, "2005-10-15 12:00:00", 48),
        ]

        county_data = [
            ("Fairfield", 4887830),
            ("Hartford", 5869550),
            ("Litchfield", 3975201),
        ]

        weather_data = [
            (2005100800, "Fairfield", 231.9845528, 5.785229939, 9, 0, 0, 0, 186),
            (2005100800, "Hartford", 209.4889918, 6.364401081, 15, 1, 0, 0, 86),
            (2005101512, "Litchfield", 3.220320857, 9.944104234, 24, 6, 0, 0, 46),
        ]

        # Insert storms
        for storm in storm_data:
            self.conn.execute(
                "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(?, ?, ?)",
                storm,
            )

        # Insert counties
        for county in county_data:
            self.conn.execute("INSERT INTO County(county, ohLine) VALUES(?, ?)", county)

        # Insert weather data
        for wd in weather_data:
            self.conn.execute(
                """INSERT INTO WeatherData(
                    stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                    wgt5, wgt9, wgt13, wgt18, outage
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                wd,
            )

        self.conn.commit()

        # Verify data was inserted
        cursor = self.conn.execute("SELECT COUNT(*) FROM Storm")
        self.assertEqual(cursor.fetchone()[0], 2)

        cursor = self.conn.execute("SELECT COUNT(*) FROM County")
        self.assertEqual(cursor.fetchone()[0], 3)

        cursor = self.conn.execute("SELECT COUNT(*) FROM WeatherData")
        self.assertEqual(cursor.fetchone()[0], 3)

    def test_zero_outage_allowed(self):
        """Test that zero outage values are allowed (>= 0 constraint)."""
        # Setup
        self.conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(?, ?, ?)",
            (2005100800, "2005-10-08 00:00:00", 48),
        )
        self.conn.execute(
            "INSERT INTO County(county, ohLine) VALUES(?, ?)", ("Fairfield", 4887830)
        )
        self.conn.commit()

        # Insert with zero outage (should succeed)
        self.conn.execute(
            """INSERT INTO WeatherData(
                stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                wgt5, wgt9, wgt13, wgt18, outage
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (2005100800, "Fairfield", 231.98, 5.79, 9, 0, 0, 0, 0),
        )
        self.conn.commit()

        cursor = self.conn.execute(
            "SELECT outage FROM WeatherData WHERE stormCode = ? AND county = ?",
            (2005100800, "Fairfield"),
        )
        row = cursor.fetchone()
        self.assertEqual(row["outage"], 0)

    def test_zero_precipitation_allowed(self):
        """Test that zero precipitation values are allowed (>= 0 constraint)."""
        # Setup
        self.conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(?, ?, ?)",
            (2005100800, "2005-10-08 00:00:00", 48),
        )
        self.conn.execute(
            "INSERT INTO County(county, ohLine) VALUES(?, ?)", ("Fairfield", 4887830)
        )
        self.conn.commit()

        # Insert with zero precipitation (should succeed)
        self.conn.execute(
            """INSERT INTO WeatherData(
                stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                wgt5, wgt9, wgt13, wgt18, outage
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (2005100800, "Fairfield", 0.0, 5.79, 9, 0, 0, 0, 186),
        )
        self.conn.commit()

        cursor = self.conn.execute(
            "SELECT sumAFWA_TOTPRECIP FROM WeatherData WHERE stormCode = ? AND county = ?",
            (2005100800, "Fairfield"),
        )
        row = cursor.fetchone()
        self.assertEqual(row["sumAFWA_TOTPRECIP"], 0.0)


if __name__ == "__main__":
    unittest.main()
