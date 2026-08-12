# Storm-Scoped Analysis — Implementation Plan

Status: **steps 0.1–7 implemented and committed.** Phase 2 (§6) deliberately
not started. See §12 for what was built differently from this plan and why.

Scope: three storm-scoped capabilities (summary, ranking, two-storm comparison),
a Phase 2 metadata-enrichment design, and a portability constraint added
mid-review (must survive a move to PostgreSQL).

**Revision 2** — incorporates the stakeholder decisions on §7 and §8, and the
confirmed Capability 3 scope change (forecast vs historical, Mode A / Mode B).
Sections revised: §0.1 (new), §3.2, §4.1, §4.3, §4.4, §5, §7.1, §7.4, §8, §9,
§10. Estimate delta in §11.

---

## 0.1 BLOCKER — process constraint 1 is not currently satisfied

> **Resolved.** The six regression tests landed in `91cfdf9` and `e1b1f35`
> before step 1, as `tests/test_code_review_regressions.py`. The section below
> is left as written — it records the state of the repository when the plan was
> drafted, not today's. See §12.

The instruction was: *"The six code-review fixes must be committed with their
regression tests written first."*

Current state of the repository:

- Working tree is **clean**; the six fixes are committed as `7fd6fe4`
  ("Duplicate heat map render, Reasoning failure destroying valid SQL results,
  TypeError on SQL NULLs, Empty comparison treated as a hit, Routing misroutes,
  External outage counting").
- That commit touches **6 production files and 0 test files.**
- No test in the suite references any of the fixed internals —
  `DATA_ARTIFACT_KEYWORDS`, `_matches_word`, `_as_float`, or `rows_updated`
  return zero grep hits under `tests/`.
- The suite passes: 297 tests, OK (`python -m unittest discover -s tests`).
  But it passed *before* the fixes too, which is the point — it does not cover
  them.

So the "committed" half is done and the "with their regression tests" half is
not. All six fixes are currently unprotected: any of them can silently regress,
including the two this plan builds directly on top of (`task_routing_agent`'s
strict data-management check and `storm_reasoning_agent`'s `_as_float` NULL
tolerance).

Recommended pre-step, ahead of step 1 in §10 — six regression tests, one per
fix, committed before any storm-scoped work begins:

| Fix | Test | Rough size |
|---|---|---|
| Routing misroutes | whole-word `DATA_ARTIFACT_KEYWORDS` matching; "crawl" ≠ "raw"; "normalize outages using the database" stays analytics | ~35 |
| Reasoning failure destroying valid SQL results | reasoning raises → `AnalyticsResult` keeps `sql`/`rows`, gains a warning notice | ~30 |
| TypeError on SQL NULLs | `_as_float` / ordering keys survive `None` from a LEFT JOIN aggregate | ~25 |
| Empty comparison treated as a hit | absent storm code → `_reasoning_comparison_rows` returns falsy, does not short-circuit the fallback chain | ~30 |
| External outage counting | pure-duplicate batch → `rows_loaded == 0`, `rows_updated == n` | ~30 |
| Duplicate heat map render | Streamlit renders the heat map once per analytics result | ~20 |

~170 lines, one commit. This is a prerequisite, not part of the estimate delta
in §11.

Also note: **`pytest` is not installed in this environment.** The suite runs
under `python -m unittest discover -s tests`. New tests must be
`unittest`-compatible; do not introduce pytest fixtures or bare `assert`
parametrization.

---

## 0. What I verified against the live database first

Everything below is measured from `data/wiser.db`, not assumed. Several findings
change the design, so they lead.

| Check | Result | Consequence |
|---|---|---|
| `COUNT(*) FROM Storm` | **294** | Report 294, flag the stakeholder's 312. Open Q2. |
| `SELECT DISTINCT eventLength` | **`[48]` — every storm** | Duration is a constant. See §7.1. |
| Max storm total outage | **21,557** (`2020080406`) | Five figures, not six. Open Q1. |
| `SUM(outage)` Fairfield, all storms | 43,475 | Matches the brief. |
| Counties | 8, all `state='CT'` | Open Q4. |
| Dates mapping to >1 storm | **3** (`2021-09-01` → `2021090100`, `2021090112`; also `2022-03-31`, `2022-10-13`) | Date input needs real disambiguation, not a TODO. |
| `MAX(peakWSPD10)` statewide | 13.11 m/s (29.3 mph) | Wind range is narrow; ranking spreads are small. |
| Storms with any county `wgt13 > 0` | **86 of 294** | Comparison row "counties above 13 m/s" is 0 for 71% of storms. |
| SQLite version in env | 3.49.1 | Window functions available — ranking can use `RANK() OVER`. |

Router probe of realistic new phrasings against the **current** agent:

```
rag_knowledge      | summarize storm 2020080406
rag_knowledge      | describe storm 2020080406
rag_knowledge      | what was the impact of storm 2012102806
rag_knowledge      | how does storm 2020080406 rank
similarity_search  | rank storms by peak outages
weather_analytics  | compare storm 2020080406 and 2011082800
weather_analytics  | storm summary for 2020080406
```

Four of seven land on the wrong route today. `summarize`, `describe`, `impact`,
and `how does` are all `RAG_KEYWORDS`, and `rank storms` is a
`SIMILARITY_KEYWORDS` entry. **New storm intents must be evaluated before the
RAG and similarity blocks**, which is exactly the placement most likely to
regress existing fixtures — so the gate has to be narrow. See §3.

---

## 1. Architecture

Three new modules, deliberately separated so the formatting layer (which
Capability 3 says is provisional) can change without touching SQL, and so the
SQL layer has one Postgres migration surface.

```
src/wiser/units.py            NEW ~40   m/s→mph, mm→in. Single conversion home.
src/wiser/storm_analysis.py   NEW ~320  All storm-scoped SQL. Returns dataclasses.
src/wiser/storm_reports.py    NEW ~260  Dataclass → (report_text, structured rows).
```

Data flow, identical for all three capabilities:

```
router → Route.STORM_*  →  handlers.handle_storm_*
                             ├─ parse storm token(s) from ctx.query
                             ├─ storm_analysis.resolve_storm()        ← DB
                             ├─ storm_analysis.storm_summary/…()      ← DB
                             ├─ storm_reports.render_*()  → report text + rows
                             └─ _attach_reasoning()                   ← optional LLM
                           → AnalyticsResult(report=…, rows=…, notices=…)
```

**These handlers do not call `SqlAiAgent`.** The reports have a fixed field
order, fixed unit conversions, and mandatory caveats; an LLM writing the SQL
would make output non-reproducible for exactly the queries a stakeholder will
screenshot. Fixed SQL, deterministic rendering, LLM used only for the
interpretive layer on top.

### Result type: extend `AnalyticsResult`, don't add a new one

Add two fields:

```python
report: str | None = None          # the rendered report block
report_kind: str | None = None     # "storm_summary" | "storm_ranking" | "storm_comparison"
```

Rationale: a storm report needs `rows` (tables + heat maps), `reasoning`
(Capability 3 reuses the divergence logic), and `notices` (caveats, the 294-vs-312
flag, disambiguation) — that is `AnalyticsResult` already. A separate dataclass
would force new branches in both renderers, in `turn_from_result`, and in
`streamlit_app._heatmap_metric_for_result`, for no gain. Cost of this choice: a
reader of `AnalyticsResult` must now know two of its fields are only populated
on the storm routes. Acceptable; `dry_run_prompt` and `fallback_title` already
have that property.

---

## 2. Postgres portability (constraint added mid-review)

Full Postgres support is a larger project than these three capabilities. What I
am committing to here is: **new code is dialect-neutral, and the port surface is
one function.** Pre-existing blockers are listed in §2.3 as backlog, not
silently claimed as handled.

### 2.1 Rules the new code follows

1. **All storm SQL lives in `storm_analysis.py`.** No SQL in handlers or the
   report layer. One file to port.
2. **Every query goes through one funnel.**
   ```python
   def _fetch(conn, sql: str, params: Sequence[object] = ()) -> list[tuple]:
   ```
   `?` placeholders today; a psycopg port rewrites markers in this one function
   rather than at ~30 call sites. I am deliberately *not* building a dialect
   abstraction layer or pulling in SQLAlchemy — that is speculative work this
   change does not need, and it would be a much larger diff than the feature.
3. **No `strftime`.** Select raw `eventDateTime` / `stormCode` and do all date
   derivation and formatting in Python. (`stormCode` is an integer `YYYYMMDDHH`;
   integer arithmetic on it is portable, `SUBSTR` on it is not.)
4. **No `ROUND(<float>, n)` in SQL.** This is a genuine trap, not pedantry:
   Postgres has no `round(double precision, integer)` overload — it errors.
   `handlers.py`, `query_engine.py`, and `visualization.py` all do this today
   (§2.3). New code rounds in the presentation layer, which is also what the
   unit-conversion requirement asks for.
5. **Window functions for ranking**, `RANK() OVER (ORDER BY …)` and
   `COUNT(*) OVER ()`. Verified working on the env's SQLite 3.49.1; supported by
   Postgres since 8.4. This is the portable way to get "5th out of 294" in one
   pass, and it is faster than fetching 294 rows and ranking in Python.
6. **Row → dataclass by positional index**, never by dict key. Unquoted
   `stormCode` folds to `stormcode` in Postgres, so key-based access silently
   breaks on migration. `query_engine.py` and `visualization.py` already use
   positional access; new code matches them.
7. **Type against a `Protocol`, not `sqlite3.Connection`.** New modules declare
   `class DbConnection(Protocol): def execute(...)`. Zero runtime cost, and it
   stops the new code from inheriting the existing `sqlite3.Connection`
   annotations that a port has to unpick.
8. **Phase 2 DDL avoids `AUTOINCREMENT`** (SQLite-only keyword) by using a
   natural primary key. See §6.

### 2.2 One-line hardening worth doing now

`sql_ai_agent._validate_sql` hardcodes `dialect="sqlite"` in the `sqlglot` call.
Promote it to a module constant `SQL_DIALECT = "sqlite"`. ~3 lines, makes the
validator's dialect a single edit later.

### 2.3 Pre-existing Postgres blockers — backlog, NOT in this change

Listed so the estimate is honest. None are introduced by this work; all must be
resolved before a real migration.

| Blocker | Location |
|---|---|
| `ROUND(float, n)` — errors on Postgres | `handlers.py:519-527,546-550`, `query_engine.py:23`, `visualization.py:145-149` |
| `PRAGMA database_list` + `file:…?mode=ro` for read-only exec | `sql_ai_agent.py:403-421` |
| `strftime('%Y', …)` in the SQL-AI system prompt + 3 few-shot examples | `sql_ai_agent.py:46,99,123-127` |
| `sqlite_master` introspection | `database.py:30-37` |
| `AUTOINCREMENT`, `executescript`, `PRAGMA foreign_keys` | `schema.sql`, `database.py:24-26,51-54` |
| `sqlite3.Connection` annotations throughout | 9 modules |

The SQL-AI prompt one is the nastiest: the LLM is *taught* SQLite dialect, so a
port needs a prompt variant plus a re-run of `test_sql_ai_ground_truth.py`.

---

## 3. Routing

### 3.1 New intents

```python
class Route(str, Enum):
    ...
    STORM_SUMMARY    = "storm_summary"
    STORM_RANKING    = "storm_ranking"
    STORM_COMPARISON = "storm_comparison"
```

### 3.2 Keyword strategy — the gate is an identifier, not a verb

The controlling insight from the §0 probe: storm-scoped queries are recognisable
by **containing a resolvable storm identifier**, not by their verb. Verbs like
`summarize` / `describe` / `impact` / `rank` are already owned by RAG and
similarity, and widening those routes' losses on bare verbs would regress
`rag_005`, `rag_008`, and `similarity_002`.

Patterns:

```python
_STORM_CODE_PATTERN = re.compile(r"\b(\d{10})\b")            # YYYYMMDDHH
_STORM_DATE_PATTERN = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b") # ISO only, at routing time
```

ISO-only at the routing layer is intentional: `8/4/2020` and `August 4, 2020`
are handled by the *handler's* parser, where a parse failure can produce a
helpful notice. Loosening the *router's* pattern is what creates false positives.

Cue sets (all matched whole-word via the existing `_matches_word`):

```python
STORM_SUMMARY_CUES    = {"summary", "summarize", "summarise", "overview",
                         "report", "describe", "details", "breakdown",
                         "profile", "what happened", "tell me about"}
STORM_RANKING_CUES    = {"rank", "ranks", "ranked", "ranking", "percentile",
                         "how bad", "compare to other storms", "on record",
                         "historically", "out of"}
STORM_COMPARISON_CUES = {"compare", "comparison", "vs", "versus", "against",
                         "difference between", "differ"}
```

### 3.2.1 Resolving the gate 0 / Mode B conflict

Revision 1 had gate 0 skip **all** storm intents on any numeric-param match, so
similarity kept priority (§8.5). Mode B queries carry numeric parameters *and* a
comparison cue *and* often a storm identifier, so that gate made Mode B
unreachable. §8.5 and Mode B are in direct conflict and the boundary has to be
drawn somewhere.

**Proposed boundary — draw it on "is a specific database storm named?"**

| Query shape | Route | Why |
|---|---|---|
| Numeric params, no comparison cue | `similarity_search` | Unchanged. The existing behaviour. |
| Numeric params + `similar` phrasing | `similarity_search` | Unchanged, even with a storm code present. §8.5 holds. |
| Numeric params + comparison cue, **no** storm identifier | `similarity_search` | *This is the similarity search.* Comparing a forecast to "the record" is exactly what `find_similar` does. Presentation upgrade only — see below. |
| Numeric params + comparison cue + **storm identifier** | `STORM_COMPARISON` (Mode B) | Genuinely new analysis: forecast positioned against one named storm, not against a ranked neighbour list. |
| Two storm identifiers + comparison cue | `STORM_COMPARISON` (Mode A) | As originally specified. |

Justification: gate 0 yields **only** when a comparison cue and a storm
identifier are both present. Everything that routes to similarity today still
routes to similarity, because none of those queries name a specific storm code
alongside a comparison verb.

The third row is the important one, and it follows the reuse instruction. A
statewide "how does an upcoming 12 m/s storm compare to the record?" stays on
`Route.SIMILARITY_SEARCH`; what changes is that `handle_similarity_search` gains
an optional comparison-report rendering when a forecast/comparison cue is
present. **The routing fight collapses into a presentation choice**, exactly as
suggested. No new route, no parallel implementation, no fixture churn.

### 3.2.2 Decision order

Inserted **at the top of `route()`, before the RAG block**:

```
0.  if _NUMERIC_PARAM_PATTERN matches:
        if (comparison cue AND storm identifier):  fall through to rule 1  (Mode B)
        else:                                      skip all storm intents  (similarity)
0b. if "similar"/"similar to" in text  → skip all storm intents            (§8.5 confirmed)
1.  >=2 storm identifiers AND a comparison cue                 → STORM_COMPARISON (Mode A)
1b. 1 storm identifier AND a comparison cue AND numeric params → STORM_COMPARISON (Mode B)
2.  exactly 1 storm identifier AND a ranking cue               → STORM_RANKING
3.  exactly 1 storm identifier AND a summary cue               → STORM_SUMMARY
3b. exactly 1 storm identifier AND the word "storm"
        AND no other route cue                                 → STORM_SUMMARY
    (covers the bare "storm 2020080406")
4.  …existing RAG / similarity / data / analytics chain, unchanged
```

Ranking is checked before summary because "give me a summary of how storm X
ranks" is a ranking request; summary is the more general fallback.

Additional forecast cues, used by rule 1b and by the similarity-handler
presentation switch — `{"upcoming", "forecast", "forecasted", "forecast for",
"expected", "projected", "incoming", "predicted"}`. These are **not** sufficient
on their own to route anywhere new; they only select a renderer.

Reason strings will contain `"storm"` and the capability name, e.g.
`"Matched storm-scoped comparison intent (forecast parameters + storm identifier)."`

### 3.3 Regression argument for the protected fixtures

| Fixture | Query | Why it still passes |
|---|---|---|
| `data_001`–`data_007` | build/load/normalize/schema/ingest/retrieve/refresh | None contains a 10-digit run or an ISO date. Gate 1–3b cannot fire; the `_looks_like_data_management` path is untouched. `data_007`'s `data/testdata.csv` has no 10-digit run. |
| `analytics_011` | `top counties from database` | No storm identifier. |
| `analytics_015` | `normalize outages by population` | No storm identifier. |
| `analytics_006` | `How many storms happened between 2010 and 2015?` | `2010`/`2015` are 4-digit; `\b\d{10}\b` does not match. No storm cue either. |
| `rag_005` | `impact of the new storm in Connecticut` | No identifier → falls through to RAG unchanged. |
| `rag_008` | `explain top counties by outage` | No identifier. |
| `similarity_002` | `rank storms similar to wind=6.5` | Gate 0 (`wind=`, no storm identifier → does not yield) *and* gate 0b (`similar`) both short-circuit before the ranking cue is read. |
| `similarity_001,003–008` | numeric-param queries | Gate 0 does not yield: none carries a comparison cue **and** a storm identifier. |

The residual risk is a query carrying both a storm code and a documentation
verb — e.g. *"explain the methodology behind storm 2020080406"*. Under this
order that routes to `STORM_SUMMARY`, not RAG. I think that is the right call
(the user named a storm), but it is a behaviour change worth naming, and I will
add it as a fixture so the choice is explicit rather than accidental.

---

## 4. Capability specs

### 4.1 Shared: unit conversion (`units.py`)

```python
MS_TO_MPH    = 2.23694
MM_TO_INCHES = 1 / 25.4

# Output direction — DB (metric) → report (imperial)
def ms_to_mph(value: float | None) -> float | None
def mm_to_inches(value: float | None) -> float | None

# Input direction — forecast (imperial) → DB units, for Mode B
def mph_to_ms(value: float | None) -> float | None
def inches_to_mm(value: float | None) -> float | None

def format_mph(value, digits=1) -> str      # "24.3 mph"  | "not available"
def format_inches(value, digits=2) -> str   # "1.21 in"   | "not available"
def format_count(value) -> str              # "21,557"    | "not available"
```

The inverse pair is defined as the exact reciprocal (`value / MS_TO_MPH`,
`value * 25.4`) rather than a second hardcoded constant, so the round trip
cannot drift — one constant per dimension, per the "no scattered magic numbers"
requirement. Round-trip tests in §5.2 pin `mph_to_ms(ms_to_mph(x)) ≈ x`.

Mode B needs the input direction because forecasts arrive in mph and inches
while `WeatherData` and the similarity engine's Z-scores are m/s and mm.
Conversion happens once, at parse time in the handler, before anything reaches
`SimilarityQueryAgent` — the engine never sees imperial units.

Stored values stay metric; conversion happens only when `storm_reports` builds a
line. `None` renders as an explicit unavailability string — never `0`, never
omitted. The structured rows carry **both**: `peak_wind_ms` and `peak_wind_mph`,
so heat maps and tables (which are metric today, per
`visualization.COUNTY_HEATMAP_METRICS`) keep working while the report is
imperial.

### 4.2 Capability 1 — Storm Summary

`storm_analysis.storm_summary(conn, storm_code, *, county=None, state="CT")`

Two queries:
1. Storm header — `SELECT stormCode, eventDateTime, eventLength FROM Storm WHERE stormCode = ?`
2. Per-county rows joined to `County` (filtered by `c.state = ?`, and by county
   when scoped), returning outage, wind, precip, `wgt*`, `ohLine`, and the
   ohLine-normalized value.

Everything else — statewide total, argmax county for wind and precip, top-3
outage counties, normalized ranking — is computed in Python from those rows.
Eight counties; a second aggregate round-trip buys nothing and costs
portability.

Returns:

```python
@dataclass(frozen=True)
class StormSummary:
    storm_code: int
    event_datetime: str
    duration_hours: int
    scope: str                      # "statewide (CT)" | "Fairfield County"
    counties: tuple[CountyStormRow, ...]
    statewide_outage: int
    max_wind: CountyValue | None    # (county, value_ms)
    max_precip: CountyValue | None
    top_outage_counties: tuple[CountyValue, ...]      # 3
    top_normalized_counties: tuple[CountyValue, ...]  # 3
    # Phase 2 placeholders, always None until §6 lands:
    storm_type: None = None
    peak_snowfall_mm: None = None
```

Rendering (field order per brief; wording differs where §7 says the brief's
wording would be wrong):

```
Storm date:        2020-08-04
Storm type:        not available — requires external storm metadata (Phase 2)
Analysis window:   48 hours  (fixed model window, identical for all 294 storms)

Weather Conditions
  Maximum wind speed:  22.3 mph in New Haven County   [modeled, AFWA peakWSPD10]
  Maximum rainfall:    1.22 in in New Haven County    [modeled, AFWA]
  Peak snowfall:       not available — requires external storm metadata (Phase 2)

Storm Impact
  Statewide outages (trouble spots dispatched):  21,557
  Counties with the highest outages:
    Fairfield County — 5,935
    Hartford County — 4,792
    New Haven County — 2,958

Relative Impact
  After normalizing by overhead line length, Fairfield County experienced the
  greatest relative impact (1,214.25 per million ohLine), followed by Middlesex
  and Hartford.

Caveat: values are county-level and cover only fields present in the WISER
database. Wind and precipitation are AFWA model output, not station
observations. "Outages" counts trouble spots dispatched, not customer accounts.
```

Three deliberate deviations from the template, each justified in §7:
`Duration` → `Analysis window` (7.1); `Peak statewide outages` →
`Statewide outages` (7.2); `[modeled]` labels present from day one (7.3).

### 4.3 Capability 2 — Storm Ranking

`storm_analysis.storm_ranking(conn, storm_code, *, metrics=None, county=None, state="CT")`

One CTE per scope, window-function ranked:

```sql
WITH storm_state AS (
  SELECT w.stormCode,
         SUM(w.outage)              AS total_outage,
         MAX(w.peakWSPD10)          AS max_wind,
         MAX(w.sumAFWA_TOTPRECIP)   AS max_precip
  FROM WeatherData w
  JOIN County c ON LOWER(c.county) = LOWER(w.county)
  WHERE c.state = ?
  GROUP BY w.stormCode
),
ranked AS (
  SELECT stormCode,
         total_outage, max_wind, max_precip,
         RANK() OVER (ORDER BY total_outage DESC) AS rank_outage,
         RANK() OVER (ORDER BY max_wind    DESC) AS rank_wind,
         RANK() OVER (ORDER BY max_precip  DESC) AS rank_precip,
         COUNT(*) OVER ()                        AS total_storms
  FROM storm_state
)
SELECT * FROM ranked WHERE stormCode = ?;
```

County scope swaps the `WHERE c.state = ?` for `WHERE LOWER(w.county) = ?` and
drops the `SUM` to a plain per-county value. **Default is state level**, per the
brief: county rows are aggregated to state before ranking (SUM outage, MAX wind,
MAX precip). `state` is always a bound parameter defaulting to `'CT'`, never a
literal (§8.4).

**Supported ranking metrics: peak outages, max wind speed, max precipitation.**
Duration is **dropped** (§7.1 decision) — `eventLength` is constant, so a
duration rank carries no information. A query explicitly asking to rank by
duration gets a notice explaining that the field is a fixed model window, not a
storm property; it does not silently fall back to another metric.

Output:

```
The storm on 2020-08-04 ranks 1st out of 294 storms by statewide outages
and 34th by maximum wind speed, placing it in the top 1% of the historical
record.

Scope: statewide (CT, 8 counties). Ranked against all 294 storms in the WISER
database.

Caveat: "outages" counts trouble spots dispatched, not customer accounts.
Wind is AFWA model output. Storm counts and outage magnitudes reflect the
current development dataset and will change when the production dataset lands.
```

Every literal in that block is read from the DB at render time. Nothing about
the count is hardcoded, in the code or in the tests (§8.2).

Rank and total are always reported together, never rank alone. Ties use `RANK()`
(competition ranking) and the report says so when the tie group is >1.

### 4.4 Capability 3 — Storm Comparison (Mode A: historical / Mode B: forecast)

#### 4.4.0 Honest assessment: how much of Mode B is actually new?

Asked for directly, so stated directly: **Mode B is mostly the existing
similarity path with a different renderer.** I read
`SimilarityQueryAgent.find_similar`, `similarity_engine.rank_by_similarity`, and
`handlers.parse_similarity_params` before writing this.

What already exists and will be reused verbatim:

| Mode B needs | Already exists | Location |
|---|---|---|
| Parse `wind=12, precip=30` from NL | `parse_similarity_params` + `FEATURE_ALIASES` | `handlers.py:87`, `cli_helpers.py:3` |
| County extraction from NL | `parse_similarity_county` | `handlers.py:114` |
| Rank history against a parameter vector | `find_similar` → `rank_by_similarity` | `similarity_query_agent.py:62` |
| Outage of each analogue storm | `find_similar` returns `outage` per row | `similarity_query_agent.py:142` |
| Missing-param / missing-county error notices | `handle_similarity_search` | `handlers.py:591` |
| Wind-must-be-positive validation | `validate_wind_positive` | `cli_helpers.py:29` |

That is the whole retrieval half. **No parallel implementation will be built.**

What is genuinely new, and it is only three things:

1. **A comparison-report renderer** over similarity rows (`storm_reports.py`).
2. **Imperial→metric input conversion** (§4.1), because forecasts are mph/inches
   and the engine's Z-scores are m/s/mm.
3. **Statewide scope**, which does not exist today — see the constraint below.

**Constraint found while reading the code, and it is not cosmetic:**
`_fetch_historical` is county-scoped and `find_similar` takes `county` as a
*required* positional argument. There is no statewide similarity path. Worse,
`compute_feature_stats` computes Z-score mean/std **over the fetched county's
history only**, so similarity scores from two different counties are computed in
two different normalized spaces and are **not comparable to each other**.

Therefore a statewide Mode B cannot be implemented by averaging or merging eight
county runs — that would be arithmetic on incommensurable scores. Two honest
options:

- **(a) Recommended.** Statewide Mode B runs `find_similar` per county and
  reports the analogues **per county**, with no cross-county similarity ranking
  and an explicit note that scores are within-county. Zero engine changes.
- **(b)** Add a statewide `_fetch_historical` variant that aggregates to storm
  level first and computes one Z-score space over 294 storm-level rows. Cleaner
  output, but it is a real change to the similarity engine's contract and would
  need its own parity tests against `test_similarity_algorithm.py`.

Recommending (a) for this change and filing (b) as a follow-up. (a) is honest
about what the engine can support and touches no existing code.

#### 4.4.1 Mode A — DB storm vs DB storm

`storm_analysis.compare_storms(conn, code_a, code_b, *, state="CT")` — internally
two `storm_summary` calls plus one `storm_ranking` call per storm, so there is no
third SQL path to maintain.

The "where they differ most" section **reuses** the reasoning agent's divergence
logic rather than duplicating it. That logic is currently private
(`_lower_wind_higher_outage_observation`, plus the normalized-flip branch inside
`reason_locally`). Plan: extract it into a public, side-effect-free helper in
`storm_reasoning_agent.py` —

```python
@dataclass(frozen=True)
class RankingDivergence:
    raw_leader: dict; normalized_leader: dict
    raw_metric: str; normalized_metric: str
    flipped: bool

def normalized_ranking_divergence(rows) -> RankingDivergence | None
```

— and have both `reason_locally` and `storm_reports.render_storm_comparison`
call it. This is a behaviour-preserving refactor; §5 covers the characterization
test that proves it.

Geographic concentration is computed directly: top county's share of the storm's
statewide outage total, plus the Herfindahl-style concentration across all 8
counties, so "put X% in Fairfield" vs "more evenly distributed" is a measured
claim, not an LLM adjective.

The comparison table renders as a fixed-width text block **and** as structured
rows, so the Streamlit table and the CLI both work. The formatting function is
isolated in `storm_reports.py` precisely because this spec is unapproved.

Per §7.4, the threshold row is **`wgt9`** ("counties with 9+ m/s hours"), not
`wgt13` — only 86 of 294 storms have any county above 13 m/s, so the `wgt13` row
would read `0 0 +0` for 71% of comparisons.

#### 4.4.2 Mode B — forecast storm vs the historical record

`storm_analysis.compare_forecast(conn, params, *, county=None, storm_code=None,
state="CT", limit=5)`

There is **no outage value on the forecast side.** Outage is the outcome; a
forecast gives wind and precipitation only. So "Statewide outages" cannot be a
symmetric table row, and rendering it blank would be worse than not rendering it.

Resolution, per the recommendation: the report has two parts.

**Part 1 — where the forecast sits in the record.** Percentile position of the
forecast's wind and precip against the historical distribution. This is a real,
computable, symmetric comparison; it uses the ranking CTE from §4.3 with the
forecast value substituted for a storm's value.

**Part 2 — closest historical analogues and what they produced.** The top-N
similar storms from `find_similar`, each with its actual outage, explicitly
labeled as historical outcomes.

```
Forecast Comparison: upcoming storm vs historical record — Fairfield County

Forecast input (projection, not a record):
  Wind:          27.0 mph  (12.1 m/s)
  Precipitation:  1.18 in  (30.0 mm)

Position in the record
  Wind:          would rank 12th of 294 storms  (top 4%)
  Precipitation: would rank 88th of 294 storms  (top 30%)

Closest historical analogues in Fairfield County
  Storm         Date         Wind      Precip    Outages (actual)
  2020080406    2020-08-04   21.7 mph  1.21 in   5,935
  2011082800    2011-08-28   20.9 mph  1.09 in   4,102
  …

These are outages recorded for past storms with similar modeled conditions.
They are NOT a prediction for the forecast storm.

Caveat: the forecast column is a projection supplied by the user, not a database
record. Similarity is computed within Fairfield County's history only. Outage
figures are trouble spots dispatched, not customer accounts.
```

Every forecast-side value is labeled `(projection)` at the column header and the
analogue outages carry `(actual)` — the two must never appear in an unlabeled
column together, the same discipline §7.3 applies to modeled vs observed.

**Mode B input completeness policy.** Partial input produces a notice, never a
half-report:

| Input | Behaviour |
|---|---|
| wind only | Supported. The engine guarantees 1-D parity with the legacy wind-only algorithm. |
| precip only | Supported. Notice: single-feature match, wind not constrained. |
| wind + precip | Supported. The expected case. |
| any `wgt*` only | Supported, with a notice that threshold-hour-only matching is weak. |
| no numeric params | Not a Mode B query. Error notice reusing `handle_similarity_search`'s existing wording. |
| wind ≤ 0 | Rejected by existing `validate_wind_positive`. |
| county named | County scope. |
| no county | Statewide → per-county analogues per option (a), with the within-county-scores note. |

---

## 5. Test plan

All new tests use in-memory SQLite seeded from `sql/schema.sql`, matching the
existing `tests/test_handlers.py::_make_conn` pattern. No network.
**`unittest` only — pytest is not installed in this environment** (§0.1).

**Dataset-independence rule (§8.2).** No test hardcodes a storm count, a storm
code, an outage magnitude, or a county total from `data/wiser.db`. Every test
seeds its own fixture DB and asserts on *relationships*:

- rank/total consistency — `1 <= rank <= total`, `total == COUNT(DISTINCT stormCode)`
  read from the same connection
- ordering — the storm with the larger seeded metric ranks strictly better
- tie semantics — equal metrics produce equal `RANK()`, and the next distinct
  value skips accordingly
- aggregation identity — statewide value equals the SUM/MAX of its seeded county rows
- percentile arithmetic — derived from the returned `rank`/`total`, not a literal

The only literal counts permitted are those the test itself inserted. This is
what lets the production dataset land without re-baselining, per §8.2.

### 5.1 Routing — `tests/fixtures/task_routing_cases.json` (+16 cases)

Written in the same change as the routing code, per constraint 2.

| ID | Query | Expected | Guards |
|---|---|---|---|
| `storm_summary_001` | `storm summary for 2020080406` | `storm_summary` | happy path, code |
| `storm_summary_002` | `summarize storm 2020080406` | `storm_summary` | **wins over RAG `summarize`** |
| `storm_summary_003` | `describe storm 2020080406` | `storm_summary` | **wins over RAG `describe`** |
| `storm_summary_004` | `what was the impact of storm 2012102806` | `storm_summary` | **wins over RAG `impact`** |
| `storm_summary_005` | `tell me about the storm on 2020-08-04` | `storm_summary` | ISO date input |
| `storm_summary_006` | `storm 2020080406` | `storm_summary` | bare identifier (gate 3b) |
| `storm_summary_007` | `storm summary for Hartford in 2020080406` | `storm_summary` | county-scoped |
| `storm_rank_001` | `how does storm 2020080406 rank` | `storm_ranking` | **wins over RAG `how does`** |
| `storm_rank_002` | `where does storm 2020080406 rank out of all storms` | `storm_ranking` | |
| `storm_rank_003` | `what percentile is storm 2011082800 for wind` | `storm_ranking` | metric selection |
| `storm_rank_004` | `how does storm 2020080406 rank in Fairfield` | `storm_ranking` | county scope |
| `storm_compare_001` | `compare storm 2020080406 and 2011082800` | `storm_comparison` | |
| `storm_compare_002` | `storm 2020080406 vs storm 2012102806` | `storm_comparison` | |
| `storm_compare_003` | `difference between 2020-08-04 and 2011-08-28 storms` | `storm_comparison` | ISO dates |
| `routing_guard_001` | `find storms similar to 2020080406 wind=9.2` | `similarity_search` | **gate 0 + 0b hold** |
| `routing_guard_002` | `explain the methodology behind storm 2020080406` | `storm_summary` | documents the §3.3 behaviour change |

Mode B / gate-0-boundary fixtures (+6), pinning **both** sides of §3.2.1:

| ID | Query | Expected | Pins |
|---|---|---|---|
| `storm_forecast_001` | `compare an upcoming storm with wind=12 to storm 2020080406` | `storm_comparison` | **gate 0 yields**: cue + identifier + params |
| `storm_forecast_002` | `how would a forecast storm with wind=12, precip=30 compare to 2011082800 in Fairfield` | `storm_comparison` | Mode B, county-scoped |
| `storm_forecast_003` | `how does an upcoming storm with wind=12 compare to the historical record` | `similarity_search` | **no identifier → stays similarity**; presentation-only upgrade |
| `storm_forecast_004` | `find storms similar to wind=12 like storm 2020080406` | `similarity_search` | gate 0b: `similar` wins even with an identifier present |
| `storm_forecast_005` | `compare storms similar to wind=9.2` | `similarity_search` | cue but no identifier → gate 0 does not yield |
| `storm_forecast_006` | `compare storm 2020080406 and 2011082800` | `storm_comparison` | Mode A unaffected by the new gate |

`test_task_routing_ground_truth.py` asserts `len(cases) >= 40`; raise to `>= 64`
so the fixture cannot silently shrink.

Also add to `tests/test_task_routing.py`: an explicit non-regression test
asserting all seven `data_*` fixtures plus `analytics_011` / `analytics_015`
still route correctly **with a 10-digit number appended** — the adversarial
version of the §3.3 table.

### 5.2 `tests/test_units.py` (NEW, ~60 lines)

- `ms_to_mph(13.11)` ≈ 29.32; `mm_to_inches(231.98)` ≈ 9.13.
- `None` in → `None` out **in all four directions**; formatters return the
  unavailability string, never `"0"`.
- **Round-trip**: `mph_to_ms(ms_to_mph(x))` ≈ `x` and `inches_to_mm(mm_to_inches(x))`
  ≈ `x` across a range of values, to `float` tolerance.
- `mph_to_ms(0)` is `0.0`, not `None` — zero is a value, absence is not.
- Rounding is half-even and stable at the report's digit counts.

### 5.3 `tests/test_storm_analysis.py` (NEW, ~220 lines)

- `resolve_storm`: code hit; ISO-date hit; **date matching two storms returns an
  ambiguity object listing both** (seeded from the real `2021-09-01` case); miss
  returns `None`.
- `storm_summary`: statewide total = sum of county rows; max-wind and max-precip
  argmax pick the right county including on ties (deterministic tiebreak by
  county name); county-scoped call filters correctly; `storm_type` and
  `peak_snowfall_mm` are `None`.
- `storm_ranking`: rank + total on a seeded 5-storm set, asserted by the
  dataset-independence rule above; verifies the county→state aggregation
  (SUM outage / MAX wind / MAX precip) explicitly; ties produce equal `RANK()`
  and a flagged tie group; county scope ranks within that county only;
  **`state` parameter is honoured** — seed two states, assert a CT-scoped rank
  ignores the other state's storms (§8.4).
- **Duration ranking is rejected**, not silently substituted (§7.1): requesting
  it returns a notice and no rank.
- `compare_storms` (Mode A): difference signs and percentages; `wgt9 > 0` county
  counts (§7.4); concentration share.
- `compare_forecast` (Mode B): forecast percentile position against a seeded
  distribution; analogue rows carry real `outage` values; **county-scoped call
  delegates to `SimilarityQueryAgent` rather than reimplementing ranking** —
  asserted by patching the agent and checking it was called; statewide call
  produces per-county analogue groups and never a cross-county similarity
  ordering (§4.4.0 option (a)).
- Mode B partial input: each row of the §4.4.2 policy table gets a case;
  no-params and `wind <= 0` produce notices, not exceptions or half-reports.
- **Portability guard**: a test that greps the module source for `strftime`,
  `ROUND(`, `PRAGMA`, `AUTOINCREMENT`, and `sqlite_master` and fails on a hit.
  Crude, but it is the only thing that will actually stop rule §2.1 from eroding.

### 5.4 `tests/test_storm_reports.py` (NEW, ~180 lines)

- Field **order** and section presence for each of the four reports — summary,
  ranking, Mode A comparison, Mode B forecast comparison (asserting order and
  labels, not exact punctuation, per the brief).
- Phase 2 fields render the explicit unavailability string — and a test that the
  rendered text contains no fabricated storm type, snowfall, or gust value.
- Imperial values appear in the report; metric values appear in the rows.
- Rank lines always carry both rank and total.
- Every report ends with a caveat line.
- **Mode B labeling, the highest-risk assertion in this file**: the forecast
  column is labeled a projection; analogue outages are labeled actual/historical;
  the rendered text contains the explicit "NOT a prediction" disclaimer; and no
  outage number appears in the forecast column under any input.
- Mode B shows both imperial and metric for each forecast input (`27.0 mph
  (12.1 m/s)`), proving the conversion is visible rather than silent.

### 5.5 `tests/test_storm_reasoning_agent.py` (MODIFY, +~60 lines)

- **Characterization test first**: pin current `reason_locally` output on the
  divergence fixture, *then* extract `normalized_ranking_divergence`, then assert
  output is byte-identical. This is what makes the refactor safe.
- Direct unit tests for the extracted helper: flip detected, no-flip detected,
  missing-column returns `None`.

### 5.6 `tests/test_handlers.py` (MODIFY, +~140 lines)

- Each `handle_storm_*` returns an `AnalyticsResult` with `report`, `rows`,
  `report_kind` set, and produces no stdout (`_assert_no_stdout`).
- Unknown storm code → empty rows + an error notice, **not** an exception.
- Ambiguous date → notice listing both codes, no report, no arbitrary pick.
- Comparison with only one resolvable storm → notice, no half-report.
- **Mode A / Mode B selection**: same handler, branch chosen by whether forecast
  params are present; a Mode B query with an unresolvable storm code degrades to
  the statewide forecast report plus a notice, rather than erroring out.
- **`handle_similarity_search` presentation switch**: with a forecast/comparison
  cue the result carries a `report`; without one the result is
  **byte-identical to today's** — the direct test of §3.2.1's claim that this is
  presentation-only.
- Forecast unit parsing: `wind=27mph` and `wind=12` resolve per the §8.7
  decision, whichever way it goes; the test pins it explicitly.
- `turn_from_result` returns `None` for storm reports (no LLM SQL) — asserted so
  the memory behaviour is intentional, not incidental (and see §8.6: follow-up
  memory is deferred, so this is the current contract, not a bug).
- Existing analytics tests must pass unmodified. That is the constraint-3 check.

### 5.7 Renderer smoke tests

`tests/test_wiser_cli.py` and `tests/test_streamlit_app.py`: a result carrying
`report` prints/renders the block; a result without one is byte-identical to
today's output.

---

## 6. Phase 2 — storm metadata enrichment (design only)

Three fields absent from the schema: **storm type**, **peak snowfall**, **wind
gust**. New table, following the `ExternalOutageObservation` pattern — external
data with provenance, never written into `WeatherData`.

### 6.1 Schema sketch

```sql
CREATE TABLE StormMetadataObservation (
    sourceSystem    TEXT NOT NULL,        -- 'NCEI_STORM_EVENTS' | 'GHCN_DAILY'
    sourceRecordId  TEXT NOT NULL,        -- NCEI EVENT_ID | 'GHCN:<station>:<date>'
    stormCode       BIGINT,               -- resolved WISER storm; NULL if unmatched
    county          VARCHAR(64),          -- NULL = statewide / unassigned
    state           VARCHAR(2) NOT NULL DEFAULT 'CT',
    observedDate    DATE NOT NULL,
    stormType       TEXT,                 -- NCEI EVENT_TYPE, verbatim, never normalized
    stormName       TEXT,                 -- named storms
    snowfallMm      REAL,                 -- GHCN SNOW, converted at ingest
    windGustMs      REAL,                 -- GHCN WSFG, converted at ingest
    measurementKind TEXT NOT NULL,        -- 'observed' | 'modeled'
    stationId       TEXT,                 -- GHCN station, NULL for NCEI
    stationCount    INTEGER,              -- stations behind a county aggregate
    sourceUrl       TEXT,
    ingestedAt      TIMESTAMP NOT NULL,
    rawPayload      TEXT NOT NULL,
    PRIMARY KEY (sourceSystem, sourceRecordId),
    FOREIGN KEY (stormCode) REFERENCES Storm(stormCode),
    CHECK (measurementKind IN ('observed', 'modeled')),
    CHECK (snowfallMm  IS NULL OR snowfallMm  >= 0),
    CHECK (windGustMs  IS NULL OR windGustMs  >= 0),
    CHECK (LENGTH(state) = 2)
);

CREATE INDEX idx_storm_metadata_storm  ON StormMetadataObservation(stormCode);
CREATE INDEX idx_storm_metadata_county ON StormMetadataObservation(county, observedDate);
```

Three notes on this DDL:
- **Natural primary key**, no `AUTOINCREMENT` — Postgres-portable as written.
  `ExternalOutageObservation` uses `INTEGER PRIMARY KEY AUTOINCREMENT`, which
  would need rewriting on a port; the new table should not repeat that.
- `measurementKind` is `NOT NULL` and CHECK-constrained. This is the mechanism
  that enforces the brief's labeling rule structurally rather than by convention:
  `peakWSPD10` is `modeled`, an NCEI gust is `observed`, and the report layer
  refuses to place them in the same column without a label.
- `stormCode` is nullable. External sources will produce records that do not
  match a WISER storm; dropping them loses provenance, and forcing a match
  invents one.

### 6.2 Source recommendation

**NCEI Storm Events Database first**, GHCN-Daily second.

NCEI is a direct grain match — it is already county-level (FIPS), date-scoped,
and carries `EVENT_TYPE` and named storms. It delivers the highest-value missing
field (storm type) with no aggregation step and therefore no aggregation
judgment call. Bulk CSVs by year, stable schema, no API key.

GHCN-Daily supplies `SNOW` and `WSFG` but is **station-level**, so it needs a
station→county mapping and an explicit, documented aggregation rule. Recommend
MAX across in-county stations for both, with `stationCount` stored alongside so
a value derived from one station is visibly weaker than one from twelve. Without
that column the aggregate silently overstates confidence.

Both are batch-ingested by `scripts/ingest_storm_metadata.py`, never
query-time web search — reproducibility and attribution, per the brief.

### 6.3 Matching strategy (the part most likely to go wrong)

WISER storms are `YYYYMMDDHH` with a fixed 48-hour window; NCEI events have
begin/end datetimes. Proposed rule: an NCEI event matches a storm when its
`BEGIN_DATE_TIME` falls inside `[eventDateTime, eventDateTime + 48h)` **and**
the county matches. One event can match two storms (the 3 same-date storm pairs
in §0 guarantee this), and one storm can match many events. Store all matches;
resolve to a single displayed `stormType` at query time with a documented
precedence, and surface the count when >1 type matched. Do not pick silently.

Until this lands, Capability 1 renders those three fields as explicitly
unavailable. No inference, ever — the same discipline `sql_ai_agent`'s prompt
already applies to population.

---

## 7. Where I think the brief is wrong

Stated plainly, as asked, rather than worked around.

### 7.1 "Duration" is not a duration — and duration ranking is meaningless

`SELECT DISTINCT eventLength FROM Storm` returns exactly `[48]`. All 294 storms.
`eventLength` is the fixed AFWA model analysis window, not how long the storm
lasted.

Consequences:
- Capability 1's `Duration: XX hours` would print `48 hours` every single time.
- Capability 2's duration ranking is a 294-way tie. Every storm is "1st out of
  294", which is worse than useless — it looks like a working feature.
- Capability 3's `Duration  XX hours  XX hours  +X hours` row is always `+0`.

**DECIDED.** Relabel to `Analysis window: 48 hours (fixed model window)` in
Capability 1, and **drop duration from the ranking metrics entirely** — not
implemented with a forced-tie notice. The original "support duration ranking"
constraint predated the `eventLength = 48` finding. A request to rank by
duration returns a notice explaining the field is a fixed model window; it does
not silently substitute another metric.

If real storm duration is wanted later it is a Phase 2 field (NCEI carries
begin/end datetimes), not something the current schema can produce.

### 7.2 "Peak statewide outages" is a total, not a peak

`WeatherData` has one row per `(stormCode, county)` — there is no time series.
`SUM(outage)` over counties is the storm's total; there is no intra-storm
maximum to take. Calling it "peak" implies a time-resolved series we do not
have, and a stakeholder reading "peak" will reasonably assume "at the worst
moment". Recommend `Statewide outages (trouble spots dispatched)`.

The same wording issue appears in Capability 2's `peak statewide outages` and
Capability 3's table row. I will keep the metric identical and change only the
label, so nothing downstream shifts.

### 7.3 Modeled-vs-observed labeling should start now, not when gusts land

The brief defers the modeled/observed distinction until NCEI gust data arrives.
But `peakWSPD10` and `sumAFWA_TOTPRECIP` are *already* AFWA model output being
reported to a stakeholder as "Maximum wind speed" and "Maximum rainfall" —
phrasings that read as observations. Labeling them `[modeled, AFWA]` from day
one costs one string per line, and it means Phase 2 adds a value next to an
existing label rather than retrofitting labels onto values people have already
been reading unlabeled for months. Cheaper now than later.

### 7.4 Capability 3's "counties above 13 m/s" is near-empty for most storms

Only 86 of 294 storms have any county with `wgt13 > 0`. For the other 208 the
row reads `0  0  +0`. Correct, but it will look broken.

**DECIDED: switch the comparison row to `wgt9`** — a threshold with real spread
in this dataset. The row label becomes "Counties with 9+ m/s wind hours".

Note also: the correct implementation is `wgt13 > 0` (hours the wind exceeded
13 m/s), **not** `peakWSPD10 > 13`. Statewide max `peakWSPD10` is 13.11 m/s, so
the naive reading would return ≤1 county across the entire database.

---

## 8. Open questions — resolved

1. **`outage` semantics — RESOLVED, no action.** The development dataset is
   obfuscated. The stakeholder's six-figure sample reflects the real data, which
   will map onto this schema when it arrives. Do not rescale, do not relabel, do
   not treat the magnitude difference as a defect. The `[trouble spots
   dispatched]` label stays because it is accurate about what the column means.

2. **294 vs 312 storms — RESOLVED, no action.** Same cause. Report the real
   count from the DB. **Testing implication, carried into §5**: no test
   hardcodes a storm count, storm code, or outage magnitude from the live DB;
   ranking tests assert rank/total consistency and ordering only, so the
   production dataset lands without re-baselining. The ranking notice is
   reworded to state plainly that counts reflect the current development
   dataset — the "does not match, root cause unknown" framing is dropped.

3. **Town-level vs county-level — CONFIRMED and preferred.** Town-level phrasing
   gets an explicit "county-level only" notice plus the containing county. No
   town-shaped approximation.

4. **"Statewide" scope — STATE-AWARE NOW.** `state` flows as a bound parameter
   defaulting to `'CT'`, never a literal, throughout `storm_analysis.py` and
   both ranking CTEs. Choose the more scalable option wherever the question
   recurs. §5.3 has a two-state seed test proving the parameter is honoured
   rather than decorative.

5. **"similar to <storm code>" — CONFIRMED: similarity keeps priority.** Gate 0b
   preserves existing behaviour. But this decision collided with Mode B, and the
   boundary is redrawn in **§3.2.1**: gate 0 yields *only* when a comparison cue
   and a storm identifier are both present. Everything that routes to similarity
   today still does.

6. **Storm-scoped follow-up memory — DEFERRED, do not build.** Out of scope for
   this change.

   Reason to record, because it is a design argument and not just a scheduling
   one: the entire routing-safety case in §3.3 rests on storm intents requiring
   an explicit identifier in the query text. A session-backed path would route
   storm intents **without** one, reintroducing precisely the false-positive
   surface the identifier gate exists to prevent — every bare "how bad was it?"
   becomes a candidate storm query.

   Revisit as its own change, with its own rules: an active `last_storm_code`
   **and** a storm cue, evaluated **below** the data-management and similarity
   gates, plus an expiry rule so a stale storm code cannot silently capture an
   unrelated question ten turns later.

   **File as a follow-up issue** so it is not discovered as a gap during a demo.

7. **NEW — forecast parameter units are ambiguous (Mode B).** `FEATURE_ALIASES`
   maps `wind` → `peakWSPD10`, which is **m/s**. But forecasts are quoted in
   mph, and a user typing `wind=12` for an "upcoming storm" plausibly means
   either 12 m/s (26.8 mph, a severe CT storm) or 12 mph (5.4 m/s, a breeze).
   The two readings differ by 5×, and both are in-range for this dataset, so
   there is no safe guess.

   Options: (a) require an explicit suffix for imperial — `wind=27mph`,
   `precip=1.2in` — and treat bare numbers as m/s/mm, consistent with the
   existing similarity CLI; (b) treat bare numbers as imperial in Mode B only,
   since forecast language is imperial; (c) accept bare numbers but echo the
   interpretation prominently and require confirmation.

   **Recommending (a)**: it is consistent with the existing parameter
   convention, it cannot silently misread, and the §4.4.2 report already shows
   both units so a wrong entry is visible immediately. Needs a decision before
   Mode B parsing is written — it is the one Mode B question I cannot resolve
   from the codebase.

---

## 9. File-by-file change list

Revision 2 sizes. Δ column is the change from revision 1.

| File | Change | Size | Δ |
|---|---|---|---|
| **Pre-step (§0.1)** — six regression tests for `7fd6fe4` | prerequisite, separate commit | ~170 | **+170** |
| `src/wiser/units.py` | **NEW** — constants, 4 converters, formatters | ~55 | +15 |
| `src/wiser/storm_analysis.py` | **NEW** — storm SQL, dataclasses, `_fetch` funnel, `compare_forecast` | ~375 | +55 |
| `src/wiser/storm_reports.py` | **NEW** — 4 renderers → (text, rows) | ~350 | +90 |
| `src/wiser/agents/task_routing_agent.py` | 3 `Route` members, patterns, cue sets, gate-0 yield rule, forecast cues | +95 | +20 |
| `src/wiser/handlers.py` | 3 storm handlers, Mode A/B branch, forecast param parsing (reused), `report` fields, similarity presentation switch | +190 | +70 |
| `src/wiser/agents/storm_reasoning_agent.py` | extract public `normalized_ranking_divergence()` | +35 / −20 | — |
| `src/wiser/agents/sql_ai_agent.py` | `SQL_DIALECT` constant (§2.2) | +3 / −1 | — |
| `scripts/wiser_cli.py` | print `result.report` ahead of rows | +10 | — |
| `streamlit_app.py` | render the report block | +12 | — |
| `tests/fixtures/task_routing_cases.json` | +22 cases (16 + 6 Mode B / gate-0) | +150 | +40 |
| `tests/test_task_routing_ground_truth.py` | raise count floor 40 → 64 | +1 / −1 | — |
| `tests/test_task_routing.py` | adversarial non-regression | +40 | — |
| `tests/test_units.py` | **NEW** — incl. round-trip | ~85 | +25 |
| `tests/test_storm_analysis.py` | **NEW** — incl. Mode B, state-param, portability grep guard | ~310 | +90 |
| `tests/test_storm_reports.py` | **NEW** — incl. Mode B labeling assertions | ~260 | +80 |
| `tests/test_storm_reasoning_agent.py` | characterization + extracted-helper | +60 | — |
| `tests/test_handlers.py` | 3 handler suites, Mode A/B, similarity byte-identity | +200 | +60 |
| `tests/test_wiser_cli.py`, `tests/test_streamlit_app.py` | render smoke tests | +30 | — |

**Not touched:** `sql/schema.sql` (Phase 2 only), `query_engine.py`,
`visualization.py`, `storm_weather_analytics_agent.py`, `similarity_engine.py`,
`similarity_query_agent.py`. County-aggregate behaviour is unchanged by
construction. **The similarity engine and agent are explicitly untouched** —
Mode B calls `find_similar` as-is (§4.4.0), which is what makes the
"byte-identical similarity result without a forecast cue" test in §5.6
achievable.

Removed relative to revision 1: duration-ranking implementation and its tests
(§7.1), roughly −15 production / −20 test, already netted into the Δ column.

## 10. Build order

**Step 0 — PREREQUISITE, separate commit (§0.1).** Six regression tests for the
already-committed fixes in `7fd6fe4`. Nothing below starts until this lands,
because steps 5 and 6 build directly on two of those unprotected fixes.

1. `units.py` + `test_units.py` — all four converters incl. round-trip. No
   dependencies; unblocks both the report layer and Mode B parsing.
2. `storm_analysis.py` (Mode A paths) + tests, incl. the portability guard and
   the two-state parameter test. Pure functions against a seeded DB.
3. `storm_reasoning_agent` divergence extraction, characterization test **first**.
4. `storm_reports.py` — summary, ranking, Mode A comparison + tests.
5. **Routing**: 3 intents, the gate-0 yield rule, all 22 fixtures, the
   adversarial non-regression test. Run the full suite here. This is the step
   that can regress existing routes and it should fail loudly *before* any
   handler exists to mask it.
6. Handlers (Mode A) + `AnalyticsResult` fields + registry.
7. **Mode B**, as its own increment once Mode A is green end-to-end:
   `compare_forecast` (delegating to `SimilarityQueryAgent`), the Mode B
   renderer, forecast param parsing, the `handle_similarity_search` presentation
   switch, and the byte-identity test. Blocked on the §8.7 unit decision.
8. CLI and Streamlit rendering.
9. Phase 2: schema + ingest script, separate change.
10. File the §8.6 follow-up-memory issue and the §4.4.0 option-(b) statewide
    similarity issue.

Risk isolation: steps 1–4 cannot regress anything (new files only). Step 5 is
the routing risk concentration point, deliberately alone so a bisect lands on
it. Step 7 is the second concentration point — it is the only step that can
alter existing similarity behaviour, and it is sequenced after Mode A is proven
so a failure there cannot be confused with a Mode A defect.

---

## 11. Estimate delta (revision 1 → revision 2)

| | Rev 1 | Rev 2 | Δ |
|---|---|---|---|
| Production, new files | ~620 | ~780 | +160 |
| Production, modified files | ~155 | ~245 | +90 |
| Tests (feature) | ~840 | ~1,135 | +295 |
| Tests (§0.1 prerequisite) | — | ~170 | +170 |
| **Total** | **~1,615** | **~2,330** | **+715** |

Where the growth is:

- **Mode B: ~+390** (analysis +55, renderer +90, handler +70, units +15,
  routing +20, tests +140). This is the confirmed scope change and it is the
  bulk of the delta. It would have been roughly double without the reuse
  decision in §4.4.0 — delegating to `SimilarityQueryAgent` removes the entire
  retrieval and scoring half.
- **§0.1 prerequisite: +170.** Not new feature work; it is the missing half of
  an already-shipped commit.
- **Dataset-independent test rewrite: ~+90.** Asserting relationships rather
  than literals costs more lines per test than hardcoding numbers would, and it
  is what buys no-re-baselining when the production dataset lands (§8.2).
- **Duration ranking removed: −35**, already netted in.

Two things gate the start: the §0.1 prerequisite commit (blocks everything) and
the §8.7 unit-convention decision (blocks step 7 only — steps 0–6 can proceed
without it).

---

## 12. As-built — deviations from this plan

Recorded so the plan stays trustworthy as a record of what exists.

| Commit | Step |
|---|---|
| `91cfdf9`, `e1b1f35` | 0.1 — regression tests for the six fixes in `7fd6fe4` |
| `c6c1abe` | 1 — `units.py` |
| `10657ce`, `ccd7b35` | 2, 2b — `storm_analysis.py` incl. Mode B |
| `32a00ef` | 3 — divergence extraction |
| `c688596` | 4 — `storm_reports.py` |
| `1d13bcf`, `732fbaf` | 5a, 5b — storm intents, then the gate 0 boundary |
| `c902497` | 6 — handlers |
| `e937a18` | 7 — CLI and Streamlit |

**Sequencing.** Mode B analysis became its own commit (2b) rather than a final
step, once Capability 3 was approved and folded into steps 2/4/5/6. Step 5 was
split into 5a (storm intents) and 5b (gate 0 revision) so a bisect points at one
routing change rather than two.

**Things found during implementation that changed the design:**

1. **The bare-identifier routing rule was too loose.** The adversarial
   non-regression test caught `"load the raw storm csv 2020080406"` routing to
   `STORM_SUMMARY`. Rule 3b now also yields to `_looks_like_data_management`.
   Documentation verbs still do *not* block it — that is fixture
   `routing_guard_002`, a deliberate behaviour change.

2. **Forecast unit suffixes must touch the number.** Allowing a space made
   `"wind=12 to storm 2020080406"` read `"to"` as a unit, and would have made
   `"precip=1.2 in Fairfield"` read the preposition as inches. Attached-only is
   also what §8.7 Option A specified.

3. **Mode B with a named storm needed a rendering that did not exist.** Routing
   rule 1b sends "compare a forecast to storm X" to Mode B, but §4.4.2's report
   had nowhere to put storm X. It renders as a "Named reference storm (actual)"
   section — measured history beside the analogues, never a forecast outcome.
   An unresolvable reference still renders the forecast half and reports what
   was dropped.

4. **The concentration bullet claimed a difference the reader could not see.**
   Against the real database both storms showed 28% and the report then declared
   one "more concentrated". It now says "comparably concentrated" when the two
   round to the same displayed percentage.

5. **Reasoning is attached to the summary only.** Its rows are county-shaped,
   which is what the reasoning layer reads. Ranking and comparison rows are
   metric-shaped, and those reports already carry their interpretation computed
   deterministically.

6. **Two pre-existing registry tests hardcoded the four-route set.** They now
   derive it from `Route`, so a future route fails them until it is wired up.

**Unchanged from plan but worth restating:** `state` is a bound parameter
throughout; no test hardcodes a storm count or magnitude from `data/wiser.db`;
the portability guard extracts the module's SQL literals with `ast` and asserts
against those; duration is not rankable and requesting it is refused rather
than silently substituted.

**Not built, by instruction:** Phase 2 (§6) — the `StormMetadataObservation`
table and NCEI/GHCN ingest. Capability 1 renders storm type, peak snowfall, and
wind gust as explicitly unavailable until it lands.

**Follow-ups filed by this work:** §8.6 storm-scoped follow-up memory; §4.4.0
option (b), a storm-level statewide Z-score space for cross-county forecast
analogues.

### 12.1 After the plan closed

Work that landed on top of the eight steps above. Recorded here because it
changes behaviour the plan specified, not because it was planned.

| Commit | Change |
|---|---|
| `cbe525d` | Demo feedback: gust semantics, percentile scoping, divergence, display |
| `965674f` | Zero-baseline metrics are described rather than scored; same-date storm siblings flagged |
| `0a71912` | The same-date sibling warning extended to summaries and disambiguation |
| `6310a59` | Statewide forecast queries need no county, and honour the units given |
| `78d4999` | Reasoning unit errors fixed; report prose wrapped for slides |
| `02a5d79` | Side-by-side county maps for a comparison, on one shared colour scale |
| `891f5dd` | Sequential one-hue palette, magnitude compression, banded value legend |

**Then a code review of that map work, one commit per finding:**

| Commit | Finding |
|---|---|
| `6e93336` | A stray module-level `unittest.main()` hid five test classes from direct runs |
| `2aecf2a` | A regression guard compared hex strings, so `max()` returned the no-data grey and the assertion held no matter what the scale did |
| `bd7b128` | The scale curve was inferred from the data (`min_value > 0`) rather than declared by the metric; one county reporting 0 reshaded the whole map, and zero-anchoring every metric flattened wind spreads into the top bands |
| `42d2df9` | The No-data key was placed past the right edge once the swatches hit their minimum width |
| `99ee7c0` | A flat range painted every county the darkest colour and labelled all seven legend bands identically |
| `0807f36` | An unrelated pre-existing crash: one `_render_bar_chart` call site never got the axis labels the signature had required since `cbe525d` |

**Design decisions taken during those fixes**, which the plan did not cover:

1. **`zero_anchored` lives on the metric registry.** Whether a quantity's floor
   is a meaningful zero is a property of the quantity, so `CountyHeatmapMetric`
   declares it and `_scale_position`, `_band_edges` and `comparison_map_panels`
   read it. Anything unregistered defaults to linear.
2. **A flat map reads as empty at zero, neutral otherwise.** A range with no
   spread cannot honestly claim either end of the ramp, so zero paints the light
   end and any other flat value paints mid-ramp, with the legend naming the
   value outright.
3. **The No-data key wraps rather than shrinks.** Below the width where swatches
   hit their 28px floor it drops to its own row instead of being pushed off the
   canvas.

**The rest of that review, closed afterwards:**

| Commit | Finding |
|---|---|
| `53ab762` | Band labels were set at 10px and then the map was scaled to ~0.5 to fit a column; panels now render at their own size against a matching frame, and alternate boundaries carry the value |
| `fe16c37` | No-data grey sat at 1.04:1 against the ramp's lightest colour; it is now a hatch over a warm base at 1.26:1 minimum against the whole ramp |
| `1c17b00` | A below-resolution difference dropped its sign, on exactly the rows where both value columns print the same number |
| `830662a` | "peaks at 12.35 in Fairfield" read the county as inches; the county is named first and the unit comes from the registry |
| `b430be7` | The tooltip omitted the storm-scoped `outage` column, and the legend header printed "1M ohLine" twice |
| `b951208` | Two number formatters in one image, one of them using banker's rounding |
| `e18dae1` | The precision loop tested the previous iteration's result; tooltip units were copied out of the registry |

All fifteen findings from that review are now closed.
