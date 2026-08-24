# WISER Deployment Plan — Streamlit Community Cloud + Neon

**Goal:** a stable public URL we send to sponsors, who open it and run their own queries.
**Budget:** $0 infrastructure. OpenAI usage is the only variable cost, and only during the demo.
**Status:** **Phases 0–2 complete.** The Neon project is live on PostgreSQL 17.11 (`neondb`, AWS us-east-1), seeded, and serving the app through the restricted `wiser_app` role. Phase 3 (Streamlit Cloud) is next and needs a person to authorize GitHub access.

Measured on the live database:

| | |
|---|---|
| Neon round-trip | **15 ms** median (13.5 ms for a real query) — the database is not a bottleneck |
| Seeded rows | Storm 294, County 8, WeatherData 2352 — exact match with local |
| Uncached AI query | **3.24 s**, essentially all OpenAI latency |
| Same query, cached | **0.08 s** — a 40× drop, identical rows, since only the LLM step is reused |
| R1 attack | `DROP` / `DROP IF EXISTS` / `TRUNCATE` / `ALTER` / core `INSERT` — **all refused** |

### Two milestones

This plan covers both, but only Milestone A is being built now.

| | **A — Sponsor demo (now)** | **B — Finished product (later)** |
|---|---|---|
| Who supplies the OpenAI key | **We do.** One key in Streamlit secrets, hard monthly cap. | **Each user does.** They paste their own key into the app. |
| Who pays for AI queries | Us | Them |
| Access | Private app, invited sponsor emails | Out of scope here — revisit when B is scheduled |
| Code changes needed | None to the key path | Real work — see §8 |

The demo deliberately does **not** ship the BYOK path. Section 8 scopes it so we know what we are committing to, and so nothing built for A has to be torn out for B.

---

## 1. Target topology

```
Sponsor's browser
        |
        v
Streamlit Community Cloud            Neon (managed Postgres 17)
  runs streamlit_app.py       <----> Storm / County / WeatherData
  installs requirements.txt           RagDocument / RagChunk
  secrets: DATABASE_URL,              ExternalOutageObservation
           OPENAI_API_KEY
        |
        v
  api.openai.com                 (AI SQL generation + impact summaries)
  nhc.noaa.gov / ncei.noaa.gov   (external storm context, fetched at runtime)
```

Two hosts, both free tier. No container, no custom domain, no CI.

### Why this shape

- Streamlit Cloud is purpose-built for "push to GitHub, get a shareable link," and handles Python deps and secrets for us.
- Neon gives managed Postgres 17, which `sql/schema.sql` already targets.
- The dataset is tiny — `data/testdata.csv` is 202 KB, the whole `data/` tree is 401 KB — so we are nowhere near any free-tier storage limit.

### Known free-tier behaviour to plan around

Verified against live docs, August 2026:

| Constraint | Value | Effect on a sponsor demo |
|---|---|---|
| Streamlit private apps, free tier | **1** | Decision 1 spends our only slot. A second private app would need a paid tier or a public repo. |
| Streamlit app sleep | After **12 quiet hours** | Only an overnight gap sleeps the app. During an active sponsor window it stays warm on its own. |
| Streamlit app memory | **1 GB** | `faiss-cpu` + `numpy` may not be worth the footprint; both are optional with graceful fallback. |
| Streamlit custom domains | Not available | The URL will be `<name>.streamlit.app`. Pick the subdomain deliberately. |
| Streamlit private-repo access | Read-only deploy key | Streamlit gets read-only SSH access to the repo. It cannot push. |
| Neon organization members | **Unlimited on free** | Both teammates can be members at no cost. |

**Mitigation for the sleep problem:** warm the app once each morning of a sponsor window. Because sleep needs 12 quiet hours, this is a once-a-day action, not a per-call one — much less of a liability than it first appeared. This remains the main thing we trade away by not paying for Render.

**Unverified:** whether Streamlit app-admin rights follow GitHub write permission automatically. The docs describe full-admin vs view-only roles but do not state the inheritance model. Confirm in Phase 3 — it determines whether both teammates get admin for free or one must be added explicitly.

