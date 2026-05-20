"""
Test Suite for load_data.py

This test suite validates:
1. Data loads correctly into all three tables
2. Row counts match the original CSV
3. Foreign key constraints are respected
4. Data integrity (no duplicates, valid values)
5. SELECT queries work correctly on loaded data

=============================================================================
POSTGRESQL CONVERSION NOTES (for test_load_data.py)
=============================================================================

When converting this test suite to work with PostgreSQL:

1. IMPORT CHANGES:
   Replace:
       import sqlite3
   With:
       import psycopg2
       import psycopg2.extras

2. CONNECTION IN setUpClass():
   The setUpClass method calls load_data() which returns a connection.
   When load_data() is converted to PostgreSQL, no changes needed here.

3. TEST QUERIES:
   Most SQL queries are database-agnostic and will work without changes.
   The COUNT(DISTINCT ...) subquery is already PostgreSQL-compatible.

4. CURSOR USAGE:
   Currently uses sqlite3.Row for dict-like access.
   With PostgreSQL, use DictCursor for same behavior:
       cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

5. TEST DATABASE (RECOMMENDED):
   Create a separate test database:

   $ psql -U postgres
   postgres=# CREATE DATABASE wiser_test_db;
   postgres=# CREATE USER wiser_test_user WITH PASSWORD 'password';
   postgres=# GRANT ALL PRIVILEGES ON DATABASE wiser_test_db TO wiser_test_user;

6. EXCEPTION HANDLING:
   Change from sqlite3.IntegrityError to psycopg2.IntegrityError

   Most tests don't check for specific exceptions, so many need no changes.

7. FULL CONVERSION CHECKLIST:
   - [ ] Change import sqlite3 to import psycopg2, psycopg2.extras
   - [ ] Update setUpClass to use psycopg2 connection
   - [ ] Update exception handling from sqlite3 to psycopg2
   - [ ] Create test PostgreSQL database and credentials
   - [ ] Run test suite against PostgreSQL
   - [ ] Verify all 27 tests pass

=============================================================================
"""

import csv
import sqlite3  # TODO POSTGRES: Replace with: import psycopg2, psycopg2.extras
import sys
import unittest
from pathlib import Path

# Add scripts directory to path to import load_data
sys.path.insert(0, str(Path(__file__).parent))
from load_data import load_data, parse_storm_code


class TestParseStormCode(unittest.TestCase):
    """Test the storm code parsing function."""

    def test_parse_valid_event_code(self):
        """Test parsing a valid event code."""
        storm_code, event_dt = parse_storm_code("CT-2005100800")
        self.assertEqual(storm_code, 2005100800)
        self.assertEqual(event_dt.year, 2005)
        self.assertEqual(event_dt.month, 10)
        self.assertEqual(event_dt.day, 8)
        self.assertEqual(event_dt.hour, 0)

    def test_parse_another_event_code(self):
        """Test parsing another valid event code."""
        storm_code, event_dt = parse_storm_code("CT-2005101512")
        self.assertEqual(storm_code, 2005101512)
        self.assertEqual(event_dt.year, 2005)
        self.assertEqual(event_dt.month, 10)
        self.assertEqual(event_dt.day, 15)
        self.assertEqual(event_dt.hour, 12)

    def test_invalid_event_code_format(self):
        """Test that invalid event codes raise ValueError."""
        with self.assertRaises(ValueError):
            parse_storm_code("INVALID")

    def test_invalid_month(self):
        """Test that invalid month raises ValueError."""
        with self.assertRaises(ValueError):
            parse_storm_code("CT-2005130800")  # Month 13 doesn't exist


