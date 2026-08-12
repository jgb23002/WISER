from __future__ import annotations

import html
import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CT_COUNTY_GEOJSON = (
    REPO_ROOT / "data" / "geography" / "ct_counties_legacy.geojson"
)

CT_LEGACY_COUNTY_SOURCE_URL = (
    "https://geodata.ct.gov/datasets/ctmaps::ct-counties"
)


@dataclass(frozen=True)
class CountyHeatmapMetric:
    key: str
    label: str
    value_label: str
    unit: str
    help_text: str


COUNTY_HEATMAP_METRICS: dict[str, CountyHeatmapMetric] = {
    "outage": CountyHeatmapMetric(
        key="outage",
        label="Outages",
        value_label="Outages",
        unit="trouble spots",
        help_text="Outage trouble spots for a single storm, by county.",
    ),
    "total_outage": CountyHeatmapMetric(
        key="total_outage",
        label="Total outages",
        value_label="Total outage",
        unit="trouble spots",
        help_text="Sum of outage trouble spots by county.",
    ),
    "avg_outage": CountyHeatmapMetric(
        key="avg_outage",
        label="Average outages",
        value_label="Average outage",
        unit="trouble spots",
        help_text="Average outage trouble spots per storm-county record.",
    ),
    "peak_outage": CountyHeatmapMetric(
        key="peak_outage",
        label="Peak outage",
        value_label="Peak outage",
        unit="trouble spots",
        help_text="Largest outage trouble-spot count observed in the county.",
    ),
    "avg_peak_wind": CountyHeatmapMetric(
        key="avg_peak_wind",
        label="Average peak wind",
        value_label="Average peak wind",
        unit="m/s",
        help_text="Average peak wind speed at 10m height.",
    ),
    "max_peak_wind": CountyHeatmapMetric(
        key="max_peak_wind",
        label="Maximum peak wind",
        value_label="Maximum peak wind",
        unit="m/s",
        help_text="Maximum peak wind speed at 10m height.",
    ),
    "outage_per_million_ohline": CountyHeatmapMetric(
        key="outage_per_million_ohline",
        label="Outage per line exposure",
        value_label="Outage per 1M ohLine",
        unit="trouble spots / 1M ohLine",
        help_text="Outage trouble spots normalized by county overhead-line exposure.",
    ),
}

_COUNTY_FIELDS = ("county", "County", "COUNTY", "NAME", "BASENAME")
#: One hue, light to dark: the quantity is a magnitude, so the colour has to
#: order itself the way the numbers do. The previous ramp ran blue then orange
#: then red, which reads as three categories rather than one scale.
COUNTY_HEATMAP_PALETTE = (
    "#cde2fb",
    "#9ec5f4",
    "#6da7ec",
    "#3987e5",
    "#256abf",
    "#184f95",
    "#0d366b",
)
_NO_DATA_COLOR = "#d7dce2"

#: Outage counts are heavily skewed, so a zero-anchored linear scale wastes the
#: whole ramp on the top storm: comparing a 121-outage storm against a 777-outage
#: one on a linear 0–171 scale puts seven of the small storm's eight counties in
#: the first band and paints it a flat white. Positioning by ``value ** 0.4``
#: (near a square root) keeps the ordering exact while giving the low end enough
#: of the ramp to vary. The legend prints each band's value range, so nothing
#: about the compression has to be guessed from the colour.
_SCALE_EXPONENT = 0.4
#: Above this relative luminance a fill takes dark ink; below it, light ink.
_INK_FLIP_LUMINANCE = 0.18


def load_county_geojson(
    path: str | Path = DEFAULT_CT_COUNTY_GEOJSON,
) -> dict[str, Any]:
    """Load the legacy Connecticut county GeoJSON asset."""
    with Path(path).open(encoding="utf-8") as handle:
        data = json.load(handle)
    _validate_geojson(data)
    return data


def available_storm_options(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Return recent storms for heat-map scoping controls."""
    rows = conn.execute(
        """
        SELECT
            s.stormCode,
            s.eventDateTime,
            SUM(w.outage) AS total_outage
        FROM Storm s
        JOIN WeatherData w ON w.stormCode = s.stormCode
        GROUP BY s.stormCode, s.eventDateTime
        ORDER BY s.eventDateTime DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "stormCode": int(row[0]),
            "eventDateTime": row[1],
            "total_outage": int(row[2]),
        }
        for row in rows
    ]


