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

import sys
import unittest
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

TESTS_DIR = Path(__file__).parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from pg_harness import make_conn  # noqa: E402


# ---------------------------------------------------------------------------
# Introspection helpers
#
# SQLite exposed its catalogue through PRAGMA table_info and sqlite_master.
# PostgreSQL uses the SQL-standard information_schema plus pg_catalog. Two
# consequences run through every assertion below: identifiers come back
# folded to lowercase, and the type names are PostgreSQL's own -- REAL is
# reported as 'double precision', TIMESTAMP as 'timestamp without time zone'.
# ---------------------------------------------------------------------------


def _table_exists(conn, table: str) -> bool:
    return conn.execute(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = current_schema()
          AND table_type = 'BASE TABLE'
          AND table_name = %s
        """,
        (table.lower(),),
    ).fetchone() is not None


def _columns(conn, table: str) -> dict[str, str]:
    """Replacement for PRAGMA table_info: {folded column name: type}."""
    rows = conn.execute(
        """
        SELECT column_name,
               CASE WHEN character_maximum_length IS NOT NULL
                    THEN data_type || '(' || character_maximum_length || ')'
                    ELSE data_type END
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = %s
        """,
        (table.lower(),),
    ).fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


def _primary_key(conn, table: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a
          ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = (current_schema() || '.' || %s)::regclass
          AND i.indisprimary
        """,
        (table.lower(),),
    ).fetchall()
    return [str(row[0]) for row in rows]


def _indexes(conn) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
        ).fetchall()
    }


class TestSchemaStructure(unittest.TestCase):
    """Test database schema table and column definitions."""

    @classmethod
    def setUpClass(cls):
        """Rebuild the test schema and load sql/schema.sql."""
        cls.conn = make_conn()

    @classmethod
    def tearDownClass(cls):
        """The harness owns the shared connection; just clear any state."""
        cls.conn.rollback()

    def test_storm_table_exists(self):
        """Test that Storm table exists."""
        self.assertTrue(_table_exists(self.conn, "Storm"))

    def test_county_table_exists(self):
        """Test that County table exists."""
        self.assertTrue(_table_exists(self.conn, "County"))

    def test_weatherdata_table_exists(self):
        """Test that WeatherData table exists."""
        self.assertTrue(_table_exists(self.conn, "WeatherData"))

    def test_external_outage_table_exists(self):
        """Test that ExternalOutageObservation table exists."""
        self.assertTrue(_table_exists(self.conn, "ExternalOutageObservation"))

    def test_sponsor_support_tables_exist(self):
        """Test that region, metric, and hourly support tables exist."""
        for table_name in (
            "RegionCountyMapping",
            "StormMetricDefinition",
            "HourlyWeatherObservation",
        ):
            with self.subTest(table=table_name):
                self.assertTrue(_table_exists(self.conn, table_name))

    def test_storm_columns(self):
        """Test Storm table has correct columns."""
        columns = _columns(self.conn, "Storm")

        self.assertIn("stormcode", columns)
        self.assertIn("eventdatetime", columns)
        self.assertIn("eventlength", columns)

        # Verify data types
        self.assertEqual(columns["stormcode"], "bigint")
        self.assertEqual(columns["eventdatetime"], "timestamp without time zone")
        self.assertEqual(columns["eventlength"], "integer")

    def test_county_columns(self):
        """Test County table has correct columns."""
        columns = _columns(self.conn, "County")

        self.assertIn("county", columns)
        self.assertIn("state", columns)
        self.assertIn("ohline", columns)

        # Verify data types
        self.assertEqual(columns["county"], "character varying(64)")
        self.assertEqual(columns["state"], "character varying(2)")
        self.assertEqual(columns["ohline"], "bigint")

    def test_weatherdata_columns(self):
        """Test WeatherData table has correct columns."""
        columns = _columns(self.conn, "WeatherData")

        expected_columns = {
            "stormcode": "bigint",
            "county": "character varying(64)",
            "sumafwa_totprecip": "double precision",
            "peakwspd10": "double precision",
            "wgt5": "integer",
            "wgt9": "integer",
            "wgt13": "integer",
            "wgt18": "integer",
            "outage": "integer",
        }

        for col_name, col_type in expected_columns.items():
            self.assertIn(col_name, columns)
            self.assertEqual(columns[col_name], col_type)

    def test_external_outage_columns(self):
        """Test ExternalOutageObservation table has correct columns."""
        columns = _columns(self.conn, "ExternalOutageObservation")

        expected_columns = {
            "id": "integer",
            "sourcesystem": "text",
            "sourcerecordid": "text",
            "observedat": "timestamp without time zone",
            "county": "character varying(64)",
            "serviceterritory": "text",
            "outagecount": "integer",
            "customercount": "integer",
            "restorationstatus": "text",
            "rawpayload": "text",
        }

        for col_name, col_type in expected_columns.items():
            self.assertIn(col_name, columns)
            self.assertEqual(columns[col_name], col_type)

    def test_hourly_weather_columns(self):
        """Test HourlyWeatherObservation supports long-form hourly metrics."""
        columns = _columns(self.conn, "HourlyWeatherObservation")

        expected_columns = {
            "stormcode": "bigint",
            "county": "character varying(64)",
            "houroffset": "integer",
            "metrickey": "text",
            "metricvalue": "double precision",
            "observedat": "timestamp without time zone",
            "sourcecolumn": "text",
        }

        for col_name, col_type in expected_columns.items():
            self.assertIn(col_name, columns)
            self.assertEqual(columns[col_name], col_type)

    def test_region_and_metric_indexes_exist(self):
        index_names = _indexes(self.conn)

        self.assertIn("idx_region_county_state", index_names)
        self.assertIn("idx_hourly_weather_storm_county_hour", index_names)
        self.assertIn("idx_hourly_weather_metric", index_names)

    def test_storm_primary_key(self):
        """Test Storm has correct primary key."""
        pk_columns = _primary_key(self.conn, "Storm")
        self.assertEqual(pk_columns, ["stormcode"])

    def test_county_primary_key(self):
        """Test County has correct primary key."""
        pk_columns = _primary_key(self.conn, "County")
        self.assertEqual(pk_columns, ["county"])

    def test_weatherdata_primary_key(self):
        """Test WeatherData has composite primary key."""
        pk_columns = sorted(_primary_key(self.conn, "WeatherData"))
        self.assertEqual(pk_columns, ["county", "stormcode"])

    def test_indexes_exist(self):
        """Test that all required indexes are created."""
        index_names = _indexes(self.conn)

        self.assertIn("idx_weatherdata_outage", index_names)
        self.assertIn("idx_weatherdata_county", index_names)
        self.assertIn("idx_storm_datetime", index_names)
        self.assertIn("idx_external_outage_source_time", index_names)
        self.assertIn("idx_external_outage_county", index_names)


