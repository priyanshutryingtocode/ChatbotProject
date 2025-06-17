from setup import supabase_client

supabase = supabase_client()

def get_order_by_id(order_id):

    try:
        response = supabase.table('Order_Status').select('*').eq('order_id', int(order_id)).execute()
        return response.data[0] if response.data else None
    except Exception as e:
        print(f"Database error in get_order_by_id: {e}")
        return None

def get_orders_by_email(email):

    try:
        response = supabase.table('Order_Status').select('*').eq('customer_email', email).execute()
        return response.data if response.data else []
    except Exception as e:
        print(f"Database error in get_orders_by_email: {e}")
        return []

def get_orders_by_phone(phone):

    try:
        import re
        clean_phone = re.sub(r'[-.\s+()\[\]]', '', phone)
        response = supabase.table('Order_Status').select('*').eq('customer_phone', clean_phone).execute()
        return response.data if response.data else []
    except Exception as e:
        print(f"Database error in get_orders_by_phone: {e}")
        return []

def search_orders_by_name(name):

    try:
        response = supabase.table('Order_Status').select('*').ilike('customer_name', f'%{name}%').execute()
        return response.data if response.data else []
    except Exception as e:
        print(f"Database error in search_orders_by_name: {e}")
        return []

def format_order_for_display(order):

    if not order:
        return "No order data available"
    
    formatted = f"**Order #{order.get('order_id', 'N/A')}**\n"
    formatted += f"Status: {order.get('order_status', 'N/A')}\n"
    formatted += f"Customer: {order.get('customer_name', 'N/A')}\n"
    
    if order.get('customer_email'):
        formatted += f"Email: {order['customer_email']}\n"
    if order.get('customer_phone'):
        formatted += f"Phone: {order['customer_phone']}\n"
    if order.get('order_date'):
        formatted += f"Order Date: {order['order_date']}\n"
    if order.get('delivery_date'):
        formatted += f"Delivery Date: {order['delivery_date']}\n"
    if order.get('total_amount'):
        formatted += f"Total: ${float(order['total_amount']):.2f}\n"
    if order.get('items_ordered'):
        formatted += f"Items: {order['items_ordered']}\n"
    if order.get('payment_status'):
        formatted += f"Payment Status: {order['payment_status']}\n"
    if order.get('delivery_driver'):
        formatted += f"Delivery Driver: {order['delivery_driver']}\n"
    if order.get('estimated_delivery_time'):
        formatted += f"Estimated Delivery Time: {order['estimated_delivery_time']}\n"
    if order.get('actual_delivery_time'):
        formatted += f"Actual Delivery Time: {order['actual_delivery_time']}\n"
    
    
    return formatted