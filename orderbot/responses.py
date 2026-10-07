"""Turning database rows into customer-facing text.

Order facts are rendered deterministically here rather than by the language
model, so a reply can never invent a status, a date, or a tracking number. The
functions take the verified order data as arguments; they hold no state.
"""

import time

from .config import STATUS_EXPLANATIONS
from .database import format_order_for_display, format_order_number, format_timestamp
from .routing import ORDER_REFUND_STATUS_RE


def typewriter(text: str, word_delay: float = 0.006, max_animated_words: int = 60):
    """Fast typing variant: a brief effect for short deterministic replies;
    anything longer renders instantly."""
    words = text.split(" ")
    if len(words) > max_animated_words:
        yield text
        return
    for index, word in enumerate(words):
        yield word + (" " if index < len(words) - 1 else "")
        if index < len(words) - 1:
            time.sleep(word_delay)


def format_grounded_lookup(db_results: dict, name_only: bool = False) -> str:
    """Exact database-backed response for a new identity lookup."""
    orders = db_results.get("matched") or []
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

    order = orders[0]
    shipment = (order.get("shipments") or [{}])[0]
    message = user_input.lower()
    order_number = format_order_number(order.get("public_order_id"))

    if name_only and any(word in message for word in ("driver", "track", "payment", "paid", "refund", "item")):
        return (
            "For privacy, name-only verification can show order status and delivery timing only. "
            "Please provide the email address or phone number on the order for additional details."
        )

    if "driver" in message:
        driver = shipment.get("delivery_driver_name")
        if driver:
            return f"The delivery driver for order #{order_number} is **{driver}**."
        return f"No delivery driver has been assigned yet for order #{order_number}."
    # "track" already substring-matches "tracking" and "tracking number".
    if "track" in message:
        tracking = shipment.get("tracking_number")
        return (
            f"The tracking number for order #{order_number} is `{tracking}`."
            if tracking
            else f"Tracking information is not available yet for order #{order_number}."
        )
    if any(word in message for word in ("when", "arrive", "delivery", "deliver")):
        delivered = shipment.get("delivered_at")
        estimated = shipment.get("estimated_delivery_at")
        if delivered:
            return f"Order #{order_number} was delivered on **{format_timestamp(delivered)}**."
        if estimated:
            return f"The estimated delivery for order #{order_number} is **{format_timestamp(estimated)}**."
        return f"There is no delivery estimate available yet for order #{order_number}."
    if ORDER_REFUND_STATUS_RE.search(user_input):
        return f"Payment status for order #{order_number}: **{order.get('payment_status', 'Unknown')}**."
    if any(word in message for word in ("status", "where is", "where's")):
        status = order.get("status", "Unknown")
        return f"Order #{order_number} is currently **{status}**. {STATUS_EXPLANATIONS.get(status, '')}".strip()
    if any(word in message for word in ("payment", "paid", "refund")):
        return f"Payment status for order #{order_number}: **{order.get('payment_status', 'Unknown')}**."
    return None
