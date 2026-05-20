# Database Loading Scripts - Implementation Guide

## Overview

This directory contains Python scripts for loading weather data from CSV files into a SQLite database with comprehensive testing to ensure data integrity.

## Files

- **`load_data.py`** (517 lines) - Main data loading script with PostgreSQL conversion guide
- **`test_load_data.py`** (407 lines) - Comprehensive test suite (27 tests)

## Quick Start

### Load Data
```bash
python load_data.py ../csvs/testdata.csv
```

### Run Tests
```bash
python test_load_data.py
```

### Load to File Database
```bash
python load_data.py ../csvs/testdata.csv wiser.db
```

## What It Does

### Data Loading Process

The script reads `testdata.csv` (2,352 rows) and creates three database tables:

```
County (8 records)
├── county (PRIMARY KEY)
└── ohLine

Storm (294 records)
├── stormCode (PRIMARY KEY)
├── eventDateTime
└── eventLength

WeatherData (2,352 records)
├── stormCode (FOREIGN KEY → Storm)
├── county (FOREIGN KEY → County)
├── sumAFWA_TOTPRECIP, peakWSPD10
├── wgt5, wgt9, wgt13, wgt18
└── outage
```

### Insertion Order

Data **must** be inserted in this order to satisfy foreign key constraints:
1. **County** → 8 unique Connecticut counties
2. **Storm** → 294 unique weather events (2005-2023)
3. **WeatherData** → 2,352 weather records

## Features

### `load_data.py`
- ✓ Reads CSV with UTF-8 BOM encoding support
- ✓ Automatically deduplicates counties and storms
- ✓ Parses eventCode (CT-YYYYMMDDHH) to extract timestamps
- ✓ Validates data integrity (stormCode consistency, value ranges)
- ✓ Shows progress every 100 records
- ✓ Supports in-memory or file-based SQLite databases
- ✓ Comprehensive error handling with detailed messages
- ✓ **Includes PostgreSQL conversion guide** (see "PostgreSQL Conversion" section)

### `test_load_data.py`
- ✓ 27 comprehensive unit tests
- ✓ Tests parsing, loading, queries, and error handling
- ✓ Validates row counts match CSV
- ✓ Checks foreign key constraints
- ✓ Tests SQL queries (SELECT, WHERE, JOIN, GROUP BY)
- ✓ Verifies data integrity (no duplicates)
- ✓ **100% pass rate**

## Expected Results

### Data Loading Output
```
======================================================================
CSV Data Summary:
======================================================================
Total CSV rows read:        2352
Unique counties:            8
Unique storms:              294
Total weather data records: 2352

======================================================================
Data Load Complete!
======================================================================
Counties:   8
Storms:     294
WeatherData: 2352

All data loaded successfully!
```

### Test Output
```
======================================================================
TEST SUMMARY
======================================================================
Tests run:    27
Successes:    27
Failures:     0
Errors:       0

[PASS] All tests passed!
```

## PostgreSQL Conversion

**NOTE**: Both `load_data.py` and `test_load_data.py` contain detailed PostgreSQL conversion notes at the top of their docstrings.

### Quick Summary

To convert from SQLite to PostgreSQL for production:

1. **Install PostgreSQL adapter**:
   ```bash
   pip install psycopg2-binary
   ```

2. **Key changes needed**:
   - Replace `import sqlite3` with `import psycopg2`
   - Replace `?` placeholders with `%s` in SQL queries
   - Update connection setup to use host, port, user, password
   - Change `executescript()` to individual `execute()` calls
   - Update exception handling from `sqlite3` to `psycopg2`

3. **See detailed comments in code**:
   - Open `load_data.py` → Read the PostgreSQL Conversion Notes section (lines 11-123)
   - Open `test_load_data.py` → Read the PostgreSQL Conversion Notes section (lines 9-63)

4. **Configuration**:
   ```python
   import psycopg2
   conn = psycopg2.connect(
       host='localhost',
       port=5432,
       database='wiser_db',
       user='wiser_user',
       password='your_password'
   )
   ```

5. **For production**, use environment variables:
   ```python
   import os
   conn = psycopg2.connect(
       host=os.getenv('DB_HOST'),
       port=os.getenv('DB_PORT', 5432),
       database=os.getenv('DB_NAME'),
       user=os.getenv('DB_USER'),
       password=os.getenv('DB_PASSWORD')
   )
   ```

### Why PostgreSQL?

SQLite is great for:
- Development and testing (current use)
- Single-user applications
- No server setup required

PostgreSQL is better for:
- **Production**: Multi-user access, better concurrency
- **Scaling**: Handles larger datasets and more complex queries
- **Reliability**: Built-in replication, backup features
- **Server deployment**: Like wisermaci.engr.uconn.edu

## Technical Details

### Storm Code Parsing

The script parses event codes like "CT-2005100800":
- Extracts timestamp: 2005100800
- Derives eventDateTime: 2005-10-08 00:00:00
- Validates against CSV stormCode column

