"""Tests for wiser.units — metric/imperial conversion and forecast input parsing.

Conversion happens only at the presentation boundary, so these are pure
functions with no database or network involvement.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SRC_DIR = REPO_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.units import (  # noqa: E402
    COUNT,
    DEPTH,
    MM_PER_INCH,
    MS_TO_MPH,
    SPEED,
    UNAVAILABLE,
    AmbiguousUnitError,
    UnknownUnitError,
    canonical_unit,
    describe_ambiguity,
    format_count,
    format_inches,
    format_mm,
    format_mph,
    format_ms,
    format_signed,
    inches_to_mm,
    mm_to_inches,
    mph_to_ms,
    ms_to_mph,
    parse_measurement,
    supported_suffixes,
)


class TestOutputConversion(unittest.TestCase):
    def test_ms_to_mph(self):
        self.assertAlmostEqual(ms_to_mph(13.11149342), 29.325, places=2)
        self.assertAlmostEqual(ms_to_mph(1.0), MS_TO_MPH, places=6)
        self.assertEqual(ms_to_mph(0), 0.0)

    def test_mm_to_inches(self):
        self.assertAlmostEqual(mm_to_inches(231.9845528), 9.133, places=2)
        self.assertAlmostEqual(mm_to_inches(MM_PER_INCH), 1.0, places=9)
        self.assertEqual(mm_to_inches(0), 0.0)


class TestInputConversion(unittest.TestCase):
    """Mode B supplies forecasts in imperial; the engine needs metric."""

    def test_mph_to_ms(self):
        self.assertAlmostEqual(mph_to_ms(MS_TO_MPH), 1.0, places=9)
        self.assertAlmostEqual(mph_to_ms(27.0), 12.070, places=2)

    def test_inches_to_mm(self):
        self.assertAlmostEqual(inches_to_mm(1.0), MM_PER_INCH, places=9)
        self.assertAlmostEqual(inches_to_mm(1.2), 30.48, places=6)

    def test_zero_is_a_value_not_an_absence(self):
        for converter in (ms_to_mph, mm_to_inches, mph_to_ms, inches_to_mm):
            with self.subTest(converter=converter.__name__):
                self.assertEqual(converter(0), 0.0)
                self.assertIsNotNone(converter(0))


class TestRoundTrip(unittest.TestCase):
    """The inverse is the reciprocal of one constant, so it cannot drift."""

    def test_speed_round_trip(self):
        for value in (0.0, 0.5, 3.7, 9.2, 13.11149342, 100.0):
            with self.subTest(value=value):
                self.assertAlmostEqual(mph_to_ms(ms_to_mph(value)), value, places=9)
                self.assertAlmostEqual(ms_to_mph(mph_to_ms(value)), value, places=9)

    def test_depth_round_trip(self):
        for value in (0.0, 1.0, 25.4, 231.9845528, 1000.0):
            with self.subTest(value=value):
                self.assertAlmostEqual(inches_to_mm(mm_to_inches(value)), value, places=9)
                self.assertAlmostEqual(mm_to_inches(inches_to_mm(value)), value, places=9)


class TestNoneHandling(unittest.TestCase):
    def test_none_in_none_out_all_four_directions(self):
        for converter in (ms_to_mph, mm_to_inches, mph_to_ms, inches_to_mm):
            with self.subTest(converter=converter.__name__):
                self.assertIsNone(converter(None))

    def test_non_finite_is_treated_as_absent(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.assertIsNone(ms_to_mph(value))

    def test_bool_is_not_a_measurement(self):
        self.assertIsNone(ms_to_mph(True))

    def test_formatters_report_absence_never_zero(self):
        for formatter in (
            format_mph,
            format_inches,
            format_ms,
            format_mm,
            format_count,
            format_signed,
        ):
            with self.subTest(formatter=formatter.__name__):
                rendered = formatter(None)
                self.assertEqual(rendered, UNAVAILABLE)
                self.assertNotIn("0", rendered)


class TestFormatting(unittest.TestCase):
    def test_format_mph_and_inches(self):
        self.assertEqual(format_mph(12.070), "27.0 mph")
        self.assertEqual(format_inches(30.48), "1.20 in")

    def test_format_preserves_stored_units_when_asked(self):
        self.assertEqual(format_ms(9.2), "9.2 m/s")
        self.assertEqual(format_mm(30.5), "30.5 mm")

    def test_format_count_uses_thousands_separators(self):
        self.assertEqual(format_count(21557), "21,557")
        self.assertEqual(format_count(0), "0")

    def test_format_signed_always_shows_a_sign(self):
        self.assertEqual(format_signed(5453), "+5,453")
        self.assertEqual(format_signed(-5453), "-5,453")
        self.assertEqual(format_signed(0), "+0")


class TestParseMeasurement(unittest.TestCase):
    """Option A: forecast input requires an explicit unit suffix."""

    def test_speed_suffixes_convert_to_metres_per_second(self):
        self.assertAlmostEqual(parse_measurement("27mph", dimension=SPEED), 12.070, places=2)
        self.assertAlmostEqual(parse_measurement("12ms", dimension=SPEED), 12.0, places=9)
        self.assertAlmostEqual(parse_measurement("12 m/s", dimension=SPEED), 12.0, places=9)

    def test_depth_suffixes_convert_to_millimetres(self):
        self.assertAlmostEqual(parse_measurement("1.2in", dimension=DEPTH), 30.48, places=6)
        self.assertAlmostEqual(parse_measurement("30mm", dimension=DEPTH), 30.0, places=9)
        self.assertAlmostEqual(parse_measurement('1.2"', dimension=DEPTH), 30.48, places=6)

    def test_suffix_parsing_is_case_and_space_insensitive(self):
        self.assertAlmostEqual(
            parse_measurement("  27 MPH ", dimension=SPEED), 12.070, places=2
        )

    def test_bare_number_is_rejected_for_a_dimensioned_value(self):
        with self.assertRaises(AmbiguousUnitError):
            parse_measurement("12", dimension=SPEED)
        with self.assertRaises(AmbiguousUnitError):
            parse_measurement("30", dimension=DEPTH)

    def test_ambiguity_message_names_both_readings_and_equivalents(self):
        message = describe_ambiguity(12, dimension=SPEED)
        self.assertIn("mph", message)
        self.assertIn("ms", message)
        # 12 m/s is 26.8 mph; 12 mph is 5.36 m/s. Both must appear so the user
        # can see the size of the mistake.
        self.assertIn("26.8", message)
        self.assertIn("5.36", message)

    def test_depth_ambiguity_message_names_both_readings(self):
        message = describe_ambiguity(30, dimension=DEPTH)
        self.assertIn("mm", message)
        self.assertIn("in", message)
        self.assertIn("1.18", message)   # 30 mm in inches
        self.assertIn("762", message)    # 30 in in mm

    def test_counts_are_dimensionless_and_take_no_suffix(self):
        self.assertEqual(parse_measurement("4", dimension=COUNT), 4.0)
        with self.assertRaises(UnknownUnitError):
            parse_measurement("4mph", dimension=COUNT)

    def test_unknown_suffix_is_rejected_with_the_accepted_list(self):
        with self.assertRaises(UnknownUnitError) as caught:
            parse_measurement("27knots", dimension=SPEED)
        self.assertIn("mph", str(caught.exception))

    def test_non_measurement_text_raises(self):
        for raw in ("", "fast", "mph", "12.3.4"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_measurement(raw, dimension=SPEED)

    def test_supported_suffixes_and_canonical_unit(self):
        self.assertIn("mph", supported_suffixes(SPEED))
        self.assertIn("mm", supported_suffixes(DEPTH))
        self.assertEqual(canonical_unit(SPEED), "m/s")
        self.assertEqual(canonical_unit(DEPTH), "mm")

    def test_parsed_forecast_matches_the_direct_converter(self):
        """Parsing and converting must agree — one conversion path, not two."""
        self.assertAlmostEqual(
            parse_measurement("27mph", dimension=SPEED), mph_to_ms(27.0), places=12
        )
        self.assertAlmostEqual(
            parse_measurement("1.2in", dimension=DEPTH), inches_to_mm(1.2), places=12
        )


if __name__ == "__main__":
    unittest.main()
