"""Policy/FAQ retrieval over Supabase pgvector (lightweight RAG).

Embeds text via the google-genai SDK pinned to 768 dimensions (matching the
pgvector column), asks the match_knowledge_chunks RPC for the nearest chunks
and returns them formatted for the system prompt. Mirrors database.py's error
style: retrieval failures are logged and swallowed, so an outage degrades to
a no-match answer instead of raising into the chat flow.
"""

import logging

from setup import GEMINI_API_KEY, supabase_server_client

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "gemini-embedding-001"
# Gemini embeddings default to 3072 dims; pin 768 to match the pgvector column.
OUTPUT_DIMENSIONALITY = 768
DEFAULT_MATCH_COUNT = 4

_genai_client = None


def _client():
    return supabase_server_client()


def _get_genai_client():
    """Lazily build the google-genai client shared by every embed call."""
    global _genai_client
    if _genai_client is None:
        from google import genai

        _genai_client = genai.Client(api_key=GEMINI_API_KEY)
    return _genai_client


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
            raise ValueError(
                f"Embedding has {size} dims; expected at least {OUTPUT_DIMENSIONALITY}."
            )
    return fitted


def _sdk_embed(texts: list[str]) -> list[list[float]]:
    """Call whichever Google GenAI SDK is installed; no dimension guarantee."""
    try:
        client = _get_genai_client()
        from google.genai import types

        try:
            response = client.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=texts,
                config=types.EmbedContentConfig(output_dimensionality=OUTPUT_DIMENSIONALITY),
            )
        except TypeError:
            # Installed SDK predates EmbedContentConfig: take default dims and
            # truncate below instead.
            response = client.models.embed_content(model=EMBEDDING_MODEL, contents=texts)
        return [list(item.values) for item in response.embeddings]
    except ImportError:
        import google.generativeai as genai_legacy # type: ignore

        genai_legacy.configure(api_key=GEMINI_API_KEY)
        response = genai_legacy.embed_content(
            model=f"models/{EMBEDDING_MODEL}",
            content=texts,
            output_dimensionality=OUTPUT_DIMENSIONALITY,
        )
        return [list(item["values"]) for item in response["embeddings"]]


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


def _embed_query(query: str) -> list[float]:
    return embed_texts([query])[0]


def _match_chunks(embedding: list[float], k: int) -> list[dict]:
    response = (
        _client()
        .rpc("match_knowledge_chunks", {"query_embedding": embedding, "match_count": k})
        .execute()
    )
    return response.data or []


def format_policy_context(matches: list[dict]) -> str | None:
    """Format retrieved chunks as labelled source blocks for the prompt."""
    if not matches:
        return None
    blocks = []
    for match in matches:
        heading = f" › {match['heading']}" if match.get("heading") else ""
        content = (match.get("content") or "").strip()
        blocks.append(f"[source: {match.get('doc_title', 'Unknown')}{heading}]\n{content}")
    return "\n\n".join(blocks)


def retrieve_policies(query: str, k: int = DEFAULT_MATCH_COUNT) -> str | None:
    """Return formatted policy excerpts for a question, or None when nothing useful is found."""
    cleaned = (query or "").strip()
    if not cleaned:
        return None
    try:
        embedding = _embed_query(cleaned)
        matches = _match_chunks(embedding, k)
    except Exception:
        logger.exception("Policy retrieval failed")
        return None
    return format_policy_context(matches)
