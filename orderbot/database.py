"""Queries against the normalized customer/order schema.

This module is used by the server-side Streamlit process. Do not import it into
browser code or expose the service-role key it uses.
"""

import logging
import re
from datetime import datetime, timedelta, timezone

from .config import supabase_server_client

IST = timezone(timedelta(hours=5, minutes=30))
logger = logging.getLogger(__name__)
MAX_ORDERS_PER_LOOKUP = 20

# Only columns the app actually reads. Every extra column is paid for on each
# lookup, and a verified lookup can run up to four of these queries.
ORDER_SELECT = """
    public_order_id, status, payment_status, currency, total_amount, ordered_at,
    customers!inner(full_name, email, phone),
    order_items(product_name, quantity),
    shipments(carrier, tracking_number, delivery_driver_name, estimated_delivery_at, delivered_at),
    order_events(event_type, event_at, message)
"""


def format_order_number(order_id: int | str | None) -> str:
    """Display numeric public order IDs consistently as 0001 through 1000."""
    try:
        return f"{int(order_id):04d}"
    except (TypeError, ValueError):
        return "N/A"


def format_timestamp(timestamp: str | None) -> str:
    """Turn ISO 8601 timestamps from Supabase into a readable IST label."""
    if not timestamp:
        return "Not available"
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(IST)
        hour = parsed.strftime("%I").lstrip("0") or "0"
        return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year} at {hour}:{parsed.strftime('%M %p')} IST"
    except (TypeError, ValueError):
        return timestamp


def _client():
    return supabase_server_client()


