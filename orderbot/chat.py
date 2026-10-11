import json
import logging
import re
from datetime import datetime, timezone

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool

from .agent import build_agent
from .actions import stage_action, submit_confirmed_action
from .config import MAX_ORDER_ID, MIN_ORDER_ID, SYSTEM_PROMPT, chatmodel
from .conversations import ConversationStore
from .database import format_order_number
from .identity import count_lookup_fields, extract_info_from_query, has_lookup_identifier
from .order_context import format_database_context
from .orders import OrderCriteria, OrderLookup
from .orders import lookup_order as perform_order_lookup
from .privacy import persisted_message
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
from .tools import format_order_lookup_payload

logger = logging.getLogger(__name__)


def _message_text(content) -> str:
    """Extract displayable text from LangChain's string or block-list content."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    text_parts = []
    for block in content:
        if isinstance(block, str):
            text_parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            text_parts.append(block["text"])
    return "".join(text_parts)


def _format_policy_excerpts(chunks: list[dict]) -> str:
    """Render public policy answers from retrieved text, not model prose."""
    sections = []
    for chunk in chunks[:4]:
        content = chunk.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        excerpt = content.strip()
        if len(excerpt) > 1200:
            excerpt = excerpt[:1199].rsplit(" ", 1)[0].rstrip() + "…"
        heading = chunk.get("heading")
        if heading:
            sections.append(f"**{heading}**\n\n{excerpt}")
        else:
            sections.append(excerpt)
    if not sections:
        return "I found policy references but couldn't read their text. Please contact support to confirm the current policy."
    return "Here are the relevant excerpts from our help documents:\n\n" + "\n\n---\n\n".join(sections)


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
_ACTION_REQUEST_RE = re.compile(
    r"\b(?:cancel|return)\s+(?:my|the|this|that|order|item)\b"
    r"|\b(?:i\s+want\s+to|i\s+need\s+to|i(?:'d|\s+would)\s+like\s+to|please)\s+(?:cancel|return)\b",
    re.IGNORECASE,
)


class OrderChatHandler:
    def __init__(
        self,
        conversation_id: str | None = None,
        session_id: str | None = None,
        stored_messages: list[dict] | None = None,
    ):
        # Identifies the browser session, not a person. Passed by main.py so the
        # conversation it creates can be listed and reloaded by that same
        # session only. See migration 006.
        self.session_id = session_id
        self.llm = chatmodel()
        self.conversations = ConversationStore(conversation_id, session_id)
        self.conversation_history = []
        self.last_db_results = None
        self.verification = VerificationState()
        self.verification_guard = VerificationAttemptGuard()
        self._count_agent_failure = True
        self.strong_verified_order_id = None
        self.strong_verified_at = None
        self.pending_action = None
        # The last successful lookup used a customer name as its only secondary
        # detail, which restricts what may be disclosed. Scoping the flag to the
        # single stored result set is equivalent to tracking individual order
        # ids, because the flag derives from the lookup criteria, not the rows.
        self.name_only_verified = False
        self.conversation_history = self.conversations.load(stored_messages)
        self.agent = build_agent(self._make_order_tool(), self._make_action_tools())

    @property
    def conversation_id(self) -> str | None:
        return self.conversations.conversation_id

    def _make_order_tool(self):
        """Bind the verified lookup tool to this handler's attempt guard/state."""

        @tool("get_order_status")
        def get_order_status(
            order_id: int,
            email: str = "",
            phone: str = "",
            name: str = "",
            fields: list[str] | None = None,
        ) -> str:
            """Look up an order after verifying the order number and a matching email, phone, or name."""
            if self.verification_guard.locked:
                return json.dumps({"status": "too_many_attempts"})
            if self.strong_verified_order_id and self.strong_verified_order_id != int(order_id):
                self.last_db_results = None
                self.name_only_verified = False
                self._clear_strong_verification()
                self.pending_action = None

            lookup = perform_order_lookup(OrderCriteria(order_id, email, phone, name))
            payload = format_order_lookup_payload(lookup, fields)
            if lookup.status == "found":
                self.last_db_results = {"matched": lookup.orders}
                self.name_only_verified = lookup.name_only
                self.verification_guard.record_success()
                self.verification.reset()
                self._remember_strong_verification(lookup)
            elif lookup.status == "not_found":
                if self._count_agent_failure and self.verification_guard.record_failure():
                    payload = format_order_lookup_payload(OrderLookup("too_many_attempts"))
                self.last_db_results = None
                self.name_only_verified = False
                self._clear_strong_verification()
                self.verification.reset()
            elif lookup.status == "need_more_info":
                self.verification = VerificationState.from_criteria(lookup.criteria)
            elif lookup.status == "unavailable":
                self.last_db_results = None
                self.name_only_verified = False
            return json.dumps(payload)

        return get_order_status

    def _make_action_tools(self):
        """Create request tools that only stage an in-memory confirmation."""

        def stage(action: str, order_id: int, reason: str) -> str:
            if not self._has_recent_strong_verification(order_id):
                return json.dumps({"status": "verification_required"})
            self.pending_action = stage_action(action, order_id, reason, self.conversation_id)
            return json.dumps(
                {
                    "status": "pending_confirmation",
                    "action": action,
                    "order_id": order_id,
                }
            )

        @tool("request_return")
        def request_return(order_id: int, reason: str) -> str:
            """Stage a customer return request for human review; does not return or refund the order."""
            if not reason or not reason.strip() or len(reason.strip()) > 300:
                return json.dumps({"status": "invalid_reason"})
            return stage("return_request", order_id, reason)

        @tool("cancel_order")
        def cancel_order(order_id: int, reason: str = "Customer request") -> str:
            """Stage an order cancellation request for human review; does not cancel the order."""
            if len(reason.strip()) > 300:
                return json.dumps({"status": "invalid_reason"})
            return stage("cancel_request", order_id, reason)

        return [request_return, cancel_order]

    def _remember_strong_verification(self, lookup) -> None:
        if lookup.criteria.get("email") or lookup.criteria.get("phone"):
            self.strong_verified_order_id = int(lookup.criteria["order_id"])
            self.strong_verified_at = datetime.now(timezone.utc)
        else:
            self._clear_strong_verification()

    def _clear_strong_verification(self) -> None:
        self.strong_verified_order_id = None
        self.strong_verified_at = None

    def _has_recent_strong_verification(self, order_id: int) -> bool:
        if not self.strong_verified_at or self.name_only_verified:
            return False
        age = datetime.now(timezone.utc) - self.strong_verified_at
        orders = self._orders()
        return (
            self.strong_verified_order_id == int(order_id)
            and age.total_seconds() <= 600
            and any(int(order.get("public_order_id", -1)) == int(order_id) for order in orders)
        )

    def confirm_pending_action(self, request_key: str) -> tuple[bool, str]:
        """Revalidate and persist the exact pending request after UI confirmation."""
        pending = self.pending_action
        if not pending or pending["request_key"] != request_key:
            return False, "This request has expired. Please submit it again."
        if pending["expires_at"] <= datetime.now(timezone.utc):
            self.pending_action = None
            return False, "This request has expired. Please submit it again."
        if not self._has_recent_strong_verification(pending["order_id"]):
            self.pending_action = None
            return False, "Please verify this order again with its email or phone before submitting the request."

        result = submit_confirmed_action(pending)
        if not result:
            return False, "I couldn't submit the request. Please try again shortly."
        self.pending_action = None
        message = (
            f"Your {pending['action'].replace('_request', '')} request was recorded for human review. "
            "The order has not been changed."
        )
        self.conversation_history.append(("assistant", message))
        self._persist_message("assistant", message, order_context=True)
        return True, message

    def discard_pending_action(self, request_key: str) -> None:
        if self.pending_action and self.pending_action["request_key"] == request_key:
            self.pending_action = None

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

        has_order_context = bool(self.last_db_results or (db_results or {}).get("matched"))
        verification_turn = bool(self.verification.has_any)
        self._persist_message("user", user_input, order_context=has_order_context or verification_turn)
        self._persist_message("assistant", response, order_context=has_order_context)

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

        if extracted["order_ids"] and self.strong_verified_order_id:
            if int(extracted["order_ids"][0]) != self.strong_verified_order_id:
                self._clear_strong_verification()
                self.last_db_results = None
                self.name_only_verified = False
                self.pending_action = None

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
        if self._is_action_request(user_input):
            supplied_order_id = int(extracted["order_ids"][0]) if extracted["order_ids"] else None
            verified_order_id = self.strong_verified_order_id
            if (
                verified_order_id
                and (supplied_order_id is None or supplied_order_id == verified_order_id)
                and self._has_recent_strong_verification(verified_order_id)
            ):
                return self._run_tool_lookup(user_input, on_step=on_step)

            self.verification.merge(extracted)
            if self.verification.order_id and self.verification.has_secondary:
                lookup_response, lookup_results = self._run_verified_lookup(
                    user_input, record_response=False
                )
                if (
                    lookup_results
                    and self.strong_verified_order_id
                    and self._has_recent_strong_verification(self.strong_verified_order_id)
                ):
                    return self._run_tool_lookup(user_input, on_step=on_step)
                return lookup_response, lookup_results
            response = self._ask_for_missing_field() if self.verification.has_any else self._two_field_prompt()
            self.conversation_history.append(("assistant", response))
            return response, {}

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
            return self._run_verified_lookup(user_input)

        # Verification in progress (or identity info just supplied): stay
        # deterministic and collect the missing detail. The free-form model is
        # never used mid-verification, so it cannot invent order data.
        if self.verification.has_any:
            response = self._ask_for_missing_field()
            self.conversation_history.append(("assistant", response))
            return response, {}

        # An order inquiry or policy question the rules could not classify:
        # let the tool loop resolve it (get_order_status enforces verification
        # server-side; search_policies retrieves public policy documents).
        if not self.last_db_results and (
            self._is_order_inquiry(user_input, extracted) or is_policy_question(user_input)
        ):
            return self._run_tool_lookup(user_input, on_step=on_step)

        return self._continue_with_context(user_input), {}

    def _run_verified_lookup(self, user_input: str | None = None, record_response: bool = True):
        """Look up orders only when every collected field matches the same order."""
        try:
            if self.verification_guard.locked:
                self.verification.reset()
                response = "Too many unsuccessful verification attempts. Please wait a few minutes before trying again."
                self.conversation_history.append(("assistant", response))
                return response, {}
            self.last_db_results = None
            self.name_only_verified = False
            self._clear_strong_verification()
            lookup = perform_order_lookup(OrderCriteria.from_mapping(self.verification.as_criteria()))
            if lookup.status == "unavailable":
                response = "Order lookup is temporarily unavailable. Please try again shortly."
                self.conversation_history.append(("assistant", response))
                return response, {}
            orders = lookup.orders if lookup.status == "found" else []
        except Exception:
            logger.exception("Order lookup service failed")
            self.last_db_results = None
            self.name_only_verified = False
            response = FALLBACK_MESSAGE
            self.conversation_history.append(("assistant", response))
            return response, {}

        if not orders:
            # The regex extractor can build a plausible-looking but wrong value
            # ("im gaurang and my order number is 13" captured the name as
            # "gaurang and my"). Rather than tell the customer their own name is
            # wrong, let the model re-read the message once. It only proposes
            # fields: find_orders still enforces the two-field rule, so a
            # hallucinated name returns nothing just as a wrong one would.
            if user_input:
                retry = self._retry_lookup_via_model(user_input)
                if retry is not None:
                    return retry

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
        self._remember_strong_verification(lookup)
        response = format_grounded_lookup(db_results, name_only=name_only)
        if record_response:
            self.conversation_history.append(("assistant", response))
        return response, db_results

    def _retry_lookup_via_model(self, user_input):
        """Re-read a message through the tool loop after a deterministic miss.

        Returns (response, db_results) when the model recovers the order, or
        None when it does not, in which case the caller records the failure
        normally. A recovery does not touch the attempt guard: the customer
        supplied correct details and only our extractor misread them, so
        counting it would penalise them for our bug.
        """
        try:
            # A retry is safe only through the shared verified lookup service.
            # The agent may extract fields, but it cannot grant authorization.
            messages = ChatPromptTemplate.from_messages(self._build_message_list(user_input)).format_messages()
            self._count_agent_failure = False
            try:
                state = self.agent.invoke({"messages": messages})
            finally:
                self._count_agent_failure = True
            for message in state.get("messages", []):
                if getattr(message, "type", "") != "tool" or getattr(message, "name", "") != "get_order_status":
                    continue
                payload = json.loads(message.content)
                if payload.get("status") == "found":
                    if self.last_db_results:
                        response = format_grounded_lookup(self.last_db_results, name_only=self.name_only_verified)
                        self.conversation_history.append(("assistant", response))
                        return response, self.last_db_results
        except Exception:
            logger.exception("Model retry after failed lookup failed")
        return None

    def _run_tool_lookup(self, user_input, on_step=None):
        """Resolve an ambiguous inquiry through the LangChain agent."""
        try:
            if on_step:
                step = "Searching policies..." if is_policy_question(user_input) else "Verifying order details..."
                on_step(step)
            if on_step:
                on_step("Composing answer...")

            def _gen():
                pieces = []
                tool_results = []
                for chunk, _metadata in self.agent.stream(
                    {"messages": ChatPromptTemplate.from_messages(self._build_message_list(user_input)).format_messages()},
                    stream_mode="messages",
                ):
                    if getattr(chunk, "type", "") == "tool":
                        try:
                            tool_results.append((getattr(chunk, "name", ""), json.loads(chunk.content)))
                        except (TypeError, ValueError):
                            continue
                        continue
                    piece = _message_text(getattr(chunk, "content", ""))
                    if piece:
                        pieces.append(piece)
                full = "".join(pieces)
                order_result = next((result for name, result in tool_results if name == "get_order_status"), None)
                policy_result = next((result for name, result in tool_results if name == "search_policies"), None)
                action_result = next(
                    (result for name, result in tool_results if name in ("request_return", "cancel_order")), None
                )
                policy_only = is_policy_question(user_input) and not is_order_specific_question(
                    user_input, bool(self.last_db_results)
                )
                if action_result:
                    full = {
                        "pending_confirmation": "Please review the support request below and confirm it to submit. This sends a request for human review; it does not change the order.",
                        "verification_required": "Please verify this order with its email address or phone number before requesting an order change.",
                        "invalid_reason": "Please provide a short reason (300 characters or fewer) for the request.",
                    }.get(action_result.get("status"), "I couldn't prepare that support request. Please contact support for help.")
                elif policy_only and (not policy_result or policy_result.get("status") != "found"):
                    status = (policy_result or {}).get("status")
                    full = (
                        "Policy search is temporarily unavailable. Please contact support to confirm the current policy."
                        if status == "unavailable"
                        else "I couldn't find a relevant policy in the available help documents. Please contact support to confirm the current policy."
                    )
                elif policy_only and policy_result and policy_result.get("status") == "found":
                    full = _format_policy_excerpts(policy_result.get("chunks") or [])
                elif self._is_order_inquiry(user_input) and not order_result and not policy_only:
                    full = self._two_field_prompt()
                elif order_result and order_result.get("status") != "found":
                    status = order_result.get("status")
                    full = {
                        "need_more_info": self._two_field_prompt(),
                        "invalid_order_id": "Order number must be between 0001 and 1000. Please check it and try again.",
                        "verification_failed": "I couldn't verify those details. Please check them and try again.",
                        "too_many_attempts": "Too many unsuccessful verification attempts. Please wait before trying again.",
                        "unavailable": "Order lookup is temporarily unavailable. Please try again shortly.",
                    }.get(status, self._two_field_prompt())
                elif policy_result and policy_result.get("status") != "found":
                    full = (
                        "Policy search is temporarily unavailable. Please contact support to confirm the current policy."
                        if policy_result.get("status") == "unavailable"
                        else "I couldn't find a relevant policy in the available help documents. Please contact support to confirm the current policy."
                    )
                if not full.strip():
                    if order_result and order_result.get("status") == "found" and self.last_db_results:
                        full = format_grounded_lookup(self.last_db_results, name_only=self.name_only_verified)
                    elif self._is_order_inquiry(user_input):
                        full = self._two_field_prompt()
                    else:
                        full = FALLBACK_MESSAGE
                if policy_result and policy_result.get("status") == "found":
                    sources = list(
                        dict.fromkeys(
                            f"{chunk['source_title']} > {chunk['heading']}" if chunk.get("heading") else chunk["source_title"]
                            for chunk in policy_result.get("chunks", [])
                        )
                    )
                    if sources:
                        source_note = "\n\n**Sources:** " + "; ".join(sources)
                        full += source_note
                # Buffer until every tool result is known. This prevents any
                # premature model text from leaking an order claim before the
                # server-side verification result can replace it deterministically.
                yield full
                self.conversation_history.append(("assistant", full))

            return _gen(), {}
        except Exception as exc:
            logger.error("Tool lookup failed (%s)", type(exc).__name__)
            response = (
                "Policy search is temporarily unavailable. Please try again shortly."
                if is_policy_question(user_input)
                else "Order lookup is temporarily unavailable. Please try again shortly."
            )
            self.conversation_history.append(("assistant", response))
            return iter([response]), {}

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

    @staticmethod
    def _is_action_request(user_input: str) -> bool:
        lowered = user_input.casefold().strip()
        if re.match(
            r"^(?:how|what|when|where|can i|am i|should i|do i need to|is it possible|can you tell me|"
            r"don't|do not|never|shouldn't|should not)\b",
            lowered,
        ):
            return False
        return bool(_ACTION_REQUEST_RE.search(lowered))

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
        history = self.conversation_history
        current_already_appended = bool(history and history[-1] == ("human", user_input))
        prior_history = history[:-1] if current_already_appended else history
        # Keep at most five prior turns and redact them again before passing
        # them to the model. Only the current user turn stays unredacted so
        # identity fields supplied in this request can be checked by the tool.
        recent_history = prior_history[-5:]
        messages.extend(
            (
                role,
                persisted_message(
                    "user" if role == "human" else role,
                    content,
                    order_context=bool(self.last_db_results and role == "assistant"),
                ),
            )
            for role, content in recent_history
        )
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
                piece = _message_text(getattr(chunk, "content", ""))
                if piece:
                    pieces.append(piece)
                    yield piece
            self.conversation_history.append(("assistant", "".join(pieces)))

        return _gen()

    def _persist_message(self, role: str, content: str, order_context: bool = False) -> None:
        """Persist one privacy-filtered transcript message. Never raises."""
        self.conversations.append(role, content, order_context=order_context)

    def end_session(self) -> None:
        """Mark the current conversation as ended (best-effort)."""
        self.conversations.end()

    def clear_context(self):
        self.conversation_history = []
        self.last_db_results = None
        self.verification.reset()
        self.name_only_verified = False
        self._clear_strong_verification()
        self.pending_action = None

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
