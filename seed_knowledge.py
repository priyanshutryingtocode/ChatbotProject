"""Seed knowledge/*.md into Supabase pgvector for policy/FAQ retrieval.
Reads the markdown policy docs, splits them under their headings into
~600-character chunks, embeds each chunk via retriever.embed_texts
(gemini-embedding-001 pinned to 768 dimensions) and upserts documents +
chunks.

Ingestion is atomic per document. Embeddings are prepared locally, then a
single database RPC replaces metadata and chunks in one transaction.

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
OVERLAP_CHARS = 120
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")


def _tail_for_overlap(text: str) -> str:
    """Return a word-boundary tail small enough to carry into the next chunk."""
    if len(text) <= OVERLAP_CHARS:
        return text
    tail = text[-OVERLAP_CHARS:]
    boundary = tail.find(" ")
    return tail[boundary + 1:] if boundary >= 0 else tail


def _split_long_paragraph(paragraph: str) -> list[str]:
    """Split an oversized paragraph without cutting words where possible."""
    pieces = []
    remaining = paragraph.strip()
    while len(remaining) > MAX_CHARS:
        boundary = remaining.rfind(" ", 0, MAX_CHARS + 1)
        if boundary <= 0:
            boundary = MAX_CHARS
        pieces.append(remaining[:boundary].strip())
        remaining = remaining[boundary:].strip()
    if remaining:
        pieces.append(remaining)
    return pieces


def chunk_markdown(text: str) -> list[tuple[str | None, str]]:
    """Split Markdown by heading and paragraph with overlap between chunks."""
    sections: list[tuple[str | None, list[str]]] = []
    heading: str | None = None
    lines: list[str] = []

    def finish_section() -> None:
        body = "\n".join(lines).strip()
        if body:
            sections.append((heading, [part.strip() for part in re.split(r"\n\s*\n+", body) if part.strip()]))

    for line in text.splitlines():
        match = HEADING_RE.match(line.strip())
        if match:
            finish_section()
            lines = []
            heading = match.group(2).strip()
        else:
            lines.append(line)
    finish_section()

    chunks: list[tuple[str | None, str]] = []
    for section_heading, paragraphs in sections:
        buffer = ""
        for paragraph in paragraphs:
            for part in _split_long_paragraph(paragraph):
                candidate = f"{buffer}\n\n{part}".strip() if buffer else part
                if buffer and len(candidate) > MAX_CHARS:
                    chunks.append((section_heading, buffer))
                    overlap = _tail_for_overlap(buffer)
                    with_overlap = f"{overlap}\n\n{part}".strip()
                    # A large following paragraph may leave no room for the
                    # overlap; preserve the size guarantee in that case.
                    buffer = with_overlap if len(with_overlap) <= MAX_CHARS else part
                else:
                    buffer = candidate
        if buffer:
            chunks.append((section_heading, buffer))
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

        # Embed title and heading with each body so retrieval retains the
        # document context even when body language is generic.
        contents = [f"{title}\n{heading or ''}\n{body}".strip() for heading, body in chunks]
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
