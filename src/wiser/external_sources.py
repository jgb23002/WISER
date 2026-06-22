from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import hashlib
import html
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
from typing import Callable, Sequence
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from wiser.openai_summarizer import (
    OpenAISummarizationError,
    summarize_storm_impact_with_openai,
)
from wiser.rag import (
    RagIndexSummary,
    RagResult,
    build_augmented_context,
    chunk_text,
    format_excerpt,
    initialize_rag_schema,
    tokenize,
)
from wiser.similarity import SimilarityBackend, preferred_backend_name, rank_texts

DEFAULT_STORM_CONTEXT_URLS = (
    "https://www.nhc.noaa.gov/data/tcr/index.php?season=2020&basin=atl",
    "https://www.nhc.noaa.gov/data/tcr/index.php?season=2020&basin=epac",
    "https://www.ncei.noaa.gov/access/billions/state-summary/CT",
)
HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
PDF_CONTENT_TYPES = frozenset({"application/pdf", "application/x-pdf"})
IMPACT_TERMS = frozenset(
    {
        "casualties",
        "coastal",
        "cost",
        "damage",
        "damages",
        "death",
        "deaths",
        "fatalities",
        "flood",
        "flooding",
        "hail",
        "impact",
        "impacts",
        "outage",
        "outages",
        "rainfall",
        "storm",
        "surge",
        "tornado",
        "tropical",
        "wind",
        "winds",
    }
)


@dataclass(frozen=True)
class FetchedResource:
    url: str
    content: bytes
    content_type: str


@dataclass(frozen=True)
class SourceLink:
    url: str
    title: str


@dataclass(frozen=True)
class SourceDocument:
    url: str
    title: str
    content: str
    content_type: str


@dataclass(frozen=True)
class CrawlResult:
    documents: tuple[SourceDocument, ...]
    failures: tuple[str, ...]


@dataclass(frozen=True)
class ExternalSourceIndexSummary:
    documents_seen: int
    documents_indexed: int
    chunks_indexed: int
    total_documents: int
    total_chunks: int
    failures: tuple[str, ...]


@dataclass(frozen=True)
class StormImpactAnswer:
    question: str
    answer: str
    context: str
    results: tuple[RagResult, ...]
    cited_results: tuple[RagResult, ...]
    used_openai: bool = False
    openai_error: str | None = None


Fetcher = Callable[[str], FetchedResource]


def fetch_url(url: str, *, timeout: int = 20) -> FetchedResource:
    normalized_url = normalize_url(url)
    request = Request(
        normalized_url,
        headers={
            "User-Agent": (
                "WISER storm impact context crawler "
                "(research prototype; contact project owner)"
            )
        },
    )
    with urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get_content_type()
        return FetchedResource(
            url=response.geturl(),
            content=response.read(),
            content_type=content_type,
        )


def crawl_source_documents(
    urls: Sequence[str] = DEFAULT_STORM_CONTEXT_URLS,
    *,
    include_linked_pdfs: bool = True,
    max_linked_pdfs: int = 50,
    fetcher: Fetcher | None = None,
) -> CrawlResult:
    fetch = fetcher or fetch_url
    documents: list[SourceDocument] = []
    failures: list[str] = []
    seen_urls: set[str] = set()

    for url in urls:
        normalized_url = normalize_url(url)
        if normalized_url in seen_urls:
            continue
        seen_urls.add(normalized_url)

        try:
            fetched = fetch(normalized_url)
        except Exception as exc:  # pragma: no cover - depends on remote services
            failures.append(f"{normalized_url}: {exc}")
            continue

        document = source_document_from_resource(fetched)
        if document.content:
            documents.append(document)

        if not include_linked_pdfs or max_linked_pdfs <= 0:
            continue
        if not _looks_like_html(fetched):
            continue

        html_text = fetched.content.decode("utf-8", errors="ignore")
        linked_pdfs = discover_pdf_links(html_text, fetched.url)
        for link in linked_pdfs[:max_linked_pdfs]:
            if link.url in seen_urls:
                continue
            seen_urls.add(link.url)
            try:
                linked_resource = fetch(link.url)
            except Exception as exc:  # pragma: no cover - depends on remote services
                failures.append(f"{link.url}: {exc}")
                continue
            linked_document = source_document_from_resource(
                linked_resource,
                title_hint=link.title,
            )
            if linked_document.content:
                documents.append(linked_document)

    return CrawlResult(documents=tuple(documents), failures=tuple(failures))


