from __future__ import annotations

import psycopg

from wiser.database import executemany
import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo


DEFAULT_SOURCE_TIMEZONE = "America/New_York"


@dataclass(frozen=True)
class ExternalOutageObservation:
    source_system: str
    source_record_id: str
    observed_at: str
    county: str | None
    service_territory: str | None
    outage_count: int | None
    customer_count: int | None
    restoration_status: str | None
    raw_payload: str


@dataclass(frozen=True)
class ExternalLoadSummary:
    source_system: str
    rows_seen: int
    rows_loaded: int
    table_count: int
    # Existing rows refreshed by an upsert rather than newly inserted.
    rows_updated: int = 0


CSV_ALIASES: dict[str, tuple[str, ...]] = {
    "source_record_id": (
        "source_record_id",
        "record_id",
        "event_id",
        "incident_id",
        "outage_id",
        "id",
        "ticket",
    ),
    "observed_at": (
        "observed_at",
        "observed_time",
        "timestamp",
        "time",
        "date_time",
        "datetime",
        "reported_at",
        "reported_time",
        "last_updated",
        "updated_at",
    ),
    "county": (
        "county",
        "county_name",
        "municipality_county",
    ),
    "service_territory": (
        "service_territory",
        "territory",
        "region",
        "area",
        "district",
        "division",
    ),
    "outage_count": (
        "outage_count",
        "outages",
        "trouble_spots",
        "trouble_spot_count",
        "incidents",
        "incident_count",
        "active_outages",
    ),
    "customer_count": (
        "customer_count",
        "customers",
        "customers_affected",
        "affected_customers",
        "cust_affected",
        "customers_out",
    ),
    "restoration_status": (
        "restoration_status",
        "status",
        "etr",
        "estimated_restoration",
        "restoration",
    ),
}


