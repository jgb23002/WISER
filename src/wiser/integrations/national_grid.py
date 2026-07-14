from __future__ import annotations

from pathlib import Path

from wiser.integrations.external_outage import (
    DEFAULT_SOURCE_TIMEZONE,
    ExternalOutageObservation,
    parse_csv_observations,
)

SOURCE_SYSTEM = "National Grid"

NATIONAL_GRID_ALIASES = {
    "source_record_id": ("outage_id", "incident_id", "ngrid_id"),
    "observed_at": ("reported_time", "last_updated", "snapshot_time"),
    "county": ("county", "county_name"),
    "service_territory": ("service_area", "region", "division"),
    "outage_count": ("active_outages", "incidents", "outage_count"),
    "customer_count": ("customers_out", "customers_affected", "cust_affected"),
    "restoration_status": ("etr", "estimated_restoration", "status"),
}


def parse_national_grid_csv(
    path: str | Path,
    *,
    default_timezone: str = DEFAULT_SOURCE_TIMEZONE,
) -> list[ExternalOutageObservation]:
    return parse_csv_observations(
        path,
        source_system=SOURCE_SYSTEM,
        aliases=NATIONAL_GRID_ALIASES,
        default_timezone=default_timezone,
    )
