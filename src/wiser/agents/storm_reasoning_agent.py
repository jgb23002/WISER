from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Any, Callable, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from wiser.openai_summarizer import load_dotenv

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_REASONING_MODEL = "gpt-4o-mini"
MAX_REASONING_ROWS = 12

_SYSTEM_PROMPT = """\
You are the WISER storm reasoning analyst.

Your job is not to write SQL. A separate, validated SQLite step already did
that. Use only the user's question, the SQL query, and the SQL result rows.

Reasoning rules:
- Start from measured evidence in the rows: county, stormCode, outage,
  peakWSPD10, precipitation, wind-threshold hours, ohLine, and normalized
  columns when present.
- outage means dispatched trouble spots / outage locations, not customers.

UNITS — every numeric column, because mislabelling one is the easiest way to
be badly wrong:
- peakWSPD10 is peak SUSTAINED wind speed at 10m height, in METRES PER SECOND.
- peakGUST10 is peak wind GUST magnitude at 10m height, in METRES PER SECOND,
  when source data provides it. It may be NULL in legacy rows.
- sumAFWA_TOTPRECIP is total precipitation in MILLIMETRES. It is never inches.
  A value like 30.67 is 30.67 mm, which is 1.21 inches — not "30.67 inches".
- wgt5, wgt9, wgt13, wgt18 are HOURS the wind GUST exceeded 5/9/13/18 m/s.
  They are gust thresholds, not hours above peakWSPD10, and they are counts of
  hours rather than a speed.
- ohLine is overhead-line exposure. It can be used as an infrastructure
  normalizer, but it is not population.
- Rows may also carry imperial companions: peak_wind_mph and precip_in. When
  quoting a value to the reader, prefer these and state the unit you used.
  Never attach an imperial unit to a metric column, or vice versa.

SUPERLATIVES — check "highest", "lowest", "most", "least" against EVERY row
you were given before making the claim. Naming the wrong row contradicts the
report shown directly above your answer.
- If the question asks "why", separate row-supported observations from
  hypotheses. Mention plausible drivers such as exposure, duration,
  precipitation, infrastructure vulnerability, and reporting/dispatch effects
  only as hypotheses unless directly present in the rows.
- If asked to normalize by population and no population field is present, say
  that population is unavailable in the current WISER schema. Do not invent
  population values. Use provided normalized fields only when they exist.
- Prefer comparing raw and normalized rankings when both are present.
- Return 2 to 4 concise bullets. Include concrete row values in the bullets.
- Do not mention unavailable tables, hidden data, or implementation details.
"""


class StormReasoningError(RuntimeError):
    """Raised when optional OpenAI reasoning cannot complete."""


@dataclass(frozen=True)
class StormReasoningResult:
    answer: str
    prompt: str
    mode: str
    openai_error: str | None = None


# Metric names the divergence helper looks for, in preference order.
RAW_IMPACT_METRICS: tuple[str, ...] = ("total_outage", "outage")
NORMALIZED_IMPACT_METRICS: tuple[str, ...] = ("outage_per_million_ohline",)


@dataclass(frozen=True)
class RankingDivergence:
    """Which row leads on a raw metric versus an exposure-normalized one.

    Normalizing outages by overhead-line exposure can reorder a ranking: the
    county with the most trouble spots is not always the worst hit per mile of
    line. Both the local reasoning fallback and the storm comparison report
    need that observation, so it is computed here once.
    """

    normalized_metric: str
    normalized_leader: dict[str, object]
    raw_metric: str | None = None
    raw_leader: dict[str, object] | None = None

    @property
    def flipped(self) -> bool:
        """True when normalizing changes which row leads."""
        if self.raw_leader is None:
            return False
        return _row_label(self.raw_leader) != _row_label(self.normalized_leader)


