"""Hermetic tests for seed_knowledge.ingest — atomic hash semantics.

Uses a chainable fake Supabase client (same pattern as test_database.py) and
real temp markdown files, so the full per-document sequence runs without any
network or embedding API access.
"""

from copy import deepcopy

import pytest

import seed_knowledge


MD = (
    "# Returns Policy\n"
    "\n"
    "## Return window\n"
    "Items can be returned within 30 days of delivery.\n"
    "\n"
    "## Condition requirements\n"
    "Unused and in original packaging."
)


class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    """Chainable stand-in for supabase-py's query builder, backed by state."""

    def __init__(self, client, table):
        self._c = client
        self._t = table
        self._mode = None
        self._payload = None
        self._filters = {}

    def select(self, *a):
        self._mode = "select"
        return self

    def insert(self, payload):
        self._mode = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._mode = "update"
        self._payload = payload
        return self

    def delete(self):
        self._mode = "delete"
        return self

    def eq(self, column, value):
        self._filters[column] = value
        return self

    def execute(self):
        client = self._c
        if self._t == "knowledge_documents":
            if self._mode == "select":
                match = client.documents.get(self._filters.get("title"))
                return _Resp([dict(match)] if match else [])
            if self._mode == "insert":
                doc_id = client._next_id
                client._next_id += 1
                row = {"id": doc_id, **self._payload}
                client.documents[row["title"]] = row
                client.log.append(("documents", "insert", dict(self._payload)))
                return _Resp([dict(row)])
            if self._mode == "update":
                doc_id = self._filters.get("id")
                row = next(d for d in client.documents.values() if d["id"] == doc_id)
                row.update(self._payload)
                client.log.append(("documents", "update", dict(self._payload)))
                return _Resp([])
        elif self._t == "knowledge_chunks":
            if self._mode == "delete":
                client.chunks.pop(self._filters.get("document_id"), None)
                client.log.append(("chunks", "delete", None))
                return _Resp([])
            if self._mode == "insert":
                if client.fail_on_chunks_insert:
                    raise RuntimeError("chunk insert failed")
                doc_id = self._payload[0]["document_id"]
                client.chunks.setdefault(doc_id, []).extend(dict(r) for r in self._payload)
                client.log.append(("chunks", "insert", [dict(r) for r in self._payload]))
                return _Resp(self._payload)
        raise AssertionError(f"unhandled call: {self._t}/{self._mode}")


class _RPC:
    def __init__(self, client, name, params):
        self._c = client
        self._name = name
        self._params = params

    def execute(self):
        assert self._name == "replace_knowledge_document"
        client = self._c
        if client.fail_on_replace:
            raise RuntimeError("document replacement failed")

        documents = deepcopy(client.documents)
        chunks = deepcopy(client.chunks)
        title = self._params["p_title"]
        row = documents.get(title)
        if row is None:
            row = {"id": client._next_id, "title": title}
            client._next_id += 1
            documents[title] = row
        row.update({
            "source_file": self._params["p_source_file"],
            "content_hash": self._params["p_content_hash"],
        })
        document_id = row["id"]
        chunks[document_id] = [
            {"document_id": document_id, **chunk}
            for chunk in self._params["p_chunks"]
        ]
        client.documents = documents
        client.chunks = chunks
        client.log.append(("rpc", self._name, deepcopy(self._params)))
        return _Resp([{"replace_knowledge_document": document_id}])


class _FakeClient:
    def __init__(self, documents=None, chunks=None):
        # documents: {title: {id, title, content_hash, source_file}}
        self.documents = dict(documents or {})
        self.chunks = dict(chunks or {})  # {document_id: [rows]}
        self.log = []
        self.fail_on_replace = False
        self._next_id = max((d["id"] for d in self.documents.values()), default=0) + 1

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params):
        return _RPC(self, name, params)


