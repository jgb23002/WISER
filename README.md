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
| Similarity search | `similar`, `find similar`, `rank storms`, or parameter patterns (e.g., `wind=`) … | `SimilarityQueryAgent` |

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

# Run a similarity search using natural language
python scripts/wiser_cli.py "find storms similar to wind=9.2 precip=45 in Fairfield" --limit 5

# Ask for a short storm-impact summary from NOAA/NHC/NCEI web/PDF context
python scripts/wiser_cli.py "impact of the new storm in Connecticut" --limit 5
```

The `--limit` flag controls how many rows are returned for top-N queries (default: 10).
For RAG queries, `--limit` controls retrieved context chunks. Use `--rebuild-rag` to refresh the local index, `--show-rag-context` to print the full augmented prompt, and repeat `--rag-source <path>` to index a custom file or directory.
For similarity searches, `--county` overrides county auto-detection from query text.

Impact-style storm questions automatically crawl the default NOAA/NHC/NCEI context sources, including linked National Hurricane Center Tropical Cyclone Report PDFs. WISER stores extracted web/PDF paragraphs in the existing RAG tables, ranks them with optional Meta/Facebook FAISS similarity search when available, and falls back to a dependency-free exact text-vector ranking otherwise. See [docs/EXTERNAL_STORM_CONTEXT.md](docs/EXTERNAL_STORM_CONTEXT.md).

If `OPENAI_API_KEY` is set in `.env` or the shell environment, WISER uses the OpenAI Responses API to generate short cited impact summaries from the retrieved paragraphs. Without a key, or with `--no-openai-summary`, it uses the local extractive summary.

---

### Step 3 — Storm similarity (multi-dimensional parameters)

After bootstrap, use `find_similar_storms.py` to rank historical storms in a county by how close they are to a current event across up to 6 weather features. Similarity is calculated using a multi-dimensional Z-score Euclidean (L2) distance in a standardized, unit-free space.

**Valid counties** (case-insensitive; use quotes for two-word names):

`Fairfield`, `Hartford`, `Litchfield`, `Middlesex`, `New Haven`, `New London`, `Tolland`, `Windham`

```bash
# Direct multi-dimensional similarity search using wind and precipitation
python scripts/find_similar_storms.py --county Fairfield --wind-speed 9.2 --precip 45.0 --limit 5

# Search using only peak wind speed (produces identical output to the legacy 1-D algorithm)
python scripts/find_similar_storms.py --county Hartford --wind-speed 6.5

# Search using other duration threshold weather features
python scripts/find_similar_storms.py --county "New Haven" --wind-speed 10.5 --wgt5 12 --wgt9 4 --limit 5

# Use a different database file
python scripts/find_similar_storms.py --county Windham --wind-speed 7.25 --db data/wiser.db
```

| Flag | Required | Description |
|---|---|---|
| `--county` | Yes | Connecticut county name |
| `--wind-speed` | No | Current storm peak wind speed at 10 m, in **m/s** (must be > 0) |
| `--precip` | No | Total precipitation during storm, in **mm** |
| `--wgt5 / --wgt9 / --wgt13 / --wgt18` | No | Hours where wind exceeded 5/9/13/18 m/s |
| `--limit` | No | Number of similar storms to show (default: `5`) |
| `--db` | No | SQLite path (default: `data/wiser.db`) |

Example output (values depend on your inputs):

```
====================================================================================================
Storm Similarity Analysis for Fairfield
====================================================================================================

Query Parameters:
  Wind (m/s): 9.2
  Precip (mm): 45

Historical storms in Fairfield: 294

Rank   Storm Code      Similarity   Outages    Wind (m/s)   Precip (mm)  Event Date         
--------------------------------------------------------------------------------
1      2019101618      82.4%        132        9.202        35.4         2019-10-16 18:00:00
...
```

Use the ranked historical **outages** as a rough guide for what a current event with similar weather conditions might look like in that county. Algorithm details: [docs/STORM_SIMILARITY_ALGORITHM.md](docs/STORM_SIMILARITY_ALGORITHM.md).

---

## Running the Tests

To run the entire test suite (104 tests) covering database schema, RAG indexing/retrieval, task routing, query parsing, and similarity algorithms:

```bash
python -m unittest discover -s tests -v
```

Expected output:
```
...
----------------------------------------------------------------------
Ran 104 tests in 0.120s

