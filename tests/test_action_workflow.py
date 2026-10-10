import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from orderbot.actions import stage_action, submit_confirmed_action
from orderbot.chat import OrderChatHandler


class FakeResponse:
    def __init__(self, data=None):
        self.data = data or []


class FakeQuery:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.row = None
        self.selected = False
        self.filters = {}

    def insert(self, row):
        self.row = row
        return self

    def select(self, _fields):
        self.selected = True
        return self

    def eq(self, field, value):
        self.filters[field] = value
        return self

    def limit(self, _count):
        return self

    def execute(self):
        if self.error:
            raise self.error
        return self.response


def make_handler():
    with (
        patch("orderbot.chat.chatmodel", return_value=MagicMock()),
        patch("orderbot.chat.build_agent", return_value=MagicMock()),
    ):
        return OrderChatHandler(stored_messages=[])


def strongly_verify(handler, order_id=42, age=timedelta(seconds=0)):
    handler.last_db_results = {"matched": [{"public_order_id": order_id, "status": "Processing"}]}
    handler.strong_verified_order_id = order_id
    handler.strong_verified_at = datetime.now(timezone.utc) - age
    handler.name_only_verified = False


def test_stage_creates_random_short_lived_request_without_database_access():
    first = stage_action("return_request", 42, "Changed my mind", None)
    second = stage_action("return_request", 42, "Changed my mind", None)

    assert first["request_key"] != second["request_key"]
    assert first["action"] == "return_request"
    assert first["order_id"] == 42
    assert first["expires_at"] - first["created_at"] == timedelta(minutes=5)


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("Email me at priya@example.test", "Email me at [redacted email]"),
        ("Call +1 212-555-0188", "[Verification details redacted]"),
        ("Order 42 was a duplicate", "[Verification details redacted]"),
    ],
)
def test_staged_reason_is_privacy_filtered(reason, expected):
    pending = stage_action("cancel_request", 42, reason, None)
    assert pending["reason"] == expected


