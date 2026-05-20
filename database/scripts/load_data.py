"""
Load Weather Data from CSV into Database

This script reads data from testdata.csv and inserts it into three tables
in the correct order: County -> Storm -> WeatherData.

The script ensures foreign key constraints are satisfied by respecting
the dependency order.

=============================================================================
POSTGRESQL CONVERSION NOTES (for production deployment)
=============================================================================

1. DEPENDENCIES:
   Install PostgreSQL adapter:
   $ pip install psycopg2-binary

2. IMPORT CHANGES:
   Replace:
       import sqlite3
   With:
       import psycopg2
       from psycopg2 import sql

3. CONNECTION SETUP:
   Replace the sqlite3.connect() calls with:

   conn = psycopg2.connect(
       host="localhost",          # or your server IP
       port=5432,
       database="wiser_db",
       user="wiser_user",
       password="your_password"
   )

   Best practice: Use environment variables or config file:

   import os
   conn = psycopg2.connect(
       host=os.getenv('DB_HOST', 'localhost'),
       port=os.getenv('DB_PORT', 5432),
       database=os.getenv('DB_NAME', 'wiser_db'),
       user=os.getenv('DB_USER', 'wiser_user'),
       password=os.getenv('DB_PASSWORD')
   )

4. SQL SYNTAX CHANGES:
   SQLite to PostgreSQL differences:

   a) Remove "IF NOT EXISTS" if tables already exist

   b) Change BIGINT to BIGINT (same, but be explicit)

   c) Change REAL to DOUBLE PRECISION or NUMERIC(10,2) for better precision

   d) Keep VARCHAR(255), TIMESTAMP, INT as-is (compatible)

   e) PostgreSQL is stricter about CHECK constraints - current ones are fine

5. CURSOR CHANGES:
   Replace:
       conn.row_factory = sqlite3.Row
   With:
       # PostgreSQL returns tuples by default, for dict-like access:
       import psycopg2.extras
       cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

6. PARAMETER PLACEHOLDERS:
   Replace ? with %s in all SQL queries:

   SQLite:  cursor.execute("INSERT INTO County VALUES (?, ?)", (val1, val2))
   PostgreSQL: cursor.execute("INSERT INTO County VALUES (%s, %s)", (val1, val2))

7. AUTOCOMMIT:
   PostgreSQL requires explicit commit. Current conn.commit() calls are fine.
   For better error handling:

   try:
       # your operations
       conn.commit()
   except Exception as e:
       conn.rollback()
       raise

8. CONNECTION POOLING (recommended for production):
   from psycopg2 import pool

   connection_pool = psycopg2.pool.SimpleConnectionPool(
       1, 20,  # min and max connections
       host=..., database=..., user=..., password=...
   )
   conn = connection_pool.getconn()

9. TESTING:
   After conversion, update test_load_data.py with same PostgreSQL connection.
   Consider using a separate test database:

   test_db = "wiser_test_db"

10. PERFORMANCE OPTIMIZATION (for large datasets):
    Use COPY command instead of INSERT for bulk loading:

    from io import StringIO

    buffer = StringIO()
    for record in weather_data:
        buffer.write(f"{record['stormCode']}\t{record['county']}\t...\n")
    buffer.seek(0)

    cursor.copy_from(buffer, 'WeatherData', sep='\t',
                     columns=['stormCode', 'county', ...])

11. CONFIGURATION FILE EXAMPLE:
    Create config/database.ini:

    [postgresql]
    host=localhost
    port=5432
    database=wiser_db
    user=wiser_user
    password=your_password

    Then read with configparser module.

=============================================================================
"""

import csv
import sqlite3  # TODO POSTGRES: Replace with: import psycopg2
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Tuple


