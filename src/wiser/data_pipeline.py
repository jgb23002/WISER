from __future__ import annotations

import csv
import json
import re
import warnings
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

EXPECTED_COLUMNS = [
    "eventCode",
    "stormCode",
    "eventLength",
    "County",
    "ohLine",
    "sumAFWA_TOTPRECIP",
    "peakWSPD10",
    "wgt5",
    "wgt9",
    "wgt13",
    "wgt18",
    "outage",
]

HOURLY_WINDOW_HOURS = 48
HOURLY_COLUMN_PATTERN = re.compile(
    r"^(?P<metric>[A-Za-z][A-Za-z0-9_]*?)[_-]?(?:h|hr|hour)(?P<hour>[0-4]?\d)$",
    re.IGNORECASE,
)
HOURLY_METRIC_ALIASES = {
    "wind": "peakWSPD10",
    "windspeed": "peakWSPD10",
    "wind_speed": "peakWSPD10",
    "peakwspd10": "peakWSPD10",
    "precip": "sumAFWA_TOTPRECIP",
    "precipitation": "sumAFWA_TOTPRECIP",
    "rain": "sumAFWA_TOTPRECIP",
    "rainfall": "sumAFWA_TOTPRECIP",
    "humidity": "humidity",
    "rh": "humidity",
    "gust": "gust",
    "peakgust": "peakGUST10",
    "peak_gust": "peakGUST10",
    "peakgust10": "peakGUST10",
    "peak_gust10": "peakGUST10",
}
PEAK_GUST_ALIASES = (
    "peakGUST10",
    "peakGust",
    "peak_gust",
    "windGust",
    "wind_gust",
    "gust",
    "maxGust",
    "max_gust",
)
CURRENT_OBSERVED_AT_ALIASES = ("observedAt", "timestamp", "time", "dateTime", "datetime")
CURRENT_COUNTY_ALIASES = ("county", "County")
CURRENT_STATE_ALIASES = ("state", "State")
CURRENT_WIND_ALIASES = ("peakWSPD10", "wind", "windSpeed", "wind_speed")
CURRENT_PRECIP_ALIASES = (
    "sumAFWA_TOTPRECIP",
    "precip",
    "precipitation",
    "rain",
    "rainfall",
)
CURRENT_HUMIDITY_ALIASES = ("humidity", "relativeHumidity", "relative_humidity", "rh")
CURRENT_OUTAGE_ALIASES = ("outage", "outages", "outageCount", "outage_count")


def parse_event_code(event_code: str) -> tuple[int, datetime, str]:
    """Parse eventCode formatted like 'CT-YYYYMMDDHH'.

    Returns:
        tuple: (storm_code, event_datetime, state)
    """
    state, raw_storm_code = event_code.split("-", maxsplit=1)
    storm_code = int(raw_storm_code)
    event_datetime = datetime.strptime(raw_storm_code, "%Y%m%d%H")
    return storm_code, event_datetime, state


def load_raw_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        rows = list(reader)
        columns = reader.fieldnames or []
    missing = [column for column in EXPECTED_COLUMNS if column not in columns]
    if missing:
        raise ValueError(f"Missing expected columns: {missing}")
    return rows