def index_external_sources(
    conn: sqlite3.Connection,
    urls: Sequence[str] = DEFAULT_STORM_CONTEXT_URLS,
    *,
    rebuild: bool = False,
    include_linked_pdfs: bool = True,
    max_linked_pdfs: int = 50,
    fetcher: Fetcher | None = None,
) -> ExternalSourceIndexSummary:
    crawl_result = crawl_source_documents(
        urls,
        include_linked_pdfs=include_linked_pdfs,
        max_linked_pdfs=max_linked_pdfs,
        fetcher=fetcher,
    )
    summary = index_external_documents(
        conn,
        crawl_result.documents,
        rebuild=rebuild,
    )
    return ExternalSourceIndexSummary(
        documents_seen=summary.documents_seen,
        documents_indexed=summary.documents_indexed,
        chunks_indexed=summary.chunks_indexed,
        total_documents=summary.total_documents,
        total_chunks=summary.total_chunks,
        failures=crawl_result.failures,
    )


def index_external_documents(
    conn: sqlite3.Connection,
    documents: Sequence[SourceDocument],
    *,
    rebuild: bool = False,
    max_tokens: int = 180,
    overlap_tokens: int = 30,
) -> RagIndexSummary:
    initialize_rag_schema(conn)
    if rebuild:
        _clear_external_documents(conn)

    documents_indexed = 0
    chunks_indexed = 0
    for document in documents:
        content = normalize_text(document.content)
        if not content:
            continue

        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        existing = conn.execute(
            """
            SELECT id, checksum, (
                SELECT COUNT(*) FROM RagChunk WHERE documentId = RagDocument.id
            ) AS chunk_count
            FROM RagDocument
            WHERE path = ?
            """,
            (document.url,),
        ).fetchone()
        if existing and existing[1] == checksum and int(existing[2]) > 0:
            continue

        if existing:
            document_id = int(existing[0])
            conn.execute("DELETE FROM RagChunk WHERE documentId = ?", (document_id,))
            conn.execute(
                """
                UPDATE RagDocument
                SET title = ?, checksum = ?, indexedAt = datetime('now')
                WHERE id = ?
                """,
                (document.title, checksum, document_id),
            )
        else:
            cursor = conn.execute(
                """
                INSERT INTO RagDocument(path, title, checksum, indexedAt)
                VALUES(?, ?, ?, datetime('now'))
                """,
                (document.url, document.title, checksum),
            )
            document_id = int(cursor.lastrowid)

        chunks = chunk_text(
            content,
            max_tokens=max_tokens,
            overlap_tokens=overlap_tokens,
        )
        chunks = [chunk for chunk in chunks if is_readable_text(chunk)]
        if not chunks:
            continue
        conn.executemany(
            """
            INSERT INTO RagChunk(documentId, chunkIndex, content, tokenCount)
            VALUES(?, ?, ?, ?)
            """,
            [
                (document_id, chunk_index, chunk, len(tokenize(chunk)))
                for chunk_index, chunk in enumerate(chunks)
            ],
        )
        documents_indexed += 1
        chunks_indexed += len(chunks)

    conn.commit()
    total_documents = int(conn.execute("SELECT COUNT(*) FROM RagDocument").fetchone()[0])
    total_chunks = int(conn.execute("SELECT COUNT(*) FROM RagChunk").fetchone()[0])
    return RagIndexSummary(
        documents_seen=len(documents),
        documents_indexed=documents_indexed,
        chunks_indexed=chunks_indexed,
        total_documents=total_documents,
        total_chunks=total_chunks,
    )


