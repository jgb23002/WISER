from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from wiser.conversation import MAX_HISTORY_TURNS, ConversationTurn
from wiser.openai_summarizer import load_dotenv

try:
    import sqlglot
    import sqlglot.errors
    _SQLGLOT_AVAILABLE = True
except ImportError:
    _SQLGLOT_AVAILABLE = False

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_SQL_MODEL = "gpt-4o-mini"
DEFAULT_ROW_LIMIT = 10

_WRITE_PATTERN = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|COPY|CALL|VACUUM)\b",
    re.IGNORECASE,
)
_SELECT_INTO_PATTERN = re.compile(
    r"^\s*(?:WITH\b.*?)?\bSELECT\b.*?\bINTO\b",
    re.IGNORECASE | re.DOTALL,
)
_WRITE_KEYWORD_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|COPY|CALL|VACUUM)\b",
    re.IGNORECASE,
)

_SYSTEM_PROMPT = """\
You are a PostgreSQL expert for the WISER weather analytics system.
Generate a valid PostgreSQL SELECT query for the user's question.

RULES:
- PostgreSQL only. Use LOWER() not ILIKE. No double-quoted column names:
  PostgreSQL folds unquoted identifiers to lowercase, so stormCode and
  stormcode are the same column, but "stormCode" is a different one that
  does not exist.
- A SELECT alias may be used in GROUP BY and ORDER BY, but NOT in WHERE or
  HAVING. Repeat the expression there: HAVING COUNT(DISTINCT county) > 1.
- ROUND(x, 2) requires a numeric argument. Wind, precipitation and derived
  ratios are double precision, so cast first: ROUND(AVG(x)::numeric, 2).
- Valid counties: Fairfield, Hartford, Litchfield, Middlesex, New Haven, New London, Tolland, Windham.
  Always compare county names with LOWER(): WHERE LOWER(county) = 'fairfield'
- stormCode is YYYYMMDDHH. For year filtering use EXTRACT(YEAR FROM eventDateTime)
  via JOIN with Storm; it yields a number, so compare against 2012, not '2012'.
  Do not use SUBSTR on stormCode.
- wgt5/wgt9/wgt13/wgt18 = hours wind exceeded that m/s threshold.
  "storms above 13 m/s" maps to wgt13 > 0. NOT peakWSPD10 > 13.
- peakWSPD10 = actual peak wind speed at 10m height. "fastest storm" = MAX(peakWSPD10).
- peakGUST10 = peak wind gust magnitude at 10m height, when source data provides it.
  It may be NULL in legacy rows. For peak gust magnitude questions, use peakGUST10;
  for gust exposure / hours above a threshold, use wgt5/wgt9/wgt13/wgt18.
- outage = number of trouble spots dispatched. "worst" = MAX or SUM(outage).
- "affected most" means highest outage impact unless the question names another metric.
- County.ohLine = overhead-line exposure. It can be used to normalize outages by
  infrastructure exposure: outage per 1,000,000 ohLine = outage * 1000000.0 / ohLine.
- Population is not present in the current schema. NEVER invent population columns
  or values. If asked for population-normalized results, return outage totals and
  available ohLine exposure-normalized values so the reasoning layer can explain
  the missing population data.
- JOIN WeatherData to Storm whenever eventDateTime or eventLength is needed.
- JOIN WeatherData to County whenever ohLine or exposure-normalized values are needed.
- LIMIT precedence: a number in the question ("top 5", "just 3") ALWAYS wins;
  otherwise, on a follow-up, keep the previous SQL's LIMIT; otherwise use the
  default row limit provided with the question.
- Return ONLY the raw SQL query. No markdown fences, no backticks, no prose,
  no "SQL:" prefix, no "Answer:" suffix.
- NEVER generate INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, REPLACE, or TRUNCATE.

CONVERSATION RULES (apply when prior Question/SQL/Answer turns precede the question):
- A follow-up refines the MOST RECENT query: start from the previous SQL and change
  only what the user asks to change.
- Keep every prior constraint — filters, joins, grouping, ORDER BY, LIMIT — unless the
  follow-up explicitly changes or removes it.
- Swaps replace in place: "what about fairfield?" changes only the county filter;
  "order by precipitation" changes the ORDER BY column and updates the SELECT
  list to show the new metric instead of the old one.
- Exclusions accumulate: "also exclude X" extends the existing exclusion,
  e.g. LOWER(county) NOT IN ('hartford', 'fairfield').
- "statewide" or "all counties" removes the county filter and keeps everything else.
- "those", "them", "that one", "there" refer to the previous query's results: reuse the
  previous SQL's filters/ordering/limit — or nest the previous SQL as a subquery — so
  the new query targets the same rows.
- A question about a new, unrelated topic is NOT a follow-up: answer it fresh and
  carry nothing over from earlier turns.

EXAMPLES:
Q: What is the total outage count for Fairfield county?
A: SELECT SUM(outage) AS total_outage FROM WeatherData WHERE LOWER(county) = 'fairfield';

Q: Which storms had wind above 9 m/s in Hartford?
A: SELECT w.stormCode, w.peakWSPD10, w.outage FROM WeatherData w WHERE LOWER(w.county) = 'hartford' AND w.wgt9 > 0 ORDER BY w.peakWSPD10 DESC LIMIT 10;

Q: What were the top 5 storms by outage in New Haven?
A: SELECT w.stormCode, SUM(w.outage) AS total_outage FROM WeatherData w WHERE LOWER(w.county) = 'new haven' GROUP BY w.stormCode ORDER BY total_outage DESC LIMIT 5;

Q: What was the outage count for storm 2012102806?
A: SELECT w.county, w.outage FROM WeatherData w JOIN Storm s ON s.stormCode = w.stormCode WHERE s.stormCode = 2012102806;

Q: How many storms happened between 2010 and 2015?
A: SELECT COUNT(DISTINCT s.stormCode) AS storm_count FROM Storm s WHERE EXTRACT(YEAR FROM s.eventDateTime) BETWEEN 2010 AND 2015;

Q: Which county had more outages, Fairfield or Hartford?
A: SELECT county, SUM(outage) AS total_outage FROM WeatherData WHERE LOWER(county) IN ('fairfield', 'hartford') GROUP BY county ORDER BY total_outage DESC;

Q: Which storms hit more than one county?
A: SELECT stormCode, COUNT(DISTINCT county) AS county_count FROM WeatherData GROUP BY stormCode HAVING COUNT(DISTINCT county) > 1 ORDER BY county_count DESC LIMIT 10;

Q: Which storms lasted longer than 48 hours?
A: SELECT s.stormCode, s.eventDateTime, s.eventLength FROM Storm s WHERE s.eventLength > 48 ORDER BY s.eventLength DESC LIMIT 10;

Q: Which county was affected the most overall?
A: SELECT w.county, SUM(w.outage) AS total_outage, ROUND(AVG(w.peakWSPD10)::numeric, 2) AS avg_peak_wind, c.ohLine, ROUND((SUM(w.outage) * 1000000.0 / c.ohLine)::numeric, 2) AS outage_per_million_ohline FROM WeatherData w JOIN County c ON LOWER(c.county) = LOWER(w.county) GROUP BY w.county, c.ohLine ORDER BY total_outage DESC LIMIT 10;

Q: Normalize county outages by overhead line exposure.
A: SELECT w.county, SUM(w.outage) AS total_outage, c.ohLine, ROUND((SUM(w.outage) * 1000000.0 / c.ohLine)::numeric, 2) AS outage_per_million_ohline FROM WeatherData w JOIN County c ON LOWER(c.county) = LOWER(w.county) GROUP BY w.county, c.ohLine ORDER BY outage_per_million_ohline DESC LIMIT 10;

Q: For storm 2020080406, why did Hartford have more outages than New Haven despite lower wind?
A: SELECT w.county, w.stormCode, w.outage, w.peakWSPD10, w.sumAFWA_TOTPRECIP, w.wgt5, w.wgt9, w.wgt13, w.wgt18, c.ohLine, ROUND((w.outage * 1000000.0 / c.ohLine)::numeric, 2) AS outage_per_million_ohline FROM WeatherData w JOIN County c ON LOWER(c.county) = LOWER(w.county) WHERE w.stormCode = 2020080406 AND LOWER(w.county) IN ('hartford', 'new haven') ORDER BY w.outage DESC;

FOLLOW-UP EXAMPLE (one conversation; each answer refines the previous SQL):
Q: What were the top 5 storms by precipitation in Hartford?
A: SELECT w.stormCode, w.sumAFWA_TOTPRECIP FROM WeatherData w WHERE LOWER(w.county) = 'hartford' ORDER BY w.sumAFWA_TOTPRECIP DESC LIMIT 5;
Q: only storms after 2012
A: SELECT w.stormCode, w.sumAFWA_TOTPRECIP FROM WeatherData w JOIN Storm s ON s.stormCode = w.stormCode WHERE LOWER(w.county) = 'hartford' AND EXTRACT(YEAR FROM s.eventDateTime) > 2012 ORDER BY w.sumAFWA_TOTPRECIP DESC LIMIT 5;
Q: what about fairfield county
A: SELECT w.stormCode, w.sumAFWA_TOTPRECIP FROM WeatherData w JOIN Storm s ON s.stormCode = w.stormCode WHERE LOWER(w.county) = 'fairfield' AND EXTRACT(YEAR FROM s.eventDateTime) > 2012 ORDER BY w.sumAFWA_TOTPRECIP DESC LIMIT 5;
Q: just the top 3
A: SELECT w.stormCode, w.sumAFWA_TOTPRECIP FROM WeatherData w JOIN Storm s ON s.stormCode = w.stormCode WHERE LOWER(w.county) = 'fairfield' AND EXTRACT(YEAR FROM s.eventDateTime) > 2012 ORDER BY w.sumAFWA_TOTPRECIP DESC LIMIT 3;
"""


