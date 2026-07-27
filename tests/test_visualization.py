from __future__ import annotations

import sqlite3
import sys
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SRC_DIR = REPO_ROOT / "src"
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.visualization import (  # noqa: E402
    COUNTY_HEATMAP_METRICS,
    county_metric_rows,
    load_county_geojson,
    normalize_county_name,
    render_county_heatmap_svg,
)


class TestCountyVisualization(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.conn.executemany(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(?, ?, ?)",
            [
                (2020080406, "2020-08-04 06:00:00", 36),
                (2012102806, "2012-10-28 06:00:00", 72),
            ],
        )
        self.conn.executemany(
            "INSERT INTO County(county, ohLine) VALUES(?, ?)",
            [
                ("Fairfield", 1000),
                ("Hartford", 2000),
            ],
        )
        self.conn.executemany(
            "INSERT INTO WeatherData(stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,"
            " wgt5, wgt9, wgt13, wgt18, outage) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (2020080406, "Fairfield", 30.0, 10.0, 4, 1, 0, 0, 300),
                (2020080406, "Hartford", 25.0, 9.0, 3, 0, 0, 0, 500),
                (2012102806, "Fairfield", 80.0, 12.0, 8, 2, 1, 0, 700),
            ],
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_county_metric_rows_aggregate_and_normalize(self):
        rows = county_metric_rows(self.conn, "outage_per_million_ohline")

        self.assertEqual(rows[0]["county"], "Fairfield")
        self.assertEqual(rows[0]["total_outage"], 1000)
        self.assertEqual(rows[0]["storms_observed"], 2)
        self.assertEqual(rows[0]["outage_per_million_ohline"], 1000000.0)
        self.assertEqual(rows[1]["county"], "Hartford")

    def test_county_metric_rows_can_scope_to_one_storm(self):
        rows = county_metric_rows(
            self.conn,
            "total_outage",
            storm_code=2020080406,
        )

        self.assertEqual([row["county"] for row in rows], ["Hartford", "Fairfield"])
        self.assertEqual(rows[0]["total_outage"], 500)

    def test_legacy_geojson_has_expected_counties(self):
        geojson = load_county_geojson()
        counties = sorted(
            normalize_county_name(feature["properties"]["County"])
            for feature in geojson["features"]
        )

        self.assertEqual(len(counties), 8)
        self.assertIn("Fairfield", counties)
        self.assertIn("New London", counties)

    def test_render_county_heatmap_svg_contains_legend_and_counties(self):
        geojson = load_county_geojson()
        rows = county_metric_rows(self.conn, "total_outage")
        svg = render_county_heatmap_svg(geojson, rows, "total_outage")

        self.assertIn("<svg", svg)
        self.assertIn("Fairfield", svg)
        self.assertIn("Hartford", svg)
        self.assertIn("Total outage", svg)
        self.assertIn("No data", svg)

    def test_rejects_unknown_metric(self):
        with self.assertRaises(ValueError):
            county_metric_rows(self.conn, "bogus_metric")


if __name__ == "__main__":
    unittest.main()


class TestSharedColourScale(unittest.TestCase):
    """Two maps shown together must be comparable by colour."""

    def setUp(self):
        self.geojson = load_county_geojson()
        self.strong = [
            {"county": "Fairfield", "outage": 6000},
            {"county": "Hartford", "outage": 3000},
        ]
        self.weak = [
            {"county": "Fairfield", "outage": 600},
            {"county": "Hartford", "outage": 300},
        ]

    def _fills(self, rows, value_range=None):
        """County polygon fills only.

        Matching every fill would also catch the legend swatches, which always
        render the whole palette and would make any two maps look identical.
        """
        svg = render_county_heatmap_svg(
            self.geojson, rows, "outage", value_range=value_range
        )
        return re.findall(r'<path [^>]*fill="(#[0-9a-fA-F]{6})"', svg)

    def test_independent_scales_paint_the_weak_storm_as_darkly_as_the_strong(self):
        """The defect a shared range exists to prevent."""
        self.assertEqual(
            max(self._fills(self.strong)), max(self._fills(self.weak))
        )

    def test_a_shared_range_keeps_the_weak_storm_lighter(self):
        shared = (300.0, 6000.0)
        strong = set(self._fills(self.strong, shared))
        weak = set(self._fills(self.weak, shared))

        self.assertIn("#bd0026", strong, "the strongest county should be hottest")
        self.assertNotIn(
            "#bd0026", weak, "the weaker storm must not use the hottest colour"
        )

    def test_outage_is_a_supported_metric(self):
        self.assertIn("outage", COUNTY_HEATMAP_METRICS)
