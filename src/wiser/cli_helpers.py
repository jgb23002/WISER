from __future__ import annotations

FEATURE_ALIASES: dict[str, str] = {
    "wind": "peakWSPD10",
    "windspeed": "peakWSPD10",
    "wind_speed": "peakWSPD10",
    "peakwspd10": "peakWSPD10",
    "precip": "sumAFWA_TOTPRECIP",
    "precipitation": "sumAFWA_TOTPRECIP",
    "sumafwa_totprecip": "sumAFWA_TOTPRECIP",
    "wgt5": "wgt5",
    "wgt9": "wgt9",
    "wgt13": "wgt13",
    "wgt18": "wgt18",
}

# Ordered (argparse dest, WeatherData column) pairs used by find_similar_storms.py
# to translate parsed CLI args into feature dicts.  Keep in sync with FEATURE_ALIASES.
FLAG_TO_FEATURE: list[tuple[str, str]] = [
    ("wind_speed", "peakWSPD10"),
    ("precip", "sumAFWA_TOTPRECIP"),
    ("wgt5", "wgt5"),
    ("wgt9", "wgt9"),
    ("wgt13", "wgt13"),
    ("wgt18", "wgt18"),
]


def validate_wind_positive(params: dict[str, float]) -> str | None:
    """Return an error message if peakWSPD10 is present but not positive, else None."""
    if "peakWSPD10" in params and params["peakWSPD10"] <= 0:
        return "Wind speed must be positive (> 0)."
    return None


def validate_limit(limit: int) -> str | None:
    """Return an error message if limit < 1, else None."""
    if limit < 1:
        return "Limit must be at least 1."
    return None
