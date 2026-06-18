# WISER System Architecture

## System Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         DATA SOURCES & INPUT                             │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                           │
│  data/testdata.csv  (2,352 rows of raw weather data)                   │
│  Columns: eventCode, stormCode, eventLength, County, ohLine,            │
│           sumAFWA_TOTPRECIP, peakWSPD10, wgt5-18, outage                │
│                                                                           │
│  README.md, docs/, scripts/, sql/, src/wiser/                             │
│  Project knowledge sources for local RAG retrieval                         │
│                                                                           │
└────────────────────────────┬────────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    DATA PIPELINE PROCESSING                              │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                           │
│  1. Parse CSV                                                            │
│     └─ src/wiser/data_pipeline.py::load_raw_csv()                       │
│                                                                           │
│  2. Normalize Data                                                       │
│     └─ src/wiser/data_pipeline.py::normalize_rows()                     │
│        ├─ Parse eventCode → (stormCode, eventDateTime, state)           │
│        ├─ Extract unique Storm records (294 unique)                     │
│        ├─ Extract unique County records with state (8 unique)           │
│        └─ Create WeatherData records (2,352 records)                    │
│                                                                           │
│  3. Write Normalized CSVs                                                │
│     └─ src/wiser/data_pipeline.py::write_normalized_csvs()              │
│        ├─ data/normalized/storm.csv                                      │
│        ├─ data/normalized/county.csv                                     │
│        └─ data/normalized/weather_data.csv                               │
│                                                                           │
└────────────────────────────┬────────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      DATABASE LAYER                                      │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                           │
│  1. Initialize Schema                                                    │
│     └─ src/wiser/database.py::initialize_schema(conn)                   │
│        └─ Load from sql/schema.sql                                       │
│           ├─ CREATE TABLE Storm                                          │
│           ├─ CREATE TABLE County                                         │
│           ├─ CREATE TABLE WeatherData                                    │
│           └─ CREATE INDEXES                                              │
│                                                                           │
│  2. Load Normalized Data                                                 │
│     └─ src/wiser/database.py::load_normalized_rows()                    │
│        ├─ INSERT INTO Storm (294 rows)                                   │
│        ├─ INSERT INTO County with state (8 rows)                         │
│        └─ INSERT INTO WeatherData (2,352 rows)                           │
│                                                                           │
│  Result: data/wiser.db (SQLite database)                                 │
│                                                                           │
└────────────────────────────┬────────────────────────────────────────────┘
                             │
         ┌───────────────────┼───────────────────┐
         │                   │                   │
         ▼                   ▼                   ▼
┌──────────────────────────┐ │ ┌───────────────────────┐
│   AGENT LAYER            │ │ │   QUERY LAYER         │
├──────────────────────────┤ │ ├───────────────────────┤
│                          │ │ │                       │
│  Task Routing Agent      │ │ │  Query Engine         │
│  ├─ Routes NL queries    │ │ │  ├─ top_counties...() │
│  │  by keyword/params    │ │ │  ├─ top_storms...()   │
│  │                       │ │ │  └─ county_history()  │
│  └─ Output: Route enum   │ │ │                       │
│                          │ │ │  data/wiser.db ◄──────┘
│  Data Retrieval Agent    │ │ └───────────────────────┘
│  ├─ normalize()          │ │
│  ├─ save_normalized()    │ │
│  └─ build_database()     │ ├─────────────────┐
│                          │                   │
│  Analytics Agent         │                   ▼
│  ├─ county_outage_...()  │        ┌───────────────────────┐
│  ├─ top_storms_by...()   │        │  SIMILARITY ENGINE    │
│  ├─ county_storm_...()   │        ├───────────────────────┤
│  │                       │        │                       │
│  RAG Knowledge Agent     │        │  Similarity Engine    │
│  ├─ ensure_index()       │        │  ├─ rank_by_sim...()  │
│  ├─ retrieve()           │        │  └─ compute_stats()   │
│  ├─ build_context()      │        │                       │
│  └─ answer()             │        └───────────────────────┘
│                          │
│  Similarity Query Agent  │
│  ├─ find_similar() ──────┼───────────────▲
│                          │               │
└────────┬─────────────────┘               │
         │                                 │
         ▼                                 │
