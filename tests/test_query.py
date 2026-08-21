"""Tests for query.py."""
import query


class TestExtractOrderIds:
    def test_order_number_is_phrase(self):
        info = query.extract_info_from_query("order number is 42")
        assert info["order_ids"] == ["42"]

    def test_hash_prefix(self):
        info = query.extract_info_from_query("status of #123 please")
        assert "123" in info["order_ids"]

    def test_bare_its_phrase(self):
        info = query.extract_info_from_query("it's 7")
        assert "7" in info["order_ids"]

    def test_out_of_range_order_id_excluded(self):
        # 1500 is outside the supported 1-1000 range and should be dropped.
        info = query.extract_info_from_query("order 1500")
        assert info["order_ids"] == []

    def test_no_duplicate_order_ids(self):
        info = query.extract_info_from_query("order 42, order #42 again")
        assert info["order_ids"] == ["42"]


class TestExtractEmail:
    def test_extracts_valid_email(self):
        info = query.extract_info_from_query("my email is jane@example.com")
        assert info["emails"] == ["jane@example.com"]

    def test_no_email_present(self):
        info = query.extract_info_from_query("order 42")
        assert info["emails"] == []


class TestExtractPhone:
    def test_extracts_plain_10_digit_phone(self):
        info = query.extract_info_from_query("phone 1234567890")
        assert info["phones"] == ["1234567890"]

    def test_extracts_dashed_phone(self):
        info = query.extract_info_from_query("call me at 123-456-7890")
        assert info["phones"] == ["1234567890"]


class TestExtractName:
    def test_my_name_is_phrase(self):
        info = query.extract_info_from_query("my name is Jane Doe")
        assert "Jane Doe" in info["names"]

    def test_name_colon_phrase(self):
        info = query.extract_info_from_query("name: Jane Doe")
        assert "Jane Doe" in info["names"]

    def test_names_with_digits_are_rejected(self):
        info = query.extract_info_from_query("my name is Jane 42")
        assert not any(any(c.isdigit() for c in name) for name in info["names"])

    def test_prose_after_name_is_not_swallowed(self):
        # Regression: the loose "name <space>" pattern used to capture
        # "on it should be gaurang" wholesale, failing a legitimate lookup.
        info = query.extract_info_from_query("name on it should be gaurang")
        assert info["names"] == ["gaurang"]

    def test_customer_is_phrase(self):
        info = query.extract_info_from_query("customer is Priya Sharma")
        assert "Priya Sharma" in info["names"]

    def test_overlapping_name_patterns_dedupe(self):
        info = query.extract_info_from_query("name is Jane Doe")
        assert info["names"].count("Jane Doe") == 1


class TestCountAndHasLookupFields:
    def test_count_lookup_fields_two(self):
        assert query.count_lookup_fields("order 42, email jane@example.com") == 2

    def test_count_lookup_fields_zero(self):
        assert query.count_lookup_fields("hello there") == 0

    def test_has_lookup_identifier_true_for_named_field(self):
        assert query.has_lookup_identifier("order 42") is True

    def test_has_lookup_identifier_true_for_bare_hash(self):
        assert query.has_lookup_identifier("#42") is True

    def test_has_lookup_identifier_false_for_greeting(self):
        assert query.has_lookup_identifier("hello, how are you?") is False


class TestContextFieldsForQuery:
    def test_delivery_intent(self):
        fields = query.context_fields_for_query("when will it arrive?")
        assert fields == {"delivery"}

    def test_tracking_intent(self):
        fields = query.context_fields_for_query("what's the tracking number?")
        assert fields == {"tracking", "carrier"}

    def test_multiple_intents_union(self):
        fields = query.context_fields_for_query("who is the driver and when will it arrive?")
        assert fields == {"driver", "delivery"}

    def test_no_matched_intent_returns_none(self):
        assert query.context_fields_for_query("thanks!") is None

    def test_empty_query_returns_none(self):
        assert query.context_fields_for_query("") is None


class TestNormalizeFieldKeys:
    def test_aliases_map_to_canonical_keys(self):
        assert query.normalize_field_keys(["tracking_number", "payment_status"]) == {"tracking", "payment"}

    def test_unknown_fields_dropped(self):
        assert query.normalize_field_keys(["not_a_real_field"]) == set()

    def test_empty_input(self):
        assert query.normalize_field_keys([]) == set()
        assert query.normalize_field_keys(None) == set()


class TestFormatDatabaseContext:
    def _db_results(self, order):
        return {"matched": [order]}

    def test_no_filter_includes_all_available_fields(self, sample_order):
        context = query.format_database_context(self._db_results(sample_order))
        assert "Order number: 42" in context
        assert "Status: In Transit" in context
        assert "Tracking number: 1Z999AA10123456784" in context
        assert "Delivery driver: Bob" in context

    def test_query_filters_to_intent_plus_baseline(self, sample_order):
        context = query.format_database_context(self._db_results(sample_order), query="who is my driver?")
        assert "Order number: 42" in context  # baseline always included
        assert "Status: In Transit" in context  # baseline always included
        assert "Delivery driver: Bob" in context
        assert "Tracking number" not in context  # not requested, should be filtered out

    def test_explicit_fields_override_query(self, sample_order):
        context = query.format_database_context(
            self._db_results(sample_order), query="who is my driver?", fields=["tracking"]
        )
        assert "Tracking number: 1Z999AA10123456784" in context
        assert "Delivery driver" not in context

    def test_missing_shipment_falls_back_to_not_available(self, sample_order_no_shipment):
        context = query.format_database_context(self._db_results(sample_order_no_shipment))
        assert "Delivery estimate: Not available" in context
