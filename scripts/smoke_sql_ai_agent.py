"""
Live batch harness for SqlAiAgent against the real OpenAI API, using the
cheapest practical model by default.

NOT a unit test: this script spends real API credits, so it lives in
scripts/ and is intentionally excluded from `python -m unittest discover`.
The offline unit tests are in tests/test_sql_ai_agent.py.

Runs natural-language questions end-to-end (NL -> LLM -> SQL -> validate ->
read-only execute) and reports SQL, answer, rows, and latency per query.

Scales along three axes:
  * Data     -- point at any real DB with --dsn, or use the built-in throwaway
               schema (built from sql/schema.sql) when --dsn is omitted.
  * Queries  -- pass --query repeatedly, or --queries-file to load hundreds
               (one per line; blank lines and '#' comments ignored).
  * Throughput -- --workers N runs queries concurrently (calls are I/O-bound
               HTTP); each task opens its own connection, so it is safe.

Requires OPENAI_API_KEY (from a repo-root .env or the environment).

Usage:
    python scripts/smoke_sql_ai_agent.py
    python scripts/smoke_sql_ai_agent.py --model gpt-4.1-nano
    python scripts/smoke_sql_ai_agent.py --dsn postgresql://wiser:wiser123@localhost:5433/wiser \n        --queries-file eval/questions.txt --workers 8
    python scripts/smoke_sql_ai_agent.py --query "top 3 storms by outage" --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import secrets
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.agents.sql_ai_agent import SqlAiAgent  # noqa: E402
from wiser.database import connect, resolve_dsn  # noqa: E402
from wiser.openai_summarizer import load_dotenv  # noqa: E402

# Matches the agent's default; gpt-4.1-nano is marginally cheaper if needed.
CHEAP_MODEL = "gpt-4o-mini"

# Sample rows for the throwaway fixture, so queries return something real.
_STORM_ROW = (2012102806, "2012-10-28 06:00:00", 72)
_COUNTY_FAIRFIELD = ("Fairfield", 4887830)
_COUNTY_HARTFORD = ("Hartford", 5869550)
_WD_FAIRFIELD = (2012102806, "Fairfield", 150.5, 12.3, 20, 8, 2, 0, 350)

DEFAULT_QUERIES = [
    "What is the total outage count for Fairfield county?",
    "Which storms lasted longer than 48 hours?",
    "Top storms by outage",
]


@dataclass
class QueryResult:
    query: str
    ok: bool
    latency_s: float
    sql: str = ""
    answer: str = ""
    row_count: int = 0
    error: str = ""


def _build_fixture_schema() -> str:
    """Create a throwaway PostgreSQL schema from schema.sql + sample rows.

    PostgreSQL has no temp-file database, so isolation comes from a uniquely
    named schema. Returns its name; the caller drops it when finished.
    """
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    schema_name = "wiser_smoke_" + secrets.token_hex(8)

    conn = connect()
    try:
        conn.execute("CREATE SCHEMA " + schema_name)
        conn.execute("SET search_path TO " + schema_name)
        conn.execute(schema_sql)
        conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength)"
            " VALUES(%s, %s, %s)",
            _STORM_ROW,
        )
        conn.execute("INSERT INTO County(county, ohLine) VALUES(%s, %s)", _COUNTY_FAIRFIELD)
        conn.execute("INSERT INTO County(county, ohLine) VALUES(%s, %s)", _COUNTY_HARTFORD)
        conn.execute(
            "INSERT INTO WeatherData(stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,"
            " wgt5, wgt9, wgt13, wgt18, outage)"
            " VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            _WD_FAIRFIELD,
        )
        conn.commit()
    finally:
        if conn is not None:
            conn.close()
    return schema_name


def _drop_fixture_schema(schema_name: str) -> None:
    conn = connect()
    try:
        conn.execute("DROP SCHEMA IF EXISTS " + schema_name + " CASCADE")
        conn.commit()
    finally:
        if conn is not None:
            conn.close()


def _load_queries(args: argparse.Namespace) -> list[str]:
    queries: list[str] = list(args.queries or [])
    if args.queries_file:
        for raw in Path(args.queries_file).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                queries.append(line)
    return queries or list(DEFAULT_QUERIES)


def _run_one(
    agent: SqlAiAgent, dsn: str | None, schema_name: str | None, query: str
) -> QueryResult:
    """Execute a single query on its own connection (thread-safe)."""
    start = time.perf_counter()
    conn = None
    try:
        # Connecting and setting search_path must be inside the try: a failure
        # in either one would otherwise leak the connection and escape the
        # per-query error handling, killing the whole run over one bad query.
        conn = connect(dsn)
        if schema_name:
            conn.execute("SET search_path TO " + schema_name)
        result = agent.run(query, conn)
        return QueryResult(
            query=query,
            ok=True,
            latency_s=round(time.perf_counter() - start, 3),
            sql=result["sql"],
            answer=result["answer"],
            row_count=len(result["rows"]),
        )
    except Exception as exc:  # noqa: BLE001 - one query must not stop the run
        return QueryResult(
            query=query,
            ok=False,
            latency_s=round(time.perf_counter() - start, 3),
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if conn is not None:
            conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=CHEAP_MODEL, help=f"OpenAI model (default: {CHEAP_MODEL}).")
    parser.add_argument(
        "--dsn",
        help="PostgreSQL URL of an existing DB. Omit to use a throwaway schema.",
    )
    parser.add_argument("--query", action="append", dest="queries", help="A question (repeatable).")
    parser.add_argument("--queries-file", type=Path, help="File of questions, one per line ('#' comments allowed).")
    parser.add_argument("--workers", type=int, default=1, help="Concurrent workers (default: 1).")
    parser.add_argument("--json", action="store_true", help="Emit results as JSON instead of human-readable text.")
    args = parser.parse_args()

    load_dotenv()
    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "OPENAI_API_KEY is not set. Add it to a repo-root .env file "
            "(see .env.example) or export it, then re-run.",
            file=sys.stderr,
        )
        return 1

    queries = _load_queries(args)
    agent = SqlAiAgent(model=args.model)

    fixture_schema: str | None = None
    if not args.dsn:
        fixture_schema = _build_fixture_schema()
    target = resolve_dsn(args.dsn)

    if not args.json:
        print(f"Model: {args.model} | DB: {target} | queries: {len(queries)} | workers: {args.workers}\n")

    results: list[QueryResult] = []
    try:
        workers = max(1, min(args.workers, len(queries)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_run_one, agent, args.dsn, fixture_schema, q): q
                for q in queries
            }
            for future in as_completed(futures):
                res = future.result()
                results.append(res)
                if not args.json:
                    _print_result(res)
    finally:
        if fixture_schema is not None:
            _drop_fixture_schema(fixture_schema)

    succeeded = sum(1 for r in results if r.ok)
    if args.json:
        print(json.dumps({"results": [asdict(r) for r in results],
                          "succeeded": succeeded, "total": len(results)}, indent=2))
    else:
        avg = sum(r.latency_s for r in results) / len(results) if results else 0.0
        print(f"Done: {succeeded}/{len(results)} succeeded | avg latency {avg:.2f}s")
    return 0 if succeeded == len(results) else 1


def _print_result(res: QueryResult) -> None:
    print(f"Q: {res.query}  ({res.latency_s}s)")
    if res.ok:
        print(f"   SQL:    {res.sql}")
        print(f"   Answer: {res.answer}  [{res.row_count} row(s)]")
    else:
        print(f"   FAILED: {res.error}")
    print()


if __name__ == "__main__":
    raise SystemExit(main())
