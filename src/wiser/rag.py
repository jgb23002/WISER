from __future__ import annotations

import hashlib
import math
import re
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import psycopg

from wiser.database import executemany

# ---------------------------------------------------------------------------
# Process-level caches
# ---------------------------------------------------------------------------
#
# Three layers, all keyed by the connection's DSN so two databases in one
# process never share state, and all reset by the test harness:
#
# 1. _schema_ensured — the CREATE TABLE IF NOT EXISTS block runs once per
#    DSN, not once per query.
# 2. _index_manifest — (mtime_ns, size) of every file confirmed indexed. A
#    matching stat lets ensure_index skip the read + SHA-256 entirely; any
#    mismatch falls through to the checksum compare, which stays the source
#    of truth for whether a document is rewritten.
#
#    The tradeoff is the usual one for stat-based staleness (make, mypy, and
#    pytest's cache all take it): an edit that leaves both mtime and size
#    identical is not noticed. It is process-local, so a restart or
#    rebuild=True re-reads everything, and the corpus here is the repo's own
#    docs and source.
# 3. _search_cache — tokenized chunk records plus term document-frequencies,
#    valid for one "generation". Any write to the RAG tables (indexing here,
#    the external-sources indexer, a clear) bumps the generation.

_rag_cache_lock = threading.Lock()
_schema_ensured: set[str] = set()
_index_manifest: dict[tuple[str, str], tuple[int, int]] = {}
_search_cache: dict[str, tuple[int, list["_ChunkRecord"], Counter]] = {}
_rag_generation = 0


def _conn_scope(conn: psycopg.Connection) -> str:
    try:
        return str(conn.info.dsn)
    except Exception:  # noqa: BLE001 - fakes in tests may lack .info
        return ""


def bump_rag_generation() -> None:
    """Invalidate cached search records after any RAG-table write."""
    global _rag_generation
    with _rag_cache_lock:
        _rag_generation += 1
        _search_cache.clear()


def reset_rag_caches() -> None:
    """Drop every process-level RAG cache (test hook)."""
    global _rag_generation
    with _rag_cache_lock:
        _schema_ensured.clear()
        _index_manifest.clear()
        _search_cache.clear()
        _rag_generation += 1

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAG_SOURCES = ("README.md", "docs", "scripts", "sql", "src/wiser")
SUPPORTED_EXTENSIONS = frozenset({".md", ".py", ".sql", ".txt"})
IGNORED_DIR_NAMES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
    }
)
TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_]*|[0-9]+(?:\.[0-9]+)?")
CODE_QUERY_TERMS = frozenset(
    {
        "class",
        "cli",
        "code",
        "function",
        "implementation",
        "method",
        "module",
        "python",
        "script",
        "source",
    }
)
STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "do",
        "does",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "this",
        "to",
        "what",
        "when",
        "where",
        "which",
        "with",
    }
)


@dataclass(frozen=True)
class RagIndexSummary:
    documents_seen: int
    documents_indexed: int
    chunks_indexed: int
    total_documents: int
    total_chunks: int


@dataclass(frozen=True)
class RagResult:
    source_path: str
    title: str
    chunk_index: int
    score: float
    content: str


@dataclass(frozen=True)
class RagAnswer:
    question: str
    answer: str
    context: str
    results: tuple[RagResult, ...]
    cited_results: tuple[RagResult, ...]


def tokenize(text: str) -> list[str]:
    return [match.group(0).lower() for match in TOKEN_RE.finditer(text)]


def expand_query_terms(query: str) -> list[str]:
    raw_terms = tokenize(query)
    terms = [term for term in raw_terms if term not in STOP_WORDS]
    if not terms:
        terms = raw_terms

    expanded_terms = list(terms)
    if any(term in raw_terms for term in {"how", "work", "works"}):
        expanded_terms.extend(["overview", "approach", "algorithm"])
    if "similarity" in terms:
        expanded_terms.extend(["wind", "distance", "score"])
        if "algorithm" not in expanded_terms:
            expanded_terms.append("algorithm")
    return expanded_terms


