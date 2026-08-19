"""Queries against the normalized customer/order schema.

This module is used by the server-side Streamlit process. Do not import it into
browser code or expose the service-role key it uses.
"""

import logging
import re
from datetime import datetime, timezone

from setup import supabase_server_client

logger = logging.getLogger(__name__)
MAX_ORDERS_PER_LOOKUP = 20

ORDER_SELECT = """
    public_order_id, status, priority, payment_status, currency,
    subtotal, shipping_fee, tax_amount, total_amount, special_instructions,
    ordered_at, cancelled_at,
    customers!inner(full_name, email, phone),
    order_items(product_sku, product_name, quantity, unit_price, line_total),
    shipments(carrier, tracking_number, delivery_driver_name, estimated_delivery_at, delivered_at, delivery_time_slot),
    order_events(event_type, event_at, message)
"""


def format_order_number(order_id: int | str | None) -> str:
    """Display numeric public order IDs consistently as 0001 through 1000."""
    try:
        return f"{int(order_id):04d}"
    except (TypeError, ValueError):
        return "N/A"


def format_timestamp(timestamp: str | None) -> str:
    """Turn ISO 8601 timestamps from Supabase into a readable UTC label."""
    if not timestamp:
        return "Not available"
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        hour = parsed.strftime("%I").lstrip("0") or "0"
        return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year} at {hour}:{parsed.strftime('%M %p')} UTC"
    except (TypeError, ValueError):
        return timestamp


def _client():
    return supabase_server_client()


def _orders_for_customer_ids(customer_ids: list[str]) -> list[dict]:
    if not customer_ids:
        return []
    response = (
        _client().table("orders")
        .select(ORDER_SELECT)
        .in_("customer_id", customer_ids)
        .order("ordered_at", desc=True)
        .limit(MAX_ORDERS_PER_LOOKUP)
        .execute()
    )
    return response.data or []


def get_order_by_id(order_id: int | str) -> dict | None:
    try:
        response = (
            _client().table("orders")
            .select(ORDER_SELECT)
            .eq("public_order_id", int(order_id))
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None
    except (TypeError, ValueError):
        return None
    except Exception:
        logger.exception("Database error in get_order_by_id")
        return None


def get_orders_by_email(email: str) -> list[dict]:
    try:
        customers = _client().table("customers").select("id").eq("email", email.strip().lower()).execute().data or []
        return _orders_for_customer_ids([customer["id"] for customer in customers])
    except Exception:
        logger.exception("Database error in get_orders_by_email")
        return []


def get_orders_by_phone(phone: str) -> list[dict]:
    try:
        normalized_phone = re.sub(r"[-.\s+()\[\]]", "", phone)
        customers = _client().table("customers").select("id").eq("phone", normalized_phone).execute().data or []
        return _orders_for_customer_ids([customer["id"] for customer in customers])
    except Exception:
        logger.exception("Database error in get_orders_by_phone")
        return []


def search_orders_by_name(name: str) -> list[dict]:
    try:
        customers = (
            _client().table("customers")
            .select("id")
            .ilike("full_name", f"%{name.strip()}%")
            .limit(MAX_ORDERS_PER_LOOKUP)
            .execute()
            .data
            or []
        )
        return _orders_for_customer_ids([customer["id"] for customer in customers])
    except Exception:
        logger.exception("Database error in search_orders_by_name")
        return []


def format_order_for_display(order: dict | None) -> str:
    if not order:
        return "No order data available"

    customer = order.get("customers") or {}
    shipment = (order.get("shipments") or [{}])[0]
    items = order.get("order_items") or []
    event_history = sorted(order.get("order_events") or [], key=lambda event: event.get("event_at") or "", reverse=True)

    lines = [
        f"**Order #{format_order_number(order.get('public_order_id'))}**",
        f"Status: {order.get('status', 'N/A')}",
        f"Customer: {customer.get('full_name', 'N/A')}",
        f"Ordered: {format_timestamp(order.get('ordered_at'))}",
        f"Payment: {order.get('payment_status', 'N/A')}",
        f"Total: {order.get('currency', 'USD')} {float(order.get('total_amount') or 0):.2f}",
    ]
    if customer.get("email"):
        lines.append(f"Email: {customer['email']}")
    if customer.get("phone"):
        lines.append(f"Phone: {customer['phone']}")
    if items:
        lines.append("Items: " + ", ".join(f"{item['quantity']}× {item['product_name']}" for item in items))
    if shipment.get("tracking_number"):
        lines.append(f"Tracking: {shipment['tracking_number']} ({shipment.get('carrier', 'N/A')})")
    if shipment.get("estimated_delivery_at"):
        lines.append(f"Estimated Delivery: {format_timestamp(shipment['estimated_delivery_at'])}")
    if shipment.get("delivered_at"):
        lines.append(f"Delivered: {format_timestamp(shipment['delivered_at'])}")
    if event_history:
        latest = event_history[0]
        lines.append(f"Latest Update: {latest.get('event_type', 'N/A')} — {format_timestamp(latest.get('event_at'))}")
    return "  \n".join(lines)