def county_metric_rows(
    conn: sqlite3.Connection,
    metric_key: str,
    *,
    storm_code: int | str | None = None,
) -> list[dict[str, object]]:
    """Aggregate county-level values for one supported heat-map metric."""
    if metric_key not in COUNTY_HEATMAP_METRICS:
        supported = ", ".join(COUNTY_HEATMAP_METRICS)
        raise ValueError(f"Unsupported county heat-map metric: {metric_key}. {supported}")

    where_sql = ""
    params: list[object] = []
    if storm_code is not None:
        where_sql = "WHERE w.stormCode = ?"
        params.append(int(storm_code))

    rows = conn.execute(
        f"""
        SELECT
            w.county,
            COUNT(*) AS storms_observed,
            SUM(w.outage) AS total_outage,
            ROUND(AVG(w.outage), 2) AS avg_outage,
            MAX(w.outage) AS peak_outage,
            ROUND(AVG(w.peakWSPD10), 2) AS avg_peak_wind,
            ROUND(MAX(w.peakWSPD10), 2) AS max_peak_wind,
            c.ohLine,
            ROUND(SUM(w.outage) * 1000000.0 / c.ohLine, 2)
                AS outage_per_million_ohline
        FROM WeatherData w
        JOIN County c ON LOWER(c.county) = LOWER(w.county)
        {where_sql}
        GROUP BY w.county, c.ohLine
        ORDER BY {metric_key} DESC, w.county ASC
        """,
        params,
    ).fetchall()
    return [_coerce_metric_row(row) for row in rows]


def render_county_heatmap_svg(
    geojson: dict[str, Any],
    metric_rows: Sequence[dict[str, object]],
    metric_key: str,
    *,
    title: str | None = None,
    width: int = 860,
    height: int = 560,
    value_range: tuple[float, float] | None = None,
) -> str:
    """Render an inline SVG choropleth for the legacy CT county polygons.

    ``value_range`` pins the colour scale instead of deriving it from these
    rows. Two maps shown side by side MUST share a range: scaled
    independently, a storm with 975 outages paints as dark as one with 21,557,
    which inverts the comparison the reader is making.
    """
    if metric_key not in COUNTY_HEATMAP_METRICS:
        supported = ", ".join(COUNTY_HEATMAP_METRICS)
        raise ValueError(f"Unsupported county heat-map metric: {metric_key}. {supported}")

    features = list(geojson.get("features") or [])
    if not features:
        raise ValueError("GeoJSON does not contain any features.")

    metric = COUNTY_HEATMAP_METRICS[metric_key]
    rows_by_county = {
        normalize_county_name(str(row["county"])): row
        for row in metric_rows
        if row.get("county") is not None
    }
    values = [
        float(row[metric_key])
        for row in metric_rows
        if isinstance(row.get(metric_key), (int, float))
        and math.isfinite(float(row[metric_key]))
    ]
    if value_range is not None:
        min_value, max_value = float(value_range[0]), float(value_range[1])
    else:
        min_value = min(values) if values else 0.0
        max_value = max(values) if values else 0.0
    bbox = _feature_bbox(features)

    paths: list[str] = []
    labels: list[str] = []
    for feature in features:
        county = _feature_county_name(feature)
        row = rows_by_county.get(normalize_county_name(county))
        value = row.get(metric_key) if row else None
        fill = _value_color(float(value), min_value, max_value) if _is_number(value) else _NO_DATA_COLOR
        path_data = _geometry_path(feature["geometry"], bbox, width=width, height=height)
        tooltip = _tooltip_text(county, row, metric_key) if row else f"{county}: no data"
        paths.append(
            "<path "
            f"d=\"{html.escape(path_data)}\" "
            f"fill=\"{fill}\" "
            "stroke=\"#23313f\" stroke-width=\"1.2\" "
            "vector-effect=\"non-scaling-stroke\">"
            f"<title>{html.escape(tooltip)}</title>"
            "</path>"
        )
        label_point = _geometry_label_point(feature["geometry"], bbox, width=width, height=height)
        if label_point is not None:
            label_value = _format_value(value, metric.unit) if _is_number(value) else "No data"
            name_class, value_class = _label_classes(fill)
            labels.append(
                "<text "
                f"x=\"{label_point[0]:.1f}\" y=\"{label_point[1]:.1f}\" "
                "text-anchor=\"middle\" dominant-baseline=\"middle\" "
                f"class=\"{name_class}\">"
                f"{html.escape(county)}"
                f"<tspan x=\"{label_point[0]:.1f}\" dy=\"14\" class=\"{value_class}\">"
                f"{html.escape(label_value)}</tspan></text>"
            )

    legend = _legend_svg(metric, min_value, max_value, width=width, y=height - 64)
    safe_title = html.escape(title or metric.label)
    return (
        "<svg xmlns=\"http://www.w3.org/2000/svg\" "
        f"viewBox=\"0 0 {width} {height}\" role=\"img\" "
        f"aria-label=\"{safe_title}\" style=\"width:100%;height:auto;\">"
        "<style>"
        ".map-title{font:700 22px system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;fill:#f8fafc}"
        ".map-subtitle{font:500 13px system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;fill:#cbd5e1}"
        ".county-label{font:700 12px system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;fill:#101820;paint-order:stroke;stroke:#fff;stroke-width:3px;stroke-linejoin:round}"
        ".county-value{font:600 10px system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;fill:#263747}"
        ".county-ink-light{fill:#f8fafc;stroke:#0b1220}"
        ".legend-label{font:600 11px system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;fill:#cbd5e1}"
        ".legend-band{font:600 10px system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;fill:#cbd5e1}"
        "</style>"
        f"<text x=\"24\" y=\"30\" class=\"map-title\">{safe_title}</text>"
        f"<text x=\"24\" y=\"50\" class=\"map-subtitle\">{html.escape(metric.value_label)} by legacy Connecticut county</text>"
        "<g transform=\"translate(0,18)\">"
        + "".join(paths)
        + "".join(labels)
        + "</g>"
        + legend
        + "</svg>"
    )


