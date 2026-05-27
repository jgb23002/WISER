# WISER

![WISER Image](docs/WISER%20Image.png)

**W**eather **I**ntegrated **S**torm **E**vent **R**esponse — a Connecticut storm outage analytics system built on a multi-agent architecture. It ingests raw storm CSV data, normalizes it into a relational SQLite database, and answers natural-language queries through a task routing + analytics pipeline.

> See the live system at [wisermaci.engr.uconn.edu](http://wisermaci.engr.uconn.edu/)

---

## Prerequisites

- Python **3.9+**
- No external dependencies — everything uses the Python standard library (`sqlite3`, `csv`, `datetime`, `argparse`, `unittest`)

---

## Setup

**1. Clone the repo and navigate into it**
```bash
git clone <repo-url>
cd WISER
```

**2. (Optional but recommended) Create and activate a virtual environment**
```bash
# macOS / Linux
python -m venv .venv
source .venv/bin/activate

# Windows
python -m venv .venv
.venv\Scripts\activate
```

**3. Install dependencies**
```bash
pip install -r requirements.txt
```
> No packages are installed at this stage — the file is a placeholder for future additions.

---

## Running the Project

### Step 1 — Bootstrap the database

Parses `data/testdata.csv`, normalizes it into three tables, writes normalized CSVs, and builds `data/wiser.db`:

```bash
python scripts/bootstrap_wiser.py
```

Expected output:
```
Normalized files written:
  - data/normalized/storm.csv
  - data/normalized/county.csv
  - data/normalized/weather_data.csv

Top 5 counties by total outages:
  - Fairfield: total=..., avg=..., peak=..., storms=...
  ...

SQLite database built: data/wiser.db
Table counts:
  - Storm: 294
  - County: 8
  - WeatherData: 2352
```

Override defaults with flags:
```bash
python scripts/bootstrap_wiser.py \
  --input data/testdata.csv \
  --output-dir data/normalized \
  --db data/wiser.db
```

---

### Step 2 — Query with the CLI

`wiser_cli.py` routes natural-language queries through the agent system. The **TaskRoutingAgent** classifies your query by keyword and dispatches it to the appropriate agent:

| Query type | Keywords detected | Agent called |
|---|---|---|
| Data / DB management | `load`, `csv`, `database`, `schema`, `ingest` … | `DataRetrievalProcessingAgent` |
| Weather analytics | `outage`, `storm`, `county`, `top`, `trend` … | `StormWeatherAnalyticsAgent` |

```bash
# Top counties by total outage
python scripts/wiser_cli.py "top counties by outage" --limit 5

# Top storms by total outage
python scripts/wiser_cli.py "top storms by outage" --limit 5

# Full storm history for a specific county
python scripts/wiser_cli.py "history for county Hartford" --limit 10

# Rebuild the database from CSV
python scripts/wiser_cli.py "build sqlite database from csv"
```

The `--limit` flag controls how many rows are returned for top-N queries (default: 10).

---

## Running the Tests

### Schema tests

```bash
python -m unittest tests.test_schema -v
```

Expected output:
```
test_county_columns ... ok
test_county_primary_key ... ok
...
----------------------------------------------------------------------
Ran 27 tests in 0.012s

OK
```

The test suite validates the database schema in an isolated in-memory SQLite instance — no files are created. Tests cover:

| Class | Tests | What's validated |
|---|---|---|
| `TestSchemaStructure` | 11 | Tables exist, column names, data types, PKs, indexes |
| `TestConstraints` | 13 | CHECK, UNIQUE, FK, composite PK enforcement |
| `TestDataIntegrity` | 3 | Realistic inserts, zero-value edge cases |

---

### Storm similarity algorithm tests

Run from the `scripts/` directory so the relative import of `find_similar_storms` resolves correctly:

```bash
cd scripts
python -m unittest test_find_similar_storms -v
```

Or run a single named test class:

```bash
cd scripts
python -m unittest test_find_similar_storms.TestComputeStatistics -v
python -m unittest test_find_similar_storms.TestCalculateSimilarity -v
python -m unittest test_find_similar_storms.TestFindSimilarStorms -v
```

Expected output:
```
test_all_identical_historical_winds_matching_current ... ok
test_all_identical_historical_winds_non_matching_current ... ok
...
----------------------------------------------------------------------
Ran 53 tests in 0.05s

OK
```

All tests create and destroy their own temporary SQLite database — no existing data files are touched.

#### What the tests cover

| Class | Tests | What's validated |
|---|---|---|
| `TestComputeStatistics` | 13 | Mean, Bessel-corrected std dev, min/max, empty list, single value, negative and large speeds |
| `TestCalculateSimilarity` | 12 | Similarity formula `1/(1+\|Δz\|)`, Z-score correctness, symmetry, zero std-dev branch, [0,1] range, MD spec example (91.7%) |
| `TestFetchHistoricalStorms` | 8 | Empty DB, county filtering, case-insensitivity, `limit_fetch`, field presence and types |
| `TestFindSimilarStorms` | 18 | Descending sort order, `limit` (cap / over-fetch / zero), exact-match rank, `_statistics` metadata, county isolation, field preservation |
| `TestIntegration` | 7 | Realistic distributions, outlier storms, determinism, metadata vs manual calculation, two-county independence, rank ordering |

---

## Project Structure

```text
WISER/
├── data/
│   └── testdata.csv              # Raw input — 2,352 storm-county rows
├── docs/
│   ├── ARCHITECTURE.md           # System design & data flow diagrams
│   └── WISER Database Info.png   # Schema diagram
├── scripts/
│   ├── bootstrap_wiser.py           # Parses CSV → normalizes → builds DB
│   ├── find_similar_storms.py       # Statistical storm similarity (Z-score ranking)
│   ├── test_find_similar_storms.py  # 53 unit + integration tests for the above
│   └── wiser_cli.py                 # Natural-language query entry point
├── sql/
│   └── schema.sql                # DDL — source of truth for all three tables
├── src/
│   └── wiser/
│       ├── data_pipeline.py      # CSV parsing & row normalization
│       ├── database.py           # Schema init, data loading, row counts
│       ├── query_engine.py       # SQL queries (top counties, storms, history)
│       └── agents/
│           ├── task_routing_agent.py              # Keyword → route classifier
│           ├── data_retrieval_processing_agent.py # Orchestrates pipeline + DB build
│           └── storm_weather_analytics_agent.py   # Wraps query engine for analytics
├── tests/
│   ├── __init__.py
│   └── test_schema.py            # 27 schema validation tests
├── requirements.txt
└── README.md
```

---

## How It Works

```
data/testdata.csv
       │
       ▼
data_pipeline.py          Parse eventCode → stormCode + eventDateTime
                          Deduplicate into Storm (294), County (8), WeatherData (2,352)
       │
       ▼
database.py               Create tables, enforce FK/CHECK constraints, insert rows
       │
       ▼
data/wiser.db             SQLite database with 3 normalized tables + 3 indexes
       │
       ├── TaskRoutingAgent        Classifies query by keyword
       │         │
       │    DATA_RETRIEVAL ──────► DataRetrievalProcessingAgent  (rebuild DB)
       │    WEATHER_ANALYTICS ───► StormWeatherAnalyticsAgent    (query DB)
       │
       ▼
  Console output
```

---

## Database Schema

Schema defined in `sql/schema.sql`. Three normalized tables:

| Table | Primary Key | Rows |
|---|---|---|
| `Storm` | `stormCode BIGINT` | 294 |
| `County` | `county VARCHAR(64)` | 8 |
| `WeatherData` | `(stormCode, county)` | 2,352 |

![Schema Diagram](docs/WISER%20Database%20Info.png)

Key decisions:
- **BIGINT** for `stormCode` and `ohLine` — 10-digit codes require more than 32-bit int
- **TIMESTAMP** for `eventDateTime` — proper temporal type, not plain text
- **VARCHAR(64)** for `county` — bounded, not unbounded TEXT
- **`state` column dropped** — always `CT`, storing it would be redundant
- **CHECK constraints** on all numeric fields — invalid data rejected at DB level
- **3 indexes** — `outage DESC`, `county`, `eventDateTime` for common query patterns

## Data Field Reference

| Field | Type | Description |
|---|---|---|
| `stormCode` | BIGINT | 10-digit code formatted YYYYMMDDHH (e.g. `2005100800`) |
| `eventDateTime` | TIMESTAMP | Parsed from stormCode — event start time |
| `eventLength` | INTEGER | Storm duration in hours (always 48) |
| `county` | VARCHAR(64) | One of 8 Connecticut counties |
| `ohLine` | BIGINT | Overhead power line length in county |
| `sumAFWA_TOTPRECIP` | REAL | Total precipitation during storm (mm) |
| `peakWSPD10` | REAL | Peak wind speed at 10 m height (m/s) |
| `wgt5 / wgt9 / wgt13 / wgt18` | INTEGER | Hours where wind exceeded 5/9/13/18 m/s |
| `outage` | INTEGER | Trouble spots where crews were dispatched |
