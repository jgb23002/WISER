import sys
import tempfile
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).parent.parent / "src"
TESTS_DIR = Path(__file__).parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from pg_harness import executemany, make_conn, new_connection  # noqa: E402,F401

from wiser.rag import (  # noqa: E402
    answer_rag,
    build_augmented_context,
    chunk_text,
    index_sources,
    resolve_source_paths,
    search_rag,
)


class TestRagChunking(unittest.TestCase):
    def test_chunk_text_respects_token_limit(self):
        text = " ".join(f"token{i}" for i in range(40))

        chunks = chunk_text(text, max_tokens=12, overlap_tokens=2)

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.split()), 12)

    def test_chunk_text_rejects_invalid_overlap(self):
        with self.assertRaises(ValueError):
            chunk_text("sample text", max_tokens=10, overlap_tokens=10)


class TestRagIndexAndSearch(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        docs_dir = self.root / "docs"
        docs_dir.mkdir()
        src_dir = self.root / "src" / "wiser"
        src_dir.mkdir(parents=True)
        (docs_dir / "storm.md").write_text(
            "# Storm Similarity\n\n"
            "The storm similarity algorithm compares peak wind speed values "
            "for a county and ranks historical outages by statistical distance.",
            encoding="utf-8",
        )
        (docs_dir / "database.md").write_text(
            "# Database\n\n"
            "The SQLite schema stores Storm, County, and WeatherData rows.",
            encoding="utf-8",
        )
        (src_dir / "rag.py").write_text(
            "def expand_query_terms(query):\n"
            "    if 'similarity' in query:\n"
            "        return ['algorithm', 'wind', 'distance', 'score']\n",
            encoding="utf-8",
        )
        self.conn = make_conn()

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_resolve_source_paths_filters_supported_files(self):
        (self.root / "docs" / "diagram.png").write_text("not indexed", encoding="utf-8")

        paths = resolve_source_paths(["docs"], root=self.root)

        self.assertEqual(
            [path.relative_to(self.root).as_posix() for path in paths],
            ["docs/database.md", "docs/storm.md"],
        )

    def test_index_sources_and_search_returns_best_context(self):
        summary = index_sources(
            self.conn,
            ["docs"],
            root=self.root,
            rebuild=True,
            max_tokens=40,
            overlap_tokens=5,
        )

        results = search_rag(
            self.conn,
            "storm similarity wind speed outage",
            limit=2,
        )

        self.assertEqual(summary.documents_seen, 2)
        self.assertEqual(summary.total_documents, 2)
        self.assertGreaterEqual(summary.total_chunks, 2)
        self.assertEqual(results[0].source_path, "docs/storm.md")
        self.assertIn("peak wind speed", results[0].content)

    def test_index_sources_refreshes_changed_document(self):
        index_sources(self.conn, ["docs"], root=self.root, rebuild=True)
        (self.root / "docs" / "storm.md").write_text(
            "# Storm Similarity\n\nFairfield outage context now mentions crews.",
            encoding="utf-8",
        )

        summary = index_sources(self.conn, ["docs"], root=self.root)
        results = search_rag(self.conn, "Fairfield crews", limit=1)

        self.assertEqual(summary.documents_indexed, 1)
        self.assertIn("Fairfield outage context", results[0].content)

    def test_build_augmented_context_includes_source_labels(self):
        index_sources(self.conn, ["docs"], root=self.root, rebuild=True)
        results = search_rag(self.conn, "sqlite schema", limit=1)

        context = build_augmented_context("sqlite schema", results)

        self.assertIn("Question: sqlite schema", context)
        self.assertIn("docs/database.md#chunk-0", context)

    def test_answer_rag_returns_grounded_answer_with_citations(self):
        index_sources(self.conn, ["docs"], root=self.root, rebuild=True)

        answer = answer_rag(
            self.conn,
            "how does storm similarity rank outage events",
            limit=2,
        )

        self.assertIn("peak wind speed", answer.answer)
        self.assertIn("[1]", answer.answer)
        self.assertEqual(answer.results[0].source_path, "docs/storm.md")
        self.assertEqual(answer.cited_results[0].source_path, "docs/storm.md")
        self.assertIn("Retrieved context:", answer.context)

    def test_answer_rag_handles_empty_query(self):
        index_sources(self.conn, ["docs"], root=self.root, rebuild=True)

        answer = answer_rag(self.conn, "", limit=2)

        self.assertEqual(answer.results, ())
        self.assertEqual(answer.cited_results, ())
        self.assertIn("not have enough indexed context", answer.answer)

    def test_explanation_queries_prefer_docs_over_rag_code(self):
        (self.root / "docs" / "use_cases.md").write_text(
            "# Similarity Use Cases\n\n"
            "When a current storm has wind speed data, WISER uses historical "
            "storms with similar wind distance and score to estimate outages.",
            encoding="utf-8",
        )
        index_sources(self.conn, ["docs", "src/wiser"], root=self.root, rebuild=True)

        results = search_rag(
            self.conn,
            "how does storm similarity work",
            limit=2,
        )
        answer = answer_rag(
            self.conn,
            "how does storm similarity work",
            limit=2,
        )

        self.assertEqual(results[0].source_path, "docs/storm.md")
        self.assertNotIn("src/wiser/rag.py", [result.source_path for result in results])
        self.assertNotIn(
            "src/wiser/rag.py",
            [result.source_path for result in answer.cited_results],
        )
        self.assertNotIn("expand_query_terms", answer.answer)

    def test_cited_results_match_answer_citation_numbers(self):
        (self.root / "docs" / "use_cases.md").write_text(
            "# Similarity Use Cases\n\n"
            "When a current storm has wind speed data, WISER estimates "
            "outages by comparing similar historical storm scores.",
            encoding="utf-8",
        )
        index_sources(self.conn, ["docs"], root=self.root, rebuild=True)

        answer = answer_rag(
            self.conn,
            "how does storm similarity estimate outages",
            limit=3,
        )

        for citation_number, _ in enumerate(answer.cited_results, start=1):
            self.assertIn(f"[{citation_number}]", answer.answer)
        self.assertNotIn(f"[{len(answer.cited_results) + 1}]", answer.answer)


if __name__ == "__main__":
    unittest.main()
