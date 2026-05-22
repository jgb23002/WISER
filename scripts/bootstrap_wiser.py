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
from wiser.database import get_table_counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap WISER starter artifacts.")
    parser.add_argument(
        "--input",
        default="testdata.csv",
        help="Path to raw WISER CSV (default: testdata.csv).",
    )
    parser.add_argument(
        "--output-dir",
        default="data/normalized",
        help="Directory for normalized CSV outputs.",
    )
    parser.add_argument(
        "--db",
        default="data/wiser.db",
        help="SQLite database file to build from normalized data.",
    )
    args = parser.parse_args()

    data_agent = DataRetrievalProcessingAgent(args.input)
    storm_path, county_path, storm_county_path = data_agent.save_normalized(args.output_dir)

    analytics_agent = StormWeatherAnalyticsAgent(data_agent)
    summary = analytics_agent.county_outage_summary()[:5]
    conn = data_agent.build_database(args.db)
    table_counts = get_table_counts(conn)
    conn.close()

    print("Normalized files written:")
    print(f"  - {storm_path}")
    print(f"  - {county_path}")
    print(f"  - {storm_county_path}")
    print()
    print("Top 5 counties by total outages:")
    for row in summary:
        print(
            f"  - {row['County']}: total={row['total_outage']}, "
            f"avg={row['avg_outage']}, peak={row['peak_outage']}, "
            f"storms={row['storms_observed']}"
        )
    print()
    print(f"SQLite database built: {args.db}")
    print("Table counts:")
    for table_name, count in table_counts.items():
        print(f"  - {table_name}: {count}")


if __name__ == "__main__":
    main()