class TestDataLoading(unittest.TestCase):
    """Test the data loading functionality."""

    @classmethod
    def setUpClass(cls):
        """Set up test fixtures once for the entire test class."""
        csv_path = Path(__file__).parent.parent / "csvs" / "testdata.csv"
        if not csv_path.exists():
            raise FileNotFoundError(f"Test data CSV not found: {csv_path}")

        # Load test data
        cls.load_result = load_data(str(csv_path))
        cls.conn = cls.load_result["connection"]
        cls.cursor = cls.conn.cursor()

    @classmethod
    def tearDownClass(cls):
        """Close database connection after all tests."""
        cls.conn.close()

    def test_csv_row_count(self):
        """Verify CSV has the expected number of rows."""
        csv_path = Path(__file__).parent.parent / "csvs" / "testdata.csv"
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            csv_rows = sum(1 for _ in reader)

        self.assertEqual(
            self.load_result["total_csv_rows"],
            csv_rows,
            f"CSV row count mismatch: expected {csv_rows}, "
            f"got {self.load_result['total_csv_rows']}",
        )

    def test_county_count(self):
        """Test that county count is reasonable and matches expected."""
        county_count = self.load_result["County"]

        # Counties should be > 0
        self.assertGreater(county_count, 0, "No counties were loaded")

        # For Connecticut data, expect 8 counties
        # (Fairfield, Hartford, Litchfield, Middlesex, New Haven, New London, Tolland, Windham)
        self.assertEqual(
            county_count, 8, f"Expected 8 Connecticut counties, got {county_count}"
        )

    def test_storm_count(self):
        """Test that storm count is reasonable."""
        storm_count = self.load_result["Storm"]
        self.assertGreater(storm_count, 0, "No storms were loaded")

        # There should be at least 1 unique storm
        self.assertGreaterEqual(storm_count, 1)

    def test_weather_data_count(self):
        """Test that weather data count matches CSV."""
        weather_count = self.load_result["WeatherData"]
        csv_count = self.load_result["total_csv_rows"]

        # Weather data count should equal CSV rows (each row becomes a weather record)
        self.assertEqual(
            weather_count,
            csv_count,
            f"Weather data count ({weather_count}) should match CSV rows ({csv_count})",
        )

    # ===== TABLE STRUCTURE TESTS =====

    def test_county_table_exists(self):
        """Test that County table was created."""
        self.cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='County'"
        )
        self.assertIsNotNone(self.cursor.fetchone(), "County table does not exist")

    def test_storm_table_exists(self):
        """Test that Storm table was created."""
        self.cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='Storm'"
        )
        self.assertIsNotNone(self.cursor.fetchone(), "Storm table does not exist")

    def test_weather_data_table_exists(self):
        """Test that WeatherData table was created."""
        self.cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='WeatherData'"
        )
        self.assertIsNotNone(self.cursor.fetchone(), "WeatherData table does not exist")

    # ===== SELECT QUERY TESTS =====

    def test_select_all_counties(self):
        """Test SELECT * FROM County."""
        self.cursor.execute("SELECT county, ohLine FROM County")
        counties = self.cursor.fetchall()

        self.assertEqual(
            len(counties), self.load_result["County"], "SELECT County count mismatch"
        )

        # Verify each county has valid ohLine
        for county in counties:
            self.assertIsNotNone(county[0], "County name is NULL")
            self.assertGreater(county[1], 0, "ohLine should be > 0")

    def test_select_all_storms(self):
        """Test SELECT * FROM Storm."""
        self.cursor.execute("SELECT stormCode, eventDateTime, eventLength FROM Storm")
        storms = self.cursor.fetchall()

        self.assertEqual(
            len(storms), self.load_result["Storm"], "SELECT Storm count mismatch"
        )

        # Verify each storm has valid data
        for storm in storms:
            self.assertIsNotNone(storm[0], "stormCode is NULL")
            self.assertIsNotNone(storm[1], "eventDateTime is NULL")
            self.assertGreater(storm[2], 0, "eventLength should be > 0")

    def test_select_all_weather_data(self):
        """Test SELECT * FROM WeatherData."""
        self.cursor.execute(
            """SELECT stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                      wgt5, wgt9, wgt13, wgt18, outage
               FROM WeatherData"""
        )
        weather_data = self.cursor.fetchall()

        self.assertEqual(
            len(weather_data),
            self.load_result["WeatherData"],
            "SELECT WeatherData count mismatch",
        )

        # Verify each weather record has valid data
        for record in weather_data:
            self.assertIsNotNone(record[0], "stormCode is NULL")
            self.assertIsNotNone(record[1], "county is NULL")
            self.assertGreaterEqual(record[2], 0, "sumAFWA_TOTPRECIP should be >= 0")
            self.assertGreater(record[3], 0, "peakWSPD10 should be > 0")

    def test_select_with_filter(self):
        """Test SELECT with WHERE clause."""
        self.cursor.execute("SELECT COUNT(*) FROM WeatherData WHERE outage > 0")
        count = self.cursor.fetchone()[0]
        self.assertGreater(count, 0, "No weather data with outage > 0")

    def test_select_with_join(self):
        """Test SELECT with JOIN across tables."""
        self.cursor.execute(
            """SELECT w.stormCode, w.county, s.eventDateTime, c.ohLine
               FROM WeatherData w
               JOIN Storm s ON w.stormCode = s.stormCode
               JOIN County c ON w.county = c.county
               LIMIT 10"""
        )
        results = self.cursor.fetchall()
        self.assertGreater(len(results), 0, "JOIN query returned no results")

    def test_select_count_by_county(self):
        """Test GROUP BY query by county."""
        self.cursor.execute(
            """SELECT county, COUNT(*) as record_count
               FROM WeatherData
               GROUP BY county
               ORDER BY county"""
        )
        results = self.cursor.fetchall()

        # Should have results for each county
        self.assertEqual(
            len(results), self.load_result["County"], "GROUP BY county count mismatch"
        )

    def test_select_count_by_storm(self):
        """Test GROUP BY query by storm."""
        self.cursor.execute(
            """SELECT stormCode, COUNT(*) as record_count
               FROM WeatherData
               GROUP BY stormCode
               ORDER BY stormCode"""
        )
        results = self.cursor.fetchall()

        # Should have results for each storm
        self.assertEqual(
            len(results), self.load_result["Storm"], "GROUP BY storm count mismatch"
        )

    # ===== DATA INTEGRITY TESTS =====

    def test_no_duplicate_counties(self):
        """Test that there are no duplicate counties."""
        self.cursor.execute("SELECT COUNT(DISTINCT county) FROM County")
        distinct_count = self.cursor.fetchone()[0]

        self.cursor.execute("SELECT COUNT(*) FROM County")
        total_count = self.cursor.fetchone()[0]

        self.assertEqual(
            distinct_count, total_count, "Duplicate counties found in table"
        )

    def test_no_duplicate_storms(self):
        """Test that there are no duplicate storms."""
        self.cursor.execute("SELECT COUNT(DISTINCT stormCode) FROM Storm")
        distinct_count = self.cursor.fetchone()[0]

        self.cursor.execute("SELECT COUNT(*) FROM Storm")
        total_count = self.cursor.fetchone()[0]

        self.assertEqual(distinct_count, total_count, "Duplicate storms found in table")

    def test_no_duplicate_weather_records(self):
        """Test that there are no duplicate weather records."""
        # In SQLite, use subquery to count distinct combinations
        self.cursor.execute(
            """SELECT COUNT(*) FROM
               (SELECT DISTINCT stormCode, county FROM WeatherData)"""
        )
        distinct_count = self.cursor.fetchone()[0]

        self.cursor.execute("SELECT COUNT(*) FROM WeatherData")
        total_count = self.cursor.fetchone()[0]

        self.assertEqual(
            distinct_count, total_count, "Duplicate weather records found in table"
        )

    # ===== FOREIGN KEY CONSTRAINT TESTS =====

    def test_all_weather_storms_exist(self):
        """Test that all stormCodes in WeatherData exist in Storm."""
        self.cursor.execute(
            """SELECT COUNT(*) FROM WeatherData w
               WHERE w.stormCode NOT IN (SELECT stormCode FROM Storm)"""
        )
        orphaned_count = self.cursor.fetchone()[0]
        self.assertEqual(
            orphaned_count, 0, "Found weather records with non-existent storms"
        )

    def test_all_weather_counties_exist(self):
        """Test that all counties in WeatherData exist in County."""
        self.cursor.execute(
            """SELECT COUNT(*) FROM WeatherData w
               WHERE w.county NOT IN (SELECT county FROM County)"""
        )
        orphaned_count = self.cursor.fetchone()[0]
        self.assertEqual(
            orphaned_count, 0, "Found weather records with non-existent counties"
        )

    # ===== SPECIFIC DATA VALUE TESTS =====

    def test_county_names_expected_values(self):
        """Test that expected Connecticut counties are in the database."""
        expected_counties = {
            "Fairfield",
            "Hartford",
            "Litchfield",
            "Middlesex",
            "New Haven",
            "New London",
            "Tolland",
            "Windham",
        }

        self.cursor.execute("SELECT county FROM County ORDER BY county")
        actual_counties = {row[0] for row in self.cursor.fetchall()}

        self.assertEqual(
            actual_counties,
            expected_counties,
            f"County mismatch. Expected {expected_counties}, got {actual_counties}",
        )

    def test_weather_data_value_ranges(self):
        """Test that weather data values are within expected ranges."""
        self.cursor.execute(
            """SELECT MIN(sumAFWA_TOTPRECIP), MAX(sumAFWA_TOTPRECIP),
                      MIN(peakWSPD10), MAX(peakWSPD10),
                      MIN(outage), MAX(outage)
               FROM WeatherData"""
        )
        result = self.cursor.fetchone()

        # Check that we have reasonable weather data
        self.assertIsNotNone(result[0], "No precipitation data")
        self.assertGreaterEqual(result[0], 0, "Negative precipitation")
        self.assertGreater(result[2], 0, "Invalid wind speed minimum")
        self.assertGreaterEqual(result[4], 0, "Negative outage count")

    # ===== AGGREGATE TESTS =====

    def test_total_records_sum(self):
        """Test that sum of weather records by storm equals total."""
        self.cursor.execute(
            "SELECT SUM(cnt) FROM (SELECT COUNT(*) as cnt FROM WeatherData GROUP BY stormCode)"
        )
        sum_by_storm = self.cursor.fetchone()[0]

        self.cursor.execute("SELECT COUNT(*) FROM WeatherData")
        total = self.cursor.fetchone()[0]

        self.assertEqual(
            sum_by_storm, total, "Sum of records by storm doesn't match total"
        )


class TestErrorHandling(unittest.TestCase):
    """Test error handling in data loading."""

    def test_missing_csv_file(self):
        """Test that FileNotFoundError is raised for missing CSV."""
        with self.assertRaises(FileNotFoundError):
            load_data("nonexistent_file.csv")


def run_tests():
    """Run all tests and generate a report."""
    # Create test suite
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # Add all test classes
    suite.addTests(loader.loadTestsFromTestCase(TestParseStormCode))
    suite.addTests(loader.loadTestsFromTestCase(TestDataLoading))
    suite.addTests(loader.loadTestsFromTestCase(TestErrorHandling))

    # Run tests with verbose output
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    # Print summary
    print("\n" + "=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)
    print(f"Tests run:    {result.testsRun}")
    print(
        f"Successes:    {result.testsRun - len(result.failures) - len(result.errors)}"
    )
    print(f"Failures:     {len(result.failures)}")
    print(f"Errors:       {len(result.errors)}")

    if result.wasSuccessful():
        print("\n[PASS] All tests passed!")
        return 0
    else:
        print("\n[FAIL] Some tests failed!")
        return 1


if __name__ == "__main__":
    sys.exit(run_tests())
