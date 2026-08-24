# Streamlit deploy — the only steps that need the repo owner

Everything else is done. Neon is live, seeded, hardened, and already answering
real queries; the app has been run against it end to end. What remains cannot
be done by anyone without GitHub access to `jgb23002/WISER`, because Streamlit
Community Cloud authorizes against the repo itself.

Budget about five minutes, plus a few minutes of build time.

---

## Before you start

You need two values from whoever set this up:

| | |
|---|---|
| **Neon app DSN** | The `wiser_app` connection string. **Not** `neondb_owner` — that role can drop tables and is deliberately not what the app uses. |
| **OpenAI API key** | The same one in the repo-root `.env`. |

---

## 1. Sign in

Go to **https://share.streamlit.io** and choose **Continue with GitHub**.

Sign in with the GitHub account that can see `jgb23002/WISER`. The repo is
private, so a Google or email sign-in lands in a workspace that cannot find it.

## 2. Create the app

Click **Create app**, then choose the option for an **existing repo** — not
"create one from a template".

| Field | Value |
|---|---|
| Repository | `jgb23002/WISER` |
| Branch | **`deploy`** |
| Main file path | `streamlit_app.py` |
| Python version | **3.13** if offered |
| App URL | your choice of `<name>.streamlit.app` |

The branch matters. `deploy` only ever moves when someone deliberately
fast-forwards it, so active work on `main` cannot change what a sponsor is
looking at mid-demo.

GitHub will prompt to grant Streamlit access to the private repo. It takes a
read-only deploy key and cannot push.

## 3. Paste the secrets BEFORE deploying

Open **Advanced settings** on the same screen and paste all three lines. Doing
it now avoids a failed first boot — without `DATABASE_URL` the app cannot reach
Neon.

```toml
DATABASE_URL = "postgresql://wiser_app:<password>@<host>.neon.tech/neondb?sslmode=require"
OPENAI_API_KEY = "sk-..."
WISER_OPENAI_MODEL = "gpt-4o-mini"
```

**Keep all three at root level.** Streamlit exposes only root-level secrets as
environment variables, and this app reads `os.environ` throughout. Nesting any
of them under a `[section]` header does not raise an error — the app quietly
falls back to a localhost database and fails to connect, which looks like a
network problem rather than a config one.

## 4. Deploy

First build takes a few minutes: `requirements.txt` pulls streamlit, pandas,
pyarrow and numpy. The pinned set has been verified to install cleanly into a
fresh virtualenv, so a resolution failure here would be a surprise.

## 5. Confirm it is private, then invite sponsors

Because the repo is private the app should default to private — **confirm it
rather than assuming.** This is the gate keeping the OpenAI budget from being
open to anyone with the link.

Then add each sponsor's email as a viewer. They sign in on first open.

## 6. Set the OpenAI budget cap

In the OpenAI account, before sending anyone the link. Every sponsor query
bills this key, and the cap is the only backstop if a link is forwarded.

---

## What is already done, so you can skip it

- **Neon**: project live on PostgreSQL 17.11, seeded (Storm 294, County 8,
  WeatherData 2352 — matching local exactly).
- **The restricted role**: `wiser_app` created and verified. `DROP`,
  `DROP IF EXISTS`, `TRUNCATE`, `ALTER`, and writes to core tables are all
  refused, while RAG and the external-outage staging table stay writable.
- **The destructive-query guard**: typing "rebuild the database" into the real
  UI was tested against the live database. It fails safely and the app does not
  crash.
- **Every example query**: all 13 non-destructive suggestions were run against
  Neon and return results.
- **The slow first query**: "impact of the new storm in Connecticut" crawls
  NOAA on first use and took 95 seconds. That has already been run, and the
  index (54 documents, 2350 chunks) is stored **in Neon**, so it survives app
  restarts and sleeps. The same query now takes 4 seconds. No sponsor will pay
  that cost.
- **Dependencies**: pinned to a locally verified set and confirmed to install
  into a clean virtualenv.
- **A clean first impression**: the app was driven against the live database
  until it produced zero spurious warnings. Two were found and fixed -- the
  evaluation panel needed CREATE on the database, and the external-outage
  staging table needed app ownership because CREATE INDEX IF NOT EXISTS checks
  ownership. Sponsors now see warnings only when something is genuinely wrong.
- **The evaluation panel works and is worth showing**: 27/27 SQL ground-truth
  cases and 73/73 routing cases pass against the deployed database.

## After it is up

Tell whoever is running the deployment. Two checks need the live URL and
cannot be done in advance: confirming `sqlglot` loaded in the cloud
environment rather than silently falling back to regex SQL validation, and
walking the sponsor-facing routes through the deployed app.