def _run(monkeypatch, tmp_path, *, reingest=False, fail_chunks=False, preexisting=None, prechunks=None):
    (tmp_path / "returns.md").write_text(MD, encoding="utf-8")
    digest = seed_knowledge.file_hash(tmp_path / "returns.md")
    client = _FakeClient(documents=preexisting, chunks=prechunks)
    client.fail_on_replace = fail_chunks
    monkeypatch.setattr(seed_knowledge, "embed_texts", lambda texts: [[0.25] * 768 for _ in texts])
    summary = seed_knowledge.ingest(client, tmp_path, reingest=reingest)
    return client, summary, digest


class TestIngestNewDocument:
    def test_atomic_rpc_writes_document_and_chunks_together(self, monkeypatch, tmp_path):
        client, summary, digest = _run(monkeypatch, tmp_path)

        assert summary == (1, len(client.chunks[next(iter(client.chunks))]))

        writes = [e for e in client.log if e[0] == "rpc"]
        assert len(writes) == 1
        assert writes[0][2]["p_content_hash"] == digest
        assert len(digest) == 64

        doc_row = client.documents["Returns Policy"]
        assert doc_row["content_hash"] == digest
        rows = client.chunks[doc_row["id"]]
        assert all(len(row["embedding"]) == 768 for row in rows)
        assert all(row["document_id"] == doc_row["id"] for row in rows)


class TestIngestUnchanged:
    def test_matching_hash_short_circuits_without_writes(self, monkeypatch, tmp_path):
        (tmp_path / "returns.md").write_text(MD, encoding="utf-8")
        digest = seed_knowledge.file_hash(tmp_path / "returns.md")
        preexisting = {
            "Returns Policy": {
                "id": 5,
                "title": "Returns Policy",
                "content_hash": digest,
                "source_file": "returns.md",
            }
        }
        prechunks = {5: [{"document_id": 5, "chunk_index": 0, "stale": True}]}

        client, summary, _ = _run(
            monkeypatch, tmp_path, reingest=False, preexisting=preexisting, prechunks=prechunks
        )

        assert summary == (0, 0)
        # Only the existence check ran — no insert/update/delete anywhere.
        writes = [e for e in client.log if e[1] != "select"]
        assert writes == []


class TestIngestReingest:
    def test_reingest_replaces_stale_chunks_and_refreshes_hash(self, monkeypatch, tmp_path):
        (tmp_path / "returns.md").write_text(MD, encoding="utf-8")
        digest = seed_knowledge.file_hash(tmp_path / "returns.md")
        preexisting = {
            "Returns Policy": {
                "id": 5,
                "title": "Returns Policy",
                "content_hash": digest,
                "source_file": "returns.md",
            }
        }
        prechunks = {5: [{"document_id": 5, "chunk_index": 0, "stale": True}]}

        client, summary, _ = _run(
            monkeypatch, tmp_path, reingest=True, preexisting=preexisting, prechunks=prechunks
        )

        assert summary[0] == 1
        rows = client.chunks[5]
        assert rows and all(not row.get("stale") for row in rows)
        writes = [e for e in client.log if e[0] == "rpc"]
        assert len(writes) == 1


class TestIngestAtomicity:
    def test_replacement_failure_leaves_document_and_chunks_unchanged(self, monkeypatch, tmp_path):
        # Built inline (not via _run) so the client exists independently of
        # the expected ingest failure.
        (tmp_path / "returns.md").write_text(MD, encoding="utf-8")
        digest = seed_knowledge.file_hash(tmp_path / "returns.md")
        client = _FakeClient()
        client.fail_on_replace = True
        monkeypatch.setattr(seed_knowledge, "embed_texts", lambda texts: [[0.25] * 768 for _ in texts])

        with pytest.raises(RuntimeError):
            seed_knowledge.ingest(client, tmp_path, reingest=False)

        assert "Returns Policy" not in client.documents
        assert client.chunks == {}
