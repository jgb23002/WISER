"""Tests for wiser.storm_reports.

These assert structure and field order, not exact punctuation — the plan asks
for the stakeholder's field order and section structure, not a character match.

The highest-risk assertions in this file are the Mode B labelling ones: the
forecast column must be labelled a projection, analogue outages must be
labelled actual, and no outage figure may ever appear on the forecast side.
"""

from __future__ import annotations

import re
import sys

import psycopg
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SRC_DIR = REPO_ROOT / "src"
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"

TESTS_DIR = Path(__file__).parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from pg_harness import executemany, make_conn, new_connection  # noqa: E402,F401

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
    comparison_map_panels,
    display_rows,
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


def _make_conn() -> psycopg.Connection:
    conn = make_conn()
    executemany(conn, 
        "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(%s, %s, %s)",
        _STORMS,
    )
    executemany(conn, 
        "INSERT INTO County(county, state, ohLine) VALUES(%s, %s, %s)", _COUNTIES
    )
    executemany(conn, 
        "INSERT INTO WeatherData(stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,"
        " wgt5, wgt9, wgt13, wgt18, outage) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        _WEATHER,
    )
    conn.commit()
    return conn


def _flat(text: str) -> str:
    """Whitespace-collapsed, so assertions on prose survive line wrapping.

    Report prose is wrapped to a fixed width for slides and screenshots, so a
    sentence may span lines. Tests assert on the sentence, not on where the
    wrap happened to fall.
    """
    return " ".join(text.split())


def _order_of(text: str, *fragments: str) -> list[int]:
    return [text.index(fragment) for fragment in fragments]


_STRUCTURAL = ("Geographic concentration:", "Normalized divergence:")


def _metric_bullets(text: str) -> list[str]:
    """Bullets about the table's metrics, excluding structural observations."""
    section = text.split("Where they differ most:")[1]
    return [
        line
        for line in section.splitlines()
        if line.strip().startswith("- ")
        and not any(marker in line for marker in _STRUCTURAL)
    ]


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
        # Gust is absent from this list on purpose: gust EXPOSURE is measured
        # from the wgt thresholds. Only its magnitude is a Phase 2 field.
        for field in ("Storm type:", "Peak snowfall:"):
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

    def test_sentence_names_the_date(self):
        self.assertIn("The storm on 2020-08-04 ranks", self.report.text)

    def test_percentile_is_scoped_when_metrics_disagree(self):
        """An unqualified "top 1%" reads as a claim about the storm overall,
        when the wind and precipitation ranks are far lower."""
        self.assertIn("Its strongest placing is", self.report.text)
        self.assertIn("the metrics disagree", self.report.text)

    def test_single_metric_ranking_states_its_percentile_plainly(self):
        from wiser.storm_analysis import METRIC_OUTAGE

        report = render_storm_ranking(
            storm_ranking(self.conn, 2020080406, metrics=[METRIC_OUTAGE])
        )
        self.assertIn("That places it in the top", _flat(report.text))
        self.assertNotIn("metrics disagree", _flat(report.text))

    def test_percentile_never_claims_better_than_one_percent(self):
        self.assertNotIn("top 0%", self.report.text)

    def test_scope_is_stated(self):
        self.assertIn("Scope: statewide (CT)", self.report.text)

    def test_caveat_is_one_line(self):
        """The ranking sentence is the answer; a four-sentence caveat under it
        buries the thing the reader came for. Dataset vintage and the duration
        explanation are carried as notices instead."""
        caveat = self.report.text.split("Caveat:")[1].strip()
        self.assertEqual(caveat.count("."), 1, caveat)
        self.assertIn("trouble spots dispatched", caveat)
        self.assertNotIn("development dataset", caveat)
        self.assertNotIn("Duration is not rankable", caveat)

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
        self.assertEqual(len(self.report.rows), 5)

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
        executemany(conn, 
            "INSERT INTO WeatherData(stormCode, county, sumAFWA_TOTPRECIP,"
            " peakWSPD10, wgt5, wgt9, wgt13, wgt18, outage)"
            " VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
        self.assertIn(_flat(NOT_A_PREDICTION), _flat(self.report.text))
        self.assertIn("NOT a prediction", _flat(self.report.text))

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
            _flat(self.report.text),
            "Forecast Comparison:",
            f"Forecast input {PROJECTION_LABEL}",
            "Position in the record",
            "Closest historical analogues",
            "NOT a prediction",
            "Caveat:",
        )
        self.assertEqual(positions, sorted(positions))

    def test_caveat_says_the_forecast_is_not_a_record(self):
        self.assertIn("not a database record", _flat(self.report.text))
        self.assertIn("no outage figure is attributed to it", _flat(self.report.text))

    def test_statewide_report_warns_scores_are_within_county(self):
        statewide = render_forecast_comparison(
            compare_forecast(self.conn, {"peakWSPD10": 9.0}, limit=2)
        )
        self.assertIn("not comparable between counties", _flat(statewide.text))
        self.assertIn("Fairfield County", statewide.text)
        self.assertIn("Hartford County", statewide.text)

    def test_threshold_only_input_explains_the_missing_position(self):
        report = render_forecast_comparison(
            compare_forecast(self.conn, {"wgt5": 12}, county="Fairfield")
        )
        self.assertIn("No positionable values supplied", _flat(report.text))
        self.assertIn("Closest historical analogues", report.text)


