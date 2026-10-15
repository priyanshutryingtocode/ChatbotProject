"""Seed knowledge/*.md into Supabase pgvector for policy/FAQ retrieval.
Reads the markdown policy docs, splits them under their headings into
~600-character chunks, embeds each chunk via retriever.embed_texts
(gemini-embedding-001 pinned to 768 dimensions) and upserts documents +
chunks.

Ingestion is atomic per document. Embeddings are prepared locally, then a
single database RPC replaces metadata and chunks in one transaction.

Examples:
    python scripts/seed_knowledge.py                 # ingest new/changed docs
    python scripts/seed_knowledge.py --reingest      # re-embed every doc

Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env. Embedding calls
use a separate, more generous quota than chat generation.
"""

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orderbot.retriever import EMBEDDING_MODEL, embed_texts

REPO_ROOT = Path(__file__).resolve().parent.parent

MAX_CHARS = 600

OVERLAP_CHARS = 120

CHUNKER_VERSION = "markdown-v2"

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")


def _tail_for_overlap(text: str) -> str:
    """Return a word-boundary tail small enough to carry into the next chunk."""
    if len(text) <= OVERLAP_CHARS:
        return text
    tail = text[-OVERLAP_CHARS:]
    boundary = tail.find(" ")
    return tail[boundary + 1 :] if boundary >= 0 else tail


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
    heading_stack: list[tuple[int, str]] = []
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
            level = len(match.group(1))
            heading_stack = [(depth, title) for depth, title in heading_stack if depth < level]
            heading_stack.append((level, match.group(2).strip()))
            heading = " > ".join(title for _depth, title in heading_stack)
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
    source_directory = (
        "knowledge" if knowledge_dir.resolve() == (REPO_ROOT / "knowledge").resolve() else str(knowledge_dir.resolve())
    )
    source_files = sorted(knowledge_dir.glob("*.md"))
    active_source_files = {path.name for path in source_files}
    for path in source_files:
        text = path.read_text(encoding="utf-8")
        title = doc_title(path, text)
        digest = file_hash(path)

        existing = (
            client.table("knowledge_documents")
            .select("id, content_hash, embedding_model, chunker_version")
            .eq("source_directory", source_directory)
            .eq("source_file", path.name)
            .execute()
            .data
            or []
        )
        if (
            existing
            and existing[0].get("content_hash") == digest
            and existing[0].get("embedding_model") == EMBEDDING_MODEL
            and existing[0].get("chunker_version") == CHUNKER_VERSION
            and not reingest
        ):
            print(f"Skipping '{title}' (unchanged).")
            continue

        chunks = chunk_markdown(text)

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
                "p_source_directory": source_directory,
                "p_embedding_model": EMBEDDING_MODEL,
                "p_chunker_version": CHUNKER_VERSION,
                "p_content_hash": digest,
                "p_chunks": rows,
            },
        ).execute()

        documents_ingested += 1
        total_chunks += len(rows)
        print(f"Ingested '{title}': {len(rows)} chunks.")

    stored_documents = (
        client.table("knowledge_documents")
        .select("id, source_file")
        .eq("source_directory", source_directory)
        .execute()
        .data
        or []
    )
    for document in stored_documents:
        if document.get("source_file") not in active_source_files:
            client.table("knowledge_documents").delete().eq("id", document["id"]).execute()
            print(f"Removed stale policy document '{document.get('source_file', 'Unknown')}'.")

    return documents_ingested, total_chunks


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest knowledge/*.md into Supabase pgvector.")
    parser.add_argument(
        "--dir", type=Path, default=REPO_ROOT / "knowledge", help="Markdown source directory (default: knowledge/)."
    )
    parser.add_argument("--reingest", action="store_true", help="Re-embed every document even if unchanged.")
    args = parser.parse_args()

    load_dotenv()
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    api_key = os.getenv("GEMINI_API_KEY")
    missing = [
        name
        for name, value in (("SUPABASE_URL", url), ("SUPABASE_SERVICE_ROLE_KEY", key), ("GEMINI_API_KEY", api_key))
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}.")

    from supabase import create_client

    if not args.dir.exists():
        raise FileNotFoundError(f"Knowledge directory not found: {args.dir}")

    client = create_client(url, key).schema("order_support")
    documents, chunks = ingest(client, args.dir, args.reingest)
    print(f"Done: {documents} document(s), {chunks} chunk(s) embedded.")


if __name__ == "__main__":
    main()
