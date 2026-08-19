"""Storm-scoped analysis: summary, ranking, and two-storm comparison.

This module owns every storm-scoped SQL query in WISER. Handlers and the
report layer call these functions and never write SQL themselves, so a move
to PostgreSQL had exactly one file to port — and inside that file, exactly one
function (``_fetch``) that binds parameters.

Portability rules this module holds to (see the plan, section 2):

* No engine-specific date functions: dates derive from ``stormCode``,
  which is an integer ``YYYYMMDDHH``, using integer arithmetic only.
* No ``ROUND(<float>, n)`` — PostgreSQL has no such overload for double
  precision. Rounding is the presentation layer's job.
* Ranking uses ``RANK() OVER`` / ``COUNT(*) OVER``.
* Rows are mapped to dataclasses **positionally**. Unquoted ``stormCode``
  folds to ``stormcode`` in PostgreSQL, so key-based access would break
  silently. This is what kept the migration mechanical.
* ``state`` is always a bound parameter, never a literal.
* Connections are typed against a ``DbConnection`` Protocol rather than a
  concrete driver connection class.

``tests/test_storm_analysis.py`` enforces the first two mechanically.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Protocol, Sequence

DEFAULT_STATE = "CT"

# Ranking metric keys. Duration is deliberately absent: Storm.eventLength is a
# fixed analysis window (identical for every storm), so a duration rank would
# be a tie across the whole record and carry no information.
METRIC_OUTAGE = "outage"
METRIC_MAX_WIND = "max_wind"
METRIC_MAX_GUST = "max_gust"
METRIC_MAX_PRECIP = "max_precip"

RANKABLE_METRICS: tuple[str, ...] = (METRIC_OUTAGE, METRIC_MAX_WIND, METRIC_MAX_PRECIP)

METRIC_LABELS: dict[str, str] = {
    METRIC_OUTAGE: "statewide outages",
    METRIC_MAX_WIND: "maximum wind speed",
    METRIC_MAX_GUST: "maximum wind gust",
    METRIC_MAX_PRECIP: "maximum precipitation",
}

COUNTY_METRIC_LABELS: dict[str, str] = {
    METRIC_OUTAGE: "outages",
    METRIC_MAX_WIND: "maximum wind speed",
    METRIC_MAX_GUST: "maximum wind gust",
    METRIC_MAX_PRECIP: "maximum precipitation",
}

# The wind-threshold column used for the comparison table's "counties above
# threshold" row. wgt13 is near-zero for most storms in this dataset, so wgt9
# is the threshold with usable spread.
COMPARISON_THRESHOLD_MS = 9
COMPARISON_THRESHOLD_COLUMN = f"wgt{COMPARISON_THRESHOLD_MS}"
COMPARISON_THRESHOLD_LABEL = (
    f"Counties with {COMPARISON_THRESHOLD_MS}+ m/s wind hours"
)


class DbConnection(Protocol):
    """Minimal read surface. Deliberately not a concrete driver class."""

    def execute(
        self, sql: str, parameters: Sequence[Any] | None = ..., /
    ) -> Any: ...


# ---------------------------------------------------------------------------
# The single parameter-binding funnel
# ---------------------------------------------------------------------------


def _fetch(
    conn: DbConnection,
    sql: str,
    params: Sequence[Any] = (),
) -> list[tuple]:
    """Execute a read query and return raw positional rows.

    Every query in this module goes through here. Placeholders are psycopg's
    ``%s`` style and are written out at each call site rather than rewritten
    here: a regex pass over the SQL would also corrupt any literal ``?`` or
    ``%`` inside a string literal.

    Empty params are passed as ``None``, not ``()``. psycopg only enters
    parameter-interpolation mode when parameters are supplied, and in that
    mode a literal ``%`` in the SQL is an error unless doubled. Passing
    ``None`` keeps parameterless queries on the simple protocol.
    """
    cursor = conn.execute(sql, tuple(params) if params else None)
    return [tuple(row) for row in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StormRef:
    """A storm identified in the database."""

    storm_code: int
    event_datetime: str
    event_length: int

    @property
    def date(self) -> str:
        """The calendar date, derived from stormCode without a date function."""
        return storm_code_to_date(self.storm_code)


@dataclass(frozen=True)
class StormResolution:
    """The outcome of resolving a user-supplied storm token.

    A date can name more than one storm (the dataset has same-day storm pairs
    at different hours), so an ambiguous resolution is a first-class result,
    not an error and never an arbitrary pick.
    """

    token: str
    matches: tuple[StormRef, ...] = ()

    @property
    def ok(self) -> bool:
        return len(self.matches) == 1

    @property
    def ambiguous(self) -> bool:
        return len(self.matches) > 1

    @property
    def missing(self) -> bool:
        return not self.matches

    @property
    def storm(self) -> StormRef | None:
        return self.matches[0] if self.ok else None


@dataclass(frozen=True)
class CountyStormRow:
    """One county's observations for one storm."""

    county: str
    state: str
    outage: int
    peak_wind_ms: float
    peak_gust_ms: float | None
    precip_mm: float
    wgt5: int
    wgt9: int
    wgt13: int
    wgt18: int
    oh_line: int

    @property
    def outage_per_million_ohline(self) -> float | None:
        if not self.oh_line:
            return None
        return self.outage * 1_000_000.0 / self.oh_line

    def threshold_hours(self, column: str) -> int:
        return int(getattr(self, column))


@dataclass(frozen=True)
class CountyValue:
    """A county paired with one metric value, for 'highest X in Y County'."""

    county: str
    value: float


# Gust thresholds, strongest first. wgt* counts HOURS above a threshold, so it
# is exposure, never a peak magnitude — the highest crossed threshold is a
# measured floor on the gust, not the gust itself.
GUST_THRESHOLDS: tuple[int, ...] = (18, 13, 9, 5)


@dataclass(frozen=True)
class GustExposure:
    """The strongest gust threshold this storm crossed, and for how long.

    ``threshold_ms`` is a floor: the gust reached at least this speed for
    ``hours`` hours. When ``peakGUST10`` exists it is reported separately.
    """

    threshold_ms: int
    hours: int
    county: str


@dataclass(frozen=True)
class StormSummary:
    storm: StormRef
    state: str
    county_scope: str | None
    counties: tuple[CountyStormRow, ...]
    statewide_outage: int
    max_wind: CountyValue | None
    max_precip: CountyValue | None
    top_outage_counties: tuple[CountyValue, ...]
    top_normalized_counties: tuple[CountyValue, ...]
    counties_above_threshold: int
    # Measured exposure, not a magnitude. None when no threshold was crossed.
    gust_exposure: GustExposure | None = None
    # Phase 2 fields. Always None until the external metadata table lands;
    # never inferred, never defaulted to a number.
    storm_type: None = None
    peak_snowfall_mm: None = None
    # Peak gust MAGNITUDE from source data. None for legacy rows where only
    # wgt* threshold-hour exposure is available.
    wind_gust_ms: CountyValue | None = None

    @property
    def scope_label(self) -> str:
        if self.county_scope:
            return f"{self.county_scope} County"
        return f"statewide ({self.state})"

    @property
    def top_outage_share(self) -> float | None:
        """The leading county's share of the storm's total, 0..1."""
        if not self.statewide_outage or not self.top_outage_counties:
            return None
        return self.top_outage_counties[0].value / self.statewide_outage


@dataclass(frozen=True)
class MetricRank:
    metric: str
    label: str
    value: float | None
    rank: int
    total: int
    tie_count: int

    @property
    def tied(self) -> bool:
        return self.tie_count > 1

    @property
    def percentile(self) -> float:
        """Top-N percent. Rank 1 of 294 → 0.34."""
        if not self.total:
            return 0.0
        return self.rank / self.total * 100.0


