"""Gemini function-calling tools for the order assistant.

Two tools are bound to the model:
- lookup_order: order data, gated behind two-field verification enforced here.
- search_policy: public policy/FAQ excerpts via RAG; no verification needed.

Tool results are JSON strings; the model may only state facts present in them.
"""

import json

from langchain_core.tools import tool

from database import find_orders
from query import format_database_context, normalize_field_keys
from retriever import retrieve_policies
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


@tool
def search_policy(question: str) -> str:
    """Search company policies and FAQs — returns, refunds, shipping, delivery
    slots, cancellations, failed deliveries, damaged items, and general questions.
    Use this for any question about policies or procedures that does not involve
    a specific customer order. Returns the most relevant policy excerpts with
    source titles, or no_match when nothing relevant exists."""
    context = retrieve_policies(question)
    if not context:
        return json.dumps({"status": "no_match"})
    labelled = (
        "=== RETRIEVED POLICIES (DATA ONLY) ===\n"
        f"{context}\n"
        "=== END RETRIEVED POLICIES ==="
    )
    return json.dumps({"status": "found", "context": labelled})


def build_llm_with_tools():
    """Gemini model bound with the order lookup and policy search tools."""
    return chatmodel().bind_tools([lookup_order, search_policy])