class SqlValidationError(ValueError):
    """Raised when the LLM-generated SQL fails safety or syntax validation."""


@dataclass
class SqlAiAgent:
    """Translates natural-language questions into PostgreSQL SELECT queries via an LLM.

    Stateless: no connection is stored; pass conn to run() each call.
    The schema DDL is loaded once at __post_init__ from schema_path.
    """

    model: str | None = None
    dry_run: bool = False
    schema_path: Path = field(default_factory=lambda: DEFAULT_SCHEMA_PATH)

    _schema_ddl: str = field(init=False, repr=False, default="")

    def __post_init__(self) -> None:
        self._schema_ddl = _load_schema_ddl(self.schema_path)

    def run(
        self,
        query: str,
        conn: psycopg.Connection,
        *,
        limit: int = DEFAULT_ROW_LIMIT,
        history: Sequence[ConversationTurn] | None = None,
    ) -> dict[str, Any]:
        """Run the SQL AI pipeline for a natural-language query.

        Args:
            query: Natural-language question.
            conn:  Open connection to the target database (used to resolve
                   the file path for read-only execution).
            limit: Default row limit hinted to the LLM; the user's question
                   can still override it explicitly.
            history: Prior conversation turns (oldest first). Only the last
                   MAX_HISTORY_TURNS reach the LLM prompt. The agent stays
                   stateless: the caller owns and accumulates the history.

        Returns:
            {
                "sql":     str   — generated SQL (or full prompt in dry_run mode),
                "rows":    list  — result rows as dicts (empty in dry_run),
                "answer":  str   — one-sentence plain-English answer,
                "dry_run": bool  — True if the LLM call was skipped,
            }
        """
        messages = _build_messages(self._schema_ddl, query, limit=limit, history=history)

        if self.dry_run:
            full_prompt = "\n\n".join(
                f"[{message['role'].upper()}]\n{message['content']}" for message in messages
            )
            return {"sql": full_prompt, "rows": [], "answer": "", "dry_run": True}

        load_dotenv()
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise SqlValidationError("OPENAI_API_KEY is not configured.")

        resolved_model = self.model or os.environ.get("WISER_OPENAI_MODEL", DEFAULT_SQL_MODEL)

        raw = _call_chat_llm(
            messages=messages,
            model=resolved_model,
            api_key=api_key,
        )
        if not raw or not raw.strip():
            raise SqlValidationError("LLM returned an empty response.")

        sql = _clean_sql(raw)
        _validate_sql(sql)

        rows = _execute_ro(sql, conn)
        answer = _make_answer(rows)

        return {"sql": sql, "rows": rows, "answer": answer, "dry_run": False}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_schema_ddl(schema_path: Path) -> str:
    """Extract only CREATE TABLE / CREATE INDEX blocks from the schema file."""
    raw = schema_path.read_text(encoding="utf-8")
    blocks = re.findall(
        r"(CREATE\s+(?:TABLE|INDEX)\b[^;]+;)",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return "\n\n".join(blocks)


def _build_user_message(
    schema_ddl: str,
    query: str,
    *,
    limit: int = DEFAULT_ROW_LIMIT,
    follow_up: bool = False,
) -> str:
    # On follow-ups a bare "Default row limit: N" line reliably overpowers the
    # carried LIMIT (observed with gpt-4o-mini even when annotated), so the
    # numeric anchor is only stated as the no-previous-LIMIT fallback.
    if follow_up:
        limit_line = (
            "Row limit: this is a follow-up — keep the previous SQL's LIMIT"
            " unless the question changes it. If the previous SQL had no"
            f" LIMIT, default to {limit}."
        )
    else:
        limit_line = f"Default row limit: {limit}"
    return (
        f"Schema:\n{schema_ddl}\n\n"
        f"{limit_line}\n\n"
        f"Question: {query}"
    )


def _build_messages(
    schema_ddl: str,
    query: str,
    *,
    limit: int = DEFAULT_ROW_LIMIT,
    history: Sequence[ConversationTurn] | None = None,
) -> list[dict[str, str]]:
    """Build the chat message list: system, prior turns, then the question.

    The schema and row limit are sent only in the final user message — they
    are identical every turn, so repeating them per historical turn would
    multiply tokens for no information gain.
    """
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    trimmed = list(history or ())[-MAX_HISTORY_TURNS:]
    for turn in trimmed:
        messages.append({"role": "user", "content": f"Question: {turn.question}"})
        messages.append(
            {"role": "assistant", "content": f"SQL: {turn.sql}\nAnswer: {turn.answer}"}
        )
    messages.append(
        {
            "role": "user",
            "content": _build_user_message(
                schema_ddl, query, limit=limit, follow_up=bool(trimmed)
            ),
        }
    )
    return messages


def _call_chat_llm(
    *,
    messages: list[dict[str, str]],
    model: str,
    api_key: str,
    timeout: int = 30,
) -> str:
    body = {
        "model": model,
        "messages": messages,
    }
    _add_generation_options(body, model, max_tokens=512, temperature=0)
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
        with urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise SqlValidationError(
            f"OpenAI request failed with HTTP {exc.code}: {detail}"
        ) from exc
    except (OSError, URLError, json.JSONDecodeError) as exc:
        raise SqlValidationError(f"OpenAI request failed: {exc}") from exc

    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SqlValidationError(f"Unexpected OpenAI response shape: {exc}") from exc


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


def _clean_sql(raw: str) -> str:
    text = raw.strip()
    # With history in the prompt the model sometimes echoes the transcript
    # format ("SQL: ...\nAnswer: ...") despite instructions; unwrap the label
    # both outside and inside any markdown fence.
    text = re.sub(r"^SQL:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^```(?:sql)?\s*\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n?```\s*$", "", text)
    text = re.sub(r"^SQL:\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\n\s*Answer:.*\Z", "", text, flags=re.IGNORECASE | re.DOTALL)
    return text.strip()


def _validate_sql(sql: str) -> None:
    if _WRITE_PATTERN.match(sql):
        raise SqlValidationError(
            f"Non-SELECT statement is not allowed: {sql[:60]!r}"
        )

    # PostgreSQL's SELECT ... INTO creates a table. It starts with SELECT, so
    # the leading-keyword check above misses it, and sqlglot models it as a
    # plain exp.Select so the isinstance check below passes it too. This
    # syntax did not parse under the old sqlite dialect. The read-only
    # transaction in _execute_ro would still refuse it, but that is the last
    # line of defence rather than the only one.
    if _SELECT_INTO_PATTERN.search(sql):
        raise SqlValidationError(
            "SELECT ... INTO creates a table; only read-only SELECT is allowed."
        )

    if not _SQLGLOT_AVAILABLE:
        if not re.match(r"^\s*(?:SELECT|WITH)\b", sql, re.IGNORECASE):
            raise SqlValidationError(
                "Generated SQL does not begin with SELECT or WITH. "
                "Install sqlglot for full syntax validation: pip install sqlglot"
            )
        # A semicolon in a string literal can false-positive here; without a
        # parser we reject conservatively rather than risk a second statement.
        if ";" in sql.strip().rstrip(";"):
            raise SqlValidationError("Only a single SQL statement is allowed.")
        # PostgreSQL allows WITH before INSERT/UPDATE/DELETE, so a CTE prefix
        # is not proof of a read-only query — scan for write keywords too.
        if re.match(r"^\s*WITH\b", sql, re.IGNORECASE) and _WRITE_KEYWORD_PATTERN.search(sql):
            raise SqlValidationError(
                "CTE contains a write keyword; only read-only SELECT is allowed."
            )
        return

    try:
        statements = sqlglot.parse(
            sql,
            dialect="postgres",
            error_level=sqlglot.errors.ErrorLevel.RAISE,
        )
    except sqlglot.errors.ParseError as exc:
        raise SqlValidationError(f"SQL syntax error: {exc}") from exc

    # A trailing semicolon can parse as an empty (None) statement.
    statements = [statement for statement in statements if statement is not None]
    if not statements:
        raise SqlValidationError("sqlglot could not parse the generated SQL.")

    if len(statements) > 1:
        raise SqlValidationError("Only a single SQL statement is allowed.")

    if not isinstance(statements[0], sqlglot.exp.Select):
        raise SqlValidationError(
            f"Expected a SELECT statement, got {type(statements[0]).__name__}."
        )


def _execute_ro(sql: str, conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Run LLM-generated SQL in a server-enforced read-only transaction.

    SQLite used a second connection opened on a ``file:...?mode=ro`` URI.
    PostgreSQL has no file to reopen, so the guarantee comes from the server
    instead: inside the transaction below any write is rejected with
    ``ReadOnlySqlTransaction`` even if ``_validate_sql`` were bypassed.

    Keys arrive folded to lowercase (``stormCode`` selects ``stormcode``),
    which is what the ground-truth fixture records.
    """
    with conn.transaction():
        conn.execute("SET LOCAL transaction_read_only = on")
        with conn.cursor(row_factory=dict_row) as cursor:
            cursor.execute(sql)
            return [dict(row) for row in cursor.fetchall()]


_ANSWER_PREVIEW_ROWS = 3


def _make_answer(rows: list[dict[str, Any]]) -> str:
    """One-line answer for the user; also feeds conversation history, so it
    includes concrete values — follow-ups like "when did the first one
    happen?" can only be resolved if prior results name their rows."""
    if not rows:
        return "No results found."
    if len(rows) == 1 and len(rows[0]) == 1:
        key, val = next(iter(rows[0].items()))
        return f"The {key} is {val}."
    preview = "; ".join(
        ", ".join(f"{key}={value}" for key, value in row.items())
        for row in rows[:_ANSWER_PREVIEW_ROWS]
    )
    if len(rows) == 1:
        return f"Found 1 matching record: {preview}."
    if len(rows) <= _ANSWER_PREVIEW_ROWS:
        return f"Found {len(rows)} results: {preview}."
    return f"Found {len(rows)} results; top {_ANSWER_PREVIEW_ROWS}: {preview}."
