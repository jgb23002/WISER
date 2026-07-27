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
        at.chat_input[0].set_value(query).run()
        self.assertFalse(at.exception, f"app crashed on query: {at.exception}")
        return at

    def test_loads_without_crashing(self):
        self._start_app()

    def test_app_version_change_clears_stale_conversation(self):
        at = self._start_app()
        at.session_state["app_state_version"] = "old-build"
        at.session_state["messages"] = [{"role": "user", "content": "old warning"}]
        at.session_state["sql_history"] = ["old sql turn"]
        at.session_state["last_route"] = "weather_analytics"

        at.run()

        self.assertFalse(at.exception, f"app crashed on rerun: {at.exception}")
        self.assertEqual(at.session_state["messages"], [])
        self.assertEqual(at.session_state["sql_history"], [])
        self.assertIsNone(at.session_state["last_route"])

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

    def test_transcript_persists_across_turns(self):
        at = self._start_app()
        self._widget(at.checkbox, "Dry-run SQL").check()
        self._run_query(at, "top storms by outage")
        self._run_query(at, "top counties by outage")
        markdown_text = " ".join(md.value for md in at.markdown)
        # Both user turns are still in the replayed transcript.
        self.assertIn("top storms by outage", markdown_text)
        self.assertIn("top counties by outage", markdown_text)
        self.assertEqual(len(at.session_state["messages"]), 4)

    def test_unknown_follow_up_sticks_to_analytics(self):
        at = self._start_app()
        self._widget(at.checkbox, "Dry-run SQL").check()
        self._run_query(at, "top storms by outage")
        self._run_query(at, "what about Hartford?")
        markdown_text = " ".join(md.value for md in at.markdown)
        self.assertIn("sticky route", markdown_text)


if __name__ == "__main__":
    unittest.main()


class TestStormReportRendering(unittest.TestCase):
    """Structural checks on the Streamlit storm-report path.

    Asserted with ast rather than by importing: streamlit_app.py calls main()
    at module scope, so importing it runs the app.
    """

    @classmethod
    def setUpClass(cls):
        import ast

        cls.ast = ast
        cls.tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
        cls.functions = {
            node.name: node
            for node in ast.walk(cls.tree)
            if isinstance(node, ast.FunctionDef)
        }

    def test_storm_report_renderer_exists(self):
        self.assertIn("_render_storm_report", self.functions)

    def _streamlit_calls(self, function_name: str) -> set[str]:
        """Names of st.* widgets called in a function."""
        return {
            node.func.attr
            for node in self.ast.walk(self.functions[function_name])
            if isinstance(node, self.ast.Call)
            and isinstance(node.func, self.ast.Attribute)
            and isinstance(node.func.value, self.ast.Name)
            and node.func.value.id == "st"
        }

    def test_report_is_rendered_preformatted_not_as_markdown(self):
        """The reports use column alignment and leading-space indents that
        markdown would collapse."""
        calls = self._streamlit_calls("_render_storm_report")
        self.assertIn("code", calls)
        self.assertNotIn("markdown", calls)

    def test_analytics_renderer_checks_for_a_report(self):
        source = self.ast.dump(self.functions["_render_analytics"])
        self.assertIn("report", source)
        self.assertIn("_render_storm_report", source)

    def test_similarity_renderer_checks_for_a_report(self):
        source = self.ast.dump(self.functions["_render_similarity"])
        self.assertIn("_render_storm_report", source)

    def test_heatmap_is_still_rendered_once_on_the_report_path(self):
        source = self.ast.dump(self.functions["_render_analytics"])
        self.assertEqual(source.count("_render_analytics_heatmap"), 3)
