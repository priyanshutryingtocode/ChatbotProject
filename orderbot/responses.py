"""Turning database rows into customer-facing text.

Order facts are rendered deterministically here rather than by the language
model, so a reply can never invent a status, a date, or a tracking number. The
functions take the verified order data as arguments; they hold no state.
"""

import re
import time

from .config import STATUS_EXPLANATIONS
from .database import format_order_for_display, format_timestamp, order_summary, related_one
from .routing import ORDER_REFUND_STATUS_RE

# --- follow-up intent classification ---------------------------------------
#
# Matching on substrings ("driver" in message) misreads identity questions as
# timing questions: "who delivered it" contains "deliver", so it fell through to
# the date branch and answered with a timestamp. Two rules prevent that:
#
#   * An identity question (who/which/what + a delivery topic) is a driver
#     question, and is checked before the date branch.
#   * The date branch excludes wh-words, so no identity question can reach it.
#
# Patterns are word-anchored because substring matching also fired on "when" in
# "whenever" and "paid" in "unpaid". Order is most-specific first: the first
# match wins, so a question naming both a field and a date ("what's the
# tracking number, and when does it arrive") is answered about tracking.

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
    # An identity question about delivery. The topic is optional because the
    # wh-word alone is enough once a verified order is in context: "by whom?"
    # names no delivery noun but can only mean the driver.
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
    # Order changes are not something this assistant can perform, so an explicit
    # change request is declined deterministically rather than answered with a
    # field. Checked before payment and date: "delivery" and "refund" both appear
    # in change requests and would otherwise be answered as a timing or payment
    # question ("return my refund" was answered "Payment status: Paid").
    # Still below the field intents, so a question that names one ("what's the
    # tracking number, I want to cancel") is answered about the field.
    #
    # A refund *request* is a change; a refund *status* question is not, which is
    # why a verb is required here — bare "refund" falls through to payment.
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
    # Explicit refund *status* wording, e.g. "what's my refund status". Listed
    # after the broad payment rule because both match it and the broad rule is
    # sufficient; kept so the intent set stays self-documenting.
    ("payment", re.compile(ORDER_REFUND_STATUS_RE)),
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

# Facts a name-only verification may never disclose.
_SENSITIVE_INTENTS = frozenset({"driver", "tracking", "payment", "items"})


def classify_intent(message: str) -> str | None:
    """Return the intent of a follow-up message, or None if unrecognised.

    Pure and stateless: no database, no model, no handler state.
    """
    for name, pattern in _INTENTS:
        if pattern.search(message):
            return name
    return None


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
        if name_only:
            # A common name can match several orders. Keep the same disclosure
            # limit as a single name-verified result; never dump full rows here.
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

    # Name-only verification is a lower-confidence check, so it discloses status
    # and delivery timing only. Keyed on the detected intent rather than a word
    # list, which previously missed phrasings like "which courier brought it".
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

    if intent == "date":
        delivered = shipment.get("delivered_at")
        estimated = shipment.get("estimated_delivery_at")
        if delivered:
            return f"Order #{order_number} was delivered on **{format_timestamp(delivered)}**."
        if estimated:
            return f"The estimated delivery for order #{order_number} is **{format_timestamp(estimated)}**."
        return f"There is no delivery estimate available yet for order #{order_number}."

    # "items": the row is not something the order-level facts can answer.
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
