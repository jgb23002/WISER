from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
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
    r"^\s*(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|DETACH)\b",
    re.IGNORECASE,
)
_WRITE_KEYWORD_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|DETACH|PRAGMA)\b",
    re.IGNORECASE,
)

_SYSTEM_PROMPT = """\
You are a SQLite expert for the WISER weather analytics system.
Generate a valid SQLite SELECT query for the user's question.

RULES:
- SQLite only. Use LOWER() not ILIKE. No double-quoted column names.
- Valid counties: Fairfield, Hartford, Litchfield, Middlesex, New Haven, New London, Tolland, Windham.
  Always compare county names with LOWER(): WHERE LOWER(county) = 'fairfield'
- stormCode is YYYYMMDDHH. For year filtering use strftime('%Y', eventDateTime) via JOIN with Storm.
  Do not use SUBSTR on stormCode.
- wgt5/wgt9/wgt13/wgt18 = hours wind exceeded that m/s threshold.
  "storms above 13 m/s" maps to wgt13 > 0. NOT peakWSPD10 > 13.
- peakWSPD10 = actual peak wind speed at 10m height. "fastest storm" = MAX(peakWSPD10).
- outage = number of trouble spots dispatched. "worst" = MAX or SUM(outage).
- JOIN WeatherData to Storm whenever eventDateTime or eventLength is needed.
- Apply the default row limit provided with the question unless the user asks for a different number.
- Return ONLY the raw SQL query. No markdown fences, no backticks, no prose.
- NEVER generate INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, REPLACE, or TRUNCATE.

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
A: SELECT COUNT(DISTINCT s.stormCode) AS storm_count FROM Storm s WHERE strftime('%Y', s.eventDateTime) BETWEEN '2010' AND '2015';

Q: Which county had more outages, Fairfield or Hartford?
A: SELECT county, SUM(outage) AS total_outage FROM WeatherData WHERE LOWER(county) IN ('fairfield', 'hartford') GROUP BY county ORDER BY total_outage DESC;

Q: Which storms hit more than one county?
A: SELECT stormCode, COUNT(DISTINCT county) AS county_count FROM WeatherData GROUP BY stormCode HAVING county_count > 1 ORDER BY county_count DESC LIMIT 10;

Q: Which storms lasted longer than 48 hours?
A: SELECT s.stormCode, s.eventDateTime, s.eventLength FROM Storm s WHERE s.eventLength > 48 ORDER BY s.eventLength DESC LIMIT 10;
"""


class SqlValidationError(ValueError):
    """Raised when the LLM-generated SQL fails safety or syntax validation."""


@dataclass
class SqlAiAgent:
    """Translates natural-language questions into SQLite SELECT queries via an LLM.

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
        conn: sqlite3.Connection,
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

        db_path = _get_db_path(conn)
        rows = _execute_ro(sql, db_path)
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
) -> str:
    return (
        f"Schema:\n{schema_ddl}\n\n"
        f"Default row limit: {limit}\n\n"
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
    for turn in list(history or ())[-MAX_HISTORY_TURNS:]:
        messages.append({"role": "user", "content": f"Question: {turn.question}"})
        messages.append(
            {"role": "assistant", "content": f"SQL: {turn.sql}\nAnswer: {turn.answer}"}
        )
    messages.append(
        {"role": "user", "content": _build_user_message(schema_ddl, query, limit=limit)}
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
        "max_tokens": 512,
        "temperature": 0,
    }
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


def _clean_sql(raw: str) -> str:
    text = raw.strip()
    text = re.sub(r"^```(?:sql)?\s*\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _validate_sql(sql: str) -> None:
    if _WRITE_PATTERN.match(sql):
        raise SqlValidationError(
            f"Non-SELECT statement is not allowed: {sql[:60]!r}"
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
        # SQLite allows WITH before INSERT/UPDATE/DELETE, so a CTE prefix is
        # not proof of a read-only query — scan for write keywords too.
        if re.match(r"^\s*WITH\b", sql, re.IGNORECASE) and _WRITE_KEYWORD_PATTERN.search(sql):
            raise SqlValidationError(
                "CTE contains a write keyword; only read-only SELECT is allowed."
            )
        return

    try:
        statements = sqlglot.parse(
            sql,
            dialect="sqlite",
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


def _get_db_path(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA database_list").fetchone()
    db_path = row[2] if row else ""
    if not db_path:
        raise SqlValidationError(
            "Cannot determine database path from connection for read-only execution."
        )
    return db_path


def _execute_ro(sql: str, db_path: str) -> list[dict[str, Any]]:
    ro_uri = Path(db_path).as_uri() + "?mode=ro"
    ro_conn = sqlite3.connect(ro_uri, uri=True)
    try:
        ro_conn.row_factory = sqlite3.Row
        cursor = ro_conn.execute(sql)
        return [dict(row) for row in cursor.fetchall()]
    finally:
        ro_conn.close()


def _make_answer(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No results found."
    if len(rows) == 1 and len(rows[0]) == 1:
        key, val = next(iter(rows[0].items()))
        return f"The {key} is {val}."
    if len(rows) == 1:
        return "Found 1 matching record."
    return f"Found {len(rows)} results."
