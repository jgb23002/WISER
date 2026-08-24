from __future__ import annotations

import hashlib
import math
import re
import threading
from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Literal, Sequence

# numpy is optional, exactly like faiss. When present the exact backend does
# its dot products as one matrix multiply instead of a Python loop per
# candidate; when absent the pure-Python path below still runs.
try:
    import numpy as _np  # type: ignore[import-not-found]
except ImportError:
    _np = None

SimilarityBackend = Literal["auto", "faiss", "exact"]

TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_]*|[0-9]+(?:\.[0-9]+)?")
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
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "this",
        "to",
        "with",
    }
)


@dataclass(frozen=True)
class TextSimilarityMatch:
    index: int
    score: float
    text: str


def tokenize_for_similarity(text: str) -> list[str]:
    tokens = [_normalize_token(match.group(0)) for match in TOKEN_RE.finditer(text)]
    filtered = [token for token in tokens if token not in STOP_WORDS]
    return filtered or tokens


# Hashed vectors are pure functions of (text, dimensions) and the same chunk
# texts are re-ranked on every external-context query, so the last N are kept.
_VECTOR_CACHE_MAX_ENTRIES = 2048
_vector_cache: "OrderedDict[tuple[str, int], list[float]]" = OrderedDict()
_vector_cache_lock = threading.Lock()


def clear_vector_cache() -> None:
    """Drop every cached text vector (test hook)."""
    with _vector_cache_lock:
        _vector_cache.clear()


def vectorize_text_cached(text: str, *, dimensions: int = 384) -> list[float]:
    """vectorize_text with memoization. The returned list must not be mutated."""
    key = (text, dimensions)
    with _vector_cache_lock:
        vector = _vector_cache.get(key)
        if vector is not None:
            _vector_cache.move_to_end(key)
            return vector
    vector = vectorize_text(text, dimensions=dimensions)
    with _vector_cache_lock:
        _vector_cache[key] = vector
        _vector_cache.move_to_end(key)
        while len(_vector_cache) > _VECTOR_CACHE_MAX_ENTRIES:
            _vector_cache.popitem(last=False)
    return vector


def vectorize_text(text: str, *, dimensions: int = 384) -> list[float]:
    if dimensions <= 0:
        raise ValueError("dimensions must be positive")

    vector = [0.0] * dimensions
    token_counts = Counter(tokenize_for_similarity(text))
    if not token_counts:
        return vector

    for token, count in token_counts.items():
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % dimensions
        vector[bucket] += 1.0 + math.log(count)

    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


def rank_texts(
    query: str,
    texts: Sequence[str],
    *,
    limit: int = 5,
    backend: SimilarityBackend = "auto",
    dimensions: int = 384,
) -> list[TextSimilarityMatch]:
    if limit <= 0 or not texts:
        return []

    query_vector = vectorize_text(query, dimensions=dimensions)
    if not any(query_vector):
        return []

    text_vectors = [
        vectorize_text_cached(text, dimensions=dimensions) for text in texts
    ]
    ranked: list[tuple[int, float]]
    if backend in {"auto", "faiss"}:
        ranked = _rank_with_faiss(query_vector, text_vectors, limit)
        if ranked:
            return _matches_from_ranked(texts, ranked, limit)

    ranked = _rank_exact(query_vector, text_vectors)
    return _matches_from_ranked(texts, ranked, limit)


def preferred_backend_name() -> str:
    return "faiss" if _faiss_available() else "exact"


def _rank_exact(
    query_vector: Sequence[float],
    text_vectors: Sequence[Sequence[float]],
) -> list[tuple[int, float]]:
    if _np is not None and text_vectors:
        # One float64 matrix-vector product replaces N Python dot loops. The
        # sort stays in Python on the same (-score, index) key, so ordering
        # and tie-breaking are unchanged.
        matrix = _np.asarray(text_vectors, dtype=_np.float64)
        scores = matrix @ _np.asarray(query_vector, dtype=_np.float64)
        ranked = list(enumerate(scores.tolist()))
    else:
        ranked = [
            (index, _dot_product(query_vector, vector))
            for index, vector in enumerate(text_vectors)
        ]
    return sorted(ranked, key=lambda item: (-item[1], item[0]))


def _rank_with_faiss(
    query_vector: Sequence[float],
    text_vectors: Sequence[Sequence[float]],
    limit: int,
) -> list[tuple[int, float]]:
    try:
        import faiss  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except ImportError:
        return []

    if not text_vectors:
        return []

    matrix = np.array(text_vectors, dtype="float32")
    query_matrix = np.array([query_vector], dtype="float32")
    index = faiss.IndexFlatIP(matrix.shape[1])
    index.add(matrix)
    distances, indices = index.search(query_matrix, min(limit, len(text_vectors)))
    return [
        (int(match_index), float(score))
        for match_index, score in zip(indices[0], distances[0])
        if int(match_index) >= 0
    ]


def _matches_from_ranked(
    texts: Sequence[str],
    ranked: Sequence[tuple[int, float]],
    limit: int,
) -> list[TextSimilarityMatch]:
    matches: list[TextSimilarityMatch] = []
    for index, score in ranked:
        if score <= 0:
            continue
        matches.append(
            TextSimilarityMatch(
                index=index,
                score=score,
                text=texts[index],
            )
        )
        if len(matches) >= limit:
            break
    return matches


def _dot_product(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(left_value * right_value for left_value, right_value in zip(left, right))


def _faiss_available() -> bool:
    try:
        import faiss  # type: ignore[import-not-found] # noqa: F401
        import numpy  # type: ignore[import-not-found] # noqa: F401
    except ImportError:
        return False
    return True


def _normalize_token(token: str) -> str:
    normalized = token.lower()
    if len(normalized) > 3 and normalized.endswith("ies"):
        return f"{normalized[:-3]}y"
    if len(normalized) > 3 and normalized.endswith("s"):
        return normalized[:-1]
    return normalized