### Data Deduplication

- **Counties**: Uses dict to track 8 unique county names
- **Storms**: Uses dict keyed by stormCode (294 unique)
- **WeatherData**: Stored as-is (one record per CSV row)

### UTF-8 BOM Handling

The CSV file has a UTF-8 BOM character. The script uses `encoding='utf-8-sig'` to automatically strip it and correctly read column names.

### Foreign Key Constraints

By inserting in order (County → Storm → WeatherData), all foreign key references are valid when WeatherData records are inserted.

## Data Specifications

### Connecticut Counties (8)
- Fairfield, Hartford, Litchfield, Middlesex
- New Haven, New London, Tolland, Windham

### Storms
- **stormCode**: 10-digit timestamp (YYYYMMDDHH)
- **eventDateTime**: Parsed from stormCode
- **eventLength**: 48 hours (standard)
- **Date Range**: 2005-2023

### Weather Data Fields
- **sumAFWA_TOTPRECIP**: Precipitation (mm), >= 0
- **peakWSPD10**: Peak wind speed (m/s), > 0
- **wgt5-18**: Hours with winds above threshold, >= 0
- **outage**: Trouble spots/outages, >= 0

## Test Coverage

### TestParseStormCode (4 tests)
- Valid event code parsing
- Date extraction from timestamps
- Invalid format error handling
- Invalid date error handling

### TestDataLoading (19 tests)
- Table structure verification
- Row count validation
- SELECT query tests
- JOIN and GROUP BY tests
- Data integrity checks
- Foreign key validation
- Value range checks
- Aggregate queries

### TestErrorHandling (4 tests)
- Missing CSV file
- Invalid data formats
- Constraint violations

## Common Queries

Once loaded, you can query the database:

```sql
-- All counties
SELECT * FROM County ORDER BY county;

-- Storms with high outages
SELECT w.stormCode, w.county, w.outage
FROM WeatherData w
WHERE w.outage > 100
ORDER BY w.outage DESC;

-- Weather summary by county
SELECT w.county, COUNT(*) as storms, AVG(w.sumAFWA_TOTPRECIP) as avg_precip
FROM WeatherData w
GROUP BY w.county
ORDER BY avg_precip DESC;

-- Storm date range
SELECT MIN(eventDateTime) as earliest, MAX(eventDateTime) as latest
FROM Storm;
```

## Programmatic Usage

```python
from load_data import load_data

# Load data
result = load_data('../csvs/testdata.csv')
conn = result['connection']
cursor = conn.cursor()

# Query data
cursor.execute("SELECT * FROM County")
for county in cursor.fetchall():
    print(county)

# Access counts
print(f"Counties: {result['County']}")
print(f"Storms: {result['Storm']}")
print(f"Weather records: {result['WeatherData']}")
```

## Troubleshooting

### "CSV file not found"
Make sure you're in the correct directory:
```bash
cd WISER/database/scripts
python load_data.py ../csvs/testdata.csv
```

### Encoding Issues
The script handles UTF-8 BOM automatically. If you have a different CSV encoding, modify line 121:
```python
with open(csv_file, 'r', encoding='utf-8-sig') as f:
```

### Foreign Key Errors
The script inserts data in the correct order. If you modify the code, ensure the order is maintained: County → Storm → WeatherData.

## Performance

- Load 2,352 rows: < 1 second
- Run 27 tests: ~0.04 seconds
- Query operations: < 10ms

## Requirements

- Python 3.7+
- Built-in modules only: `sqlite3`, `csv`, `datetime`, `pathlib`, `unittest`
- No external dependencies required

## Design Decisions

1. **SQLite**: Lightweight, no server setup required
2. **In-memory by default**: Fast testing, optional file persistence
3. **Deduplication at load time**: Prevents constraint violations
4. **Ordered insertion**: Respects foreign key dependencies
5. **Comprehensive testing**: 27 tests cover all scenarios
6. **UTF-8 BOM handling**: Solves real-world CSV encoding issues

## Implementation Notes

### Solved Issues

1. **UTF-8 BOM** - CSV had BOM character; used `utf-8-sig` encoding
2. **Duplicate data** - CSV had duplicate counties/storms; automatic deduplication
3. **Foreign keys** - Inserted in correct order to satisfy constraints
4. **SQLite COUNT** - Used subquery for COUNT(DISTINCT col1, col2)
5. **Terminal encoding** - Replaced special characters with ASCII equivalents

### Data Quality

✓ All 2,352 CSV rows loaded
✓ 8 Connecticut counties verified
✓ 294 unique storms parsed
✓ No data loss or modification
✓ All constraints enforced
✓ 100% test pass rate

## Summary

This implementation provides a robust, tested solution for loading weather data into a SQLite database (or PostgreSQL for production). The scripts handle real-world issues (encoding, duplicates, constraints) and include comprehensive testing to ensure data integrity.

Both scripts contain extensive PostgreSQL conversion comments to guide eventual migration to production database.

**Status**: Production-ready ✓
