from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent
SRC_DIR = REPO_ROOT / "src"
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "sql_ai_ground_truth.json"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.agents.sql_ai_agent import SqlAiAgent, _validate_sql  # noqa: E402


def _load_fixture() -> dict:
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _build_fixture_db(fixture: dict) -> tuple[sqlite3.Connection, Path]:
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_path = Path(tmp)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

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
    return conn, db_path


def _execute(conn: sqlite3.Connection, sql: str) -> list[dict[str, object]]:
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql).fetchall()]
    finally:
        conn.row_factory = None


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
        cls.conn, cls.db_path = _build_fixture_db(cls.fixture)
        cls.agent = SqlAiAgent()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        try:
            cls.db_path.unlink(missing_ok=True)
        except OSError:
            pass

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
                    self.assertEqual(result["rows"], case["expected_rows"])
                    self.assertFalse(result["dry_run"])


if __name__ == "__main__":
    unittest.main()
