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
    #: True when zero is a meaningful floor for this quantity — "no outages",
    #: not "the smallest reading in this slice". Magnitude metrics are anchored
    #: at zero and compressed (see ``_SCALE_EXPONENT``); everything else is a
    #: narrow band the reader wants spread out, so it stays linear. Deriving
    #: this from the data instead put the choice on whether some county
    #: happened to report a zero, which flipped the whole map on one row.
    zero_anchored: bool = False


COUNTY_HEATMAP_METRICS: dict[str, CountyHeatmapMetric] = {
    "outage": CountyHeatmapMetric(
        key="outage",
        label="Outages",
        value_label="Outages",
        unit="trouble spots",
        help_text="Outage trouble spots for a single storm, by county.",
        zero_anchored=True,
    ),
    "total_outage": CountyHeatmapMetric(
        key="total_outage",
        label="Total outages",
        value_label="Total outage",
        unit="trouble spots",
        help_text="Sum of outage trouble spots by county.",
        zero_anchored=True,
    ),
    "avg_outage": CountyHeatmapMetric(
        key="avg_outage",
        label="Average outages",
        value_label="Average outage",
        unit="trouble spots",
        help_text="Average outage trouble spots per storm-county record.",
        zero_anchored=True,
    ),
    "peak_outage": CountyHeatmapMetric(
        key="peak_outage",
        label="Peak outage",
        value_label="Peak outage",
        unit="trouble spots",
        help_text="Largest outage trouble-spot count observed in the county.",
        zero_anchored=True,
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
        zero_anchored=True,
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
#: No data is not a value on the ramp, so it is drawn as a texture rather than
#: as another shade. A grey can only ever be one step of a seven-step ramp away
#: from colliding with it — the previous #d7dce2 sat at 1.04:1 against the
#: ramp's lightest colour, which is no difference at all on a projector. The
#: base is warm where the ramp is cool, and the hatch carries the distinction
#: even in greyscale.
NO_DATA_PATTERN_ID = "wiser-no-data"
NO_DATA_BASE_COLOR = "#fcf9f4"
_NO_DATA_HATCH_COLOR = "#9aa3ad"
_NO_DATA_COLOR = f"url(#{NO_DATA_PATTERN_ID})"
#: The hatch is drawn for a panel shown at 1:1, so the period is chosen to read
#: at that size rather than through a downscale.
_NO_DATA_PATTERN = (
    f"<defs><pattern id=\"{NO_DATA_PATTERN_ID}\" width=\"8\" height=\"8\" "
    "patternUnits=\"userSpaceOnUse\" patternTransform=\"rotate(45)\">"
    f"<rect width=\"8\" height=\"8\" fill=\"{NO_DATA_BASE_COLOR}\" />"
    f"<line x1=\"0\" y1=\"0\" x2=\"0\" y2=\"8\" stroke=\"{_NO_DATA_HATCH_COLOR}\" "
    "stroke-width=\"2\" />"
    "</pattern></defs>"
)

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

#: Dimensions for a map drawn in a side-by-side comparison panel. Type in an
#: SVG is sized in viewBox units, so it only arrives at its declared size when
#: the viewBox matches the frame. Rendering the full-width 860x560 map into a
#: ~450px column scaled everything by ~0.5 and put the band labels near 5px;
#: no font size can win against that, because the transform scales it too.
#: The renderer therefore draws panels at their own size and the frame matches.
PANEL_MAP_WIDTH = 480
PANEL_MAP_HEIGHT = 460


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
        fill = (
            _value_color(
                float(value),
                min_value,
                max_value,
                zero_anchored=metric.zero_anchored,
            )
            if _is_number(value)
            else _NO_DATA_COLOR
        )
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
            # Ink is chosen against a colour, and the no-data fill is a pattern
            # reference; its base is what the label actually sits on.
            name_class, value_class = _label_classes(
                fill if fill != _NO_DATA_COLOR else NO_DATA_BASE_COLOR
            )
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
        ".legend-band{font:600 11px system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;fill:#cbd5e1}"
        "</style>"
        + _NO_DATA_PATTERN
        + f"<text x=\"24\" y=\"30\" class=\"map-title\">{safe_title}</text>"
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


def metric_unit(metric_key: str) -> str | None:
    """The registered unit for ``metric_key``, or None if it has none.

    One place owns unit strings. Callers that print a metric's value read it
    from here rather than restating it, so a rename cannot leave two spellings
    of the same unit in circulation.
    """
    metric = COUNTY_HEATMAP_METRICS.get(metric_key)
    return metric.unit if metric else None


def is_zero_anchored(metric_key: str) -> bool:
    """Whether zero is a meaningful floor for ``metric_key``.

    Unknown metrics answer False: a quantity nobody has declared a floor for
    gets the linear scale, which is the safe reading of an unknown unit.
    """
    metric = COUNTY_HEATMAP_METRICS.get(metric_key)
    return bool(metric and metric.zero_anchored)


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


def _scale_position(
    value: float,
    min_value: float,
    max_value: float,
    *,
    zero_anchored: bool,
) -> float:
    """Where ``value`` sits on the colour ramp, as 0..1.

    A magnitude scale gets compressed (see ``_SCALE_EXPONENT``). A quantity
    with no meaningful zero — peak wind across the counties of one storm, say —
    is a narrow band where the reader cares about the spread inside it, so that
    one stays linear. Which of the two applies is declared by the metric, not
    inferred from whether this slice of data happens to contain a zero.
    """
    span = max_value - min_value
    if span <= 0:
        return 1.0
    linear = min(1.0, max(0.0, (value - min_value) / span))
    if not zero_anchored:
        return linear
    return linear**_SCALE_EXPONENT


def _band_edges(
    min_value: float, max_value: float, *, zero_anchored: bool
) -> list[float]:
    """The value at every band boundary, lowest first. One more than bands.

    The inverse of :func:`_scale_position`, so the legend can state the range
    each colour covers instead of only the two endpoints.
    """
    bands = len(COUNTY_HEATMAP_PALETTE)
    span = max_value - min_value
    if span <= 0:
        return [min_value] * (bands + 1)
    exponent = 1.0 / _SCALE_EXPONENT if zero_anchored else 1.0
    return [min_value + span * (index / bands) ** exponent for index in range(bands + 1)]


def _is_flat_range(min_value: float, max_value: float) -> bool:
    """True when every county carries the same value, so there is no spread."""
    return math.isclose(min_value, max_value)


def _flat_color(value: float) -> str:
    """The one colour a map with no spread in it should wear.

    A flat range says nothing about relative magnitude, so it cannot honestly
    use either end of the ramp — except at zero, which is not "the bottom of
    this range" but "nothing happened", and reads as the empty end. Painting
    the dark end here turned two storms that both recorded no outages into a
    solid near-black Connecticut.
    """
    if math.isclose(value, 0.0):
        return COUNTY_HEATMAP_PALETTE[0]
    return COUNTY_HEATMAP_PALETTE[len(COUNTY_HEATMAP_PALETTE) // 2]


def _value_color(
    value: float,
    min_value: float,
    max_value: float,
    *,
    zero_anchored: bool,
) -> str:
    if not math.isfinite(value):
        return _NO_DATA_COLOR
    if _is_flat_range(min_value, max_value):
        return _flat_color(value)
    bands = len(COUNTY_HEATMAP_PALETTE)
    position = _scale_position(value, min_value, max_value, zero_anchored=zero_anchored)
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
    bands = len(COUNTY_HEATMAP_PALETTE)

    header = _legend_header(metric)
    parts = [
        f"<text x=\"{x}\" y=\"{y - 10}\" class=\"legend-label\">{html.escape(header)}</text>"
    ]

    if _is_flat_range(min_value, max_value):
        # No spread means no bands. Seven swatches all labelled "0–0" describe a
        # scale that isn't there; one swatch and the value itself is the truth.
        flat = _format_value(min_value, metric.unit)
        parts.append(
            f"<rect x=\"{x}\" y=\"{y}\" width=\"24\" height=\"14\" rx=\"2\" "
            f"fill=\"{_flat_color(min_value)}\" />"
        )
        parts.append(
            f"<text x=\"{x + 32}\" y=\"{y + 12}\" class=\"legend-label\">"
            f"All counties {html.escape(flat)}</text>"
        )
        swatch_right = x + 24 + 8 + _text_width(f"All counties {flat}")
    else:
        box_width = _band_box_width(width, x=x, gap=gap, bands=bands)
        edges = _band_edges(min_value, max_value, zero_anchored=metric.zero_anchored)
        for index, color in enumerate(COUNTY_HEATMAP_PALETTE):
            box_x = x + index * (box_width + gap)
            parts.append(
                f"<rect x=\"{box_x:.1f}\" y=\"{y}\" "
                f"width=\"{box_width:.1f}\" height=\"14\" rx=\"2\" fill=\"{color}\" />"
            )
        for edge_index, text in _labelled_edges(edges):
            edge_x = _edge_x(edge_index, x=x, box_width=box_width, gap=gap, bands=bands)
            parts.append(
                f"<text x=\"{edge_x:.1f}\" y=\"{y + 27}\" "
                f"text-anchor=\"{_edge_anchor(edge_index, bands)}\" class=\"legend-band\">"
                f"{html.escape(text)}</text>"
            )
        swatch_right = x + bands * box_width + (bands - 1) * gap

    no_data_x, no_data_y = _no_data_origin(swatch_right, width=width, x=x, y=y)
    parts.append(
        f"<rect x=\"{no_data_x:.1f}\" y=\"{no_data_y}\" width=\"{_NO_DATA_SWATCH}\" "
        f"height=\"14\" rx=\"2\" fill=\"{_NO_DATA_COLOR}\" />"
    )
    parts.append(
        f"<text x=\"{no_data_x + _NO_DATA_SWATCH + _NO_DATA_GAP:.1f}\" "
        f"y=\"{no_data_y + 12}\" class=\"legend-label\">No data</text>"
    )
    return "<g>" + "".join(parts) + "</g>"


#: The No-data key: swatch, gap, then the label itself.
_NO_DATA_SWATCH = 24
_NO_DATA_GAP = 8
_NO_DATA_BLOCK = _NO_DATA_SWATCH + _NO_DATA_GAP + 52
#: Clearance between the colour swatches and the No-data key beside them.
_NO_DATA_CLEARANCE = 12


def _legend_header(metric: CountyHeatmapMetric) -> str:
    """``label (unit)``, minus any part of the unit the label already says.

    A whole-string containment test only catches an exact repeat, so
    "Outage per 1M ohLine" paired with "trouble spots / 1M ohLine" printed
    "1M ohLine" twice in one 45-character label. Comparing on the unit's
    distinctive tail catches the overlap that actually occurs.
    """
    label = metric.value_label
    unit = metric.unit
    tail = unit.rsplit("/", 1)[-1].strip()
    if tail.lower() in label.lower():
        unit = unit[: unit.rfind(tail)].strip().rstrip("/").strip()
    if not unit or unit.lower() in label.lower():
        return label
    return f"{label} ({unit})"


def _band_box_width(width: int, *, x: int, gap: int, bands: int) -> float:
    """Swatch width that leaves the No-data key its reserved room.

    Solved against the full budget — two margins, the inter-swatch gaps, the
    clearance and the key — rather than against a bare subtraction, so the
    swatches cannot run into the key at the default width.
    """
    budget = width - (x * 2) - _NO_DATA_CLEARANCE - _NO_DATA_BLOCK - gap * (bands - 1)
    return max(28, budget / bands)


def _no_data_origin(
    swatch_right: float, *, width: int, x: int, y: int
) -> tuple[float, int]:
    """Where the No-data key goes: beside the swatches, or on its own row.

    Below a certain width the swatches hit their minimum size and stop
    shrinking, at which point there is no longer room beside them. Deriving
    this from the swatches' true right edge keeps the key inside the viewBox
    instead of pushing it off the canvas.
    """
    beside = swatch_right + _NO_DATA_CLEARANCE
    if beside + _NO_DATA_BLOCK <= width - x:
        return (beside, y)
    return (float(x), y + 44)


def _text_width(text: str) -> float:
    """Rough width of a legend label at 11px, for layout arithmetic only."""
    return len(text) * 6.2


def _labelled_edges(edges: Sequence[float]) -> list[tuple[int, str]]:
    """Which band boundaries carry a number, and what it says.

    Every boundary would collide at panel width, so alternate ones are labelled
    and the reader reads the ones between as lying in between. Both ends are
    always labelled — a scale whose top is unstated cannot be read at all.
    """
    last = len(edges) - 1
    indices = list(range(0, last + 1, 2))
    if indices[-1] != last:
        indices.append(last)
    texts = _edge_texts(edges)
    return [(index, texts[index]) for index in indices]


def _edge_x(
    index: int, *, x: int, box_width: float, gap: int, bands: int
) -> float:
    """The x of band boundary ``index``: swatch edges, and midway across gaps."""
    if index <= 0:
        return float(x)
    if index >= bands:
        return x + bands * box_width + (bands - 1) * gap
    return x + index * (box_width + gap) - gap / 2


def _edge_anchor(index: int, bands: int) -> str:
    """Endpoints anchor inward so their text cannot leave the viewBox."""
    if index <= 0:
        return "start"
    if index >= bands:
        return "end"
    return "middle"


def _edge_texts(edges: Sequence[float]) -> list[str]:
    """Every boundary value, at the coarsest precision that keeps them distinct.

    Counts want no decimals; a metric whose whole range is a couple of units
    wide would collapse every boundary to "9" without them.
    """
    # A range with no width can never be made distinct, so there is nothing to
    # chase; hoisted out of the loop because it cannot change inside it.
    degenerate = edges[-1] <= edges[0]
    texts = [_round_to(value, 0) for value in edges]
    if degenerate:
        return texts
    for decimals in (0, 1, 2):
        texts = [_round_to(value, decimals) for value in edges]
        if len(set(texts)) == len(texts):
            break
    return texts


def _round_to(value: float, decimals: int) -> str:
    """One renderer for both the county labels and the legend boundaries.

    Delegates to :func:`_format_value` so a quantity is spelled the same way
    everywhere in one image, and rounds half away from zero. ``round()`` is
    banker's rounding: it printed 2.5 as "2" and 3.5 as "4", and a legend
    boundary that disagrees with the county label beside it on .5 is a
    discrepancy the reader cannot diagnose.
    """
    factor = 10**decimals
    magnitude = math.floor(abs(value) * factor + 0.5) / factor
    return _format_value(
        -magnitude if value < 0 else magnitude, "", decimals=decimals
    )


#: Extra tooltip context, shown only when the caller's rows carry it. Storm-scoped
#: rows and aggregate rows have different columns, and listing an absent one as
#: "No data" reads as a gap in the data rather than a column this view never had.
#: ``(row key, tooltip label)``. Units are NOT restated here — they are read
#: from the metric registry, so renaming a unit there cannot leave the tooltip
#: printing the old spelling with no test or type error to catch it.
_TOOLTIP_EXTRAS: tuple[tuple[str, str], ...] = (
    ("outage", "outages"),
    ("total_outage", "total outage"),
    ("avg_peak_wind", "avg peak wind"),
    ("peakWSPD10", "peak wind"),
    ("outage_per_million_ohline", "per 1M ohLine"),
)
#: peakWSPD10 is a raw WeatherData column rather than a heat-map metric, so the
#: registry has no unit for it. It is the only one that has to be declared here.
_UNREGISTERED_TOOLTIP_UNITS = {"peakWSPD10": "m/s"}


def _tooltip_unit(key: str) -> str:
    return metric_unit(key) or _UNREGISTERED_TOOLTIP_UNITS.get(key, "")


def _tooltip_text(
    county: str,
    row: dict[str, object] | None,
    metric_key: str,
) -> str:
    if not row:
        return f"{county}: no data"
    metric = COUNTY_HEATMAP_METRICS[metric_key]
    parts = [f"{county}: {metric.value_label} {_format_value(row.get(metric_key), metric.unit)}"]
    for key, label in _TOOLTIP_EXTRAS:
        if key == metric_key or not _is_number(row.get(key)):
            continue
        parts.append(f"{label} {_format_value(row.get(key), _tooltip_unit(key))}")
    return "; ".join(parts)


def _format_value(value: object, unit: str, *, decimals: int | None = None) -> str:
    """Render a metric value. ``decimals`` pins the precision for band labels."""
    if not _is_number(value):
        return "No data"
    number = float(value)
    if decimals is not None:
        text = f"{number:,.{decimals}f}"
        return f"{text} {unit}".rstrip() if unit else text
    if unit == "m/s":
        return f"{number:.2f} {unit}"
    if "1M" in unit:
        return f"{number:.2f}"
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}"


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))
