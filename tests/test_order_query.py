import unittest
from types import SimpleNamespace
from unittest.mock import patch

from orderbot.database import find_orders, get_messages


class Query:
    def __init__(self):
        self.filters = []
        self.execution_count = 0

    def select(self, fields):
        self.fields = fields
        return self

    def eq(self, field, value):
        self.filters.append(("eq", field, value))
        return self

    def ilike(self, field, value):
        self.filters.append(("ilike", field, value))
        return self

    def execute(self):
        self.execution_count += 1
        return SimpleNamespace(data=[{"public_order_id": 42}])


class Client:
    def __init__(self, query):
        self.query = query

    def table(self, name):
        self.table_name = name
        return self.query


class OrderQueryTests(unittest.TestCase):
    def test_all_supplied_identity_fields_filter_one_database_query(self):
        query = Query()
        with patch("orderbot.database._client", return_value=Client(query)):
            result = find_orders(
                {"order_id": "42", "email": "JANE@EXAMPLE.TEST", "phone": "+1 (234) 567-8901", "name": "Jo_n%"}
            )

        self.assertEqual(result, [{"public_order_id": 42}])
        self.assertEqual(query.execution_count, 1)
        self.assertEqual(
            query.filters,
            [
                ("eq", "public_order_id", 42),
                ("eq", "customers.email", "jane@example.test"),
                ("eq", "customers.phone", "2345678901"),
                ("ilike", "customers.full_name", "%Jon%"),
            ],
        )

    def test_history_returns_latest_messages_in_chronological_order(self):
        class MessageQuery(Query):
            def order(self, field, desc=False):
                self.ordering = (field, desc)
                return self

            def limit(self, count):
                self.limit_count = count
                return self

            def execute(self):
                return SimpleNamespace(data=[{"id": "new"}, {"id": "old"}])

        query = MessageQuery()
        with patch("orderbot.database._client", return_value=Client(query)):
            result = get_messages("conversation", limit=2)

        self.assertEqual([message["id"] for message in result], ["old", "new"])
        self.assertEqual(query.ordering, ("message_seq", True))
        self.assertEqual(query.limit_count, 2)


if __name__ == "__main__":
    unittest.main()
