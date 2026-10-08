import json
import logging
import re

from langchain_core.messages import ToolMessage
from langchain_core.prompts import ChatPromptTemplate

from .config import MAX_ORDER_ID, MIN_ORDER_ID, SYSTEM_PROMPT, chatmodel
from .database import (
    append_message,
    build_criteria,
    create_conversation,
    end_conversation,
    find_orders,
    format_order_number,
    get_messages,
)
from .query import count_lookup_fields, extract_info_from_query, format_database_context, has_lookup_identifier
from .responses import format_grounded_lookup, format_order_follow_up, typewriter
from .routing import (
    BARE_ORDER_NUMBER_RE,
    FALLBACK_MESSAGE,
    FOLLOW_UP_WORDS,
    is_order_specific_question,
    is_policy_question,
    looks_like_bare_cancel,
    looks_like_bare_name,
)
from .state import VerificationAttemptGuard, VerificationState
from .tools import build_llm_with_tools, lookup_order, search_policy

logger = logging.getLogger(__name__)

# An explicit bail-out mid-verification ("never mind", "skip", "forget it").
#
# Matched with fullmatch(), not search(): a message that merely *contains* an
# abort word ("there's nothing wrong with my order", "I want to skip ahead") is
# still a real turn and must not discard the identity collected so far. Only a
# message that is nothing but a bail-out abandons verification. Mirrors the
# anchored BARE_CANCEL_RE in routing.py.
_ABORT_RE = re.compile(
    r"(?:ok,?\s+|please\s+)?"
    r"(?:never\s*mind|forget\s+(?:it|that)|skip(?:\s+(?:it|that|this))?|nothing(?:\s+else)?)"
    r"[.!]?",
    re.IGNORECASE,
)

# The only order fields written to the conversation log. Everything identifying
# — customers (name, email, phone), shipments (tracking number, driver name),
# order_items and order_events — is deliberately excluded. The assistant's
# rendered reply already carries the customer-facing facts, so nothing is lost
# from the transcript itself.
_REDACTED_ORDER_FIELDS = (
    "public_order_id",
    "status",
    "payment_status",
    "currency",
    "total_amount",
    "ordered_at",
)


def redact_db_results(db_results: dict | None) -> dict:
    """Strip personal data from a lookup result before it is stored.

    Returns an empty dict when there is nothing worth keeping, so the column
    stays null rather than holding an empty object.
    """
    if not db_results or not isinstance(db_results, dict):
        return {}
    matched = db_results.get("matched") or []
    if not matched:
        return {}
    return {
        "matched": [
            {key: order.get(key) for key in _REDACTED_ORDER_FIELDS} for order in matched if isinstance(order, dict)
        ]
    }


