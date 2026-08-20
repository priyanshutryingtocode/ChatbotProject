"""Gemini function-calling tools for the order assistant.

The lookup_order tool is the only database tool. It enforces the two-field
verification rule server-side: an order number plus one of email, phone, or
name must all point to the same order before anything is returned.
"""

import json

from langchain_core.tools import tool

from database import find_orders
from query import format_database_context, normalize_field_keys
from setup import chatmodel


@tool
def lookup_order(order_id: int, email: str = "", phone: str = "", customer_name: str = "", fields: list[str] | None = None) -> str:
    """Look up an order. Requires an order number (0001-1000) plus at least one
    of: the customer's email, phone number, or name on the order. Returns the
    matching order details, a request for more identity details, or not-found.

    Pass a `fields` list naming only the details the customer is asking about
    (e.g. ["status"], ["tracking"], ["carrier"], ["delivery"], ["driver"],
    ["payment"], ["items"], ["customer"]). The order number and status are
    always included. Unknown field names are ignored.
    """
    criteria = {"order_id": str(order_id)}
    if email.strip():
        criteria["email"] = email.strip()
    if phone.strip():
        criteria["phone"] = phone.strip()
    if customer_name.strip():
        criteria["name"] = customer_name.strip()

    if len(criteria) < 2:
        return json.dumps(
            {
                "status": "need_more_info",
                "message": "Two identity details are required: an order number plus the customer's email, phone number, or name.",
            }
        )

    orders = find_orders(criteria, require_order_id=True)
    if not orders:
        return json.dumps({"status": "not_found"})

    db_results = {"matched": orders}
    selected = normalize_field_keys(fields) if fields else set()
    context = format_database_context(db_results, fields=selected if selected else None)

    summary_fields = ["public_order_id", "status"]
    if not selected or "payment" in selected:
        summary_fields += ["payment_status", "total_amount", "currency"]

    return json.dumps(
        {
            "status": "found",
            "orders": [{key: order.get(key) for key in summary_fields} for order in orders],
            "context": context,
        }
    )


def build_llm_with_tools():
    """Gemini model bound with the order lookup tool."""
    return chatmodel().bind_tools([lookup_order])