@dataclass(frozen=True)
class StormRanking:
    storm: StormRef
    state: str
    county_scope: str | None
    total_storms: int
    metrics: tuple[MetricRank, ...]

    @property
    def scope_label(self) -> str:
        if self.county_scope:
            return f"{self.county_scope} County"
        return f"statewide ({self.state})"

    def metric(self, key: str) -> MetricRank | None:
        for entry in self.metrics:
            if entry.metric == key:
                return entry
        return None


@dataclass(frozen=True)
class MetricDelta:
    """One row of the two-storm comparison table."""

    metric: str
    label: str
    value_a: float | None
    value_b: float | None

    @property
    def difference(self) -> float | None:
        if self.value_a is None or self.value_b is None:
            return None
        return self.value_a - self.value_b

    @property
    def percent_change(self) -> float | None:
        difference = self.difference
        if difference is None or not self.value_b:
            return None
        return difference / self.value_b * 100.0


@dataclass(frozen=True)
class StormComparison:
    summary_a: StormSummary
    summary_b: StormSummary
    ranking_a: StormRanking
    ranking_b: StormRanking
    deltas: tuple[MetricDelta, ...]
    state: str


@dataclass(frozen=True)
class ForecastPosition:
    """Where a forecast value would sit in the historical record."""

    metric: str
    label: str
    value: float
    rank: int
    total: int

    @property
    def percentile(self) -> float:
        if not self.total:
            return 0.0
        return self.rank / self.total * 100.0


@dataclass(frozen=True)
class AnalogueStorm:
    """A past storm with similar modelled conditions, and what it produced.

    ``outage`` is that historical storm's recorded outcome. It is never a
    prediction for the forecast storm, and the report layer must label it so.
    """

    storm_code: int
    date: str
    county: str
    similarity: float
    peak_wind_ms: float
    precip_mm: float
    outage: int


@dataclass(frozen=True)
class CountyAnalogues:
    county: str
    analogues: tuple[AnalogueStorm, ...]


@dataclass(frozen=True)
class ForecastComparison:
    """A forecast storm positioned against the historical record.

    Deliberately carries no outage value for the forecast itself: outage is an
    outcome, and a forecast supplies only conditions. The analogues carry real
    recorded outages; nothing here predicts one.
    """

    params: dict[str, float]
    county_scope: str | None
    state: str
    total_storms: int
    positions: tuple[ForecastPosition, ...]
    analogue_groups: tuple[CountyAnalogues, ...]
    #: Counties in the state that were not shown, so the report can say so
    #: rather than silently presenting a partial picture as the whole.
    counties_omitted: int = 0

    @property
    def scope_label(self) -> str:
        if self.county_scope:
            return f"{self.county_scope} County"
        return f"statewide ({self.state})"


@dataclass(frozen=True)
class PeriodSpec:
    """A month, season, year, or custom group of storms."""

    key: str
    label: str
    kind: str
    month: int | None = None
    season: str | None = None
    year: int | None = None
    exclude_key: str | None = None

    def includes(self, storm_code: int) -> bool:
        year, month, _day = storm_code_to_ymd(storm_code)
        if self.exclude_key:
            excluded = period_from_key(self.exclude_key)
            return not excluded.includes(storm_code)
        if self.year is not None and year != self.year:
            return False
        if self.month is not None:
            return month == self.month
        if self.season is not None:
            return month in SEASON_MONTHS[self.season]
        return True


@dataclass(frozen=True)
class GroupObservation:
    storm_code: int
    date: str
    county: str
    outage: int
    peak_wind_ms: float
    peak_gust_ms: float | None
    precip_mm: float
    humidity: float | None
    wgt5: int
    wgt9: int
    wgt13: int
    wgt18: int
    oh_line: int

    def value(self, metric_key: str) -> float:
        if metric_key == "outage":
            return float(self.outage)
        if metric_key == "peakWSPD10":
            return self.peak_wind_ms
        if metric_key == "peakGUST10":
            if self.peak_gust_ms is None:
                raise StormAnalysisError("peak gust magnitude is not available")
            return self.peak_gust_ms
        if metric_key == "sumAFWA_TOTPRECIP":
            return self.precip_mm
        if metric_key == "humidity":
            if self.humidity is None:
                raise StormAnalysisError("humidity is not available")
            return self.humidity
        if metric_key in {"wgt5", "wgt9", "wgt13", "wgt18"}:
            return float(getattr(self, metric_key))
        raise StormAnalysisError(f"metric {metric_key!r} is not available")


@dataclass(frozen=True)
class StormOutagePoint:
    group_label: str
    storm_code: int
    date: str
    outage: int


@dataclass(frozen=True)
class StormGroupSummary:
    key: str
    label: str
    group_kind: str
    region: str | None
    storm_count: int
    county_count: int
    total_outage: int
    avg_outage_per_storm: float | None
    median_outage_per_storm: float | None
    max_storm_outage: int | None
    max_storm_code: int | None
    peak_wind_ms: float | None
    peak_gust_ms: float | None
    max_precip_mm: float | None
    avg_precip_mm: float | None
    max_humidity: float | None
    avg_humidity: float | None
    gust_hours_9: int
    gust_hours_13: int
    outage_per_million_ohline: float | None
    storm_outages: tuple[StormOutagePoint, ...]
    data_source: str = "historical"


@dataclass(frozen=True)
class StormGroupAnalysis:
    title: str
    scope_label: str
    group_by: str
    summaries: tuple[StormGroupSummary, ...]
    rows: tuple[dict[str, Any], ...]
    distribution_rows: tuple[dict[str, Any], ...]
    region_rows: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ThresholdSpec:
    metric_key: str
    label: str
    stored_threshold: float
    display_threshold: str
    comparison_metric: str | None = None


@dataclass(frozen=True)
class ThresholdAnalysis:
    title: str
    threshold: ThresholdSpec
    scope_label: str
    rows: tuple[dict[str, Any], ...]
    matched_rows: int
    matched_storms: int
    matched_counties: int
    total_rows: int
    used_gust_exposure_fallback: bool = False
    source: str = "historical"


class StormAnalysisError(ValueError):
    """Raised when a requested storm or metric cannot be analysed."""


MONTH_NAMES: tuple[str, ...] = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

SEASON_MONTHS: dict[str, tuple[int, ...]] = {
    "winter": (12, 1, 2),
    "spring": (3, 4, 5),
    "summer": (6, 7, 8),
    "fall": (9, 10, 11),
}
SEASON_LABELS: dict[str, str] = {
    "winter": "Winter",
    "spring": "Spring",
    "summer": "Summer",
    "fall": "Fall",
}

DEFAULT_REGION_COUNTIES: dict[str, tuple[str, ...]] = {
    "Northern CT": ("Hartford", "Litchfield", "Tolland", "Windham"),
    "Southern CT": ("Fairfield", "Middlesex", "New Haven", "New London"),
    "Eastern CT": ("New London", "Tolland", "Windham"),
    "Western CT": ("Fairfield", "Litchfield"),
    "Central CT": ("Hartford", "Middlesex", "New Haven"),
}


# ---------------------------------------------------------------------------
# Storm code / date helpers — integer arithmetic, no date functions
# ---------------------------------------------------------------------------


def storm_code_to_date(storm_code: int) -> str:
    """``2020080406`` → ``'2020-08-04'``.

    stormCode is YYYYMMDDHH, so the date is integer division by 100. Doing it
    this way keeps the module free of SQL date functions and of any dependence
    on how a driver returns a TIMESTAMP column.
    """
    day = int(storm_code) // 100
    year, month, date = day // 10000, (day // 100) % 100, day % 100
    return f"{year:04d}-{month:02d}-{date:02d}"


def storm_code_to_ymd(storm_code: int) -> tuple[int, int, int]:
    day = int(storm_code) // 100
    return day // 10000, (day // 100) % 100, day % 100


def storm_code_hour(storm_code: int) -> int:
    return int(storm_code) % 100


