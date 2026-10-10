import json
import unittest
from unittest.mock import patch

from orderbot.contracts import OrderLookup, PolicyChunk, PolicySearchResult
from orderbot.tools import get_order_status, search_policies


class ReadToolBehaviorTests(unittest.TestCase):
    @patch("orderbot.tools.perform_order_lookup")
    def test_missing_identity_returns_no_order_data(self, lookup):
        lookup.return_value = OrderLookup("need_more_info")
        payload = json.loads(get_order_status.invoke({"order_id": 42}))

        self.assertEqual(payload["status"], "need_more_info")
        self.assertNotIn("orders", payload)
        self.assertNotIn("context", payload)

    @patch("orderbot.tools.perform_order_lookup")
    def test_wrong_identity_is_generic_and_contains_no_order_data(self, lookup):
        lookup.return_value = OrderLookup("not_found")
        payload = json.loads(get_order_status.invoke({"order_id": 42, "email": "wrong@example.test"}))

        self.assertEqual(payload["status"], "verification_failed")
        self.assertNotIn("orders", payload)
        self.assertNotIn("context", payload)

    @patch("orderbot.tools.perform_order_lookup")
    def test_verified_lookup_returns_bounded_order_data(self, lookup):
        lookup.return_value = OrderLookup(
            "found",
            orders=[{"public_order_id": 42, "status": "In Transit", "tracking_number": "TRACK-42"}],
        )
        payload = json.loads(get_order_status.invoke({"order_id": 42, "email": "jane@example.test"}))

        self.assertEqual(payload["status"], "found")
        self.assertEqual(payload["orders"][0]["public_order_id"], 42)
        self.assertIn("In Transit", payload["context"])

    @patch("orderbot.tools.perform_order_lookup")
    def test_name_only_lookup_is_marked_limited(self, lookup):
        lookup.return_value = OrderLookup(
            "found", orders=[{"public_order_id": 42, "status": "In Transit"}], name_only=True
        )
        payload = json.loads(get_order_status.invoke({"order_id": 42, "name": "Jane Doe", "fields": ["payment"]}))

        self.assertEqual(payload["verification_level"], "name_limited")
        self.assertNotIn("payment_status", payload["orders"][0])
        self.assertNotIn("Jane Doe", payload["context"])

    @patch("orderbot.tools.search_policy_chunks")
    def test_policy_no_match_is_distinct(self, search):
        search.return_value = PolicySearchResult("no_match")
        self.assertEqual(json.loads(search_policies.invoke({"query": "returns"})), {"status": "no_match"})

    @patch("orderbot.tools.search_policy_chunks")
    def test_policy_failure_is_distinct(self, search):
        search.return_value = PolicySearchResult("unavailable")
        self.assertEqual(json.loads(search_policies.invoke({"query": "returns"})), {"status": "unavailable"})

    @patch("orderbot.tools.search_policy_chunks")
    def test_policy_matches_keep_source_title_and_heading(self, search):
        search.return_value = PolicySearchResult(
            "found", [PolicyChunk(source_title="Refunds", heading="Eligibility", content="Within 30 days.")]
        )
        payload = json.loads(search_policies.invoke({"query": "refund"}))

        self.assertEqual(payload["status"], "found")
        self.assertEqual(payload["chunks"][0]["source_title"], "Refunds")
        self.assertEqual(payload["chunks"][0]["heading"], "Eligibility")


if __name__ == "__main__":
    unittest.main()
