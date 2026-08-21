import pytest

import database


# ---------------------------------------------------------------------------
# Pure formatters — no mocking needed
# ---------------------------------------------------------------------------

class TestFormatOrderNumber:
    def test_pads_to_four_digits(self):
        assert database.format_order_number(7) == "0007"

    def test_accepts_string_input(self):
        assert database.format_order_number("42") == "0042"

    def test_invalid_input_returns_na(self):
        assert database.format_order_number(None) == "N/A"
        assert database.format_order_number("not-a-number") == "N/A"


class TestFormatTimestamp:
    def test_formats_iso_z_timestamp(self):
        result = database.format_timestamp("2026-08-05T15:00:00Z")
        assert "2026" in result
        assert "UTC" in result

    def test_missing_timestamp(self):
        assert database.format_timestamp(None) == "Not available"
        assert database.format_timestamp("") == "Not available"

    def test_unparseable_timestamp_returned_as_is(self):
        assert database.format_timestamp("not-a-date") == "not-a-date"


class TestFormatOrderForDisplay:
    def test_none_order(self):
        assert database.format_order_for_display(None) == "No order data available"

    def test_full_order_includes_key_fields(self, sample_order):
        text = database.format_order_for_display(sample_order)
        assert "Order #0042" in text
        assert "Jane Doe" in text
        assert "jane@example.com" in text
        assert "Tracking: 1Z999AA10123456784" in text
        assert "Driver: Bob" in text

    def test_order_without_shipment_omits_shipment_lines(self, sample_order_no_shipment):
        text = database.format_order_for_display(sample_order_no_shipment)
        assert "Tracking:" not in text
        assert "Driver:" not in text


# ---------------------------------------------------------------------------
# find_orders — business logic (isolated from Supabase via monkeypatched
# per-field lookup functions)
# ---------------------------------------------------------------------------

class TestFindOrdersValidation:
    def test_requires_order_id_by_default(self):
        with pytest.raises(ValueError):
            database.find_orders({"email": "jane@example.com"})

    def test_requires_at_least_two_fields(self):
        with pytest.raises(ValueError):
            database.find_orders({"order_id": "42"})

    def test_blank_values_are_stripped_before_validation(self):
        with pytest.raises(ValueError):
            database.find_orders({"order_id": "42", "email": "   "})


class TestFindOrdersMatching:
    def test_matching_order_id_and_email_returns_order(self, monkeypatch, sample_order):
        monkeypatch.setattr(database, "get_order_by_id", lambda oid: sample_order)
        monkeypatch.setattr(database, "get_orders_by_email", lambda email: [sample_order])

        result = database.find_orders({"order_id": "42", "email": "jane@example.com"})
        assert result == [sample_order]

    def test_mismatched_fields_return_empty(self, monkeypatch, sample_order):
        other_order = dict(sample_order, public_order_id=99)
        monkeypatch.setattr(database, "get_order_by_id", lambda oid: sample_order)
        monkeypatch.setattr(database, "get_orders_by_email", lambda email: [other_order])

        result = database.find_orders({"order_id": "42", "email": "someone-else@example.com"})
        assert result == []

    def test_any_empty_field_result_short_circuits_to_empty(self, monkeypatch, sample_order):
        monkeypatch.setattr(database, "get_order_by_id", lambda oid: sample_order)
        monkeypatch.setattr(database, "get_orders_by_phone", lambda phone: [])  # no match on phone

        result = database.find_orders({"order_id": "42", "phone": "0000000000"})
        assert result == []


# ---------------------------------------------------------------------------
# Supabase-boundary functions — faked client, verifying wiring/shape rather
# than Supabase's actual server-side filtering (that belongs in an
# integration test against a real/test Supabase project).
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """Chainable stand-in for supabase-py's query builder."""

    def __init__(self, data):
        self._data = data

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def in_(self, *a, **k):
        return self

    def ilike(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def update(self, *a, **k):
        return self

    def insert(self, rows, **k):
        batch = rows if isinstance(rows, list) else [rows]
        # Mimic Postgres column defaults (gen_random_uuid()) supplying
        # server-generated ids on every inserted row.
        self._data = [dict(row, id=f"generated-{n}") for n, row in enumerate(batch)]
        return self

    def execute(self):
        return _FakeResponse(self._data)


class _FakeClient:
    def __init__(self, table_data: dict):
        self._table_data = table_data

    def table(self, name):
        return _FakeQuery(self._table_data.get(name, []))


@pytest.fixture
def fake_client(monkeypatch, sample_order):
    client = _FakeClient(
        {
            "orders": [sample_order],
            "customers": [{"id": "cust-1"}],
            "conversations": [{"id": "conv-1"}],
            "messages": [{"id": "msg-1"}],
        }
    )
    monkeypatch.setattr(database, "_client", lambda: client)
    return client


class TestSupabaseWiring:
    def test_get_order_by_id_returns_first_row(self, fake_client, sample_order):
        assert database.get_order_by_id(42) == sample_order

    def test_get_order_by_id_invalid_id_returns_none(self, fake_client):
        assert database.get_order_by_id("not-a-number") is None

    def test_get_orders_by_email_returns_orders(self, fake_client, sample_order):
        assert database.get_orders_by_email("jane@example.com") == [sample_order]

    def test_get_orders_by_phone_returns_orders(self, fake_client, sample_order):
        assert database.get_orders_by_phone("123-456-7890") == [sample_order]

    def test_search_orders_by_name_returns_orders(self, fake_client, sample_order):
        assert database.search_orders_by_name("Jane") == [sample_order]

    def test_create_conversation_returns_generated_id(self, fake_client):
        assert database.create_conversation() == "generated-0"

    def test_append_message_returns_generated_id(self, fake_client):
        assert database.append_message("conv-1", "user", "hello") == "generated-0"

    def test_supabase_errors_are_swallowed_not_raised(self, monkeypatch, sample_order):
        def raise_error():
            raise RuntimeError("connection refused")

        monkeypatch.setattr(database, "_client", raise_error)
        # None of these should raise — they should log and return an empty/falsy result.
        assert database.get_orders_by_email("jane@example.com") == []
        assert database.get_orders_by_phone("1234567890") == []
        assert database.search_orders_by_name("Jane") == []
        assert database.create_conversation() == ""
