"""Tests for CSV normalization and state extraction."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.data_pipeline import (  # noqa: E402
    normalize_current_weather_rows,
    normalize_hourly_rows,
    normalize_rows,
)


class TestNormalizeRowsState(unittest.TestCase):
    def test_event_code_extracts_state_into_county_dict(self):
        """eventCode CT-2005100800 must produce state == 'CT' in county rows."""
        sample_row = {
            "eventCode": "CT-2005100800",
            "stormCode": "2005100800",
            "eventLength": "12",
            "County": "Fairfield",
            "ohLine": "1500",
            "sumAFWA_TOTPRECIP": "25.5",
            "peakWSPD10": "9.2",
            "wgt5": "4",
            "wgt9": "2",
            "wgt13": "1",
            "wgt18": "0",
            "outage": "120",
        }

        _, county_rows, _ = normalize_rows([sample_row])

        self.assertEqual(len(county_rows), 1)
        self.assertEqual(county_rows[0]["county"], "Fairfield")
        self.assertEqual(county_rows[0]["state"], "CT")
        self.assertEqual(county_rows[0]["ohLine"], 1500)

    def test_future_hourly_columns_normalize_to_long_rows(self):
        sample_row = {
            "eventCode": "CT-2005100800",
            "stormCode": "2005100800",
            "eventLength": "48",
            "County": "Fairfield",
            "ohLine": "1500",
            "sumAFWA_TOTPRECIP": "25.5",
            "peakWSPD10": "9.2",
            "wgt5": "4",
            "wgt9": "2",
            "wgt13": "1",
            "wgt18": "0",
            "outage": "120",
            "wind_h00": "8.5",
            "humidity_hour47": "84",
        }

        hourly_rows = normalize_hourly_rows([sample_row])

        self.assertEqual(len(hourly_rows), 2)
        self.assertEqual(hourly_rows[0]["metricKey"], "peakWSPD10")
        self.assertEqual(hourly_rows[0]["hourOffset"], 0)
        self.assertEqual(hourly_rows[0]["observedAt"], "2005-10-08 00:00:00")
        self.assertEqual(hourly_rows[1]["metricKey"], "humidity")
        self.assertEqual(hourly_rows[1]["hourOffset"], 47)
        self.assertEqual(hourly_rows[1]["observedAt"], "2005-10-09 23:00:00")

    def test_peak_gust_is_loaded_from_direct_or_hourly_columns(self):
        direct_row = _sample_row(peakGUST10="18.5")
        hourly_row = _sample_row(eventCode="CT-2005100900", stormCode="2005100900")
        hourly_row["gust_h00"] = "11.0"
        hourly_row["gust_h01"] = "14.0"

        _, _, weather_rows = normalize_rows([direct_row, hourly_row])
        by_storm = {row["stormCode"]: row for row in weather_rows}

        self.assertEqual(by_storm[2005100800]["peakGUST10"], 18.5)
        self.assertEqual(by_storm[2005100900]["peakGUST10"], 14.0)

    def test_current_weather_rows_normalize_for_staging_table(self):
        rows = normalize_current_weather_rows(
            [
                {
                    "id": "obs-1",
                    "timestamp": "2026-08-11T14:00",
                    "county": "Fairfield",
                    "windSpeed": "9.1",
                    "windGust": "17.2",
                    "precipitation": "12.5",
                    "humidity": "82",
                    "outages": "44",
                }
            ],
            source_system="mock_feed",
        )

        self.assertEqual(rows[0]["sourceSystem"], "mock_feed")
        self.assertEqual(rows[0]["sourceRecordId"], "obs-1")
        self.assertEqual(rows[0]["observedAt"], "2026-08-11 14:00:00")
        self.assertEqual(rows[0]["peakGUST10"], 17.2)
        self.assertEqual(rows[0]["humidity"], 82.0)


def _sample_row(**overrides):
    row = {
        "eventCode": "CT-2005100800",
        "stormCode": "2005100800",
        "eventLength": "12",
        "County": "Fairfield",
        "ohLine": "1500",
        "sumAFWA_TOTPRECIP": "25.5",
        "peakWSPD10": "9.2",
        "wgt5": "4",
        "wgt9": "2",
        "wgt13": "1",
        "wgt18": "0",
        "outage": "120",
    }
    row.update(overrides)
    return row


if __name__ == "__main__":
    unittest.main()
