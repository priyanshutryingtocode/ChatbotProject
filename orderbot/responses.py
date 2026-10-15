"""Turning database rows into customer-facing text.

Order facts are rendered deterministically here rather than by the language
model, so a reply can never invent a status, a date, or a tracking number. The
functions take the verified order data as arguments; they hold no state.
"""

import re
from .config import STATUS_EXPLANATIONS
from .database import format_order_for_display, format_timestamp, order_summary, related_one

_WH = r"who|whom|which|what"

_INTENTS = (
    (
        "delivery_issue",
        re.compile(
            r"\b(?:i|we)\s+(?:(?:have|has|do|did)\s+not|haven't|havent|hasn't|hasnt|"
            r"don't|dont|didn't|didnt)\s+(?:yet\s+)?(?:receive(?:d)?|get|got|find|see|locate)\b|"
            r"\b(?:package|parcel|order)\s+(?:never\s+)?(?:arrived|received)\b",
            re.I,
        ),
    ),
    (
        "order_date",
        re.compile(
            r"\b(?:when\s+(?:was|did)\s+(?:my\s+|the\s+)?order\s+(?:placed|ordered)|"
            r"when\s+did\s+i\s+(?:place|order)\s+(?:my\s+|the\s+)?order|"
            r"(?:order|purchase)\s+date|date\s+(?:of|when)\s+(?:the\s+)?order)\b",
            re.I,
        ),
    ),
    (
        "driver",
        re.compile(
            rf"\b(?:{_WH})\b[^.?!\n]{{0,30}}?\b(?:driver|deliver(?:ed|ing)?|courier|"
            rf"bring|brought|van|drop(?:ped|s|ping)?)\b"
            rf"|^\s*(?:by\s+)?(?:{_WH})\b\s*\??\s*$",
            re.I,
        ),
    ),
    (
        "tracking",
        re.compile(r"\b(?:tracking|track|consignment|waybill|courier|carrier|shipped\s+by|shipping\s+company)\b", re.I),
    ),
    (
        "refusal",
        re.compile(
            r"\b(?:cancel|cancell?ing|return|returns|exchange|swap|"
            r"(?:change|modify|update|edit|amend|fix)\s+(?:my|the|this)?\s*"
            r"(?:address|order|details?|email|phone|number)|"
            r"refuse\s+(?:it|the|delivery)?|"
            r"(?:get|receive|request|claim|apply\s+for|process)\s+(?:a|an|my|the)?\s*refund)\b",
            re.I,
        ),
    ),
    (
        "payment",
        re.compile(
            r"\b(?:payment|pay|paid|paying|refund|refunded|charge|charged|bill|billed|money|owe|invoice|price|cost)\b",
            re.I,
        ),
    ),
    (
        "items",
        re.compile(
            r"\b(?:item|items|product|products|contents?|contain|contains|included?|bought|buy|"
            r"purchase[ds]?|ordered)\b",
            re.I,
        ),
    ),
    (
        "date",
        re.compile(
            r"\b(?:when|arrive|arrive[sd]?|arriving|eta|expected|estimate[ds]?|deliver|delivery|"
            r"delivered|ship|shipped|still|date|day|arrival)\b",
            re.I,
        ),
    ),
    (
        "status",
        re.compile(
            r"\b(?:status|where(?:'s| is| has| did)?|progress|update|stage|state|happen(?:ed)?|go(?:ing)?)\b", re.I
        ),
    ),
)

_SENSITIVE_INTENTS = frozenset({"driver", "tracking", "payment", "items", "order_date"})


def classify_intent(message: str) -> str | None:
    """Return the intent of a follow-up message, or None if unrecognised.

    Pure and stateless: no database, no model, no handler state.
    """
    for name, pattern in _INTENTS:
        if pattern.search(message):
            return name
    return None


