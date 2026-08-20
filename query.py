import re
from database import find_orders

def extract_info_from_query(query):

    info = {'order_ids': [], 'emails': [], 'phones': [], 'names': []}
    
    order_patterns = [
        r'\b(?:order\s*(?:number|id)?|number|no\.?)\s*(?:is|:)?\s*[:#]?\s*(\d{1,4})\b',  # "order number is 4", "number 4", "no 4", "order: 4"
        r"\b(?:it'?s|it is)\s+(\d{1,4})\b",  # "its 4", "it's 4", "it is 4"
        r'order[:\s#]+(\d+)',           # "order 123", "order: 123", "order #123"
        r'order\s*id[:\s#]*(\d+)',      # "order id 123", "order id: 123"
        r'#(\d+)',                      # "#123"
        r'order\s*number[:\s#]*(\d+)',  # "order number 123"
        r'\b(\d{4,})\b'                 # Any 4+ digit number (standalone)
    ]
    
    for pattern in order_patterns:
        matches = re.findall(pattern, query, re.IGNORECASE)
        for match in matches:
            try:
                order_id = int(match)
                if 1 <= order_id <= 1000 and str(order_id) not in info['order_ids']:
                    info['order_ids'].append(str(order_id))
            except ValueError:
                continue
    
    email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    info['emails'] = re.findall(email_pattern, query)

    phone_patterns = [
        r'\b\d{10}\b',                          # 1234567890
        r'\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b',     # 123-456-7890, 123.456.7890, 123 456 7890
    ]
    
    for pattern in phone_patterns:
        matches = re.findall(pattern, query)
        for phone in matches:
            clean_phone = re.sub(r'[-.\s+()\[\]]', '', phone)
            if clean_phone.isdigit() and len(clean_phone) >= 10:
                info['phones'].append(clean_phone[-10:])  
    
    name_patterns = [
        r'name[:\s]+([A-Za-z][A-Za-z\s]{1,30})',        # "name: John Doe"
        r'customer[:\s]+([A-Za-z][A-Za-z\s]{1,30})',     # "customer: John Doe"
        r"(?:my name is|my name's|i'?m|i am|this is) ([A-Za-z][A-Za-z\s]{1,30})",  # "my name is John Doe", "i'm John Doe"
        r'([A-Za-z][A-Za-z\s]{1,30})\s+is my name',     # "John Doe is my name"
    ]
    
    for pattern in name_patterns:
        matches = re.findall(pattern, query, re.IGNORECASE)
        for name in matches:
            clean_name = re.sub(r'^(?:my|i|is|am|are)\s+', '', name.strip(), flags=re.IGNORECASE)
            if len(clean_name) > 2 and not any(c.isdigit() for c in clean_name):
                info['names'].append(clean_name)
    
    return info


def count_lookup_fields(query: str) -> int:
    """Number of distinct identity field types present (order id, email, phone, name)."""
    info = extract_info_from_query(query)
    return sum(1 for values in info.values() if values)


def has_lookup_identifier(query: str) -> bool:
    """Whether a message explicitly asks the database to identify an order/customer."""
    info = extract_info_from_query(query)
    if any(info.values()):
        return True
    # Treat an out-of-range order number as a lookup too, so it produces a
    # grounded “not found” response rather than a free-form model answer.
    return bool(re.search(r"(?:\border\s*(?:id|number)?\s*[:#]?\s*|#)\d+", query, re.IGNORECASE))


def query_database(user_query):
    """Look up orders only when an order number and one more identity field are
    provided. Every supplied field must match the same order before anything
    is returned; insufficient or mismatched lookups return {}."""
    extracted_info = extract_info_from_query(user_query)

    if not extracted_info["order_ids"] or count_lookup_fields(user_query) < 2:
        return {}

    criteria = {"order_id": extracted_info["order_ids"][0]}
    if extracted_info["emails"]:
        criteria["email"] = extracted_info["emails"][0]
    if extracted_info["phones"]:
        criteria["phone"] = extracted_info["phones"][0]
    if extracted_info["names"]:
        criteria["name"] = extracted_info["names"][0]

    orders = find_orders(criteria)
    return {"matched": orders} if orders else {}

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
    for value in db_results.values():
        orders = value if isinstance(value, list) else [value]
        for order in orders:
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

# Public field names the lookup_order tool may request, aliased to canonical keys.
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

# Intent keywords -> relevant context fields. The union of all matched groups is
# applied on top of the always-on baseline.
INTENT_MAP = [
    (re.compile(r"\b(?:delivery\s*driver|driver|drivers|who\s*is\s*delivering)\b"), {"driver"}),
    (re.compile(r"\b(?:tracking|track|tracking\s*number|courier|carrier|shipped\s*by|shipping\s*company)\b"), {"tracking", "carrier"}),
    (re.compile(r"\b(?:when|arrive|arrival|eta|expected|deliver|delivered|delivery\s*(?:date|time|estimate))\b"), {"delivery"}),
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
    customer = order.get("customers") or {}
    shipment = (order.get("shipments") or [{}])[0]
    items = order.get("order_items") or []
    delivery_lines = []
    if shipment.get("estimated_delivery_at"):
        delivery_lines.append(f"Estimated delivery: {shipment['estimated_delivery_at']}")
    if shipment.get("delivered_at"):
        delivery_lines.append(f"Delivered at: {shipment['delivered_at']}")
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