class TestGustExposureIsNeverAPeak(unittest.TestCase):
    """wgt* counts HOURS above a threshold. It is exposure, and rendering it as
    a peak gust would invent a magnitude the schema does not contain.

    The data proves wgt* and peakWSPD10 are different quantities: rows exist
    with wgt18 > 0 against a peakWSPD10 of 7.5 m/s, which is impossible if both
    measured the same wind.
    """

    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def _summary_text(self, weather) -> str:
        self.conn.execute("DELETE FROM WeatherData")
        executemany(self.conn, 
            "INSERT INTO WeatherData(stormCode, county, sumAFWA_TOTPRECIP,"
            " peakWSPD10, wgt5, wgt9, wgt13, wgt18, outage)"
            " VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            weather,
        )
        self.conn.commit()
        return render_storm_summary(storm_summary(self.conn, 2020080406)).text

    def test_exposure_states_the_threshold_as_a_floor(self):
        text = self._summary_text(
            [(2020080406, "Fairfield", 30.0, 9.5, 20, 8, 6, 0, 600)]
        )
        self.assertIn("Wind gust exposure:", text)
        self.assertIn("exceeded 13 m/s", text)
        self.assertIn("for 6 hours", text)

    def test_no_wgt_value_is_ever_rendered_as_a_peak_gust(self):
        text = self._summary_text(
            [(2020080406, "Fairfield", 30.0, 9.5, 20, 8, 6, 2, 600)]
        )
        self.assertNotIn("Peak wind gust:", text)
        self.assertNotIn("Peak gust", text)
        self.assertIn("exceeded 18 m/s", text)

    def test_strongest_crossed_threshold_wins(self):
        text = self._summary_text(
            [(2020080406, "Fairfield", 30.0, 9.5, 20, 8, 6, 2, 600)]
        )
        self.assertIn("exceeded 18 m/s", text)
        self.assertNotIn("exceeded 13 m/s", text)

    def test_line_is_omitted_when_no_threshold_was_crossed(self):
        text = self._summary_text(
            [(2020080406, "Fairfield", 30.0, 4.0, 0, 0, 0, 0, 600)]
        )
        self.assertNotIn("Wind gust exposure:", text)
        self.assertIn("Peak wind gust:", text)
        self.assertIn(PHASE_2_UNAVAILABLE, text)

    def test_caveat_explains_exposure_is_not_magnitude(self):
        text = self._summary_text(
            [(2020080406, "Fairfield", 30.0, 9.5, 20, 8, 6, 0, 600)]
        )
        self.assertIn("measured floor", text)
        self.assertIn("legacy rows do not provide peak gust magnitude", text)

    def test_true_peak_gust_renders_when_peakgust10_exists(self):
        self.conn.execute(
            "UPDATE WeatherData SET peakGUST10 = %s WHERE stormCode = %s AND county = %s",
            (18.0, 2020080406, "Fairfield"),
        )
        self.conn.commit()

        text = render_storm_summary(storm_summary(self.conn, 2020080406)).text

        self.assertIn("Peak wind gust:", text)
        self.assertIn("40.3 mph", text)
        self.assertIn("source peakGUST10", text)