def normalize_county_name(value: str) -> str:
    text = " ".join(value.strip().split())
    if text.lower().endswith(" county"):
        text = text[:-7]
    return " ".join(part.capitalize() for part in text.split())


def metric_display_name(metric_key: str) -> str:
    return COUNTY_HEATMAP_METRICS[metric_key].label



def _validate_geojson(data: dict[str, Any]) -> None:
    if data.get("type") != "FeatureCollection":
        raise ValueError("Expected a GeoJSON FeatureCollection.")
    features = data.get("features")
    if not isinstance(features, list):
        raise ValueError("GeoJSON FeatureCollection has no features list.")


def _coerce_metric_row(row: sqlite3.Row | Sequence[Any]) -> dict[str, object]:
    return {
        "county": row[0],
        "storms_observed": int(row[1]),
        "total_outage": int(row[2]),
        "avg_outage": float(row[3]),
        "peak_outage": int(row[4]),
        "avg_peak_wind": float(row[5]),
        "max_peak_wind": float(row[6]),
        "ohLine": int(row[7]),
        "outage_per_million_ohline": float(row[8]),
    }


def _feature_county_name(feature: dict[str, Any]) -> str:
    properties = feature.get("properties") or {}
    for field in _COUNTY_FIELDS:
        value = properties.get(field)
        if value:
            return normalize_county_name(str(value))
    return "Unknown"


def _feature_bbox(features: Sequence[dict[str, Any]]) -> tuple[float, float, float, float]:
    points = [
        point
        for feature in features
        for point in _iter_geometry_points(feature.get("geometry") or {})
    ]
    if not points:
        raise ValueError("GeoJSON features do not contain polygon coordinates.")
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _iter_geometry_points(geometry: dict[str, Any]) -> Iterable[tuple[float, float]]:
    for ring in _iter_rings(geometry):
        for point in ring:
            if len(point) >= 2:
                yield (float(point[0]), float(point[1]))


def _iter_rings(geometry: dict[str, Any]) -> Iterable[Sequence[Sequence[float]]]:
    geom_type = geometry.get("type")
    coordinates = geometry.get("coordinates") or []
    if geom_type == "Polygon":
        yield from coordinates
    elif geom_type == "MultiPolygon":
        for polygon in coordinates:
            yield from polygon