def source_document_from_resource(
    resource: FetchedResource,
    *,
    title_hint: str | None = None,
) -> SourceDocument:
    if _looks_like_pdf(resource):
        content = extract_text_from_pdf(resource.content)
        title = title_hint or _title_from_url(resource.url)
        return SourceDocument(
            url=resource.url,
            title=title,
            content=content,
            content_type="application/pdf",
        )

    text = resource.content.decode("utf-8", errors="ignore")
    blocks = extract_text_blocks_from_html(text)
    title = title_hint or extract_title_from_html(text) or _title_from_url(resource.url)
    return SourceDocument(
        url=resource.url,
        title=title,
        content="\n\n".join(blocks),
        content_type=resource.content_type,
    )


def discover_pdf_links(html_text: str, base_url: str) -> list[SourceLink]:
    links: list[SourceLink] = []
    seen: set[str] = set()
    list_items = re.findall(
        r"<li\b[^>]*>(.*?)</li>",
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    for list_item in list_items:
        match = re.search(
            r"<a\b[^>]*href=[\"']([^\"']+\.pdf(?:\?[^\"']*)?)[\"'][^>]*>"
            r"\s*PDF\s*</a>",
            list_item,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            continue
        url = urljoin(base_url, html.unescape(match.group(1)))
        if url in seen:
            continue
        item_text = strip_html(list_item)
        title = re.split(r"\bPDF\b", item_text, maxsplit=1, flags=re.IGNORECASE)[0]
        title = title.strip(" -\u2013\t\n") or _title_from_url(url)
        links.append(SourceLink(url=url, title=title))
        seen.add(url)

    if links:
        return links

    for match in re.finditer(
        r"<a\b[^>]*href=[\"']([^\"']+\.pdf(?:\?[^\"']*)?)[\"'][^>]*>(.*?)</a>",
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        url = urljoin(base_url, html.unescape(match.group(1)))
        if url in seen:
            continue
        title = strip_html(match.group(2)) or _title_from_url(url)
        links.append(SourceLink(url=url, title=title))
        seen.add(url)
    return links


def extract_text_blocks_from_html(html_text: str) -> list[str]:
    cleaned = re.sub(
        r"<(script|style|noscript|svg)\b[^>]*>.*?</\1>",
        " ",
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(r"<br\s*/?>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"</(p|div|li|tr|h[1-6]|section|article|table)>",
        "\n\n",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    text = html.unescape(cleaned)

    blocks = []
    for block in re.split(r"\n\s*\n", text):
        normalized = normalize_text(block)
        if len(tokenize(normalized)) < 2:
            continue
        blocks.append(normalized)
    return blocks


def extract_title_from_html(html_text: str) -> str | None:
    for pattern in (
        r"<h1\b[^>]*>(.*?)</h1>",
        r"<title\b[^>]*>(.*?)</title>",
    ):
        match = re.search(pattern, html_text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            title = strip_html(match.group(1))
            if title:
                return title
    return None


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return normalize_text(html.unescape(text))


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    for extractor in (
        _extract_pdf_with_pypdf,
        _extract_pdf_with_pdftotext,
        _extract_pdf_literal_strings,
    ):
        text = extractor(pdf_bytes)
        if len(tokenize(text)) >= 8:
            return normalize_pdf_text(text)
    return ""


def normalize_pdf_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"-\n(?=[a-z])", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return normalize_text(text)


def normalize_text(text: str) -> str:
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n\s+", "\n", text)
    return text.strip()


def search_external_context(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 5,
    backend: SimilarityBackend = "auto",
    source_urls: Sequence[str] | None = None,
) -> list[RagResult]:
    if limit <= 0:
        return []

    initialize_rag_schema(conn)
    rows = conn.execute(
        """
        SELECT
            d.path,
            d.title,
            c.chunkIndex,
            c.content
        FROM RagChunk c
        JOIN RagDocument d ON d.id = c.documentId
        WHERE d.path LIKE 'http://%' OR d.path LIKE 'https://%'
        """
    ).fetchall()
    if not rows:
        return []

    source_hosts = _source_hosts(source_urls or ())
    filtered_rows = []
    for row in rows:
        source_path = str(row[0])
        content = str(row[3])
        if source_hosts and urlparse(source_path).netloc.lower() not in source_hosts:
            continue
        if not is_readable_text(content):
            continue
        filtered_rows.append(row)
    if not filtered_rows:
        return []

    texts = [str(row[3]) for row in filtered_rows]
    matches = rank_texts(query, texts, limit=limit, backend=backend)
    results: list[RagResult] = []
    for match in matches:
        row = filtered_rows[match.index]
        results.append(
            RagResult(
                source_path=str(row[0]),
                title=str(row[1]),
                chunk_index=int(row[2]),
                score=match.score,
                content=str(row[3]).strip(),
            )
        )
    return results


def answer_storm_impact(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 5,
    max_sentences: int = 4,
    backend: SimilarityBackend = "auto",
    use_openai: bool = True,
    openai_model: str | None = None,
    source_urls: Sequence[str] | None = None,
) -> StormImpactAnswer:
    results = tuple(
        search_external_context(
            conn,
            query,
            limit=limit,
            backend=backend,
            source_urls=source_urls,
        )
    )
    if not results:
        context = build_augmented_context(query, results)
        return StormImpactAnswer(
            question=query,
            answer=(
                "I do not have indexed NOAA/NHC/NCEI context for this impact query. "
                "Run the web-source crawl first, then ask again."
            ),
            context=context,
            results=tuple(),
            cited_results=tuple(),
        )

    context = build_augmented_context(query, results)
    openai_error: str | None = None
    if use_openai:
        try:
            answer = summarize_storm_impact_with_openai(
                query,
                results,
                model=openai_model,
                max_bullets=max_sentences,
            )
        except OpenAISummarizationError as exc:
            openai_error = _short_error_message(str(exc))
            answer = ""
        if answer:
            cited_results = _cited_results_from_answer(answer, results)
            if cited_results:
                return StormImpactAnswer(
                    question=query,
                    answer=answer,
                    context=context,
                    results=results,
                    cited_results=cited_results,
                    used_openai=True,
                )

    selected = _select_summary_sentences(
        query,
        results,
        max_sentences=max_sentences,
    )
    if not selected:
        selected = [
            (1, format_excerpt(results[0].content, max_chars=360)),
        ]

    cited_positions: list[int] = []
    for source_position, _ in selected:
        if source_position not in cited_positions:
            cited_positions.append(source_position)
    citation_number_by_position = {
        source_position: citation_number
        for citation_number, source_position in enumerate(cited_positions, start=1)
    }
    answer = "\n".join(
        f"- {_clean_summary_statement(sentence)} "
        f"[{citation_number_by_position[source_position]}]"
        for source_position, sentence in selected
    )
    cited_results = tuple(results[source_position - 1] for source_position in cited_positions)
    return StormImpactAnswer(
        question=query,
        answer=answer,
        context=context,
        results=results,
        cited_results=cited_results,
        used_openai=False,
        openai_error=openai_error,
    )


def should_use_external_storm_context(query: str) -> bool:
    text = query.lower()
    if "impact" in text and ("storm" in text or "hurricane" in text):
        return True
    return any(
        term in text
        for term in (
            "billion-dollar",
            "climate disaster",
            "hurricane report",
            "national hurricane center",
            "ncei",
            "nhc",
            "tropical cyclone report",
        )
    )


def similarity_backend_status() -> str:
    backend_name = preferred_backend_name()
    if backend_name == "faiss":
        return "FAISS"
    return "exact local fallback"


def _cited_results_from_answer(
    answer: str,
    results: Sequence[RagResult],
) -> tuple[RagResult, ...]:
    cited_positions: list[int] = []
    for match in re.finditer(r"\[(\d+)\]", answer):
        source_position = int(match.group(1))
        if 1 <= source_position <= len(results) and source_position not in cited_positions:
            cited_positions.append(source_position)
    return tuple(results[source_position - 1] for source_position in cited_positions)


def _select_summary_sentences(
    query: str,
    results: Sequence[RagResult],
    *,
    max_sentences: int,
) -> list[tuple[int, str]]:
    query_terms = set(tokenize(query))
    scored: list[tuple[float, int, str]] = []
    seen: set[str] = set()

    for result_position, result in enumerate(results, start=1):
        for sentence in split_sentences(result.content):
            normalized_sentence = " ".join(tokenize(sentence))
            if normalized_sentence in seen:
                continue
            seen.add(normalized_sentence)
            tokens = set(tokenize(sentence))
            if len(tokens) < 5:
                continue
            query_overlap = len(tokens & query_terms)
            impact_overlap = len(tokens & IMPACT_TERMS)
            if query_overlap == 0 and impact_overlap == 0:
                continue
            if _looks_like_table_metadata(sentence) and impact_overlap < 2:
                continue
            score = (
                query_overlap
                + (0.75 * impact_overlap)
                + (0.5 * result.score)
                + (1.0 / result_position)
            )
            scored.append((score, result_position, sentence))

    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    selected = scored[: max(1, max_sentences)]
    selected.sort(key=lambda item: item[1])
    return [(source_position, sentence) for _, source_position, sentence in selected]


def _clean_summary_statement(statement: str) -> str:
    cleaned = normalize_text(statement)
    cleaned = _join_broken_month_names(cleaned)
    cleaned = re.sub(
        r"^\$[\d,.]+(?:\.\d+)?\s+\*?\s*(?:CI\s+)?\d+\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    impact_start = _impact_clause_start(cleaned)
    if impact_start > 0:
        cleaned = cleaned[impact_start:].lstrip(" -;:,")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if cleaned:
        cleaned = f"{cleaned[0].upper()}{cleaned[1:]}"
    return format_excerpt(cleaned, max_chars=260)


def _impact_clause_start(statement: str) -> int:
    starts = [
        match.start()
        for pattern in (
            r"\bhigh wind\b",
            r"\bwind and flood damage\b",
            r"\bdamaging wind",
            r"\bheavy rain",
            r"\bflash flooding\b",
            r"\bflooding\b",
            r"\bstorm surge\b",
            r"\bwinter storm impacts\b",
            r"\btropical storm [A-Z][a-z]+ made landfall\b",
            r"\bSandy also caused\b",
            r"\bpower outages?\b",
            r"\binterruption to critical\b",
        )
        for match in [re.search(pattern, statement, flags=re.IGNORECASE)]
        if match
    ]
    return min(starts) if starts else 0


def _join_broken_month_names(text: str) -> str:
    replacements = {
        r"\bJan uary\b": "January",
        r"\bFeb ruary\b": "February",
        r"\bMar ch\b": "March",
        r"\bApr il\b": "April",
        r"\bJun e\b": "June",
        r"\bJul y\b": "July",
        r"\bAug ust\b": "August",
        r"\bSep tember\b": "September",
        r"\bOct ober\b": "October",
        r"\bNov ember\b": "November",
        r"\bDec ember\b": "December",
    }
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text)
    return text


def _looks_like_table_metadata(statement: str) -> bool:
    return bool(
        re.search(r"^\$[\d,.]+", statement)
        or re.search(r"\bCI\s+\d+\b", statement)
        or re.search(r"\b(?:Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\w+\s+\d{1,2},\s+\d{4}", statement)
    )


def _short_error_message(message: str, *, max_chars: int = 220) -> str:
    compact = re.sub(r"\s+", " ", message).strip()
    compact = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", compact)
    return format_excerpt(compact, max_chars=max_chars)


def split_sentences(text: str) -> list[str]:
    sentences: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = normalize_text(paragraph)
        if not paragraph:
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
            sentence = normalize_text(sentence)
            if sentence:
                sentences.append(sentence)
    return sentences


def is_readable_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if len(tokenize(stripped)) < 4:
        return False

    control_count = sum(
        1
        for char in stripped
        if ord(char) < 32 and char not in {"\n", "\r", "\t"}
    )
    if control_count / max(1, len(stripped)) > 0.02:
        return False

    printable_count = sum(1 for char in stripped if char.isprintable() or char in "\n\r\t")
    if printable_count / max(1, len(stripped)) < 0.9:
        return False

    ascii_letter_count = sum(
        1 for char in stripped if char.isascii() and char.isalpha()
    )
    if ascii_letter_count / max(1, len(stripped)) < 0.15:
        return False

    alpha_tokens = [
        token for token in tokenize(stripped) if any(char.isalpha() for char in token)
    ]
    long_alpha_tokens = [token for token in alpha_tokens if len(token) >= 3]
    if len(long_alpha_tokens) / max(1, len(alpha_tokens)) < 0.35:
        return False

    return True


def normalize_url(url: str) -> str:
    stripped = url.strip()
    if not stripped:
        raise ValueError("url must not be empty")
    if not re.match(r"^https?://", stripped, flags=re.IGNORECASE):
        stripped = f"https://{stripped}"
    return stripped


def _source_hosts(urls: Sequence[str]) -> set[str]:
    hosts: set[str] = set()
    for url in urls:
        try:
            host = urlparse(normalize_url(url)).netloc.lower()
        except ValueError:
            continue
        if host:
            hosts.add(host)
    return hosts


def _clear_external_documents(conn: sqlite3.Connection) -> None:
    document_ids = [
        int(row[0])
        for row in conn.execute(
            """
            SELECT id FROM RagDocument
            WHERE path LIKE 'http://%' OR path LIKE 'https://%'
            """
        ).fetchall()
    ]
    for document_id in document_ids:
        conn.execute("DELETE FROM RagChunk WHERE documentId = ?", (document_id,))
        conn.execute("DELETE FROM RagDocument WHERE id = ?", (document_id,))
    conn.commit()


def _looks_like_html(resource: FetchedResource) -> bool:
    return resource.content_type in HTML_CONTENT_TYPES or _url_suffix(resource.url) in {
        "",
        ".html",
        ".htm",
        ".php",
    }


def _looks_like_pdf(resource: FetchedResource) -> bool:
    return resource.content_type in PDF_CONTENT_TYPES or _url_suffix(resource.url) == ".pdf"


def _url_suffix(url: str) -> str:
    return Path(urlparse(url).path).suffix.lower()


def _title_from_url(url: str) -> str:
    path = Path(urlparse(url).path)
    stem = path.stem or urlparse(url).netloc
    return stem.replace("_", " ").replace("-", " ").title()


def _extract_pdf_with_pypdf(pdf_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError:
        return ""
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return ""


def _extract_pdf_with_pdftotext(pdf_bytes: bytes) -> str:
    if shutil.which("pdftotext") is None:
        return ""

    with tempfile.TemporaryDirectory() as temp_dir:
        pdf_path = Path(temp_dir) / "source.pdf"
        text_path = Path(temp_dir) / "source.txt"
        pdf_path.write_bytes(pdf_bytes)
        try:
            subprocess.run(
                ["pdftotext", "-layout", str(pdf_path), str(text_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError):
            return ""
        return text_path.read_text(encoding="utf-8", errors="ignore")


def _extract_pdf_literal_strings(pdf_bytes: bytes) -> str:
    strings: list[str] = []
    for match in re.finditer(rb"\((?:\\.|[^\\)])*\)", pdf_bytes):
        literal = match.group(0)[1:-1]
        decoded = _decode_pdf_literal(literal)
        if decoded and re.search(r"[A-Za-z]{3,}", decoded):
            strings.append(decoded)
    return " ".join(strings)


def _decode_pdf_literal(literal: bytes) -> str:
    replacements = {
        rb"\n": b"\n",
        rb"\r": b"\r",
        rb"\t": b"\t",
        rb"\b": b"\b",
        rb"\f": b"\f",
        rb"\(": b"(",
        rb"\)": b")",
        rb"\\": b"\\",
    }
    for old, new in replacements.items():
        literal = literal.replace(old, new)
    try:
        return literal.decode("utf-8")
    except UnicodeDecodeError:
        return literal.decode("latin-1", errors="ignore")