class TestComparisonLeadsWithTheLargestDifference(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def _seed(self, weather):
        self.conn.execute("DELETE FROM WeatherData")
        executemany(self.conn, 
            "INSERT INTO WeatherData(stormCode, county, sumAFWA_TOTPRECIP,"
            " peakWSPD10, wgt5, wgt9, wgt13, wgt18, outage)"
            " VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            weather,
        )
        self.conn.commit()

    def test_metric_bullets_are_ordered_by_relative_magnitude(self):
        """The invariant: whatever the data, the largest relative gap leads."""
        report = render_storm_comparison(
            compare_storms(self.conn, 2020080406, 2021090100)
        )
        bullets = _metric_bullets(report.text)
        percentages = [
            int(m) for line in bullets for m in re.findall(r"(\d+)% higher", line)
        ]
        self.assertEqual(percentages, sorted(percentages, reverse=True))

    def test_metric_bullets_come_before_the_structural_ones(self):
        """Structural observations are demoted: the metrics in the table above
        are what the reader is comparing."""
        report = render_storm_comparison(
            compare_storms(self.conn, 2020080406, 2021090100)
        )
        section = report.text.split("Where they differ most:")[1]
        self.assertLess(
            section.index("Statewide outages:"),
            section.index("Geographic concentration:"),
        )

    def test_a_zero_baseline_metric_is_described_not_scored(self):
        """Every zero-baseline gap is "100% higher" regardless of size, so
        scoring one lets a two-county difference outrank a fivefold difference
        in rainfall."""
        self._seed(
            [
                # Precipitation differs 4x. Threshold counties go 2 -> 0, which
                # would score 100% and top the list if it were ranked.
                (2020080406, "Fairfield", 20.0, 9.5, 5, 4, 0, 0, 500),
                (2020080406, "Hartford", 20.0, 9.5, 5, 4, 0, 0, 500),
                (2021090100, "Fairfield", 80.0, 9.5, 5, 0, 0, 0, 500),
                (2021090100, "Hartford", 80.0, 9.5, 5, 0, 0, 0, 500),
            ]
        )
        report = render_storm_comparison(
            compare_storms(self.conn, 2020080406, 2021090100)
        )
        bullets = _metric_bullets(report.text)

        self.assertIn("precipitation", bullets[0].lower())
        self.assertNotIn("100% higher", report.text)

        described = bullets[-1]
        self.assertIn("wind hours above 9 m/s in 2 counties", described)
        self.assertIn("had none", described)

    def test_a_zero_baseline_metric_sorts_after_every_ranked_one(self):
        self._seed(
            [
                (2020080406, "Fairfield", 20.0, 9.5, 5, 4, 0, 0, 500),
                (2020080406, "Hartford", 20.0, 9.5, 5, 4, 0, 0, 500),
                (2021090100, "Fairfield", 21.0, 9.5, 5, 0, 0, 0, 100),
                (2021090100, "Hartford", 21.0, 9.5, 5, 0, 0, 0, 100),
            ]
        )
        report = render_storm_comparison(
            compare_storms(self.conn, 2020080406, 2021090100)
        )
        bullets = _metric_bullets(report.text)
        self.assertGreater(len(bullets), 1)
        self.assertIn("had none", bullets[-1])
        for bullet in bullets[:-1]:
            self.assertNotIn("had none", bullet)

    def test_both_sides_zero_is_not_reported_as_a_difference(self):
        self._seed(
            [
                (2020080406, "Fairfield", 20.0, 9.5, 5, 0, 0, 0, 500),
                (2021090100, "Fairfield", 80.0, 9.5, 5, 0, 0, 0, 500),
            ]
        )
        report = render_storm_comparison(
            compare_storms(self.conn, 2020080406, 2021090100)
        )
        self.assertNotIn("had none", report.text)

    def test_a_dominant_precipitation_gap_leads_the_section(self):
        """The defect this section was rewritten for: the table showed
        precipitation differing ~7x while every bullet said the storms were
        alike."""
        self._seed(
            [
                # Same outage and wind; precipitation differs by 8x.
                (2020080406, "Fairfield", 10.0, 9.0, 5, 1, 0, 0, 500),
                (2020080406, "Hartford", 10.0, 9.0, 5, 1, 0, 0, 500),
                (2021090100, "Fairfield", 80.0, 9.0, 5, 1, 0, 0, 500),
                (2021090100, "Hartford", 80.0, 9.0, 5, 1, 0, 0, 500),
            ]
        )
        report = render_storm_comparison(
            compare_storms(self.conn, 2020080406, 2021090100)
        )
        section = report.text.split("Where they differ most:")[1]
        bullets = [l for l in section.splitlines() if l.strip().startswith("- ")]
        self.assertIn("precipitation", bullets[0].lower())
        self.assertIn("8.0x", bullets[0])

    def test_a_large_ratio_is_stated_as_a_multiple(self):
        report = render_storm_comparison(
            compare_storms(self.conn, 2020080406, 2021090100)
        )
        self.assertIn("x Storm", report.text)

    def test_identical_storms_are_not_listed_under_differ_most(self):
        self.conn.execute("DELETE FROM WeatherData")
        executemany(self.conn, 
            "INSERT INTO WeatherData(stormCode, county, sumAFWA_TOTPRECIP,"
            " peakWSPD10, wgt5, wgt9, wgt13, wgt18, outage)"
            " VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (2020080406, "Fairfield", 30.0, 9.5, 20, 8, 0, 0, 600),
                (2021090100, "Fairfield", 30.0, 9.5, 20, 8, 0, 0, 600),
            ],
        )
        self.conn.commit()
        report = render_storm_comparison(
            compare_storms(self.conn, 2020080406, 2021090100)
        )
        self.assertIn("How they compare:", report.text)
        self.assertNotIn("Where they differ most:", report.text)

    def test_matching_normalized_behaviour_collapses_to_one_line(self):
        report = render_storm_comparison(
            compare_storms(self.conn, 2020080406, 2021090100)
        )
        self.assertEqual(report.text.count("Normalized divergence:"), 1)
        self.assertIn("for both storms", report.text)