def date_to_storm_code_range(iso_date: str) -> tuple[int, int]:
    """``'2020-08-04'`` → ``(2020080400, 2020080500)``, a half-open range.

    Integer bounds rather than timestamp comparison: portable across drivers
    and immune to how a TIMESTAMP column is textually rendered.
    """
    parts = iso_date.split("-")
    if len(parts) != 3:
        raise StormAnalysisError(f"not an ISO date: {iso_date!r}")
    try:
        year, month, day = (int(part) for part in parts)
    except ValueError as exc:
        raise StormAnalysisError(f"not an ISO date: {iso_date!r}") from exc
    lower = ((year * 10000) + (month * 100) + day) * 100
    return lower, lower + 100


def month_period(month: int, *, year: int | None = None) -> PeriodSpec:
    label = MONTH_NAMES[int(month) - 1]
    key = f"month:{int(month):02d}"
    if year is not None:
        label = f"{label} {year}"
        key = f"{key}:{int(year)}"
    return PeriodSpec(key=key, label=label, kind="month", month=int(month), year=year)


def season_period(season: str, *, year: int | None = None) -> PeriodSpec:
    key_name = season.lower()
    if key_name == "autumn":
        key_name = "fall"
    if key_name not in SEASON_MONTHS:
        raise StormAnalysisError(f"unknown season: {season!r}")
    label = SEASON_LABELS[key_name]
    key = f"season:{key_name}"
    if year is not None:
        label = f"{label} {year}"
        key = f"{key}:{int(year)}"
    return PeriodSpec(key=key, label=label, kind="season", season=key_name, year=year)


def all_period(label: str = "All storms") -> PeriodSpec:
    return PeriodSpec(key="all", label=label, kind="all")


def other_period(base: PeriodSpec, label: str | None = None) -> PeriodSpec:
    return PeriodSpec(
        key=f"not:{base.key}",
        label=label or f"All storms outside {base.label}",
        kind="baseline",
        exclude_key=base.key,
    )


def period_from_key(key: str) -> PeriodSpec:
    if key == "all":
        return all_period()
    if key.startswith("month:"):
        parts = key.split(":")
        year = int(parts[2]) if len(parts) > 2 else None
        return month_period(int(parts[1]), year=year)
    if key.startswith("season:"):
        parts = key.split(":")
        year = int(parts[2]) if len(parts) > 2 else None
        return season_period(parts[1], year=year)
    raise StormAnalysisError(f"unknown period key: {key!r}")


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_storm(conn: DbConnection, token: str) -> StormResolution:
    """Resolve a storm code or ISO date to the storm(s) it names."""
    text = str(token).strip()

    if text.isdigit() and len(text) == 10:
        rows = _fetch(
            conn,
            """
            SELECT stormCode, eventDateTime, eventLength
            FROM Storm
            WHERE stormCode = %s
            """,
            (int(text),),
        )
        return StormResolution(token=text, matches=tuple(_storm_ref(row) for row in rows))

    lower, upper = date_to_storm_code_range(text)
    rows = _fetch(
        conn,
        """
        SELECT stormCode, eventDateTime, eventLength
        FROM Storm
        WHERE stormCode >= %s AND stormCode < %s
        ORDER BY stormCode ASC
        """,
        (lower, upper),
    )
    return StormResolution(token=text, matches=tuple(_storm_ref(row) for row in rows))


def same_date_siblings(conn: DbConnection, storm_code: int) -> tuple[StormRef, ...]:
    """Other storms sharing this storm's calendar date.

    Every storm carries the same fixed 48-hour analysis window, so two storms
    entered 6 or 12 hours apart have windows overlapping by 36-42 hours. In the
    current dataset such pairs report near-identical county outages while their
    weather aggregates differ, which is what one weather system recorded twice
    looks like.

    Reported, never merged: deduplicating would be a data decision, and the
    pairs are real rows.
    """
    lower, upper = date_to_storm_code_range(storm_code_to_date(storm_code))
    rows = _fetch(
        conn,
        """
        SELECT stormCode, eventDateTime, eventLength
        FROM Storm
        WHERE stormCode >= %s AND stormCode < %s AND stormCode <> %s
        ORDER BY stormCode ASC
        """,
        (lower, upper, int(storm_code)),
    )
    return tuple(_storm_ref(row) for row in rows)


def storm_count(conn: DbConnection, *, state: str = DEFAULT_STATE) -> int:
    """Number of storms with observations in a state."""
    rows = _fetch(
        conn,
        """
        SELECT COUNT(DISTINCT w.stormCode)
        FROM WeatherData w
        JOIN County c ON LOWER(c.county) = LOWER(w.county)
        WHERE LOWER(c.state) = LOWER(%s)
        """,
        (state,),
    )
    return int(rows[0][0]) if rows else 0


# ---------------------------------------------------------------------------
# Capability 1 — storm summary
# ---------------------------------------------------------------------------


def storm_summary(
    conn: DbConnection,
    storm_code: int,
    *,
    county: str | None = None,
    state: str = DEFAULT_STATE,
    threshold_column: str = COMPARISON_THRESHOLD_COLUMN,
) -> StormSummary:
    """Per-county observations for one storm, plus the derived headline values.

    Aggregates (statewide total, argmax county, top-N) are computed in Python
    from the county rows. There are eight counties; a second aggregate round
    trip would buy nothing and cost portability.
    """
    header = _fetch(
        conn,
        """
        SELECT stormCode, eventDateTime, eventLength
        FROM Storm
        WHERE stormCode = %s
        """,
        (int(storm_code),),
    )
    if not header:
        raise StormAnalysisError(f"storm {storm_code} is not in the database")
    storm = _storm_ref(header[0])

    sql = """
        SELECT
            w.county,
            c.state,
            w.outage,
            w.peakWSPD10,
            w.peakGUST10,
            w.sumAFWA_TOTPRECIP,
            w.wgt5,
            w.wgt9,
            w.wgt13,
            w.wgt18,
            c.ohLine
        FROM WeatherData w
        JOIN County c ON LOWER(c.county) = LOWER(w.county)
        WHERE w.stormCode = %s
          AND LOWER(c.state) = LOWER(%s)
    """
    params: list[Any] = [int(storm_code), state]
    if county:
        sql += " AND LOWER(w.county) = LOWER(%s)"
        params.append(county)
    sql += " ORDER BY w.outage DESC, w.county ASC"

    counties = tuple(_county_storm_row(row) for row in _fetch(conn, sql, params))

    statewide_outage = sum(row.outage for row in counties)
    normalized = [
        CountyValue(row.county, row.outage_per_million_ohline)
        for row in counties
        if row.outage_per_million_ohline is not None
    ]

    return StormSummary(
        storm=storm,
        state=state,
        county_scope=counties[0].county if county and counties else (county or None),
        counties=counties,
        statewide_outage=statewide_outage,
        max_wind=_argmax(counties, lambda row: row.peak_wind_ms),
        wind_gust_ms=_argmax_optional(counties, lambda row: row.peak_gust_ms),
        max_precip=_argmax(counties, lambda row: row.precip_mm),
        top_outage_counties=tuple(
            sorted(
                (CountyValue(row.county, float(row.outage)) for row in counties),
                key=lambda item: (-item.value, item.county),
            )[:3]
        ),
        top_normalized_counties=tuple(
            sorted(normalized, key=lambda item: (-item.value, item.county))[:3]
        ),
        counties_above_threshold=sum(
            1 for row in counties if row.threshold_hours(threshold_column) > 0
        ),
        gust_exposure=_gust_exposure(counties),
    )


def _gust_exposure(counties: Sequence[CountyStormRow]) -> GustExposure | None:
    """Strongest gust threshold crossed, with the longest exposure at it."""
    for threshold in GUST_THRESHOLDS:
        crossing = [
            row for row in counties if row.threshold_hours(f"wgt{threshold}") > 0
        ]
        if not crossing:
            continue
        # Same tiebreak as _argmax: most hours, then alphabetically first, so
        # two equal counties always resolve the same way in both places.
        leader = sorted(
            crossing,
            key=lambda row: (-row.threshold_hours(f"wgt{threshold}"), row.county),
        )[0]
        return GustExposure(
            threshold_ms=threshold,
            hours=leader.threshold_hours(f"wgt{threshold}"),
            county=leader.county,
        )
    return None


