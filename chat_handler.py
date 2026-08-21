import json
import logging
import re

from langchain_core.messages import ToolMessage
from langchain_core.prompts import ChatPromptTemplate

from database import (
    append_message,
    create_conversation,
    end_conversation,
    find_orders,
    format_order_for_display,
    format_order_number,
    format_timestamp,
    get_messages,
)
from query import count_lookup_fields, extract_info_from_query, format_database_context, has_lookup_identifier
from setup import SYSTEM_PROMPT, chatmodel
from tools import build_llm_with_tools, lookup_order, search_policy

logger = logging.getLogger(__name__)

STATUS_EXPLANATIONS = {
    "Processing": "The order has been received and is being prepared.",
    "In Transit": "The order has been dispatched and is on its way.",
    "Out for Delivery": "The order is with the delivery driver.",
    "Delivered": "The delivery has been completed.",
    "Cancelled": "The order has been cancelled.",
    "Failed Delivery": "A delivery attempt was unsuccessful.",
}

FOLLOW_UP_WORDS = ("track", "tracking", "status", "where is", "where's", "when", "arrive", "delivery", "deliver", "payment", "paid", "refund")

# Keywords marking a public-policy / FAQ question. Policy answers come from
# retrieved documents and never require identity verification.
POLICY_WORDS = (
    "policy", "policies", "return", "returns", "refund", "refunded",
    "exchange", "warranty", "damaged", "damage", "cancellation",
    "shipping cost", "delivery charge", "delivery slot", "time slot",
)

# Phrase-level policy intents that keyword matching alone would miss.
POLICY_PHRASES = (
    re.compile(r"\bhow\s+(?:do|can)\s+i\s+cancel\b"),
    re.compile(r"\bcancel(?:\s+my|\s+the)?\s+order\b"),
    re.compile(r"\breturn\s+(?:my|the|an)?\s*(?:order|item)\b"),
)

# Words that are clearly conversational filler rather than a bare name.
BARE_NAME_STOPWORDS = {
    "hello", "hi", "hey", "thanks", "thank", "thank you", "yes", "yep", "yeah",
    "no", "nope", "sure", "ok", "okay", "good", "great", "right", "alright",
    "see", "nothing", "never", "cancel", "stop", "done", "ok done",
}

# First words that signal a question or non-identity statement.
BARE_NAME_QUESTION_WORDS = {
    "where", "what", "when", "how", "why", "who", "which", "can", "could",
    "would", "will", "shall", "is", "are", "do", "does", "did", "please",
    "wait", "hold", "let", "i", "my", "we", "you", "it", "there", "cancel",
}

# A bare abort of the current flow ("cancel", "cancel that") — distinct from a
# cancellation *request* about an order ("cancel my order"), which is policy.
_BARE_CANCEL_RE = re.compile(r"^(?:ok[,\s]*)?(?:please\s+)?cancel(?:\s+(?:it|that|this))?\s*(?:please)?$")


def _looks_like_bare_cancel(message_lower: str) -> bool:
    candidate = message_lower.strip()
    return bool(_BARE_CANCEL_RE.fullmatch(candidate))


def _looks_like_bare_name(text: str) -> bool:
    """Whether a short, all-letter message is plausibly the customer's name."""
    candidate = text.strip().strip(".,;:!?")
    if not 2 <= len(candidate) <= 60:
        return False
    if not re.fullmatch(r"[A-Za-z][A-Za-z \-'\.]*", candidate):
        return False
    words = candidate.split()
    if not 1 <= len(words) <= 3:
        return False
    lowered = candidate.lower()
    if lowered in BARE_NAME_STOPWORDS or words[0].lower() in BARE_NAME_QUESTION_WORDS:
        return False
    return True


