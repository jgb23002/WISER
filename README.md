# WISER

![WISER Image](docs/WISER%20Image.png)

**W**eather **I**ntegrated **S**torm **E**vent **R**esponse — a Connecticut storm outage analytics system built on a multi-agent architecture. It ingests raw storm CSV data, normalizes it into a relational SQLite database, answers natural-language analytics queries, writes storm-scoped reports (summary, historical ranking, and storm-vs-storm or forecast-vs-history comparison), and retrieves grounded RAG context from project docs/code. Query it from the CLI or the Streamlit web interface — both are thin renderers over the same shared handler layer (`src/wiser/handlers.py`), so there is exactly one implementation of routing, dispatch, and query logic.

> Reference for future integration: [wisermaci.engr.uconn.edu](http://wisermaci.engr.uconn.edu/) — the UConn WISER site (not connected to this repo).

Documentation:

- [WISER documentation](docs/WISER_DOCUMENTATION.md) — architecture, agents, schema, similarity, RAG, integrations
- [Storm-scoped analysis plan](docs/STORM_SCOPED_ANALYSIS_PLAN.md) — the design record for the storm summary/ranking/comparison capabilities, with an as-built section recording where the build deviated from it
- [Storm reasoning test cases](docs/storm_reasoning_presentation.md) — worked reasoning examples with their evaluations

---

## Prerequisites

- Python **3.9+**
- Core workflows use only the Python standard library (`sqlite3`, `csv`, `datetime`, `argparse`, `unittest`). Optional dependencies: `faiss-cpu` and `numpy` for fast text similarity, `pypdf` for PDF extraction, `sqlglot` for full syntax validation of AI-generated SQL, and an `OPENAI_API_KEY` (in `.env` or the environment) for AI-generated SQL analytics queries and impact summaries. All fall back gracefully when absent.

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
> No packages are installed at this stage — the file documents optional packages for external-context acceleration (`faiss-cpu`, `numpy`, `pypdf`). Uncomment and install only what you need.

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
| RAG / knowledge retrieval | `rag`, `docs`, `explain`, `algorithm`, `reference`, `impact`, `hurricane`, `nhc`, `ncei` … | `RAGKnowledgeAgent` plus optional external web/PDF context |
| Weather analytics | `outage`, `storm`, `county`, `top`, `trend`, `affected`, `normalize`, `population` … | `SqlAiAgent` (LLM → validated read-only SELECT) plus `StormReasoningAgent`; falls back to fixed analytical query wrappers |
| Similarity search | `similar`, `find similar`, `rank storms`, or parameter patterns (e.g., `wind=`) … | `SimilarityQueryAgent` |
| Storm summary | `summary/summarize/describe/impact of` + a storm identifier … | `storm_analysis` + `storm_reports` (Capability 1) |
| Storm ranking | `rank`, `percentile`, `where does … rank` + a storm identifier … | `storm_analysis` + `storm_reports` (Capability 2) |
| Storm comparison | `compare`, `vs`, `difference between` + two storms, or a forecast (`wind=12ms`) … | `storm_analysis` + `storm_reports` (Capability 3, Mode A/B) |

```bash
# Top counties by total outage
python scripts/wiser_cli.py "top counties by outage" --limit 5

# Reason over county impact, exposure, and normalization
python scripts/wiser_cli.py "which county was affected the most overall?" --limit 8
python scripts/wiser_cli.py "normalize outages by population" --limit 8
python scripts/wiser_cli.py \
  "for storm 2020080406, why did Hartford have more outages than New Haven despite lower wind?"

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

# Storm-scoped reports: one storm, its place in the record, or two compared
python scripts/wiser_cli.py "storm summary for 2020080406"
python scripts/wiser_cli.py "where does storm 2020080406 rank out of all storms"
python scripts/wiser_cli.py "compare storm 2020080406 and 2011082800"

# Compare an inbound forecast against the historical record (Mode B)
python scripts/wiser_cli.py "compare an upcoming storm with wind=12ms to storm 2020080406"
```

The `--limit` flag controls how many rows are returned for top-N queries (default: 10).
For RAG queries, `--limit` controls retrieved context chunks. Use `--rebuild-rag` to refresh the local index, `--show-rag-context` to print the full augmented prompt, and repeat `--rag-source <path>` to index a custom file or directory.
For similarity searches, `--county` overrides county auto-detection from query text.

Weather-analytics queries are answered in two stages. First, the **SqlAiAgent** generates a SQLite `SELECT`, which is validated (syntax + SELECT-only, via `sqlglot` when installed) and executed against a read-only connection. Then the **StormReasoningAgent** receives only the user question, validated SQL, and returned rows, and explains the pattern with concrete row values and caveats. On validation or API failure, the CLI falls back to fixed analytical query wrappers for top counties, top storms, county history, affected-most summaries, exposure normalization, and county comparisons. Related flags: `--sql-model <id>` picks the OpenAI model for SQL generation (default: `WISER_OPENAI_MODEL` or `gpt-4o-mini`), `--show-sql` prints the generated SQL alongside results, `--show-reasoning-prompt` prints the reasoning prompt built from result rows, and `--dry-run-sql` prints the exact SQL-generation prompt without making an API call.

Population is not currently part of the WISER schema. If a user asks for per-capita normalization, WISER says that explicitly and shows the available infrastructure exposure normalization (`outage_per_million_ohline`) instead of inventing population values.

**Storm-scoped reports.** A query naming a specific storm — by 10-digit code (`2020080406`) or by date (`2020-08-04`) — routes to one of three report capabilities in `storm_analysis.py` and `storm_reports.py` rather than to ad-hoc SQL:

1. **Summary** — statewide and per-county impact for one storm.
2. **Ranking** — where that storm sits in the historical record, by percentile.
3. **Comparison** — *Mode A* compares two stored storms; *Mode B* compares an inbound forecast against its closest historical analogues, whose outages are recorded outcomes, never a prediction.

The database stores metric units (`m/s`, `mm`); reports convert to imperial (`mph`, `inches`) at the presentation boundary only, in `units.py`. Forecast inputs accept either system when the suffix is attached to the number (`wind=12ms`, `wind=27mph`, `precip=1.2in`) — a detached token would read `"in Fairfield"` as inches. Fields that need storm metadata WISER does not yet hold (storm type, peak snowfall, wind gusts) are rendered as explicitly unavailable rather than inferred.

**Conversational follow-ups.** `--interactive` (`-i`) starts a multi-turn session where analytics follow-ups reuse the conversation context — each successful turn's question, generated SQL, and answer are replayed to the LLM (last 5 turns), so *"what about Hartford?"* after *"top counties by outage"* resolves correctly. Follow-ups that match no routing keywords stick to the analytics route. Inside the session, `clear` resets the conversation and `exit`/`quit`/Ctrl-D leaves. The Streamlit app has the same behaviour through its chat interface.

```bash
python scripts/wiser_cli.py --interactive --show-sql
wiser> top counties by outage
wiser> what about only Hartford?    # follow-up: reuses conversation context
wiser> exit
```

Impact-style storm questions automatically crawl the default NOAA/NHC/NCEI context sources the first time (including linked National Hurricane Center Tropical Cyclone Report PDFs — roughly 60 downloads, so expect a minute or more). Later impact queries reuse the indexed chunks and answer in seconds; request a crawl (`--crawl-web-sources` / the Streamlit checkbox) or a RAG rebuild to refresh them. WISER stores extracted web/PDF paragraphs in the existing RAG tables, ranks them with optional Meta/Facebook FAISS similarity search when available, and falls back to a dependency-free exact text-vector ranking otherwise. See [docs/WISER_DOCUMENTATION.md](docs/WISER_DOCUMENTATION.md).

If `OPENAI_API_KEY` is set in `.env` or the shell environment, WISER uses the OpenAI Responses API to generate short cited impact summaries from the retrieved paragraphs, and OpenAI can also generate row-grounded analytics reasoning. Without a key, or with `--no-openai-summary`, it uses local extractive summaries and deterministic local analytical reasoning.

Fixture-backed EagleEye and National Grid CSV loading is available through a neutral staging table (`ExternalOutageObservation`). These loaders do not require OpenAI and do not perform live API calls:

```bash
# Load an EagleEye-style CSV into the external outage staging table
python3 scripts/wiser_cli.py \
  --load-external \
  --external-source eagleeye \
  --external-file tests/fixtures/eagleeye_sample.csv \
  --db data/wiser.db

# Load a National Grid-style CSV into the same staging table
python3 scripts/wiser_cli.py \
  --load-external \
  --external-source national-grid \
  --external-file tests/fixtures/national_grid_sample.csv \
  --db data/wiser.db
```

The parser preserves source provenance (`sourceSystem`, `sourceRecordId`, `rawPayload`) and normalizes timestamps to UTC. Live EagleEye/National Grid API pulls still require source credentials and endpoint/field documentation.

---

### Web interface (Streamlit)

The same agent system is available as a web UI. It consumes the identical
handler layer the CLI uses (`wiser.handlers`) — no duplicated routing,
dispatch, or SQL logic — and reads `OPENAI_API_KEY` from the same repo-root
`.env` / environment as the CLI.

```bash
pip install streamlit
streamlit run streamlit_app.py
```

- A no-install presentation mock is available at
  [`mock_interface/index.html`](mock_interface/index.html). It mirrors the
  implementation status panels, EagleEye/National Grid staging preview,
  agent demo queries, and architecture diagram for professor walkthroughs.
- The sidebar covers the common CLI options: database/CSV paths, result
  limit, county override, dry-run SQL, show SQL, show reasoning prompt, show
  RAG context, rebuild RAG, external crawling, OpenAI summary/reasoning
  toggle, and similarity backend.
  A few flags stay CLI-only: `--rag-source`, `--web-source`,
  `--max-linked-pdfs`, `--no-linked-pdfs`, `--openai-model`, and
  `--sql-model`. The OpenAI model comes from `WISER_OPENAI_MODEL` in the
  repo-root `.env`.
- Example-query buttons cover each route and are color-coded by the agent
  they route to; the submitted query and route decision are shown in the
  same color for every run.
- The top tabs expose presentation-ready status: routing/SQL ground-truth
  metrics, a county heat-map panel, EagleEye/National Grid sample staging,
  and recent staged external outage rows.
- County heat maps use the legacy 8-county Connecticut boundary layer in
  `data/geography/ct_counties_legacy.geojson`, matching the county names in
  the dataset rather than newer planning-region geography. The map can color
  counties by single-storm outages, outage totals, average/peak outage,
  average/maximum wind speed, or outages normalized by overhead-line exposure.
- A storm comparison draws both counties' maps side by side on **one shared
  colour scale**, so the same shade means the same number in each — scaled
  independently, the smaller storm would paint as dark as the larger one and
  invert the comparison. The ramp runs one hue light-to-dark, and because
  outage counts are heavily skewed it positions values by `value ** 0.4`, which
  buys a moderately smaller storm back some of its own contrast. It does not
  rescue every ratio: against a storm several hundred times larger, a quiet
  storm's counties all land in the first band and its map reads uniformly pale.
  That is the comparison rather than a missing layer — the caption above each
  map names that storm's own peak county and value. The SVG legend labels
  alternate band boundaries with their values —
  both ends always — and counties with no data are drawn as a hatch rather
  than another shade, so "we measured zero" and "we have no measurement" are
  never the same fill. Panels render at their own size with a matching frame,
  so nothing is scaled down and the labels arrive at the size they were set.
- Without an `OPENAI_API_KEY`, a banner explains that analytics fall back to
  fixed queries and impact summaries use the local extractive mode — the app
  keeps working, nothing crashes.

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

Use the ranked historical **outages** as a rough guide for what a current event with similar weather conditions might look like in that county. Algorithm details: [docs/WISER_DOCUMENTATION.md](docs/WISER_DOCUMENTATION.md).

---

## Running the Tests

To run the entire test suite covering database schema, RAG indexing/retrieval, external source parsing, OpenAI summarization, SQL AI agent validation, task routing, the shared handler layer, query parsing, and similarity algorithms:

```bash
python3 -m unittest discover -s tests -v
```

Expected output:
```
...
----------------------------------------------------------------------
Ran <n> tests in <time>s

OK
```

Ground-truth evaluation shortcuts:

```bash
python3 scripts/wiser_cli.py --evaluate-routing --evaluate-sql
```

Expected output:

```text
routing: 68/68 passed (100.0%)
sql: 27/27 passed (100.0%)
```

The test files cover the following functionality:

| Test File | Description |
|---|---|
| `tests/test_schema.py` | Validates database constraints (CHECK, UNIQUE, FK), composite primary keys, table structures, and column datatypes. |
| `tests/test_database.py` | Verifies database schema initialization and backwards compatibility with legacy structures. |
| `tests/test_rag.py` | Tests grounding, chunking, indexing, and answer retrieval inside the RAG engine. |
| `tests/test_external_sources.py` | Validates external web/PDF indexing, URL normalisation, HTML/PDF extraction, and text-vector ranking. |
| `tests/test_external_integrations.py` | Validates fixture-backed EagleEye/National Grid CSV parsers, external outage staging schema, timestamp normalization, and loading/upserts. |
| `tests/test_openai_summarizer.py` | Verifies OpenAI Responses API request construction, response parsing, and dotenv loading. |
| `tests/test_sql_ai_agent.py` | Verifies SQL AI agent schema loading, fence stripping, SELECT-only validation, dry-run mode, mocked LLM pipeline, and handler fallback rendering. All LLM calls are mocked. |
| `tests/test_sql_ai_ground_truth.py` | Runs the SQL AI ground-truth fixture against a deterministic SQLite seed DB and mocked `SqlAiAgent` responses. |
| `tests/test_storm_reasoning_agent.py` | Verifies row-grounded reasoning prompts, local fallback analysis, population caveats, and wind/outage comparison behavior. |
| `tests/test_reasoning_characterization.py` | Pins the exact output of every branch of the local reasoning fallback, so a refactor that changes wording has to say so. |
| `tests/test_units.py` | Metric/imperial conversion and forecast measurement parsing — pure functions, no DB or network. |
| `tests/test_storm_analysis.py` | Storm summary/ranking/comparison analysis, including Mode B forecast analogues. Each test seeds its own database and asserts on relationships, never on a magnitude from `data/wiser.db`. |
| `tests/test_storm_reports.py` | Report structure and field order for all four report kinds, with the Mode B labelling rules that keep an analogue's outage from reading as a forecast. |
| `tests/test_storm_handlers.py` | Storm-scoped handlers return structured dataclasses and never print. |
| `tests/test_code_review_regressions.py` | One test class per production fix shipped in `7fd6fe4`, so a reintroduced defect fails here rather than in a demo. |
| `tests/test_conversation_memory.py` | Multi-turn prompt construction, history trimming, sticky routing, and history threading through the analytics handler. |
| `tests/test_handlers.py` | Verifies the shared handler layer returns structured results (no printing): analytics LLM/fallback/dry-run/reasoning paths, similarity validation errors, RAG answers, dispatch registry. |
| `tests/test_streamlit_app.py` | Headless Streamlit smoke tests (AppTest): app loads, similarity and dry-run analytics render, errors surface without crashing. Skipped when `streamlit` is not installed. |
| `tests/test_similarity_algorithm.py` | Validates Z-score calculations, statistics, symmetry, and legacy single-dimension mathematical correctness. |
| `tests/test_similarity_engine.py` | Verifies multi-dimensional Z-score calculations (parity, weight overrides, zero std dev, partial features). |
| `tests/test_task_routing.py` | Tests keyword and pattern matching routing decisions (avoiding RAG collisions, rejecting unknown aliases). |
| `tests/test_task_routing_ground_truth.py` | Runs the JSON task-routing ground-truth corpus and route-priority regression cases. |
| `tests/test_wiser_cli.py` | Verifies similarity query parsing, county auto-detection, dispatch registry, and CLI rendering of handler results. |
| `tests/test_visualization.py` | Validates county metric aggregation, legacy county GeoJSON loading, and SVG heat-map rendering. |
| `tests/test_data_pipeline.py` | Validates parsing of CSV columns and correct extraction of state codes from event codes. |

---

## Project Structure

```text
WISER/
├── data/
│   ├── geography/
│   │   ├── README.md             # Source notes for the legacy CT county boundary asset
│   │   └── ct_counties_legacy.geojson
│   ├── normalized/               # Generated by bootstrap — storm.csv, county.csv, weather_data.csv
│   ├── wiser.db                  # Generated by bootstrap — the SQLite database
│   └── testdata.csv              # Raw input — 2,352 storm-county rows
├── docs/
│   ├── WISER_DOCUMENTATION.md    # Combined architecture, agents, schema, similarity, RAG, integrations
│   ├── STORM_SCOPED_ANALYSIS_PLAN.md  # Design record for the storm capabilities, plus as-built notes
│   ├── storm_reasoning_presentation.md # Worked reasoning test cases and their evaluations
│   ├── WISER Database Info.png   # Schema diagram
│   └── WISER Image.png           # README banner image
├── scripts/
│   ├── bootstrap_wiser.py        # Parses CSV → normalizes → builds DB
│   ├── find_similar_storms.py    # Storm similarity CLI (supports up to 6 weather features)
│   ├── smoke_sql_ai_agent.py     # Live OpenAI eval harness for SqlAiAgent (not a unit test)
│   └── wiser_cli.py              # CLI entry point (argparse + text rendering over wiser.handlers)
├── sql/
│   └── schema.sql                # DDL — source of truth for all three tables
├── src/
│   └── wiser/
│       ├── cli_helpers.py        # Shared CLI constants (FEATURE_ALIASES, FLAG_TO_FEATURE, validators)
│       ├── conversation.py       # Conversational memory: ConversationTurn + sticky-route helper
│       ├── data_pipeline.py      # CSV parsing & row normalization
│       ├── database.py           # Schema init, data loading, row counts
│       ├── external_sources.py   # External web/PDF crawling, indexing, and impact summarization
│       ├── evaluation.py         # Ground-truth fixture evaluation helpers
│       ├── integrations/         # EagleEye/National Grid CSV parser + outage staging helpers
│       ├── openai_summarizer.py  # OpenAI Responses API wrapper for impact summaries
│       ├── visualization.py      # County heat-map metrics and SVG rendering
│       ├── query_engine.py       # SQL queries (top counties, storms, history, county count)
│       ├── handlers.py           # Shared route handlers + result types (consumed by CLI and Streamlit)
│       ├── rag.py                # RAG document indexing + lexical retrieval
│       ├── similarity.py         # Text-vector ranking (exact + optional FAISS backend)
│       ├── similarity_engine.py  # Multi-dimensional L2 Z-score similarity calculations
│       ├── storm_analysis.py     # Storm summary/ranking/comparison analysis + forecast analogues
│       ├── storm_reports.py      # Renders those analyses as reports, tables, and map panels
│       ├── units.py              # Metric↔imperial conversion at the presentation boundary
│       └── agents/
│           ├── task_routing_agent.py              # Keyword → route classifier
│           ├── data_retrieval_processing_agent.py # Orchestrates pipeline + DB build
│           ├── rag_knowledge_agent.py             # Builds/searches local RAG index
│           ├── similarity_query_agent.py          # Performs similarity query & ranking
│           ├── sql_ai_agent.py                    # NL → validated SELECT → read-only exec (LLM)
│           ├── storm_reasoning_agent.py           # Row-grounded analytical reasoning
│           └── storm_weather_analytics_agent.py   # Wraps query engine for analytics (fallback)
├── tests/
│   ├── __init__.py
│   ├── fixtures/
│   │   ├── eagleeye_sample.csv
│   │   ├── national_grid_sample.csv
│   │   ├── sql_ai_ground_truth.json
│   │   └── task_routing_cases.json
│   ├── test_code_review_regressions.py # One class per production fix shipped in 7fd6fe4
│   ├── test_conversation_memory.py   # Multi-turn prompts, sticky routing, and REPL tests
│   ├── test_data_pipeline.py         # Data pipeline and state extraction tests
│   ├── test_database.py              # Database initialization compatibility tests
│   ├── test_external_sources.py      # External source indexing, parsing, and ranking tests
│   ├── test_external_integrations.py # EagleEye/National Grid fixture parser + staging tests
│   ├── test_openai_summarizer.py     # OpenAI summarizer request/response tests
│   ├── test_rag.py                   # RAG indexing/retrieval tests
│   ├── test_reasoning_characterization.py # Exact-output pins for the local reasoning fallback
│   ├── test_storm_analysis.py        # Storm summary/ranking/comparison + forecast analogue tests
│   ├── test_storm_handlers.py        # Storm-scoped handler structured-result tests
│   ├── test_storm_reports.py         # Report structure, field order, and Mode B labelling tests
│   ├── test_storm_reasoning_agent.py # Row-grounded reasoning prompt/fallback tests
│   ├── test_units.py                 # Metric/imperial conversion and forecast parsing tests
│   ├── test_handlers.py              # Shared handler layer structured-result tests
│   ├── test_schema.py                # 27 schema validation tests
│   ├── test_sql_ai_agent.py          # SQL AI agent validation, pipeline, and fallback tests
│   ├── test_sql_ai_ground_truth.py   # SQL AI semantic ground-truth tests
│   ├── test_streamlit_app.py         # Headless Streamlit smoke tests (skipped without streamlit)
│   ├── test_task_routing.py          # Route classification tests
│   ├── test_task_routing_ground_truth.py # Routing ground-truth fixture tests
│   ├── test_similarity_algorithm.py  # Legacy similarity algorithm correctness tests
│   ├── test_similarity_engine.py     # Multi-dimensional similarity engine tests
│   ├── test_visualization.py         # County heat-map aggregation/rendering tests
│   └── test_wiser_cli.py             # Similarity parsing and CLI rendering tests
├── mock_interface/
│   └── index.html                    # No-install presentation mock of the Streamlit UI
├── .env.example                      # Template for OPENAI_API_KEY / WISER_OPENAI_MODEL
├── requirements.txt
├── streamlit_app.py                  # Streamlit web interface (renders wiser.handlers results)
└── README.md
```

---

## How It Works

```text
User query
    |
    v
TaskRoutingAgent
    |
    |-- DATA_RETRIEVAL
    |   "build sqlite database from csv"
    |   DataRetrievalProcessingAgent rebuilds data/wiser.db from data/testdata.csv.
    |
    |-- WEATHER_ANALYTICS
    |   "top counties by outage"
    |   "how many storms happened between 2010 and 2015?"
    |   SqlAiAgent asks an LLM for a SQLite SELECT, validates it
    |   (syntax + SELECT-only, sqlglot when installed), and executes it
    |   on a read-only connection. On validation or API failure it falls
    |   back to StormWeatherAnalyticsAgent's fixed query wrappers in
    |   query_engine.py ("top storms", "top counties", county history).
    |
    |-- SIMILARITY_SEARCH
    |   "find storms similar to wind=9.2 precip=45 in Fairfield"
    |   SimilarityQueryAgent ranks historical storms with weighted L2 Z-score
    |   similarity over up to 6 weather features in similarity_engine.py.
    |
    |-- STORM_SUMMARY / STORM_RANKING / STORM_COMPARISON
    |   "storm summary for 2020080406"
    |   "where does storm 2020080406 rank out of all storms"
    |   "compare storm 2020080406 and 2011082800"
    |   "compare an upcoming storm with wind=12ms to storm 2020080406"
    |   A query naming a storm (10-digit code or date) is analysed in
    |   storm_analysis.py and rendered by storm_reports.py: impact summary,
    |   historical percentile, storm-vs-storm (Mode A), or forecast-vs-
    |   analogues (Mode B). Reports convert to imperial in units.py; fields
    |   needing metadata WISER lacks render as explicitly unavailable.
    |
    `-- RAG_KNOWLEDGE
        "explain the storm similarity algorithm"
        "impact of the new storm in Connecticut"
        RAGKnowledgeAgent indexes local .md/.py/.sql/.txt sources with rag.py.
        Impact-style questions can also crawl NOAA/NHC/NCEI web pages and
        linked PDFs via external_sources.py, rank text with FAISS when installed
        or an exact text-vector fallback, and summarize with OpenAI when an API
        key is configured or a local extractive fallback otherwise.

find_similar_storms.py bypasses TaskRoutingAgent and directly runs
SimilarityQueryAgent for --county + weather-flag similarity searches.

Handlers for every route live in src/wiser/handlers.py and return
structured result dataclasses. scripts/wiser_cli.py (text) and
streamlit_app.py (web) are interchangeable renderers over that one layer.
```

---

## Database Schema

Schema defined in `sql/schema.sql`. Three normalized core tables plus one external outage staging table:

| Table | Primary Key | Rows |
|---|---|---|
| `Storm` | `stormCode BIGINT` | 294 |
| `County` | `county VARCHAR(64)` | 8 |
| `WeatherData` | `(stormCode, county)` | 2,352 |
| `ExternalOutageObservation` | `id INTEGER` plus unique `(sourceSystem, sourceRecordId, observedAt)` | Optional external feed rows |

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