def parse_storm_code(event_code: str) -> Tuple[int, datetime]:
    """
    Parse eventCode to extract stormCode and derive eventDateTime.

    eventCode format: CT-YYYYMMDDHH (e.g., CT-2005100800)
    Returns: (stormCode as int, eventDateTime as TIMESTAMP)

    NOTE: This function is database-agnostic and requires no changes for PostgreSQL
    """
    try:
        # Extract the timestamp part (last 10 digits)
        timestamp_str = event_code.split("-")[1]  # e.g., '2005100800'

        # Parse: YYYY=2005, MM=10, DD=08, HH=00
        year = int(timestamp_str[0:4])
        month = int(timestamp_str[4:6])
        day = int(timestamp_str[6:8])
        hour = int(timestamp_str[8:10])

        event_date_time = datetime(year, month, day, hour, 0, 0)
        return int(timestamp_str), event_date_time
    except Exception as e:
        raise ValueError(f"Failed to parse event code '{event_code}': {str(e)}")


def load_data(csv_path: str, db_path: str = None) -> Dict:
    """
    Load data from CSV file into database tables.

    Args:
        csv_path: Path to the testdata.csv file
        db_path: Path to SQLite database (uses in-memory if not specified)
                 TODO POSTGRES: Change to db_config dict with host, port, user, password

    Returns:
        Dictionary with row counts for each table:
        {'County': count, 'Storm': count, 'WeatherData': count, 'total_csv_rows': count}

    Raises:
        FileNotFoundError: If CSV file doesn't exist
        sqlite3.Error: If database operation fails

    TODO POSTGRES: Update docstring to reflect psycopg2.Error instead of sqlite3.Error
    """

    # Validate CSV file exists
    csv_file = Path(csv_path)
    if not csv_file.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    # =========================================================================
    # DATABASE CONNECTION
    # TODO POSTGRES: Replace this entire section
    # =========================================================================
    # Current SQLite code:
    if db_path:
        conn = sqlite3.connect(db_path)
    else:
        conn = sqlite3.connect(":memory:")

    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # POSTGRES replacement:
    # import psycopg2
    # import psycopg2.extras
    #
    # conn = psycopg2.connect(
    #     host=db_config.get('host', 'localhost'),
    #     port=db_config.get('port', 5432),
    #     database=db_config.get('database', 'wiser_db'),
    #     user=db_config.get('user', 'wiser_user'),
    #     password=db_config.get('password')
    # )
    # cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    # =========================================================================

    try:
        # =====================================================================
        # CREATE TABLES
        # TODO POSTGRES: Minor SQL syntax adjustments needed
        # =====================================================================
        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS County (
                county VARCHAR(255) NOT NULL PRIMARY KEY,
                ohLine INT NOT NULL,
                CHECK (ohLine > 0)
            );

            CREATE TABLE IF NOT EXISTS Storm (
                stormCode BIGINT NOT NULL PRIMARY KEY,
                eventDateTime TIMESTAMP NOT NULL,
                eventLength INT NOT NULL,
                UNIQUE (eventDateTime),
                CHECK (eventLength > 0)
            );

            CREATE TABLE IF NOT EXISTS WeatherData (
                stormCode BIGINT NOT NULL,
                county VARCHAR(255) NOT NULL,
                sumAFWA_TOTPRECIP REAL NOT NULL,
                peakWSPD10 REAL NOT NULL,
                wgt5 INT NOT NULL,
                wgt9 INT NOT NULL,
                wgt13 INT NOT NULL,
                wgt18 INT NOT NULL,
                outage INT NOT NULL,
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
        """)

        # POSTGRES replacement:
        # Note: executescript() doesn't exist in psycopg2, execute each separately:
        #
        # cursor.execute("""
        #     CREATE TABLE IF NOT EXISTS County (
        #         county VARCHAR(255) NOT NULL PRIMARY KEY,
        #         ohLine INT NOT NULL,
        #         CHECK (ohLine > 0)
        #     )
        # """)
        #
        # cursor.execute("""
        #     CREATE TABLE IF NOT EXISTS Storm (
        #         stormCode BIGINT NOT NULL PRIMARY KEY,
        #         eventDateTime TIMESTAMP NOT NULL,
        #         eventLength INT NOT NULL,
        #         UNIQUE (eventDateTime),
        #         CHECK (eventLength > 0)
        #     )
        # """)
        #
        # cursor.execute("""
        #     CREATE TABLE IF NOT EXISTS WeatherData (
        #         stormCode BIGINT NOT NULL,
        #         county VARCHAR(255) NOT NULL,
        #         sumAFWA_TOTPRECIP DOUBLE PRECISION NOT NULL,  -- Changed from REAL
        #         peakWSPD10 DOUBLE PRECISION NOT NULL,          -- Changed from REAL
        #         wgt5 INT NOT NULL,
        #         wgt9 INT NOT NULL,
        #         wgt13 INT NOT NULL,
        #         wgt18 INT NOT NULL,
        #         outage INT NOT NULL,
        #         PRIMARY KEY (stormCode, county),
        #         FOREIGN KEY (stormCode) REFERENCES Storm(stormCode),
        #         FOREIGN KEY (county) REFERENCES County(county),
        #         CHECK (sumAFWA_TOTPRECIP >= 0),
        #         CHECK (peakWSPD10 > 0),
        #         CHECK (wgt5 >= 0),
        #         CHECK (wgt9 >= 0),
        #         CHECK (wgt13 >= 0),
        #         CHECK (wgt18 >= 0),
        #         CHECK (outage >= 0)
        #     )
        # """)
        #
        # conn.commit()  -- Important: Commit table creation
        # =====================================================================

        # Track unique records to insert
        counties: Dict[str, int] = {}  # county -> ohLine
        storms: Dict[
            int, Tuple[datetime, int]
        ] = {}  # stormCode -> (eventDateTime, eventLength)
        weather_data: list = []  # List of weather data records

        # Read CSV and organize data
        # NOTE: This section is database-agnostic, no changes needed for PostgreSQL
        with open(csv_file, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            total_rows = 0

            for row in reader:
                total_rows += 1

                try:
                    # Extract and validate required fields
                    event_code = row["eventCode"].strip()
                    storm_code_str = row["stormCode"].strip()
                    event_length = int(row["eventLength"].strip())
                    county = row["County"].strip()
                    oh_line = int(row["ohLine"].strip())

                    # Parse storm code and derive event date time
                    parsed_storm_code, event_date_time = parse_storm_code(event_code)

                    # Validate stormCode matches parsed value
                    if int(storm_code_str) != parsed_storm_code:
                        print(
                            f"Warning: stormCode mismatch in row {total_rows}. "
                            f"Expected {parsed_storm_code}, got {storm_code_str}"
                        )

                    # Collect county data (deduplicate)
                    if county not in counties:
                        counties[county] = oh_line

                    # Collect storm data (deduplicate)
                    if parsed_storm_code not in storms:
                        storms[parsed_storm_code] = (event_date_time, event_length)

                    # Collect weather data
                    weather_data.append(
                        {
                            "stormCode": parsed_storm_code,
                            "county": county,
                            "sumAFWA_TOTPRECIP": float(
                                row["sumAFWA_TOTPRECIP"].strip()
                            ),
                            "peakWSPD10": float(row["peakWSPD10"].strip()),
                            "wgt5": int(row["wgt5"].strip()),
                            "wgt9": int(row["wgt9"].strip()),
                            "wgt13": int(row["wgt13"].strip()),
                            "wgt18": int(row["wgt18"].strip()),
                            "outage": int(row["outage"].strip()),
                        }
                    )

                except KeyError as ke:
                    print(f"Error in row {total_rows}: Missing column {str(ke)}")
                    print(f"Available columns: {list(row.keys())}")
                    raise
                except ValueError as ve:
                    print(f"Error in row {total_rows}: {str(ve)}")
                    raise

        print(f"\n{'=' * 70}")
        print(f"CSV Data Summary:")
        print(f"{'=' * 70}")
        print(f"Total CSV rows read:        {total_rows}")
        print(f"Unique counties:            {len(counties)}")
        print(f"Unique storms:              {len(storms)}")
        print(f"Total weather data records: {len(weather_data)}")

        # Insert data in the correct order: County -> Storm -> WeatherData

        # =====================================================================
        # 1. INSERT COUNTIES
        # TODO POSTGRES: Change ? to %s in SQL
        # =====================================================================
        print(f"\n{'=' * 70}")
        print(f"Inserting Counties...")
        print(f"{'=' * 70}")
        for county, oh_line in sorted(counties.items()):
            cursor.execute(
                "INSERT INTO County (county, ohLine) VALUES (?, ?)", (county, oh_line)
            )
            # POSTGRES: Change to:
            # cursor.execute(
            #     "INSERT INTO County (county, ohLine) VALUES (%s, %s)",
            #     (county, oh_line)
            # )
            print(f"  [OK] Inserted County: {county} (ohLine: {oh_line})")

        conn.commit()
        county_count = len(counties)
        print(f"Total counties inserted: {county_count}")

        # =====================================================================
        # 2. INSERT STORMS
        # TODO POSTGRES: Change ? to %s in SQL
        # =====================================================================
        print(f"\n{'=' * 70}")
        print(f"Inserting Storms...")
        print(f"{'=' * 70}")
        for storm_code in sorted(storms.keys()):
            event_date_time, event_length = storms[storm_code]
            cursor.execute(
                "INSERT INTO Storm (stormCode, eventDateTime, eventLength) VALUES (?, ?, ?)",
                (storm_code, event_date_time, event_length),
            )
            # POSTGRES: Change to:
            # cursor.execute(
            #     "INSERT INTO Storm (stormCode, eventDateTime, eventLength) VALUES (%s, %s, %s)",
            #     (storm_code, event_date_time, event_length)
            # )
            print(
                f"  [OK] Inserted Storm: {storm_code} (DateTime: {event_date_time}, Length: {event_length}h)"
            )

        conn.commit()
        storm_count = len(storms)
        print(f"Total storms inserted: {storm_count}")

        # =====================================================================
        # 3. INSERT WEATHER DATA
        # TODO POSTGRES: Change ? to %s in SQL
        # For better performance with large datasets, consider using COPY
        # =====================================================================
        print(f"\n{'=' * 70}")
        print(f"Inserting WeatherData...")
        print(f"{'=' * 70}")
        for idx, data in enumerate(weather_data, 1):
            cursor.execute(
                """INSERT INTO WeatherData
                   (stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                    wgt5, wgt9, wgt13, wgt18, outage)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    data["stormCode"],
                    data["county"],
                    data["sumAFWA_TOTPRECIP"],
                    data["peakWSPD10"],
                    data["wgt5"],
                    data["wgt9"],
                    data["wgt13"],
                    data["wgt18"],
                    data["outage"],
                ),
            )
            # POSTGRES: Change to:
            # cursor.execute(
            #     """INSERT INTO WeatherData
            #        (stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
            #         wgt5, wgt9, wgt13, wgt18, outage)
            #        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            #     (data["stormCode"], data["county"], data["sumAFWA_TOTPRECIP"],
            #      data["peakWSPD10"], data["wgt5"], data["wgt9"],
            #      data["wgt13"], data["wgt18"], data["outage"])
            # )

            if idx % 100 == 0:
                print(f"  [OK] Inserted {idx} weather records...")

        conn.commit()
        weather_count = len(weather_data)
        print(f"Total weather data records inserted: {weather_count}")

        # Print summary
        print(f"\n{'=' * 70}")
        print(f"Data Load Complete!")
        print(f"{'=' * 70}")
        print(f"Counties:   {county_count}")
        print(f"Storms:     {storm_count}")
        print(f"WeatherData: {weather_count}")

        return {
            "County": county_count,
            "Storm": storm_count,
            "WeatherData": weather_count,
            "total_csv_rows": total_rows,
            "connection": conn,  # Return connection for testing
        }

    except sqlite3.IntegrityError as e:
        # TODO POSTGRES: Change to psycopg2.IntegrityError
        print(f"\nError: Database constraint violation!")
        print(f"Details: {str(e)}", file=sys.stderr)
        conn.rollback()
        raise
    except ValueError as e:
        print(f"\nError: Data parsing error!")
        print(f"Details: {str(e)}", file=sys.stderr)
        conn.rollback()
        raise
    except Exception as e:
        print(f"\nError: Unexpected error during data load!")
        print(f"Details: {str(e)}", file=sys.stderr)
        conn.rollback()
        raise


if __name__ == "__main__":
    # Get CSV path from command line or use default
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "../csvs/testdata.csv"

    try:
        result = load_data(csv_path)
        print(f"\nAll data loaded successfully!")
        sys.exit(0)
    except Exception as e:
        print(f"\nData loading failed: {str(e)}", file=sys.stderr)
        sys.exit(1)
