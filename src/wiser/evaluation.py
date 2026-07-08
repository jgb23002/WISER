from __future__ import annotations

import json
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from wiser.agents.sql_ai_agent import _validate_sql
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

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "sql_ground_truth.db"
        conn = sqlite3.connect(str(db_path))
        try:
            build_sql_fixture_database(conn, fixture, schema_path=schema_path)
            results = [
                _evaluate_sql_case(conn, case) for case in fixture["cases"]
            ]
        finally:
            conn.close()

    passed = sum(1 for result in results if result.passed)
    return EvaluationSummary(
        name="sql",
        total=len(results),
        passed=passed,
        results=tuple(results),
    )


def build_sql_fixture_database(
    conn: sqlite3.Connection,
    fixture: dict,
    *,
    schema_path: str | Path,
) -> None:
    conn.executescript(Path(schema_path).read_text(encoding="utf-8"))

    for storm in fixture["database"]["storms"]:
        conn.execute(
            """
            INSERT INTO Storm(stormCode, eventDateTime, eventLength)
            VALUES(:stormCode, :eventDateTime, :eventLength)
            """,
            storm,
        )

    for county in fixture["database"]["counties"]:
        conn.execute(
            """
            INSERT INTO County(county, state, ohLine)
            VALUES(:county, :state, :ohLine)
            """,
            county,
        )

    for row in fixture["database"]["weather_data"]:
        conn.execute(
            """
            INSERT INTO WeatherData(
                stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,
                wgt5, wgt9, wgt13, wgt18, outage
            )
            VALUES(
                :stormCode, :county, :sumAFWA_TOTPRECIP, :peakWSPD10,
                :wgt5, :wgt9, :wgt13, :wgt18, :outage
            )
            """,
            row,
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
    conn: sqlite3.Connection,
    case: dict,
) -> EvaluationCaseResult:
    sql = case["expected_sql"]
    try:
        _validate_sql(sql)
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


def _execute(conn: sqlite3.Connection, sql: str) -> list[dict[str, object]]:
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql).fetchall()]
    finally:
        conn.row_factory = None


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
