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
    COUNTY_HEATMAP_PALETTE,
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
        self.darkest = COUNTY_HEATMAP_PALETTE[-1]

    def _fills(self, rows, value_range=None):
        """County polygon fills only.

        Matching every fill would also catch the legend swatches, which always
        render the whole palette and would make any two maps look identical.
        """
        svg = render_county_heatmap_svg(
            self.geojson, rows, "outage", value_range=value_range
        )
        return re.findall(r'<path [^>]*fill="(#[0-9a-fA-F]{6})"', svg)

    def _fills_by_county(self, rows, value_range=None):
        svg = render_county_heatmap_svg(
            self.geojson, rows, "outage", value_range=value_range
        )
        pairs = re.findall(
            r'fill="(#[0-9a-fA-F]{6})"[^>]*><title>([^:<]+):', svg
        )
        return {county: fill for fill, county in pairs}

    def _darkest_palette_fill(self, rows, value_range=None):
        """The furthest-along-the-ramp colour actually painted on a county.

        Ranked by palette index rather than by hex string: the no-data grey
        is not on the ramp at all, and it sorts above every colour that is.
        """
        ramp = list(COUNTY_HEATMAP_PALETTE)
        painted = [fill for fill in self._fills(rows, value_range) if fill in ramp]
        return max(painted, key=ramp.index)

    def test_independent_scales_paint_the_weak_storm_as_darkly_as_the_strong(self):
        """The defect a shared range exists to prevent.

        Scaled to itself, each storm spends its whole ramp on its own peak, so
        600 outages paint exactly as dark as 6,000 and the reader comparing two
        maps sees no difference.
        """
        self.assertEqual(
            self._darkest_palette_fill(self.strong),
            self._darkest_palette_fill(self.weak),
        )
        self.assertEqual(
            self._darkest_palette_fill(self.weak),
            self.darkest,
            "both storms should reach the dark end of their own ramp",
        )

    def test_a_shared_range_keeps_the_weak_storm_lighter(self):
        shared = (0.0, 6000.0)
        strong = set(self._fills(self.strong, shared))
        weak = set(self._fills(self.weak, shared))

        self.assertIn(self.darkest, strong, "the strongest county should be darkest")
        self.assertNotIn(
            self.darkest, weak, "the weaker storm must not use the darkest colour"
        )

    def test_a_weak_storm_still_varies_on_a_shared_scale(self):
        """The defect the scale exists to avoid: a flat, empty-looking map.

        These are the 2009-05-09 county outages against the 2022-02-17 peak
        that reported the bug: on a linear ramp every one of them lands in the
        first band and the whole state paints white.
        """
        rows = [
            {"county": "Fairfield", "outage": 44},
            {"county": "Litchfield", "outage": 22},
            {"county": "Hartford", "outage": 17},
            {"county": "Windham", "outage": 10},
            {"county": "Tolland", "outage": 5},
        ]
        fills = self._fills_by_county(rows, (0.0, 171.0))
        shades = {fills[row["county"]] for row in rows}

        self.assertGreaterEqual(len(shades), 3, "the weak storm must show structure")
        self.assertNotIn(
            COUNTY_HEATMAP_PALETTE[0],
            shades,
            "a county with outages should not paint as the empty end of the ramp",
        )
        self.assertNotIn(
            COUNTY_HEATMAP_PALETTE[-1],
            shades,
            "and it must stay clear of the darker storm's end of the ramp",
        )

    def test_the_scale_still_orders_counties_across_both_maps(self):
        """Compression may not reorder anything: more outages, never lighter."""
        shared = (0.0, 6000.0)
        ramp = list(COUNTY_HEATMAP_PALETTE)
        weak = self._fills_by_county(self.weak, shared)
        strong = self._fills_by_county(self.strong, shared)

        ranked = [
            ramp.index(weak["Hartford"]),  # 300
            ramp.index(weak["Fairfield"]),  # 600
            ramp.index(strong["Hartford"]),  # 3,000
            ramp.index(strong["Fairfield"]),  # 6,000
        ]
        self.assertEqual(ranked, sorted(ranked))
        self.assertLess(ranked[1], ranked[2])

    def test_legend_names_the_value_range_of_every_band(self):
        svg = render_county_heatmap_svg(
            self.geojson, self.strong, "outage", value_range=(0.0, 6000.0)
        )
        bands = re.findall(r'class="legend-band">([^<]+)<', svg)

        self.assertEqual(len(bands), len(COUNTY_HEATMAP_PALETTE))
        self.assertTrue(bands[0].startswith("0"), bands[0])
        self.assertTrue(bands[-1].endswith("6,000"), bands[-1])

    def test_dark_fills_take_light_ink(self):
        """Near-black text on a near-black county is unreadable."""
        svg = render_county_heatmap_svg(
            self.geojson, self.strong, "outage", value_range=(0.0, 6000.0)
        )
        self.assertIn("county-ink-light", svg)

    def test_outage_is_a_supported_metric(self):
        self.assertIn("outage", COUNTY_HEATMAP_METRICS)