class OrderChatHandler:
    def __init__(self, conversation_id: str | None = None):
        self.llm = chatmodel()
        self.llm_with_tools = build_llm_with_tools()
        self.conversation_history = []
        self.last_db_results = None
        # Identity fields supplied across messages, collected toward a verified lookup.
        self.pending_identity = {}
        self.conversation_id = conversation_id
        self.last_message_ids: dict[str, str] = {}

        if conversation_id:
            try:
                stored = get_messages(conversation_id, limit=50)
                self.conversation_history = [
                    ("human" if message.get("role") == "user" else "assistant", message.get("content", ""))
                    for message in stored
                ]
            except Exception:
                logger.exception("Failed to load conversation history")

    def process_user_message(self, user_input):
        if not user_input or not user_input.strip():
            greeting = (
                "Hello! I'm here to help you check your order status. To look up an order "
                "I need two details: your order number plus your email, phone number, or name."
            )
            self._persist_message("user", user_input or "")
            self._persist_message("assistant", greeting)
            return greeting, {}

        self._persist_message("user", user_input)
        try:
            self.conversation_history.append(("human", user_input))
            response, db_results = self._route_message(user_input)
        except Exception:
            logger.exception("Error processing user message")
            response = "I'm sorry, there was an error processing your request. Please try again or contact support."
            db_results = {}
            self.conversation_history.append(("assistant", response))

        self._persist_message("assistant", response, db_results)
        return response, db_results

    def _route_message(self, user_input):
        extracted = extract_info_from_query(user_input)

        # While awaiting an order number, a bare standalone number is the order.
        if not extracted["order_ids"] and self.pending_identity and not self.pending_identity.get("order_id"):
            bare_match = re.search(r"\b(\d{1,4})\b", user_input)
            if bare_match:
                candidate = int(bare_match.group(1))
                if 1 <= candidate <= 1000:
                    extracted["order_ids"] = [str(candidate)]

        # While awaiting the second identity detail, a bare alphabetic message
        # is the customer's name.
        if (
            not any(extracted.values())
            and self.pending_identity.get("order_id")
            and not self.pending_identity.get("email")
            and not self.pending_identity.get("phone")
            and not self.pending_identity.get("name")
            and _looks_like_bare_name(user_input)
        ):
            extracted["names"] = [user_input.strip().strip(".,;:!?")]

        # A message that only re-references the already-verified order is a
        # follow-up, not a new lookup, so it can keep using stored context.
        verified_ids = self._verified_order_ids()
        message_order_ids = {int(order_id) for order_id in extracted["order_ids"]}
        if verified_ids and message_order_ids and message_order_ids <= verified_ids and count_lookup_fields(user_input) < 2:
            return self._continue_with_context(user_input), {}

        # Let the user bail out of a half-finished verification. An explicit
        # policy question instead interrupts collection to answer, keeping the
        # collected identity so verification can resume afterwards.
        if self.pending_identity:
            lowered = user_input.lower()
            if _looks_like_bare_cancel(lowered) or re.search(
                r"\b(?:never\s*mind|forget\s*(?:it|that)|skip|nothing)\b", lowered
            ):
                self.pending_identity = {}
                response = "No problem — we can start over whenever you're ready. To check an order I'll need your order number plus your email, phone number, or name."
                self.conversation_history.append(("assistant", response))
                return response, {}
            if self._is_policy_question(user_input):
                return self._run_tool_lookup(user_input)

        self._merge_pending_identity(extracted)
        has_order = self.pending_identity.get("order_id") is not None
        has_secondary = bool(
            self.pending_identity.get("email")
            or self.pending_identity.get("phone")
            or self.pending_identity.get("name")
        )

        # Order number plus a second identity field: run the verified lookup now.
        if has_order and has_secondary:
            return self._run_verified_lookup()

        # Verification in progress (or identity info just supplied): stay
        # deterministic and collect the missing detail. The free-form model is
        # never used mid-verification, so it cannot invent order data.
        if self.pending_identity:
            response = self._ask_for_missing_field()
            self.conversation_history.append(("assistant", response))
            return response, {}

        # An order inquiry or policy question the rules could not classify:
        # let the tool loop resolve it (lookup_order enforces verification
        # server-side; search_policy retrieves public policy documents).
        if not self.last_db_results and (
            self._is_order_inquiry(user_input) or self._is_policy_question(user_input)
        ):
            return self._run_tool_lookup(user_input)

        return self._continue_with_context(user_input), {}

    def _merge_pending_identity(self, extracted) -> bool:
        """Merge identity fields from a message into pending_identity.

        Returns True if any field was added or changed. A new, different order
        number invalidates previously gathered secondary fields.
        """
        changed = False
        if extracted["order_ids"]:
            new_order_id = extracted["order_ids"][0]
            if self.pending_identity.get("order_id") != new_order_id:
                if self.pending_identity.get("order_id") is not None:
                    self.pending_identity = {}
                self.pending_identity["order_id"] = new_order_id
                changed = True
        if extracted["emails"]:
            email = extracted["emails"][0]
            if self.pending_identity.get("email") != email:
                self.pending_identity["email"] = email
                changed = True
        if extracted["phones"]:
            phone = extracted["phones"][0]
            if self.pending_identity.get("phone") != phone:
                self.pending_identity["phone"] = phone
                changed = True
        if extracted["names"]:
            name = extracted["names"][0]
            if self.pending_identity.get("name") != name:
                self.pending_identity["name"] = name
                changed = True
        return changed

    def _run_verified_lookup(self):
        """Look up orders only when every collected field matches the same order."""
        try:
            orders = find_orders(self.pending_identity, require_order_id=True)
        except ValueError:
            orders = []

        if not orders:
            self.pending_identity = {}
            response = "I couldn't find an order matching that information. Please check the order number or customer details and try again."
            self.conversation_history.append(("assistant", response))
            return response, {}

        db_results = {"matched": orders}
        self.last_db_results = db_results
        self.pending_identity = {}
        response = self._format_grounded_lookup(db_results)
        self.conversation_history.append(("assistant", response))
        return response, db_results

    def _run_tool_lookup(self, user_input):
        """Resolve an ambiguous order inquiry through the tool-calling model.

        The lookup_order tool enforces the two-field rule server-side. Tool
        results seed pending_identity / stored context so later deterministic
        turns can continue naturally.
        """
        try:
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

            final = self.llm_with_tools.invoke(messages)
            response = getattr(final, "content", "") or (
                self._format_grounded_lookup(db_results) if db_results else self._two_field_prompt()
            )
            self.conversation_history.append(("assistant", response))
            return response, db_results
        except Exception:
            logger.exception("Tool lookup failed")
            response = self._two_field_prompt()
            self.conversation_history.append(("assistant", response))
            return response, {}

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
                self.pending_identity = {}
        elif payload.get("status") == "need_more_info":
            self.pending_identity = self._criteria_from_args(args)
        else:  # not_found / error
            self.pending_identity = {}
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
        pending = self.pending_identity
        if pending.get("order_id"):
            return (
                f"I have your order number (#{format_order_number(pending['order_id'])}). "
                "To verify it, please also provide the email, phone number, or name on the order."
            )
        have = [label for label, key in (("email", "email"), ("phone number", "phone"), ("name", "name")) if pending.get(key)]
        return (
            f"I have your {', '.join(have)}. To verify your order, I also need your order number — "
            "for example `order 42`."
        )

    def _is_order_inquiry(self, user_input: str) -> bool:
        message = user_input.lower()
        return bool(has_lookup_identifier(user_input) or any(word in message for word in FOLLOW_UP_WORDS))

    def _is_policy_question(self, user_input: str) -> bool:
        message = user_input.lower()
        if any(word in message for word in POLICY_WORDS):
            return True
        return any(pattern.search(message) for pattern in POLICY_PHRASES)

    def _two_field_prompt(self) -> str:
        return (
            "I need two details to verify your order before I can share anything: "
            "your order number plus your email, phone number, or name. "
            "For example: `order 42, email you@example.com`."
        )

    def _build_message_list(self, user_input):
        """System prompt (with query-relevant order data) + history + current message."""
        if self.last_db_results:
            context = format_database_context(self.last_db_results, query=user_input)
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
        """Answer a follow-up from stored context, or conversationally without data."""
        deterministic_response = self._format_order_follow_up(user_input)
        if deterministic_response:
            self.conversation_history.append(("assistant", deterministic_response))
            return deterministic_response

        prompt_template = ChatPromptTemplate.from_messages(self._build_message_list(user_input))
        chain = prompt_template | self.llm
        response = chain.invoke({})
        self.conversation_history.append(("assistant", response.content))
        return response.content

    @staticmethod
    def _criteria_from_args(args: dict) -> dict:
        criteria = {}
        if args.get("order_id") is not None:
            criteria["order_id"] = str(args["order_id"])
        if args.get("email"):
            criteria["email"] = args["email"]
        if args.get("phone"):
            criteria["phone"] = args["phone"]
        if args.get("customer_name"):
            criteria["name"] = args["customer_name"]
        return criteria

    def _persist_message(self, role: str, content: str, db_results: dict | None = None) -> None:
        """Persist one turn to the conversation log. Never raises."""
        self._ensure_conversation()
        if not self.conversation_id:
            return
        db_role = "user" if role == "human" else role
        message_id = append_message(self.conversation_id, db_role, content, db_results)
        self.last_message_ids[db_role] = message_id

    def _ensure_conversation(self) -> None:
        if not self.conversation_id:
            self.conversation_id = create_conversation()

    def end_session(self) -> None:
        """Mark the current conversation as ended (best-effort)."""
        if self.conversation_id:
            end_conversation(self.conversation_id)
            self.conversation_id = None

    def clear_context(self):

        self.conversation_history = []
        self.last_db_results = None
        self.pending_identity = {}
        self.last_message_ids = {}

    def _verified_order_ids(self) -> set[int]:
        """Order IDs established by the last successful two-field lookup."""
        verified = set()
        if not self.last_db_results:
            return verified
        for value in self.last_db_results.values():
            orders = value if isinstance(value, list) else [value]
            for order in orders:
                order_id = order.get("public_order_id")
                if order_id is not None:
                    verified.add(int(order_id))
        return verified

    def get_conversation_history(self):

        return self.conversation_history.copy()

    def has_order_context(self):

        return self.last_db_results is not None

    def get_current_order_info(self):

        return self.last_db_results if self.last_db_results else None

    @staticmethod
    def _format_grounded_lookup(db_results):
        """Create an exact database-backed response for a new identity lookup."""
        orders = []
        for value in db_results.values():
            orders.extend(value if isinstance(value, list) else [value])

        if len(orders) != 1:
            details = "\n\n".join(format_order_for_display(order) for order in orders)
            return f"I found {len(orders)} matching orders. Here are the current database details:\n\n{details}"

        order = orders[0]
        shipment = (order.get("shipments") or [{}])[0]
        status = order.get("status", "Unknown")
        lines = [
            f"Order #{format_order_number(order.get('public_order_id'))} is **{status}**.",
            STATUS_EXPLANATIONS.get(status, "This is the current status recorded in the order system."),
        ]
        if shipment.get("estimated_delivery_at"):
            lines.append(f"Estimated delivery: **{format_timestamp(shipment['estimated_delivery_at'])}**.")
        if shipment.get("delivered_at"):
            lines.append(f"Delivered: **{format_timestamp(shipment['delivered_at'])}**.")
        if shipment.get("tracking_number"):
            lines.append(f"Tracking number: `{shipment['tracking_number']}`.")
        return "\n\n".join(lines)

    def _format_order_follow_up(self, user_input):
        """Answer high-risk order facts without relying on the language model."""
        if not self.last_db_results:
            return None

        orders = []
        for value in self.last_db_results.values():
            orders.extend(value if isinstance(value, list) else [value])
        if len(orders) != 1:
            return None

        order = orders[0]
        shipment = (order.get("shipments") or [{}])[0]
        message = user_input.lower()
        order_number = format_order_number(order.get("public_order_id"))

        if "driver" in message:
            driver = shipment.get("delivery_driver_name")
            if driver:
                return f"The delivery driver for order #{order_number} is **{driver}**."
            return f"No delivery driver has been assigned yet for order #{order_number}."
        if any(word in message for word in ("track", "tracking number", "tracking")):
            tracking = shipment.get("tracking_number")
            return f"The tracking number for order #{order_number} is `{tracking}`." if tracking else f"Tracking information is not available yet for order #{order_number}."
        if any(word in message for word in ("when", "arrive", "delivery", "deliver")):
            delivered = shipment.get("delivered_at")
            estimated = shipment.get("estimated_delivery_at")
            if delivered:
                return f"Order #{order_number} was delivered on **{format_timestamp(delivered)}**."
            if estimated:
                return f"The estimated delivery for order #{order_number} is **{format_timestamp(estimated)}**."
            return f"There is no delivery estimate available yet for order #{order_number}."
        if any(word in message for word in ("status", "where is", "where's")):
            status = order.get("status", "Unknown")
            return f"Order #{order_number} is currently **{status}**. {STATUS_EXPLANATIONS.get(status, '')}".strip()
        if any(word in message for word in ("payment", "paid", "refund")):
            return f"Payment status for order #{order_number}: **{order.get('payment_status', 'Unknown')}**."
        return None