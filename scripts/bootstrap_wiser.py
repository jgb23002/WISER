#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.agents.data_retrieval_processing_agent import DataRetrievalProcessingAgent
from wiser.agents.storm_weather_analytics_agent import StormWeatherAnalyticsAgent
from wiser.database import connect, get_table_counts, resolve_dsn


def _row_counts(conn) -> dict[str, int]:
    """Row counts for every base table present in the target schema."""
    tables = [
        str(row[0])
        for row in conn.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'
            """
        ).fetchall()
    ]
    counts: dict[str, int] = {}
    for table in tables:
        counts[table] = int(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )
    return counts


def _guard_existing_data(dsn: str | None, *, force: bool) -> None:
    """Refuse to wipe a populated database unless --force is given.

    Under SQLite the blast radius of a stray bootstrap was one local file
    that regenerates in seconds. --dsn now accepts any PostgreSQL URL and
    resolve_dsn() falls back to $DATABASE_URL, so the same command can drop
    every table on a shared server. sql/schema.sql opens with DROP TABLE.

    This guards the *accidental* path only. handlers.handle_data_retrieval
    reaches the same DROP through an explicit "rebuild the database"
    request, which is a deliberate instruction rather than a typo, so it is
    intentionally not gated here.
    """
    if force:
        return
    try:
        conn = connect(dsn)
    except Exception:
        return  # nothing reachable to protect; let the real error surface later
    try:
        # Count every table, not just the three core ones: a server holding
        # only staged external-outage rows or a built RAG index is still
        # populated, and that is exactly the state a partially-built schema is
        # in. Checking has_required_tables() first would skip the guard in
        # precisely that case.
        counts = _row_counts(conn)
    finally:
        conn.close()

    if not any(counts.values()):
        return

    populated = ", ".join(
        f"{name}={count}" for name, count in counts.items() if count
    )
    raise SystemExit(
        f"Refusing to rebuild: {resolve_dsn(dsn)} already holds data "
        f"({populated}).\n"
        "Bootstrapping drops and recreates every table. Re-run with --force "
        "if that is what you want."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap WISER starter artifacts.")
    parser.add_argument(
        "--input",
        default="data/testdata.csv",
        help="Path to raw WISER CSV (default: data/testdata.csv).",
    )
    parser.add_argument(
        "--output-dir",
        default="data/normalized",
        help="Directory for normalized CSV outputs.",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help=(
            "PostgreSQL connection URL to build from normalized data. "
            "Defaults to $DATABASE_URL, then the local Docker Compose instance."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Rebuild even when the target already holds data. sql/schema.sql "
            "starts with DROP TABLE, so bootstrapping wipes whatever is there."
        ),
    )
    args = parser.parse_args()

    _guard_existing_data(args.dsn, force=args.force)

    data_agent = DataRetrievalProcessingAgent(args.input)
    storm_path, county_path, weather_data_path = data_agent.save_normalized(
        args.output_dir
    )

    print("Normalized files written:")
    print(f"  - {storm_path}")
    print(f"  - {county_path}")
    print(f"  - {weather_data_path}")
    print()

    conn = data_agent.build_database(args.dsn)
    table_counts = get_table_counts(conn)

    analytics_agent = StormWeatherAnalyticsAgent()
    summary = analytics_agent.county_outage_summary_from_db(conn, limit=5)
    conn.close()

    print("Top 5 counties by total outages:")
    for row in summary:
        print(
            f"  - {row['county']}: total={row['total_outage']}, "
            f"avg={row['avg_outage']}, peak={row['peak_outage']}, "
            f"storms={row['storms_observed']}"
        )
    print()
    print(f"PostgreSQL database built: {resolve_dsn(args.dsn)}")
    print("Table counts:")
    for table_name, count in table_counts.items():
        print(f"  - {table_name}: {count}")


if __name__ == "__main__":
    main()