class TestSignedDifferenceFormatting(unittest.TestCase):
    """The Difference column has to agree with the two values beside it."""

    def setUp(self):
        from wiser.storm_reports import _signed_unit
        from wiser.units import format_inches, format_mph

        self.signed = _signed_unit
        self.mph = format_mph
        self.inches = format_inches

    def test_a_real_gap_keeps_its_sign(self):
        self.assertEqual(self.signed(-2.0, self.mph), "-4.5 mph")
        self.assertEqual(self.signed(2.0, self.mph), "+4.5 mph")

    def test_a_gap_too_small_to_print_becomes_a_bound(self):
        """'-0.0 mph' under 22.4 and 22.5 reads as a broken subtraction."""
        self.assertEqual(self.signed(-0.0219, self.mph), "-<0.1 mph")
        self.assertEqual(self.signed(0.02, self.inches), "+<0.01 in")

    def test_a_bound_still_says_which_storm_was_larger(self):
        """The one row where the sign is the only thing left to read.

        Both value columns print the same rounded number here, so if the
        Difference column drops the sign too, the direction of the gap is
        recoverable from nowhere on the page.
        """
        self.assertTrue(self.signed(-0.0219, self.mph).startswith("-"))
        self.assertTrue(self.signed(0.0219, self.mph).startswith("+"))

    def test_an_exact_zero_is_still_a_zero(self):
        self.assertEqual(self.signed(0.0, self.mph), "+0.0 mph")


