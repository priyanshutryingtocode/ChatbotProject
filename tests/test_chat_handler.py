from unittest.mock import MagicMock

import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import chat_handler
import tools as tools_namespace
from chat_handler import OrderChatHandler, _looks_like_bare_name


@pytest.fixture(autouse=True)
def patch_llm_and_persistence(monkeypatch):
    """Every test in this module gets a handler that never touches Gemini or
    Supabase. Tests that care about find_orders' return value override it
    per-test with monkeypatch.setattr(chat_handler, "find_orders", ...).
    """
    monkeypatch.setattr(chat_handler, "chatmodel", lambda: MagicMock())
    monkeypatch.setattr(chat_handler, "build_llm_with_tools", lambda: MagicMock())
    monkeypatch.setattr(chat_handler, "create_conversation", lambda *a, **k: "conv-1")
    monkeypatch.setattr(chat_handler, "append_message", lambda *a, **k: "msg-1")
    monkeypatch.setattr(chat_handler, "get_messages", lambda *a, **k: [])


def _scripted_tool_llm(monkeypatch, first_calls, final_text):
    """Replace llm_with_tools with a MagicMock that plays a scripted tool loop."""
    first = AIMessage(content="", tool_calls=first_calls)
    final = AIMessage(content=final_text)
    scripted = MagicMock()
    scripted.invoke.side_effect = [first, final]
    monkeypatch.setattr(chat_handler, "build_llm_with_tools", lambda: scripted)
    return scripted


class TestLooksLikeBareName:
    def test_simple_name_accepted(self):
        assert _looks_like_bare_name("Jane Doe") is True

    def test_greeting_rejected(self):
        assert _looks_like_bare_name("hello") is False

    def test_question_rejected(self):
        assert _looks_like_bare_name("where is it") is False

    def test_too_long_rejected(self):
        assert _looks_like_bare_name("a " * 40) is False

    def test_contains_digits_rejected(self):
        assert _looks_like_bare_name("Jane 42") is False


class TestEmptyInput:
    def test_empty_message_returns_greeting(self):
        handler = OrderChatHandler()
        response, db_results = handler.process_user_message("")
        assert "two details" in response.lower()
        assert db_results == {}


class TestTwoFieldVerificationFlow:
    def test_order_id_and_email_in_one_message_runs_lookup(self, monkeypatch, sample_order):
        monkeypatch.setattr(chat_handler, "find_orders", lambda criteria, require_order_id=True: [sample_order])
        handler = OrderChatHandler()

        response, db_results = handler.process_user_message(
            "check order 42, email jane@example.com"
        )

        assert db_results == {"matched": [sample_order]}
        assert "Order #0042" in response
        assert "In Transit" in response

    def test_order_id_only_asks_for_second_field(self):
        handler = OrderChatHandler()
        response, db_results = handler.process_user_message("order 42")

        assert db_results == {}
        assert "0042" in response
        assert "email" in response.lower() or "phone" in response.lower()

    def test_second_field_arrives_in_a_later_message(self, monkeypatch, sample_order):
        monkeypatch.setattr(chat_handler, "find_orders", lambda criteria, require_order_id=True: [sample_order])
        handler = OrderChatHandler()

        handler.process_user_message("order 42")
        response, db_results = handler.process_user_message("my email is jane@example.com")

        assert db_results == {"matched": [sample_order]}
        assert "Order #0042" in response

    def test_mismatched_details_return_not_found_and_reset_pending(self, monkeypatch):
        monkeypatch.setattr(chat_handler, "find_orders", lambda criteria, require_order_id=True: [])
        handler = OrderChatHandler()

        response, db_results = handler.process_user_message(
            "order 42, email nobody@example.com"
        )

        assert db_results == {}
        assert "couldn't find" in response.lower()
        assert handler.pending_identity == {}

    def test_cancel_mid_verification_resets_state(self):
        handler = OrderChatHandler()
        handler.process_user_message("order 42")
        response, db_results = handler.process_user_message("never mind")

        assert handler.pending_identity == {}
        assert "start over" in response.lower()

    def test_new_order_id_invalidates_previously_collected_fields(self):
        # Only the order id is given (no secondary field yet), so no lookup
        # fires and pending_identity stays populated between turns.
        handler = OrderChatHandler()
        handler.process_user_message("order 42")
        assert handler.pending_identity == {"order_id": "42"}

        # A second, different order number should reset — not append to —
        # whatever was collected so far.
        handler.process_user_message("actually order 99")
        assert handler.pending_identity == {"order_id": "99"}


