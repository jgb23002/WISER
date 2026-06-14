from __future__ import annotations

import csv
import warnings
from datetime import datetime
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

        weather_data_rows.append(
            {
                "stormCode": storm_code,
                "county": county,
                "sumAFWA_TOTPRECIP": float(row["sumAFWA_TOTPRECIP"]),
                "peakWSPD10": float(row["peakWSPD10"]),
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
