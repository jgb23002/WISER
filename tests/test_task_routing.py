import sys
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.agents.task_routing_agent import Route, TaskRoutingAgent  # noqa: E402


class TestTaskRoutingAgent(unittest.TestCase):
    def setUp(self):
        self.agent = TaskRoutingAgent()

    def test_routes_explanation_queries_to_rag(self):
        decision = self.agent.route("explain the storm similarity algorithm")

        self.assertEqual(decision.route, Route.RAG_KNOWLEDGE)

    def test_routes_similarity_method_queries_to_rag(self):
        decision = self.agent.route("how does storm similarity work")

        self.assertEqual(decision.route, Route.RAG_KNOWLEDGE)

    def test_keeps_top_counties_as_weather_analytics(self):
        decision = self.agent.route("top counties by outage")

        self.assertEqual(decision.route, Route.WEATHER_ANALYTICS)

    def test_keeps_database_build_as_data_retrieval(self):
        decision = self.agent.route("build sqlite database from csv")

        self.assertEqual(decision.route, Route.DATA_RETRIEVAL)


if __name__ == "__main__":
    unittest.main()