class TestConstraints(unittest.TestCase):
    """Test constraint enforcement."""

    def setUp(self):
        """Rebuild the test schema before each test."""
        self.conn = make_conn()

    def tearDown(self):
        """A failed statement aborts the transaction, so always roll back."""
        self.conn.rollback()

    def test_insert_valid_storm(self):
        """Test inserting valid storm record."""
        self.conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(%s, %s, %s)",
            (2005100800, "2005-10-08 00:00:00", 48),
        )
        self.conn.commit()

        cursor = self.conn.execute(
            "SELECT stormCode FROM Storm WHERE stormCode = %s", (2005100800,)
        )
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 2005100800)

    def test_storm_eventlength_check_constraint(self):
        """Test that eventLength must be > 0."""
        with self.assertRaises(psycopg.IntegrityError):
            self.conn.execute(
                "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(%s, %s, %s)",
                (2005100800, "2005-10-08 00:00:00", 0),
            )
            self.conn.commit()

    def test_storm_unique_datetime_constraint(self):
        """Test that eventDateTime must be unique."""
        # Insert first record
        self.conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(%s, %s, %s)",
            (2005100800, "2005-10-08 00:00:00", 48),
        )
        self.conn.commit()

        # Try to insert duplicate datetime
        with self.assertRaises(psycopg.IntegrityError):
            self.conn.execute(
                "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(%s, %s, %s)",
                (2005100801, "2005-10-08 00:00:00", 48),
            )
            self.conn.commit()

    def test_county_primary_key_constraint(self):
        """Test that county name is unique primary key."""
        # Insert first record
        self.conn.execute(
            "INSERT INTO County(county, ohLine) VALUES(%s, %s)", ("Fairfield", 4887830)
        )
        self.conn.commit()

        # Try to insert duplicate county
        with self.assertRaises(psycopg.IntegrityError):
            self.conn.execute(
                "INSERT INTO County(county, ohLine) VALUES(%s, %s)",
                ("Fairfield", 5000000),
            )
            self.conn.commit()

    def test_county_ohline_check_constraint(self):
        """Test that ohLine must be > 0."""
        with self.assertRaises(psycopg.IntegrityError):
            self.conn.execute(
                "INSERT INTO County(county, ohLine) VALUES(%s, %s)", ("Fairfield", 0)
            )
            self.conn.commit()

    def test_county_ohline_negative_check_constraint(self):
        """Test that ohLine must not be negative."""
        with self.assertRaises(psycopg.IntegrityError):
            self.conn.execute(
                "INSERT INTO County(county, ohLine) VALUES(%s, %s)", ("Fairfield", -1000)
            )
            self.conn.commit()

    def test_insert_valid_weatherdata(self):
        """Test inserting valid weather data record."""
        # Insert prerequisite storm and county
        self.conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(%s, %s, %s)",
            (2005100800, "2005-10-08 00:00:00", 48),
        )
        self.conn.execute(
            "INSERT INTO County(county, ohLine) VALUES(%s, %s)", ("Fairfield", 4887830)
        )
        self.conn.commit()

        # Insert weather data
        self.conn.execute(
            """INSERT INTO WeatherData(
                stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                wgt5, wgt9, wgt13, wgt18, outage
            ) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (2005100800, "Fairfield", 231.98, 5.79, 9, 0, 0, 0, 186),
        )
        self.conn.commit()

        cursor = self.conn.execute(
            "SELECT * FROM WeatherData WHERE stormCode = %s AND county = %s",
            (2005100800, "Fairfield"),
        )
        row = cursor.fetchone()
        self.assertIsNotNone(row)

    def test_weatherdata_foreign_key_storm(self):
        """Test that stormCode must exist in Storm table."""
        # Insert county but not storm
        self.conn.execute(
            "INSERT INTO County(county, ohLine) VALUES(%s, %s)", ("Fairfield", 4887830)
        )
        self.conn.commit()

        # Try to insert weather data with non-existent storm
        with self.assertRaises(psycopg.IntegrityError):
            self.conn.execute(
                """INSERT INTO WeatherData(
                    stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                    wgt5, wgt9, wgt13, wgt18, outage
                ) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (2005100800, "Fairfield", 231.98, 5.79, 9, 0, 0, 0, 186),
            )
            self.conn.commit()

    def test_weatherdata_foreign_key_county(self):
        """Test that county must exist in County table."""
        # Insert storm but not county
        self.conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(%s, %s, %s)",
            (2005100800, "2005-10-08 00:00:00", 48),
        )
        self.conn.commit()

        # Try to insert weather data with non-existent county
        with self.assertRaises(psycopg.IntegrityError):
            self.conn.execute(
                """INSERT INTO WeatherData(
                    stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                    wgt5, wgt9, wgt13, wgt18, outage
                ) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (2005100800, "Fairfield", 231.98, 5.79, 9, 0, 0, 0, 186),
            )
            self.conn.commit()

    def test_weatherdata_sumafwa_totprecip_check(self):
        """Test that sumAFWA_TOTPRECIP must be >= 0."""
        # Setup
        self.conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(%s, %s, %s)",
            (2005100800, "2005-10-08 00:00:00", 48),
        )
        self.conn.execute(
            "INSERT INTO County(county, ohLine) VALUES(%s, %s)", ("Fairfield", 4887830)
        )
        self.conn.commit()

        # Try to insert with negative precipitation
        with self.assertRaises(psycopg.IntegrityError):
            self.conn.execute(
                """INSERT INTO WeatherData(
                    stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                    wgt5, wgt9, wgt13, wgt18, outage
                ) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (2005100800, "Fairfield", -10.0, 5.79, 9, 0, 0, 0, 186),
            )
            self.conn.commit()

    def test_weatherdata_peakwspd10_check(self):
        """Test that peakWSPD10 must be > 0."""
        # Setup
        self.conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(%s, %s, %s)",
            (2005100800, "2005-10-08 00:00:00", 48),
        )
        self.conn.execute(
            "INSERT INTO County(county, ohLine) VALUES(%s, %s)", ("Fairfield", 4887830)
        )
        self.conn.commit()

        # Try to insert with zero wind speed
        with self.assertRaises(psycopg.IntegrityError):
            self.conn.execute(
                """INSERT INTO WeatherData(
                    stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                    wgt5, wgt9, wgt13, wgt18, outage
                ) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (2005100800, "Fairfield", 231.98, 0.0, 9, 0, 0, 0, 186),
            )
            self.conn.commit()

    def test_weatherdata_wgt_values_check(self):
        """Test that wgt5-18 values must be >= 0."""
        # Setup
        self.conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(%s, %s, %s)",
            (2005100800, "2005-10-08 00:00:00", 48),
        )
        self.conn.execute(
            "INSERT INTO County(county, ohLine) VALUES(%s, %s)", ("Fairfield", 4887830)
        )
        self.conn.commit()

        # Try with negative wgt5
        with self.assertRaises(psycopg.IntegrityError):
            self.conn.execute(
                """INSERT INTO WeatherData(
                    stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                    wgt5, wgt9, wgt13, wgt18, outage
                ) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (2005100800, "Fairfield", 231.98, 5.79, -1, 0, 0, 0, 186),
            )
            self.conn.commit()

    def test_weatherdata_outage_check(self):
        """Test that outage must be >= 0."""
        # Setup
        self.conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(%s, %s, %s)",
            (2005100800, "2005-10-08 00:00:00", 48),
        )
        self.conn.execute(
            "INSERT INTO County(county, ohLine) VALUES(%s, %s)", ("Fairfield", 4887830)
        )
        self.conn.commit()

        # Try with negative outage
        with self.assertRaises(psycopg.IntegrityError):
            self.conn.execute(
                """INSERT INTO WeatherData(
                    stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                    wgt5, wgt9, wgt13, wgt18, outage
                ) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (2005100800, "Fairfield", 231.98, 5.79, 9, 0, 0, 0, -10),
            )
            self.conn.commit()

    def test_weatherdata_composite_primary_key(self):
        """Test that (stormCode, county) composite key is enforced."""
        # Setup
        self.conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(%s, %s, %s)",
            (2005100800, "2005-10-08 00:00:00", 48),
        )
        self.conn.execute(
            "INSERT INTO County(county, ohLine) VALUES(%s, %s)", ("Fairfield", 4887830)
        )
        self.conn.commit()

        # Insert first weather data record
        self.conn.execute(
            """INSERT INTO WeatherData(
                stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                wgt5, wgt9, wgt13, wgt18, outage
            ) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (2005100800, "Fairfield", 231.98, 5.79, 9, 0, 0, 0, 186),
        )
        self.conn.commit()

        # Try to insert duplicate composite key
        with self.assertRaises(psycopg.IntegrityError):
            self.conn.execute(
                """INSERT INTO WeatherData(
                    stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                    wgt5, wgt9, wgt13, wgt18, outage
                ) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (2005100800, "Fairfield", 100.0, 6.0, 10, 1, 0, 0, 200),
            )
            self.conn.commit()


class TestDataIntegrity(unittest.TestCase):
    """Test data integrity scenarios."""

    def setUp(self):
        """Rebuild the test schema before each test."""
        self.conn = make_conn()

    def tearDown(self):
        """A failed statement aborts the transaction, so always roll back."""
        self.conn.rollback()

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
                "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(%s, %s, %s)",
                storm,
            )

        # Insert counties
        for county in county_data:
            self.conn.execute("INSERT INTO County(county, ohLine) VALUES(%s, %s)", county)

        # Insert weather data
        for wd in weather_data:
            self.conn.execute(
                """INSERT INTO WeatherData(
                    stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                    wgt5, wgt9, wgt13, wgt18, outage
                ) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
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
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(%s, %s, %s)",
            (2005100800, "2005-10-08 00:00:00", 48),
        )
        self.conn.execute(
            "INSERT INTO County(county, ohLine) VALUES(%s, %s)", ("Fairfield", 4887830)
        )
        self.conn.commit()

        # Insert with zero outage (should succeed)
        self.conn.execute(
            """INSERT INTO WeatherData(
                stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                wgt5, wgt9, wgt13, wgt18, outage
            ) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (2005100800, "Fairfield", 231.98, 5.79, 9, 0, 0, 0, 0),
        )
        self.conn.commit()

        cursor = self.conn.execute(
            "SELECT outage FROM WeatherData WHERE stormCode = %s AND county = %s",
            (2005100800, "Fairfield"),
        )
        row = cursor.fetchone()
        self.assertEqual(row[0], 0)

    def test_zero_precipitation_allowed(self):
        """Test that zero precipitation values are allowed (>= 0 constraint)."""
        # Setup
        self.conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(%s, %s, %s)",
            (2005100800, "2005-10-08 00:00:00", 48),
        )
        self.conn.execute(
            "INSERT INTO County(county, ohLine) VALUES(%s, %s)", ("Fairfield", 4887830)
        )
        self.conn.commit()

        # Insert with zero precipitation (should succeed)
        self.conn.execute(
            """INSERT INTO WeatherData(
                stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                wgt5, wgt9, wgt13, wgt18, outage
            ) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (2005100800, "Fairfield", 0.0, 5.79, 9, 0, 0, 0, 186),
        )
        self.conn.commit()

        cursor = self.conn.execute(
            "SELECT sumAFWA_TOTPRECIP FROM WeatherData WHERE stormCode = %s AND county = %s",
            (2005100800, "Fairfield"),
        )
        row = cursor.fetchone()
        self.assertEqual(row[0], 0.0)


if __name__ == "__main__":
    unittest.main()
