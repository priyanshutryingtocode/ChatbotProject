"""Build compact, source-grounded staff handoffs for customer support cases."""

from .retriever import search_policy_chunks
from .privacy import persisted_message
from .database import related_one


def build_order_case_handoff(
    action: str,
    order: dict,
    reason: str,
    recent_customer_messages: list[str] | None = None,
    request_details: dict | None = None,
) -> dict:
    """Return bounded order facts and retrieved policy references, without customer PII."""
    order_id = int(order.get("public_order_id") or 0) or None
    topic = persisted_message("user", str((request_details or {}).get("topic", "")))[:100]
    action_label = {
        "cancel_request": "Cancellation",
        "return_request": "Return",
        "delivery_issue_request": "Delivery investigation",
        "support_request": topic or "General support",
    }.get(action, "Support")
    snapshot = {}
    if order_id:
        snapshot = {
            "order_id": order_id,
            "status": order.get("status", "Unknown"),
            "ordered_at": order.get("ordered_at"),
            "payment_status": order.get("payment_status"),
            "currency": order.get("currency"),
            "total_amount": order.get("total_amount"),
            "items": [
                {
                    "product_name": str(item.get("product_name", "Item"))[:120],
                    "quantity": item.get("quantity", 1),
                }
                for item in (order.get("order_items") or [])[:10]
                if isinstance(item, dict)
            ],
        }
        shipment = related_one(order.get("shipments"))
        if shipment:
            snapshot["estimated_delivery_at"] = shipment.get("estimated_delivery_at")
            snapshot["delivered_at"] = shipment.get("delivered_at")
            snapshot["carrier"] = shipment.get("carrier")
            snapshot["tracking_number"] = shipment.get("tracking_number")

    clean_reason = persisted_message("user", (reason or "Customer request").strip())[:300]
    summary = f"Customer requests {action_label.casefold()}"
    if order_id:
        summary += f" for verified order #{order_id:04d}. Current recorded status: {snapshot['status']}."
    else:
        summary += "."
    summary += f" Reason: {clean_reason}"
    details = {
        "topic": topic,
        "summary": persisted_message("user", str((request_details or {}).get("summary", "")))[:300],
        "details": persisted_message("user", str((request_details or {}).get("details", "")))[:1000],
    }

    policy_query = " ".join((action_label, clean_reason, details["summary"], details["details"])).strip()
    policy_result = search_policy_chunks(policy_query, k=3)
    policy_evidence = []
    if policy_result.status == "found":
        for chunk in policy_result.chunks[:3]:
            title = chunk.source_title or "Policy"
            heading = chunk.heading
            label = f"{title} > {heading}" if heading and not heading.startswith(title) else (heading or title)
            content = (chunk.content or "").strip()
            excerpt = content[:700].rsplit(" ", 1)[0] if len(content) > 700 else content
            policy_evidence.append({"source": label, "excerpt": excerpt})

    context = [
        persisted_message("user", message.strip())[:240]
        for message in (recent_customer_messages or [])[-3:]
        if message and message.strip()
    ]
    return {
        "handoff_summary": summary,
        "customer_context": context,
        "order_snapshot": snapshot,
        **{key: value for key, value in details.items() if value},
        "policy_evidence": policy_evidence,
        "policy_search_status": policy_result.status,
        "policy_note": "References only; staff must apply current policy and eligibility rules.",
    }