class TestFollowUpFromStoredContext:
    def test_follow_up_uses_deterministic_formatting_not_the_llm(self, monkeypatch, sample_order):
        monkeypatch.setattr(chat_handler, "find_orders", lambda criteria, require_order_id=True: [sample_order])
        handler = OrderChatHandler()
        handler.process_user_message("order 42, email jane@example.com")

        response, db_results = handler.process_user_message("who is the driver?")

        assert "Bob" in response
        # The mocked LLM should never have been invoked for this deterministic path.
        handler.llm.invoke.assert_not_called()

    def test_tracking_follow_up(self, monkeypatch, sample_order):
        monkeypatch.setattr(chat_handler, "find_orders", lambda criteria, require_order_id=True: [sample_order])
        handler = OrderChatHandler()
        handler.process_user_message("order 42, email jane@example.com")

        response, _ = handler.process_user_message("what's my tracking number?")
        assert "1Z999AA10123456784" in response


class TestClearContext:
    def test_clear_context_resets_all_state(self, monkeypatch, sample_order):
        monkeypatch.setattr(chat_handler, "find_orders", lambda criteria, require_order_id=True: [sample_order])
        handler = OrderChatHandler()
        handler.process_user_message("order 42, email jane@example.com")

        handler.clear_context()

        assert handler.conversation_history == []
        assert handler.last_db_results is None
        assert handler.pending_identity == {}
        assert handler.has_order_context() is False


class TestPolicyQuestions:
    def test_policy_question_routes_to_search_policy(self, monkeypatch):
        # Hermetic: stub the policy tool itself so no embedding API is touched;
        # only its wiring into the tool loop is under test here.
        policy_tool = MagicMock()
        policy_tool.invoke.return_value = json.dumps(
            {"status": "found", "context": "[source: Returns › Return window]\n30-day window."}
        )
        monkeypatch.setattr(chat_handler, "search_policy", policy_tool)

        scripted = _scripted_tool_llm(
            monkeypatch,
            first_calls=[{"name": "search_policy", "args": {"question": "return window"}, "id": "t1"}],
            final_text="Per our Returns policy, items can be returned within 30 days.",
        )
        handler = OrderChatHandler()

        response, db_results = handler.process_user_message("what is your return policy?")

        assert "30 days" in response
        assert db_results == {}
        # The model's chosen question reached the real tool boundary unchanged.
        policy_tool.invoke.assert_called_once_with({"question": "return window"})
        # Two model calls: tool selection + grounded final answer.
        assert scripted.invoke.call_count == 2
        # The second call must contain the search_policy ToolMessage payload.
        second_messages = scripted.invoke.call_args_list[1].args[0]
        tool_messages = [m for m in second_messages if isinstance(m, ToolMessage)]
        assert any(json.loads(m.content).get("status") == "found" for m in tool_messages)
        # Plain LLM (non-tool) path never used.
        handler.llm.invoke.assert_not_called()

    def test_cancel_request_mid_verification_answers_policy_and_keeps_pending(self, monkeypatch):
        _scripted_tool_llm(
            monkeypatch,
            first_calls=[{"name": "search_policy", "args": {"question": "cancel order"}, "id": "t2"}],
            final_text="Orders can be cancelled free of charge while still Processing.",
        )
        handler = OrderChatHandler()
        handler.process_user_message("order 42")

        response, db_results = handler.process_user_message("cancel my order")

        assert "cancelled free of charge" in response
        assert db_results == {}
        # Identity collection resumes afterwards — state was preserved.
        assert handler.pending_identity == {"order_id": "42"}

    def test_bare_cancel_still_resets_pending(self):
        handler = OrderChatHandler()
        handler.process_user_message("order 42")
        assert handler.pending_identity == {"order_id": "42"}

        response, db_results = handler.process_user_message("cancel")

        assert handler.pending_identity == {}
        assert db_results == {}
        assert "start over" in response.lower()

    def test_multi_tool_turn_lookup_plus_policy(self, monkeypatch):
        # Out-of-range order number cannot be verified deterministically, so
        # the message reaches the tool loop where the model may call both
        # tools together.
        monkeypatch.setattr(tools_namespace, "find_orders", lambda criteria, require_order_id=True: [])
        monkeypatch.setattr(
            chat_handler, "search_policy",
            type("S", (), {"invoke": staticmethod(lambda args: json.dumps({"status": "found", "context": "[source: Returns]\n30-day window."}))})(),
        )
        scripted = _scripted_tool_llm(
            monkeypatch,
            first_calls=[
                {"name": "search_policy", "args": {"question": "return window"}, "id": "t3"},
                {"name": "lookup_order", "args": {"order_id": 5000, "email": "jane@example.com"}, "id": "t4"},
            ],
            final_text="Returns are accepted within 30 days of delivery.",
        )
        handler = OrderChatHandler()

        response, db_results = handler.process_user_message("how do I return order 5000?")

        assert "30 days" in response
        assert db_results == {}
        # Both tool payloads were fed back to the model.
        second_messages = scripted.invoke.call_args_list[1].args[0]
        tool_contents = [json.loads(m.content) for m in second_messages if isinstance(m, ToolMessage)]
        statuses = {payload.get("status") for payload in tool_contents}
        assert statuses == {"found", "not_found"}