# ---------------------------------------------------------------------------
# Capability 2 — storm ranking
# ---------------------------------------------------------------------------

# One template, two scope variants. The inner SELECT differs only in whether
# county rows are aggregated to state level; the ranking half is identical.
_STATE_SCOPE_SQL = """
        SELECT
            w.stormCode AS storm_code,
            SUM(w.outage) AS outage,
            MAX(w.peakWSPD10) AS max_wind,
            MAX(w.sumAFWA_TOTPRECIP) AS max_precip
        FROM WeatherData w
        JOIN County c ON LOWER(c.county) = LOWER(w.county)
        WHERE LOWER(c.state) = LOWER(%s)
        GROUP BY w.stormCode
"""

_COUNTY_SCOPE_SQL = """
        SELECT
            w.stormCode AS storm_code,
            w.outage AS outage,
            w.peakWSPD10 AS max_wind,
            w.sumAFWA_TOTPRECIP AS max_precip
        FROM WeatherData w
        JOIN County c ON LOWER(c.county) = LOWER(w.county)
        WHERE LOWER(c.state) = LOWER(%s)
          AND LOWER(w.county) = LOWER(%s)
"""

_RANKING_SQL_TEMPLATE = """
    WITH storm_scope AS ({scope}),
    ranked AS (
        SELECT
            storm_code,
            outage,
            max_wind,
            max_precip,
            RANK() OVER (ORDER BY outage DESC) AS rank_outage,
            RANK() OVER (ORDER BY max_wind DESC) AS rank_max_wind,
            RANK() OVER (ORDER BY max_precip DESC) AS rank_max_precip,
            COUNT(*) OVER (PARTITION BY outage) AS tie_outage,
            COUNT(*) OVER (PARTITION BY max_wind) AS tie_max_wind,
            COUNT(*) OVER (PARTITION BY max_precip) AS tie_max_precip,
            COUNT(*) OVER () AS total_storms
        FROM storm_scope
    )
    SELECT
        outage, rank_outage, tie_outage,
        max_wind, rank_max_wind, tie_max_wind,
        max_precip, rank_max_precip, tie_max_precip,
        total_storms
    FROM ranked
    WHERE storm_code = %s
"""


def storm_ranking(
    conn: DbConnection,
    storm_code: int,
    *,
    metrics: Sequence[str] | None = None,
    county: str | None = None,
    state: str = DEFAULT_STATE,
) -> StormRanking:
    """Rank one storm against the historical record.

    Default scope is state level: county rows are aggregated per storm
    (SUM outage, MAX wind, MAX precip) before ranking. Naming a county ranks
    within that county's history instead.

    Rank and total always travel together — a rank alone is not a claim.
    """
    requested = tuple(metrics) if metrics else RANKABLE_METRICS
    unknown = [metric for metric in requested if metric not in RANKABLE_METRICS]
    if unknown:
        raise StormAnalysisError(
            f"not a rankable metric: {', '.join(unknown)}. "
            f"Available: {', '.join(RANKABLE_METRICS)}."
        )

    header = _fetch(
        conn,
        "SELECT stormCode, eventDateTime, eventLength FROM Storm WHERE stormCode = %s",
        (int(storm_code),),
    )
    if not header:
        raise StormAnalysisError(f"storm {storm_code} is not in the database")

    if county:
        sql = _RANKING_SQL_TEMPLATE.format(scope=_COUNTY_SCOPE_SQL)
        params: list[Any] = [state, county, int(storm_code)]
    else:
        sql = _RANKING_SQL_TEMPLATE.format(scope=_STATE_SCOPE_SQL)
        params = [state, int(storm_code)]

    rows = _fetch(conn, sql, params)
    if not rows:
        scope = f"{county} County" if county else state
        raise StormAnalysisError(
            f"storm {storm_code} has no observations in {scope}"
        )

    row = rows[0]
    total_storms = int(row[9])
    labels = COUNTY_METRIC_LABELS if county else METRIC_LABELS
    # Column offsets: (value, rank, tie) triples in RANKABLE_METRICS order.
    offsets = {METRIC_OUTAGE: 0, METRIC_MAX_WIND: 3, METRIC_MAX_PRECIP: 6}

    ranked = tuple(
        MetricRank(
            metric=metric,
            label=labels[metric],
            value=_as_optional_float(row[offsets[metric]]),
            rank=int(row[offsets[metric] + 1]),
            total=total_storms,
            tie_count=int(row[offsets[metric] + 2]),
        )
        for metric in requested
    )

    return StormRanking(
        storm=_storm_ref(header[0]),
        state=state,
        county_scope=county,
        total_storms=total_storms,
        metrics=ranked,
    )


# ---------------------------------------------------------------------------
# Capability 3, Mode A — two database storms
# ---------------------------------------------------------------------------


def compare_storms(
    conn: DbConnection,
    storm_code_a: int,
    storm_code_b: int,
    *,
    state: str = DEFAULT_STATE,
    threshold_column: str = COMPARISON_THRESHOLD_COLUMN,
) -> StormComparison:
    """Compare two storms already in the database.

    Built entirely from ``storm_summary`` and ``storm_ranking`` so there is no
    third SQL path to keep in step with the other two.
    """
    summary_a = storm_summary(
        conn, storm_code_a, state=state, threshold_column=threshold_column
    )
    summary_b = storm_summary(
        conn, storm_code_b, state=state, threshold_column=threshold_column
    )
    ranking_a = storm_ranking(conn, storm_code_a, state=state)
    ranking_b = storm_ranking(conn, storm_code_b, state=state)

    deltas = (
        MetricDelta(
            metric=METRIC_OUTAGE,
            label="Statewide outages",
            value_a=float(summary_a.statewide_outage),
            value_b=float(summary_b.statewide_outage),
        ),
        MetricDelta(
            metric=METRIC_MAX_WIND,
            label="Max wind speed",
            value_a=summary_a.max_wind.value if summary_a.max_wind else None,
            value_b=summary_b.max_wind.value if summary_b.max_wind else None,
        ),
        MetricDelta(
            metric=METRIC_MAX_GUST,
            label="Max wind gust",
            value_a=summary_a.wind_gust_ms.value if summary_a.wind_gust_ms else None,
            value_b=summary_b.wind_gust_ms.value if summary_b.wind_gust_ms else None,
        ),
        MetricDelta(
            metric=METRIC_MAX_PRECIP,
            label="Max precipitation",
            value_a=summary_a.max_precip.value if summary_a.max_precip else None,
            value_b=summary_b.max_precip.value if summary_b.max_precip else None,
        ),
        MetricDelta(
            metric="counties_above_threshold",
            label=COMPARISON_THRESHOLD_LABEL,
            value_a=float(summary_a.counties_above_threshold),
            value_b=float(summary_b.counties_above_threshold),
        ),
    )

    return StormComparison(
        summary_a=summary_a,
        summary_b=summary_b,
        ranking_a=ranking_a,
        ranking_b=ranking_b,
        deltas=deltas,
        state=state,
    )


# ---------------------------------------------------------------------------
# Capability 3, Mode B — a forecast storm against the historical record
# ---------------------------------------------------------------------------

# Forecast parameters that can be positioned against the record. wgt* values
# still feed the similarity search; they simply have no percentile row.
_POSITIONABLE = {
    "peakWSPD10": (METRIC_MAX_WIND, "maximum wind speed"),
    "sumAFWA_TOTPRECIP": (METRIC_MAX_PRECIP, "maximum precipitation"),
}

_FORECAST_POSITION_SQL_TEMPLATE = """
    WITH storm_scope AS ({scope})
    SELECT
        SUM(CASE WHEN max_wind > %s THEN 1 ELSE 0 END),
        SUM(CASE WHEN max_precip > %s THEN 1 ELSE 0 END),
        COUNT(*)
    FROM storm_scope
"""