def normalized_ranking_divergence(
    rows: Sequence[dict[str, object]],
    *,
    raw_metrics: Sequence[str] = RAW_IMPACT_METRICS,
    normalized_metrics: Sequence[str] = NORMALIZED_IMPACT_METRICS,
    require_all: bool = False,
) -> RankingDivergence | None:
    """Find the raw and exposure-normalized leaders in a set of result rows.

    Args:
        rows: result rows to inspect.
        raw_metrics: candidate raw metric column names, in preference order.
        normalized_metrics: candidate normalized column names.
        require_all: when True, every row must carry a numeric value for the
            normalized metric. Comparing a specific pair of rows needs this —
            a claim that one of two counties is "higher when normalized" is
            not supportable if only one of them was measured.

    Returns:
        A RankingDivergence, or None when no normalized metric is available.
    """
    row_list = list(rows)
    if not row_list:
        return None

    normalized_metric = _first_present_key(row_list, normalized_metrics)
    if normalized_metric is None:
        return None
    if require_all and not all(
        _is_number(row.get(normalized_metric)) for row in row_list
    ):
        return None

    normalized_leader = max(
        row_list, key=lambda row: _as_float(row.get(normalized_metric))
    )

    raw_metric = _first_present_key(row_list, raw_metrics)
    raw_leader = (
        max(row_list, key=lambda row: _as_float(row.get(raw_metric)))
        if raw_metric is not None
        else None
    )

    return RankingDivergence(
        normalized_metric=normalized_metric,
        normalized_leader=normalized_leader,
        raw_metric=raw_metric,
        raw_leader=raw_leader,
    )


UrlOpener = Callable[..., Any]


@dataclass
class StormReasoningAgent:
    """Explain validated SQL analytics rows in storm-analysis language."""

    model: str | None = None

    def run(
        self,
        query: str,
        *,
        sql: str | None,
        rows: Sequence[dict[str, object]] | None,
        use_openai: bool = True,
        api_key: str | None = None,
        opener: UrlOpener | None = None,
    ) -> StormReasoningResult:
        row_list = list(rows or [])
        prompt = build_reasoning_prompt(query, sql=sql, rows=row_list)

        if not row_list:
            return StormReasoningResult(
                answer="No result rows were returned, so there is not enough evidence to compare counties or explain storm impacts.",
                prompt=prompt,
                mode="local analytical fallback",
            )

        if not use_openai:
            return StormReasoningResult(
                answer=reason_locally(query, row_list),
                prompt=prompt,
                mode="local analytical fallback",
            )

        load_dotenv()
        resolved_api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not resolved_api_key:
            return StormReasoningResult(
                answer=reason_locally(query, row_list),
                prompt=prompt,
                mode="local analytical fallback (OPENAI_API_KEY not set)",
            )

        resolved_model = (
            self.model
            or os.environ.get("WISER_OPENAI_REASONING_MODEL")
            or os.environ.get("WISER_OPENAI_MODEL")
            or DEFAULT_REASONING_MODEL
        )
        try:
            answer = _call_chat_llm(
                prompt=prompt,
                model=resolved_model,
                api_key=resolved_api_key,
                opener=opener,
            )
        except StormReasoningError as exc:
            return StormReasoningResult(
                answer=reason_locally(query, row_list),
                prompt=prompt,
                mode="local analytical fallback (OpenAI reasoning failed)",
                openai_error=str(exc),
            )

        return StormReasoningResult(
            answer=answer,
            prompt=prompt,
            mode=f"OpenAI reasoning ({resolved_model})",
        )


def build_reasoning_prompt(
    query: str,
    *,
    sql: str | None,
    rows: Sequence[dict[str, object]],
) -> str:
    displayed_rows = list(rows[:MAX_REASONING_ROWS])
    hidden_count = max(len(rows) - len(displayed_rows), 0)
    row_json = json.dumps(displayed_rows, indent=2, sort_keys=True, default=str)
    lines = [
        f"Question: {query}",
        "",
        "Validated SQL:",
        sql or "No SQL available; rows came from a fixed fallback analytics query.",
        "",
        f"SQL result rows shown: {len(displayed_rows)} of {len(rows)}",
        row_json,
    ]
    if hidden_count:
        lines.extend(
            [
                "",
                f"{hidden_count} additional rows were returned but omitted from this prompt.",
                "Do not make claims that require the omitted rows.",
            ]
        )
    lines.extend(
        [
            "",
            "Answer the user's analytical question. Focus on interpretation, comparison,",
            "normalization, and caveats; do not repeat the SQL.",
        ]
    )
    return "\n".join(lines)


