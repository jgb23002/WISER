"""Smoke tests for the Streamlit interface.

Runs the app headlessly via streamlit.testing.v1.AppTest against a temp
database. No network calls: the analytics scenario uses dry-run mode and
the similarity scenario never touches an LLM. Skipped when streamlit is
not installed (it is an optional dependency, see requirements.txt).
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SRC_DIR = REPO_ROOT / "src"
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"
APP_PATH = REPO_ROOT / "streamlit_app.py"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

try:
    from streamlit.testing.v1 import AppTest

    _STREAMLIT_AVAILABLE = True
except ImportError:
    _STREAMLIT_AVAILABLE = False


def _build_temp_db() -> Path:
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tmp_path = Path(tmp)
    conn = sqlite3.connect(str(tmp_path))
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO Storm(stormCode, eventDateTime, eventLength) VALUES(?, ?, ?)",
            (2012102806, "2012-10-28 06:00:00", 72),
        )
        conn.execute(
            "INSERT INTO County(county, ohLine) VALUES(?, ?)", ("Fairfield", 4887830)
        )
        conn.execute(
            "INSERT INTO WeatherData(stormCode, county, sumAFWA_TOTPRECIP, peakWSPD10,"
            " wgt5, wgt9, wgt13, wgt18, outage) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (2012102806, "Fairfield", 150.5, 12.3, 20, 8, 2, 0, 350),
        )
        conn.commit()
    finally:
        conn.close()
    return tmp_path


@unittest.skipUnless(_STREAMLIT_AVAILABLE, "streamlit is not installed")
class TestStreamlitApp(unittest.TestCase):
    def setUp(self):
        self.db_path = _build_temp_db()

    def tearDown(self):
        try:
            self.db_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _start_app(self) -> "AppTest":
        at = AppTest.from_file(str(APP_PATH), default_timeout=60)
        at.run()
        self.assertFalse(at.exception, f"app crashed on load: {at.exception}")
        return at

    def _widget(self, widgets, label):
        match = next((w for w in widgets if w.label == label), None)
        self.assertIsNotNone(match, f"widget not found: {label}")
        return match

    def _run_query(self, at: "AppTest", query: str) -> "AppTest":
        self._widget(at.text_input, "SQLite database").set_value(str(self.db_path))
        at.text_input(key="query_input").set_value(query)
        self._widget(at.button, "Run query").set_value(True)
        at.run()
        self.assertFalse(at.exception, f"app crashed on query: {at.exception}")
        return at

    def test_loads_without_crashing(self):
        self._start_app()

    def test_similarity_query_renders_results(self):
        at = self._start_app()
        self._run_query(at, "find storms similar to wind=9.2 in Fairfield")
        markdown_text = " ".join(md.value for md in at.markdown)
        self.assertIn("similarity_search", markdown_text)
        self.assertTrue(len(at.dataframe) >= 1, "expected a results dataframe")

    def test_dry_run_analytics_shows_prompt_without_api_call(self):
        at = self._start_app()
        self._widget(at.checkbox, "Dry-run SQL").check()
        self._run_query(at, "top storms by outage")
        markdown_text = " ".join(md.value for md in at.markdown)
        self.assertIn("weather_analytics", markdown_text)
        code_blocks = " ".join(code.value for code in at.code)
        self.assertIn("[SYSTEM]", code_blocks)

    def test_similarity_missing_county_shows_error_not_crash(self):
        at = self._start_app()
        self._run_query(at, "find storms similar to wind=9.2")
        error_text = " ".join(err.value for err in at.error)
        self.assertIn("County required", error_text)


if __name__ == "__main__":
    unittest.main()