def chunk_text(
    text: str,
    *,
    max_tokens: int = 180,
    overlap_tokens: int = 30,
) -> list[str]:
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    if overlap_tokens < 0:
        raise ValueError("overlap_tokens must not be negative")
    if overlap_tokens >= max_tokens:
        raise ValueError("overlap_tokens must be smaller than max_tokens")

    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", text)
        if paragraph.strip()
    ]
    chunks: list[str] = []
    current_parts: list[str] = []
    current_count = 0

    def flush_current() -> None:
        nonlocal current_parts, current_count
        chunk = "\n\n".join(current_parts).strip()
        if chunk:
            chunks.append(chunk)
        if overlap_tokens and chunk:
            overlap_text = " ".join(chunk.split()[-overlap_tokens:])
            current_parts = [overlap_text] if overlap_text else []
            current_count = len(tokenize(overlap_text))
        else:
            current_parts = []
            current_count = 0

    for paragraph in paragraphs:
        paragraph_tokens = tokenize(paragraph)
        paragraph_count = len(paragraph_tokens)
        if paragraph_count > max_tokens:
            if current_parts:
                flush_current()
                current_parts = []
                current_count = 0

            words = paragraph.split()
            step = max_tokens - overlap_tokens
            for start in range(0, len(words), step):
                word_chunk = " ".join(words[start : start + max_tokens]).strip()
                if word_chunk:
                    chunks.append(word_chunk)
            continue

        if current_parts and current_count + paragraph_count > max_tokens:
            flush_current()

        current_parts.append(paragraph)
        current_count += paragraph_count

    if current_parts:
        chunk = "\n\n".join(current_parts).strip()
        if chunk:
            chunks.append(chunk)
    return chunks


