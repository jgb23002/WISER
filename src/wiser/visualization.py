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
_PALETTE = ("#f7fbff", "#c6dbef", "#6baed6", "#fd8d3c", "#bd0026")
_NO_DATA_COLOR = "#d7dce2"


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
) -> str:
    """Render an inline SVG choropleth for the legacy CT county polygons."""
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
            labels.append(
                "<text "
                f"x=\"{label_point[0]:.1f}\" y=\"{label_point[1]:.1f}\" "
                "text-anchor=\"middle\" dominant-baseline=\"middle\" "
                "class=\"county-label\">"
                f"{html.escape(county)}"
                f"<tspan x=\"{label_point[0]:.1f}\" dy=\"14\" class=\"county-value\">"
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
        ".legend-label{font:600 11px system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;fill:#cbd5e1}"
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
    map_bottom = 94
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


def _value_color(value: float, min_value: float, max_value: float) -> str:
    if not math.isfinite(value):
        return _NO_DATA_COLOR
    if math.isclose(min_value, max_value):
        return _PALETTE[-1]
    ratio = (value - min_value) / (max_value - min_value)
    index = min(len(_PALETTE) - 1, max(0, int(ratio * (len(_PALETTE) - 1))))
    return _PALETTE[index]


def _legend_svg(
    metric: CountyHeatmapMetric,
    min_value: float,
    max_value: float,
    *,
    width: int,
    y: int,
) -> str:
    box_width = 44
    gap = 4
    x = 24
    parts = [
        f"<text x=\"{x}\" y=\"{y - 10}\" class=\"legend-label\">{html.escape(metric.value_label)}</text>"
    ]
    for index, color in enumerate(_PALETTE):
        parts.append(
            f"<rect x=\"{x + index * (box_width + gap)}\" y=\"{y}\" "
            f"width=\"{box_width}\" height=\"14\" rx=\"2\" fill=\"{color}\" />"
        )
    parts.append(
        f"<text x=\"{x}\" y=\"{y + 34}\" class=\"legend-label\">"
        f"{html.escape(_format_value(min_value, metric.unit))}</text>"
    )
    right_x = x + (len(_PALETTE) - 1) * (box_width + gap) + box_width
    parts.append(
        f"<text x=\"{right_x}\" y=\"{y + 34}\" text-anchor=\"end\" class=\"legend-label\">"
        f"{html.escape(_format_value(max_value, metric.unit))}</text>"
    )
    no_data_x = max(right_x + 36, width - 164)
    parts.append(
        f"<rect x=\"{no_data_x}\" y=\"{y}\" width=\"24\" height=\"14\" rx=\"2\" "
        f"fill=\"{_NO_DATA_COLOR}\" />"
    )
    parts.append(
        f"<text x=\"{no_data_x + 32}\" y=\"{y + 12}\" class=\"legend-label\">No data</text>"
    )
    return "<g>" + "".join(parts) + "</g>"


def _tooltip_text(
    county: str,
    row: dict[str, object] | None,
    metric_key: str,
) -> str:
    if not row:
        return f"{county}: no data"
    metric = COUNTY_HEATMAP_METRICS[metric_key]
    return (
        f"{county}: {metric.value_label} "
        f"{_format_value(row.get(metric_key), metric.unit)}; "
        f"total outage {_format_value(row.get('total_outage'), 'trouble spots')}; "
        f"avg peak wind {_format_value(row.get('avg_peak_wind'), 'm/s')}"
    )


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