def state_counties(conn: DbConnection, *, state: str = DEFAULT_STATE) -> tuple[str, ...]:
    """County names in a state, ordered."""
    rows = _fetch(
        conn,
        "SELECT county FROM County WHERE LOWER(state) = LOWER(%s) ORDER BY county ASC",
        (state,),
    )
    return tuple(str(row[0]) for row in rows)


#: Analogues shown per county, and counties shown when the scope is statewide.
#: Eight counties at ten rows each is eighty rows of table for one question.
DEFAULT_ANALOGUES_PER_COUNTY = 3
DEFAULT_ANALOGUE_COUNTIES = 3


def compare_forecast(
    conn: DbConnection,
    params: dict[str, float],
    *,
    county: str | None = None,
    state: str = DEFAULT_STATE,
    limit: int = DEFAULT_ANALOGUES_PER_COUNTY,
    max_counties: int | None = DEFAULT_ANALOGUE_COUNTIES,
    similarity_agent: Any | None = None,
) -> ForecastComparison:
    """Position a forecast against the record and find its closest analogues.

    Retrieval and scoring are delegated to ``SimilarityQueryAgent`` — this is
    the existing similarity search with a comparison report on top, not a
    parallel implementation of it.

    Scope note: the similarity engine standardises features to Z-scores over
    **one county's** history, so scores from different counties live in
    different normalised spaces and are not comparable. A statewide request
    therefore returns analogues grouped per county rather than a single merged
    ranking, which would be arithmetic on incommensurable numbers.
    """
    if not params:
        raise StormAnalysisError(
            "a forecast comparison needs at least one weather parameter"
        )

    if similarity_agent is None:  # imported lazily: keeps the module importable
        from wiser.agents.similarity_query_agent import SimilarityQueryAgent

        similarity_agent = SimilarityQueryAgent()

    if county:
        counties: tuple[str, ...] = (county,)
        counties_omitted = 0
    else:
        # Statewide: rank counties by historical outage so the shown few are
        # the ones that matter, and say how many were left out.
        ranked = _counties_by_outage(conn, state=state)
        if not ranked:
            raise StormAnalysisError(f"no counties found for state {state!r}")
        counties = tuple(ranked[:max_counties] if max_counties else ranked)
        counties_omitted = len(ranked) - len(counties)

    positions = _forecast_positions(conn, params, county=county, state=state)
    total_storms = (
        positions[0].total
        if positions
        else _scope_storm_total(conn, county=county, state=state)
    )

    groups: list[CountyAnalogues] = []
    for scope_county in counties:
        matches = similarity_agent.find_similar(conn, scope_county, params, limit=limit)
        if not matches:
            continue
        groups.append(
            CountyAnalogues(
                county=scope_county,
                analogues=tuple(
                    _analogue_storm(match, scope_county) for match in matches
                ),
            )
        )

    return ForecastComparison(
        params=dict(params),
        county_scope=county,
        state=state,
        total_storms=total_storms,
        positions=positions,
        analogue_groups=tuple(groups),
        counties_omitted=counties_omitted,
    )


def _counties_by_outage(
    conn: DbConnection, *, state: str = DEFAULT_STATE
) -> list[str]:
    """Counties in a state, worst-hit first across the whole record."""
    rows = _fetch(
        conn,
        """
        SELECT w.county, SUM(w.outage) AS total_outage
        FROM WeatherData w
        JOIN County c ON LOWER(c.county) = LOWER(w.county)
        WHERE LOWER(c.state) = LOWER(%s)
        GROUP BY w.county
        ORDER BY total_outage DESC, w.county ASC
        """,
        (state,),
    )
    return [str(row[0]) for row in rows]


def _forecast_positions(
    conn: DbConnection,
    params: dict[str, float],
    *,
    county: str | None,
    state: str,
) -> tuple[ForecastPosition, ...]:
    """Where each positionable forecast value would rank in the record."""
    positionable = {
        feature: value
        for feature, value in params.items()
        if feature in _POSITIONABLE
    }
    if not positionable:
        return ()

    if county:
        sql = _FORECAST_POSITION_SQL_TEMPLATE.format(scope=_COUNTY_SCOPE_SQL)
        scope_params: list[Any] = [state, county]
    else:
        sql = _FORECAST_POSITION_SQL_TEMPLATE.format(scope=_STATE_SCOPE_SQL)
        scope_params = [state]

    # Absent parameters bind a value that cannot be exceeded downward; their
    # column is discarded below, so the bound value never reaches the report.
    wind = float(positionable.get("peakWSPD10", 0.0))
    precip = float(positionable.get("sumAFWA_TOTPRECIP", 0.0))

    rows = _fetch(conn, sql, [*scope_params, wind, precip])
    if not rows or rows[0][2] in (None, 0):
        return ()

    better_wind, better_precip, total = rows[0]
    counts = {
        "peakWSPD10": int(better_wind or 0),
        "sumAFWA_TOTPRECIP": int(better_precip or 0),
    }

    return tuple(
        ForecastPosition(
            metric=_POSITIONABLE[feature][0],
            label=_POSITIONABLE[feature][1],
            value=float(value),
            rank=counts[feature] + 1,
            total=int(total),
        )
        for feature, value in positionable.items()
    )


def _scope_storm_total(
    conn: DbConnection, *, county: str | None, state: str
) -> int:
    if not county:
        return storm_count(conn, state=state)
    rows = _fetch(
        conn,
        """
        SELECT COUNT(DISTINCT w.stormCode)
        FROM WeatherData w
        JOIN County c ON LOWER(c.county) = LOWER(w.county)
        WHERE LOWER(c.state) = LOWER(%s)
          AND LOWER(w.county) = LOWER(%s)
        """,
        (state, county),
    )
    return int(rows[0][0]) if rows else 0


def _analogue_storm(match: dict[str, Any], county: str) -> AnalogueStorm:
    storm_code = int(match["stormCode"])
    return AnalogueStorm(
        storm_code=storm_code,
        date=storm_code_to_date(storm_code),
        county=county,
        similarity=float(match.get("similarity", 0.0)),
        peak_wind_ms=float(match["peakWSPD10"]),
        precip_mm=float(match["sumAFWA_TOTPRECIP"]),
        outage=int(match["outage"]),
    )


# ---------------------------------------------------------------------------
# Sponsor follow-up — period, group, region, and threshold analytics
# ---------------------------------------------------------------------------


def storm_group_analysis(
    conn: DbConnection,
    periods: Sequence[PeriodSpec],
    *,
    title: str,
    group_by: str,
    region: str | None = None,
    county: str | None = None,
    state: str = DEFAULT_STATE,
) -> StormGroupAnalysis:
    """Summarise storm groups such as July vs February or month by month."""
    observations = _group_observations(
        conn, state=state, region=region, county=county
    )
    summaries = tuple(
        _summarize_group(
            period,
            [
                row for row in observations
                if period.includes(row.storm_code)
            ],
            region=region or (f"{county} County" if county else None),
        )
        for period in periods
    )
    return StormGroupAnalysis(
        title=title,
        scope_label=_scope_label(region, state, county=county),
        group_by=group_by,
        summaries=summaries,
        rows=tuple(_group_summary_row(summary) for summary in summaries),
        distribution_rows=tuple(
            _distribution_row(point)
            for summary in summaries
            for point in summary.storm_outages
        ),
        region_rows=tuple(region_summary_rows(conn, state=state, period=periods[0]))
        if region is None and county is None and len(periods) == 1
        else (),
    )


def month_by_month_analysis(
    conn: DbConnection,
    *,
    region: str | None = None,
    county: str | None = None,
    state: str = DEFAULT_STATE,
) -> StormGroupAnalysis:
    return storm_group_analysis(
        conn,
        tuple(month_period(month) for month in range(1, 13)),
        title="Month-by-month storm analysis",
        group_by="month",
        region=region,
        county=county,
        state=state,
    )