Sources: [Streamlit status and limitations](https://docs.streamlit.io/deploy/streamlit-community-cloud/status) · [Streamlit manage-your-app](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app) · [Neon changelog 2026-03-13](https://neon.com/docs/changelog/2026-03-13) · [Neon plans](https://neon.com/docs/introduction/plans)

---

## 2. What the codebase already gives us

Verified against the current `main` (`67d6aca`):

- `data/testdata.csv` **is committed**, so the seed data reaches the cloud with the repo.
- `data/normalized/` is gitignored, but `scripts/bootstrap_wiser.py` regenerates it from `testdata.csv`. Nothing is lost.
- `.env` is gitignored. No secrets are in git history.
- `resolve_dsn()` precedence is: explicit arg → `$DATABASE_URL` → local Docker default. Setting `DATABASE_URL` in Streamlit secrets is sufficient to repoint the whole app.
- `sql_ai_agent.py` runs generated SQL under `SET LOCAL transaction_read_only = on`, so there is already one layer of write protection on the AI path.
- Optional deps degrade gracefully when absent (per README) — `sqlglot`, `faiss`, `pypdf`, `openai` are all absent from the local `.venv` and the suite still passes.

---

## 3. Risks specific to making this public

These are the things that change when strangers, rather than we, drive the app.

### R1 — A sponsor can wipe the database by typing a sentence

`TaskRoutingAgent` matches the keyword `"rebuild"` and routes to `Route.DATA_RETRIEVAL` → `handle_data_retrieval` (`src/wiser/handlers.py:400`) → `build_database()`, and `sql/schema.sql` opens with `DROP TABLE`.

A sponsor typing *"rebuild the database"* into the query box therefore drops and reloads every table.

Severity is moderate, not fatal: it rebuilds from the committed CSV, so the data comes back. But it would derail a live demo, and it is trivially reachable by accident.

**Fix:** run the app under a Postgres role that lacks DDL rights, so the DROP fails with a permission error instead of succeeding. See Decision 5 — this interacts with R2.

### R2 — The RAG features need write access, so "just make it read-only" does not work

`rag.py` and `external_sources.py` both write at runtime: `CREATE TABLE IF NOT EXISTS RagDocument / RagChunk`, plus `INSERT` and `DELETE` against both. A blanket read-only role breaks the RAG knowledge route and the external-context crawl.

Two viable postures, decided in Decision 5:

- **(a) Scoped role** — `SELECT` only on `Storm`, `County`, `WeatherData`; read/write on `RagDocument`, `RagChunk`, `ExternalOutageObservation`; no DDL on the core tables. Keeps every feature, blocks R1.
- **(b) Fully read-only** — pre-build the RAG index during seeding, then serve read-only and hide the "Rebuild RAG index" checkbox (`streamlit_app.py:858`). Simplest and safest, but external context goes stale and cannot refresh.

### R3 — Our OpenAI key funds every sponsor query *(Milestone A only)*

Each AI-SQL or summary request bills our account. Public plus unauthenticated equals uncapped spend.

**Fix:** a hard monthly budget cap on the OpenAI account, plus the invite gate from Decision 3. Two layers — the cap bounds the damage, the invite list bounds who can cause it.

**Third layer, now implemented: a generated-SQL cache.** `SqlAiAgent` can reuse previously generated SQL when the prompt is byte-identical, skipping the OpenAI call entirely. Sponsors demoing the same handful of example queries — which is exactly what the `EXAMPLE_QUERIES` suggestions encourage — pay for the first run of each and nothing after. Enabled by default in the Streamlit UI ("Cache generated SQL"), off by default at the library level.

Only the LLM step is cached; the SQL is always re-executed, so results stay live and a cache hit can never serve stale rows. Details in §9.

This risk **disappears at Milestone B**: once each user supplies their own key, their usage bills them, not us. R3 is a demo-window problem with a known end date, not a permanent condition.

### R4 — SQL validation is running in fallback mode

`sqlglot` is optional and currently not installed, so `sql_ai_agent.py` falls back to regex keyword matching for SELECT-only enforcement instead of real parse-level validation. Acceptable locally; not what we want when strangers author the prompts that generate the SQL.

**Fix:** uncomment `sqlglot` in `requirements.txt` before deploying. This is a required step, not optional hardening.

### R5 — `requirements.txt` is not deploy-ready

Only `psycopg[binary]` is uncommented. `streamlit` itself is commented out. Streamlit Cloud installs from this file, so it needs editing before the first deploy regardless of any other decision.

### R6 — A user-pasted API key is a credential we are holding *(Milestone B only)*

Not live for the demo. Recorded now because it constrains how BYOK gets built, and getting it wrong leaks someone else's paid credential.

Once a user types their key into the app, that key is in our process memory. Four places it must never reach:

1. **The conversation transcript.** `HandlerContext` carries `history: tuple[ConversationTurn, ...]`. If a key ever lands in a turn, it persists into the memory feature and is replayed into later prompts — including prompts sent to OpenAI.
2. **The database.** Nothing in the RAG or conversation tables should ever receive it.
3. **Logs and error text.** An exception that interpolates the key into its message will surface it in the Streamlit UI and the cloud logs.
4. **Streamlit secrets.** Secrets are for *our* credentials. A user key is per-session input and belongs only in `st.session_state`, entered through a `type="password"` field.

**Precedent to follow:** `streamlit_app.py:556` already redacts the DSN password before it reaches the browser or the transcript. That is exactly the right instinct and exactly the pattern BYOK needs — the work is applying it to a second secret, not inventing an approach.

**Also note:** Streamlit Community Cloud's 1 GB app process is shared across concurrent sessions. `st.session_state` is per-session and does not cross users, but anything promoted to module scope or a cached resource **would**. No `@st.cache_resource` may ever hold a user key.

---

## 4. Decisions

### Resolved

| # | Decision | Resolution | Consequence |
|---|---|---|---|
| **1** | Repo visibility | **Keep `jgb23002/WISER` private** | Spends the free tier's private-app allowance. Verify the current allowance before Phase 3 — if it is one app, this is our only one. Code and history stay closed. |
| **3** | Auth model | **Streamlit private-app email invites** | Native to a private-repo deploy, so no gate code in `streamlit_app.py`. Per-person revocation, which matters because Decision 4 ships a live API key. Cost: sponsors sign in on first open, and we need their addresses up front. |
| **4** | AI key ownership | **Demo: our key, hard-capped. Product: user-supplied (BYOK).** | For the demo, sponsors see AI SQL generation and impact summaries at our expense — R3 is live but bounded by the cap and the invite list. For the finished product the cost and abuse surface transfer to the user entirely, which retires R3 but introduces R6. BYOK work is scoped in §8 and is **not** part of this deployment. |
| **5** | DB write posture | **Scoped role** (R2 option a) | Every feature keeps working, and R1 is closed — `DROP` is denied at the role level. Costs extra grant setup in Phase 2. |
| **2** | Account ownership | **Both teammates, on both services** | Free on both tiers: Neon allows unlimited free org members, and Streamlit private-repo access is free. Neither of us is a single point of failure. One open item — see "Unverified" in §1 — is whether Streamlit admin follows repo write access automatically. |
| **6** | Deploy branch | **Dedicated `deploy` branch** | Streamlit tracks `deploy`; `main` stays free for active development. Production moves only when we deliberately fast-forward `deploy`, so a mid-demo push cannot break what a sponsor is looking at. |

Decisions 3 and 4 interlock deliberately: the key is live, so the gate must revoke per person rather than all-or-nothing.

**All seven decisions are now settled.** Phases 0–5 are unblocked.

### Decided by default (raise it if you disagree)

| # | Decision | Default | Reasoning |
|---|---|---|---|
| **7** | Runtime NOAA fetches | **Leave enabled, verify from cloud egress in Phase 4** | They are a real product feature, so disabling them shows sponsors less than we have. But their reliability is outside our control — so Phase 4 tests them from Streamlit Cloud specifically, and if they prove flaky we pre-index the content and disable the live crawl before sponsors see it. |

---

## 5. Phased plan

All decisions are settled, so every phase below is unblocked. They run in order — each phase's exit criterion gates the next.

### Phase 0 — Prep the repo for cloud install  [COMPLETE]

1. Edit `requirements.txt`: uncomment `streamlit`, uncomment `sqlglot` (R4), pin versions.
2. Decide on `numpy` / `faiss-cpu` / `pypdf` — each costs install time and memory against the ~1 GB cap; all are optional with graceful fallback.
3. Add `.streamlit/config.toml` if we want to control theme or server options. Does not exist yet.
4. Confirm the Python version Streamlit Cloud should use (local is 3.13.5; there is no `pyproject.toml` pinning a floor).

**Exit criterion:** a clean `pip install -r requirements.txt` into a fresh venv, then `streamlit run streamlit_app.py` works locally against local Docker Postgres.

### Phase 1 — Stand up Neon  [COMPLETE]

1. Create the Neon project, Postgres **17** to match `sql/schema.sql`. Invite the other teammate as an org member (free, unlimited).
2. Capture the pooled connection string. **Neon requires TLS — the DSN needs `sslmode=require`.**
3. Verify `psycopg` connects to Neon from this machine with that exact DSN before anything else depends on it.

**Exit criterion:** `SELECT version()` over the Neon DSN returns PostgreSQL 17.

### Phase 2 — Seed the database  [COMPLETE]

1. Run `scripts/bootstrap_wiser.py --dsn "<neon-dsn>"` locally, as the **owner** role. Seeding needs DDL; only the app's runtime role is restricted.
2. Verify row counts against the local Docker instance — they should match exactly.
3. If Decision 5 picks option (b), also build the RAG index now, while write access still exists.
4. Create the restricted runtime role: `psql "<owner-dsn>" -v app_password="'...'" -f sql/deploy_runtime_role.sql`. That script is written and **rehearsed end-to-end against the local container**, not just drafted.
5. Re-verify: connect as the **runtime** role and confirm reads work and a `DROP TABLE` is refused.

**Exit criterion:** the runtime role can answer a normal query and cannot drop a table. Verified locally — the full handler stack answers "top 3 counties by outage" over the pool as `wiser_app`, while `DROP TABLE`, `DROP TABLE IF EXISTS`, `TRUNCATE`, `ALTER TABLE`, and `INSERT` into core tables are all refused.

#### Four things the rehearsal found that the design assumed wrong

These are the reason the script exists rather than a handful of GRANT lines in this document:

1. **`CREATE` on the schema is required, not a loosening.** `rag.py` runs `CREATE TABLE IF NOT EXISTS` on RagDocument/RagChunk, and PostgreSQL checks the schema's CREATE privilege *before* the IF-NOT-EXISTS shortcut — so a strict no-DDL role raises `InsufficientPrivilege` even when the tables already exist, breaking every RAG route.
2. **R1 is closed by ownership, not by withholding CREATE.** A role with `USAGE, CREATE` still cannot `DROP`/`TRUNCATE`/`ALTER` a table it does not own. Granting CREATE therefore costs nothing against R1.
3. **`StormCounty` must not appear in the grants.** `schema.sql` DROPs that name but never CREATEs it, so granting on it aborts the whole script.
4. **`ALTER DEFAULT PRIVILEGES` is a trap here.** It cannot be scoped to table names, so it would hand the app write access to every future owner-created table — including `WeatherData` after a re-seed, silently undoing the read-only core. The script omits it; re-run the script after any re-seed instead.
5. **The database name cannot be hardcoded.** Locally it is `wiser`; **Neon names its default `neondb`**, and `GRANT ... ON DATABASE wiser` fails outright with "database does not exist". `GRANT` has no way to say "the current one", so the script builds that statement dynamically from `current_database()`.

#### Rehearsed twice, on purpose

Once against the seeded local database, and once against a **fresh, empty database named `neondb`** — because Neon starts empty and the two cases differ in a way that matters. On a fresh database `RagDocument`/`RagChunk` do not exist yet, so the grants skip them and the app creates and *owns* them on first use; on a re-used database the owner already holds them and the grants apply. The script handles both, and the full sequence was verified end to end: `bootstrap_wiser.py` seeds → role script runs → the app self-bootstraps its RAG tables → writes succeed → `DROP`/`TRUNCATE` on core tables still refused.

### Phase 3 — Deploy the app  [NEXT]

0. Create the `deploy` branch from `main` (Decision 6) and push it.
1. Authorize Streamlit Cloud against the private GitHub repo (Decision 1). Confirm whether the second teammate gets admin automatically via repo write access, or must be added explicitly — this is the one unverified item from §1.
2. Create the app tracking **`deploy`**, entrypoint `streamlit_app.py`.
3. Populate secrets: `DATABASE_URL` (runtime role, `sslmode=require`), `OPENAI_API_KEY`, `WISER_OPENAI_MODEL`. The key here is **ours** — this is Milestone A. Milestone B replaces it with per-user input (§8), it does not add to it.
4. Confirm the app is private, then add each sponsor's email to the viewer list (Decision 3). Keep that list somewhere we can audit later.
5. Set the app subdomain — pick a name worth putting in front of sponsors, since a custom domain is not available on this tier.

**Exit criterion:** the public URL loads and answers a query end to end.

### Phase 4 — Harden and verify *(no blockers once Phase 3 lands)*

1. Set the OpenAI hard monthly budget cap (R3). Do this **before** sending any sponsor the link, not after. Since this is our key for the whole demo window, the cap is the only thing standing between an enthusiastic sponsor and a real bill.
2. Attempt the R1 attack deliberately: type "rebuild the database" as a sponsor would, and confirm it fails safely rather than wiping the DB.
3. Confirm `sqlglot` is actually imported in production, not silently falling back (R4).
4. Exercise every route sponsors will touch: SQL AI, analytics, similarity, RAG, visualization.
5. Decide the fate of the runtime NOAA fetches per Decision 7.
6. Check for anything leaking into the UI. `streamlit_app.py:556` already redacts the DSN password; confirm nothing else does.

**Exit criterion:** the R1 attack fails, and every sponsor-facing route returns a correct result.

### Phase 5 — Hand off

1. Write a short sponsor-facing note: what WISER does, 3–5 example queries worth trying, and who to contact.
2. Warn sponsors about the cold start, or warm the app before sending the link.
3. Agree who watches the OpenAI spend and the Neon usage in the first week.

---

## 6. Rollback

- **Bad deploy:** reset `deploy` back to the last known-good commit and force-push. Streamlit redeploys from the branch head, so this is a one-line rollback that never touches `main`.
- **Data corrupted or wiped:** re-run `scripts/bootstrap_wiser.py --force` against Neon. Everything derives from the committed `data/testdata.csv`, so recovery takes minutes and loses nothing permanent.
- **Cost runaway:** delete the `OPENAI_API_KEY` secret. The app keeps working with AI routes degraded.
- **Full teardown:** delete the Streamlit app and the Neon project. No other infrastructure exists.

---

## 7. Cost

**Infrastructure: $0**, at the tiers described, for both milestones. Streamlit Community Cloud and Neon are free, and Neon's unlimited free org members mean adding teammates costs nothing.

**OpenAI: the only variable cost, and only during Milestone A.** Bounded by the hard monthly cap set in Phase 4 and by the invite list from Decision 3. At Milestone B this line goes to zero for us — each user's queries bill their own key.

Infrastructure cost only appears if we outgrow the free tiers: sustained traffic past the 1 GB app cap, or a decision to eliminate the 12-hour sleep. The paid fallback is Render at roughly $14/month for app plus database, which also buys a custom domain.

---

## 8. Milestone B — bring-your-own-key *(scoped, not scheduled)*

Not part of this deployment. Scoped here so the size is known and so Milestone A does not build anything that has to be undone.

### The good news: it is half-built

The key already threads as a parameter through two of the three call sites. This was designed for, not bolted on.

| Component | State | Work needed |
|---|---|---|
| `openai_summarizer.summarize_storm_impact_with_openai` | `api_key` param, env fallback | None |
| `storm_reasoning_agent` (`:169`, `:190`) | `api_key` param, env fallback | None |
| `sql_ai_agent` (`:206`) | Reads `os.environ` directly, **no param** | Add an `api_key` parameter, keep env as the fallback |
| `openai_config()` (`:61`) | Reads `os.environ` directly for `api_key_present` | Accept an optional key so the "is a key present?" check reflects the session, not the process |
| `HandlerContext` | Has `openai_model`, `sql_model`, `use_openai_summary` — **no key field** | Add `openai_api_key: str \| None`, thread through `build_context` and `handlers.py:2020` |
| `streamlit_app.py` | No key input exists | Add a `type="password"` field, hold it in `st.session_state` only |

The env fallback must stay in every case. It is what lets the CLI, the test suite, and our own demo deploy keep working unchanged — Milestone B should be additive, not a migration.

### Design constraints

- **Session-scoped, never global.** Per R6, no module-level variable and no `@st.cache_resource` may hold a user key.
- **Redact on every egress path.** Follow the DSN precedent at `streamlit_app.py:556`.
- **Never persisted.** Not to the database, not to the conversation history, not to logs.
- **Graceful when absent.** The app already degrades rather than crashes without a key. A user who has not pasted one should get a clear prompt to do so, not a stack trace.

### Open questions for when B is scheduled

1. Do users supply a key per session, or do we offer to remember it? Remembering it means storing someone else's credential, which is a materially larger commitment.
2. Does the app validate the key on entry (a cheap probe call) or fail on first real use?
3. Do we still ship a fallback key for evaluation, or is BYOK mandatory in the product?

None of these need answering now. They are recorded so B starts from a known position rather than a blank page.

---

## 9. Generated-SQL cache *(implemented)*

Built in response to R3: every repeated question was re-billing a fresh OpenAI call.

### Where the cache boundary sits

```
_build_messages()  ->  _call_chat_llm()  ->  _clean_sql()  ->  _validate_sql()  ->  _execute_ro()
  deterministic         SLOW / BILLED         deterministic     deterministic       MUST STAY LIVE
                        |____________________ cached ______________________|
```

Only the generation half is reused. Execution is deliberately excluded: rows go stale as soon as the data changes, and `handle_data_retrieval` can rebuild the database mid-session, so a cached result set could be wrong in a way a cached *query* cannot. Every hit re-runs the SQL against live data.

### Design

| Aspect | Choice | Reason |
|---|---|---|
| Key | SHA-256 of the exact `messages` payload plus the resolved model | `messages` already folds in the schema DDL, question, row limit, and conversation history — everything `_build_messages()` varies on. Two calls that would send OpenAI identical bytes share an entry; nothing else does. |
| Never in the key | The API key | Keying on a credential would fragment the cache per user at Milestone B and put a secret in a dictionary key. Not needed: the key does not change what SQL comes back. |
| Value | The **validated** SQL string only | Nothing unvalidated is reachable from the cache, because entries are written only after `_validate_sql` passes. |
| Re-validation | Runs again on every hit | The cache is ordinary process memory on the path to statement execution. It re-checks rather than trusting itself — cheap insurance on a security boundary. |
| Bounds | 256 entries, LRU eviction | The key derives from user-supplied text, so an unbounded dict is a memory leak inside a 1 GB app. |
| Concurrency | `threading.Lock` | Streamlit serves concurrent sessions from one process. |
| Default | **Off** at the library level, **on** in the Streamlit UI | The test suite patches `_call_chat_llm` and counts invocations; a default-on cache would silently change what those assertions mean. Opt-in keeps every existing caller's behaviour identical. |

### Surface

- `SqlAiAgent(use_cache=True)` — opt in.
- `ctx.use_sql_cache` — threads through `build_context()` like `dry_run_sql` already does.
- `result["cached"]` — new key on the `run()` return, `True` when the SQL was reused.
- `clear_sql_cache()` / `sql_cache_stats()` — reset and observe (`entries`, `hits`, `misses`).

### Verification

10 tests in `TestGeneratedSqlCache` cover: one LLM call for a repeated question, off-by-default, **rows re-executed on a hit**, differing history and differing model as distinct entries, sharing across instances, invalid SQL never cached, the entry cap, hit/miss accounting, and dry-run never caching.

Full suite: **648 passed, 348 subtests** (was 638 before this work).

### Milestone B note

The cache is shared process-wide, so under BYOK one user's generated SQL can serve another's identical question. The *data* is a shared storm database and the *key* is never cached, so no credential or private data crosses sessions — but the question text is the cache key, so revisit this if user queries ever become confidential.
