#!/usr/bin/env python3
"""
Find similar historical storms based on peak wind speed (peakWSPD10).

Uses 1D statistical distance (Z-score) to rank historical storms by similarity
to a current storm event in a specific county.

Usage:
    python scripts/find_similar_storms.py --county Fairfield --wind-speed 9.2
    python scripts/find_similar_storms.py --county Hartford --wind-speed 6.5 --limit 10
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

from wiser.database import connect


def fetch_historical_storms(
    conn: sqlite3.Connection,
    county: str,
    limit_fetch: int = 1000,
) -> list[dict[str, object]]:
    """
    Fetch historical storm weather data for a county.

    Args:
        conn: Database connection
        county: County name (case-insensitive)
        limit_fetch: Max number of historical records to fetch

    Returns:
        List of dicts with stormCode, peakWSPD10, sumAFWA_TOTPRECIP, outage
    """
    cursor = conn.execute(
        """
        SELECT
            w.stormCode,
            w.peakWSPD10,
            w.sumAFWA_TOTPRECIP,
            w.outage,
            s.eventDateTime,
            s.eventLength
        FROM WeatherData w
        JOIN Storm s ON s.stormCode = w.stormCode
        WHERE LOWER(w.county) = LOWER(?)
        ORDER BY s.eventDateTime DESC
        LIMIT ?
        """,
        (county, limit_fetch),
    )

    rows = cursor.fetchall()
    return [
        {
            "stormCode": int(row[0]),
            "peakWSPD10": float(row[1]),
            "sumAFWA_TOTPRECIP": float(row[2]),
            "outage": int(row[3]),
            "eventDateTime": row[4],
            "eventLength": int(row[5]),
        }
        for row in rows
    ]


def compute_statistics(wind_speeds: list[float]) -> tuple[float, float, float, float]:
    """
    Compute mean, standard deviation, min, and max for wind speeds.

    Uses Bessel's correction (n-1) for sample standard deviation.

    Args:
        wind_speeds: List of peakWSPD10 values

    Returns:
        Tuple of (mean, std_dev, min_val, max_val)
    """
    if not wind_speeds:
        return 0.0, 0.0, 0.0, 0.0

    n = len(wind_speeds)
    mean = sum(wind_speeds) / n

    if n < 2:
        return mean, 0.0, min(wind_speeds), max(wind_speeds)

    variance = sum((x - mean) ** 2 for x in wind_speeds) / (n - 1)
    std_dev = math.sqrt(variance)

    return mean, std_dev, min(wind_speeds), max(wind_speeds)


def calculate_similarity(
    historical_wind: float,
    current_wind: float,
    mean: float,
    std_dev: float,
) -> tuple[float, float, float]:
    """
    Calculate similarity between current and historical storm.

    Uses Z-score (standardized distance) to measure similarity.

    Similarity = 1 / (1 + normalized_distance)
    - 1.0 = identical wind speed
    - 0.5 = 1 standard deviation away
    - 0.33 = 2 standard deviations away

    Args:
        historical_wind: Historical peakWSPD10
        current_wind: Current peakWSPD10
        mean: Mean of historical wind speeds
        std_dev: Standard deviation of historical wind speeds

    Returns:
        Tuple of (similarity_score, z_historical, z_current)
    """
    if std_dev == 0:
        # All historical values are identical
        if historical_wind == current_wind:
            return 1.0, 0.0, 0.0
        else:
            return 0.0, 0.0, 0.0

    z_historical = (historical_wind - mean) / std_dev
    z_current = (current_wind - mean) / std_dev
    distance = abs(z_historical - z_current)
    similarity = 1.0 / (1.0 + distance)

    return similarity, z_historical, z_current


def find_similar_storms(
    county: str,
    current_wind_speed: float,
    db_path: str | Path = "data/wiser.db",
    limit: int = 5,
) -> list[dict[str, object]]:
    """
    Find top N historical storms in a county with similar wind speeds.

    Args:
        county: County name
        current_wind_speed: Current storm peakWSPD10 (m/s)
        db_path: Path to SQLite database
        limit: Number of top matches to return

    Returns:
        List of similar storms ranked by similarity score
    """
    conn = connect(db_path)

    try:
        # Fetch historical data
        historical = fetch_historical_storms(conn, county)

        if not historical:
            return []

        # Compute statistics
        wind_speeds = [row["peakWSPD10"] for row in historical]
        mean, std_dev, min_wind, max_wind = compute_statistics(wind_speeds)

        # Calculate similarity for each historical storm
        results = []
        z_current = (current_wind_speed - mean) / std_dev if std_dev > 0 else 0.0

        for row in historical:
            similarity, z_historical, _ = calculate_similarity(
                row["peakWSPD10"],
                current_wind_speed,
                mean,
                std_dev,
            )

            results.append(
                {
                    "stormCode": row["stormCode"],
                    "peakWSPD10": row["peakWSPD10"],
                    "eventDateTime": row["eventDateTime"],
                    "sumAFWA_TOTPRECIP": row["sumAFWA_TOTPRECIP"],
                    "outage": row["outage"],
                    "similarity": similarity,
                    "z_score": z_historical,
                    "eventLength": row["eventLength"],
                }
            )

        # Sort by similarity (descending) and return top N
        results.sort(key=lambda x: x["similarity"], reverse=True)

        # Add statistics to the first result dict as metadata
        if results:
            results[0]["_statistics"] = {
                "mean": mean,
                "std_dev": std_dev,
                "min_wind": min_wind,
                "max_wind": max_wind,
                "z_current": z_current,
                "total_historical": len(historical),
            }

        return results[:limit]

    finally:
        conn.close()


def print_results(
    county: str,
    current_wind: float,
    results: list[dict[str, object]],
) -> None:
    """Pretty-print the similarity results."""
    if not results:
        print(f"\nNo historical data found for county: {county}\n")
        return

    # Extract and print statistics
    stats = results[0].pop("_statistics", {})

    print("\n" + "=" * 90)
    print(f"Storm Similarity Analysis for {county}")
    print("=" * 90)
    print(f"\nCurrent Storm:")
    print(f"  Peak Wind Speed (peakWSPD10): {current_wind:.2f} m/s")
    print(
        f"  Z-Score: {stats.get('z_current', 0):.2f} (standardized distance from mean)"
    )

    print(f"\nHistorical Data Summary ({county}):")
    print(f"  Total Historical Storms: {stats.get('total_historical', 0)}")
    print(f"  Mean Wind Speed: {stats.get('mean', 0):.2f} m/s")
    print(f"  Std Dev: {stats.get('std_dev', 0):.2f} m/s")
    print(
        f"  Range: {stats.get('min_wind', 0):.2f} - {stats.get('max_wind', 0):.2f} m/s"
    )

    print(
        f"\n{'Rank':<6} {'Storm Code':<15} {'Wind (m/s)':<12} {'Z-Score':<10} {'Similarity':<12} {'Outages':<10} {'Precip (mm)':<12} {'Event Date':<19}"
    )
    print("-" * 90)

    for rank, result in enumerate(results, 1):
        print(
            f"{rank:<6} "
            f"{result['stormCode']:<15} "
            f"{result['peakWSPD10']:<12.2f} "
            f"{result['z_score']:<10.2f} "
            f"{result['similarity']:<12.1%} "
            f"{result['outage']:<10} "
            f"{result['sumAFWA_TOTPRECIP']:<12.2f} "
            f"{result['eventDateTime']:<19}"
        )

    print("\n" + "=" * 90 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find historical storms similar to a current event based on peak wind speed."
    )
    parser.add_argument(
        "--county",
        required=True,
        help="County name (e.g., Fairfield, Hartford, New Haven)",
    )
    parser.add_argument(
        "--wind-speed",
        type=float,
        required=True,
        help="Current storm peak wind speed in m/s (e.g., 9.2)",
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

    if args.wind_speed <= 0:
        print("Error: wind-speed must be positive (> 0)")
        sys.exit(1)

    if args.limit < 1:
        print("Error: limit must be at least 1")
        sys.exit(1)

    try:
        results = find_similar_storms(
            county=args.county,
            current_wind_speed=args.wind_speed,
            db_path=args.db,
            limit=args.limit,
        )
        print_results(args.county, args.wind_speed, results)

    except FileNotFoundError:
        print(f"\nError: Database not found at {args.db}")
        print("Please run: python scripts/bootstrap_wiser.py")
        sys.exit(1)
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
