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

    def test_routes_storm_impact_queries_to_rag(self):
        decision = self.agent.route("impact of the new storm")

        self.assertEqual(decision.route, Route.RAG_KNOWLEDGE)

    def test_keeps_top_counties_as_weather_analytics(self):
        decision = self.agent.route("top counties by outage")

        self.assertEqual(decision.route, Route.WEATHER_ANALYTICS)

    def test_routes_affected_most_to_weather_analytics(self):
        decision = self.agent.route("which county was affected the most overall?")

        self.assertEqual(decision.route, Route.WEATHER_ANALYTICS)

    def test_routes_normalization_to_weather_analytics(self):
        decision = self.agent.route("normalize outages by population")

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

    def test_routes_known_wgt_parameter_to_similarity_search(self):
        decision = self.agent.route("find storms with wgt18=4")

        self.assertEqual(decision.route, Route.SIMILARITY_SEARCH)

    def test_does_not_route_unknown_wgt_parameter_to_similarity_search(self):
        decision = self.agent.route("find storms with wgt99=4")

        self.assertNotEqual(decision.route, Route.SIMILARITY_SEARCH)

    def test_routes_similarity_word_with_numeric_params_to_search(self):
        """'similarity' + wind= must not be swallowed by RAG."""
        decision = self.agent.route("find storm similarity wind=9.2")

        self.assertEqual(decision.route, Route.SIMILARITY_SEARCH)

    def test_routes_sponsor_month_comparison_to_weather_analytics(self):
        decision = self.agent.route(
            "how does storms in July and Februrary relate to storms"
        )

        self.assertEqual(decision.route, Route.WEATHER_ANALYTICS)

    def test_routes_this_month_intensity_question_to_weather_analytics(self):
        decision = self.agent.route(
            "is the storms this month large and more intense than in February"
        )

        self.assertEqual(decision.route, Route.WEATHER_ANALYTICS)

    def test_routes_regional_threshold_question_to_weather_analytics(self):
        decision = self.agent.route("northern CT storms with wind above 40mph")

        self.assertEqual(decision.route, Route.WEATHER_ANALYTICS)


if __name__ == "__main__":
    unittest.main()


class TestStormIntentsDoNotRegressExistingRoutes(unittest.TestCase):
    """The adversarial version of the plan's regression argument.

    Storm intents run ahead of RAG, similarity, data-management, and analytics,
    so the identifier gate is the only thing keeping them from capturing those
    routes. These tests append a storm identifier to queries that must NOT
    become storm-scoped, proving the gate needs a storm CUE as well as an
    identifier.
    """

    # The seven data_* fixtures plus the two routing-priority analytics cases
    # named in the plan's constraints.
    PROTECTED = {
        Route.DATA_RETRIEVAL: (
            "build sqlite database from csv",
            "load the raw storm csv",
            "normalize the weather csv",
            "show database schema",
            "ingest the source data",
            "retrieve the sqlite table counts",
            "refresh db from data/testdata.csv",
        ),
        Route.WEATHER_ANALYTICS: (
            "top counties from database",
            "normalize outages by population",
        ),
    }

    def setUp(self):
        self.agent = TaskRoutingAgent()

    def test_protected_queries_route_correctly(self):
        for expected, queries in self.PROTECTED.items():
            for query in queries:
                with self.subTest(query=query):
                    self.assertIs(self.agent.route(query).route, expected)

    def test_a_stray_storm_code_does_not_capture_protected_queries(self):
        """A ten-digit number alone must not divert these to a storm intent."""
        for expected, queries in self.PROTECTED.items():
            for query in queries:
                adversarial = f"{query} 2020080406"
                with self.subTest(query=adversarial):
                    self.assertIs(self.agent.route(adversarial).route, expected)

    def test_a_stray_iso_date_does_not_capture_protected_queries(self):
        for expected, queries in self.PROTECTED.items():
            for query in queries:
                adversarial = f"{query} on 2020-08-04"
                with self.subTest(query=adversarial):
                    self.assertIs(self.agent.route(adversarial).route, expected)

    def test_year_ranges_are_not_storm_identifiers(self):
        """Four-digit years must not read as storm codes or dates."""
        self.assertIs(
            self.agent.route("How many storms happened between 2010 and 2015?").route,
            Route.WEATHER_ANALYTICS,
        )

    def test_storm_cue_without_an_identifier_stays_on_its_old_route(self):
        for query, expected in (
            ("impact of the new storm in Connecticut", Route.RAG_KNOWLEDGE),
            ("explain top counties by outage", Route.RAG_KNOWLEDGE),
            ("summarize the documentation", Route.RAG_KNOWLEDGE),
            ("rank storms similar to wind=6.5", Route.SIMILARITY_SEARCH),
            ("top storms by outage", Route.WEATHER_ANALYTICS),
        ):
            with self.subTest(query=query):
                self.assertIs(self.agent.route(query).route, expected)

    def test_numeric_feature_params_keep_similarity_priority(self):
        for query in (
            "find storms similar to wind=9.2, precip=45 in Fairfield",
            "find storm similarity wind=9.2",
            "storms like wgt18=4 in Hartford",
        ):
            with self.subTest(query=query):
                self.assertIs(
                    self.agent.route(query).route, Route.SIMILARITY_SEARCH
                )


