from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import psycopg
from psycopg.rows import dict_row

from wiser.agents.sql_ai_agent import validate_sql
from wiser.database import connect, executemany
from wiser.agents.task_routing_agent import TaskRoutingAgent


@dataclass(frozen=True)
class EvaluationCaseResult:
    case_id: str
    passed: bool
    message: str


@dataclass(frozen=True)
class EvaluationSummary:
    name: str
    total: int
    passed: int
    results: tuple[EvaluationCaseResult, ...]

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def pass_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.passed / self.total


def load_json(path: str | Path) -> object:
    with Path(path).open(encoding="utf-8") as fh:
        return json.load(fh)


def evaluate_routing_cases(
    fixture_path: str | Path,
    *,
    agent: TaskRoutingAgent | None = None,
) -> EvaluationSummary:
    cases = load_json(fixture_path)
    if not isinstance(cases, list):
        raise ValueError("Routing fixture must be a list of cases.")

    router = agent or TaskRoutingAgent()
    results: list[EvaluationCaseResult] = []
    for case in cases:
        decision = router.route(case["query"])
        expected = case["expected_route"]
        if decision.route.value != expected:
            results.append(
                EvaluationCaseResult(
                    case_id=case["id"],
                    passed=False,
                    message=(
                        f"expected {expected}, got {decision.route.value}: "
                        f"{case['query']}"
                    ),
                )
            )
            continue

        expected_reason = case.get("expected_reason_contains")
        if expected_reason and expected_reason.lower() not in decision.reason.lower():
            results.append(
                EvaluationCaseResult(
                    case_id=case["id"],
                    passed=False,
                    message=(
                        f"route matched but reason did not contain "
                        f"{expected_reason!r}: {decision.reason}"
                    ),
                )
            )
            continue

        results.append(
            EvaluationCaseResult(
                case_id=case["id"],
                passed=True,
                message="ok",
            )
        )

    passed = sum(1 for result in results if result.passed)
    return EvaluationSummary(
        name="routing",
        total=len(results),
        passed=passed,
        results=tuple(results),
    )


def evaluate_sql_ground_truth(
    fixture_path: str | Path,
    *,
    schema_path: str | Path,
) -> EvaluationSummary:
    fixture = load_json(fixture_path)
    if not isinstance(fixture, dict) or "cases" not in fixture:
        raise ValueError("SQL fixture must contain a top-level 'cases' list.")

    # PostgreSQL has no throwaway file database. A uniquely named schema gives
    # the same isolation: the fixture tables live there and are dropped after.
    schema_name = f"wiser_eval_{secrets.token_hex(8)}"
    conn = connect()
    try:
        conn.execute(f"CREATE SCHEMA {schema_name}")
        conn.execute(f"SET search_path TO {schema_name}")
        conn.commit()
        build_sql_fixture_database(conn, fixture, schema_path=schema_path)
        results = [
            _evaluate_sql_case(conn, case) for case in fixture["cases"]
        ]
    finally:
        _drop_eval_schema(conn, schema_name)

    passed = sum(1 for result in results if result.passed)
    return EvaluationSummary(
        name="sql",
        total=len(results),
        passed=passed,
        results=tuple(results),
    )


def _drop_eval_schema(conn: psycopg.Connection, schema_name: str) -> None:
    """Drop the temp schema without letting cleanup mask the real failure.

    If the server dropped the connection mid-run, every statement here raises.
    Unguarded, the first one would replace the original traceback and skip the
    DROP entirely, leaving an orphaned 8-table schema behind on every run. So
    each step is best-effort, and a dead connection gets one fresh connection
    to finish the job.
    """
    try:
        conn.rollback()
        conn.execute("SET search_path TO public")
        conn.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE")
        conn.commit()
        return
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass

    try:
        cleanup = connect()
        try:
            cleanup.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE")
            cleanup.commit()
        finally:
            cleanup.close()
    except Exception:
        pass


