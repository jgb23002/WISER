from __future__ import annotations

import csv
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


def parse_event_code(event_code: str) -> tuple[str, int, datetime]:
    """Parse eventCode formatted like 'CT-YYYYMMDDHH'."""
    state, raw_storm_code = event_code.split("-", maxsplit=1)
    storm_code = int(raw_storm_code)
    event_datetime = datetime.strptime(raw_storm_code, "%Y%m%d%H")
    return state, storm_code, event_datetime


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
    storm_county_rows: list[dict[str, object]] = []

    for row in rows:
        state, storm_code, event_datetime = parse_event_code(row["eventCode"])
        county = row["County"]

        storm_by_code[storm_code] = {
            "stormCode": storm_code,
            "state": state,
            "eventDateTime": event_datetime.strftime("%Y-%m-%d %H:%M:%S"),
            "eventLength": int(row["eventLength"]),
        }

        county_by_name[county] = {
            "County": county,
            "ohLine": int(float(row["ohLine"])),
        }

        storm_county_rows.append(
            {
                "stormCode": storm_code,
                "County": county,
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
    county_rows = sorted(county_by_name.values(), key=lambda item: str(item["County"]))
    storm_county_rows.sort(key=lambda item: (int(item["stormCode"]), str(item["County"])))
    return storm_rows, county_rows, storm_county_rows


def write_normalized_csvs(
    storm_rows: list[dict[str, object]],
    county_rows: list[dict[str, object]],
    storm_county_rows: list[dict[str, object]],
    output_dir: str | Path,
) -> None:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    write_rows(output_path / "storm.csv", storm_rows)
    write_rows(output_path / "county.csv", county_rows)
    write_rows(output_path / "storm_county.csv", storm_county_rows)


def write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
