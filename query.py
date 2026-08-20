import re
from database import find_orders

def extract_info_from_query(query):

    info = {'order_ids': [], 'emails': [], 'phones': [], 'names': []}
    
    order_patterns = [
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
        r'my name is ([A-Za-z][A-Za-z\s]{1,30})',        # "my name is John Doe"
        r'i am ([A-Za-z][A-Za-z\s]{1,30})',              # "i am John Doe"
        r'this is ([A-Za-z][A-Za-z\s]{1,30})',           # "this is John Doe"
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

def format_database_context(db_results):
    """Serialize only needed, labelled database values for the conversational model."""
    lines = ["=== DATABASE RESULTS (DATA ONLY) ==="]
    for value in db_results.values():
        orders = value if isinstance(value, list) else [value]
        for order in orders:
            customer = order.get("customers") or {}
            shipment = (order.get("shipments") or [{}])[0]
            items = order.get("order_items") or []
            lines.extend(
                [
                    f"Order number: {order.get('public_order_id')}",
                    f"Status: {order.get('status')}",
                    f"Payment status: {order.get('payment_status')}",
                    f"Customer name: {customer.get('full_name')}",
                    f"Tracking number: {shipment.get('tracking_number') or 'Not available'}",
                    f"Estimated delivery: {shipment.get('estimated_delivery_at') or 'Not available'}",
                    f"Delivered at: {shipment.get('delivered_at') or 'Not available'}",
                    "Items: " + ", ".join(item.get("product_name", "Unknown item") for item in items),
                    "---",
                ]
            )
    lines.append("=== END DATABASE RESULTS ===")
    return "\n".join(lines)