def ensure_external_outage_schema(conn: psycopg.Connection) -> None:
    # psycopg runs the multi-statement DDL directly when no params are passed.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ExternalOutageObservation (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            sourceSystem TEXT NOT NULL,
            sourceRecordId TEXT NOT NULL,
            observedAt TIMESTAMP NOT NULL,
            county VARCHAR(64),
            serviceTerritory TEXT,
            outageCount INTEGER,
            customerCount INTEGER,
            restorationStatus TEXT,
            rawPayload TEXT NOT NULL,
            UNIQUE (sourceSystem, sourceRecordId, observedAt),
            CHECK (outageCount IS NULL OR outageCount >= 0),
            CHECK (customerCount IS NULL OR customerCount >= 0)
        );

        CREATE INDEX IF NOT EXISTS idx_external_outage_source_time
            ON ExternalOutageObservation(sourceSystem, observedAt DESC);
        CREATE INDEX IF NOT EXISTS idx_external_outage_county
            ON ExternalOutageObservation(county);
        """
    )
    conn.commit()


def parse_csv_observations(
    path: str | Path,
    *,
    source_system: str,
    aliases: Mapping[str, Sequence[str]] | None = None,
    default_timezone: str = DEFAULT_SOURCE_TIMEZONE,
) -> list[ExternalOutageObservation]:
    alias_map = _merge_aliases(aliases)
    source_path = Path(path)
    with source_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise ValueError(f"{source_path} has no CSV header row.")

        observations = []
        for row_number, row in enumerate(reader, start=2):
            try:
                observations.append(
                    observation_from_csv_row(
                        row,
                        source_system=source_system,
                        aliases=alias_map,
                        default_timezone=default_timezone,
                    )
                )
            except ValueError as exc:
                raise ValueError(f"{source_path}:{row_number}: {exc}") from exc
        return observations


def observation_from_csv_row(
    row: Mapping[str, str | None],
    *,
    source_system: str,
    aliases: Mapping[str, Sequence[str]] | None = None,
    default_timezone: str = DEFAULT_SOURCE_TIMEZONE,
) -> ExternalOutageObservation:
    alias_map = _merge_aliases(aliases)
    normalized_row = {_normalize_key(key): value for key, value in row.items()}
    raw_payload = json.dumps(dict(row), sort_keys=True)

    observed_at_raw = _pick_value(normalized_row, alias_map["observed_at"])
    if not observed_at_raw:
        raise ValueError("missing required observed_at/timestamp field")

    observed_at = normalize_timestamp(
        observed_at_raw,
        default_timezone=default_timezone,
    )
    source_record_id = _pick_value(
        normalized_row, alias_map["source_record_id"]
    ) or _generated_source_record_id(source_system, observed_at, raw_payload)

    outage_count = parse_optional_int(
        _pick_value(normalized_row, alias_map["outage_count"]),
        field_name="outage_count",
    )
    customer_count = parse_optional_int(
        _pick_value(normalized_row, alias_map["customer_count"]),
        field_name="customer_count",
    )

    return ExternalOutageObservation(
        source_system=source_system,
        source_record_id=source_record_id,
        observed_at=observed_at,
        county=normalize_county(_pick_value(normalized_row, alias_map["county"])),
        service_territory=_empty_to_none(
            _pick_value(normalized_row, alias_map["service_territory"])
        ),
        outage_count=outage_count,
        customer_count=customer_count,
        restoration_status=_empty_to_none(
            _pick_value(normalized_row, alias_map["restoration_status"])
        ),
        raw_payload=raw_payload,
    )


def load_external_observations(
    conn: psycopg.Connection,
    observations: Sequence[ExternalOutageObservation],
) -> ExternalLoadSummary:
    ensure_external_outage_schema(conn)
    if not observations:
        # Without a batch there is no source to scope a count to; reporting the
        # table-wide total here would attribute every other source's rows to it.
        return ExternalLoadSummary(
            source_system="",
            rows_seen=0,
            rows_loaded=0,
            table_count=0,
        )

    # Counted table-wide so a mixed-source batch still yields a correct insert
    # delta; only table_count is scoped to the batch's source.
    count_before = external_observation_count(conn)
    executemany(
            conn,
        """
        INSERT INTO ExternalOutageObservation(
            sourceSystem,
            sourceRecordId,
            observedAt,
            county,
            serviceTerritory,
            outageCount,
            customerCount,
            restorationStatus,
            rawPayload
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(sourceSystem, sourceRecordId, observedAt) DO UPDATE SET
            county = excluded.county,
            serviceTerritory = excluded.serviceTerritory,
            outageCount = excluded.outageCount,
            customerCount = excluded.customerCount,
            restorationStatus = excluded.restorationStatus,
            rawPayload = excluded.rawPayload
        """,
        [
            (
                observation.source_system,
                observation.source_record_id,
                observation.observed_at,
                observation.county,
                observation.service_territory,
                observation.outage_count,
                observation.customer_count,
                observation.restoration_status,
                observation.raw_payload,
            )
            for observation in observations
        ],
    )
    conn.commit()

    source_system = observations[0].source_system
    # Rows the upsert actually added; a pure-duplicate batch inserts nothing.
    rows_inserted = external_observation_count(conn) - count_before
    distinct_keys = len(
        {
            (
                observation.source_system,
                observation.source_record_id,
                observation.observed_at,
            )
            for observation in observations
        }
    )
    return ExternalLoadSummary(
        source_system=source_system,
        rows_seen=len(observations),
        rows_loaded=rows_inserted,
        rows_updated=distinct_keys - rows_inserted,
        table_count=external_observation_count(conn, source_system=source_system),
    )


def external_observation_count(
    conn: psycopg.Connection,
    *,
    source_system: str | None = None,
) -> int:
    ensure_external_outage_schema(conn)
    if source_system:
        return int(
            conn.execute(
                """
                SELECT COUNT(*) FROM ExternalOutageObservation
                WHERE sourceSystem = %s
                """,
                (source_system,),
            ).fetchone()[0]
        )
    return int(
        conn.execute("SELECT COUNT(*) FROM ExternalOutageObservation").fetchone()[0]
    )


def recent_external_observations(
    conn: psycopg.Connection,
    *,
    source_system: str | None = None,
    limit: int = 10,
) -> list[dict[str, object]]:
    ensure_external_outage_schema(conn)
    sql = """
        SELECT
            sourceSystem,
            sourceRecordId,
            observedAt,
            county,
            serviceTerritory,
            outageCount,
            customerCount,
            restorationStatus
        FROM ExternalOutageObservation
    """
    params: tuple[object, ...]
    if source_system:
        sql += " WHERE sourceSystem = %s"
        params = (source_system, limit)
    else:
        params = (limit,)
    sql += " ORDER BY observedAt DESC LIMIT %s"
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "sourceSystem": row[0],
            "sourceRecordId": row[1],
            "observedAt": row[2],
            "county": row[3],
            "serviceTerritory": row[4],
            "outageCount": row[5],
            "customerCount": row[6],
            "restorationStatus": row[7],
        }
        for row in rows
    ]


def normalize_timestamp(
    value: str,
    *,
    default_timezone: str = DEFAULT_SOURCE_TIMEZONE,
) -> str:
    text = value.strip()
    if not text:
        raise ValueError("timestamp is empty")

    text = text.replace("Z", "+00:00")
    parsed: datetime | None = None
    for candidate in (
        text,
        text.replace(" ", "T", 1),
    ):
        try:
            parsed = datetime.fromisoformat(candidate)
            break
        except ValueError:
            continue

    if parsed is None:
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%m/%d/%Y %H:%M",
            "%m/%d/%Y %I:%M %p",
        ):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        raise ValueError(f"invalid timestamp: {value!r}")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(default_timezone))
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_optional_int(value: str | None, *, field_name: str) -> int | None:
    text = _empty_to_none(value)
    if text is None:
        return None
    try:
        parsed = int(float(text.replace(",", "")))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be numeric: {value!r}") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} must be non-negative: {value!r}")
    return parsed


def normalize_county(value: str | None) -> str | None:
    text = _empty_to_none(value)
    if text is None:
        return None
    text = text.lower().replace(" county", "")
    return " ".join(word.capitalize() for word in text.split())


def _merge_aliases(
    aliases: Mapping[str, Sequence[str]] | None,
) -> dict[str, tuple[str, ...]]:
    merged = {key: tuple(value) for key, value in CSV_ALIASES.items()}
    if aliases:
        for canonical, source_aliases in aliases.items():
            merged[canonical] = tuple(source_aliases) + merged.get(canonical, ())
    return {
        canonical: tuple(_normalize_key(alias) for alias in source_aliases)
        for canonical, source_aliases in merged.items()
    }


def _pick_value(
    normalized_row: Mapping[str, str | None],
    aliases: Iterable[str],
) -> str | None:
    for alias in aliases:
        value = normalized_row.get(alias)
        if _empty_to_none(value) is not None:
            return value.strip() if value is not None else None
    return None


def _normalize_key(key: str) -> str:
    return "".join(char.lower() for char in key if char.isalnum())


def _empty_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text if text else None


def _generated_source_record_id(
    source_system: str,
    observed_at: str,
    raw_payload: str,
) -> str:
    digest = hashlib.sha256(
        f"{source_system}|{observed_at}|{raw_payload}".encode("utf-8")
    ).hexdigest()
    return f"generated:{digest[:16]}"