class TestScaleAnchoring(unittest.TestCase):
    """Which curve a metric gets is a property of the metric, not of its data."""

    def setUp(self):
        self.geojson = load_county_geojson()

    def _band_index(self, rows, metric, value_range, county):
        svg = render_county_heatmap_svg(
            self.geojson, rows, metric, value_range=value_range
        )
        pairs = re.findall(r'fill="(#[0-9a-fA-F]{6})"[^>]*><title>([^:<]+):', svg)
        fills = {name: fill for fill, name in pairs}
        return list(COUNTY_HEATMAP_PALETTE).index(fills[county])

    def test_a_magnitude_metric_compresses_even_with_no_zero_county(self):
        """The knife edge: whether some county reported 0 must not pick the curve.

        Outages are a magnitude whose floor is "none", so the ramp compresses
        whether or not this particular slice happens to contain a zero. On the
        midpoint of 5..100 a linear scale lands in band 3; compressed, band 5.
        """
        rows = [{"county": "Fairfield", "outage": 52.5}]

        self.assertEqual(
            self._band_index(rows, "outage", (5.0, 100.0), "Fairfield"), 5
        )

    def test_a_wind_metric_stays_linear(self):
        """The guard on the other side: wind speed has no meaningful zero."""
        rows = [{"county": "Fairfield", "avg_peak_wind": 15.0}]

        self.assertEqual(
            self._band_index(rows, "avg_peak_wind", (10.0, 20.0), "Fairfield"), 3
        )


_RECT_RE = re.compile(r'<rect x="([\d.]+)" y="[^"]*" width="([\d.]+)"')
_LEGEND_TEXT_RE = re.compile(r'<text x="([\d.]+)"[^>]*class="legend-label">([^<]*)<')
#: "No data" at the 11px legend weight, rounded up. The renderer reserves room
#: for this label, so the test has to know roughly what it costs.
_NO_DATA_LABEL_WIDTH = 52


class TestLegendLayout(unittest.TestCase):
    def test_the_legend_stays_inside_a_narrow_map(self):
        """Nothing the legend draws may cross the right edge of the viewBox."""
        geojson = load_county_geojson()
        rows = [{"county": "Fairfield", "outage": 600}]
        width = 300

        svg = render_county_heatmap_svg(
            geojson, rows, "outage", width=width, value_range=(0.0, 6000.0)
        )

        for x, box_width in _RECT_RE.findall(svg):
            self.assertLessEqual(
                float(x) + float(box_width), width, f"swatch at x={x} overflows"
            )
        for x, text in _LEGEND_TEXT_RE.findall(svg):
            if text == "No data":
                self.assertLessEqual(
                    float(x) + _NO_DATA_LABEL_WIDTH,
                    width,
                    "the No data label runs past the right edge",
                )


if __name__ == "__main__":
    unittest.main()
