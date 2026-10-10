"""Policy/FAQ retrieval over Supabase pgvector (lightweight RAG).

Embeds text via the google-genai SDK pinned to 768 dimensions (matching the
pgvector column), asks the match_knowledge_chunks RPC for the nearest chunks
and returns them formatted for the system prompt. Mirrors database.py's error
style: retrieval failures are logged and swallowed, so an outage degrades to
a no-match answer instead of raising into the chat flow.
"""

import logging
from functools import lru_cache
from typing import Any

from .config import GEMINI_API_KEY, supabase_server_client
from .contracts import PolicyChunk, PolicySearchResult

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "gemini-embedding-001"
# Gemini embeddings default to 3072 dims; pin 768 to match the pgvector column.
OUTPUT_DIMENSIONALITY = 768
DEFAULT_MATCH_COUNT = 4
# Authoritative similarity floor; passed on every call. Mirrored as the RPC
# default in supabase/migrations/005_rag_retrieval_quality.sql.
MIN_SIMILARITY = 0.6


def _client():
    return supabase_server_client()


@lru_cache(maxsize=1)
def _get_genai_client():
    """Lazily build the google-genai client shared by every embed call."""
    from google import genai

    return genai.Client(api_key=GEMINI_API_KEY)


def fit_dimensions(vectors: list[list[float]]) -> list[list[float]]:
    """Force vectors to exactly OUTPUT_DIMENSIONALITY dimensions.

    Gemini embedding models are Matryoshka-trained, so keeping the leading
    dims of a larger vector yields a valid lower-dimensional embedding. Longer
    vectors are truncated; shorter ones indicate a broken pin and raise loudly
    here rather than failing cryptically inside Postgres.
    """
    fitted = []
    for vector in vectors:
        size = len(vector)
        if size == OUTPUT_DIMENSIONALITY:
            fitted.append(list(vector))
        elif size > OUTPUT_DIMENSIONALITY:
            fitted.append(list(vector[:OUTPUT_DIMENSIONALITY]))
        else:
            raise ValueError(f"Embedding has {size} dims; expected at least {OUTPUT_DIMENSIONALITY}.")
    return fitted


def _sdk_embed(texts: list[str]) -> list[list[float]]:
    """Call the pinned google-genai SDK. No dimension guarantee."""
    from google.genai import types

    response = _get_genai_client().models.embed_content(
        model=EMBEDDING_MODEL,
        contents=texts,
        config=types.EmbedContentConfig(output_dimensionality=OUTPUT_DIMENSIONALITY),
    )
    return [list(item.values) for item in response.embeddings]


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts at exactly OUTPUT_DIMENSIONALITY dimensions.

    Raises on hard failures (auth, network, count mismatch, short vectors);
    callers decide how to degrade.
    """
    if not texts:
        return []
    vectors = _sdk_embed(texts)
    if len(vectors) != len(texts):
        raise RuntimeError(f"Embedder returned {len(vectors)} vectors for {len(texts)} inputs.")
    return fit_dimensions(vectors)


def _normalize_query(query: str) -> str:
    """Normalize public policy queries before using them as cache keys."""
    return " ".join(query.split()).casefold()


@lru_cache(maxsize=512)
def _cached_query_embedding(query: str) -> tuple[float, ...]:
    """Cache bounded, normalized query embeddings."""
    return tuple(embed_texts([query])[0])


def _embed_query(query: str) -> list[float]:
    return list(_cached_query_embedding(_normalize_query(query)))


def _match_chunks(embedding: list[float], k: int) -> list[dict]:
    response = (
        _client()
        .rpc(
            "match_knowledge_chunks",
            {
                "query_embedding": embedding,
                "match_count": k,
                "min_similarity": MIN_SIMILARITY,
            },
        )
        .execute()
    )
    return response.data or []


def _retrieve_matches(query: str, k: int) -> list[dict[str, Any]]:
    cleaned = (query or "").strip()
    if not cleaned:
        return []
    embedding = _embed_query(cleaned)
    return _match_chunks(embedding, k)


def retrieve_policy_matches(query: str, k: int = DEFAULT_MATCH_COUNT) -> list[dict]:
    """Return score-qualified policy matches, or an empty list on no match/error."""
    try:
        return _retrieve_matches(query, k)
    except Exception as exc:
        logger.error("Policy retrieval failed (%s)", type(exc).__name__)
        return []


def search_policy_chunks(query: str, k: int = DEFAULT_MATCH_COUNT) -> PolicySearchResult:
    """Return typed, source-labelled policy matches with distinct failure states."""
    if not (query or "").strip():
        return PolicySearchResult("no_match")
    try:
        matches = _retrieve_matches(query, k)
    except Exception as exc:
        logger.error("Policy retrieval failed (%s)", type(exc).__name__)
        return PolicySearchResult("unavailable")

    if not matches:
        return PolicySearchResult("no_match")

    chunks = [
        PolicyChunk(
            source_title=match.get("doc_title") or "Unknown",
            heading=match.get("heading"),
            content=(match.get("content") or "").strip(),
            similarity=match.get("similarity"),
        )
        for match in matches
        if (match.get("content") or "").strip()
    ]
    if not chunks:
        return PolicySearchResult("no_match")

    return PolicySearchResult(
        "found",
        chunks,
    )