def normalize_rows(
    rows: list[dict[str, str]],
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    storm_by_code: dict[int, dict[str, object]] = {}
    county_by_name: dict[str, dict[str, object]] = {}
    weather_data_rows: list[dict[str, object]] = []

    for row in rows:
        storm_code, event_datetime, state = parse_event_code(row["eventCode"])
        csv_storm_code = int(row["stormCode"])
        if csv_storm_code != storm_code:
            warnings.warn(
                f"stormCode {csv_storm_code} does not match eventCode-derived "
                f"stormCode {storm_code}; using eventCode value.",
                stacklevel=2,
            )

        county = row["County"]
        oh_line = int(float(row["ohLine"]))
        existing_county = county_by_name.get(county)
        if existing_county is not None and existing_county["ohLine"] != oh_line:
            warnings.warn(
                f"Inconsistent ohLine for county {county}: "
                f"{existing_county['ohLine']} vs {oh_line}; using latest value.",
                stacklevel=2,
            )

        storm_by_code[storm_code] = {
            "stormCode": storm_code,
            "eventDateTime": event_datetime.strftime("%Y-%m-%d %H:%M:%S"),
            "eventLength": int(row["eventLength"]),
        }

        county_by_name[county] = {
            "county": county,
            "state": state,
            "ohLine": oh_line,
        }

        direct_gust = _optional_float(row, PEAK_GUST_ALIASES)
        # One scan of the row's hourly columns serves both gust lookups.
        hourly_peaks = _peak_hourly_metrics(row) if direct_gust is None else {}
        weather_data_rows.append(
            {
                "stormCode": storm_code,
                "county": county,
                "sumAFWA_TOTPRECIP": float(row["sumAFWA_TOTPRECIP"]),
                "peakWSPD10": float(row["peakWSPD10"]),
                "peakGUST10": (
                    direct_gust
                    if direct_gust is not None
                    else hourly_peaks.get("peakGUST10", hourly_peaks.get("gust"))
                ),
                "wgt5": int(row["wgt5"]),
                "wgt9": int(row["wgt9"]),
                "wgt13": int(row["wgt13"]),
                "wgt18": int(row["wgt18"]),
                "outage": int(row["outage"]),
            }
        )

    storm_rows = sorted(storm_by_code.values(), key=lambda item: int(item["stormCode"]))
    county_rows = sorted(county_by_name.values(), key=lambda item: str(item["county"]))
    weather_data_rows.sort(
        key=lambda item: (int(item["stormCode"]), str(item["county"]))
    )
    return storm_rows, county_rows, weather_data_rows


def normalize_current_weather_rows(
    rows: list[dict[str, str]],
    *,
    source_system: str = "current_weather_csv",
) -> list[dict[str, object]]:
    """Normalize current/live weather rows into the staging-table shape."""
    current_rows: list[dict[str, object]] = []
    for index, row in enumerate(rows, start=1):
        observed_at = _first_present(row, CURRENT_OBSERVED_AT_ALIASES)
        county = _first_present(row, CURRENT_COUNTY_ALIASES)
        if not observed_at:
            raise ValueError(f"current weather row {index} is missing observedAt")
        source_record_id = (
            _first_present(row, ("sourceRecordId", "recordId", "id"))
            or f"{source_system}:{index}"
        )
        current_rows.append(
            {
                "sourceSystem": source_system,
                "sourceRecordId": source_record_id,
                "observedAt": _normalize_observed_at(observed_at),
                "county": county or None,
                "state": _first_present(row, CURRENT_STATE_ALIASES) or "CT",
                "peakWSPD10": _optional_float(row, CURRENT_WIND_ALIASES),
                "peakGUST10": _optional_float(row, PEAK_GUST_ALIASES),
                "sumAFWA_TOTPRECIP": _optional_float(row, CURRENT_PRECIP_ALIASES),
                "humidity": _optional_float(row, CURRENT_HUMIDITY_ALIASES),
                "outage": _optional_int(row, CURRENT_OUTAGE_ALIASES),
                "rawPayload": json.dumps(row, sort_keys=True),
            }
        )
    current_rows.sort(
        key=lambda item: (
            str(item["observedAt"]),
            str(item["county"] or ""),
            str(item["sourceRecordId"]),
        )
    )
    return current_rows


def normalize_hourly_rows(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    """Normalize future wide hourly source columns into long metric rows.

    The sponsor-mentioned hourly data is expected to carry 48 values per storm
    window. Source columns can be named like ``wind_h00``, ``gust_hour12``, or
    ``humidity_hr47``. Unknown metric prefixes are preserved as lowercase keys
    so mock-data columns can flow into the hourly table without a code change.
    """
    hourly_rows: list[dict[str, object]] = []
    for row in rows:
        storm_code, event_datetime, _state = parse_event_code(row["eventCode"])
        county = row["County"]
        for column, raw_value in row.items():
            if raw_value in (None, ""):
                continue
            match = HOURLY_COLUMN_PATTERN.match(column)
            if not match:
                continue
            hour_offset = int(match.group("hour"))
            if not 0 <= hour_offset < HOURLY_WINDOW_HOURS:
                continue
            metric_key = _canonical_hourly_metric(match.group("metric"))
            hourly_rows.append(
                {
                    "stormCode": storm_code,
                    "county": county,
                    "hourOffset": hour_offset,
                    "metricKey": metric_key,
                    "metricValue": float(raw_value),
                    "observedAt": _hourly_timestamp(event_datetime, hour_offset),
                    "sourceColumn": column,
                }
            )
    hourly_rows.sort(
        key=lambda item: (
            int(item["stormCode"]),
            str(item["county"]),
            int(item["hourOffset"]),
            str(item["metricKey"]),
        )
    )
    return hourly_rows


# The column set repeats for every row of the CSV, so the pattern match and
# the alias normalization are memoized on the column name rather than redone
# per row. maxsize covers a wide hourly file (48 hours x several metrics).
@lru_cache(maxsize=4096)
def _hourly_column_metric(column: str) -> str | None:
    """The canonical metric an hourly column names, or None if it is not one."""
    match = HOURLY_COLUMN_PATTERN.match(column)
    if not match:
        return None
    return _canonical_hourly_metric(match.group("metric"))


@lru_cache(maxsize=4096)
def _canonical_hourly_metric(raw: str) -> str:
    compact = raw.lower().replace("-", "_")
    compact = re.sub(r"_+", "_", compact).strip("_")
    return HOURLY_METRIC_ALIASES.get(compact, compact)


def _peak_hourly_metrics(row: dict[str, str]) -> dict[str, float]:
    """Peak value per hourly metric in one pass over the row's columns.

    Callers previously asked for one metric at a time, walking every column
    of every row once per metric.
    """
    peaks: dict[str, float] = {}
    for column, raw_value in row.items():
        if raw_value in (None, ""):
            continue
        metric_key = _hourly_column_metric(column)
        if metric_key is None:
            continue
        value = float(raw_value)
        current = peaks.get(metric_key)
        if current is None or value > current:
            peaks[metric_key] = value
    return peaks


def _peak_hourly_metric(row: dict[str, str], metric_alias: str) -> float | None:
    return _peak_hourly_metrics(row).get(metric_alias)


def _first_present(row: dict[str, str], aliases: tuple[str, ...]) -> str | None:
    for alias in aliases:
        value = row.get(alias)
        if value not in (None, ""):
            return str(value)
    return None


def _optional_float(row: dict[str, str], aliases: tuple[str, ...]) -> float | None:
    value = _first_present(row, aliases)
    return float(value) if value not in (None, "") else None


def _optional_int(row: dict[str, str], aliases: tuple[str, ...]) -> int | None:
    value = _first_present(row, aliases)
    return int(float(value)) if value not in (None, "") else None


def _normalize_observed_at(raw: str) -> str:
    text = str(raw).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return text


def _hourly_timestamp(event_datetime: datetime, hour_offset: int) -> str:
    return (event_datetime + timedelta(hours=hour_offset)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def write_normalized_csvs(
    storm_rows: list[dict[str, object]],
    county_rows: list[dict[str, object]],
    weather_data_rows: list[dict[str, object]],
    output_dir: str | Path,
) -> None:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    write_rows(output_path / "storm.csv", storm_rows)
    write_rows(output_path / "county.csv", county_rows)
    write_rows(output_path / "weather_data.csv", weather_data_rows)


def write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
