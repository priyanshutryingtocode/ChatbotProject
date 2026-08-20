from langchain_core.prompts import ChatPromptTemplate
from setup import chatmodel, SYSTEM_PROMPT
from query import count_lookup_fields, extract_info_from_query, format_database_context, has_lookup_identifier
from database import find_orders, format_order_for_display, format_order_number, format_timestamp


STATUS_EXPLANATIONS = {
    "Processing": "The order has been received and is being prepared.",
    "In Transit": "The order has been dispatched and is on its way.",
    "Out for Delivery": "The order is with the delivery driver.",
    "Delivered": "The delivery has been completed.",
    "Cancelled": "The order has been cancelled.",
    "Failed Delivery": "A delivery attempt was unsuccessful.",
}

FOLLOW_UP_WORDS = ("track", "tracking", "status", "where is", "where's", "when", "arrive", "delivery", "deliver", "payment", "paid", "refund")


class OrderChatHandler:
    def __init__(self):
        self.llm = chatmodel()
        self.conversation_history = []
        self.current_order_context = None
        self.last_db_results = None
        # Identity fields supplied across messages, collected toward a verified lookup.
        self.pending_identity = {}

    def process_user_message(self, user_input):

        if not user_input or not user_input.strip():
            return "Hello! I'm here to help you check your order status. To look up an order I need two details: your order number plus your email, phone number, or name.", {}

        try:
            self.conversation_history.append(("human", user_input))
            extracted = extract_info_from_query(user_input)

            # A message that only re-references the already-verified order is a
            # follow-up, not a new lookup, so it can keep using stored context.
            verified_ids = self._verified_order_ids()
            message_order_ids = {int(order_id) for order_id in extracted["order_ids"]}
            if verified_ids and message_order_ids and message_order_ids <= verified_ids and count_lookup_fields(user_input) < 2:
                response = self._continue_with_context(user_input)
                return response, {}

            changed = self._merge_pending_identity(extracted)
            has_order = self.pending_identity.get("order_id") is not None
            has_secondary = bool(
                self.pending_identity.get("email")
                or self.pending_identity.get("phone")
                or self.pending_identity.get("name")
            )

            # Order number plus a second identity field: run the verified lookup now.
            if has_order and has_secondary:
                return self._run_verified_lookup()

            # This message supplied identity info but not enough yet: ask for the rest.
            if changed:
                response = self._ask_for_missing_field()
                self.conversation_history.append(("assistant", response))
                return response, {}

            # No new identity info. Nudge if there is an incomplete collection,
            # otherwise answer conversationally from context (or without any data).
            if (has_order or has_secondary) and self._is_order_inquiry(user_input):
                response = self._ask_for_missing_field()
                self.conversation_history.append(("assistant", response))
                return response, {}

            # An order inquiry with no context and no extractable identity info
            # must not reach the model unguided: state the two-field rule plainly.
            if not self.current_order_context and self._is_order_inquiry(user_input):
                response = self._two_field_prompt()
                self.conversation_history.append(("assistant", response))
                return response, {}

            response = self._continue_with_context(user_input)
            return response, {}

        except Exception as e:
            error_message = "I'm sorry, there was an error processing your request. Please try again or contact support."
            self.conversation_history.append(("assistant", error_message))
            return error_message, {}

    def _merge_pending_identity(self, extracted) -> bool:
        """Merge identity fields from a message into pending_identity.

        Returns True if any field was added or changed. A new, different order
        number invalidates previously gathered secondary fields.
        """
        changed = False
        if extracted["order_ids"]:
            new_order_id = extracted["order_ids"][0]
            if self.pending_identity.get("order_id") != new_order_id:
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
        self.current_order_context = format_database_context(db_results)
        self.last_db_results = db_results
        self.pending_identity = {}
        response = self._format_grounded_lookup(db_results)
        self.conversation_history.append(("assistant", response))
        return response, db_results

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

    def _two_field_prompt(self) -> str:
        return (
            "I need two details to verify your order before I can share anything: "
            "your order number plus your email, phone number, or name. "
            "For example: `order 42, email you@example.com`."
        )

    def _continue_with_context(self, user_input):
        """Answer a follow-up from stored context, or conversationally without data."""
        deterministic_response = self._format_order_follow_up(user_input)
        if deterministic_response:
            self.conversation_history.append(("assistant", deterministic_response))
            return deterministic_response

        if self.current_order_context:
            system_prompt = (
                f"{SYSTEM_PROMPT}\n\n{self.current_order_context}\n\n"
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

        prompt_template = ChatPromptTemplate.from_messages(messages)
        chain = prompt_template | self.llm
        response = chain.invoke({})
        self.conversation_history.append(("assistant", response.content))
        return response.content

    def clear_context(self):

        self.conversation_history = []
        self.current_order_context = None
        self.last_db_results = None
        self.pending_identity = {}

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

        return self.current_order_context is not None

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