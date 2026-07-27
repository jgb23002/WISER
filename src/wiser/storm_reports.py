"""Rendering for storm-scoped analysis results.

Each renderer turns a ``storm_analysis`` dataclass into a readable report block
plus structured rows: the block is what a stakeholder reads, the rows are what
tables and heat maps consume. No SQL and no database access live here.

Two disciplines this layer is responsible for:

* **Units.** The database is metric; reports are imperial. Conversion happens
  here via ``wiser.units`` and nowhere else. Structured rows carry both, so
  the existing metric heat maps keep working unchanged.
* **Never inventing a value.** Fields the schema does not have — storm type,
  peak snowfall, wind gust — render as an explicit unavailability string. And
  modeled values are labelled as modeled from the start, so when observed gust
  data arrives it sits next to a label rather than needing one retrofitted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from wiser.agents.storm_reasoning_agent import normalized_ranking_divergence
from wiser.storm_analysis import (
    METRIC_MAX_PRECIP,
    METRIC_MAX_WIND,
    METRIC_OUTAGE,
    ForecastComparison,
    StormComparison,
    StormRanking,
    StormSummary,
)
from wiser.units import (
    format_count,
    format_inches,
    format_mph,
    format_signed,
    mm_to_inches,
    ms_to_mph,
)

# Phase 2 fields: absent from the schema, never inferred.
PHASE_2_UNAVAILABLE = "not available — requires external storm metadata (Phase 2)"

# peakWSPD10 and sumAFWA_TOTPRECIP are AFWA model output, not station
# observations. Labelling them from day one means a future NCEI gust value
# lands beside an existing label instead of needing one retrofitted onto
# numbers people have already been reading unlabelled.
MODELED_WIND_LABEL = "[modeled, AFWA peakWSPD10]"
MODELED_PRECIP_LABEL = "[modeled, AFWA]"

OUTAGE_UNIT_NOTE = "trouble spots dispatched"

REPORT_STORM_SUMMARY = "storm_summary"
REPORT_STORM_RANKING = "storm_ranking"
REPORT_STORM_COMPARISON = "storm_comparison"
REPORT_FORECAST_COMPARISON = "forecast_comparison"


@dataclass(frozen=True)
class StormReport:
    """A rendered report block plus the rows behind it."""

    kind: str
    text: str
    rows: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Capability 1 — storm summary
# ---------------------------------------------------------------------------


def render_storm_summary(
    summary: StormSummary,
    *,
    total_storms: int | None = None,
) -> StormReport:
    """Render the storm summary report."""
    lines: list[str] = [
        f"Storm date:        {summary.storm.date}",
        f"Storm type:        {PHASE_2_UNAVAILABLE}",
        f"Analysis window:   {_analysis_window(summary, total_storms)}",
        "",
        "Weather Conditions",
        f"  Maximum wind speed:  {_county_measure(summary.max_wind, format_mph)}"
        f"  {MODELED_WIND_LABEL}",
        f"  Maximum rainfall:    {_county_measure(summary.max_precip, format_inches)}"
        f"  {MODELED_PRECIP_LABEL}",
        f"  Peak snowfall:       {PHASE_2_UNAVAILABLE}",
        f"  Peak wind gust:      {PHASE_2_UNAVAILABLE}",
        "",
        "Storm Impact",
        f"  Statewide outages ({OUTAGE_UNIT_NOTE}):  "
        f"{format_count(summary.statewide_outage)}",
    ]

    if summary.top_outage_counties:
        lines.append("  Counties with the highest outages:")
        lines.extend(
            f"    {entry.county} County — {format_count(entry.value)}"
            for entry in summary.top_outage_counties
        )

    lines.extend(["", "Relative Impact", _relative_impact_sentence(summary)])
    lines.extend(["", _summary_caveat(summary)])

    return StormReport(
        kind=REPORT_STORM_SUMMARY,
        text="\n".join(lines),
        rows=county_rows(summary),
    )


def _analysis_window(summary: StormSummary, total_storms: int | None) -> str:
    hours = summary.storm.event_length
    if total_storms:
        return (
            f"{hours} hours  (fixed model window, identical for all "
            f"{format_count(total_storms)} storms)"
        )
    return f"{hours} hours  (fixed model window)"


def _county_measure(value, formatter) -> str:
    """'22.3 mph in New Haven County', or the unavailable string."""
    if value is None:
        return "not available"
    return f"{formatter(value.value)} in {value.county} County"


def _relative_impact_sentence(summary: StormSummary) -> str:
    entries = summary.top_normalized_counties
    if not entries:
        return (
            "  Overhead-line exposure is unavailable for these counties, so no "
            "normalized comparison can be made."
        )
    leader = entries[0]
    sentence = (
        f"  After normalizing by overhead line length, {leader.county} County "
        f"experienced the greatest relative impact "
        f"({leader.value:,.2f} per million ohLine)"
    )
    followers = [entry.county for entry in entries[1:]]
    if len(followers) == 1:
        sentence += f", followed by {followers[0]}"
    elif len(followers) > 1:
        sentence += f", followed by {followers[0]} and {followers[1]}"
    return sentence + "."


def _summary_caveat(summary: StormSummary) -> str:
    return (
        f"Caveat: values are county-level ({len(summary.counties)} "
        f"{'county' if len(summary.counties) == 1 else 'counties'} in scope) and cover "
        "only fields present in the WISER database. Wind and precipitation are AFWA "
        f"model output, not station observations. \"Outages\" counts "
        f"{OUTAGE_UNIT_NOTE}, not customer accounts. Storm type, snowfall, and wind "
        "gust are not in the schema and are reported as unavailable rather than "
        "estimated."
    )


# ---------------------------------------------------------------------------
# Capability 2 — storm ranking
# ---------------------------------------------------------------------------


def render_storm_ranking(ranking: StormRanking) -> StormReport:
    """Render the storm ranking report."""
    lines = [_ranking_sentence(ranking), ""]

    lines.append(
        f"Scope: {ranking.scope_label}. Ranked against all "
        f"{format_count(ranking.total_storms)} storms in the WISER database."
    )

    tied = [metric for metric in ranking.metrics if metric.tied]
    for metric in tied:
        lines.append(
            f"  Note: {format_count(metric.tie_count)} storms share this "
            f"{metric.label} value, so they share a rank."
        )

    lines.extend(["", _ranking_caveat()])
    return StormReport(
        kind=REPORT_STORM_RANKING,
        text="\n".join(lines),
        rows=ranking_rows(ranking),
    )


def _ranking_sentence(ranking: StormRanking) -> str:
    metrics = [metric for metric in ranking.metrics if metric.value is not None]
    if not metrics:
        return (
            f"The storm on {ranking.storm.date} has no rankable measurements in "
            f"{ranking.scope_label}."
        )

    clauses = [
        f"ranks {_ordinal(metrics[0].rank)} out of "
        f"{format_count(metrics[0].total)} storms in {metrics[0].label}"
    ]
    clauses.extend(
        f"{_ordinal(metric.rank)} in {metric.label}" for metric in metrics[1:]
    )
    joined = clauses[0] if len(clauses) == 1 else (
        ", ".join(clauses[:-1]) + f" and {clauses[-1]}"
    )

    best = min(metric.percentile for metric in metrics)
    return (
        f"The storm on {ranking.storm.date} {joined}, placing it in the top "
        f"{_percentile_label(best)} of the historical record."
    )


def _percentile_label(percentile: float) -> str:
    """Round up, and never claim better than 1% — 'top 0%' is not a thing."""
    return f"{max(1, math.ceil(percentile))}%"


def _ranking_caveat() -> str:
    return (
        f"Caveat: \"outages\" counts {OUTAGE_UNIT_NOTE}, not customer accounts. Wind "
        "and precipitation are AFWA model output. Storm counts and outage magnitudes "
        "reflect the current development dataset and will change when the production "
        "dataset lands. Duration is not rankable: Storm.eventLength is a fixed "
        "analysis window, identical for every storm."
    )


# ---------------------------------------------------------------------------
# Capability 3, Mode A — two database storms
# ---------------------------------------------------------------------------


def render_storm_comparison(comparison: StormComparison) -> StormReport:
    """Render the Mode A comparison: two storms already in the database."""
    date_a = comparison.summary_a.storm.date
    date_b = comparison.summary_b.storm.date

    lines = [f"Storm Comparison: {date_a} vs {date_b}", "", _scale_sentence(comparison), ""]
    lines.extend(_comparison_table(comparison))
    lines.extend(["", "Where they differ most:"])
    lines.extend(_divergence_bullets(comparison))
    lines.extend(["", _comparison_caveat()])

    return StormReport(
        kind=REPORT_STORM_COMPARISON,
        text="\n".join(lines),
        rows=comparison_rows(comparison),
    )


def _scale_sentence(comparison: StormComparison) -> str:
    rank_a = comparison.ranking_a.metric(METRIC_OUTAGE)
    rank_b = comparison.ranking_b.metric(METRIC_OUTAGE)
    if rank_a is None or rank_b is None:
        return "Scale: outage rankings are unavailable for these storms."
    return (
        f"Scale: Storm A ranks {_ordinal(rank_a.rank)} of "
        f"{format_count(rank_a.total)} by statewide outages; "
        f"Storm B ranks {_ordinal(rank_b.rank)}."
    )


_COMPARISON_FORMATTERS = {
    METRIC_OUTAGE: (format_count, format_signed),
    METRIC_MAX_WIND: (format_mph, lambda value: _signed_unit(value, format_mph)),
    METRIC_MAX_PRECIP: (format_inches, lambda value: _signed_unit(value, format_inches)),
}


def _comparison_table(comparison: StormComparison) -> list[str]:
    header = f"  {'':<32}{'Storm A':>14}{'Storm B':>14}{'Difference':>18}"
    rows = [header]
    for delta in comparison.deltas:
        value_fmt, diff_fmt = _COMPARISON_FORMATTERS.get(
            delta.metric, (format_count, format_signed)
        )
        difference = diff_fmt(delta.difference)
        percent = delta.percent_change
        if percent is not None and delta.metric == METRIC_OUTAGE:
            difference = f"{difference} ({percent:+,.0f}%)"
        rows.append(
            f"  {delta.label:<32}"
            f"{value_fmt(delta.value_a):>14}"
            f"{value_fmt(delta.value_b):>14}"
            f"{difference:>18}"
        )
    return rows


def _signed_unit(value: float | None, formatter) -> str:
    """'+1.4 mph' — sign from the raw value, unit from the formatter."""
    if value is None:
        return "not available"
    rendered = formatter(abs(value))
    return f"{'+' if value >= 0 else '-'}{rendered}"


def _divergence_bullets(comparison: StormComparison) -> list[str]:
    bullets: list[str] = []

    share_a = comparison.summary_a.top_outage_share
    share_b = comparison.summary_b.top_outage_share
    if share_a is not None and share_b is not None:
        leader_a = comparison.summary_a.top_outage_counties[0].county
        leader_b = comparison.summary_b.top_outage_counties[0].county
        sentence = (
            f"  - Geographic concentration: Storm A put {share_a:.0%} of statewide "
            f"outages in {leader_a}; Storm B put {share_b:.0%} in {leader_b}."
        )
        # Only claim a difference the reader can see. Declaring one storm "more
        # concentrated" when both render as the same percentage reads as a bug,
        # even when the underlying floats differ in the third decimal.
        if round(share_a, 2) == round(share_b, 2):
            sentence += " The two are comparably concentrated."
        else:
            more, less = ("A", "B") if share_a > share_b else ("B", "A")
            sentence += (
                f" Storm {more} was the more concentrated of the two, Storm {less}"
                " the more evenly distributed."
            )
        bullets.append(sentence)

    for label, summary in (("A", comparison.summary_a), ("B", comparison.summary_b)):
        divergence = normalized_ranking_divergence(county_rows(summary))
        if divergence is None:
            continue
        if divergence.flipped:
            bullets.append(
                f"  - Normalized divergence: for Storm {label}, ohLine normalization "
                f"flips the ranking — {divergence.raw_leader['county']} leads on raw "
                f"outages but {divergence.normalized_leader['county']} leads per "
                "million ohLine."
            )
        else:
            bullets.append(
                f"  - Normalized divergence: for Storm {label}, ohLine normalization "
                f"preserves the raw ranking — {divergence.normalized_leader['county']} "
                "leads both."
            )

    if not bullets:
        bullets.append(
            "  - The rows do not contain enough measured values to identify where "
            "these storms diverge."
        )
    return bullets


def _comparison_caveat() -> str:
    return (
        "Caveat: comparison uses only fields present in the WISER database. Wind and "
        "precipitation are AFWA model output, not station observations. \"Outages\" "
        f"counts {OUTAGE_UNIT_NOTE}, not customer accounts. Storm type, snowfall, and "
        "wind gust are unavailable and are not estimated."
    )


# ---------------------------------------------------------------------------
# Capability 3, Mode B — a forecast storm against the historical record
# ---------------------------------------------------------------------------

PROJECTION_LABEL = "(projection)"
ACTUAL_LABEL = "(actual)"
NOT_A_PREDICTION = (
    "These are outages recorded for past storms with similar modelled conditions. "
    "They are NOT a prediction for the forecast storm."
)

_FORECAST_INPUT_LABELS = {
    "peakWSPD10": "Wind",
    "sumAFWA_TOTPRECIP": "Precipitation",
    "wgt5": "Hours above 5 m/s",
    "wgt9": "Hours above 9 m/s",
    "wgt13": "Hours above 13 m/s",
    "wgt18": "Hours above 18 m/s",
}


def render_forecast_comparison(comparison: ForecastComparison) -> StormReport:
    """Render the Mode B report: a forecast positioned against the record.

    Two rules this renderer exists to enforce. The forecast column is labelled
    a projection everywhere it appears, and no outage value is ever placed in
    it — the analogue outages are historical outcomes, labelled as such, and
    the report says in as many words that they are not a prediction.
    """
    lines = [
        f"Forecast Comparison: upcoming storm vs historical record"
        f" — {comparison.scope_label}",
        "",
        f"Forecast input {PROJECTION_LABEL}:",
    ]
    lines.extend(_forecast_input_lines(comparison))

    lines.extend(["", "Position in the record"])
    if comparison.positions:
        lines.extend(
            f"  {position.label.capitalize()+':':<16} would rank "
            f"{_ordinal(position.rank)} of {format_count(position.total)} storms"
            f"  (top {_percentile_label(position.percentile)})"
            for position in comparison.positions
        )
    else:
        lines.append(
            "  No positionable values supplied. Only wind and precipitation can be"
            " placed in the historical distribution."
        )

    lines.extend(["", f"Closest historical analogues, with outages {ACTUAL_LABEL}"])
    lines.extend(_analogue_lines(comparison))

    lines.extend(["", NOT_A_PREDICTION, "", _forecast_caveat(comparison)])

    return StormReport(
        kind=REPORT_FORECAST_COMPARISON,
        text="\n".join(lines),
        rows=forecast_rows(comparison),
    )


def _forecast_input_lines(comparison: ForecastComparison) -> list[str]:
    """Show every input in both units, so a mis-entered unit is visible."""
    lines: list[str] = []
    for feature, value in comparison.params.items():
        label = _FORECAST_INPUT_LABELS.get(feature, feature)
        if feature == "peakWSPD10":
            rendered = f"{format_mph(value)}  ({value:,.1f} m/s)"
        elif feature == "sumAFWA_TOTPRECIP":
            rendered = f"{format_inches(value)}  ({value:,.1f} mm)"
        else:
            rendered = f"{format_count(value)} hours"
        lines.append(f"  {label + ':':<16} {rendered}")
    return lines


def _analogue_lines(comparison: ForecastComparison) -> list[str]:
    if not comparison.analogue_groups:
        return ["  No historical storms available to compare against."]

    lines: list[str] = []
    grouped = len(comparison.analogue_groups) > 1
    for group in comparison.analogue_groups:
        if grouped:
            lines.append(f"  {group.county} County")
        header = (
            f"  {'  ' if grouped else ''}{'Storm':<12}{'Date':<14}"
            f"{'Wind':>10}{'Precip':>10}{'Outages ' + ACTUAL_LABEL:>20}"
        )
        lines.append(header)
        for analogue in group.analogues:
            lines.append(
                f"  {'  ' if grouped else ''}{analogue.storm_code:<12}"
                f"{analogue.date:<14}"
                f"{format_mph(analogue.peak_wind_ms):>10}"
                f"{format_inches(analogue.precip_mm):>10}"
                f"{format_count(analogue.outage):>20}"
            )
    if grouped:
        lines.append(
            "  Similarity is scored within each county's own history, so scores are"
            " not comparable between counties."
        )
    return lines


def _forecast_caveat(comparison: ForecastComparison) -> str:
    scope_note = (
        f"Similarity is computed within {comparison.county_scope} County's history only."
        if comparison.county_scope
        else "Similarity is computed within each county's history separately."
    )
    return (
        f"Caveat: the forecast column is a projection supplied by the user, not a "
        f"database record, and no outage figure is attributed to it. {scope_note} "
        f"Historical wind and precipitation are AFWA model output, not station "
        f"observations. \"Outages\" counts {OUTAGE_UNIT_NOTE}, not customer accounts."
    )


# ---------------------------------------------------------------------------
# Structured rows
# ---------------------------------------------------------------------------


def county_rows(summary: StormSummary) -> list[dict[str, Any]]:
    """Per-county rows for tables, heat maps, and the reasoning layer.

    Column names deliberately match the existing analytics rows (``outage``,
    ``peakWSPD10``, ``outage_per_million_ohline``) so the reasoning agent and
    the heat-map metric picker consume these unchanged. Imperial equivalents
    are added alongside rather than replacing them.
    """
    rows: list[dict[str, Any]] = []
    for row in summary.counties:
        rows.append(
            {
                "county": row.county,
                "stormCode": summary.storm.storm_code,
                "outage": row.outage,
                "peakWSPD10": row.peak_wind_ms,
                "peak_wind_mph": ms_to_mph(row.peak_wind_ms),
                "sumAFWA_TOTPRECIP": row.precip_mm,
                "precip_in": mm_to_inches(row.precip_mm),
                "wgt5": row.wgt5,
                "wgt9": row.wgt9,
                "wgt13": row.wgt13,
                "wgt18": row.wgt18,
                "ohLine": row.oh_line,
                "outage_per_million_ohline": row.outage_per_million_ohline,
            }
        )
    return rows


def ranking_rows(ranking: StormRanking) -> list[dict[str, Any]]:
    return [
        {
            "metric": metric.metric,
            "label": metric.label,
            "value": metric.value,
            "rank": metric.rank,
            "total": metric.total,
            "percentile": metric.percentile,
            "tied_with": metric.tie_count - 1,
            "display": _metric_display(metric.metric, metric.value),
        }
        for metric in ranking.metrics
    ]


def comparison_rows(comparison: StormComparison) -> list[dict[str, Any]]:
    return [
        {
            "metric": delta.metric,
            "label": delta.label,
            "storm_a": delta.value_a,
            "storm_b": delta.value_b,
            "difference": delta.difference,
            "percent_change": delta.percent_change,
        }
        for delta in comparison.deltas
    ]


def forecast_rows(comparison: ForecastComparison) -> list[dict[str, Any]]:
    """Analogue rows. Every outage here is a recorded historical outcome.

    There is deliberately no row for the forecast itself: it has no outage,
    and giving it a row in the same table would invite reading one in.
    """
    return [
        {
            "county": analogue.county,
            "stormCode": analogue.storm_code,
            "date": analogue.date,
            "similarity": analogue.similarity,
            "peakWSPD10": analogue.peak_wind_ms,
            "peak_wind_mph": ms_to_mph(analogue.peak_wind_ms),
            "sumAFWA_TOTPRECIP": analogue.precip_mm,
            "precip_in": mm_to_inches(analogue.precip_mm),
            "outage": analogue.outage,
            "outage_basis": "historical actual",
        }
        for group in comparison.analogue_groups
        for analogue in group.analogues
    ]


def _metric_display(metric: str, value: float | None) -> str:
    if metric == METRIC_MAX_WIND:
        return format_mph(value)
    if metric == METRIC_MAX_PRECIP:
        return format_inches(value)
    return format_count(value)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _ordinal(number: int) -> str:
    """1 → '1st', 2 → '2nd', 11 → '11th'."""
    value = int(number)
    if 10 <= value % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def join_report_sections(sections: Sequence[str]) -> str:
    return "\n\n".join(section for section in sections if section)
