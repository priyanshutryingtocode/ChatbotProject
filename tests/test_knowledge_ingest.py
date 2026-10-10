import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.seed_knowledge import ingest


class FakeQuery:
    def __init__(self, client):
        self.client = client
        self.filters = {}
        self.deleting = False

    def select(self, _fields):
        return self

    def eq(self, field, value):
        self.filters[field] = value
        return self

    def delete(self):
        self.deleting = True
        return self

    def execute(self):
        selected = [row for row in self.client.documents if all(row.get(k) == v for k, v in self.filters.items())]
        if self.deleting:
            self.client.documents = [row for row in self.client.documents if row not in selected]
            return type("Result", (), {"data": selected})()
        return type("Result", (), {"data": selected})()


class FakeRpc:
    def __init__(self, client, args):
        self.client = client
        self.args = args

    def execute(self):
        self.client.documents.append(
            {
                "id": 10,
                "title": self.args["p_title"],
                "source_file": self.args["p_source_file"],
                "source_directory": self.args["p_source_directory"],
                "content_hash": self.args["p_content_hash"],
                "embedding_model": self.args["p_embedding_model"],
                "chunker_version": self.args["p_chunker_version"],
            }
        )
        return type("Result", (), {"data": 10})()


class FakeClient:
    def __init__(self, documents):
        self.documents = documents

    def table(self, _name):
        return FakeQuery(self)

    def rpc(self, _name, args):
        return FakeRpc(self, args)


class KnowledgeIngestTests(unittest.TestCase):
    def test_prunes_only_stale_documents_owned_by_ingested_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)
            (source / "active.md").write_text("# Current Policy\n\nKeep this text.", encoding="utf-8")
            client = FakeClient(
                [
                    {"id": 1, "title": "Old", "source_file": "deleted.md", "source_directory": str(source)},
                    {"id": 2, "title": "Elsewhere", "source_file": "deleted.md", "source_directory": "other"},
                ]
            )
            with patch("scripts.seed_knowledge.embed_texts", return_value=[[0.0] * 768]):
                ingest(client, source, reingest=False)

        self.assertEqual([row["id"] for row in client.documents], [2, 10])


if __name__ == "__main__":
    unittest.main()
