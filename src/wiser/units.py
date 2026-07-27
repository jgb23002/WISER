"""Unit conversion between stored (metric) and reported (imperial) values.

The WISER database stores metric: ``peakWSPD10`` is m/s and
``sumAFWA_TOTPRECIP`` is mm. Stakeholder reports are imperial. Conversion
happens here, at the presentation boundary — never in stored data, and never
as a magic number scattered through a query or a renderer.

Two directions are needed:

* **Output** (``ms_to_mph``, ``mm_to_inches``) — DB values into a report.
* **Input** (``mph_to_ms``, ``inches_to_mm``) — forecast parameters, which
  arrive in imperial, into the units the similarity engine and WeatherData
  use.

Each dimension has exactly one constant; the inverse is defined as its
reciprocal so a round trip cannot drift.
"""

from __future__ import annotations

import math
import re

MS_TO_MPH = 2.23694
MM_PER_INCH = 25.4

# Rendered in place of a value that is absent. Never "0" — a missing
# measurement and a measured zero are different claims.
UNAVAILABLE = "not available"


# ---------------------------------------------------------------------------
# Output direction: stored metric → reported imperial
# ---------------------------------------------------------------------------


def ms_to_mph(value: float | None) -> float | None:
    """Metres per second → miles per hour."""
    if not _is_number(value):
        return None
    return float(value) * MS_TO_MPH


def mm_to_inches(value: float | None) -> float | None:
    """Millimetres → inches."""
    if not _is_number(value):
        return None
    return float(value) / MM_PER_INCH


# ---------------------------------------------------------------------------
# Input direction: supplied imperial → stored metric
# ---------------------------------------------------------------------------


def mph_to_ms(value: float | None) -> float | None:
    """Miles per hour → metres per second."""
    if not _is_number(value):
        return None
    return float(value) / MS_TO_MPH


def inches_to_mm(value: float | None) -> float | None:
    """Inches → millimetres."""
    if not _is_number(value):
        return None
    return float(value) * MM_PER_INCH


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_mph(value: float | None, digits: int = 1) -> str:
    """Format an m/s value as miles per hour."""
    converted = ms_to_mph(value)
    if converted is None:
        return UNAVAILABLE
    return f"{converted:,.{digits}f} mph"


def format_inches(value: float | None, digits: int = 2) -> str:
    """Format a mm value as inches."""
    converted = mm_to_inches(value)
    if converted is None:
        return UNAVAILABLE
    return f"{converted:,.{digits}f} in"


def format_ms(value: float | None, digits: int = 1) -> str:
    """Format an m/s value in its stored unit, for modeled-value labelling."""
    if not _is_number(value):
        return UNAVAILABLE
    return f"{float(value):,.{digits}f} m/s"


def format_mm(value: float | None, digits: int = 1) -> str:
    """Format a mm value in its stored unit."""
    if not _is_number(value):
        return UNAVAILABLE
    return f"{float(value):,.{digits}f} mm"


def format_count(value: float | None) -> str:
    """Format a whole-number count with thousands separators."""
    if not _is_number(value):
        return UNAVAILABLE
    number = float(value)
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}"


def format_signed(value: float | None, digits: int = 0) -> str:
    """Format a difference with an explicit sign, for comparison tables."""
    if not _is_number(value):
        return UNAVAILABLE
    return f"{float(value):+,.{digits}f}"


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


# ---------------------------------------------------------------------------
# Unit-suffixed input parsing
# ---------------------------------------------------------------------------
#
# Forecast input must carry an explicit unit suffix. A bare "wind=12" is
# genuinely ambiguous: read as metric it is 26.8 mph, near the top of the
# historical record; read as imperial it is 5.4 m/s, an ordinary day. The two
# readings differ by 5x and both are in range, so there is no safe default —
# and the error is largest at exactly the values a user is most likely to type.
# Bare numbers are therefore rejected, not guessed.

SPEED = "speed"
DEPTH = "depth"
COUNT = "count"