class OrderChatHandler:
    def __init__(self, conversation_id: str | None = None, session_id: str | None = None):
        # Identifies the browser session, not a person. Passed by main.py so the
        # conversation it creates can be listed and reloaded by that same
        # session only. See migration 006.
        self.session_id = session_id
        self.llm = chatmodel()
        self.llm_with_tools = build_llm_with_tools()
        self.conversation_history = []
        self.last_db_results = None
        self.verification = VerificationState()
        self.verification_guard = VerificationAttemptGuard()
        # The last successful lookup used a customer name as its only secondary
        # detail, which restricts what may be disclosed. Scoping the flag to the
        # single stored result set is equivalent to tracking individual order
        # ids, because the flag derives from the lookup criteria, not the rows.
        self.name_only_verified = False
        self.conversation_id = conversation_id

        if conversation_id:
            try:
                stored = get_messages(conversation_id, limit=50)
                self.conversation_history = [
                    ("human" if message.get("role") == "user" else "assistant", message.get("content", ""))
                    for message in stored
                ]
            except Exception:
                logger.exception("Failed to load conversation history")

    @property
    def pending_identity(self) -> dict[str, str]:
        """Compatibility view for UI/tests; state mutations stay explicit."""
        return self.verification.as_criteria()

    def stream_response(self, user_input, on_step=None):
        """Yield response deltas for the UI, persisting both turns at the end.

        Deterministic branches yield their complete text instantly (short ones
        through the fast typewriter), while model-backed leaves stream real
        tokens. Never raises — failures degrade to the standard apology so the
        chat never stalls.
        """
        if not user_input or not user_input.strip():
            greeting = (
                "Hello! I'm here to help you check your order status. To look up an order "
                "I need two details: your order number plus your email, phone number, or name."
            )
            self.conversation_history.append(("assistant", greeting))
            self._persist_message("user", user_input or "")
            self._persist_message("assistant", greeting)
            yield greeting
            return

        if on_step:
            on_step("Checking your request...")

        response = None
        db_results = {}
        try:
            self.conversation_history.append(("human", user_input))
            result, db_results = self._route_message(user_input, on_step=on_step)

            if isinstance(result, str):
                response = result
                yield from typewriter(result)
            else:
                pieces = []
                for piece in result:
                    if piece:
                        pieces.append(piece)
                        yield piece
                response = "".join(pieces)
        except Exception:
            logger.exception("Error streaming response")
            response = FALLBACK_MESSAGE
            # Record the apology too, so the next turn's history does not open
            # with two consecutive human messages.
            self.conversation_history.append(("assistant", response))
            yield response

        self._persist_message("user", user_input)
        self._persist_message("assistant", response, db_results if isinstance(db_results, dict) else {})

    def _route_message(self, user_input, on_step=None):
        """Classify a turn and produce its reply.

        Assistant-turn bookkeeping belongs to the leaves: streaming generators
        append their assembled text to conversation_history at exhaustion, and
        deterministic branches append before returning.
        """
        extracted = extract_info_from_query(user_input)

        # While awaiting an order number, a message that is nothing but the
        # number is the order. Anchored on purpose: a loose search would let
        # "I ordered 3 items" register as order 3.
        if not extracted["order_ids"] and self.verification.has_any and not self.verification.order_id:
            bare_match = BARE_ORDER_NUMBER_RE.fullmatch(user_input.strip())
            if bare_match:
                candidate = int(bare_match.group(1))
                if MIN_ORDER_ID <= candidate <= MAX_ORDER_ID:
                    extracted["order_ids"] = [str(candidate)]

        # While awaiting the second identity detail, a bare alphabetic message
        # is the customer's name.
        if (
            not any(extracted.values())
            and self.verification.order_id
            and not self.verification.email
            and not self.verification.phone
            and not self.verification.name
            and looks_like_bare_name(user_input)
        ):
            extracted["names"] = [user_input.strip().strip(".,;:!?")]

        # Public-policy questions take precedence over stored order context.
        # Without this branch, a previous order lookup makes a broad request
        # such as "how can I get a refund?" look like a payment-status follow-up.
        if is_policy_question(user_input) and not is_order_specific_question(user_input, bool(self.last_db_results)):
            return self._run_tool_lookup(user_input, on_step=on_step)

        # A message that only re-references the already-verified order is a
        # follow-up, not a new lookup, so it can keep using stored context.
        verified_ids = self._verified_order_ids()
        message_order_ids = {int(order_id) for order_id in extracted["order_ids"]}
        if (
            verified_ids
            and message_order_ids
            and message_order_ids <= verified_ids
            and count_lookup_fields(user_input, extracted) < 2
        ):
            return self._continue_with_context(user_input), {}

        # Let the user bail out of a half-finished verification. An explicit
        # policy question instead interrupts collection to answer, keeping the
        # collected identity so verification can resume afterwards.
        if self.verification.has_any:
            lowered = user_input.lower()
            if looks_like_bare_cancel(lowered) or _ABORT_RE.fullmatch(lowered.strip()):
                self.verification.reset()
                response = "No problem - we can start over whenever you're ready. To check an order I'll need your order number plus your email, phone number, or name."
                self.conversation_history.append(("assistant", response))
                return response, {}

        self.verification.merge(extracted)
        has_order = self.verification.order_id is not None
        has_secondary = self.verification.has_secondary

        # Order number plus a second identity field: run the verified lookup now.
        if has_order and has_secondary:
            return self._run_verified_lookup()

        # Verification in progress (or identity info just supplied): stay
        # deterministic and collect the missing detail. The free-form model is
        # never used mid-verification, so it cannot invent order data.
        if self.verification.has_any:
            response = self._ask_for_missing_field()
            self.conversation_history.append(("assistant", response))
            return response, {}

        # An order inquiry or policy question the rules could not classify:
        # let the tool loop resolve it (lookup_order enforces verification
        # server-side; search_policy retrieves public policy documents).
        if not self.last_db_results and (
            self._is_order_inquiry(user_input, extracted) or is_policy_question(user_input)
        ):
            return self._run_tool_lookup(user_input, on_step=on_step)

        return self._continue_with_context(user_input), {}

    def _run_verified_lookup(self):
        """Look up orders only when every collected field matches the same order."""
        try:
            if self.verification_guard.locked:
                self.verification.reset()
                response = "Too many unsuccessful verification attempts. Please wait a few minutes before trying again."
                self.conversation_history.append(("assistant", response))
                return response, {}
            orders = find_orders(self.verification.as_criteria(), require_order_id=True)
        except ValueError:
            orders = []

        if not orders:
            locked = self.verification_guard.record_failure()
            self.verification.reset()
            if locked:
                response = "Too many unsuccessful verification attempts. Please wait a few minutes before trying again."
                self.conversation_history.append(("assistant", response))
                return response, {}
            response = "I couldn't find an order matching that information. Please check the order number or customer details and try again."
            self.conversation_history.append(("assistant", response))
            return response, {}

        db_results = {"matched": orders}
        self.last_db_results = db_results
        name_only = self.verification.name_only
        self.verification_guard.record_success()
        self.verification.reset()
        self.name_only_verified = name_only
        response = format_grounded_lookup(db_results, name_only=name_only)
        self.conversation_history.append(("assistant", response))
        return response, db_results

    def _run_tool_lookup(self, user_input, on_step=None):
        """Resolve an ambiguous inquiry through the tool-calling model.

        The tools enforce verification server-side. Returns (iterator of deltas,
        db_results); the grounded or two-field fallback is produced in the
        generator tail if the streamed output comes back empty.
        """
        try:
            if on_step:
                step = "Searching policies..." if is_policy_question(user_input) else "Verifying order details..."
                on_step(step)
            messages = ChatPromptTemplate.from_messages(self._build_message_list(user_input)).format_messages()
            first = self.llm_with_tools.invoke(messages)
            tool_calls = getattr(first, "tool_calls", None) or []

            if not tool_calls:
                response = getattr(first, "content", "") or self._two_field_prompt()
                self.conversation_history.append(("assistant", response))
                return response, {}

            messages.append(first)
            db_results = {}
            for call in tool_calls:
                tool_call_id = call.get("id")
                name = call.get("name")
                args = call.get("args") or {}

                if name == "lookup_order":
                    payload = self._execute_order_lookup(args)
                elif name == "search_policy":
                    payload = self._execute_policy_search(args)
                else:
                    payload = None
                    messages.append(ToolMessage(content="Unknown tool.", tool_call_id=tool_call_id))
                    continue

                messages.append(ToolMessage(content=json.dumps(payload), tool_call_id=tool_call_id))

            if on_step:
                on_step("Composing answer...")

            def _gen():
                pieces = []
                for chunk in self.llm_with_tools.stream(messages):
                    piece = getattr(chunk, "content", "") or ""
                    if piece:
                        pieces.append(piece)
                        yield piece
                full = "".join(pieces)
                if not full.strip():
                    full = format_grounded_lookup(db_results) if db_results else self._two_field_prompt()
                    yield full
                self.conversation_history.append(("assistant", full))

            return _gen(), db_results
        except Exception:
            logger.exception("Tool lookup failed")
            response = self._two_field_prompt()
            self.conversation_history.append(("assistant", response))
            return iter([response]), {}

    def _execute_order_lookup(self, args: dict) -> dict:
        """Run lookup_order and mirror its result into handler state."""
        try:
            payload = json.loads(lookup_order.invoke(args))
        except Exception:
            logger.exception("Order tool execution failed")
            payload = {"status": "error"}

        if payload.get("status") == "found":
            criteria = self._criteria_from_args(args)
            orders = find_orders(criteria, require_order_id=True) if len(criteria) >= 2 else []
            db_results = {"matched": orders} if orders else {}
            if db_results:
                self.last_db_results = db_results
                self.name_only_verified = VerificationState.from_criteria(criteria).name_only
                self.verification_guard.record_success()
                self.verification.reset()
        elif payload.get("status") == "need_more_info":
            self.verification = VerificationState.from_criteria(self._criteria_from_args(args))
        else:  # not_found / error
            self.verification.reset()
        return payload

    @staticmethod
    def _execute_policy_search(args: dict) -> dict:
        """Run search_policy. Touches no order state."""
        try:
            payload = json.loads(search_policy.invoke(args))
        except Exception:
            logger.exception("Policy tool execution failed")
            payload = {"status": "error"}
        return payload

    def _ask_for_missing_field(self) -> str:
        """Explain which identity field is still needed to complete a lookup."""
        if self.verification.order_id:
            return (
                f"I have your order number (#{format_order_number(self.verification.order_id)}). "
                "To verify it, please also provide the email, phone number, or name on the order."
            )
        have = [
            label
            for label, key in (("email", "email"), ("phone number", "phone"), ("name", "name"))
            if getattr(self.verification, key)
        ]
        return (
            f"I have your {', '.join(have)}. To verify your order, I also need your order number - "
            "for example `order 42`."
        )

    def _is_order_inquiry(self, user_input: str, extracted: dict | None = None) -> bool:
        message = user_input.lower()
        return bool(has_lookup_identifier(user_input, extracted) or any(word in message for word in FOLLOW_UP_WORDS))

    def _two_field_prompt(self) -> str:
        return (
            "I need two details to verify your order before I can share anything: "
            "your order number plus your email, phone number, or name. "
            "For example: `order 42, email you@example.com`."
        )

    def _build_message_list(self, user_input):
        """System prompt (with query-relevant order data) + history + current message."""
        if self.last_db_results:
            name_only = self.name_only_verified
            context = format_database_context(
                self.last_db_results,
                fields={"order", "status", "delivery"} if name_only else None,
                query=None if name_only else user_input,
            )
            system_prompt = (
                f"{SYSTEM_PROMPT}\n\n{context}\n\n"
                "Please use the above order information to provide accurate and helpful responses. "
                "Maintain context from previous messages in this conversation."
            )
        else:
            system_prompt = SYSTEM_PROMPT

        messages = [("system", system_prompt)]
        recent_history = self.conversation_history[-6:]
        messages.extend(recent_history)
        if not recent_history or recent_history[-1][1] != user_input:
            messages.append(("human", user_input))
        return messages

    def _continue_with_context(self, user_input):
        """Answer a follow-up from stored context, or conversationally without data.

        Returns an iterator of deltas. Conversation history is appended before
        any text is produced.
        """
        deterministic_response = format_order_follow_up(self._orders(), self.name_only_verified, user_input)
        if deterministic_response:
            self.conversation_history.append(("assistant", deterministic_response))
            return iter([deterministic_response])

        prompt_template = ChatPromptTemplate.from_messages(self._build_message_list(user_input))
        chain = prompt_template | self.llm

        def _gen():
            pieces = []
            for chunk in chain.stream({}):
                piece = getattr(chunk, "content", "") or ""
                if piece:
                    pieces.append(piece)
                    yield piece
            self.conversation_history.append(("assistant", "".join(pieces)))

        return _gen()

    @staticmethod
    def _criteria_from_args(args: dict) -> dict:
        return build_criteria(
            args.get("order_id"),
            args.get("email"),
            args.get("phone"),
            args.get("customer_name"),
        )

    def _persist_message(self, role: str, content: str, db_results: dict | None = None) -> None:
        """Persist one turn to the conversation log. Never raises.

        db_results is redacted before storage: only the order's own
        non-identifying fields are kept. The full row carries the customer's
        name, email, phone, tracking number and delivery driver, and nothing
        reads this column back, so storing it would be exposure with no benefit.
        """
        self._ensure_conversation()
        if not self.conversation_id:
            return
        append_message(
            self.conversation_id,
            "user" if role == "human" else role,
            content,
            redact_db_results(db_results),
        )

    def _ensure_conversation(self) -> None:
        if not self.conversation_id:
            self.conversation_id = create_conversation(session_id=self.session_id)

    def end_session(self) -> None:
        """Mark the current conversation as ended (best-effort)."""
        if self.conversation_id:
            end_conversation(self.conversation_id)
            self.conversation_id = None

    def clear_context(self):
        self.conversation_history = []
        self.last_db_results = None
        self.verification.reset()
        self.name_only_verified = False

    def _orders(self) -> list[dict]:
        """Orders from the last successful lookup, or none."""
        return (self.last_db_results or {}).get("matched") or []

    def _verified_order_ids(self) -> set[int]:
        """Order IDs established by the last successful two-field lookup."""
        verified = set()
        for order in self._orders():
            order_id = order.get("public_order_id")
            if order_id is not None:
                verified.add(int(order_id))
        return verified
