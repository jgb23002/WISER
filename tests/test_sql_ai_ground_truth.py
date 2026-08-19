from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent
SRC_DIR = REPO_ROOT / "src"
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "sql_ai_ground_truth.json"

TESTS_DIR = Path(__file__).parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from pg_harness import make_conn  # noqa: E402

from wiser.agents.sql_ai_agent import SqlAiAgent, _validate_sql  # noqa: E402
from wiser.evaluation import _render_row  # noqa: E402


def _load_fixture() -> dict:
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _build_fixture_db(fixture: dict) -> psycopg.Connection:
    """Seed the shared test schema from the fixture's database block."""
    conn = make_conn()

    for storm in fixture["database"]["storms"]:
        conn.execute(
            """
            INSERT INTO Storm(stormCode, eventDateTime, eventLength)
            VALUES(%(stormCode)s, %(eventDateTime)s, %(eventLength)s)
            """,
            storm,
        )

    for county in fixture["database"]["counties"]:
        conn.execute(
            """
            INSERT INTO County(county, state, ohLine)
            VALUES(%(county)s, %(state)s, %(ohLine)s)
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
                %(stormCode)s, %(county)s, %(sumAFWA_TOTPRECIP)s, %(peakWSPD10)s,
                %(wgt5)s, %(wgt9)s, %(wgt13)s, %(wgt18)s, %(outage)s
            )
            """,
            row,
        )
    conn.commit()
    return conn


def _execute(conn: psycopg.Connection, sql: str) -> list[dict[str, object]]:
    """Run one fixture query and return dict rows.

    psycopg's default row factory yields tuples, so ``dict_row`` is needed.
    Its keys are whatever PostgreSQL returned, meaning unquoted camelCase
    columns arrive folded to lowercase. Datetimes are rendered to text so the
    fixture can express expected values as JSON; a failing statement is rolled
    back to its savepoint so one bad case cannot abort the whole transaction.
    """
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cursor:
            cursor.execute(sql)
            return [_render_row(row) for row in cursor.fetchall()]


def _assert_case_contract(test: unittest.TestCase, case: dict) -> None:
    sql_lower = case["expected_sql"].lower()

    for table in case["required_tables"]:
        test.assertIn(table.lower(), sql_lower)

    for column in case["required_columns"]:
        test.assertIn(column.lower(), sql_lower)

    for token in case["forbidden_tokens"]:
        pattern = rf"\b{re.escape(token.lower())}\b"
        test.assertIsNone(
            re.search(pattern, sql_lower),
            f"{case['id']} uses forbidden token {token}",
        )


class TestSqlAiGroundTruth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = _load_fixture()
        cls.cases = cls.fixture["cases"]
        cls.conn = _build_fixture_db(cls.fixture)
        cls.agent = SqlAiAgent()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_fixture_has_acceptance_count(self):
        self.assertGreaterEqual(len(self.cases), 25)

    def test_fixture_ids_are_unique(self):
        ids = [case["id"] for case in self.cases]
        self.assertEqual(len(ids), len(set(ids)))

    def test_expected_sql_contract_and_expected_rows(self):
        for case in self.cases:
            with self.subTest(case=case["id"]):
                _assert_case_contract(self, case)
                _validate_sql(case["expected_sql"])
                self.assertEqual(
                    _execute(self.conn, case["expected_sql"]),
                    case["expected_rows"],
                )

    def test_mocked_sql_ai_matches_ground_truth_rows(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key-not-real"}), \
                patch("wiser.agents.sql_ai_agent.load_dotenv", return_value=0):
            for case in self.cases:
                with self.subTest(case=case["id"]):
                    with patch(
                        "wiser.agents.sql_ai_agent._call_chat_llm",
                        return_value=case["expected_sql"],
                    ):
                        result = self.agent.run(case["question"], self.conn)

                    self.assertEqual(result["sql"], case["expected_sql"])
                    # The agent returns driver-native values, so TIMESTAMP
                    # columns arrive as real datetime objects. The fixture is
                    # JSON and can only hold their text form, so render before
                    # comparing rather than weakening the agent.
                    self.assertEqual(
                        [_render_row(row) for row in result["rows"]],
                        case["expected_rows"],
                    )
                    self.assertFalse(result["dry_run"])


if __name__ == "__main__":
    unittest.main()
