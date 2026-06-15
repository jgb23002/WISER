import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SRC_DIR = Path(__file__).parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.external_sources import (  # noqa: E402
    DEFAULT_STORM_CONTEXT_URLS,
    OpenAISummarizationError,
    SourceDocument,
    answer_storm_impact,
    discover_pdf_links,
    extract_text_blocks_from_html,
    extract_text_from_pdf,
    index_external_documents,
    is_readable_text,
    normalize_url,
    search_external_context,
)
from wiser.similarity import rank_texts  # noqa: E402


class TestExternalSourceParsing(unittest.TestCase):
    def test_default_storm_sources_exclude_unrelated_arxiv_pdf(self):
        self.assertNotIn("https://arxiv.org/pdf/2508.05652", DEFAULT_STORM_CONTEXT_URLS)

    def test_normalize_url_adds_https(self):
        self.assertEqual(
            normalize_url("www.nhc.noaa.gov/data/tcr/index.php"),
            "https://www.nhc.noaa.gov/data/tcr/index.php",
        )

    def test_discover_pdf_links_from_nhc_list_items(self):
        html = """
        <ul>
          <li>Tropical Storm Arthur &ndash;
            <a href="/data/tcr/AL012020_Arthur.pdf">PDF</a> &ndash;
            <a href="/gis/best_track/al012020_best_track.kmz">KMZ</a>
          </li>
          <li>Hurricane Isaias –
            <a href="AL092020_Isaias.pdf">PDF</a>
          </li>
        </ul>
        """

        links = discover_pdf_links(
            html,
            "https://www.nhc.noaa.gov/data/tcr/index.php?season=2020&basin=atl",
        )

        self.assertEqual(len(links), 2)
        self.assertEqual(links[0].title, "Tropical Storm Arthur")
        self.assertEqual(
            links[0].url,
            "https://www.nhc.noaa.gov/data/tcr/AL012020_Arthur.pdf",
        )
        self.assertEqual(links[1].title, "Hurricane Isaias")
        self.assertEqual(
            links[1].url,
            "https://www.nhc.noaa.gov/data/tcr/AL092020_Isaias.pdf",
        )

    def test_extract_text_blocks_from_html_keeps_paragraphs(self):
        html = """
        <html><body>
          <h1>Connecticut Summary</h1>
          <p>From 1980-2024, Connecticut had tropical cyclone impacts.</p>
          <script>ignore me</script>
          <p>High wind and flooding caused damage to homes and infrastructure.</p>
        </body></html>
        """

        blocks = extract_text_blocks_from_html(html)

        self.assertIn("Connecticut Summary", blocks[0])
        self.assertIn("tropical cyclone impacts", " ".join(blocks))
        self.assertNotIn("ignore me", " ".join(blocks))

    def test_extract_text_from_pdf_literal_fallback(self):
        pdf_bytes = (
            b"%PDF-1.4\n"
            b"1 0 obj <<>> stream\n"
            b"BT (Hurricane Isaias caused damaging winds, heavy rainfall, "
            b"flooding impacts, and power outages.) Tj ET\n"
            b"endstream endobj\n%%EOF"
        )

        text = extract_text_from_pdf(pdf_bytes)

        self.assertIn("Hurricane Isaias caused damaging winds", text)

    def test_readable_text_rejects_printable_pdf_garbage(self):
        garbage = "ù @ ÐÒy=? 2=É( " * 40

        self.assertFalse(is_readable_text(garbage))


