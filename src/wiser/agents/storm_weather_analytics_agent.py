from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from wiser.agents.data_retrieval_processing_agent import DataRetrievalProcessingAgent


@dataclass
class StormWeatherAnalyticsAgent:
    data_agent: DataRetrievalProcessingAgent

    def county_outage_summary(self) -> list[dict[str, object]]:
        _, _, storm_county_rows = self.data_agent.normalize()

        stats: dict[str, dict[str, float]] = defaultdict(
            lambda: {
                "storms_observed": 0.0,
                "total_outage": 0.0,
                "peak_outage": 0.0,
            }
        )

        for row in storm_county_rows:
            county = str(row["County"])
            outage = float(row["outage"])
            stats[county]["storms_observed"] += 1
            stats[county]["total_outage"] += outage
            stats[county]["peak_outage"] = max(stats[county]["peak_outage"], outage)

        summary: list[dict[str, object]] = []
        for county, values in stats.items():
            storms_observed = int(values["storms_observed"])
            total_outage = int(values["total_outage"])
            avg_outage = round(values["total_outage"] / storms_observed, 2)
            peak_outage = int(values["peak_outage"])
            summary.append(
                {
                    "County": county,
                    "storms_observed": storms_observed,
                    "total_outage": total_outage,
                    "avg_outage": avg_outage,
                    "peak_outage": peak_outage,
                }
            )

        summary.sort(key=lambda row: int(row["total_outage"]), reverse=True)
        return summary

    def top_storms_by_outage(self, limit: int = 10) -> list[dict[str, object]]:
        storm_rows, _, storm_county_rows = self.data_agent.normalize()

        outage_by_storm: dict[int, int] = defaultdict(int)
        for row in storm_county_rows:
            storm_code = int(row["stormCode"])
            outage_by_storm[storm_code] += int(row["outage"])

        storm_lookup = {
            int(row["stormCode"]): row for row in storm_rows
        }
        sorted_storms = sorted(outage_by_storm.items(), key=lambda item: item[1], reverse=True)[:limit]

        results: list[dict[str, object]] = []
        for storm_code, total_outage in sorted_storms:
            storm = storm_lookup.get(storm_code, {})
            results.append(
                {
                    "stormCode": storm_code,
                    "total_outage": total_outage,
                    "state": storm.get("state"),
                    "eventDateTime": storm.get("eventDateTime"),
                    "eventLength": storm.get("eventLength"),
                }
            )
        return results