class TestComparisonMapPanels(unittest.TestCase):
    """Two county maps for a comparison, read together."""

    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)
        self.panels = comparison_map_panels(
            compare_storms(self.conn, 2020080406, 2021090100)
        )

    def test_one_panel_per_storm_labelled_with_its_date(self):
        self.assertEqual(len(self.panels), 2)
        self.assertIn("2020-08-04", self.panels[0].title)
        self.assertIn("2021-09-01", self.panels[1].title)

    def test_panels_carry_county_rows_for_their_own_storm(self):
        a_outages = {row["county"]: row["outage"] for row in self.panels[0].rows}
        b_outages = {row["county"]: row["outage"] for row in self.panels[1].rows}
        self.assertEqual(a_outages["Fairfield"], 600)
        self.assertEqual(b_outages["Fairfield"], 200)

    def test_both_panels_share_one_colour_scale(self):
        """Scaled independently, the weaker storm paints as red as the stronger
        one and inverts the comparison the reader is making."""
        self.assertEqual(self.panels[0].value_range, self.panels[1].value_range)

    def test_the_shared_scale_runs_from_zero_to_the_larger_storm(self):
        """Anchored at zero, not at the quietest county in either storm.

        Starting the ramp at the lowest observed value paints that county as if
        it had no outages at all.
        """
        every_value = [
            row["outage"] for panel in self.panels for row in panel.rows
        ]
        self.assertEqual(self.panels[0].value_range, (0.0, max(every_value)))

    def test_the_shared_scale_is_wider_than_either_storm_alone(self):
        b_values = [row["outage"] for row in self.panels[1].rows]
        low, high = self.panels[1].value_range
        self.assertGreater(high, max(b_values))

    def test_subtitle_states_the_storm_total_in_its_own_unit(self):
        self.assertIn("1,000", self.panels[0].subtitle)
        self.assertIn("trouble spots dispatched", self.panels[0].subtitle)

    def test_subtitle_names_the_storms_own_peak_county(self):
        """A quiet storm's map is faint by design; its own peak says why."""
        self.assertIn("peaks in Fairfield at 600 trouble spots", self.panels[0].subtitle)
        self.assertIn("peaks in Fairfield at 200 trouble spots", self.panels[1].subtitle)

    def test_the_peak_value_is_never_followed_by_a_bare_in(self):
        """"peaks at 12.35 in Fairfield" reads 12.35 as inches."""
        for panel in self.panels:
            self.assertNotRegex(panel.subtitle, r"peaks at [\d,.]+ in ")

    def test_metric_is_raw_outage_not_a_normalized_value(self):
        for panel in self.panels:
            with self.subTest(panel=panel.title):
                self.assertEqual(panel.metric, "outage")