def format_grounded_lookup(db_results: dict, name_only: bool = False) -> str:
    """Exact database-backed response for a new identity lookup."""
    orders = db_results.get("matched") or []
    if len(orders) != 1:
        if name_only:
            lines = [f"I found {len(orders)} matching orders. With name-only verification, I can share status and delivery timing:"]
            for order in orders:
                summary = order_summary(order)
                line = f"\n\nOrder #{summary['order_number']}: **{summary['status']}**"
                if summary["delivered_at"]:
                    line += f"; delivered {format_timestamp(summary['delivered_at'])}"
                elif summary["estimated_delivery_at"]:
                    line += f"; estimated delivery {format_timestamp(summary['estimated_delivery_at'])}"
                lines.append(line + ".")
            return "".join(lines)
        details = "\n\n".join(format_order_for_display(order) for order in orders)
        return f"I found {len(orders)} matching orders. Here are the current database details:\n\n{details}"

    order = orders[0]
    shipment = related_one(order.get("shipments"))
    summary = order_summary(order)
    status = summary["status"]
    lines = [
        f"Order #{summary['order_number']} is **{status}**.",
        STATUS_EXPLANATIONS.get(status, "This is the current status recorded in the order system."),
    ]
    if summary["estimated_delivery_at"]:
        lines.append(f"Estimated delivery: **{format_timestamp(summary['estimated_delivery_at'])}**.")
    if summary["delivered_at"]:
        lines.append(f"Delivered: **{format_timestamp(summary['delivered_at'])}**.")
    if shipment.get("tracking_number") and not name_only:
        lines.append(f"Tracking number: `{shipment['tracking_number']}`.")
    return "\n\n".join(lines)


def format_order_follow_up(orders: list[dict], name_only: bool, user_input: str) -> str | None:
    """Answer high-risk order facts without relying on the language model.

    Returns None when the question is not one of the grounded facts, letting the
    caller fall back to the model. `orders` and `name_only` come from the
    handler's verified state.
    """
    if len(orders) != 1:
        return None

    intent = classify_intent(user_input)
    if intent is None:
        return None

    if name_only and intent in _SENSITIVE_INTENTS:
        return (
            "For privacy, name-only verification can show order status and delivery timing only. "
            "Please provide the email address or phone number on the order for additional details."
        )

    order = orders[0]
    shipment = related_one(order.get("shipments"))
    summary = order_summary(order)
    order_number = summary["order_number"]

    if intent == "refusal":
        return (
            "I can't change an order myself. Please contact customer support and they'll be able to "
            "arrange cancellation, return, or any other change for you."
        )

    if intent == "delivery_issue":
        status = summary["status"]
        if status == "Delivered":
            delivered = shipment.get("delivered_at")
            delivered_text = f" on {format_timestamp(delivered)}" if delivered else ""
            return (
                f"Our system shows order #{order_number} as delivered{delivered_text}. "
                "If you haven't received it, please contact customer support to report the delivery discrepancy. "
                "I can't verify who received it or start an investigation from this chat."
            )
        estimated = shipment.get("estimated_delivery_at")
        if estimated and status == "Out for Delivery":
            return (
                f"Order #{order_number} is still marked Out for Delivery, with an estimated delivery of "
                f"{format_timestamp(estimated)}. If it does not arrive, please contact customer support."
            )
        return (
            f"The latest status for order #{order_number} is **{status}**. "
            "Please contact customer support for help locating the package."
        )

    if intent == "driver":
        driver = shipment.get("delivery_driver_name")
        if driver:
            return f"The delivery driver for order #{order_number} is **{driver}**."
        return f"No delivery driver has been assigned yet for order #{order_number}."

    if intent == "tracking":
        tracking = shipment.get("tracking_number")
        return (
            f"The tracking number for order #{order_number} is `{tracking}`."
            if tracking
            else f"Tracking information is not available yet for order #{order_number}."
        )

    if intent == "payment":
        return f"Payment status for order #{order_number}: **{order.get('payment_status', 'Unknown')}**."

    if intent == "order_date":
        ordered_at = order.get("ordered_at")
        return (
            f"Order #{order_number} was placed on **{format_timestamp(ordered_at)}**."
            if ordered_at
            else f"The order date is not available for order #{order_number}."
        )

    if intent == "date":
        delivered = shipment.get("delivered_at")
        estimated = shipment.get("estimated_delivery_at")
        if delivered:
            return f"Order #{order_number} was delivered on **{format_timestamp(delivered)}**."
        if estimated:
            return f"The estimated delivery for order #{order_number} is **{format_timestamp(estimated)}**."
        return f"There is no delivery estimate available yet for order #{order_number}."

    if intent == "items":
        items = order.get("order_items") or []
        if items:
            listed = ", ".join(
                f"{item.get('quantity', 1)}x {item.get('product_name', 'Unknown item')}" for item in items
            )
            return f"Order #{order_number} contains: {listed}."
        return f"No item details are recorded for order #{order_number}."

    status = order.get("status", "Unknown")
    return f"Order #{order_number} is currently **{status}**. {STATUS_EXPLANATIONS.get(status, '')}".strip()