def reason_locally(query: str, rows: Sequence[dict[str, object]]) -> str:
    """Deterministic explanation used when optional LLM reasoning is unavailable."""
    row_list = list(rows)
    if not row_list:
        return "No result rows were returned, so there is not enough evidence to analyze."

    primary_metric = _choose_primary_metric(query, row_list)
    if primary_metric is None:
        return (
            "The result rows were returned successfully, but they do not contain "
            "a numeric metric that can support a ranked storm-impact comparison."
        )

    ranked = sorted(
        row_list,
        key=lambda row: _as_float(row.get(primary_metric)),
        reverse=True,
    )
    top = ranked[0]
    top_label = _row_label(top)
    top_value = _format_value(top.get(primary_metric))

    bullets = [
        f"- Strongest measured signal: {top_label} ranks highest by {primary_metric} with {top_value}."
    ]
    if len(ranked) > 1:
        runner_up = ranked[1]
        detail = f"- Next highest is {_row_label(runner_up)} at {_format_value(runner_up.get(primary_metric))}"
        # A gap is only meaningful when both sides are measured values.
        if _is_number(top.get(primary_metric)) and _is_number(
            runner_up.get(primary_metric)
        ):
            diff = float(top[primary_metric]) - float(runner_up[primary_metric])
            detail += f", a gap of {_format_value(diff)}"
        bullets.append(f"{detail}.")

    outage_metric = _first_present_key(row_list, ("total_outage", "outage"))
    if (
        primary_metric in {"max_peak_wind", "avg_peak_wind", "peakWSPD10"}
        and outage_metric is not None
    ):
        outage_top = max(
            row_list,
            key=lambda row: _as_float(row.get(outage_metric)),
        )
        if _row_label(outage_top) != top_label:
            bullets.append(
                f"- Wind and outage rankings diverge: {_row_label(outage_top)} leads by {outage_metric} with {_format_value(outage_top.get(outage_metric))}, not {top_label}."
            )

    comparison = _lower_wind_higher_outage_observation(row_list)
    if comparison and _asks_why(query):
        higher, lower = comparison
        detail = (
            f"- The rows support a non-linear outage relationship: {_row_label(higher)} has "
            f"{_format_value(higher.get('outage'))} outages with "
            f"{_format_value(higher.get('peakWSPD10'))} m/s wind, while {_row_label(lower)} has "
            f"{_format_value(lower.get('outage'))} outages despite "
            f"{_format_value(lower.get('peakWSPD10'))} m/s wind."
        )
        if _is_number(higher.get("ohLine")) and _is_number(lower.get("ohLine")):
            detail += (
                f" Exposure may matter: ohLine is {_format_value(higher.get('ohLine'))} "
                f"versus {_format_value(lower.get('ohLine'))}."
            )
        # require_all: naming one of the pair as "higher when normalized" is
        # only supportable when both sides were measured.
        divergence = normalized_ranking_divergence([higher, lower], require_all=True)
        if divergence is not None:
            normalized_top = divergence.normalized_leader
            detail += (
                f" Normalized by ohLine, {_row_label(normalized_top)} is higher at "
                f"{_format_value(normalized_top.get(divergence.normalized_metric))}, "
                "so the exposure-adjusted interpretation differs from the raw outage ranking."
            )
        bullets.append(detail)
    elif _has_columns(row_list, "outage", "peakWSPD10"):
        bullets.append(
            "- Wind is one driver, but the rows should be read alongside outage, precipitation, threshold-hour, and exposure fields because outages do not always rise one-for-one with peak wind."
        )

    lower_query = query.lower()
    if "population" in lower_query:
        bullets.append(
            "- Population is not present in the current WISER schema, so a per-capita answer would need an added population table or input values."
        )
    elif "normal" in lower_query or "per" in lower_query:
        divergence = normalized_ranking_divergence(row_list)
        if divergence is not None:
            normalized_key = divergence.normalized_metric
            normalized_top = divergence.normalized_leader
            bullets.append(
                f"- Normalized view: {_row_label(normalized_top)} is highest by {normalized_key} at {_format_value(normalized_top.get(normalized_key))}, which can change the interpretation from raw outage totals."
            )

    bullets.append(
        "- Caveat: this explanation uses only returned database fields; causal claims need additional evidence such as population, vegetation, asset condition, and restoration logs."
    )
    return "\n".join(_dedupe(bullets))