def season_by_season_analysis(
    conn: DbConnection,
    *,
    region: str | None = None,
    county: str | None = None,
    state: str = DEFAULT_STATE,
) -> StormGroupAnalysis:
    return storm_group_analysis(
        conn,
        tuple(season_period(season) for season in ("winter", "spring", "summer", "fall")),
        title="Season-by-season storm analysis",
        group_by="season",
        region=region,
        county=county,
        state=state,
    )


def region_summary_rows(
    conn: DbConnection,
    *,
    state: str = DEFAULT_STATE,
    period: PeriodSpec | None = None,
) -> list[dict[str, Any]]:
    """Summaries for each default/custom region."""
    period = period or all_period()
    rows: list[dict[str, Any]] = []
    for region in _region_mapping(conn, state=state):
        analysis = storm_group_analysis(
            conn,
            (period,),
            title=f"{period.label} regional storm analysis",
            group_by="region",
            region=region,
            state=state,
        )
        if analysis.rows:
            row = dict(analysis.rows[0])
            row["group"] = region
            row["region"] = region
            rows.append(row)
    rows.sort(key=lambda row: (-int(row["total_outage"]), str(row["region"])))
    return rows


def region_group_analysis(
    conn: DbConnection,
    *,
    period: PeriodSpec | None = None,
    state: str = DEFAULT_STATE,
) -> StormGroupAnalysis:
    period = period or all_period()
    rows = tuple(region_summary_rows(conn, state=state, period=period))
    return StormGroupAnalysis(
        title=f"{period.label} regional storm analysis",
        scope_label=f"statewide ({state}) by WISER default regions",
        group_by="region",
        summaries=(),
        rows=rows,
        distribution_rows=(),
    )


def threshold_analysis(
    conn: DbConnection,
    threshold: ThresholdSpec,
    *,
    period: PeriodSpec | None = None,
    region: str | None = None,
    county: str | None = None,
    state: str = DEFAULT_STATE,
    limit: int = 10,
) -> ThresholdAnalysis:
    """Find storm-county rows exceeding a weather or outage threshold."""
    if threshold.metric_key == "humidity":
        raise StormAnalysisError(
            "humidity is registered for the upcoming mock dataset, but no "
            "humidity observations are present in the current WISER tables"
        )

    period = period or all_period()
    observations = [
        row for row in _group_observations(
            conn, state=state, region=region, county=county
        )
        if period.includes(row.storm_code)
    ]
    uses_gust_fallback = (
        threshold.metric_key == "peakGUST10"
        and not any(row.peak_gust_ms is not None for row in observations)
        and threshold.comparison_metric is not None
    )
    matches = [
        _threshold_row(row, threshold, use_gust_fallback=uses_gust_fallback)
        for row in observations
        if _exceeds_threshold(row, threshold, use_gust_fallback=uses_gust_fallback)
    ]
    matches.sort(
        key=lambda row: (
            -float(row["metric_value"]),
            -int(row["outage"]),
            int(row["stormCode"]),
            str(row["county"]),
        )
    )
    limited = tuple(matches[:limit])
    return ThresholdAnalysis(
        title=f"Threshold check: {threshold.label} > {threshold.display_threshold}",
        threshold=threshold,
        scope_label=_scope_label(region, state, period=period, county=county),
        rows=limited,
        matched_rows=len(matches),
        matched_storms=len({int(row["stormCode"]) for row in matches}),
        matched_counties=len({str(row["county"]) for row in matches}),
        total_rows=len(observations),
        used_gust_exposure_fallback=uses_gust_fallback,
    )


def current_weather_available(
    conn: DbConnection,
    *,
    year: int,
    month: int,
    state: str = DEFAULT_STATE,
) -> bool:
    """True when staged current-weather rows exist for a calendar month."""
    start, end = _month_bounds(year, month)
    try:
        rows = _fetch(
            conn,
            """
            SELECT COUNT(*)
            FROM CurrentWeatherObservation
            WHERE observedAt >= %s AND observedAt < %s
              AND LOWER(state) = LOWER(%s)
            """,
            (start, end, state),
        )
    except Exception:  # noqa: BLE001 - older DBs simply have no current feed
        return False
    return bool(rows and int(rows[0][0] or 0) > 0)


def current_weather_group_analysis(
    conn: DbConnection,
    periods: Sequence[PeriodSpec],
    *,
    current_year: int,
    current_month: int,
    title: str,
    group_by: str,
    region: str | None = None,
    county: str | None = None,
    state: str = DEFAULT_STATE,
) -> StormGroupAnalysis:
    """Hybrid group analysis that replaces this month with staged current rows."""
    rows: list[dict[str, Any]] = []
    distribution_rows: list[dict[str, Any]] = []
    summaries: list[StormGroupSummary] = []

    for period in periods:
        if _is_current_month_period(period, current_year=current_year, current_month=current_month):
            current_rows = _current_weather_observations(
                conn,
                year=current_year,
                month=current_month,
                state=state,
                region=region,
                county=county,
            )
            summary_row, distribution = _current_weather_summary_row(
                current_rows,
                year=current_year,
                month=current_month,
                region=region or (f"{county} County" if county else None),
            )
            rows.append(summary_row)
            distribution_rows.extend(distribution)
            continue

        historical = storm_group_analysis(
            conn,
            (period,),
            title=title,
            group_by=period.kind,
            region=region,
            county=county,
            state=state,
        )
        summaries.extend(historical.summaries)
        rows.extend(dict(row) for row in historical.rows)
        distribution_rows.extend(dict(row) for row in historical.distribution_rows)

    return StormGroupAnalysis(
        title=title,
        scope_label=f"{_scope_label(region, state, county=county)} · current weather staging",
        group_by=group_by,
        summaries=tuple(summaries),
        rows=tuple(rows),
        distribution_rows=tuple(distribution_rows),
        region_rows=(),
    )


def current_threshold_analysis(
    conn: DbConnection,
    threshold: ThresholdSpec,
    *,
    year: int,
    month: int,
    region: str | None = None,
    county: str | None = None,
    state: str = DEFAULT_STATE,
    limit: int = 10,
) -> ThresholdAnalysis:
    """Threshold check over staged current-weather rows for this calendar month."""
    observations = _current_weather_observations(
        conn,
        year=year,
        month=month,
        state=state,
        region=region,
        county=county,
    )
    if threshold.metric_key == "peakGUST10" and not any(
        row.get("peakGUST10") is not None for row in observations
    ):
        raise StormAnalysisError(
            "current weather observations do not include peakGUST10, so peak "
            "gust magnitude cannot be checked for this month"
        )
    if threshold.metric_key == "humidity" and not any(
        row.get("humidity") is not None for row in observations
    ):
        raise StormAnalysisError(
            "current weather observations do not include humidity values for "
            "this month"
        )

    matches = [
        _current_threshold_row(row, threshold)
        for row in observations
        if _current_exceeds_threshold(row, threshold)
    ]
    matches.sort(
        key=lambda row: (
            -float(row["metric_value"]),
            -int(row["outage"] or 0),
            str(row["stormCode"]),
            str(row["county"]),
        )
    )
    limited = tuple(matches[:limit])
    month_label = f"{MONTH_NAMES[month - 1]} {year}"
    return ThresholdAnalysis(
        title=(
            f"Current threshold check: {threshold.label} > "
            f"{threshold.display_threshold}"
        ),
        threshold=threshold,
        scope_label=f"{_scope_label(region, state, county=county)} · {month_label}",
        rows=limited,
        matched_rows=len(matches),
        matched_storms=len({str(row["stormCode"]) for row in matches}),
        matched_counties=len({str(row["county"]) for row in matches}),
        total_rows=len(observations),
        source="current_weather",
    )


