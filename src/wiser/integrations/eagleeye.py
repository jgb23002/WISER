from __future__ import annotations

from pathlib import Path

from wiser.integrations.external_outage import (
    DEFAULT_SOURCE_TIMEZONE,
    ExternalOutageObservation,
    parse_csv_observations,
)

SOURCE_SYSTEM = "EagleEye"

EAGLEEYE_ALIASES = {
    "source_record_id": ("eagleeye_id", "event_id", "case_id"),
    "observed_at": ("observation_time", "event_time", "last_seen"),
    "county": ("county_name", "county"),
    "service_territory": ("operating_area", "region", "district"),
    "outage_count": ("trouble_spots", "outage_locations", "open_tickets"),
    "customer_count": ("customers_affected", "customers_out"),
    "restoration_status": ("crew_status", "status", "restoration_status"),
}


def parse_eagleeye_csv(
    path: str | Path,
    *,
    default_timezone: str = DEFAULT_SOURCE_TIMEZONE,
) -> list[ExternalOutageObservation]:
    return parse_csv_observations(
        path,
        source_system=SOURCE_SYSTEM,
        aliases=EAGLEEYE_ALIASES,
        default_timezone=default_timezone,
    )
