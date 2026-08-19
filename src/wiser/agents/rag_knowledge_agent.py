from __future__ import annotations

import psycopg

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from wiser.rag import (
    DEFAULT_RAG_SOURCES,
    REPO_ROOT,
    RagAnswer,
    RagIndexSummary,
    answer_rag,
    index_sources,
)


@dataclass
class RAGKnowledgeAgent:
    sources: Sequence[str | Path] = DEFAULT_RAG_SOURCES
    root: str | Path = REPO_ROOT

    def ensure_index(
        self,
        conn: psycopg.Connection,
        *,
        rebuild: bool = False,
    ) -> RagIndexSummary:
        return index_sources(
            conn,
            self.sources,
            root=self.root,
            rebuild=rebuild,
        )

    def answer(
        self,
        conn: psycopg.Connection,
        query: str,
        *,
        limit: int = 5,
        max_statements: int = 4,
    ) -> RagAnswer:
        return answer_rag(
            conn,
            query,
            limit=limit,
            max_statements=max_statements,
        )
