"""
Multi-dimensional L2 Z-score similarity engine.

Pure Python, no external dependencies.
Importable with zero side effects.

The core idea
-------------
1. Standardise each feature to Z-scores (mean=0, std=1) across the historical
   dataset so all features live in the same unit-free space.
2. Measure distance as the weighted Euclidean (L2) norm in that Z-score space.
3. Convert distance to similarity:  similarity = 1 / (1 + distance)  ∈ (0, 1]

Single-feature parity guarantee
---------------------------------
rank_by_similarity(data, {"peakWSPD10": v}) produces exactly the same rankings
and scores as the legacy 1-D algorithm in find_similar_storms.py, because:
  sqrt(w * (z_hist - z_query)^2)  with w=1  =  |z_hist - z_query|
"""

from __future__ import annotations

import math


def compute_feature_stats(
    records: list[dict[str, object]],
    features: list[str],
) -> dict[str, tuple[float, float]]:
    """
    Compute per-feature sample mean and standard deviation (Bessel's correction).

    Args:
        records:  Historical dataset rows (each a dict with numeric feature keys).
        features: Feature names to compute statistics for.

    Returns:
        {feature: (mean, std_dev)}
        Empty records  → (0.0, 0.0) for every feature.
        Single record  → (value, 0.0) for every feature.
    """
    n = len(records)
    if n == 0:
        return {f: (0.0, 0.0) for f in features}

    result: dict[str, tuple[float, float]] = {}
    for feature in features:
        values = [float(r[feature]) for r in records]
        mean = sum(values) / n
        if n < 2:
            result[feature] = (mean, 0.0)
        else:
            variance = sum((x - mean) ** 2 for x in values) / (n - 1)
            result[feature] = (mean, math.sqrt(variance))
    return result


def compute_l2_zscore_distance(
    hist_values: dict[str, float],
    query_values: dict[str, float],
    stats: dict[str, tuple[float, float]],
    weights: dict[str, float],
) -> float:
    """
    Weighted L2 distance in Z-score space between two feature vectors.

    distance = sqrt( sum_f( w_f * (z_hist_f - z_query_f)^2 ) )

    Zero std-dev handling (all historical values identical for that feature):
      - query matches historical value  → 0 contribution (distances not penalised)
      - query differs from historical   → math.inf (incomparable; similarity = 0)

    Args:
        hist_values:  Feature values for one historical record.
        query_values: Feature values for the incoming query.
        stats:        {feature: (mean, std_dev)} from compute_feature_stats.
        weights:      {feature: weight}, all non-negative.

    Returns:
        Non-negative float, or math.inf when any zero-std-dev feature mismatches.
    """
    squared_sum = 0.0
    for feature, q_val in query_values.items():
        mean, std_dev = stats[feature]
        h_val = float(hist_values[feature])
        w = weights.get(feature, 1.0)

        if std_dev == 0.0:
            if h_val != q_val:
                return math.inf
            # exact match on a constant feature → zero contribution, continue
        else:
            z_hist = (h_val - mean) / std_dev
            z_query = (q_val - mean) / std_dev
            squared_sum += w * (z_hist - z_query) ** 2

    return math.sqrt(squared_sum)


def distance_to_similarity(distance: float) -> float:
    """
    Convert a non-negative distance to a similarity score in (0, 1].

      similarity = 1 / (1 + distance)
      math.inf   → 0.0
      0.0        → 1.0
    """
    if math.isinf(distance):
        return 0.0
    return 1.0 / (1.0 + distance)


def rank_by_similarity(
    historical: list[dict[str, object]],
    query_params: dict[str, float],
    feature_weights: dict[str, float] | None = None,
) -> list[tuple[dict[str, object], float]]:
    """
    Rank historical records by multi-dimensional L2 Z-score similarity.

    Args:
        historical:      List of historical storm records; each dict must
                         contain all keys present in query_params.
        query_params:    {feature: query_value} — only the listed features
                         are scored.  Other fields in the records are ignored.
        feature_weights: Optional {feature: weight}.  Defaults to 1.0 per
                         feature.

    Returns:
        List of (record, similarity_score) pairs sorted descending by score.

    Single-feature parity guarantee:
        rank_by_similarity(data, {"peakWSPD10": v}) produces the same order
        and scores as the legacy calculate_similarity() in find_similar_storms.py.
    """
    if not historical or not query_params:
        return []

    features = list(query_params.keys())
    weights = {f: (feature_weights or {}).get(f, 1.0) for f in features}

    stats = compute_feature_stats(historical, features)

    scored: list[tuple[dict[str, object], float]] = []
    for record in historical:
        hist_vals = {f: float(record[f]) for f in features}
        distance = compute_l2_zscore_distance(hist_vals, query_params, stats, weights)
        scored.append((record, distance_to_similarity(distance)))

    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored
