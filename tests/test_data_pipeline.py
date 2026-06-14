"""Tests for CSV normalization and state extraction."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.data_pipeline import normalize_rows  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