class TestGateZeroBoundary(unittest.TestCase):
    """Where numeric feature params stop meaning "similarity search".

    Mode B queries carry numeric params, a comparison cue, and often a storm
    identifier. The boundary is drawn on whether a specific database storm is
    NAMED: a forecast compared against one is a comparison; a forecast compared
    against the record as a whole is the similarity search, rendered
    differently by the handler.
    """

    def setUp(self):
        self.agent = TaskRoutingAgent()

    def test_forecast_against_a_named_storm_is_a_comparison(self):
        for query in (
            "compare an upcoming storm with wind=12ms to storm 2020080406",
            "how would a forecast storm with wind=12ms compare to 2011082800",
            "storm 2020080406 versus a forecast with precip=30mm",
        ):
            with self.subTest(query=query):
                self.assertIs(
                    self.agent.route(query).route, Route.STORM_COMPARISON
                )

    def test_forecast_against_the_record_stays_on_similarity(self):
        """No storm named: this IS the similarity search. The handler upgrades
        the presentation; the route does not change."""
        for query in (
            "how does an upcoming storm with wind=12ms compare to the historical record",
            "compare storms with wind=9.2ms",
            "what past storms match a forecast of wind=12ms",
        ):
            with self.subTest(query=query):
                self.assertIs(
                    self.agent.route(query).route, Route.SIMILARITY_SEARCH
                )

    def test_similar_phrasing_wins_even_with_an_identifier(self):
        for query in (
            "find storms similar to wind=12ms like storm 2020080406",
            "find storms similar to 2020080406",
            "storms similar to 2020080406 with wind=9.2ms",
        ):
            with self.subTest(query=query):
                self.assertIs(
                    self.agent.route(query).route, Route.SIMILARITY_SEARCH
                )

    def test_every_existing_similarity_fixture_is_unmoved(self):
        """The gate revision must not pull a single existing query across."""
        for query in (
            "find storms similar to wind=9.2, precip=45 in Fairfield",
            "rank storms similar to wind=6.5",
            "find storm similarity wind=9.2",
            "storms like wgt18=4 in Hartford",
            "similar to precipitation=51.5 for New Haven",
            "find storms with peakwspd10=10.2 and wgt9=2",
            "similar to wind_speed=8.4 in Windham",
            "find storms similar to wgt5=12 wgt13=1",
        ):
            with self.subTest(query=query):
                self.assertIs(
                    self.agent.route(query).route, Route.SIMILARITY_SEARCH
                )

    def test_mode_a_is_unaffected_by_the_gate_revision(self):
        for query in (
            "compare storm 2020080406 and 2011082800",
            "storm 2020080406 vs storm 2012102806",
            "difference between 2020-08-04 and 2011-08-28 storms",
        ):
            with self.subTest(query=query):
                self.assertIs(
                    self.agent.route(query).route, Route.STORM_COMPARISON
                )
