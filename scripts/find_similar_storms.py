#!/usr/bin/env python3
"""
Find similar historical storms using multi-dimensional L2 Z-score similarity.

Supports any combination of the 6 weather features:
  peakWSPD10, sumAFWA_TOTPRECIP, wgt5, wgt9, wgt13, wgt18

Passing only --wind-speed produces identical results to the original
1-D wind-only algorithm.

Usage:
    python scripts/find_similar_storms.py --county Fairfield --wind-speed 9.2
    python scripts/find_similar_storms.py --county Hartford \\
        --wind-speed 6.5 --precip 30.0 --limit 10
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.agents.similarity_query_agent import SimilarityQueryAgent
from wiser.database import open_weather_db



# ---------------------------------------------------------------------------
# Multi-dimensional entry point
# ---------------------------------------------------------------------------

# Maps CLI dest name → WeatherData column name (aliasing lives only here).
_FLAG_TO_FEATURE: list[tuple[str, str]] = [
    ("wind_speed", "peakWSPD10"),
    ("precip", "sumAFWA_TOTPRECIP"),
    ("wgt5", "wgt5"),
    ("wgt9", "wgt9"),
    ("wgt13", "wgt13"),
    ("wgt18", "wgt18"),
]


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

# Human-readable labels for each feature column.
_FEATURE_LABELS: dict[str, str] = {
    "peakWSPD10": "Wind (m/s)",
    "sumAFWA_TOTPRECIP": "Precip (mm)",
    "wgt5": "Wgt5 (h)",
    "wgt9": "Wgt9 (h)",
    "wgt13": "Wgt13 (h)",
    "wgt18": "Wgt18 (h)",
}


def print_results(
    county: str,
    query_params: dict[str, float],
    results: list[dict[str, object]],
    total_historical: int = 0,
) -> None:
    """Pretty-print the multi-dimensional similarity results."""
    if not results:
        print(f"\nNo historical data found for county: {county}\n")
        return

    active_features = list(query_params.keys())

    print("\n" + "=" * 100)
    print(f"Storm Similarity Analysis for {county}")
    print("=" * 100)

    print("\nQuery Parameters:")
    for feat in active_features:
        label = _FEATURE_LABELS.get(feat, feat)
        print(f"  {label}: {query_params[feat]:.4g}")

    if total_historical:
        print(f"\nHistorical storms in {county}: {total_historical}")

    # Build dynamic column headers
    feat_headers = "  ".join(
        f"{_FEATURE_LABELS.get(f, f):<12}" for f in active_features
    )
    header = (
        f"{'Rank':<6} {'Storm Code':<15} {'Similarity':<12} "
        f"{'Outages':<10} {feat_headers} {'Event Date':<19}"
    )
    print(f"\n{header}")
    print("-" * len(header))

    for rank, result in enumerate(results, 1):
        feat_vals = "  ".join(f"{result.get(f, 0):<12.4g}" for f in active_features)
        print(
            f"{rank:<6} "
            f"{result['stormCode']:<15} "
            f"{result['similarity']:<12.1%} "
            f"{result['outage']:<10} "
            f"{feat_vals} "
            f"{result['eventDateTime']:<19}"
        )

    print("\n" + "=" * 100 + "\n")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Find historical storms similar to a current event. "
            "At least one feature flag is required."
        )
    )
    parser.add_argument(
        "--county",
        required=True,
        help="County name (e.g., Fairfield, Hartford, New Haven)",
    )
    parser.add_argument(
        "--wind-speed",
        type=float,
        default=None,
        dest="wind_speed",
        help="Current storm peak wind speed in m/s (peakWSPD10)",
    )
    parser.add_argument(
        "--precip",
        type=float,
        default=None,
        help="Total precipitation in mm (sumAFWA_TOTPRECIP)",
    )
    parser.add_argument(
        "--wgt5",
        type=float,
        default=None,
        help="Hours with wind speed > 5 m/s threshold (wgt5)",
    )
    parser.add_argument(
        "--wgt9",
        type=float,
        default=None,
        help="Hours with wind speed > 9 m/s threshold (wgt9)",
    )
    parser.add_argument(
        "--wgt13",
        type=float,
        default=None,
        help="Hours with wind speed > 13 m/s threshold (wgt13)",
    )
    parser.add_argument(
        "--wgt18",
        type=float,
        default=None,
        help="Hours with wind speed > 18 m/s threshold (wgt18)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Number of similar storms to return (default: 5)",
    )
    parser.add_argument(
        "--db",
        default="data/wiser.db",
        help="Path to SQLite database (default: data/wiser.db)",
    )

    args = parser.parse_args()

    # Build query_params from whichever feature flags were supplied.
    query_params: dict[str, float] = {}
    for flag_dest, feature_name in _FLAG_TO_FEATURE:
        val = getattr(args, flag_dest, None)
        if val is not None:
            query_params[feature_name] = float(val)

    if not query_params:
        parser.error(
            "At least one feature flag is required "
            "(e.g. --wind-speed 9.2  or  --precip 45.0)"
        )

    if "peakWSPD10" in query_params and query_params["peakWSPD10"] <= 0:
        parser.error("--wind-speed must be positive (> 0)")

    if args.limit < 1:
        parser.error("--limit must be at least 1")

    try:
        conn = open_weather_db(args.db)
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM WeatherData WHERE LOWER(county) = LOWER(?)",
                (args.county,),
            ).fetchone()[0]
            agent = SimilarityQueryAgent()
            results = agent.find_similar(
                conn, args.county, query_params, limit=args.limit
            )
        finally:
            conn.close()

        print_results(args.county, query_params, results, total_historical=total)

    except FileNotFoundError:
        print(f"\nError: Database not found at {args.db}")
        print("Please run: python scripts/bootstrap_wiser.py")
        sys.exit(1)
    except sqlite3.OperationalError as e:
        print(f"\nError: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
