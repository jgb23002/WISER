import sqlite3
import sys
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.database import initialize_schema  # noqa: E402


class TestDatabaseInitialization(unittest.TestCase):
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