def _geometry_path(
    geometry: dict[str, Any],
    bbox: tuple[float, float, float, float],
    *,
    width: int,
    height: int,
) -> str:
    parts: list[str] = []
    for ring in _iter_rings(geometry):
        projected = [
            _project(float(point[0]), float(point[1]), bbox, width=width, height=height)
            for point in ring
            if len(point) >= 2
        ]
        if not projected:
            continue
        first_x, first_y = projected[0]
        parts.append(f"M {first_x:.1f} {first_y:.1f}")
        parts.extend(f"L {x:.1f} {y:.1f}" for x, y in projected[1:])
        parts.append("Z")
    return " ".join(parts)


def _geometry_label_point(
    geometry: dict[str, Any],
    bbox: tuple[float, float, float, float],
    *,
    width: int,
    height: int,
) -> tuple[float, float] | None:
    rings = list(_iter_rings(geometry))
    if not rings:
        return None
    ring = max(rings, key=len)
    points = [
        _project(float(point[0]), float(point[1]), bbox, width=width, height=height)
        for point in ring
        if len(point) >= 2
    ]
    if not points:
        return None
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


def _project(
    lon: float,
    lat: float,
    bbox: tuple[float, float, float, float],
    *,
    width: int,
    height: int,
) -> tuple[float, float]:
    min_lon, min_lat, max_lon, max_lat = bbox
    map_top = 70
    # Clearance for the banded legend and its range labels along the bottom.
    map_bottom = 106
    pad_x = 24
    usable_width = width - (pad_x * 2)
    usable_height = height - map_top - map_bottom
    lon_span = max(max_lon - min_lon, 0.000001)
    lat_span = max(max_lat - min_lat, 0.000001)
    scale = min(usable_width / lon_span, usable_height / lat_span)
    drawn_width = lon_span * scale
    drawn_height = lat_span * scale
    x_offset = (width - drawn_width) / 2
    y_offset = map_top + (usable_height - drawn_height) / 2
    x = x_offset + (lon - min_lon) * scale
    y = y_offset + (max_lat - lat) * scale
    return (x, y)


def _scale_position(value: float, min_value: float, max_value: float) -> float:
    """Where ``value`` sits on the colour ramp, as 0..1.

    A scale anchored at zero is a magnitude scale and gets compressed (see
    ``_SCALE_EXPONENT``). A scale whose floor is already well above zero — peak
    wind across counties of one storm, say — is a narrow band where the reader
    cares about the spread inside it, so that one stays linear.
    """
    span = max_value - min_value
    if span <= 0:
        return 1.0
    linear = min(1.0, max(0.0, (value - min_value) / span))
    if min_value > 0:
        return linear
    return linear**_SCALE_EXPONENT


def _band_edges(min_value: float, max_value: float) -> list[float]:
    """The value at every band boundary, lowest first. One more than bands.

    The inverse of :func:`_scale_position`, so the legend can state the range
    each colour covers instead of only the two endpoints.
    """
    bands = len(COUNTY_HEATMAP_PALETTE)
    span = max_value - min_value
    if span <= 0:
        return [min_value] * (bands + 1)
    exponent = 1.0 if min_value > 0 else 1.0 / _SCALE_EXPONENT
    return [min_value + span * (index / bands) ** exponent for index in range(bands + 1)]


def _value_color(value: float, min_value: float, max_value: float) -> str:
    if not math.isfinite(value):
        return _NO_DATA_COLOR
    if math.isclose(min_value, max_value):
        return COUNTY_HEATMAP_PALETTE[-1]
    bands = len(COUNTY_HEATMAP_PALETTE)
    position = _scale_position(value, min_value, max_value)
    index = min(bands - 1, max(0, int(position * bands)))
    return COUNTY_HEATMAP_PALETTE[index]


def _relative_luminance(color: str) -> float:
    """WCAG relative luminance of a ``#rrggbb`` fill."""
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _label_classes(fill: str) -> tuple[str, str]:
    """CSS classes for the name and value drawn on a county filled ``fill``.

    Labels flip to light ink on the dark end of the ramp. Without this the
    heaviest counties — the ones the reader looks at first — carry near-black
    text on a near-black fill.
    """
    if _relative_luminance(fill) < _INK_FLIP_LUMINANCE:
        return ("county-label county-ink-light", "county-value county-ink-light")
    return ("county-label", "county-value")


