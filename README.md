# WISER

![WISER Image](docs/WISER%20Image.png)

**W**eather **I**ntegrated **S**torm **E**vent **R**esponse — a Connecticut storm outage analytics system built on a multi-agent architecture. It ingests raw storm CSV data, normalizes it into a relational SQLite database, answers natural-language analytics queries, and retrieves grounded RAG context from project docs/code.

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
| RAG / knowledge retrieval | `rag`, `docs`, `explain`, `algorithm`, `reference` … | `RAGKnowledgeAgent` |
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

# Ask a RAG question and receive a grounded answer with sources
python scripts/wiser_cli.py "explain the storm similarity algorithm" --limit 3
```

The `--limit` flag controls how many rows are returned for top-N queries (default: 10).
For RAG queries, `--limit` controls retrieved context chunks. Use `--rebuild-rag` to refresh the local index, `--show-rag-context` to print the full augmented prompt, and repeat `--rag-source <path>` to index a custom file or directory.

---

### Step 3 — Storm similarity (your county & wind speed)

After bootstrap, use `find_similar_storms.py` to rank historical storms in a county by how close their peak wind speed (`peakWSPD10`) is to a **current** value you choose. Pass any county and any positive wind speed in m/s.

**Valid counties** (case-insensitive; use quotes for two-word names):

`Fairfield`, `Hartford`, `Litchfield`, `Middlesex`, `New Haven`, `New London`, `Tolland`, `Windham`

```bash
# Your county + your wind speed (m/s) — change both freely
python scripts/find_similar_storms.py --county Fairfield --wind-speed 9.2

# Different county and speed
python scripts/find_similar_storms.py --county Hartford --wind-speed 6.5

# Two-word county (quote the name on all shells)
python scripts/find_similar_storms.py --county "New Haven" --wind-speed 10.5

# Return more or fewer matches (default: 5)
python scripts/find_similar_storms.py --county Litchfield --wind-speed 8.0 --limit 10

# Use a different database file
python scripts/find_similar_storms.py --county Windham --wind-speed 7.25 --db data/wiser.db
```

| Flag | Required | Description |
|---|---|---|
| `--county` | Yes | Connecticut county name |
| `--wind-speed` | Yes | Current storm peak wind at 10 m, in **m/s** (must be > 0) |
| `--limit` | No | Number of similar storms to show (default: `5`) |
| `--db` | No | SQLite path (default: `data/wiser.db`) |

Example output (values depend on your inputs):

```
Storm Similarity Analysis for Fairfield
  Peak Wind Speed (peakWSPD10): 9.20 m/s
  Mean Wind Speed: 7.41 m/s  |  Std Dev: 1.69 m/s

Rank   Storm Code      Wind (m/s)   Similarity   Outages    Event Date
1      2020022700      9.20         99.9%        51         2020-02-27 00:00:00
...
```

Use the ranked historical **outages** as a rough guide for what a current event with similar wind might look like in that county. Algorithm details: [docs/STORM_SIMILARITY_ALGORITHM.md](docs/STORM_SIMILARITY_ALGORITHM.md).

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

```bash
python -m unittest tests.test_similarity_algorithm -v
```

Covers statistics (`compute_statistics`), Z-score similarity (`calculate_similarity`), symmetry, edge cases (zero std dev), and Fairfield-style scenarios. The full suite also covers RAG chunking/indexing/search, task routing, and database initialization compatibility:

```bash
python -m unittest discover -s tests -v
```

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
│   ├── find_similar_storms.py       # Storm similarity CLI (--county, --wind-speed)
│   └── wiser_cli.py                 # Natural-language query entry point
├── sql/
│   └── schema.sql                # DDL — source of truth for all three tables
├── src/
│   └── wiser/
│       ├── data_pipeline.py      # CSV parsing & row normalization
│       ├── database.py           # Schema init, data loading, row counts
│       ├── query_engine.py       # SQL queries (top counties, storms, history)
│       ├── rag.py                # RAG document indexing + lexical retrieval
│       └── agents/
│           ├── task_routing_agent.py              # Keyword → route classifier
│           ├── data_retrieval_processing_agent.py # Orchestrates pipeline + DB build
│           ├── rag_knowledge_agent.py             # Builds/searches local RAG index
│           └── storm_weather_analytics_agent.py   # Wraps query engine for analytics
├── tests/
│   ├── __init__.py
│   ├── test_database.py          # Database initialization compatibility tests
│   ├── test_rag.py               # RAG indexing/retrieval tests
│   ├── test_schema.py            # 27 schema validation tests
│   ├── test_task_routing.py      # Route classification tests
│   └── test_similarity_algorithm.py  # Similarity algorithm unit tests
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
       │    RAG_KNOWLEDGE ───────► RAGKnowledgeAgent             (retrieve sourced context)
       │    WEATHER_ANALYTICS ───► StormWeatherAnalyticsAgent    (query DB)
       │
       ├── find_similar_storms.py  --county + --wind-speed → ranked historical matches
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
