"""Tests for retriever.py policy/FAQ retrieval over pgvector."""

import pytest

import retriever


class _FakeResponse:
    def __init__(self, data):
        self.data = data


class _FakeRPC:
    def __init__(self, data):
        self._data = data
        self.rpc_calls: list[tuple] = []

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        return self

    def execute(self):
        return _FakeResponse(self._data)


def _patch_retriever(monkeypatch, rows, embed_vector=None):
    fake_rpc = _FakeRPC(rows)
    monkeypatch.setattr(retriever, "_client", lambda: fake_rpc)
    monkeypatch.setattr(retriever, "_embed_query", lambda query: embed_vector or [0.1] * 768)
    return fake_rpc


class TestRetrievePolicies:
    def test_formats_sources_and_content(self, monkeypatch):
        _patch_retriever(monkeypatch, [{"doc_title": "Refunds", "heading": "Refund timelines", "content": " Refunds take 5-7 days. ", "similarity": 0.91}])
        result = retriever.retrieve_policies("how long do refunds take")
        assert "[source: Refunds > Refund timelines]" in result
        assert "Refunds take 5-7 days." in result

    def test_headingless_source_has_no_separator(self, monkeypatch):
        _patch_retriever(monkeypatch, [{"doc_title": "Returns", "heading": None, "content": "Body text."}])
        result = retriever.retrieve_policies("returns")
        assert "[source: Returns]" in result
        assert ">" not in result

    def test_passes_embedding_and_match_count_to_rpc(self, monkeypatch):
        vector = [0.5] * 768
        fake = _patch_retriever(monkeypatch, [], embed_vector=vector)
        retriever.retrieve_policies("shipping cost", k=2)
        name, params = fake.rpc_calls[0]
        assert name == "match_knowledge_chunks"
        assert params["query_embedding"] == vector
        assert params["match_count"] == 2
        assert params["min_similarity"] == retriever.MIN_SIMILARITY

    def test_empty_matches_return_none(self, monkeypatch):
        _patch_retriever(monkeypatch, [])
        assert retriever.retrieve_policies("anything") is None

    def test_returns_raw_score_qualified_matches_for_evaluation(self, monkeypatch):
        rows = [{"doc_title": "Returns Policy", "similarity": 0.72}]
        _patch_retriever(monkeypatch, rows)
        assert retriever.retrieve_policy_matches("return window") == rows

    def test_client_error_returns_none_not_raised(self, monkeypatch):
        def broken_client():
            raise RuntimeError("connection refused")

        monkeypatch.setattr(retriever, "_client", broken_client)
        monkeypatch.setattr(retriever, "_embed_query", lambda query: [0.0] * 768)
        assert retriever.retrieve_policies("anything") is None

    def test_blank_query_short_circuits_without_db_call(self, monkeypatch):
        def forbidden_client():
            raise AssertionError("client should not be called")

        monkeypatch.setattr(retriever, "_client", forbidden_client)
        assert retriever.retrieve_policies("   ") is None
        assert retriever.retrieve_policies("") is None
        assert retriever.retrieve_policies(None) is None


class TestFormatPolicyContext:
    def test_none_for_no_matches(self):
        assert retriever.format_policy_context([]) is None

    def test_multiple_matches_joined_with_blank_lines(self):
        matches = [{"doc_title": "A", "heading": "One", "content": "alpha"}, {"doc_title": "B", "heading": None, "content": "beta"}]
        result = retriever.format_policy_context(matches)
        assert "[source: A > One]" in result and "alpha" in result
        assert "[source: B]" in result and "beta" in result


class TestFitDimensions:
    def test_exact_dims_pass_through(self):
        vector = [0.1] * retriever.OUTPUT_DIMENSIONALITY
        assert retriever.fit_dimensions([vector]) == [vector]

    def test_oversized_vectors_truncated_matryoshka_style(self):
        oversized = [1.0, 2.0] * (retriever.OUTPUT_DIMENSIONALITY // 2) * 3
        fitted = retriever.fit_dimensions([oversized])
        assert len(fitted[0]) == retriever.OUTPUT_DIMENSIONALITY
        assert fitted[0][0] == 1.0 and fitted[0][-1] == 2.0

    def test_shorter_vectors_raise_loudly(self):
        with pytest.raises(ValueError):
            retriever.fit_dimensions([[0.5] * 384])

    def test_empty_input_ok(self):
        assert retriever.fit_dimensions([]) == []