def test_expired_request_does_not_attempt_database_write():
    pending = stage_action("cancel_request", 42, "Duplicate order", None)
    pending["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)

    with patch("orderbot.actions.supabase_server_client") as client:
        assert submit_confirmed_action(pending) is None
        client.assert_not_called()


def test_confirmed_request_inserts_only_support_request_fields():
    expected = {"id": 7, "status": "submitted"}
    query = FakeQuery(FakeResponse([expected]))
    client = MagicMock()
    client.table.return_value = query
    pending = stage_action("return_request", 42, "Wrong size", "conversation-1")

    with patch("orderbot.actions.supabase_server_client", return_value=client):
        result = submit_confirmed_action(pending)

    assert result == expected
    assert query.row["order_id"] == 42
    assert query.row["action_type"] == "return_request"
    assert query.row["status"] == "submitted"
    assert query.row["conversation_id"] == "conversation-1"
    assert "request_key" in query.row
    assert "cancelled_at" not in query.row
    client.table.assert_called_once_with("action_requests")


def test_insert_retry_resolves_duplicate_by_idempotency_key():
    row = {"id": 4, "request_key": "key", "status": "submitted"}
    insert_query = FakeQuery(error=RuntimeError("duplicate"))
    read_query = FakeQuery(FakeResponse([row]))
    client = MagicMock()
    client.table.side_effect = [insert_query, read_query]
    pending = stage_action("cancel_request", 42, "Duplicate", None)
    pending["request_key"] = "key"

    with patch("orderbot.actions.supabase_server_client", return_value=client):
        result = submit_confirmed_action(pending)

    assert result == row
    assert read_query.selected is True
    assert read_query.filters == {"request_key": "key"}


def test_failed_insert_and_idempotency_lookup_return_no_success():
    query = FakeQuery(error=RuntimeError("database unavailable"))
    client = MagicMock()
    client.table.return_value = query
    pending = stage_action("return_request", 42, "Wrong size", None)

    with patch("orderbot.actions.supabase_server_client", return_value=client):
        assert submit_confirmed_action(pending) is None


@pytest.mark.parametrize(
    ("order_id", "verified", "name_only", "age", "expected"),
    [
        (42, False, False, timedelta(0), "verification_required"),
        (42, True, True, timedelta(0), "verification_required"),
        (41, True, False, timedelta(0), "verification_required"),
        (42, True, False, timedelta(minutes=11), "verification_required"),
        (42, True, False, timedelta(minutes=9), "pending_confirmation"),
    ],
)
def test_action_tool_requires_recent_strong_verification(order_id, verified, name_only, age, expected):
    handler = make_handler()
    if verified:
        strongly_verify(handler, age=age)
    handler.name_only_verified = name_only
    tools = {item.name: item for item in handler._make_action_tools()}

    result = json.loads(tools["request_return"].invoke({"order_id": order_id, "reason": "Wrong size"}))

    assert result["status"] == expected
    assert (handler.pending_action is not None) is (expected == "pending_confirmation")


@pytest.mark.parametrize(
    ("tool_name", "args", "expected_action"),
    [
        ("request_return", {"order_id": 42, "reason": "Wrong size"}, "return_request"),
        ("cancel_order", {"order_id": 42}, "cancel_request"),
    ],
)
def test_action_tools_only_stage_pending_request(tool_name, args, expected_action):
    handler = make_handler()
    strongly_verify(handler)
    tools = {item.name: item for item in handler._make_action_tools()}

    with patch("orderbot.chat.submit_confirmed_action") as submit:
        result = json.loads(tools[tool_name].invoke(args))

    assert result["status"] == "pending_confirmation"
    assert handler.pending_action["action"] == expected_action
    assert handler.pending_action["order_id"] == 42
    submit.assert_not_called()


@pytest.mark.parametrize(
    ("tool_name", "reason"),
    [("request_return", "  "), ("request_return", "x" * 301), ("cancel_order", "x" * 301)],
)
def test_action_tools_reject_empty_or_oversized_reason(tool_name, reason):
    handler = make_handler()
    strongly_verify(handler)
    tools = {item.name: item for item in handler._make_action_tools()}

    result = json.loads(tools[tool_name].invoke({"order_id": 42, "reason": reason}))

    assert result["status"] == "invalid_reason"
    assert handler.pending_action is None


def test_confirmation_requires_matching_pending_request_key():
    handler = make_handler()
    strongly_verify(handler)
    handler.pending_action = stage_action("cancel_request", 42, "Duplicate", None)

    with patch("orderbot.chat.submit_confirmed_action") as submit:
        success, message = handler.confirm_pending_action("wrong-key")

    assert success is False
    assert "expired" in message
    assert handler.pending_action is not None
    submit.assert_not_called()


def test_confirmation_rechecks_verification_before_write():
    handler = make_handler()
    strongly_verify(handler)
    handler.pending_action = stage_action("cancel_request", 42, "Duplicate", None)
    handler._clear_strong_verification()

    with patch("orderbot.chat.submit_confirmed_action") as submit:
        success, message = handler.confirm_pending_action(handler.pending_action["request_key"])

    assert success is False
    assert "verify" in message
    assert handler.pending_action is None
    submit.assert_not_called()


def test_confirmation_rejects_expired_pending_action_without_write():
    handler = make_handler()
    strongly_verify(handler)
    handler.pending_action = stage_action("return_request", 42, "Wrong size", None)
    handler.pending_action["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)

    with patch("orderbot.chat.submit_confirmed_action") as submit:
        success, message = handler.confirm_pending_action(handler.pending_action["request_key"])

    assert success is False
    assert "expired" in message
    assert handler.pending_action is None
    submit.assert_not_called()


def test_successful_confirmation_clears_pending_and_never_claims_order_changed():
    handler = make_handler()
    strongly_verify(handler)
    handler.pending_action = stage_action("cancel_request", 42, "Duplicate", None)
    request_key = handler.pending_action["request_key"]

    with (
        patch("orderbot.chat.submit_confirmed_action", return_value={"id": 9}),
        patch.object(handler, "_persist_message"),
    ):
        success, message = handler.confirm_pending_action(request_key)

    assert success is True
    assert "recorded for human review" in message
    assert "order has not been changed" in message
    assert handler.pending_action is None


def test_failed_confirmation_does_not_clear_pending_or_claim_success():
    handler = make_handler()
    strongly_verify(handler)
    handler.pending_action = stage_action("return_request", 42, "Wrong size", None)
    request_key = handler.pending_action["request_key"]

    with patch("orderbot.chat.submit_confirmed_action", return_value=None):
        success, message = handler.confirm_pending_action(request_key)

    assert success is False
    assert "couldn't submit" in message
    assert handler.pending_action["request_key"] == request_key