def _orders_for_customer_ids(customer_ids: list[str]) -> list[dict]:
    if not customer_ids:
        return []
    response = (
        _client()
        .table("orders")
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
            _client().table("orders").select(ORDER_SELECT).eq("public_order_id", int(order_id)).limit(1).execute()
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


def normalize_phone(phone: str | None) -> str:
    """Normalize a phone for matching against stored 10-digit numbers.

    Strips separators and any leading country code by keeping only the last
    10 digits. Assumes stored numbers are bare 10-digit values.
    """
    digits = re.sub(r"[-.\s+()\[\]]", "", phone or "")
    return digits[-10:]


def get_orders_by_phone(phone: str) -> list[dict]:
    try:
        normalized_phone = normalize_phone(phone)
        customers = _client().table("customers").select("id").eq("phone", normalized_phone).execute().data or []
        return _orders_for_customer_ids([customer["id"] for customer in customers])
    except Exception:
        logger.exception("Database error in get_orders_by_phone")
        return []


def escape_like(value: str) -> str:
    """Strip SQL LIKE metacharacters so a name is matched literally.

    `ilike` is a SQL LIKE, so `%` and `_` are wildcards rather than literals.
    Interpolating a user-supplied name into the pattern unescaped turned
    "order 42 + name %" into "order 42 + any customer name", which defeats the
    two-field verification. The backslash is removed too, so no escaping
    sequence can survive to reintroduce a wildcard.
    """
    return re.sub(r"[\\%_]", "", value or "")


def search_orders_by_name(name: str) -> list[dict]:
    literal = escape_like(name).strip()
    if not literal:
        return []
    try:
        customers = (
            _client()
            .table("customers")
            .select("id")
            .ilike("full_name", f"%{literal}%")
            .limit(MAX_ORDERS_PER_LOOKUP)
            .execute()
            .data
            or []
        )
        return _orders_for_customer_ids([customer["id"] for customer in customers])
    except Exception:
        logger.exception("Database error in search_orders_by_name")
        return []


def build_criteria(order_id=None, email="", phone="", name="") -> dict:
    """Build a find_orders criteria dict, dropping blank values.

    find_orders normalises and strips its own input, so this only decides which
    keys are present.
    """
    criteria = {}
    if order_id is not None:
        criteria["order_id"] = order_id
    for key, value in (("email", email), ("phone", phone), ("name", name)):
        if value and str(value).strip():
            criteria[key] = value
    return criteria


def find_orders(criteria: dict, require_order_id: bool = True) -> list[dict]:
    """Return orders matching all provided identity fields.

    Requires an order ID plus at least one of email, phone, or name. An order
    is returned only when every provided field points to the same order, so a
    mismatched field never leaks partial results.

    Raises ValueError when the required identity fields are missing.
    """
    provided = {key: str(value).strip() if value is not None else "" for key, value in criteria.items()}
    provided = {key: value for key, value in provided.items() if value}

    if require_order_id and "order_id" not in provided:
        raise ValueError("An order number is required to look up an order.")
    if len(provided) < 2:
        raise ValueError("Two identity fields are required to look up an order.")

    result_sets: dict[str, list[dict]] = {}
    if "order_id" in provided:
        order = get_order_by_id(provided["order_id"])
        result_sets["order_id"] = [order] if order else []
    if "email" in provided:
        result_sets["email"] = get_orders_by_email(provided["email"])
    if "phone" in provided:
        result_sets["phone"] = get_orders_by_phone(provided["phone"])
    if "name" in provided:
        result_sets["name"] = search_orders_by_name(provided["name"])

    if any(not orders for orders in result_sets.values()):
        return []

    first_key = next(iter(result_sets))
    common_ids = {order["public_order_id"] for order in result_sets[first_key]}
    for orders in result_sets.values():
        common_ids &= {order["public_order_id"] for order in orders}
    if not common_ids:
        return []

    by_id = {order["public_order_id"]: order for order in result_sets[first_key]}
    return [by_id[order_id] for order_id in sorted(common_ids)]


def _event_sort_key(event: dict):
    """Sort key for order events by true chronological time.

    ISO 8601 strings sort correctly only when offsets are uniform, so parse to a
    timezone-aware datetime and fall back to a plain-string comparison otherwise.
    """
    value = event.get("event_at") or ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (0, parsed.timestamp())
    except (TypeError, ValueError):
        return (1, value)


def format_order_for_display(order: dict | None) -> str:
    if not order:
        return "No order data available"

    customer = order.get("customers") or {}
    shipment = (order.get("shipments") or [{}])[0]
    items = order.get("order_items") or []
    event_history = sorted(order.get("order_events") or [], key=_event_sort_key, reverse=True)

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
        lines.append("Items: " + ", ".join(f"{item['quantity']}x {item['product_name']}" for item in items))
    if shipment.get("tracking_number"):
        lines.append(f"Tracking: {shipment['tracking_number']} ({shipment.get('carrier', 'N/A')})")
    if shipment.get("delivery_driver_name"):
        lines.append(f"Driver: {shipment['delivery_driver_name']}")
    if shipment.get("estimated_delivery_at"):
        lines.append(f"Estimated Delivery: {format_timestamp(shipment['estimated_delivery_at'])}")
    if shipment.get("delivered_at"):
        lines.append(f"Delivered: {format_timestamp(shipment['delivered_at'])}")
    if event_history:
        latest = event_history[0]
        lines.append(f"Latest Update: {latest.get('event_type', 'N/A')} - {format_timestamp(latest.get('event_at'))}")
    return "  \n".join(lines)


def create_conversation(
    channel: str = "streamlit", customer_email: str | None = None, session_id: str | None = None
) -> str:
    """Create a conversation record and return its id. Best-effort, never raises.

    `session_id` scopes the conversation to the browser session that created it
    (migration 006). Without one, a conversation is stored unscoped and invisible
    to every session — the fail-closed outcome, since it matches no filter.
    """
    try:
        row = {"channel": channel}
        if customer_email:
            row["customer_email"] = customer_email
        if session_id:
            row["session_id"] = session_id
        response = _client().table("conversations").insert(row).execute()
        return (response.data or [{}])[0].get("id", "")
    except Exception:
        logger.exception("Failed to create conversation")
        return ""


def append_message(conversation_id: str, role: str, content: str, db_results: dict | None = None) -> str:
    """Store one chat message. Returns the new message id ("" if it failed)."""
    if not conversation_id:
        return ""
    try:
        row = {"conversation_id": conversation_id, "role": role, "content": content}
        if db_results:
            row["db_results"] = db_results
        response = _client().table("messages").insert(row).execute()
        return (response.data or [{}])[0].get("id", "")
    except Exception:
        logger.exception("Failed to append message")
        return ""


def get_messages(conversation_id: str, limit: int = 50) -> list[dict]:
    """Return stored messages for a conversation, oldest first."""
    if not conversation_id:
        return []
    try:
        response = (
            _client()
            .table("messages")
            .select("id, role, content, db_results, created_at")
            .eq("conversation_id", conversation_id)
            .order("created_at", desc=False)
            .limit(limit)
            .execute()
        )
        return response.data or []
    except Exception:
        logger.exception("Failed to load messages")
        return []


def end_conversation(conversation_id: str) -> None:
    """Mark a conversation as ended. Best-effort, never raises."""
    if not conversation_id:
        return
    try:
        _client().table("conversations").update({"ended_at": datetime.now(timezone.utc).isoformat()}).eq(
            "id", conversation_id
        ).execute()
    except Exception:
        logger.exception("Failed to end conversation")


def list_conversations(limit: int = 10, session_id: str | None = None) -> list[dict]:
    """Most recent conversations, newest first. Best-effort, never raises.

    Pass `session_id` to return only that browser session's conversations.
    Omitting it returns every conversation, which is only safe for callers that
    are not user-facing — the sidebar must always scope.
    """
    try:
        query = (
            _client()
            .table("conversations")
            .select("id, channel, customer_email, created_at, ended_at")
            .order("created_at", desc=True)
            .limit(limit)
        )
        if session_id:
            query = query.eq("session_id", session_id)
        return query.execute().data or []
    except Exception:
        logger.exception("Failed to list conversations")
        return []


def conversation_belongs_to_session(conversation_id: str, session_id: str | None) -> bool:
    """Whether a conversation was created by this browser session.

    Guards the `?chat=` deep link and the recent-chats radio. Returns False for
    an unscoped conversation (null session_id) so rows predating migration 006
    stay unreachable.
    """
    if not conversation_id or not session_id:
        return False
    try:
        response = (
            _client()
            .table("conversations")
            .select("id")
            .eq("id", conversation_id)
            .eq("session_id", session_id)
            .execute()
        )
        return bool(response.data)
    except Exception:
        logger.exception("Failed to check conversation ownership")
        return False


def get_order_timeline(order: dict | None) -> list[dict]:
    """Chronological order events (oldest first) for timeline rendering."""
    if not order:
        return []
    return sorted(order.get("order_events") or [], key=_event_sort_key)


def format_event_line(event: dict) -> str:
    """One human-readable timeline row for an order event."""
    event_type = (event.get("event_type") or "update").replace("_", " ").title()
    line = f"{event_type} - {format_timestamp(event.get('event_at'))}"
    message = event.get("message")
    return f"{line} - {message}" if message else line
