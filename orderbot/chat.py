import json
import logging
import re
from datetime import datetime, timezone

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool

from .agent import build_agent
from .actions import get_action_request_status, stage_action, submit_confirmed_action
from .config import MAX_ORDER_ID, MIN_ORDER_ID, SYSTEM_PROMPT, chatmodel
from .conversations import ConversationStore
from .database import format_order_number
from .handoff import build_order_case_handoff
from .identity import count_lookup_fields, extract_info_from_query, has_lookup_identifier
from .order_context import format_database_context
from .orders import OrderCriteria, OrderLookup, lookup_order as perform_order_lookup
from .responses import format_grounded_lookup, format_order_follow_up
from .retriever import search_policy_chunks
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


def _policy_source_label(chunk: dict) -> str:
    """Format a compact source path without repeating the document title."""
    title = chunk.get("source_title") or "Unknown"
    heading = chunk.get("heading")
    prefix = f"{title} > "
    if isinstance(heading, str) and heading.casefold().startswith(prefix.casefold()):
        heading = heading[len(prefix) :]
    return f"{title} > {heading}" if heading else title


_ABORT_RE = re.compile(
    r"(?:ok,?\s+|please\s+)?"
    r"(?:never\s*mind|forget\s+(?:it|that)|skip(?:\s+(?:it|that|this))?|nothing(?:\s+else)?)"
    r"[.!]?",
    re.IGNORECASE,
)

_ACTION_REQUEST_RE = re.compile(
    r"\b(?:cancel|return)\s+(?:my|the|this|that|order|item)\b"
    r"|\bi\s+(?:want(?:ed)?|need(?:ed)?|hope(?:d)?|plan(?:ned)?)\s+to\s+(?:cancel|return)\b"
    r"|\b(?:i(?:'d|\s+would)\s+like\s+to|please)\s+(?:cancel|return)\b",
    re.IGNORECASE,
)

_ACTION_STATUS_RE = re.compile(
    r"\b(?:request|cancellation request|return request)\b.{0,50}\b(?:status|approved|approve|accepted|rejected|reviewed|update|progress)\b"
    r"|\b(?:approved|accepted|rejected|reviewed)\b.{0,40}\b(?:request|cancellation|return)\b",
    re.IGNORECASE,
)

_NEW_ORDER_INTENT_RE = re.compile(
    r"\b(?:i(?:'d|\s+would)?\s+like\s+to|i\s+(?:want|need)\s+to|can\s+i|could\s+i|"
    r"can\s+you\s+(?:help\s+me\s+)?|please\s+)\s*(?:buy|purchase|order)\b|"
    r"\b(?:place|make|create)\s+(?:a|an|my|new)?\s*order\b",
    re.IGNORECASE,
)
_GENERIC_SUPPORT_REQUEST_RE = re.compile(
    r"\b(?:create|open|raise|submit|file|make|start|log)\s+(?:a|an|the|this|that)?\s*"
    r"(?:support\s+)?(?:request|case|ticket|investigation)\b|"
    r"\b(?:request|case|ticket|investigation)\b.{0,24}\b(?:same|delivery|missing|package|order)\b",
    re.IGNORECASE,
)
_DELIVERY_PROBLEM_RE = re.compile(
    r"\b(?:didn['’]?t\s+receive|haven['’]?t\s+received|not\s+received|never\s+arrived|"
    r"marked\s+delivered|delivery\s+discrepancy|missing\s+(?:package|parcel|order))\b",
    re.IGNORECASE,
)
_DELIVERY_NOT_RECEIVED_RE = re.compile(
    r"\b(?:didn['’]?t\s+receive|haven['’]?t\s+received|not\s+received|never\s+arrived)\b",
    re.IGNORECASE,
)
_EXPLICIT_DELIVERY_REQUEST_RE = re.compile(
    r"\b(?:delivery|missing\s+(?:package|parcel|order)|undelivered)\b.{0,55}\b"
    r"(?:request|case|ticket|investigation|issue|report)\b|"
    r"\b(?:request|case|ticket|investigation|report)\b.{0,55}\b(?:delivery|missing|undelivered)\b",
    re.IGNORECASE,
)