┌──────────────────────────────────────────┴──────────────────────────────┐
│                     USER INTERFACES                                      │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                           │
│  1. Bootstrap Script                                                     │
│     scripts/bootstrap_wiser.py                                           │
│     └─ Orchestrates: Parse → Normalize → Load DB                        │
│                                                                           │
│  2. CLI Interface                                                        │
│     scripts/wiser_cli.py                                                 │
│     └─ Accepts natural language queries and parses key=value parameters: │
│        "top counties by outage"                                          │
│        "find storms similar to wind=9.2 precip=45 in Fairfield"          │
│        "explain the storm similarity algorithm"                          │
│                                                                           │
│  3. Similarity Search CLI                                                │
│     scripts/find_similar_storms.py                                       │
│     └─ Directly queries multidimensional storm similarity:               │
│        python scripts/find_similar_storms.py --county Hartford \         │
│               --wind-speed 6.5 --precip 30.0                             │
│                                                                           │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Database Schema Detail

```
STORM TABLE
┌─────────────┬─────────────┬──────────────┐
│ stormCode   │ eventDT     │ eventLength  │
│ (BIGINT)    │ (TIMESTAMP) │ (INTEGER)    │
├─────────────┼─────────────┼──────────────┤
│ 2005100800  │ 2005-10-08  │ 48           │
│ 2005101512  │ 2005-10-15  │ 48           │
│ ...         │ ...         │ ...          │
└─────────────┴─────────────┴──────────────┘
  Primary Key: stormCode
  Unique: eventDateTime
  Check: eventLength > 0

COUNTY TABLE
┌──────────────┬──────────────┬──────────────┐
│ county       │ state        │ ohLine       │
│ (VARCHAR64)  │ (VARCHAR2)   │ (BIGINT)     │
├──────────────┼──────────────┼──────────────┤
│ Fairfield    │ CT           │ 4887830      │
│ Hartford     │ CT           │ 5869550      │
│ Litchfield   │ CT           │ 3975201      │
│ Middlesex    │ CT           │ 2302299      │
│ New Haven    │ CT           │ 3233197      │
│ New London   │ CT           │ 2761047      │
│ Tolland      │ CT           │ 2028414      │
│ Windham      │ CT           │ 2400780      │
└──────────────┴──────────────┴──────────────┘
  Primary Key: county
  Check: LENGTH(state) = 2
  Check: ohLine > 0

WEATHERDATA TABLE
┌──────────┬───────────┬─────────┬──────────┬──────┬──────┬──────┬──────┬────────┐
│stormCode │ county    │ precip  │ windspd  │ wgt5 │ wgt9 │wgt13 │wgt18 │ outage │
│(BIGINT)  │(VARCHAR64)│ (REAL)  │ (REAL)   │(INT) │(INT) │(INT) │(INT) │ (INT)  │
├──────────┼───────────┼─────────┼──────────┼──────┼──────┼──────┼──────┼────────┤
│2005100800│Fairfield  │ 231.98  │ 5.79     │  9   │  0   │  0   │  0   │  186   │
│2005100800│ Hartford  │ 209.49  │ 6.36     │  15  │  1   │  0   │  0   │  86    │
│2005100800│Litchfield │ 228.15  │ 5.69     │  8   │  1   │  0   │  0   │  55    │
│...       │...        │...      │...       │...   │...   │...   │...   │...     │
└──────────┴───────────┴─────────┴──────────┴──────┴──────┴──────┴──────┴────────┘
  Composite Primary Key: (stormCode, county)
  Foreign Keys: stormCode→Storm, county→County
  Checks: precip≥0, windspd>0, wgt*≥0, outage≥0

INDEXES
  idx_weatherdata_outage(outage DESC)
  idx_weatherdata_county(county)
  idx_storm_datetime(eventDateTime)
```

---

## Data Transformation Pipeline

### Step 1: Parse eventCode

