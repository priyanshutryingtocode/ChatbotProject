import re
from database import (
    get_order_by_id,
    get_orders_by_email,
    get_orders_by_phone,
    search_orders_by_name
)

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
                if str(order_id) not in info['order_ids']:
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
            clean_name = name.strip()
            if len(clean_name) > 2 and not any(c.isdigit() for c in clean_name):
                info['names'].append(clean_name)
    
    return info

def query_database(user_query):

    extracted_info = extract_info_from_query(user_query)
    results = {}
    
    for order_id in extracted_info['order_ids']:
        order_data = get_order_by_id(order_id)
        if order_data:
            results[f'order_{order_id}'] = order_data
    
    for email in extracted_info['emails']:
        orders = get_orders_by_email(email)
        if orders:
            results[f'orders_for_{email}'] = orders
    
    for phone in extracted_info['phones']:
        orders = get_orders_by_phone(phone)
        if orders:
            results[f'orders_for_phone_{phone}'] = orders
    
    for name in extracted_info['names']:
        orders = search_orders_by_name(name)
        if orders:
            results[f'orders_for_{name}'] = orders
    
    return results

def format_database_context(db_results):

    db_context = "\n\n=== DATABASE RESULTS ===\n"
    if db_results:
        for key, value in db_results.items():
            db_context += f"\n{key.upper()}:\n"
            if isinstance(value, list):
                for i, order in enumerate(value, 1):
                    
                    order_str = str(order).replace('{', '{{').replace('}', '}}')
                    db_context += f"Order {i}: {order_str}\n"
            else:
               
                value_str = str(value).replace('{', '{{').replace('}', '}}')
                db_context += f"{value_str}\n"
    else:
        db_context += "No matching orders found.\n"
    db_context += "=== END DATABASE RESULTS ===\n"
    return db_context