def initialize_rag_schema(conn: psycopg.Connection) -> None:
    scope = _conn_scope(conn)
    if scope:
        with _rag_cache_lock:
            if scope in _schema_ensured:
                return
    # PostgreSQL enforces foreign keys unconditionally, and a parameterless
    # execute() accepts the whole multi-statement block in one round trip.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS RagDocument (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            checksum TEXT NOT NULL,
            indexedAt TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS RagChunk (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            documentId INTEGER NOT NULL,
            chunkIndex INTEGER NOT NULL,
            content TEXT NOT NULL,
            tokenCount INTEGER NOT NULL,
            UNIQUE (documentId, chunkIndex),
            FOREIGN KEY (documentId) REFERENCES RagDocument(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_rag_document_path ON RagDocument(path);
        CREATE INDEX IF NOT EXISTS idx_rag_chunk_document ON RagChunk(documentId);
        """
    )
    conn.commit()
    if scope:
        with _rag_cache_lock:
            _schema_ensured.add(scope)


def clear_rag_index(conn: psycopg.Connection) -> None:
    initialize_rag_schema(conn)
    conn.execute("DELETE FROM RagChunk")
    conn.execute("DELETE FROM RagDocument")
    conn.commit()
    with _rag_cache_lock:
        _index_manifest.clear()
    bump_rag_generation()


def resolve_source_paths(
    sources: Sequence[str | Path],
    *,
    root: str | Path = REPO_ROOT,
) -> list[Path]:
    root_path = Path(root).resolve()
    seen: set[Path] = set()
    files: list[Path] = []

    for source in sources:
        source_path = Path(source)
        if not source_path.is_absolute():
            source_path = root_path / source_path
        if not source_path.exists():
            continue

        candidate_paths = [source_path] if source_path.is_file() else source_path.rglob("*")
        for candidate_path in candidate_paths:
            if not candidate_path.is_file():
                continue
            if candidate_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            if _is_ignored(candidate_path, root_path):
                continue
            resolved_path = candidate_path.resolve()
            if resolved_path not in seen:
                seen.add(resolved_path)
                files.append(resolved_path)

    return sorted(files, key=lambda path: _source_path(path, root_path))


def index_sources(
    conn: psycopg.Connection,
    sources: Sequence[str | Path] = DEFAULT_RAG_SOURCES,
    *,
    root: str | Path = REPO_ROOT,
    rebuild: bool = False,
    max_tokens: int = 180,
    overlap_tokens: int = 30,
) -> RagIndexSummary:
    initialize_rag_schema(conn)
    if rebuild:
        clear_rag_index(conn)

    scope = _conn_scope(conn)
    root_path = Path(root).resolve()
    paths = resolve_source_paths(sources, root=root_path)
    documents_indexed = 0
    chunks_indexed = 0

    # One catalog query instead of one SELECT per file.
    existing_by_path: dict[str, tuple[int, str, int]] = {
        str(row[0]): (int(row[1]), str(row[2]), int(row[3]))
        for row in conn.execute(
            """
            SELECT d.path, d.id, d.checksum, COUNT(c.id)
            FROM RagDocument d
            LEFT JOIN RagChunk c ON c.documentId = d.id
            GROUP BY d.path, d.id, d.checksum
            """
        ).fetchall()
    }

    manifest_updates: dict[tuple[str, str], tuple[int, int]] = {}
    wrote_documents = False
    for path in paths:
        try:
            stat = path.stat()
            stat_key: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stat_key = None
        if stat_key is not None:
            with _rag_cache_lock:
                fresh = _index_manifest.get((scope, str(path))) == stat_key
            if fresh:
                # Confirmed indexed with these exact bytes on a previous pass
                # in this process; skip the read + SHA-256 entirely.
                continue

        content = path.read_text(encoding="utf-8", errors="ignore")
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        source_path = _source_path(path, root_path)
        title = _extract_title(content, path)
        existing = existing_by_path.get(source_path)

        if existing and existing[1] == checksum and existing[2] > 0:
            if stat_key is not None:
                manifest_updates[(scope, str(path))] = stat_key
            continue

        indexed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if existing:
            document_id = int(existing[0])
            conn.execute("DELETE FROM RagChunk WHERE documentId = %s", (document_id,))
            conn.execute(
                """
                UPDATE RagDocument
                SET title = %s, checksum = %s, indexedAt = %s
                WHERE id = %s
                """,
                (title, checksum, indexed_at, document_id),
            )
        else:
            cursor = conn.execute(
                """
                INSERT INTO RagDocument(path, title, checksum, indexedAt)
                VALUES(%s, %s, %s, %s)
                RETURNING id
                """,
                (source_path, title, checksum, indexed_at),
            )
            # PostgreSQL has no lastrowid; RETURNING is the portable form.
            document_id = int(cursor.fetchone()[0])

        chunks = chunk_text(
            content,
            max_tokens=max_tokens,
            overlap_tokens=overlap_tokens,
        )
        executemany(
            conn,
            """
            INSERT INTO RagChunk(documentId, chunkIndex, content, tokenCount)
            VALUES(%s, %s, %s, %s)
            """,
            [
                (document_id, chunk_index, chunk, len(tokenize(chunk)))
                for chunk_index, chunk in enumerate(chunks)
            ],
        )
        documents_indexed += 1
        chunks_indexed += len(chunks)
        wrote_documents = True
        if stat_key is not None:
            manifest_updates[(scope, str(path))] = stat_key

    conn.commit()
    # Manifest entries only land after the commit: an aborted pass must not
    # leave the process believing files are indexed.
    if manifest_updates:
        with _rag_cache_lock:
            _index_manifest.update(manifest_updates)
    if wrote_documents:
        bump_rag_generation()
    total_documents = int(conn.execute("SELECT COUNT(*) FROM RagDocument").fetchone()[0])
    total_chunks = int(conn.execute("SELECT COUNT(*) FROM RagChunk").fetchone()[0])
    return RagIndexSummary(
        documents_seen=len(paths),
        documents_indexed=documents_indexed,
        chunks_indexed=chunks_indexed,
        total_documents=total_documents,
        total_chunks=total_chunks,
    )


@dataclass(frozen=True)
class _ChunkRecord:
    """One chunk, tokenized exactly once per index generation."""

    source_path: str
    title: str
    chunk_index: int
    content: str
    token_count: int
    content_counter: Counter
    title_counter: Counter
    path_counter: Counter
    normalized_content: str
    normalized_title: str
    numeric_ratio: float


def _chunk_records(
    conn: psycopg.Connection,
) -> tuple[list[_ChunkRecord], Counter]:
    """All chunks with their token structures and term document-frequencies.

    Cached per DSN and per index generation: retrieval used to re-tokenize
    every chunk three times per query and rebuild the frequency table each
    time, for a corpus that only changes when something is indexed.
    """
    scope = _conn_scope(conn)
    with _rag_cache_lock:
        generation = _rag_generation
        entry = _search_cache.get(scope)
        if entry is not None and entry[0] == generation:
            return entry[1], entry[2]

    rows = conn.execute(
        """
        SELECT
            d.path,
            d.title,
            c.chunkIndex,
            c.content,
            c.tokenCount
        FROM RagChunk c
        JOIN RagDocument d ON d.id = c.documentId
        """
    ).fetchall()

    records: list[_ChunkRecord] = []
    document_frequency: Counter[str] = Counter()
    for row in rows:
        content = str(row[3])
        title = str(row[1])
        source_path = str(row[0])
        content_tokens = tokenize(content)
        title_tokens = tokenize(title)
        path_tokens = tokenize(source_path)
        document_frequency.update(
            set(content_tokens) | set(title_tokens) | set(path_tokens)
        )
        numeric_ratio = (
            sum(1 for token in content_tokens if token[0].isdigit())
            / len(content_tokens)
            if content_tokens
            else -1.0
        )
        records.append(
            _ChunkRecord(
                source_path=source_path,
                title=title,
                chunk_index=int(row[2]),
                content=content,
                token_count=max(1, int(row[4])),
                content_counter=Counter(content_tokens),
                title_counter=Counter(title_tokens),
                path_counter=Counter(path_tokens),
                normalized_content=" ".join(content_tokens),
                normalized_title=" ".join(title_tokens),
                numeric_ratio=numeric_ratio,
            )
        )

    with _rag_cache_lock:
        if scope:
            _search_cache[scope] = (generation, records, document_frequency)
    return records, document_frequency


def search_rag(
    conn: psycopg.Connection,
    query: str,
    *,
    limit: int = 5,
) -> list[RagResult]:
    if limit <= 0:
        return []

    initialize_rag_schema(conn)
    query_terms = expand_query_terms(query)
    if not query_terms:
        return []

    query_counter = Counter(query_terms)
    records, document_frequency = _chunk_records(conn)

    total_chunks = max(1, len(records))
    results: list[RagResult] = []
    normalized_query = " ".join(query_terms)
    for record in records:
        score = 0.0
        for term, query_count in query_counter.items():
            weighted_occurrences = (
                record.content_counter[term]
                + (3.0 * record.title_counter[term])
                + (2.0 * record.path_counter[term])
            )
            if weighted_occurrences <= 0:
                continue
            weighted_occurrences = min(weighted_occurrences, 4.0)
            inverse_document_frequency = (
                math.log((1.0 + total_chunks) / (1.0 + document_frequency[term])) + 1.0
            )
            score += query_count * weighted_occurrences * inverse_document_frequency

        if normalized_query and normalized_query in record.normalized_content:
            score += 4.0
        if normalized_query and normalized_query in record.normalized_title:
            score += 6.0

        if score <= 0:
            continue

        score = score / math.sqrt(record.token_count)
        score *= _content_quality_multiplier(
            record.content,
            record.source_path,
            query_terms,
            numeric_ratio=record.numeric_ratio,
        )
        results.append(
            RagResult(
                source_path=record.source_path,
                title=record.title,
                chunk_index=record.chunk_index,
                score=score,
                content=record.content.strip(),
            )
        )

    return sorted(
        results,
        key=lambda result: (-result.score, result.source_path, result.chunk_index),
    )[:limit]


def build_augmented_context(
    query: str,
    results: Sequence[RagResult],
    *,
    max_chars_per_chunk: int = 900,
) -> str:
    lines = [f"Question: {query}", "", "Retrieved context:"]
    for position, result in enumerate(results, start=1):
        lines.append(
            f"[{position}] {result.source_path}#chunk-{result.chunk_index} "
            f"(score={result.score:.3f})"
        )
        lines.append(format_excerpt(result.content, max_chars=max_chars_per_chunk))
        lines.append("")
    return "\n".join(lines).rstrip()


def answer_rag(
    conn: psycopg.Connection,
    query: str,
    *,
    limit: int = 5,
    max_statements: int = 4,
) -> RagAnswer:
    results = tuple(search_rag(conn, query, limit=limit))
    return synthesize_grounded_answer(
        query,
        results,
        max_statements=max_statements,
    )


def synthesize_grounded_answer(
    query: str,
    results: Sequence[RagResult],
    *,
    max_statements: int = 4,
) -> RagAnswer:
    context = build_augmented_context(query, results)
    if not results:
        return RagAnswer(
            question=query,
            answer=(
                "I do not have enough indexed context to answer this question. "
                "Rebuild the RAG index or add more sources."
            ),
            context=context,
            results=tuple(),
            cited_results=tuple(),
        )

    query_counter = Counter(expand_query_terms(query))
    ranked_statements: list[tuple[float, int, str]] = []
    seen_statements: set[str] = set()

    for result_position, result in enumerate(results, start=1):
        for statement in _candidate_statements(result.content):
            normalized_statement = " ".join(tokenize(statement))
            if not normalized_statement or normalized_statement in seen_statements:
                continue
            seen_statements.add(normalized_statement)

            statement_counter = Counter(tokenize(statement))
            if _is_low_value_statement(statement):
                continue
            overlap = sum(
                min(query_count, statement_counter[term])
                for term, query_count in query_counter.items()
            )
            if overlap == 0:
                continue

            title_overlap = sum(
                1 for term in query_counter if term in tokenize(result.title)
            )
            source_boost = 1.0 / result_position
            score = overlap + (0.25 * title_overlap) + source_boost
            ranked_statements.append((score, result_position, statement))

    if not ranked_statements:
        fallback = format_excerpt(results[0].content, max_chars=450)
        answer = f"{fallback} [1]"
        cited_positions = [1]
    else:
        ranked_statements.sort(key=lambda item: (-item[0], item[1], item[2]))
        selected = ranked_statements[:max(1, max_statements)]
        selected.sort(key=lambda item: item[1])
        cited_positions: list[int] = []
        for _, source_position, _ in selected:
            if source_position not in cited_positions:
                cited_positions.append(source_position)
        citation_number_by_position = {
            source_position: citation_number
            for citation_number, source_position in enumerate(cited_positions, start=1)
        }
        answer_lines = [
            f"- {statement} [{citation_number_by_position[source_position]}]"
            for _, source_position, statement in selected
        ]
        answer = "\n".join(answer_lines)

    cited_results = tuple(results[source_position - 1] for source_position in cited_positions)
    return RagAnswer(
        question=query,
        answer=answer,
        context=context,
        results=tuple(results),
        cited_results=cited_results,
    )


def format_excerpt(text: str, *, max_chars: int = 500) -> str:
    excerpt = re.sub(r"\s+", " ", text).strip()
    if len(excerpt) <= max_chars:
        return excerpt
    return excerpt[: max(0, max_chars - 3)].rstrip() + "..."


def _extract_title(content: str, path: Path) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title:
                return title
    return path.stem.replace("_", " ").replace("-", " ").title()


def _source_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _is_ignored(path: Path, root: Path) -> bool:
    try:
        relative_parts = path.resolve().relative_to(root.resolve()).parts
    except ValueError:
        relative_parts = path.parts
    return any(part in IGNORED_DIR_NAMES or part.startswith(".") for part in relative_parts)


def _candidate_statements(text: str) -> list[str]:
    statements: list[str] = []
    in_code_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        if "```" in stripped:
            stripped = stripped.replace("```", " ")
        stripped = stripped.lstrip("#").strip()
        stripped = stripped.lstrip("-*0123456789. ").strip()
        if not stripped or stripped in {"```", "---"}:
            continue
        if stripped.startswith("|") or stripped.startswith("```"):
            continue
        stripped = re.sub(r"`([^`]+)`", r"\1", stripped)
        stripped = stripped.replace("**", "")
        for sentence in re.split(r"(?<=[.!?])\s+", stripped):
            sentence = re.sub(r"\s+", " ", sentence).strip()
            if len(tokenize(sentence)) < 5:
                continue
            statements.append(sentence)
    return statements


def _content_quality_multiplier(
    content: str,
    source_path: str,
    query_terms: Sequence[str],
    *,
    numeric_ratio: float | None = None,
) -> float:
    """Down/up-weight a chunk by shape. ``numeric_ratio`` accepts the value
    precomputed at index time (−1.0 marks an empty token list) so retrieval
    does not re-tokenize the chunk just for this check."""
    if numeric_ratio is None:
        tokens = tokenize(content)
        if not tokens:
            return 0.0
        numeric_ratio = sum(1 for token in tokens if token[0].isdigit()) / len(tokens)
    elif numeric_ratio < 0:
        return 0.0

    lower_content = content.lower()
    lower_source_path = source_path.lower()
    query_term_set = set(query_terms)
    wants_code = bool(query_term_set & CODE_QUERY_TERMS)
    multiplier = 1.0

    if lower_source_path.startswith("docs/"):
        multiplier *= 1.3
    if lower_source_path.endswith(".py") and not wants_code:
        multiplier *= 0.35
    elif lower_source_path.endswith(".py"):
        multiplier *= 0.85

    if numeric_ratio > 0.25:
        multiplier *= 0.55
    if "```" in content:
        multiplier *= 0.8
    if "example output" in lower_content:
        multiplier *= 0.6
    if "canonical documentation lives" in lower_content:
        multiplier *= 0.45
    return multiplier


def _is_low_value_statement(statement: str) -> bool:
    lower_statement = statement.lower()
    low_value_phrases = (
        "canonical documentation lives",
        "example output",
        "file structure",
        "rank storm code",
        "python scripts/",
        "with custom database path",
        "black-box",
        "training data",
        "training overhead",
    )
    if any(phrase in lower_statement for phrase in low_value_phrases):
        return True
    if lower_statement.startswith("step ") or lower_statement.startswith("example "):
        return True
    if re.search(r"\b(class|def|import|return)\b|[a-zA-Z_]+\(", statement):
        return True
    if re.search(r"^\s*(elif|else|for|if|while)\b", statement):
        return True

    tokens = tokenize(statement)
    if not tokens:
        return True
    numeric_ratio = sum(1 for token in tokens if token[0].isdigit()) / len(tokens)
    return numeric_ratio > 0.35
