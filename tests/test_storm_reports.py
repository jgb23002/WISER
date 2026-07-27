"""Tests for wiser.storm_reports.

These assert structure and field order, not exact punctuation — the plan asks
for the stakeholder's field order and section structure, not a character match.

The highest-risk assertions in this file are the Mode B labelling ones: the
forecast column must be labelled a projection, analogue outages must be
labelled actual, and no outage figure may ever appear on the forecast side.
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SRC_DIR = REPO_ROOT / "src"
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.storm_analysis import (  # noqa: E402
    compare_forecast,
    compare_storms,
    storm_count,
    storm_ranking,
    storm_summary,
)
from wiser.storm_reports import (  # noqa: E402
    ACTUAL_LABEL,
    NOT_A_PREDICTION,
    PHASE_2_UNAVAILABLE,
    PROJECTION_LABEL,
    REPORT_FORECAST_COMPARISON,
    REPORT_STORM_COMPARISON,
    REPORT_STORM_RANKING,
    REPORT_STORM_SUMMARY,
    render_forecast_comparison,
    render_storm_comparison,
    render_storm_ranking,
    render_storm_summary,
)

_SCHEMA_SQL = SCHEMA_PATH.read_text(encoding="utf-8")

_STORMS = [
    (2020080406, "2020-08-04 06:00:00", 48),
    (2021090100, "2021-09-01 00:00:00", 48),
    (2021090112, "2021-09-01 12:00:00", 48),
]
_COUNTIES = [("Fairfield", "CT", 4_000_000), ("Hartford", "CT", 8_000_000)]
_WEATHER = [
    (2020080406, "Fairfield", 30.0, 9.5, 20, 8, 0, 0, 600),
    (2020080406, "Hartford", 25.0, 11.0, 18, 6, 1, 0, 400),
    (2021090100, "Fairfield", 50.0, 7.0, 10, 0, 0, 0, 200),
    (2021090100, "Hartford", 45.0, 6.0, 8, 0, 0, 0, 100),
    (2021090112, "Fairfield", 10.0, 5.0, 4, 0, 0, 0, 50),
    (2021090112, "Hartford", 12.0, 5.5, 5, 0, 0, 0, 60),
]


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA_SQL)
    conn.executemany(
        "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(?, ?, ?)",
        _STORMS,
    )
    conn.executemany(
        "INSERT INTO County(county, state, ohLine) VALUES(?, ?, ?)", _COUNTIES
    )
    conn.executemany(
        "INSERT INTO WeatherData(stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,"
        " wgt5, wgt9, wgt13, wgt18, outage) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        _WEATHER,
    )
    conn.commit()
    return conn


def _order_of(text: str, *fragments: str) -> list[int]:
    return [text.index(fragment) for fragment in fragments]


class TestStormSummaryReport(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)
        self.summary = storm_summary(self.conn, 2020080406)
        self.report = render_storm_summary(
            self.summary, total_storms=storm_count(self.conn)
        )

    def test_kind_and_rows(self):
        self.assertEqual(self.report.kind, REPORT_STORM_SUMMARY)
        self.assertEqual(len(self.report.rows), 2)

    def test_field_order_matches_the_specified_structure(self):
        positions = _order_of(
            self.report.text,
            "Storm date:",
            "Storm type:",
            "Analysis window:",
            "Weather Conditions",
            "Maximum wind speed:",
            "Maximum rainfall:",
            "Peak snowfall:",
            "Storm Impact",
            "Statewide outages",
            "Counties with the highest outages:",
            "Relative Impact",
            "Caveat:",
        )
        self.assertEqual(positions, sorted(positions))

    def test_phase_two_fields_render_as_explicitly_unavailable(self):
        for field in ("Storm type:", "Peak snowfall:", "Peak wind gust:"):
            with self.subTest(field=field):
                line = next(
                    l for l in self.report.text.splitlines() if l.strip().startswith(field)
                )
                self.assertIn(PHASE_2_UNAVAILABLE, line)

    def test_no_fabricated_storm_type_snowfall_or_gust(self):
        text = self.report.text.lower()
        for invented in (
            "thunderstorm",
            "nor'easter",
            "tropical",
            "blizzard",
            " in of snow",
        ):
            with self.subTest(invented=invented):
                self.assertNotIn(invented, text)

    def test_modeled_values_are_labelled_as_modeled(self):
        wind_line = next(
            l for l in self.report.text.splitlines() if "Maximum wind speed:" in l
        )
        precip_line = next(
            l for l in self.report.text.splitlines() if "Maximum rainfall:" in l
        )
        self.assertIn("modeled", wind_line)
        self.assertIn("AFWA", wind_line)
        self.assertIn("modeled", precip_line)

    def test_report_is_imperial_and_rows_keep_metric(self):
        # Hartford's 11.0 m/s is 24.6 mph.
        self.assertIn("24.6 mph", self.report.text)
        self.assertIn("Hartford County", self.report.text)
        row = next(r for r in self.report.rows if r["county"] == "Hartford")
        self.assertEqual(row["peakWSPD10"], 11.0)
        self.assertAlmostEqual(row["peak_wind_mph"], 24.606, places=2)

    def test_rows_keep_the_existing_analytics_column_names(self):
        """The reasoning agent and heat-map picker key off these names."""
        row = self.report.rows[0]
        for column in (
            "county",
            "outage",
            "peakWSPD10",
            "sumAFWA_TOTPRECIP",
            "ohLine",
            "outage_per_million_ohline",
        ):
            with self.subTest(column=column):
                self.assertIn(column, row)

    def test_duration_is_labelled_a_fixed_window_not_a_storm_property(self):
        self.assertIn("Analysis window:", self.report.text)
        self.assertIn("fixed model window", self.report.text)
        self.assertNotIn("Duration:", self.report.text)

    def test_analysis_window_reports_the_live_storm_count(self):
        self.assertIn(str(storm_count(self.conn)), self.report.text)

    def test_outage_is_labelled_trouble_spots_not_customers(self):
        self.assertIn("trouble spots dispatched", self.report.text)
        self.assertNotIn("customers affected", self.report.text.lower())

    def test_report_ends_with_a_caveat(self):
        self.assertTrue(self.report.text.rstrip().endswith("."))
        self.assertIn("Caveat:", self.report.text)

    def test_county_scoped_summary_says_so(self):
        scoped = render_storm_summary(
            storm_summary(self.conn, 2020080406, county="Hartford")
        )
        self.assertIn("1 county in scope", scoped.text)


class TestStormRankingReport(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)
        self.report = render_storm_ranking(storm_ranking(self.conn, 2020080406))

    def test_kind_and_rows(self):
        self.assertEqual(self.report.kind, REPORT_STORM_RANKING)
        self.assertEqual(len(self.report.rows), 3)

    def test_rank_always_travels_with_its_total(self):
        total = storm_count(self.conn)
        self.assertIn(f"out of {total} storms", self.report.text)
        for row in self.report.rows:
            with self.subTest(metric=row["metric"]):
                self.assertEqual(row["total"], total)
                self.assertLessEqual(row["rank"], row["total"])

    def test_sentence_names_the_date_and_a_percentile(self):
        self.assertIn("The storm on 2020-08-04 ranks", self.report.text)
        self.assertIn("placing it in the top", self.report.text)

    def test_percentile_never_claims_better_than_one_percent(self):
        self.assertNotIn("top 0%", self.report.text)

    def test_scope_is_stated(self):
        self.assertIn("Scope: statewide (CT)", self.report.text)

    def test_caveat_frames_counts_as_the_development_dataset(self):
        self.assertIn("current development dataset", self.report.text)
        self.assertNotIn("root cause unknown", self.report.text)

    def test_caveat_explains_why_duration_is_absent(self):
        self.assertIn("Duration is not rankable", self.report.text)

    def test_county_scope_is_reported(self):
        scoped = render_storm_ranking(
            storm_ranking(self.conn, 2020080406, county="Hartford")
        )
        self.assertIn("Hartford County", scoped.text)


class TestStormComparisonReport(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)
        self.report = render_storm_comparison(
            compare_storms(self.conn, 2020080406, 2021090100)
        )

    def test_kind_and_rows(self):
        self.assertEqual(self.report.kind, REPORT_STORM_COMPARISON)
        self.assertEqual(len(self.report.rows), 4)

    def test_section_order(self):
        positions = _order_of(
            self.report.text,
            "Storm Comparison:",
            "Scale:",
            "Storm A",
            "Statewide outages",
            "Where they differ most:",
            "Caveat:",
        )
        self.assertEqual(positions, sorted(positions))

    def test_table_carries_both_storms_and_a_signed_difference(self):
        outage_row = next(r for r in self.report.rows if r["metric"] == "outage")
        self.assertEqual(outage_row["storm_a"], 1000.0)
        self.assertEqual(outage_row["storm_b"], 300.0)
        self.assertEqual(outage_row["difference"], 700.0)
        self.assertIn("+700", self.report.text)

    def test_threshold_row_uses_the_nine_metre_threshold(self):
        self.assertIn("9+ m/s", self.report.text)
        self.assertNotIn("13 m/s", self.report.text)

    def test_reports_geographic_concentration_and_normalized_divergence(self):
        self.assertIn("Geographic concentration:", self.report.text)
        self.assertIn("Normalized divergence:", self.report.text)

    def test_equal_concentrations_are_not_claimed_to_differ(self):
        """Both seeded storms put 60%/66% in Fairfield. When two shares render
        as the same percentage, declaring one "more concentrated" reads as a
        bug even though the underlying floats differ."""
        conn = _make_conn()
        self.addCleanup(conn.close)
        conn.execute("DELETE FROM WeatherData")
        conn.executemany(
            "INSERT INTO WeatherData(stormCode, county, sumAFWA_TOTPRECIP,"
            " peakWSPD10, wgt5, wgt9, wgt13, wgt18, outage)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (2020080406, "Fairfield", 30.0, 9.5, 1, 0, 0, 0, 600),
                (2020080406, "Hartford", 25.0, 11.0, 1, 0, 0, 0, 400),
                (2021090100, "Fairfield", 30.0, 9.5, 1, 0, 0, 0, 300),
                (2021090100, "Hartford", 25.0, 11.0, 1, 0, 0, 0, 200),
            ],
        )
        conn.commit()
        report = render_storm_comparison(compare_storms(conn, 2020080406, 2021090100))

        self.assertIn("comparably concentrated", report.text)
        self.assertNotIn("more concentrated of the two", report.text)

    def test_caveat_limits_claims_to_database_fields(self):
        self.assertIn("only fields present in the WISER database", self.report.text)


class TestForecastComparisonReport(unittest.TestCase):
    """Mode B. The labelling assertions here are the point of the whole file."""

    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)
        self.comparison = compare_forecast(
            self.conn,
            {"peakWSPD10": 12.07, "sumAFWA_TOTPRECIP": 30.48},
            county="Fairfield",
            limit=3,
        )
        self.report = render_forecast_comparison(self.comparison)

    def test_kind_and_rows(self):
        self.assertEqual(self.report.kind, REPORT_FORECAST_COMPARISON)
        self.assertTrue(self.report.rows)

    def test_forecast_column_is_labelled_a_projection(self):
        self.assertIn(PROJECTION_LABEL, self.report.text)
        self.assertIn(f"Forecast input {PROJECTION_LABEL}", self.report.text)

    def test_analogue_outages_are_labelled_actual(self):
        self.assertIn(ACTUAL_LABEL, self.report.text)
        for row in self.report.rows:
            with self.subTest(storm=row["stormCode"]):
                self.assertEqual(row["outage_basis"], "historical actual")

    def test_report_states_it_is_not_a_prediction(self):
        self.assertIn(NOT_A_PREDICTION, self.report.text)
        self.assertIn("NOT a prediction", self.report.text)

    def test_no_outage_value_appears_on_the_forecast_side(self):
        """The single most important assertion in this suite: whatever the
        input, the forecast half of the report never carries an outage."""
        forecast_block = self.report.text.split("Position in the record")[0]
        self.assertNotIn("outage", forecast_block.lower())
        for value in (600, 200, 50, 1000):
            with self.subTest(value=value):
                self.assertNotIn(str(value), forecast_block)

    def test_forecast_inputs_show_both_units(self):
        """A mis-entered unit must be visible on the page."""
        self.assertIn("27.0 mph", self.report.text)
        self.assertIn("12.1 m/s", self.report.text)
        self.assertIn("1.20 in", self.report.text)
        self.assertIn("30.5 mm", self.report.text)

    def test_position_reports_rank_and_total(self):
        total = storm_count(self.conn)
        self.assertIn("would rank", self.report.text)
        self.assertIn(f"of {total} storms", self.report.text)

    def test_section_order(self):
        positions = _order_of(
            self.report.text,
            "Forecast Comparison:",
            f"Forecast input {PROJECTION_LABEL}",
            "Position in the record",
            "Closest historical analogues",
            "NOT a prediction",
            "Caveat:",
        )
        self.assertEqual(positions, sorted(positions))

    def test_caveat_says_the_forecast_is_not_a_record(self):
        self.assertIn("not a database record", self.report.text)
        self.assertIn("no outage figure is attributed to it", self.report.text)

    def test_statewide_report_warns_scores_are_within_county(self):
        statewide = render_forecast_comparison(
            compare_forecast(self.conn, {"peakWSPD10": 9.0}, limit=2)
        )
        self.assertIn("not comparable between counties", statewide.text)
        self.assertIn("Fairfield County", statewide.text)
        self.assertIn("Hartford County", statewide.text)

    def test_threshold_only_input_explains_the_missing_position(self):
        report = render_forecast_comparison(
            compare_forecast(self.conn, {"wgt5": 12}, county="Fairfield")
        )
        self.assertIn("No positionable values supplied", report.text)
        self.assertIn("Closest historical analogues", report.text)


if __name__ == "__main__":
    unittest.main()
