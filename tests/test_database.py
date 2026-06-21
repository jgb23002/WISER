import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.database import (  # noqa: E402
    DEFAULT_SQLITE_BUSY_TIMEOUT_MS,
    connect,
    initialize_schema,
)


class TestDatabaseInitialization(unittest.TestCase):
    def test_connect_sets_busy_timeout_and_foreign_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "wiser.db"
            conn = connect(db_path)
            try:
                busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
                foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(int(busy_timeout), DEFAULT_SQLITE_BUSY_TIMEOUT_MS)
        self.assertEqual(int(foreign_keys), 1)

    def test_initialize_schema_replaces_legacy_stormcounty_table(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(
                """
                PRAGMA foreign_keys = ON;

                CREATE TABLE Storm (
                    stormCode INTEGER PRIMARY KEY,
                    state TEXT NOT NULL,
                    eventDateTime TEXT NOT NULL,
                    eventLength INTEGER NOT NULL
                );

                CREATE TABLE County (
                    County TEXT PRIMARY KEY,
                    ohLine INTEGER NOT NULL
                );

                CREATE TABLE StormCounty (
                    stormCode INTEGER NOT NULL,
                    County TEXT NOT NULL,
                    PRIMARY KEY (stormCode, County),
                    FOREIGN KEY (stormCode) REFERENCES Storm(stormCode),
                    FOREIGN KEY (County) REFERENCES County(County)
                );
                """
            )
            conn.commit()

            initialize_schema(conn)

            table_names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertIn("Storm", table_names)
            self.assertIn("County", table_names)
            self.assertIn("WeatherData", table_names)
            self.assertNotIn("StormCounty", table_names)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
