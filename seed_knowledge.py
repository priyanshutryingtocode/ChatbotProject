"""Seed knowledge/*.md into Supabase pgvector for policy/FAQ retrieval.
Reads the markdown policy docs, splits them under their headings into
~600-character chunks, embeds each chunk via retriever.embed_texts
(gemini-embedding-001 pinned to 768 dimensions) and upserts documents +
chunks.

Ingestion is atomic per document: the content_hash that gates the "unchanged"
skip is written only AFTER its chunks are inserted, so a crash mid-document
can never leave a doc marked complete.

Examples:
    python seed_knowledge.py                 # ingest new/changed docs
    python seed_knowledge.py --reingest      # re-embed every doc

Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env. Embedding calls
use a separate, more generous quota than chat generation.
"""

import argparse
import hashlib
import os
import re
from pathlib import Path

from dotenv import load_dotenv

from retriever import embed_texts


MAX_CHARS = 600
MIN_MERGED_CHARS = 80
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")


def chunk_markdown(text: str) -> list[tuple[str | None, str]]:
    """Split markdown under headings into chunks of at most MAX_CHARS.

    Returns (heading, body) tuples in document order; headingless preamble
    chunks use None.
    """
    chunks: list[tuple[str | None, str]] = []
    current_heading: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            if chunks and len(body) < MIN_MERGED_CHARS:
                prev_heading, prev_body = chunks[-1]
                glue = "" if prev_body.endswith("\n") else "\n\n"
                chunks[-1] = (prev_heading, f"{prev_body}{glue}{body}")
            else:
                chunks.append((current_heading, body))

    for line in text.splitlines():
        match = HEADING_RE.match(line.strip())
        if match:
            flush()
            buffer = []
            current_heading = match.group(2).strip()
        else:
            buffer.append(line)
            if sum(len(part) + 1 for part in buffer) >= MAX_CHARS:
                flush()
                buffer = []
    flush()
    return chunks


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def doc_title(path: Path, text: str) -> str:
    """Use the first H1 as the document title, falling back to the file stem."""
    for line in text.splitlines():
        if line.startswith("# ") and line[2:].strip():
            return line[2:].strip()
    return path.stem.replace("-", " ").title()


def ingest(client, knowledge_dir: Path, reingest: bool) -> tuple[int, int]:
    documents_ingested = 0
    total_chunks = 0
    for path in sorted(knowledge_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        title = doc_title(path, text)
        digest = file_hash(path)

        existing = client.table("knowledge_documents").select("id, content_hash").eq("title", title).execute().data or []
        if existing and existing[0].get("content_hash") == digest and not reingest:
            print(f"Skipping '{title}' (unchanged).")
            continue

        # Prepare embeddings before changing database state. The RPC below
        # replaces the document metadata and chunks in one transaction.
        chunks = chunk_markdown(text)

        contents = [body for _, body in chunks]
        vectors = embed_texts(contents) if contents else []
        rows = [
            {
                "chunk_index": index,
                "heading": heading,
                "content": body,
                "embedding": vector,
            }
            for index, ((heading, body), vector) in enumerate(zip(chunks, vectors))
        ]
        client.rpc(
            "replace_knowledge_document",
            {
                "p_title": title,
                "p_source_file": path.name,
                "p_content_hash": digest,
                "p_chunks": rows,
            },
        ).execute()

        documents_ingested += 1
        total_chunks += len(rows)
        print(f"Ingested '{title}': {len(rows)} chunks.")
    return documents_ingested, total_chunks


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest knowledge/*.md into Supabase pgvector.")
    parser.add_argument("--dir", type=Path, default=Path("knowledge"), help="Markdown source directory.")
    parser.add_argument("--reingest", action="store_true", help="Re-embed every document even if unchanged.")
    args = parser.parse_args()

    load_dotenv()
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    api_key = os.getenv("GEMINI_API_KEY")
    missing = [name for name, value in (
        ("SUPABASE_URL", url), ("SUPABASE_SERVICE_ROLE_KEY", key), ("GEMINI_API_KEY", api_key)
    ) if not value]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}.")

    from supabase import create_client

    if not args.dir.exists():
        raise FileNotFoundError(f"Knowledge directory not found: {args.dir}")

    client = create_client(url, key)
    documents, chunks = ingest(client, args.dir, args.reingest)
    print(f"Done: {documents} document(s), {chunks} chunk(s) embedded.")


if __name__ == "__main__":
    main()
