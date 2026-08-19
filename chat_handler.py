from langchain_core.prompts import ChatPromptTemplate
from setup import chatmodel, SYSTEM_PROMPT
from query import format_database_context, has_lookup_identifier, query_database
from database import format_order_for_display, format_order_number, format_timestamp


STATUS_EXPLANATIONS = {
    "Processing": "The order has been received and is being prepared.",
    "In Transit": "The order has been dispatched and is on its way.",
    "Out for Delivery": "The order is with the delivery driver.",
    "Delivered": "The delivery has been completed.",
    "Cancelled": "The order has been cancelled.",
    "Failed Delivery": "A delivery attempt was unsuccessful.",
}

class OrderChatHandler:
    def __init__(self):
        self.llm = chatmodel()
        self.conversation_history = []
        self.current_order_context = None
        self.last_db_results = None
        
    def process_user_message(self, user_input):
        
        if not user_input or not user_input.strip():
            return "Hello! I'm here to help you check your order status. Please provide your order ID, email, phone number, or name.", {}
        
        try:
       
            self.conversation_history.append(("human", user_input))
            
          
            is_explicit_lookup = has_lookup_identifier(user_input)
            db_results = query_database(user_input)

            # Database identity lookups must never fall back to previous order
            # context. That could expose or describe the wrong order.
            if is_explicit_lookup:
                if not db_results:
                    self.current_order_context = None
                    self.last_db_results = None
                    response = "I couldn't find an order matching that information. Please check the order number or customer details and try again."
                    self.conversation_history.append(("assistant", response))
                    return response, {}

                db_context = format_database_context(db_results)
                self.current_order_context = db_context
                self.last_db_results = db_results
                response = self._format_grounded_lookup(db_results)
                self.conversation_history.append(("assistant", response))
                return response, db_results
            
            if db_results:
                db_context = format_database_context(db_results)
                self.current_order_context = db_context
                self.last_db_results = db_results
            elif self.current_order_context:
                db_context = self.current_order_context
            else:
                db_context = None

            deterministic_response = self._format_order_follow_up(user_input)
            if deterministic_response:
                self.conversation_history.append(("assistant", deterministic_response))
                return deterministic_response, {}
            
            
            if db_context:
                system_prompt = f"{SYSTEM_PROMPT}\n\n{db_context}\n\nPlease use the above order information to provide accurate and helpful responses. Maintain context from previous messages in this conversation."
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
            
            return response.content, db_results
            
        except Exception as e:
            error_message = "I'm sorry, there was an error processing your request. Please try again or contact support."
            self.conversation_history.append(("assistant", error_message))
            return error_message, {}
    
    def clear_context(self):
        
        self.conversation_history = []
        self.current_order_context = None
        self.last_db_results = None
    
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