def build_sql_fixture_database(
    conn: psycopg.Connection,
    fixture: dict,
    *,
    schema_path: str | Path,
) -> None:
    conn.execute(Path(schema_path).read_text(encoding="utf-8"))

    # Batched rather than a statement per row: this runs on Streamlit's first
    # page load (behind st.cache_data) as well as in the test suite.
    executemany(
        conn,
        """
        INSERT INTO Storm(stormCode, eventDateTime, eventLength)
        VALUES(%(stormCode)s, %(eventDateTime)s, %(eventLength)s)
        """,
        fixture["database"]["storms"],
    )
    executemany(
        conn,
        """
        INSERT INTO County(county, state, ohLine)
        VALUES(%(county)s, %(state)s, %(ohLine)s)
        """,
        fixture["database"]["counties"],
    )
    executemany(
        conn,
        """
        INSERT INTO WeatherData(
            stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
            wgt5, wgt9, wgt13, wgt18, outage
        )
        VALUES(
            %(stormCode)s, %(county)s, %(sumAFWA_TOTPRECIP)s, %(peakWSPD10)s,
            %(wgt5)s, %(wgt9)s, %(wgt13)s, %(wgt18)s, %(outage)s
        )
        """,
        fixture["database"]["weather_data"],
    )
    conn.commit()


def format_evaluation_summary(summary: EvaluationSummary) -> str:
    percent = summary.pass_rate * 100
    lines = [
        (
            f"{summary.name}: {summary.passed}/{summary.total} passed "
            f"({percent:.1f}%)"
        )
    ]
    for result in summary.results:
        if not result.passed:
            lines.append(f"  - {result.case_id}: {result.message}")
    return "\n".join(lines)


def _evaluate_sql_case(
    conn: psycopg.Connection,
    case: dict,
) -> EvaluationCaseResult:
    sql = case["expected_sql"]
    try:
        validate_sql(sql)
        _assert_required_tokens(sql, case["required_tables"])
        _assert_required_tokens(sql, case["required_columns"])
        _assert_forbidden_tokens_absent(sql, case["forbidden_tokens"])
        rows = _execute(conn, sql)
    except Exception as exc:  # noqa: BLE001 - evaluation should report and continue
        return EvaluationCaseResult(
            case_id=case["id"],
            passed=False,
            message=f"{type(exc).__name__}: {exc}",
        )

    if rows != case["expected_rows"]:
        return EvaluationCaseResult(
            case_id=case["id"],
            passed=False,
            message=f"expected rows {case['expected_rows']!r}, got {rows!r}",
        )

    return EvaluationCaseResult(
        case_id=case["id"],
        passed=True,
        message="ok",
    )


def _execute(conn: psycopg.Connection, sql: str) -> list[dict[str, object]]:
    """Run one fixture query and return dict rows.

    psycopg's default row factory yields tuples, so ``dict_row`` is required
    here. Its keys are the columns PostgreSQL actually returned, which means
    unquoted camelCase selections arrive folded to lowercase.
    """
    # A failed statement aborts the whole PostgreSQL transaction, so without
    # this savepoint one bad case would fail every case after it. SQLite had
    # no such coupling between statements.
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cursor:
            cursor.execute(sql)
            return [_render_row(row) for row in cursor.fetchall()]


def _render_row(row: dict[str, object]) -> dict[str, object]:
    """Normalize driver-native values so fixtures stay engine-neutral JSON.

    SQLite handed back TIMESTAMP columns as text; psycopg builds real
    ``datetime`` objects. ``str()`` on one reproduces the text SQLite stored,
    so the fixture keeps recording a value JSON can express. This is a
    rendering concern only -- application code still receives real datetimes.
    """
    return {key: _render_value(value) for key, value in row.items()}


def _render_value(value: object) -> object:
    if isinstance(value, (datetime, date)):
        return str(value)
    if isinstance(value, Decimal):
        # AVG() over integers and every ROUND(x::numeric, 2) come back as
        # Decimal. Decimal('7.35') != 7.35, because the float nearest 7.35
        # is 7.34999999999999964..., so a JSON fixture can never match one
        # unless the value happens to be binary-exact.
        return float(value)
    return value


def _assert_required_tokens(sql: str, required_tokens: Sequence[str]) -> None:
    lowered = sql.lower()
    for token in required_tokens:
        if token.lower() not in lowered:
            raise AssertionError(f"missing required token {token!r}")


def _assert_forbidden_tokens_absent(sql: str, forbidden_tokens: Sequence[str]) -> None:
    lowered = sql.lower()
    for token in forbidden_tokens:
        pattern = rf"\b{re.escape(token.lower())}\b"
        if re.search(pattern, lowered):
            raise AssertionError(f"forbidden token present: {token!r}")