def _group_observations(
    conn: DbConnection,
    *,
    state: str,
    region: str | None,
    county: str | None = None,
) -> tuple[GroupObservation, ...]:
    rows = _fetch(
        conn,
        """
        SELECT
            w.stormCode,
            s.eventDateTime,
            w.county,
            w.outage,
            w.peakWSPD10,
            w.peakGUST10,
            w.sumAFWA_TOTPRECIP,
            w.wgt5,
            w.wgt9,
            w.wgt13,
            w.wgt18,
            c.ohLine
        FROM WeatherData w
        JOIN Storm s ON s.stormCode = w.stormCode
        JOIN County c ON LOWER(c.county) = LOWER(w.county)
        WHERE LOWER(c.state) = LOWER(%s)
        ORDER BY w.stormCode ASC, w.county ASC
        """,
        (state,),
    )
    allowed = None
    if region is not None:
        allowed = {
            county.lower()
            for county in _region_mapping(conn, state=state).get(region, ())
        }
    observations = []
    for row in rows:
        row_county = str(row[2])
        if county is not None and row_county.lower() != county.lower():
            continue
        if allowed is not None and row_county.lower() not in allowed:
            continue
        storm_code = int(row[0])
        observations.append(
            GroupObservation(
                storm_code=storm_code,
                date=storm_code_to_date(storm_code),
                county=row_county,
                outage=int(row[3]),
                peak_wind_ms=float(row[4]),
                peak_gust_ms=_as_optional_float(row[5]),
                precip_mm=float(row[6]),
                humidity=None,
                wgt5=int(row[7]),
                wgt9=int(row[8]),
                wgt13=int(row[9]),
                wgt18=int(row[10]),
                oh_line=int(row[11]),
            )
        )
    return tuple(observations)


def _current_weather_observations(
    conn: DbConnection,
    *,
    year: int,
    month: int,
    state: str,
    region: str | None,
    county: str | None,
) -> tuple[dict[str, Any], ...]:
    start, end = _month_bounds(year, month)
    rows = _fetch(
        conn,
        """
        SELECT
            sourceRecordId,
            observedAt,
            county,
            outage,
            peakWSPD10,
            peakGUST10,
            sumAFWA_TOTPRECIP,
            humidity
        FROM CurrentWeatherObservation
        WHERE observedAt >= %s AND observedAt < %s
          AND LOWER(state) = LOWER(%s)
        ORDER BY observedAt ASC, county ASC, sourceRecordId ASC
        """,
        (start, end, state),
    )
    allowed = None
    if region is not None:
        allowed = {
            county_name.lower()
            for county_name in _region_mapping(conn, state=state).get(region, ())
        }

    observations: list[dict[str, Any]] = []
    for row in rows:
        row_county = str(row[2]) if row[2] is not None else None
        if county is not None and (row_county or "").lower() != county.lower():
            continue
        if allowed is not None and (row_county or "").lower() not in allowed:
            continue
        observations.append(
            {
                "sourceRecordId": str(row[0]),
                "observedAt": str(row[1]),
                "date": str(row[1])[:10],
                "county": row_county or "Unknown",
                "outage": _as_optional_int(row[3]),
                "peakWSPD10": _as_optional_float(row[4]),
                "peakGUST10": _as_optional_float(row[5]),
                "sumAFWA_TOTPRECIP": _as_optional_float(row[6]),
                "humidity": _as_optional_float(row[7]),
            }
        )
    return tuple(observations)