def _call_chat_llm(
    *,
    prompt: str,
    model: str,
    api_key: str,
    timeout: int = 30,
    opener: UrlOpener | None = None,
) -> str:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }
    _add_generation_options(body, model, max_tokens=700, temperature=0.2)
    data = json.dumps(body).encode("utf-8")
    req = Request(
        OPENAI_CHAT_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        open_url = opener or urlopen
        with open_url(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise StormReasoningError(
            f"OpenAI request failed with HTTP {exc.code}: {detail}"
        ) from exc
    except (OSError, URLError, json.JSONDecodeError) as exc:
        raise StormReasoningError(f"OpenAI request failed: {exc}") from exc

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise StormReasoningError(f"Unexpected OpenAI response shape: {exc}") from exc
    if not isinstance(content, str) or not content.strip():
        raise StormReasoningError("OpenAI returned an empty reasoning response.")
    return content.strip()


def _add_generation_options(
    body: dict[str, Any],
    model: str,
    *,
    max_tokens: int,
    temperature: float,
) -> None:
    if _uses_completion_token_limit(model):
        body["max_completion_tokens"] = max_tokens
        return
    body["max_tokens"] = max_tokens
    body["temperature"] = temperature


def _uses_completion_token_limit(model: str) -> bool:
    normalized = model.lower()
    return normalized.startswith(("gpt-5", "o1", "o3", "o4"))


def _choose_primary_metric(
    query: str,
    rows: Sequence[dict[str, object]],
) -> str | None:
    lower_query = query.lower()
    candidates: tuple[str, ...]
    if (
        "wind" in lower_query
        and any(token in lower_query for token in ("highest", "maximum", "max", "peak"))
        and not ("why" in lower_query or "despite" in lower_query)
    ):
        candidates = ("max_peak_wind", "avg_peak_wind", "peakWSPD10")
    elif "normal" in lower_query or "per" in lower_query:
        candidates = (
            "outage_per_million_ohline",
            "outage_per_population",
            "outage_per_100k_population",
            "total_outage",
            "outage",
        )
    elif "wind" in lower_query and "outage" not in lower_query:
        candidates = ("max_peak_wind", "avg_peak_wind", "peakWSPD10")
    elif "average" in lower_query or "avg" in lower_query:
        candidates = ("avg_outage", "total_outage", "outage")
    else:
        candidates = (
            "total_outage",
            "outage",
            "peak_outage",
            "avg_outage",
            "outage_per_million_ohline",
            "max_peak_wind",
            "avg_peak_wind",
            "peakWSPD10",
        )
    return _first_present_key(rows, candidates)


def _first_present_key(
    rows: Sequence[dict[str, object]],
    candidates: Sequence[str],
) -> str | None:
    for key in candidates:
        if any(_is_number(row.get(key)) for row in rows):
            return key
    return None


def _row_label(row: dict[str, object]) -> str:
    parts: list[str] = []
    if row.get("county") is not None:
        parts.append(str(row["county"]))
    if row.get("stormCode") is not None:
        parts.append(f"storm {row['stormCode']}")
    return " / ".join(parts) if parts else "the top row"


def _lower_wind_higher_outage_observation(
    rows: Sequence[dict[str, object]],
) -> tuple[dict[str, object], dict[str, object]] | None:
    if not _has_columns(rows, "outage", "peakWSPD10"):
        return None
    for candidate in sorted(
        rows, key=lambda row: _as_float(row.get("outage"), 0.0), reverse=True
    ):
        for other in rows:
            if candidate is other:
                continue
            if (
                _is_number(candidate.get("outage"))
                and _is_number(other.get("outage"))
                and _is_number(candidate.get("peakWSPD10"))
                and _is_number(other.get("peakWSPD10"))
                and float(candidate["outage"]) > float(other["outage"])
                and float(candidate["peakWSPD10"]) < float(other["peakWSPD10"])
            ):
                return candidate, other
    return None


def _asks_why(query: str) -> bool:
    lower = query.lower()
    return "why" in lower or "despite" in lower or "explain" in lower


def _has_columns(rows: Sequence[dict[str, object]], *columns: str) -> bool:
    return bool(rows) and all(column in rows[0] for column in columns)


def _format_value(value: object) -> str:
    if value is None:
        return "not reported"
    if not _is_number(value):
        return str(value)
    number = float(value)
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}"


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def _as_float(value: object, default: float = float("-inf")) -> float:
    """Ordering key tolerant of SQL NULLs, which LEFT JOIN aggregates can emit."""
    return float(value) if _is_number(value) else default


def _dedupe(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        if item in seen:
            continue
        unique.append(item)
        seen.add(item)
    return unique