```
Input:   "CT-2005100800"
           ▼
Split by '-'
           ▼
State:   "CT"  (extracted and stored in County)
Code:    "2005100800"
           ▼
Parse as YYYYMMDDHH
           ▼
stormCode:     2005100800 (BIGINT)
eventDateTime: 2005-10-08 00:00:00 (TIMESTAMP)
```

### Step 2: Deduplicate

```
Raw CSV (2,352 rows)
      ▼
Extract Unique Storms: 294
Extract Unique Counties: 8 (Fairfield, Hartford, Litchfield, Middlesex,
                           New Haven, New London, Tolland, Windham) with state abbreviation
Extract WeatherData: 2,352 (one per CSV row, each storm-county pair)
      ▼
Normalized Data
```

### Step 3: Insert with FK Constraints

```
1. INSERT INTO Storm (294 rows)
   ✓ All stormCodes valid
   ✓ All eventDateTimes unique
   ✓ All eventLengths > 0

2. INSERT INTO County (8 rows)
   ✓ All county names valid
   ✓ state abbreviation present
   ✓ All ohLine values > 0

3. INSERT INTO WeatherData (2,352 rows)
   ✓ All stormCodes reference existing Storm
   ✓ All counties reference existing County
   ✓ All numeric values pass CHECK constraints
   ✓ No duplicate (stormCode, county) pairs
```

---

## Query Examples

### Multi-Dimensional Storm Similarity

```python
# Agent Call
similarity_agent = SimilarityQueryAgent()
results = similarity_agent.find_similar(
    conn,
    county="Fairfield",
    query_params={"peakWSPD10": 9.2, "sumAFWA_TOTPRECIP": 45.0},
    limit=5
)

# Internally utilizes similarity_engine.py to compute L2 Z-score distances:
# stats = compute_feature_stats(historical, ["peakWSPD10", "sumAFWA_TOTPRECIP"])
# distance = compute_l2_zscore_distance(hist_values, query_values, stats, weights)
# similarity = distance_to_similarity(distance)
```

### RAG Knowledge Retrieval

```python
# Agent Call
rag_agent.ensure_index(conn)
answer = rag_agent.answer(conn, "explain storm similarity algorithm", limit=3)

# Tables Created
RagDocument(path, title, checksum, indexedAt)
RagChunk(documentId, chunkIndex, content, tokenCount)
```

RAG currently uses dependency-free lexical retrieval over project docs/code/schema, then builds an extractive grounded answer with citation markers tied to retrieved chunks. The `build_context()` method still formats those chunks into an augmented prompt for a later LLM generation step.

### Top 5 Counties by Total Outage

```python
# Agent Call
analytics_agent.county_outage_summary_from_db(conn, limit=5)

# SQL Executed
SELECT county,
       COUNT(*) AS storms_observed,
       SUM(outage) AS total_outage,
       ROUND(AVG(outage), 2) AS avg_outage,
       MAX(outage) AS peak_outage
FROM WeatherData
GROUP BY county
ORDER BY total_outage DESC
LIMIT 5;

# Uses Index: idx_weatherdata_outage (DESC on outage)
```

---

## Testing Architecture