def _legend_svg(
    metric: CountyHeatmapMetric,
    min_value: float,
    max_value: float,
    *,
    width: int,
    y: int,
) -> str:
    """A banded legend that states the value range each colour stands for.

    Endpoint-only labels leave every shade between them a guess, and they hide
    that the bands are unequal in width (see ``_SCALE_EXPONENT``).
    """
    x = 24
    gap = 4
    no_data_width = 120
    bands = len(COUNTY_HEATMAP_PALETTE)
    usable = width - (x * 2) - no_data_width
    box_width = max(28, (usable - gap * (bands - 1)) / bands)
    band_labels = _band_labels(_band_edges(min_value, max_value))

    header = metric.value_label
    if metric.unit.lower() not in header.lower():
        header = f"{header} ({metric.unit})"
    parts = [
        f"<text x=\"{x}\" y=\"{y - 10}\" class=\"legend-label\">{html.escape(header)}</text>"
    ]
    for index, color in enumerate(COUNTY_HEATMAP_PALETTE):
        box_x = x + index * (box_width + gap)
        parts.append(
            f"<rect x=\"{box_x:.1f}\" y=\"{y}\" "
            f"width=\"{box_width:.1f}\" height=\"14\" rx=\"2\" fill=\"{color}\" />"
        )
        parts.append(
            f"<text x=\"{box_x + box_width / 2:.1f}\" y=\"{y + 27}\" "
            "text-anchor=\"middle\" class=\"legend-band\">"
            f"{html.escape(band_labels[index])}</text>"
        )

    no_data_x = x + bands * (box_width + gap) + 12
    parts.append(
        f"<rect x=\"{no_data_x:.1f}\" y=\"{y}\" width=\"24\" height=\"14\" rx=\"2\" "
        f"fill=\"{_NO_DATA_COLOR}\" />"
    )
    parts.append(
        f"<text x=\"{no_data_x + 32:.1f}\" y=\"{y + 12}\" class=\"legend-label\">No data</text>"
    )
    return "<g>" + "".join(parts) + "</g>"


def _band_labels(edges: Sequence[float]) -> list[str]:
    """``low–high`` for every band, at the coarsest precision that stays distinct.

    Counts want no decimals; a metric whose whole range is a couple of units
    wide would collapse to "9–9" without them.
    """
    texts = [_round_to(value, 0) for value in edges]
    for decimals in (1, 2):
        if len(set(texts)) == len(texts) or float(edges[-1]) <= float(edges[0]):
            break
        texts = [_round_to(value, decimals) for value in edges]
    return [f"{texts[index]}–{texts[index + 1]}" for index in range(len(edges) - 1)]


def _round_to(value: float, decimals: int) -> str:
    if decimals == 0:
        return f"{round(value):,}"
    return f"{value:,.{decimals}f}"


#: Extra tooltip context, shown only when the caller's rows carry it. Storm-scoped
#: rows and aggregate rows have different columns, and listing an absent one as
#: "No data" reads as a gap in the data rather than a column this view never had.
_TOOLTIP_EXTRAS: tuple[tuple[str, str, str], ...] = (
    ("total_outage", "total outage", "trouble spots"),
    ("avg_peak_wind", "avg peak wind", "m/s"),
    ("peakWSPD10", "peak wind", "m/s"),
    ("outage_per_million_ohline", "per 1M ohLine", "trouble spots / 1M ohLine"),
)


def _tooltip_text(
    county: str,
    row: dict[str, object] | None,
    metric_key: str,
) -> str:
    if not row:
        return f"{county}: no data"
    metric = COUNTY_HEATMAP_METRICS[metric_key]
    parts = [f"{county}: {metric.value_label} {_format_value(row.get(metric_key), metric.unit)}"]
    for key, label, unit in _TOOLTIP_EXTRAS:
        if key == metric_key or not _is_number(row.get(key)):
            continue
        parts.append(f"{label} {_format_value(row.get(key), unit)}")
    return "; ".join(parts)


def _format_value(value: object, unit: str) -> str:
    if not _is_number(value):
        return "No data"
    number = float(value)
    if unit == "m/s":
        return f"{number:.2f} {unit}"
    if "1M" in unit:
        return f"{number:.2f}"
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}"


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))
