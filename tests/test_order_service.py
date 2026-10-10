import unittest
from unittest.mock import patch

from orderbot.database import DatabaseLookupError
from orderbot.orders import lookup_order


class OrderLookupServiceTests(unittest.TestCase):
    @patch("orderbot.orders.find_orders")
    def test_name_verification_returns_limited_result(self, find_orders):
        find_orders.return_value = [{"public_order_id": 42}]

        result = lookup_order({"order_id": "0042", "name": "Jane Doe"})

        self.assertEqual(result.status, "found")
        self.assertTrue(result.name_only)
        self.assertEqual(result.orders, [{"public_order_id": 42}])
        find_orders.assert_called_once_with({"order_id": 42, "name": "Jane Doe"})

    @patch("orderbot.orders.find_orders")
    def test_rejects_out_of_range_order_before_query(self, find_orders):
        result = lookup_order({"order_id": 1001, "email": "jane@example.test"})

        self.assertEqual(result.status, "invalid_order_id")
        find_orders.assert_not_called()

    @patch("orderbot.orders.find_orders")
    def test_requires_order_number_and_secondary_identifier(self, find_orders):
        self.assertEqual(lookup_order({"email": "jane@example.test"}).status, "need_more_info")
        self.assertEqual(lookup_order({"order_id": 42}).status, "need_more_info")
        find_orders.assert_not_called()

    @patch("orderbot.orders.find_orders", side_effect=DatabaseLookupError)
    def test_distinguishes_database_outage_from_invalid_identity(self, find_orders):
        result = lookup_order({"order_id": 42, "email": "jane@example.test"})

        self.assertEqual(result.status, "unavailable")
        find_orders.assert_called_once()


if __name__ == "__main__":
    unittest.main()
