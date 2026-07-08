from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SRC_DIR = REPO_ROOT / "src"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.integrations.eagleeye import parse_eagleeye_csv  # noqa: E402
from wiser.integrations.external_outage import (  # noqa: E402
    ensure_external_outage_schema,
    external_observation_count,
    load_external_observations,
    normalize_timestamp,
    observation_from_csv_row,
    recent_external_observations,
)
from wiser.integrations.national_grid import parse_national_grid_csv  # noqa: E402


class TestExternalOutageParsers(unittest.TestCase):
    def test_parse_eagleeye_fixture(self):
        rows = parse_eagleeye_csv(FIXTURE_DIR / "eagleeye_sample.csv")

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].source_system, "EagleEye")
        self.assertEqual(rows[0].source_record_id, "EE-20260702-001")
        self.assertEqual(rows[0].observed_at, "2026-07-02T18:30:00Z")
        self.assertEqual(rows[0].county, "Fairfield")
        self.assertEqual(rows[0].service_territory, "CT Shoreline")
        self.assertEqual(rows[0].outage_count, 18)
        self.assertEqual(rows[0].customer_count, 1200)
        self.assertEqual(rows[0].restoration_status, "Assessing")
        self.assertIn("EE-20260702-001", rows[0].raw_payload)

    def test_parse_national_grid_fixture(self):
        rows = parse_national_grid_csv(FIXTURE_DIR / "national_grid_sample.csv")

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].source_system, "National Grid")
        self.assertEqual(rows[0].source_record_id, "NG-20260702-9001")
        self.assertEqual(rows[0].observed_at, "2026-07-02T19:10:00Z")
        self.assertEqual(rows[0].county, "New Haven")
        self.assertEqual(rows[0].service_territory, "Connecticut South")
        self.assertEqual(rows[0].outage_count, 11)
        self.assertEqual(rows[0].customer_count, 875)

    def test_missing_optional_fields_are_allowed(self):
        observation = observation_from_csv_row(
            {
                "id": "ROW-1",
                "timestamp": "2026-07-02T14:30:00-04:00",
                "county": "Fairfield",
            },
            source_system="Fixture",
        )

        self.assertEqual(observation.source_record_id, "ROW-1")
        self.assertEqual(observation.county, "Fairfield")
        self.assertIsNone(observation.outage_count)
        self.assertIsNone(observation.customer_count)
        self.assertIsNone(observation.restoration_status)

    def test_missing_record_id_gets_stable_generated_id(self):
        row = {
            "timestamp": "2026-07-02T14:30:00-04:00",
            "county": "Fairfield",
            "outages": "2",
        }

        first = observation_from_csv_row(row, source_system="Fixture")
        second = observation_from_csv_row(row, source_system="Fixture")

        self.assertTrue(first.source_record_id.startswith("generated:"))
        self.assertEqual(first.source_record_id, second.source_record_id)

    def test_invalid_timestamp_raises_clear_error(self):
        with self.assertRaisesRegex(ValueError, "invalid timestamp"):
            observation_from_csv_row(
                {
                    "id": "bad-row",
                    "timestamp": "not-a-date",
                    "county": "Fairfield",
                },
                source_system="Fixture",
            )

    def test_negative_outage_count_raises_clear_error(self):
        with self.assertRaisesRegex(ValueError, "outage_count must be non-negative"):
            observation_from_csv_row(
                {
                    "id": "bad-row",
                    "timestamp": "2026-07-02T14:30:00-04:00",
                    "outages": "-1",
                },
                source_system="Fixture",
            )

    def test_naive_timestamp_uses_default_timezone(self):
        self.assertEqual(
            normalize_timestamp(
                "2026-07-02 14:30:00",
                default_timezone="America/New_York",
            ),
            "2026-07-02T18:30:00Z",
        )


class TestExternalOutageDatabase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_schema_table_and_indexes_exist(self):
        ensure_external_outage_schema(self.conn)

        tables = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }

        self.assertIn("ExternalOutageObservation", tables)
        self.assertIn("idx_external_outage_source_time", indexes)
        self.assertIn("idx_external_outage_county", indexes)

    def test_load_external_observations(self):
        observations = parse_eagleeye_csv(FIXTURE_DIR / "eagleeye_sample.csv")

        summary = load_external_observations(self.conn, observations)

        self.assertEqual(summary.source_system, "EagleEye")
        self.assertEqual(summary.rows_seen, 2)
        self.assertEqual(summary.rows_loaded, 2)
        self.assertEqual(summary.table_count, 2)
        self.assertEqual(external_observation_count(self.conn), 2)

    def test_load_upserts_duplicate_source_record(self):
        observations = parse_eagleeye_csv(FIXTURE_DIR / "eagleeye_sample.csv")

        load_external_observations(self.conn, observations)
        load_external_observations(self.conn, observations)

        self.assertEqual(
            external_observation_count(self.conn, source_system="EagleEye"),
            2,
        )

    def test_recent_external_observations_filters_by_source(self):
        load_external_observations(
            self.conn,
            parse_eagleeye_csv(FIXTURE_DIR / "eagleeye_sample.csv"),
        )
        load_external_observations(
            self.conn,
            parse_national_grid_csv(FIXTURE_DIR / "national_grid_sample.csv"),
        )

        rows = recent_external_observations(
            self.conn,
            source_system="National Grid",
            limit=5,
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual({row["sourceSystem"] for row in rows}, {"National Grid"})

    def test_schema_constraints_reject_negative_counts(self):
        ensure_external_outage_schema(self.conn)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO ExternalOutageObservation(
                    sourceSystem,
                    sourceRecordId,
                    observedAt,
                    outageCount,
                    rawPayload
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Fixture", "bad", "2026-07-02T18:30:00Z", -1, "{}"),
            )
            self.conn.commit()


class TestExternalOutageCsvErrors(unittest.TestCase):
    def test_file_error_mentions_row_number(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bad.csv"
            path.write_text(
                "id,timestamp,outages\n"
                "row-1,2026-07-02T14:30:00-04:00,1\n"
                "row-2,not-a-date,2\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, r"bad\.csv:3"):
                parse_eagleeye_csv(path)


if __name__ == "__main__":
    unittest.main()