def _current_weather_summary_row(
    observations: Sequence[dict[str, Any]],
    *,
    year: int,
    month: int,
    region: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    label = f"Current {MONTH_NAMES[month - 1]} {year}"
    event_totals: dict[str, int] = defaultdict(int)
    event_dates: dict[str, str] = {}
    counties = set()
    for row in observations:
        source_id = str(row["sourceRecordId"])
        event_totals[source_id] += int(row.get("outage") or 0)
        event_dates[source_id] = str(row.get("date") or "")
        if row.get("county") != "Unknown":
            counties.add(str(row["county"]))

    outage_values = sorted(event_totals.values())
    max_source_id = None
    max_outage = None
    if event_totals:
        max_source_id, max_outage = sorted(
            event_totals.items(), key=lambda item: (-item[1], item[0])
        )[0]

    total_outage = sum(int(row.get("outage") or 0) for row in observations)
    event_count = len(event_totals)
    distribution_rows = [
        {
            "group": label,
            "stormCode": source_id,
            "date": event_dates[source_id],
            "outage": outage,
            "source": "current_weather",
        }
        for source_id, outage in sorted(
            event_totals.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    return (
        {
            "group": label,
            "region": region,
            "storm_count": event_count,
            "county_count": len(counties),
            "total_outage": total_outage,
            "avg_outage_per_storm": (
                total_outage / event_count if event_count else None
            ),
            "median_outage_per_storm": _median(outage_values),
            "max_storm_outage": max_outage,
            "max_storm_code": max_source_id,
            "peak_wind_ms": _max_or_none(
                row["peakWSPD10"]
                for row in observations
                if row.get("peakWSPD10") is not None
            ),
            "peak_gust_ms": _max_or_none(
                row["peakGUST10"]
                for row in observations
                if row.get("peakGUST10") is not None
            ),
            "max_precip_mm": _max_or_none(
                row["sumAFWA_TOTPRECIP"]
                for row in observations
                if row.get("sumAFWA_TOTPRECIP") is not None
            ),
            "avg_precip_mm": _average_or_none(
                row["sumAFWA_TOTPRECIP"]
                for row in observations
                if row.get("sumAFWA_TOTPRECIP") is not None
            ),
            "max_humidity": _max_or_none(
                row["humidity"]
                for row in observations
                if row.get("humidity") is not None
            ),
            "avg_humidity": _average_or_none(
                row["humidity"]
                for row in observations
                if row.get("humidity") is not None
            ),
            "gust_hours_9": 0,
            "gust_hours_13": 0,
            "outage_per_million_ohline": None,
            "source": "current_weather",
        },
        distribution_rows,
    )


def _current_threshold_row(
    observation: dict[str, Any],
    threshold: ThresholdSpec,
) -> dict[str, Any]:
    return {
        "stormCode": observation["sourceRecordId"],
        "date": observation["date"],
        "county": observation["county"],
        "metric": threshold.label,
        "metric_value": _current_metric_value(observation, threshold.metric_key),
        "threshold": threshold.display_threshold,
        "outage": observation.get("outage"),
        "peakWSPD10": observation.get("peakWSPD10"),
        "peakGUST10": observation.get("peakGUST10"),
        "sumAFWA_TOTPRECIP": observation.get("sumAFWA_TOTPRECIP"),
        "humidity": observation.get("humidity"),
        "wgt9": None,
        "wgt13": None,
        "wgt18": None,
        "source": "current_weather",
    }


def _current_exceeds_threshold(
    observation: dict[str, Any],
    threshold: ThresholdSpec,
) -> bool:
    value = _current_metric_value(observation, threshold.metric_key)
    return value is not None and value > threshold.stored_threshold


def _current_metric_value(
    observation: dict[str, Any],
    metric_key: str,
) -> float | None:
    if metric_key == "outage":
        outage = observation.get("outage")
        return float(outage) if outage is not None else None
    value = observation.get(metric_key)
    return float(value) if value is not None else None


def _is_current_month_period(
    period: PeriodSpec,
    *,
    current_year: int,
    current_month: int,
) -> bool:
    return (
        period.kind == "month"
        and period.month == current_month
        and period.year in (None, current_year)
    )


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    start = f"{int(year):04d}-{int(month):02d}-01 00:00:00"
    next_year, next_month = (int(year) + 1, 1) if int(month) == 12 else (int(year), int(month) + 1)
    end = f"{next_year:04d}-{next_month:02d}-01 00:00:00"
    return start, end


def _region_mapping(
    conn: DbConnection,
    *,
    state: str,
) -> dict[str, tuple[str, ...]]:
    try:
        rows = _fetch(
            conn,
            """
            SELECT region, county
            FROM RegionCountyMapping
            WHERE LOWER(state) = LOWER(%s)
            ORDER BY region ASC, county ASC
            """,
            (state,),
        )
    except Exception:  # noqa: BLE001 - older DBs still get default regions
        rows = []
    mapping: dict[str, list[str]] = defaultdict(list)
    for region, county in rows:
        mapping[str(region)].append(str(county))
    if mapping:
        return {region: tuple(counties) for region, counties in mapping.items()}
    return DEFAULT_REGION_COUNTIES


def _summarize_group(
    period: PeriodSpec,
    observations: Sequence[GroupObservation],
    *,
    region: str | None,
) -> StormGroupSummary:
    storm_totals: dict[int, int] = defaultdict(int)
    storm_dates: dict[int, str] = {}
    county_lines: dict[str, int] = {}
    for row in observations:
        storm_totals[row.storm_code] += row.outage
        storm_dates[row.storm_code] = row.date
        county_lines[row.county] = row.oh_line

    outage_values = sorted(storm_totals.values())
    max_code = None
    max_outage = None
    if storm_totals:
        max_code, max_outage = sorted(
            storm_totals.items(), key=lambda item: (-item[1], item[0])
        )[0]
    total_outage = sum(row.outage for row in observations)
    storm_count_value = len(storm_totals)
    line_exposure = sum(county_lines.values())

    return StormGroupSummary(
        key=period.key,
        label=period.label,
        group_kind=period.kind,
        region=region,
        storm_count=storm_count_value,
        county_count=len(county_lines),
        total_outage=total_outage,
        avg_outage_per_storm=(
            total_outage / storm_count_value if storm_count_value else None
        ),
        median_outage_per_storm=_median(outage_values),
        max_storm_outage=max_outage,
        max_storm_code=max_code,
        peak_wind_ms=_max_or_none(row.peak_wind_ms for row in observations),
        peak_gust_ms=_max_or_none(
            row.peak_gust_ms for row in observations if row.peak_gust_ms is not None
        ),
        max_precip_mm=_max_or_none(row.precip_mm for row in observations),
        avg_precip_mm=_average_or_none(row.precip_mm for row in observations),
        max_humidity=_max_or_none(
            row.humidity for row in observations if row.humidity is not None
        ),
        avg_humidity=_average_or_none(
            row.humidity for row in observations if row.humidity is not None
        ),
        gust_hours_9=sum(row.wgt9 for row in observations),
        gust_hours_13=sum(row.wgt13 for row in observations),
        outage_per_million_ohline=(
            total_outage * 1_000_000.0 / line_exposure
            if line_exposure
            else None
        ),
        storm_outages=tuple(
            StormOutagePoint(
                group_label=period.label,
                storm_code=storm_code,
                date=storm_dates[storm_code],
                outage=outage,
            )
            for storm_code, outage in sorted(
                storm_totals.items(), key=lambda item: (-item[1], item[0])
            )
        ),
    )


def _group_summary_row(summary: StormGroupSummary) -> dict[str, Any]:
    return {
        "group": summary.label,
        "region": summary.region,
        "storm_count": summary.storm_count,
        "county_count": summary.county_count,
        "total_outage": summary.total_outage,
        "avg_outage_per_storm": summary.avg_outage_per_storm,
        "median_outage_per_storm": summary.median_outage_per_storm,
        "max_storm_outage": summary.max_storm_outage,
        "max_storm_code": summary.max_storm_code,
        "peak_wind_ms": summary.peak_wind_ms,
        "peak_gust_ms": summary.peak_gust_ms,
        "max_precip_mm": summary.max_precip_mm,
        "avg_precip_mm": summary.avg_precip_mm,
        "max_humidity": summary.max_humidity,
        "avg_humidity": summary.avg_humidity,
        "gust_hours_9": summary.gust_hours_9,
        "gust_hours_13": summary.gust_hours_13,
        "outage_per_million_ohline": summary.outage_per_million_ohline,
        "source": summary.data_source,
    }


def _distribution_row(point: StormOutagePoint) -> dict[str, Any]:
    return {
        "group": point.group_label,
        "stormCode": point.storm_code,
        "date": point.date,
        "outage": point.outage,
    }


def _threshold_row(
    observation: GroupObservation,
    threshold: ThresholdSpec,
    *,
    use_gust_fallback: bool = False,
) -> dict[str, Any]:
    metric_key = (
        threshold.comparison_metric
        if use_gust_fallback and threshold.comparison_metric
        else threshold.metric_key
    )
    return {
        "stormCode": observation.storm_code,
        "date": observation.date,
        "county": observation.county,
        "metric": threshold.label,
        "metric_value": observation.value(metric_key),
        "threshold": threshold.display_threshold,
        "outage": observation.outage,
        "peakWSPD10": observation.peak_wind_ms,
        "peakGUST10": observation.peak_gust_ms,
        "sumAFWA_TOTPRECIP": observation.precip_mm,
        "humidity": observation.humidity,
        "wgt9": observation.wgt9,
        "wgt13": observation.wgt13,
        "wgt18": observation.wgt18,
        "source": "historical",
    }


def _exceeds_threshold(
    observation: GroupObservation,
    threshold: ThresholdSpec,
    *,
    use_gust_fallback: bool = False,
) -> bool:
    if use_gust_fallback:
        metric_key = threshold.comparison_metric
        return bool(metric_key) and observation.value(metric_key) > 0
    if threshold.metric_key == "peakGUST10" and observation.peak_gust_ms is None:
        return False
    return observation.value(threshold.metric_key) > threshold.stored_threshold


def _scope_label(
    region: str | None,
    state: str,
    *,
    period: PeriodSpec | None = None,
    county: str | None = None,
) -> str:
    pieces = [region or (f"{county} County" if county else f"statewide ({state})")]
    if period is not None and period.kind != "all":
        pieces.append(period.label)
    return " · ".join(pieces)


def _median(values: Sequence[int]) -> float | None:
    if not values:
        return None
    midpoint = len(values) // 2
    if len(values) % 2:
        return float(values[midpoint])
    return (values[midpoint - 1] + values[midpoint]) / 2.0


def _max_or_none(values: Iterable[float]) -> float | None:
    items = list(values)
    return max(items) if items else None


def _average_or_none(values: Iterable[float]) -> float | None:
    items = list(values)
    return sum(items) / len(items) if items else None


# ---------------------------------------------------------------------------
# Row mapping — positional, never by key (see the module docstring)
# ---------------------------------------------------------------------------


def _storm_ref(row: Sequence[Any]) -> StormRef:
    return StormRef(
        storm_code=int(row[0]),
        event_datetime=str(row[1]),
        event_length=int(row[2]),
    )


def _county_storm_row(row: Sequence[Any]) -> CountyStormRow:
    return CountyStormRow(
        county=str(row[0]),
        state=str(row[1]),
        outage=int(row[2]),
        peak_wind_ms=float(row[3]),
        peak_gust_ms=_as_optional_float(row[4]),
        precip_mm=float(row[5]),
        wgt5=int(row[6]),
        wgt9=int(row[7]),
        wgt13=int(row[8]),
        wgt18=int(row[9]),
        oh_line=int(row[10]),
    )


def _argmax(rows: Iterable[CountyStormRow], key) -> CountyValue | None:
    """Highest value with a deterministic county-name tiebreak."""
    ordered = sorted(rows, key=lambda row: (-key(row), row.county))
    if not ordered:
        return None
    return CountyValue(ordered[0].county, float(key(ordered[0])))


def _argmax_optional(rows: Iterable[CountyStormRow], key) -> CountyValue | None:
    """Highest non-null value with a deterministic county-name tiebreak."""
    available = [(row, key(row)) for row in rows]
    ordered = sorted(
        ((row, value) for row, value in available if value is not None),
        key=lambda item: (-float(item[1]), item[0].county),
    )
    if not ordered:
        return None
    row, value = ordered[0]
    return CountyValue(row.county, float(value))


def _as_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _as_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
