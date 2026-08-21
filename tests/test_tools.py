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