OK
```

The test files cover the following functionality:

| Test File | Description |
|---|---|
| `tests/test_schema.py` | Validates database constraints (CHECK, UNIQUE, FK), composite primary keys, table structures, and column datatypes. |
| `tests/test_database.py` | Verifies database schema initialization and backwards compatibility with legacy structures. |
| `tests/test_rag.py` | Tests grounding, chunking, indexing, and answers retrieval inside the RAG engine. |
| `tests/test_similarity_algorithm.py` | Validates Z-score calculations, statistics, symmetry, and legacy single-dimension mathematical correctness. |
| `tests/test_similarity_engine.py` | Verifies multi-dimensional Z-score calculations (parity, weights overrides, zero std dev, partial features). |
| `tests/test_task_routing.py` | Tests keyword and pattern matching routing decisions (avoiding RAG collisions). |
| `tests/test_wiser_cli.py` | Verifies CLI query parsing, county auto-detection, and dispatch registry handlers. |
| `tests/test_data_pipeline.py` | Validates parsing of CSV columns and correct extraction of state codes from event codes. |

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
│   ├── bootstrap_wiser.py        # Parses CSV → normalizes → builds DB
│   ├── find_similar_storms.py    # Storm similarity CLI (supports up to 6 weather features)
│   └── wiser_cli.py              # Natural-language query entry point
├── sql/
│   └── schema.sql                # DDL — source of truth for all three tables
├── src/
│   └── wiser/
│       ├── data_pipeline.py      # CSV parsing & row normalization
│       ├── database.py           # Schema init, data loading, row counts
│       ├── query_engine.py       # SQL queries (top counties, storms, history)
│       ├── rag.py                # RAG document indexing + lexical retrieval
│       ├── similarity_engine.py  # Multi-dimensional L2 Z-score similarity calculations
│       └── agents/
│           ├── task_routing_agent.py              # Keyword → route classifier
│           ├── data_retrieval_processing_agent.py # Orchestrates pipeline + DB build
│           ├── rag_knowledge_agent.py             # Builds/searches local RAG index
│           ├── similarity_query_agent.py          # Performs similarity query & ranking
│           └── storm_weather_analytics_agent.py   # Wraps query engine for analytics
├── tests/
│   ├── __init__.py
│   ├── test_data_pipeline.py     # Data pipeline and state extraction tests
│   ├── test_database.py          # Database initialization compatibility tests
│   ├── test_rag.py               # RAG indexing/retrieval tests
│   ├── test_schema.py            # 27 schema validation tests
│   ├── test_task_routing.py      # Route classification tests
│   ├── test_similarity_algorithm.py  # Legacy similarity algorithm correctness tests
│   ├── test_similarity_engine.py # Multi-dimensional similarity engine tests
│   └── test_wiser_cli.py         # CLI parsing and dispatcher handler registry tests
├── requirements.txt
└── README.md
```

---

## How It Works

```
data/testdata.csv
       │
       ▼
data_pipeline.py          Parse eventCode → stormCode + eventDateTime + state
                          Deduplicate into Storm (294), County (8), WeatherData (2,352)
       │
       ▼
database.py               Create tables, enforce FK/CHECK constraints, insert rows
       │
       ▼
data/wiser.db             SQLite database with 3 normalized tables + 3 indexes
       │
       ├── TaskRoutingAgent        Classifies query by keyword/parameters
       │         │
       │    DATA_RETRIEVAL ──────► DataRetrievalProcessingAgent  (rebuild DB)
       │    RAG_KNOWLEDGE ───────► RAGKnowledgeAgent             (retrieve sourced context)
       │    WEATHER_ANALYTICS ───► StormWeatherAnalyticsAgent    (query DB)
       │    SIMILARITY_SEARCH ───► SimilarityQueryAgent          (rank storms via Z-score)
       │
       ├── find_similar_storms.py  --county + weather flags → ranked historical matches
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
- **`state` column in County** — state abbreviation (e.g. `'CT'`) parsed from `eventCode` to support potential multi-state expansion, defaults to `'CT'` for backwards compatibility
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
