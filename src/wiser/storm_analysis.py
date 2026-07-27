"""Storm-scoped analysis: summary, ranking, and two-storm comparison.

This module owns every storm-scoped SQL query in WISER. Handlers and the
report layer call these functions and never write SQL themselves, so a move
to PostgreSQL has exactly one file to port — and inside that file, exactly one
function (``_fetch``) that binds parameters.

Portability rules this module holds to (see the plan, section 2):

* No SQLite-only functions. No ``strftime``: dates derive from ``stormCode``,
  which is an integer ``YYYYMMDDHH``, using integer arithmetic only.
* No ``ROUND(<float>, n)`` — PostgreSQL has no such overload for double
  precision. Rounding is the presentation layer's job.
* Ranking uses ``RANK() OVER`` / ``COUNT(*) OVER``, supported by SQLite 3.25+
  and PostgreSQL 8.4+.
* Rows are mapped to dataclasses **positionally**. Unquoted ``stormCode``
  folds to ``stormcode`` in PostgreSQL, so key-based access would break
  silently on migration.
* ``state`` is always a bound parameter, never a literal.
* Connections are typed against a ``DbConnection`` Protocol rather than
  ``sqlite3.Connection``.

``tests/test_storm_analysis.py`` enforces the first two mechanically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Protocol, Sequence

DEFAULT_STATE = "CT"

# Ranking metric keys. Duration is deliberately absent: Storm.eventLength is a
# fixed analysis window (identical for every storm), so a duration rank would
# be a tie across the whole record and carry no information.
METRIC_OUTAGE = "outage"
METRIC_MAX_WIND = "max_wind"
METRIC_MAX_PRECIP = "max_precip"

RANKABLE_METRICS: tuple[str, ...] = (METRIC_OUTAGE, METRIC_MAX_WIND, METRIC_MAX_PRECIP)

METRIC_LABELS: dict[str, str] = {
    METRIC_OUTAGE: "statewide outages",
    METRIC_MAX_WIND: "maximum wind speed",
    METRIC_MAX_PRECIP: "maximum precipitation",
}

COUNTY_METRIC_LABELS: dict[str, str] = {
    METRIC_OUTAGE: "outages",
    METRIC_MAX_WIND: "maximum wind speed",
    METRIC_MAX_PRECIP: "maximum precipitation",
}

# The wind-threshold column used for the comparison table's "counties above
# threshold" row. wgt13 is near-zero for most storms in this dataset, so wgt9
# is the threshold with usable spread.
COMPARISON_THRESHOLD_COLUMN = "wgt9"
COMPARISON_THRESHOLD_LABEL = "Counties with 9+ m/s wind hours"


class DbConnection(Protocol):
    """Minimal read surface. Deliberately not ``sqlite3.Connection``."""

    def execute(self, sql: str, parameters: Sequence[Any] = ..., /) -> Any: ...


# ---------------------------------------------------------------------------
# The single parameter-binding funnel
# ---------------------------------------------------------------------------


def _fetch(
    conn: DbConnection,
    sql: str,
    params: Sequence[Any] = (),
) -> list[tuple]:
    """Execute a read query and return raw positional rows.

    Every query in this module goes through here. The ``?`` placeholder style
    is SQLite's; a psycopg port rewrites markers in this one function rather
    than at every call site.
    """
    cursor = conn.execute(sql, tuple(params))
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
    # Phase 2 fields. Always None until the external metadata table lands;
    # never inferred, never defaulted to a number.
    storm_type: None = None
    peak_snowfall_mm: None = None
    wind_gust_ms: None = None

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


class StormAnalysisError(ValueError):
    """Raised when a requested storm or metric cannot be analysed."""


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
            WHERE stormCode = ?
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
        WHERE stormCode >= ? AND stormCode < ?
        ORDER BY stormCode ASC
        """,
        (lower, upper),
    )
    return StormResolution(token=text, matches=tuple(_storm_ref(row) for row in rows))


def storm_count(conn: DbConnection, *, state: str = DEFAULT_STATE) -> int:
    """Number of storms with observations in a state."""
    rows = _fetch(
        conn,
        """
        SELECT COUNT(DISTINCT w.stormCode)
        FROM WeatherData w
        JOIN County c ON LOWER(c.county) = LOWER(w.county)
        WHERE LOWER(c.state) = LOWER(?)
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
        WHERE stormCode = ?
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
            w.sumAFWA_TOTPRECIP,
            w.wgt5,
            w.wgt9,
            w.wgt13,
            w.wgt18,
            c.ohLine
        FROM WeatherData w
        JOIN County c ON LOWER(c.county) = LOWER(w.county)
        WHERE w.stormCode = ?
          AND LOWER(c.state) = LOWER(?)
    """
    params: list[Any] = [int(storm_code), state]
    if county:
        sql += " AND LOWER(w.county) = LOWER(?)"
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
    )


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
        WHERE LOWER(c.state) = LOWER(?)
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
        WHERE LOWER(c.state) = LOWER(?)
          AND LOWER(w.county) = LOWER(?)
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
    WHERE storm_code = ?
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
        "SELECT stormCode, eventDateTime, eventLength FROM Storm WHERE stormCode = ?",
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
        precip_mm=float(row[4]),
        wgt5=int(row[5]),
        wgt9=int(row[6]),
        wgt13=int(row[7]),
        wgt18=int(row[8]),
        oh_line=int(row[9]),
    )


def _argmax(rows: Iterable[CountyStormRow], key) -> CountyValue | None:
    """Highest value with a deterministic county-name tiebreak."""
    ordered = sorted(rows, key=lambda row: (-key(row), row.county))
    if not ordered:
        return None
    return CountyValue(ordered[0].county, float(key(ordered[0])))


def _as_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)