_STANDARD_RETURN_FOLLOWUP_RE = re.compile(
    r"\b(?:standard|regular|normal)\s+(?:return\s+)?policy\b|"
    r"\bwhat\s+is\s+the\s+standard\b",
    re.IGNORECASE,
)
_STANDARD_RETURN_CONTEXT_RE = re.compile(
    r"\b(?:standard\s+)?30[- ]day\s+return\s+policy\b",
    re.IGNORECASE,
)


class OrderChatHandler:
    def __init__(
        self,
        conversation_id: str | None = None,
        session_id: str | None = None,
        stored_messages: list[dict] | None = None,
        owner_user_id: str | None = None,
        authenticated_email: str | None = None,
    ):
        self.session_id = session_id
        self.owner_user_id = owner_user_id
        self.authenticated_email = (authenticated_email or "").strip().casefold() or None
        self.llm = chatmodel()
        self.conversations = ConversationStore(conversation_id, session_id, owner_user_id)
        self.conversation_history = []
        self.last_db_results = None
        self.verification = VerificationState()
        self.verification_guard = VerificationAttemptGuard()
        self._count_agent_failure = True
        self.strong_verified_order_id = None
        self.strong_verified_at = None
        self.pending_action = None
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
            """Look up an order with its number and a matching identity detail; the server binds the authenticated email when available."""
            if self.verification_guard.locked:
                return json.dumps({"status": "too_many_attempts"})
            if self.strong_verified_order_id and self.strong_verified_order_id != int(order_id):
                self.last_db_results = None
                self.name_only_verified = False
                self._clear_strong_verification()
                self.pending_action = None

            if self.authenticated_email and email and email.strip().casefold() != self.authenticated_email:
                lookup = OrderLookup("not_found")
            else:
                lookup = perform_order_lookup(
                    OrderCriteria(order_id, self.authenticated_email or email, phone, name)
                )
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

        def stage(
            action: str,
            order_id: int | None,
            reason: str,
            request_details: dict | None = None,
        ) -> str:
            if not self.owner_user_id:
                return json.dumps({"status": "verification_required"})
            if order_id is not None and not self._has_recent_strong_verification(order_id):
                return json.dumps({"status": "verification_required"})
            verified_order = next(
                (
                    order
                    for order in self._orders()
                    if order_id is not None and int(order.get("public_order_id", -1)) == int(order_id)
                ),
                {},
            )
            recent_customer_messages = [
                content
                for role, content in self.conversation_history
                if role in {"human", "user"} and content
            ]
            handoff_context = build_order_case_handoff(
                action,
                verified_order,
                reason,
                recent_customer_messages=recent_customer_messages,
                request_details=request_details,
            )
            self.pending_action = stage_action(
                action,
                order_id,
                reason,
                self.conversation_id,
                self.owner_user_id,
                handoff_context,
            )
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

        @tool("request_delivery_issue")
        def request_delivery_issue(order_id: int, reason: str) -> str:
            """Stage a missing or misdelivered order investigation for support review."""
            if not reason or not reason.strip() or len(reason.strip()) > 300:
                return json.dumps({"status": "invalid_reason"})
            return stage("delivery_issue_request", order_id, reason)

        @tool("create_support_request")
        def create_support_request(
            topic: str,
            summary: str,
            details: str = "",
            order_id: int | None = None,
        ) -> str:
            """Prepare any customer support request for explicit confirmation and human review. Use an order ID only for an order-related request; linked orders require recent verification."""
            topic = (topic or "").strip()
            summary = (summary or "").strip()
            details = (details or "").strip()
            if not topic or len(topic) > 100 or not summary or len(summary) > 300 or len(details) > 1000:
                return json.dumps({"status": "invalid_request"})
            return stage(
                "support_request",
                order_id,
                summary,
                {"topic": topic, "summary": summary, "details": details},
            )

        return [request_return, cancel_order, request_delivery_issue, create_support_request]

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
        if pending["order_id"] is not None and not self._has_recent_strong_verification(pending["order_id"]):
            self.pending_action = None
            return False, "Please verify this order again with its email or phone before submitting the request."

        result = submit_confirmed_action(pending)
        if not result:
            return False, "I couldn't submit the request. Please try again shortly."
        self.pending_action = None
        action_label = {
            "cancel_request": "cancellation",
            "return_request": "return",
            "delivery_issue_request": "delivery investigation",
        }.get(pending["action"], "support")
        if pending["order_id"] is None:
            message = f"Your {action_label} request was recorded for human review."
        else:
            message = f"Your {action_label} request was recorded for human review. The order has not been changed."
        self.conversation_history.append(("assistant", message))
        self._persist_message("assistant", message)
        return True, message

    def discard_pending_action(self, request_key: str) -> None:
        if self.pending_action and self.pending_action["request_key"] == request_key:
            self.pending_action = None

    def stream_response(self, user_input, on_step=None):
        """Yield one complete response after work finishes; never expose partial model output."""
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
        try:
            self.conversation_history.append(("human", user_input))
            result, _ = self._route_message(user_input, on_step=on_step)

            if isinstance(result, str):
                response = result
                yield response
            else:
                response = "".join(piece for piece in result if piece)
                yield response
        except Exception:
            logger.exception("Chat response generation failed")
            response = FALLBACK_MESSAGE
            self.conversation_history.append(("assistant", response))
            yield response

        self._persist_message("user", user_input)
        self._persist_message("assistant", response)

    def _route_message(self, user_input, on_step=None):
        """Classify a turn and produce its reply.

        Assistant-turn bookkeeping belongs to the leaves: model-backed paths
        append the completed answer, and deterministic branches append before returning.
        """
        extracted = extract_info_from_query(user_input)

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

        if (
            not any(extracted.values())
            and self.verification.order_id
            and not self.verification.email
            and not self.verification.phone
            and not self.verification.name
            and looks_like_bare_name(user_input)
        ):
            extracted["names"] = [user_input.strip().strip(".,;:!?")]

        if _ACTION_STATUS_RE.search(user_input):
            verified_order_id = self.strong_verified_order_id
            if verified_order_id and self._has_recent_strong_verification(verified_order_id):
                request = get_action_request_status(verified_order_id)
                if request is None:
                    response = "I couldn't check the support request status right now. Please try again shortly."
                elif not request:
                    response = "I couldn't find a submitted support request for this verified order. Please contact support if you expected one to be recorded."
                else:
                    response = self._format_action_status(request)
            else:
                response = self._two_field_prompt()
            self.conversation_history.append(("assistant", response))
            return response, {}

        if self._is_delivery_issue_request(user_input):
            verified_order_id = self.strong_verified_order_id
            if verified_order_id and self._has_recent_strong_verification(verified_order_id):
                issue = next(
                    (
                        content.strip()
                        for role, content in reversed(self.conversation_history)
                        if role in {"human", "user"} and _DELIVERY_PROBLEM_RE.search(content)
                    ),
                    "Customer requests a delivery issue investigation.",
                )
                if re.search(r"\bmarked\s+delivered\b", issue, re.IGNORECASE) and re.search(
                    r"\b(?:didn['’]?t\s+receive|haven['’]?t\s+received|not\s+received)\b", issue, re.IGNORECASE
                ):
                    reason = "Order is marked delivered, but the customer reports it was not received."
                else:
                    reason = f"Customer requests a delivery investigation: {issue[:220]}"
                return self._stage_explicit_action_request(
                    user_input,
                    verified_order_id,
                    action="delivery_issue_request",
                    reason=reason,
                )
            response = self._ask_for_missing_field() if self.verification.has_any else self._two_field_prompt()
            self.conversation_history.append(("assistant", response))
            return response, {}

        if self.strong_verified_order_id and _DELIVERY_NOT_RECEIVED_RE.search(user_input):
            order = next(
                (
                    item
                    for item in self._orders()
                    if int(item.get("public_order_id", -1)) == self.strong_verified_order_id
                ),
                None,
            )
            if order:
                status = order.get("status") or "current status unavailable"
                response = (
                    f"I'm sorry you haven't received it. Order #{self.strong_verified_order_id:04d} "
                    f"currently shows {status}. I can prepare a delivery investigation request for our "
                    "support team. It will only be sent after you review and confirm it, and it will not "
                    "change the order."
                )
                self.conversation_history.append(("assistant", response))
                return response, {}

        if _NEW_ORDER_INTENT_RE.search(user_input):
            response = (
                "To request a new order, use the Request a new order form above the chat. "
                "Choose an item and quantity, then review and confirm your request. Support will confirm "
                "availability, pricing, and delivery; this request does not place an order or take payment."
            )
            self.conversation_history.append(("assistant", response))
            return response, {}

        if self._is_action_request(user_input):
            supplied_order_id = int(extracted["order_ids"][0]) if extracted["order_ids"] else None
            verified_order_id = self.strong_verified_order_id
            if (
                verified_order_id
                and (supplied_order_id is None or supplied_order_id == verified_order_id)
                and self._has_recent_strong_verification(verified_order_id)
            ):
                return self._stage_explicit_action_request(user_input, verified_order_id)

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

        verified_ids = self._verified_order_ids()
        message_order_ids = {int(order_id) for order_id in extracted["order_ids"]}
        if (
            verified_ids
            and message_order_ids
            and message_order_ids <= verified_ids
            and count_lookup_fields(user_input, extracted) < 2
        ):
            return self._continue_with_context(user_input), {}

        if self.verification.has_any:
            lowered = user_input.lower()
            if looks_like_bare_cancel(lowered) or _ABORT_RE.fullmatch(lowered.strip()):
                self.verification.reset()
                response = "No problem - we can start over whenever you're ready. To check an order I'll need your order number plus your email, phone number, or name."
                self.conversation_history.append(("assistant", response))
                return response, {}

        self.verification.merge(extracted)
        has_order = self.verification.order_id is not None
        has_secondary = self.verification.has_secondary or bool(self.authenticated_email and has_order)

        if has_order and has_secondary:
            return self._run_verified_lookup(user_input)

        if self.verification.has_any:
            response = self._ask_for_missing_field()
            self.conversation_history.append(("assistant", response))
            return response, {}

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
            criteria = self.verification.as_criteria()
            supplied_email = criteria.get("email")
            if self.authenticated_email and supplied_email and supplied_email.casefold() != self.authenticated_email:
                lookup = OrderLookup("not_found", criteria=criteria)
            else:
                if self.authenticated_email:
                    criteria["email"] = self.authenticated_email
                lookup = perform_order_lookup(OrderCriteria.from_mapping(criteria))
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
            def _gen():
                tool_results = []
                state = self.agent.invoke(
                    {"messages": ChatPromptTemplate.from_messages(self._build_message_list(user_input)).format_messages()}
                )
                messages = state.get("messages", [])
                for message in messages:
                    if getattr(message, "type", "") == "tool":
                        try:
                            tool_content = _message_text(getattr(message, "content", ""))
                            tool_results.append((getattr(message, "name", ""), json.loads(tool_content)))
                        except (TypeError, ValueError):
                            continue
                full = next(
                    (
                        text
                        for message in reversed(messages)
                        if getattr(message, "type", "") in ("ai", "assistant")
                        and (text := _message_text(getattr(message, "content", "")))
                    ),
                    "",
                )
                order_result = next((result for name, result in tool_results if name == "get_order_status"), None)
                policy_result = next((result for name, result in tool_results if name == "search_policies"), None)
                action_result = next(
                    (
                        result
                        for name, result in tool_results
                        if name in (
                            "request_return",
                            "cancel_order",
                            "request_delivery_issue",
                            "create_support_request",
                        )
                    ),
                    None,
                )
                policy_only = is_policy_question(user_input) and not is_order_specific_question(
                    user_input, bool(self.last_db_results)
                )
                contextual_query = (
                    self._standard_return_policy_followup(user_input) if policy_only else None
                )
                if policy_only and (
                    not policy_result or policy_result.get("status") in ("found", "no_match")
                ):
                    retry = search_policy_chunks(
                        contextual_query or user_input
                    )
                    if retry.status == "found":
                        retry_chunks = [
                            {
                                "source_title": chunk.source_title,
                                "heading": chunk.heading,
                                "content": chunk.content,
                            }
                            for chunk in retry.chunks
                        ]
                        if contextual_query:
                            retry_chunks = [
                                chunk
                                for chunk in retry_chunks
                                if "return" in (chunk.get("source_title") or "").casefold()
                            ]
                            if not retry_chunks:
                                policy_result = {"status": "no_match", "chunks": []}
                        merged_chunks = retry_chunks + (
                            [] if contextual_query else ((policy_result or {}).get("chunks") or [])
                        )
                        unique_chunks = []
                        seen_chunks = set()
                        for chunk in merged_chunks:
                            key = (chunk.get("source_title"), chunk.get("heading"), chunk.get("content"))
                            if key not in seen_chunks:
                                seen_chunks.add(key)
                                unique_chunks.append(chunk)
                        if unique_chunks:
                            policy_result = {"status": "found", "chunks": unique_chunks[:4]}
                    elif retry.status == "unavailable" and (
                        contextual_query or not policy_result or policy_result.get("status") == "no_match"
                    ):
                        policy_result = {"status": "unavailable", "chunks": []}
                    elif contextual_query or not policy_result:
                        policy_result = {"status": "no_match", "chunks": []}
                if action_result:
                    full = {
                        "pending_confirmation": "Please review the support request below and confirm it to submit. This sends a request for human review; it does not change the order.",
                        "verification_required": "Please verify this order with its email address or phone number before requesting an order change.",
                        "invalid_reason": "Please provide a short reason (300 characters or fewer) for the request.",
                        "invalid_request": "Please describe the request with a short topic and summary, then try again.",
                    }.get(action_result.get("status"), "I couldn't prepare that support request. Please contact support for help.")
                elif policy_only and (not policy_result or policy_result.get("status") != "found"):
                    status = (policy_result or {}).get("status")
                    full = (
                        "Policy search is temporarily unavailable. Please contact support to confirm the current policy."
                        if status == "unavailable"
                        else "I couldn't find a relevant policy in the help documents, so I don't want to guess. Try rephrasing the question or contact support to confirm the current policy."
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
                        else "I couldn't find a relevant policy in the help documents, so I don't want to guess. Try rephrasing the question or contact support to confirm the current policy."
                    )
                if not full.strip():
                    if order_result and order_result.get("status") == "found" and self.last_db_results:
                        full = format_grounded_lookup(self.last_db_results, name_only=self.name_only_verified)
                    elif self._is_order_inquiry(user_input):
                        full = self._two_field_prompt()
                    else:
                        full = FALLBACK_MESSAGE
                if policy_only and policy_result and policy_result.get("status") == "found":
                    sources = list(
                        dict.fromkeys(
                            _policy_source_label(chunk)
                            for chunk in policy_result.get("chunks", [])
                        )
                    )
                    if sources:
                        source_note = "\n\n**Sources:** " + "; ".join(sources)
                        full += source_note
                if on_step:
                    on_step("Preparing your response...")
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

    def _standard_return_policy_followup(self, user_input: str) -> str | None:
        """Resolve a short return-policy follow-up against the preceding assistant turn."""
        if not _STANDARD_RETURN_FOLLOWUP_RE.search(user_input):
            return None
        prior_assistant = next(
            (content for role, content in reversed(self.conversation_history) if role == "assistant"),
            "",
        )
        if not _STANDARD_RETURN_CONTEXT_RE.search(prior_assistant):
            return None
        return (
            "Returns Policy standard 30-day return policy: return window starts when order is marked "
            "Delivered; item condition and packaging requirements; non-returnable categories; how to "
            "initiate a return; return shipping charges."
        )

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
        if self.authenticated_email:
            return "Please enter your order number to look up an order on your signed-in account."
        return (
            f"I have your {', '.join(have)}. To verify your order, I also need your order number - "
            "for example `order 42`."
        )

    def _is_order_inquiry(self, user_input: str, extracted: dict | None = None) -> bool:
        message = user_input.lower()
        return bool(has_lookup_identifier(user_input, extracted) or any(word in message for word in FOLLOW_UP_WORDS))

    def _stage_explicit_action_request(
        self,
        user_input: str,
        order_id: int,
        action: str | None = None,
        reason: str | None = None,
    ):
        """Stage an explicit support action after order verification."""
        action_tool_name = (
            "request_delivery_issue"
            if action == "delivery_issue_request"
            else action or ("cancel_order" if "cancel" in user_input.casefold() else "request_return")
        )
        action_tool = next(tool for tool in self._make_action_tools() if tool.name == action_tool_name)
        result = json.loads(
            action_tool.invoke(
                {
                    "order_id": int(order_id),
                    "reason": (reason or user_input.strip())[:300] or "Customer request",
                }
            )
        )
        action_label = {
            "cancel_order": "cancellation",
            "request_return": "return",
            "request_delivery_issue": "delivery investigation",
        }[action_tool_name]
        status = result.get("status")
        if status == "pending_confirmation":
            response = (
                f"I can submit a {action_label} request for order #{int(order_id):04d} to our support team. "
                "It won't change the order automatically. Review and confirm the request below."
            )
        elif status == "verification_required":
            response = "Please verify this order with your account email or phone before requesting a change."
        else:
            response = "I couldn't prepare that request. Please contact support for help."
        self.conversation_history.append(("assistant", response))
        return response, {}

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

    def _is_delivery_issue_request(self, user_input: str) -> bool:
        if _EXPLICIT_DELIVERY_REQUEST_RE.search(user_input):
            return True
        if not _GENERIC_SUPPORT_REQUEST_RE.search(user_input):
            return False
        recent_context = " ".join(
            content for role, content in self.conversation_history[-6:] if role in {"human", "user", "assistant"}
        )
        return bool(_DELIVERY_PROBLEM_RE.search(recent_context))

    def _two_field_prompt(self) -> str:
        if self.authenticated_email:
            return "Please provide the order number for the order linked to your signed-in email account."
        return (
            "I need two details to verify your order before I can share anything: "
            "your order number plus your email, phone number, or name. "
            "For example: `order 42, email you@example.com`."
        )

    @staticmethod
    def _format_action_status(request: dict) -> str:
        action = {
            "cancel_request": "cancellation",
            "return_request": "return",
            "delivery_issue_request": "delivery investigation",
        }.get(request.get("action_type"), "support")
        status = request.get("status")
        if status == "submitted":
            return (
                f"Your {action} request is recorded and is waiting for a support agent to review it. "
                "It has not been marked reviewed or rejected, and the order has not been changed."
            )
        if status == "reviewed":
            return (
                f"A support agent has reviewed your {action} request. The request is marked Reviewed; "
                "the support system does not record a separate approval outcome or change the order automatically."
            )
        if status == "rejected":
            return f"A support agent has rejected your {action} request. Please contact support if you need more information."
        if status == "completed":
            return (
                f"Your {action} request is marked completed in the support system. "
                "You can ask me to check the verified order for its current status."
            )
        if status == "failed":
            return f"Your {action} request could not be completed. Please contact support for help."
        return "I found the support request, but its status is unavailable. Please contact support for an update."

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

        if self.authenticated_email:
            system_prompt += (
                "\n\nAUTHENTICATED ACCOUNT: This session's email was verified by Supabase Auth. "
                "The server supplies that account email to order lookup as the matching identity detail. "
                "Ask only for the order number; never request another identity detail or replace the "
                "account email with a user-provided email."
            )

        messages = [("system", system_prompt)]
        history = self.conversation_history
        current_already_appended = bool(history and history[-1] == ("human", user_input))
        prior_history = history[:-1] if current_already_appended else history
        recent_history = prior_history[-5:]
        messages.extend(
            ("user" if role == "human" else role, content)
            for role, content in recent_history
        )
        messages.append(("human", user_input))
        return messages

    def _continue_with_context(self, user_input):
        """Answer a follow-up from stored context or conversationally without data."""
        deterministic_response = format_order_follow_up(self._orders(), self.name_only_verified, user_input)
        if deterministic_response:
            self.conversation_history.append(("assistant", deterministic_response))
            return iter([deterministic_response])
        response, _ = self._run_tool_lookup(user_input)
        return response

    def _persist_message(self, role: str, content: str) -> None:
        """Persist one complete message in this authenticated user's transcript."""
        self.conversations.append(role, content)

    def end_session(self) -> None:
        """Mark the current conversation as ended (best-effort)."""
        self.conversations.end()

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