class TestDisplayRows(unittest.TestCase):
    """Structured rows are for maps and reasoning; display rows are for people."""

    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def test_summary_display_rows_use_labels_and_rounded_values(self):
        report = render_storm_summary(storm_summary(self.conn, 2020080406))
        row = display_rows(report)[0]

        self.assertIn("County", row)
        self.assertIn("Peak wind", row)
        self.assertNotIn("peakWSPD10", row)
        self.assertNotIn("outage_per_million_ohline", row)
        self.assertTrue(str(row["Peak wind"]).endswith("mph"))

    def test_ranking_display_rows_hide_metric_keys_and_raw_percentiles(self):
        report = render_storm_ranking(storm_ranking(self.conn, 2020080406))
        row = display_rows(report)[0]

        self.assertNotIn("metric", row)
        self.assertNotIn("percentile", row)
        self.assertTrue(str(row["Rank"]).endswith(("st", "nd", "rd", "th")))
        self.assertTrue(str(row["Percentile"]).startswith("top "))

    def test_comparison_display_rows_are_formatted(self):
        report = render_storm_comparison(
            compare_storms(self.conn, 2020080406, 2021090100)
        )
        row = display_rows(report)[0]

        self.assertIn("Storm A", row)
        self.assertNotIn("storm_a", row)
        self.assertNotIn("percent_change", row)

    def test_forecast_display_rows_round_similarity_and_show_the_delta(self):
        comparison = compare_forecast(
            self.conn, {"peakWSPD10": 9.0}, county="Fairfield"
        )
        report = render_forecast_comparison(comparison)
        row = display_rows(report, forecast_wind_ms=9.0)[0]

        self.assertIn("Outages (actual)", row)
        self.assertIn("vs forecast", row)

    def test_no_display_row_leaks_full_precision_floats(self):
        for report in (
            render_storm_summary(storm_summary(self.conn, 2020080406)),
            render_storm_ranking(storm_ranking(self.conn, 2020080406)),
            render_storm_comparison(
                compare_storms(self.conn, 2020080406, 2021090100)
            ),
        ):
            with self.subTest(kind=report.kind):
                for row in display_rows(report):
                    for key, value in row.items():
                        if isinstance(value, float):
                            self.assertLessEqual(
                                len(str(value).split(".")[-1]),
                                3,
                                f"{report.kind}.{key} = {value}",
                            )


class TestForecastAnalogueVolume(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self.addCleanup(self.conn.close)

    def test_defaults_are_small_enough_to_read(self):
        comparison = compare_forecast(self.conn, {"peakWSPD10": 9.0})
        for group in comparison.analogue_groups:
            with self.subTest(county=group.county):
                self.assertLessEqual(len(group.analogues), 3)
        self.assertLessEqual(len(comparison.analogue_groups), 3)

    def test_counts_are_parameters(self):
        comparison = compare_forecast(
            self.conn, {"peakWSPD10": 9.0}, limit=1, max_counties=1
        )
        self.assertEqual(len(comparison.analogue_groups), 1)
        self.assertEqual(len(comparison.analogue_groups[0].analogues), 1)

    def test_omitted_counties_are_reported_not_hidden(self):
        comparison = compare_forecast(
            self.conn, {"peakWSPD10": 9.0}, max_counties=1
        )
        self.assertEqual(comparison.counties_omitted, 1)
        self.assertIn("1 more in scope", render_forecast_comparison(comparison).text)

    def test_delta_column_makes_the_ordering_legible(self):
        report = render_forecast_comparison(
            compare_forecast(self.conn, {"peakWSPD10": 9.0}, county="Fairfield")
        )
        self.assertIn("vs forecast", report.text)
        self.assertRegex(report.text, r"[+-]\d+\.\d mph")

    def test_delta_column_is_ascii(self):
        """A Greek delta raises UnicodeEncodeError on a cp1252 console, which
        crashes the CLI rather than merely looking wrong."""
        report = render_forecast_comparison(
            compare_forecast(self.conn, {"peakWSPD10": 9.0}, county="Fairfield")
        )
        report.text.encode("cp1252")  # must not raise


if __name__ == "__main__":
    unittest.main()


class TestComparisonTableAndDataframeAgree(unittest.TestCase):
    """The report and the dataframe are two renderings of one comparison.

    The Difference column was formatted with the VALUE formatter here, so the
    table said '+<0.1 mph' while the dataframe beside it said '0.0 mph'.
    """

    def setUp(self):
        from wiser.storm_reports import _comparison_display_row

        self.render = _comparison_display_row

    def _row(self, metric, a, b):
        return self.render(
            {
                "metric": metric,
                "label": metric,
                "storm_a": a,
                "storm_b": b,
                "difference": a - b,
                "percent_change": None,
            }
        )

    def test_a_below_resolution_wind_gap_keeps_its_bound_and_sign(self):
        row = self._row("max_wind", 7.765029558, 7.765003202)

        self.assertEqual(row["Difference"], "+<0.1 mph")

    def test_a_positive_count_difference_is_signed(self):
        row = self._row("counties_above_threshold", 6.0, 4.0)

        self.assertEqual(row["Difference"], "+2")
