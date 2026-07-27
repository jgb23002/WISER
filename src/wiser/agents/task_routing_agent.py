from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from wiser.cli_helpers import FEATURE_ALIASES


class Route(str, Enum):
    DATA_RETRIEVAL = "data_retrieval"
    RAG_KNOWLEDGE = "rag_knowledge"
    SIMILARITY_SEARCH = "similarity_search"
    WEATHER_ANALYTICS = "weather_analytics"
    STORM_SUMMARY = "storm_summary"
    STORM_RANKING = "storm_ranking"
    STORM_COMPARISON = "storm_comparison"
    UNKNOWN = "unknown"


@dataclass
class RouteDecision:
    route: Route
    reason: str


class TaskRoutingAgent:
    DATA_KEYWORDS = {
        "build",
        "rebuild",
        "refresh",
        "load",
        "retrieve",
        "raw",
        "csv",
        "schema",
        "table",
        "database",
        "db",
        "sqlite",
        "ingest",
        "normalize",
    }
    DATA_ACTION_KEYWORDS = {
        "build",
        "rebuild",
        "refresh",
        "load",
        "raw",
        "csv",
        "ingest",
        "normalize",
    }
    DATA_METADATA_KEYWORDS = {
        "schema",
        "table",
        "database",
        "db",
        "sqlite",
    }
    # A data-management action only counts as ETL when it acts on a named data
    # artifact; "biggest outage load" is an analytics phrase, not a load command.
    DATA_ARTIFACT_KEYWORDS = {
        "csv",
        "database",
        "db",
        "sqlite",
        "schema",
        "table",
        "tables",
        "source data",
        "raw data",
        "raw",
    }
    ANALYTICS_KEYWORDS = {
        "analyze",
        "analysis",
        "affected",
        "effect",
        "effects",
        "exposure",
        "predict",
        "normalize",
        "normalized",
        "population",
        "per capita",
        "per-capita",
        "outage",
        "storm",
        "weather",
        "compare",
        "trend",
        "top",
        "county",
        "counties",
    }
    RAG_KEYWORDS = {
        "algorithm",
        "context",
        "describe",
        "docs",
        "document",
        "documentation",
        "explain",
        "how does",
        "hurricane",
        "impact",
        "knowledge",
        "method",
        "ncei",
        "nhc",
        "prompt",
        "rag",
        "reference",
        "steps",
        "summarize",
    }
    # Bare "similarity" is intentionally excluded from RAG_KEYWORDS so numeric
    # queries like "find storm similarity wind=9.2" are not swallowed by RAG.
    # Explanation queries still match via "explain", "how does", "algorithm", etc.
    SIMILARITY_KEYWORDS = {
        "similar",
        "find similar",
        "similar to",
        "rank storms",
    }
    # Storm-scoped intents are gated on an explicit storm IDENTIFIER, not on a
    # verb. "summarize", "describe", "impact", and "how does" already belong to
    # RAG, and "rank storms" to similarity; widening those verbs would regress
    # rag_005/rag_008/similarity_002. Requiring an identifier keeps the new
    # intents narrow enough to sit ahead of both.
    STORM_SUMMARY_CUES = {
        "summary",
        "summarize",
        "summarise",
        "overview",
        "report",
        "describe",
        "details",
        "breakdown",
        "profile",
        "what happened",
        "tell me about",
    }
    STORM_RANKING_CUES = {
        "rank",
        "ranks",
        "ranked",
        "ranking",
        "percentile",
        "how bad",
        "compare to other storms",
        "on record",
        "historically",
        "out of",
    }
    STORM_COMPARISON_CUES = {
        "compare",
        "comparison",
        "vs",
        "versus",
        "against",
        "difference between",
        "differ",
    }

    # ISO dates only at the routing layer. Looser date forms ("8/4/2020",
    # "August 4 2020") are the handler's job, where a parse failure can
    # produce a helpful notice; loosening the router's pattern is what
    # creates false positives.
    _STORM_CODE_PATTERN = re.compile(r"\b(\d{10})\b")
    _STORM_DATE_PATTERN = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

    # Matches wind=, precip=, wgt5=, etc. — same aliases the CLI parser accepts.
    _NUMERIC_PARAM_PATTERN = re.compile(
        rf"\b(?:{'|'.join(re.escape(alias) for alias in FEATURE_ALIASES)})\s*=",
        re.IGNORECASE,
    )

    def route(self, query: str) -> RouteDecision:
        text = query.lower()
        # Storm-scoped intents run first, ahead of RAG and similarity, because
        # their verbs are already owned by those routes. The identifier gate
        # inside keeps them from firing on anything that does not name a storm.
        storm_decision = self._storm_intent(query, text)
        if storm_decision is not None:
            return storm_decision
        # RAG first: explanation queries match via "explain", "how does",
        # "algorithm", etc.  Numeric param queries are excluded so
        # "find storm similarity wind=9.2" reaches SIMILARITY_SEARCH below.
        if any(keyword in text for keyword in self.RAG_KEYWORDS) and not (
            self._NUMERIC_PARAM_PATTERN.search(query)
        ):
            return RouteDecision(
                route=Route.RAG_KNOWLEDGE,
                reason="Matched documentation/RAG knowledge keywords.",
            )
        # SIMILARITY_SEARCH before ANALYTICS: keyword or numeric-param match.
        if any(keyword in text for keyword in self.SIMILARITY_KEYWORDS) or (
            self._NUMERIC_PARAM_PATTERN.search(query)
        ):
            return RouteDecision(
                route=Route.SIMILARITY_SEARCH,
                reason="Matched similarity search keywords.",
            )
        # Explicit data-management requests can contain domain words such as
        # "storm" or "weather"; keep build/load/normalize/CSV commands out of
        # the analytics path.
        if self._looks_like_data_management(text):
            return RouteDecision(
                route=Route.DATA_RETRIEVAL,
                reason="Matched data/database-oriented keywords.",
            )
        # Analytics before data: "top counties from database" is analytics,
        # not a data-load operation.
        if any(keyword in text for keyword in self.ANALYTICS_KEYWORDS):
            return RouteDecision(
                route=Route.WEATHER_ANALYTICS,
                reason="Matched weather/outage analytics keywords.",
            )
        if any(keyword in text for keyword in self.DATA_KEYWORDS):
            return RouteDecision(
                route=Route.DATA_RETRIEVAL,
                reason="Matched data/database-oriented keywords.",
            )
        return RouteDecision(
            route=Route.UNKNOWN,
            reason="No strong keyword match; needs manual decision.",
        )

    @staticmethod
    def _matches_word(text: str, keywords) -> bool:
        """Whole-word match so "crawl" does not read as "raw"."""
        return any(
            re.search(rf"\b{re.escape(keyword)}\b", text) for keyword in keywords
        )

    @classmethod
    def _storm_identifiers(cls, query: str) -> tuple[str, ...]:
        """Distinct storm codes and ISO dates named in the query, in order."""
        found = [match.group(1) for match in cls._STORM_CODE_PATTERN.finditer(query)]
        found.extend(
            match.group(1) for match in cls._STORM_DATE_PATTERN.finditer(query)
        )
        seen: set[str] = set()
        ordered: list[str] = []
        for identifier in found:
            if identifier not in seen:
                seen.add(identifier)
                ordered.append(identifier)
        return tuple(ordered)

    def _storm_intent(self, query: str, text: str) -> RouteDecision | None:
        """Storm-scoped route for this query, or None to fall through.

        Ranking is checked before summary because "give me a summary of how
        storm X ranks" is a ranking request; summary is the general fallback.
        """
        # Numeric feature parameters and "similar" phrasing belong to the
        # similarity search, which keeps priority over the storm intents.
        if self._NUMERIC_PARAM_PATTERN.search(query):
            return None
        if self._matches_word(text, ("similar",)):
            return None

        identifiers = self._storm_identifiers(query)
        if not identifiers:
            return None

        has_comparison_cue = self._matches_word(text, self.STORM_COMPARISON_CUES)

        if len(identifiers) >= 2 and has_comparison_cue:
            return RouteDecision(
                route=Route.STORM_COMPARISON,
                reason=(
                    "Matched storm-scoped comparison intent "
                    "(two storm identifiers + comparison cue)."
                ),
            )

        if len(identifiers) == 1:
            if self._matches_word(text, self.STORM_RANKING_CUES):
                return RouteDecision(
                    route=Route.STORM_RANKING,
                    reason=(
                        "Matched storm-scoped ranking intent "
                        "(storm identifier + ranking cue)."
                    ),
                )
            if self._matches_word(text, self.STORM_SUMMARY_CUES):
                return RouteDecision(
                    route=Route.STORM_SUMMARY,
                    reason=(
                        "Matched storm-scoped summary intent "
                        "(storm identifier + summary cue)."
                    ),
                )
            # A bare identifier alongside the word "storm" is a summary
            # request — but this is the loosest rule here, so it yields to any
            # competing signal. A comparison cue means a comparison missing its
            # second storm, not a summary; and "load the raw storm csv
            # 2020080406" is an ETL command that merely mentions a storm.
            # Documentation verbs deliberately do NOT block it: naming a
            # specific storm outranks "explain" (fixture routing_guard_002).
            if (
                "storm" in text
                and not has_comparison_cue
                and not self._looks_like_data_management(text)
            ):
                return RouteDecision(
                    route=Route.STORM_SUMMARY,
                    reason=(
                        "Matched bare storm identifier; "
                        "defaulting to the storm summary intent."
                    ),
                )

        return None

    def _looks_like_data_management(self, text: str) -> bool:
        # Naming the database as a source does not make a normalization question
        # ETL; only raw-file wording does. Keeps "normalize outages by population
        # using the database" in analytics.
        if (
            "normalize" in text
            and self._matches_word(
                text,
                (
                    "outage",
                    "outages",
                    "population",
                    "per capita",
                    "per-capita",
                    "county",
                    "counties",
                ),
            )
            and not self._matches_word(text, ("csv", "raw", "source data"))
        ):
            return False
        if self._matches_word(text, self.DATA_ACTION_KEYWORDS) and self._matches_word(
            text, self.DATA_ARTIFACT_KEYWORDS
        ):
            return True
        return "retrieve" in text and self._matches_word(
            text, self.DATA_METADATA_KEYWORDS
        )