class TestExternalSourceIndexAndSummary(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_index_external_documents_and_answer_impact(self):
        documents = [
            SourceDocument(
                url="https://www.ncei.noaa.gov/access/billions/state-summary/CT",
                title="Connecticut Summary",
                content=(
                    "Connecticut has been affected by billion-dollar tropical "
                    "cyclone events, severe storm events, and winter storm events. "
                    "High wind, flooding, and coastal impacts can damage homes, "
                    "businesses, vehicles, and infrastructure."
                ),
                content_type="text/html",
            ),
            SourceDocument(
                url="https://www.nhc.noaa.gov/data/tcr/AL092020_Isaias.pdf",
                title="Hurricane Isaias",
                content=(
                    "Hurricane Isaias produced damaging winds and heavy rainfall. "
                    "The storm caused power outages, flood damage, and multiple "
                    "direct deaths along the East Coast."
                ),
                content_type="application/pdf",
            ),
        ]

        summary = index_external_documents(
            self.conn,
            documents,
            rebuild=True,
            max_tokens=40,
            overlap_tokens=5,
        )
        answer = answer_storm_impact(
            self.conn,
            "impact of the new storm in Connecticut",
            limit=2,
            backend="exact",
            use_openai=False,
        )

        self.assertEqual(summary.documents_seen, 2)
        self.assertGreaterEqual(summary.total_chunks, 2)
        self.assertIn("damage", answer.answer.lower())
        self.assertIn("[1]", answer.answer)
        self.assertGreaterEqual(len(answer.cited_results), 1)

    def test_local_impact_summary_cleans_table_metadata(self):
        documents = [
            SourceDocument(
                url="https://www.ncei.noaa.gov/access/billions/state-summary/CT",
                title="Connecticut Summary",
                content=(
                    "$1.3 * CI 1 Tropical Storm Lee September 2011 Tropical Cyclone "
                    "September 1, 2011 September 5, 2011 Wind and flood damage "
                    "across the southeast and record flooding across the northeast."
                ),
                content_type="text/html",
            ),
        ]
        index_external_documents(
            self.conn,
            documents,
            rebuild=True,
            max_tokens=80,
            overlap_tokens=5,
        )

        answer = answer_storm_impact(
            self.conn,
            "impact of the new storm in Connecticut",
            limit=1,
            backend="exact",
            use_openai=False,
        )

        self.assertIn("Wind and flood damage", answer.answer)
        self.assertNotIn("$1.3", answer.answer)
        self.assertFalse(answer.used_openai)

    def test_search_filters_unreadable_chunks(self):
        documents = [
            SourceDocument(
                url="https://arxiv.org/pdf/2508.05652",
                title="Unreadable PDF",
                content=(
                    "\x11\x98\x17\xd0\x93\xf9\x13 storm impact damage "
                    "\x01=\x99?\x01 \x042\x01=\xc9("
                ),
                content_type="application/pdf",
            ),
            SourceDocument(
                url="https://www.ncei.noaa.gov/access/billions/state-summary/CT",
                title="Connecticut Summary",
                content=(
                    "High wind, flooding, and coastal impacts can damage homes, "
                    "businesses, vehicles, and infrastructure."
                ),
                content_type="text/html",
            ),
        ]
        index_external_documents(
            self.conn,
            documents,
            rebuild=True,
            max_tokens=40,
            overlap_tokens=5,
        )

        results = search_external_context(
            self.conn,
            "storm impact damage",
            limit=3,
            backend="exact",
        )

        self.assertTrue(is_readable_text(results[0].content))
        self.assertEqual(
            results[0].source_path,
            "https://www.ncei.noaa.gov/access/billions/state-summary/CT",
        )

    def test_search_can_filter_to_source_host(self):
        documents = [
            SourceDocument(
                url="https://arxiv.org/pdf/2508.05652",
                title="Outdoor Chatbot Paper",
                content="Storm impact language appears here but is not the requested source.",
                content_type="application/pdf",
            ),
            SourceDocument(
                url="https://www.ncei.noaa.gov/access/billions/state-summary/CT",
                title="Connecticut Summary",
                content="Connecticut storm impacts include high wind and flooding damage.",
                content_type="text/html",
            ),
        ]
        index_external_documents(
            self.conn,
            documents,
            rebuild=True,
            max_tokens=40,
            overlap_tokens=5,
        )

        results = search_external_context(
            self.conn,
            "storm impact damage",
            limit=3,
            backend="exact",
            source_urls=["https://www.ncei.noaa.gov/access/billions/state-summary/CT"],
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0].source_path,
            "https://www.ncei.noaa.gov/access/billions/state-summary/CT",
        )

    def test_answer_impact_uses_openai_summary_when_available(self):
        documents = [
            SourceDocument(
                url="https://www.nhc.noaa.gov/data/tcr/AL092020_Isaias.pdf",
                title="Hurricane Isaias",
                content=(
                    "Hurricane Isaias produced damaging winds and heavy rainfall. "
                    "The storm caused power outages and flood damage."
                ),
                content_type="application/pdf",
            ),
        ]
        index_external_documents(
            self.conn,
            documents,
            rebuild=True,
            max_tokens=40,
            overlap_tokens=5,
        )

        with patch(
            "wiser.external_sources.summarize_storm_impact_with_openai",
            return_value="- Model-written impact summary from retrieved evidence. [1]",
        ) as summarize:
            answer = answer_storm_impact(
                self.conn,
                "impact of the new storm",
                limit=1,
                backend="exact",
                use_openai=True,
                openai_model="gpt-test",
            )

        summarize.assert_called_once()
        self.assertIn("Model-written impact summary", answer.answer)
        self.assertEqual(len(answer.cited_results), 1)
        self.assertTrue(answer.used_openai)

    def test_answer_impact_reports_openai_fallback_error(self):
        documents = [
            SourceDocument(
                url="https://www.ncei.noaa.gov/access/billions/state-summary/CT",
                title="Connecticut Summary",
                content="Storm impacts included high wind, flooding, and infrastructure damage.",
                content_type="text/html",
            ),
        ]
        index_external_documents(
            self.conn,
            documents,
            rebuild=True,
            max_tokens=40,
            overlap_tokens=5,
        )

        with patch(
            "wiser.external_sources.summarize_storm_impact_with_openai",
            side_effect=OpenAISummarizationError("HTTP 404 model_not_found"),
        ):
            answer = answer_storm_impact(
                self.conn,
                "storm impact wind flooding damage",
                limit=1,
                backend="exact",
                use_openai=True,
                openai_model="gpt-test",
            )

        self.assertFalse(answer.used_openai)
        self.assertIn("HTTP 404", answer.openai_error)
        self.assertIn("High wind", answer.answer)


class TestSimilarityRanking(unittest.TestCase):
    def test_rank_texts_exact_prefers_relevant_impacts(self):
        texts = [
            "The database schema stores Storm and County rows.",
            "Hurricane winds caused flooding, damage, and power outages.",
            "A calm weather paragraph mentions no notable impacts.",
        ]

        matches = rank_texts(
            "storm impact wind damage outage",
            texts,
            limit=2,
            backend="exact",
        )

        self.assertEqual(matches[0].index, 1)
        self.assertGreater(matches[0].score, matches[1].score)


if __name__ == "__main__":
    unittest.main()
