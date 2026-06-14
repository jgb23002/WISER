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

    def test_routes_numeric_similarity_queries_before_analytics(self):
        """Queries with both 'storm' and 'similar' must hit SIMILARITY_SEARCH."""
        decision = self.agent.route(
            "find storms similar to wind=9.2, precip=45"
        )

        self.assertEqual(decision.route, Route.SIMILARITY_SEARCH)

    def test_routes_rank_storms_keyword_to_similarity_search(self):
        decision = self.agent.route("rank storms similar to wind=6.5")

        self.assertEqual(decision.route, Route.SIMILARITY_SEARCH)

    def test_routes_similarity_word_with_numeric_params_to_search(self):
        """'similarity' + wind= must not be swallowed by RAG."""
        decision = self.agent.route("find storm similarity wind=9.2")

        self.assertEqual(decision.route, Route.SIMILARITY_SEARCH)


if __name__ == "__main__":
    unittest.main()