# suffix → multiplier into the canonical metric unit for that dimension.
_SPEED_SUFFIXES: dict[str, float] = {
    "mph": 1.0 / MS_TO_MPH,
    "mp/h": 1.0 / MS_TO_MPH,
    "miph": 1.0 / MS_TO_MPH,
    "ms": 1.0,
    "m/s": 1.0,
    "mps": 1.0,
}
_DEPTH_SUFFIXES: dict[str, float] = {
    "in": MM_PER_INCH,
    "inch": MM_PER_INCH,
    "inches": MM_PER_INCH,
    "mm": 1.0,
}
_SUFFIXES_BY_DIMENSION: dict[str, dict[str, float]] = {
    SPEED: _SPEED_SUFFIXES,
    DEPTH: _DEPTH_SUFFIXES,
}

_CANONICAL_UNIT = {SPEED: "m/s", DEPTH: "mm"}

_MEASUREMENT_PATTERN = re.compile(
    r"^\s*(?P<value>-?\d+(?:\.\d*)?)\s*(?P<suffix>[a-zA-Z/\"']*)\s*$"
)


class AmbiguousUnitError(ValueError):
    """Raised when a measurement carries no unit and cannot be read safely."""


class UnknownUnitError(ValueError):
    """Raised when a measurement carries a suffix we do not recognise."""


def supported_suffixes(dimension: str) -> tuple[str, ...]:
    """Suffixes accepted for a dimension, for use in error messages."""
    return tuple(sorted(_SUFFIXES_BY_DIMENSION.get(dimension, {})))


def parse_measurement(raw: str, *, dimension: str) -> float:
    """Parse a unit-suffixed measurement into its canonical metric value.

    ``parse_measurement("27mph", dimension=SPEED)`` → 12.07 (m/s)
    ``parse_measurement("1.2in", dimension=DEPTH)``  → 30.48 (mm)

    ``COUNT`` values (the wgt* threshold hours) are dimensionless and accept a
    bare number.

    Raises:
        AmbiguousUnitError: the value has no suffix and the dimension needs one.
        UnknownUnitError:   the suffix is not recognised for the dimension.
        ValueError:         the text is not a measurement at all.
    """
    match = _MEASUREMENT_PATTERN.match(str(raw))
    if not match:
        raise ValueError(f"not a measurement: {raw!r}")

    value = float(match.group("value"))
    suffix = match.group("suffix").strip().lower().replace('"', "in").replace("'", "")

    if dimension == COUNT:
        if suffix:
            raise UnknownUnitError(
                f"{raw!r} is a count of hours and takes no unit suffix."
            )
        return value

    suffixes = _SUFFIXES_BY_DIMENSION.get(dimension)
    if suffixes is None:
        raise ValueError(f"unknown dimension: {dimension!r}")

    if not suffix:
        raise AmbiguousUnitError(describe_ambiguity(value, dimension=dimension))

    if suffix not in suffixes:
        accepted = ", ".join(supported_suffixes(dimension))
        raise UnknownUnitError(f"unrecognised unit {suffix!r} in {raw!r}. Use: {accepted}")

    return value * suffixes[suffix]


def describe_ambiguity(value: float, *, dimension: str) -> str:
    """Explain why a bare number was rejected, showing both readings.

    The message names each interpretation and its equivalent in the other
    unit, so the user can see the size of the mistake they were about to make.
    """
    if dimension == SPEED:
        as_metric_mph = ms_to_mph(value)
        as_imperial_ms = mph_to_ms(value)
        return (
            f"'{_trim(value)}' has no unit, and wind speed cannot be guessed: "
            f"'{_trim(value)}ms' means {_trim(value)} m/s ({as_metric_mph:,.1f} mph), "
            f"while '{_trim(value)}mph' means {as_imperial_ms:,.2f} m/s. "
            "Add a unit suffix: mph or ms."
        )
    if dimension == DEPTH:
        as_metric_in = mm_to_inches(value)
        as_imperial_mm = inches_to_mm(value)
        return (
            f"'{_trim(value)}' has no unit, and precipitation cannot be guessed: "
            f"'{_trim(value)}mm' means {_trim(value)} mm ({as_metric_in:,.2f} in), "
            f"while '{_trim(value)}in' means {as_imperial_mm:,.1f} mm. "
            "Add a unit suffix: in or mm."
        )
    raise ValueError(f"unknown dimension: {dimension!r}")


def canonical_unit(dimension: str) -> str:
    """The stored unit a parsed measurement of this dimension is expressed in."""
    return _CANONICAL_UNIT.get(dimension, "")


def _trim(value: float) -> str:
    """Render a float without a trailing '.0', for use inside messages."""
    return f"{value:g}"
