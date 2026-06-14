"""
SimilarityQueryAgent: multi-dimensional Z-score storm similarity.

Wraps the similarity engine, owns default feature weights for the current
Connecticut dataset, and accepts per-call weight overrides for future
datasets or regions.

The engine and this agent know nothing about natural language.
String-to-feature aliasing (e.g. "wind" → "peakWSPD10") belongs only in
the CLI layer (find_similar_storms.py).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import ClassVar

from wiser.similarity_engine import rank_by_similarity


@dataclass
class SimilarityQueryAgent:
    """
    Agent for multi-dimensional Z-score storm similarity queries.

    Default weights reflect equal importance across all 6 weather features
    in the current Connecticut dataset.  Pass a ``feature_weights`` dict to
    adjust for different datasets or research questions without modifying
    this class or any other agent.

    Usage::

        agent = SimilarityQueryAgent()
        results = agent.find_similar(conn, "Fairfield", {"peakWSPD10": 9.2})

        # Precipitation-heavy analysis for a future dataset
        agent2 = SimilarityQueryAgent(
            feature_weights={"peakWSPD10": 1.0, "sumAFWA_TOTPRECIP": 3.0,
                             "wgt5": 1.0, "wgt9": 1.0, "wgt13": 1.0, "wgt18": 1.0}
        )
    """

    # Equal default weights for all 6 CT weather features.
    # Override at instantiation via feature_weights= for other datasets.
    DEFAULT_WEIGHTS: ClassVar[dict[str, float]] = {
        "peakWSPD10": 1.0,
        "sumAFWA_TOTPRECIP": 1.0,
        "wgt5": 1.0,
        "wgt9": 1.0,
        "wgt13": 1.0,
        "wgt18": 1.0,
    }

    # Supply a custom dict to override weights without subclassing.
    # None → use DEFAULT_WEIGHTS.
    feature_weights: dict[str, float] | None = field(default=None)

    # Max historical records to fetch per county (matches legacy default).
    fetch_limit: int = 1000

    def find_similar(
        self,
        conn: sqlite3.Connection,
        county: str,
        query_params: dict[str, float],
        limit: int = 5,
    ) -> list[dict[str, object]]:
        """
        Find the top-N historically similar storms for a county.

        Args:
            conn:         Open database connection.
            county:       County name (case-insensitive).
            query_params: {feature: query_value} — determines which features
                          are scored.  Pass ``{"peakWSPD10": 9.2}`` to
                          replicate the legacy 1-D wind-only behavior exactly.
            limit:        Maximum number of results to return.

        Returns:
            List of storm dicts containing all WeatherData + Storm columns
            plus a ``"similarity"`` key, sorted descending by similarity.
        """
        historical = self._fetch_historical(conn, county)
        if not historical:
            return []

        weights = self._resolve_weights(query_params)
        ranked = rank_by_similarity(historical, query_params, weights)

        return [{**record, "similarity": score} for record, score in ranked[:limit]]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_weights(self, query_params: dict[str, float]) -> dict[str, float]:
        """Return per-feature weights restricted to the features in query_params."""
        base = (
            self.feature_weights
            if self.feature_weights is not None
            else self.DEFAULT_WEIGHTS
        )
        return {f: base.get(f, 1.0) for f in query_params}

    def _fetch_historical(
        self,
        conn: sqlite3.Connection,
        county: str,
    ) -> list[dict[str, object]]:
        """Fetch all WeatherData rows for a county joined with Storm metadata."""
        cursor = conn.execute(
            """
            SELECT
                w.stormCode,
                w.peakWSPD10,
                w.sumAFWA_TOTPRECIP,
                w.wgt5,
                w.wgt9,
                w.wgt13,
                w.wgt18,
                w.outage,
                s.eventDateTime,
                s.eventLength
            FROM WeatherData w
            JOIN Storm s ON s.stormCode = w.stormCode
            WHERE LOWER(w.county) = LOWER(?)
            ORDER BY s.eventDateTime DESC
            LIMIT ?
            """,
            (county, self.fetch_limit),
        )
        return [
            {
                "stormCode": int(row[0]),
                "peakWSPD10": float(row[1]),
                "sumAFWA_TOTPRECIP": float(row[2]),
                "wgt5": int(row[3]),
                "wgt9": int(row[4]),
                "wgt13": int(row[5]),
                "wgt18": int(row[6]),
                "outage": int(row[7]),
                "eventDateTime": row[8],
                "eventLength": int(row[9]),
            }
            for row in cursor.fetchall()
        ]
