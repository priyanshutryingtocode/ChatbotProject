import json

import tools


class TestLookupOrderTool:
    def test_needs_more_info_with_only_order_id(self, monkeypatch):
        # lookup_order is a @tool-decorated function; call it via .invoke()
        # the way LangChain does, rather than as a plain Python function.
        payload = json.loads(tools.lookup_order.invoke({"order_id": 42}))
        assert payload["status"] == "need_more_info"

    def test_not_found_when_no_match(self, monkeypatch):
        monkeypatch.setattr(tools, "find_orders", lambda criteria, require_order_id=True: [])
        payload = json.loads(
            tools.lookup_order.invoke({"order_id": 42, "email": "jane@example.com"})
        )
        assert payload["status"] == "not_found"

    def test_found_returns_context_and_summary(self, monkeypatch, sample_order):
        monkeypatch.setattr(tools, "find_orders", lambda criteria, require_order_id=True: [sample_order])
        payload = json.loads(
            tools.lookup_order.invoke({"order_id": 42, "email": "jane@example.com"})
        )
        assert payload["status"] == "found"
        assert payload["orders"][0]["public_order_id"] == 42
        assert "Order number: 42" in payload["context"]

    def test_fields_filter_narrows_context(self, monkeypatch, sample_order):
        monkeypatch.setattr(tools, "find_orders", lambda criteria, require_order_id=True: [sample_order])
        payload = json.loads(
            tools.lookup_order.invoke(
                {"order_id": 42, "email": "jane@example.com", "fields": ["tracking"]}
            )
        )
        assert "Tracking number" in payload["context"]
        assert "Delivery driver" not in payload["context"]

    def test_unknown_field_names_are_ignored_not_fatal(self, monkeypatch, sample_order):
        monkeypatch.setattr(tools, "find_orders", lambda criteria, require_order_id=True: [sample_order])
        payload = json.loads(
            tools.lookup_order.invoke(
                {"order_id": 42, "email": "jane@example.com", "fields": ["not_a_real_field"]}
            )
        )
        assert payload["status"] == "found"


class TestSearchPolicyTool:
    def test_found_returns_labelled_context(self, monkeypatch):
        monkeypatch.setattr(
            tools, "retrieve_policies", lambda question, k=4: "[source: Returns › Return window]\n30-day window."
        )
        payload = json.loads(tools.search_policy.invoke({"question": "what is the return window?"}))
        assert payload["status"] == "found"
        assert "RETRIEVED POLICIES" in payload["context"]
        assert "30-day window" in payload["context"]
        assert "[source: Returns" in payload["context"]

    def test_no_match_when_retriever_finds_nothing(self, monkeypatch):
        monkeypatch.setattr(tools, "retrieve_policies", lambda question, k=4: None)
        payload = json.loads(tools.search_policy.invoke({"question": "quantum entanglement?"}))
        assert payload["status"] == "no_match"

    def test_passes_question_through_to_retriever(self, monkeypatch):
        seen = {}
        def fake_retrieve(question, k=4):
            seen["question"] = question
            return None

        monkeypatch.setattr(tools, "retrieve_policies", fake_retrieve)
        tools.search_policy.invoke({"question": "shipping charges?"})
        assert seen["question"] == "shipping charges?"
