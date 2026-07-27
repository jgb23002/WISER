from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SRC_DIR = REPO_ROOT / "src"
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "task_routing_cases.json"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.agents.task_routing_agent import Route, TaskRoutingAgent  # noqa: E402


def _load_cases() -> list[dict[str, str]]:
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


class TestTaskRoutingGroundTruth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = _load_cases()
        cls.agent = TaskRoutingAgent()

    def test_fixture_has_acceptance_count(self):
        self.assertGreaterEqual(len(self.cases), 58)

    def test_fixture_ids_are_unique(self):
        ids = [case["id"] for case in self.cases]
        self.assertEqual(len(ids), len(set(ids)))

    def test_all_expected_routes_are_valid(self):
        valid_routes = {route.value for route in Route}
        for case in self.cases:
            with self.subTest(case=case["id"]):
                self.assertIn(case["expected_route"], valid_routes)

    def test_routes_match_ground_truth(self):
        for case in self.cases:
            with self.subTest(case=case["id"], query=case["query"]):
                decision = self.agent.route(case["query"])
                self.assertEqual(decision.route.value, case["expected_route"])

                expected_reason = case.get("expected_reason_contains")
                if expected_reason:
                    self.assertIn(expected_reason.lower(), decision.reason.lower())


if __name__ == "__main__":
    unittest.main()
