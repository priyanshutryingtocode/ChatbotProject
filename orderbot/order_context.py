import re
from .database import format_timestamp, related_one


def format_database_context(db_results, query=None, fields=None):
    """Serialize only the relevant, labelled database values for the model.

    The order number and status are always included so answers stay grounded.
    When a query or a tool-requested field set is given, only those additional
    details are shared; otherwise the full order summary is returned.
    """
    if fields is not None:
        selected = normalize_field_keys(fields)
        if not selected:
            selected = None
    elif query is not None:
        selected = context_fields_for_query(query)
    else:
        selected = None

    lines = ["=== DATABASE RESULTS (DATA ONLY) ==="]
    for order in db_results.get("matched") or []:
        field_lines = _context_field_lines(order)
        if selected is None:
            keys = [key for key in CONTEXT_FIELD_KEYS if key in field_lines]
        else:
            keys = [
                key
                for key in CONTEXT_FIELD_KEYS
                if key in field_lines and (key in CONTEXT_BASELINE_FIELDS or key in selected)
            ]
        for key in keys:
            lines.append(field_lines[key])
        lines.append("---")
    lines.append("=== END DATABASE RESULTS ===")
    return "\n".join(lines)


CONTEXT_FIELD_KEYS = ("order", "status", "payment", "customer", "tracking", "carrier", "delivery", "driver", "items")

CONTEXT_BASELINE_FIELDS = {"order", "status"}

FIELD_ALIASES = {
    "order": "order",
    "order_number": "order",
    "order-id": "order",
    "status": "status",
    "payment": "payment",
    "payment_status": "payment",
    "total": "payment",
    "customer": "customer",
    "customer_name": "customer",
    "name": "customer",
    "tracking": "tracking",
    "tracking_number": "tracking",
    "carrier": "carrier",
    "delivery": "delivery",
    "estimated_delivery": "delivery",
    "delivered_at": "delivery",
    "driver": "driver",
    "delivery_driver": "driver",
    "delivery_driver_name": "driver",
    "items": "items",
    "products": "items",
    "product": "items",
}

INTENT_MAP = [
    (re.compile(r"\b(?:delivery\s*driver|driver|drivers|who\s*is\s*delivering)\b"), {"driver"}),
    (
        re.compile(r"\b(?:tracking|track|tracking\s*number|courier|carrier|shipped\s*by|shipping\s*company)\b"),
        {"tracking", "carrier"},
    ),
    (
        re.compile(r"\b(?:when|arrive|arrival|eta|expected|deliver|delivered|delivery\s*(?:date|time|estimate))\b"),
        {"delivery"},
    ),
    (re.compile(r"\b(?:payment|paid|refund|refunded|charge|charged|total|amount|cost|price)\b"), {"payment"}),
    (re.compile(r"\b(?:status|where\s*is|where's|progress)\b"), {"status"}),
    (re.compile(r"\b(?:item|items|product|products|bought|purchase|contain|contains|include|included)\b"), {"items"}),
    (re.compile(r"\b(?:name|customer)\b"), {"customer"}),
]


def context_fields_for_query(query):
    """Context field keys relevant to a user query, or None when no intent matched.

    None means no filtering (send the full context). A set result is the union
    of all matched intent groups, applied on top of the baseline fields.
    """
    if not query:
        return None
    lowered = query.lower()
    matched = set()
    for pattern, fields in INTENT_MAP:
        if pattern.search(lowered):
            matched |= fields
    return matched if matched else None


def normalize_field_keys(fields):
    """Map arbitrary requested field names to canonical context keys, dropping unknown ones."""
    if not fields:
        return set()
    normalized = set()
    for field in fields:
        key = FIELD_ALIASES.get(str(field).strip().lower())
        if key:
            normalized.add(key)
    return normalized


def _context_field_lines(order):
    customer = related_one(order.get("customers"))
    shipment = related_one(order.get("shipments"))
    items = order.get("order_items") or []
    delivery_lines = []
    if shipment.get("estimated_delivery_at"):
        delivery_lines.append(f"Estimated delivery: {format_timestamp(shipment['estimated_delivery_at'])}")
    if shipment.get("delivered_at"):
        delivery_lines.append(f"Delivered at: {format_timestamp(shipment['delivered_at'])}")
    if not delivery_lines:
        delivery_lines.append("Delivery estimate: Not available")
    return {
        "order": f"Order number: {order.get('public_order_id')}",
        "status": f"Status: {order.get('status')}",
        "payment": f"Payment status: {order.get('payment_status')}",
        "customer": f"Customer name: {customer.get('full_name')}",
        "tracking": f"Tracking number: {shipment.get('tracking_number') or 'Not available'}",
        "carrier": f"Carrier: {shipment.get('carrier') or 'Not available'}",
        "delivery": "\n".join(delivery_lines),
        "driver": f"Delivery driver: {shipment.get('delivery_driver_name') or 'Not available'}",
        "items": "Items: " + ", ".join(item.get("product_name", "Unknown item") for item in items),
    }