```
test_schema.py (27 schema-integrity tests)
├─ TestSchemaStructure (11 tests)
│  ├─ Table existence
│  ├─ Column definitions (including state in County)
│  ├─ Primary key structures
│  └─ Index presence
│
├─ TestConstraints (13 tests)
│  ├─ CHECK constraints (9 tests)
│  │  ├─ eventLength > 0, ohLine > 0, LENGTH(state) = 2
│  │  └─ sumAFWA_TOTPRECIP >= 0, peakWSPD10 > 0, wgt* >= 0, outage >= 0
│  ├─ UNIQUE constraint (eventDateTime)
│  ├─ Primary key uniqueness
│  └─ Foreign key integrity
│
└─ TestDataIntegrity (3 tests)

test_similarity_engine.py (27 tests for multi-dimensional L2 algorithm properties)
├─ TestComputeFeatureStats (mean, standard deviation, Bessel's correction)
├─ TestComputeL2ZscoreDistance (weighted Euclidean norm, constant features safety)
├─ TestDistanceToSimilarity (normalization mapping to (0, 1])
├─ TestSingleFeatureParity (parities validation against legacy 1-D math)
├─ TestTwoFeatureRankingDifference (multi-feature rankings updates validation)
├─ TestZeroStdDevSafety (handles constant historical fields without crashes)
└─ TestFeatureWeightOverride (validates custom weight configuration)

test_task_routing.py (7 routing classifier tests)
├─ Validates Route.DATA_RETRIEVAL, Route.WEATHER_ANALYTICS, Route.RAG_KNOWLEDGE
└─ Validates Route.SIMILARITY_SEARCH classification and ignores RAG collisions

test_wiser_cli.py (10 CLI registry and parsing tests)
├─ Validates natural language parameter extractor (wind=9.2, wgt5=12)
├─ Validates county detection and auto-resolution from text query strings
└─ Validates dispatcher handlers registry (ROUTE_HANDLERS) mapping

test_data_pipeline.py (1 pipeline test)
└─ Test eventCode parsing and state abbreviation extraction logic

test_database.py (1 initialization test)
test_similarity_algorithm.py (9 tests)
test_rag.py (22 tests)

Result: 106/106 ✓ PASSING
```

---

## Deployment Architecture

```
DEVELOPMENT
├─ In-memory SQLite (:memory:)
│  └─ Used in tests
├─ File-based SQLite (data/wiser.db)
│  └─ Used in scripts and CLI
└─ Schema: sql/schema.sql

PRODUCTION (Future)
├─ PostgreSQL (multi-user)
│  └─ Conversion notes in legacy code
├─ Proper authentication
│  └─ Environment variables for credentials
└─ Replication and backups
   └─ Transactional consistency
```

---

## Code Dependency Graph

```
Data Sources
    │
    ▼
data_pipeline.py (CSV → Normalized dicts)
    │
    ├─────────────────┬─────────────────┐
    │                 │                 │
    ▼                 ▼                 ▼
   CSV          bootstrap_wiser.py   write_normalized_csvs()
   write        (orchestrator)           │
               │                         ▼
               │                    data/normalized/
               │
               ├─ DataRetrievalProcessingAgent
               │  │
               │  ├─ load_raw_data()
               │  ├─ normalize()
               │  ├─ save_normalized()
               │  └─ build_database()
               │      │
               │      ▼
               │  database.py
               │  ├─ initialize_schema()
               │  ├─ load_normalized_rows()
               │  └─ get_table_counts()
               │      │
               │      ▼
               │  data/wiser.db
               │
               ├─ StormWeatherAnalyticsAgent
               │  │
               │  └─ *_from_db() query wrappers
               │      │
               │      ▼
               │  query_engine.py
               │
               └─ SimilarityQueryAgent
                  │
                  └─ find_similar()
                      │
                      ▼
                  similarity_engine.py (Z-score math)
                      │
                      ▼
                  data/wiser.db (READ)

User Interfaces
    │
    ├─ wiser_cli.py
    │  │
    │  ├─ TaskRoutingAgent (route classification)
    │  ├─ DataRetrievalProcessingAgent
    │  ├─ RAGKnowledgeAgent (rag.py index & search)
    │  ├─ SimilarityQueryAgent (find_similar)
    │  └─ StormWeatherAnalyticsAgent
    │      │
    │      ▼
    │  Console output
    │
    ├─ find_similar_storms.py
    │  │
    │  └─ SimilarityQueryAgent (find_similar)
    │
    └─ bootstrap_wiser.py
       │
       └─ File outputs + console summary
```

---

## Configuration & Environment

```
Required Files
├─ sql/schema.sql          ← Source of truth for DDL
├─ data/testdata.csv       ← Raw input data
└─ src/wiser/*.py          ← Core library

Configuration
├─ DEFAULT VALUES (hardcoded in scripts)
│  ├─ input: "data/testdata.csv"
│  ├─ output-dir: "data/normalized"
│  └─ db: "data/wiser.db"
│
└─ COMMAND-LINE OVERRIDES
   ├─ --input <path>
   ├─ --output-dir <path>
   ├─ --db <path>
   └─ --limit <n>
```
