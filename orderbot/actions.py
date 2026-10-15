"""Human-reviewed order support requests; this module never changes order state."""

import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .config import order_support_client
from .privacy import persisted_message

logger = logging.getLogger(__name__)

CONFIRMATION_TTL = timedelta(minutes=5)


def stage_action(
    action: str,
    order_id: int | None,
    reason: str,
    conversation_id: str | None,
    owner_user_id: str | None = None,
    handoff_context: dict | None = None,
) -> dict:
    """Create a short-lived in-session confirmation payload; performs no write."""
    now = datetime.now(timezone.utc)
    return {
        "request_key": str(uuid4()),
        "action": action,
        "order_id": int(order_id) if order_id is not None else None,
        "reason": persisted_message("user", (reason or "Customer request").strip())[:300],
        "conversation_id": conversation_id,
        "owner_user_id": owner_user_id,
        "request_details": handoff_context or {},
        "created_at": now,
        "expires_at": now + CONFIRMATION_TTL,
    }


def submit_confirmed_action(pending: dict) -> dict | None:
    """Persist a confirmed support request idempotently; never mutates an order."""
    if pending["expires_at"] <= datetime.now(timezone.utc) or not pending.get("owner_user_id"):
        return None

    row = {
        "request_key": pending["request_key"],
        "action_type": pending["action"],
        "order_id": pending["order_id"],
        "reason": pending["reason"],
        "conversation_id": pending.get("conversation_id"),
        "owner_user_id": pending.get("owner_user_id"),
        "request_details": pending.get("request_details") or {},
        "status": "submitted",
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        response = order_support_client().table("action_requests").insert(row).execute()
        data = response.data or []
        if data:
            return data[0]
    except Exception as exc:
        logger.error("Action request submission failed (%s)", type(exc).__name__)

    try:
        response = (
            order_support_client()
            .table("action_requests")
            .select("id, request_key, action_type, order_id, status, confirmed_at")
            .eq("request_key", pending["request_key"])
            .limit(1)
            .execute()
        )
        rows = response.data or []
        return rows[0] if rows else None
    except Exception as exc:
        logger.error("Action request confirmation lookup failed (%s)", type(exc).__name__)
        return None


def list_support_cases(status_filter: str = "active", limit: int = 100) -> list[dict] | None:
    """Load a bounded staff queue; the caller must already have staff auth."""
    if status_filter not in {"active", "closed", "all"}:
        return None
    try:
        client = order_support_client()
        query = (
            client
            .table("action_requests")
            .select(
                "id, action_type, order_id, reason, request_details, status, priority, assigned_staff_user_id, "
                "confirmed_at, created_at"
            )
        )
        if status_filter == "active":
            query = query.in_("status", ["submitted", "reviewed"])
        elif status_filter == "closed":
            query = query.in_("status", ["completed", "rejected", "failed"])
        response = query.order("created_at", desc=True).limit(max(1, min(int(limit), 200))).execute()
        cases = response.data or []
        case_ids = [case["id"] for case in cases]
        if not case_ids:
            return []
        updates_response = (
            client.table("action_request_reviews")
            .select("request_id, previous_status, new_status, note, created_at")
            .in_("request_id", case_ids)
            .order("created_at", desc=True)
            .execute()
        )
        updates_by_case: dict[int, list[dict]] = {}
        for update in updates_response.data or []:
            updates_by_case.setdefault(update["request_id"], []).append(update)
        for case in cases:
            case["updates"] = updates_by_case.get(case["id"], [])
        return cases
    except Exception as exc:
        logger.error("Support request queue load failed (%s)", type(exc).__name__)
        return None


def update_support_case_routing(
    request_id: int,
    staff_user_id: str,
    *,
    assign_to_me: bool | None = None,
    priority: str | None = None,
) -> bool:
    """Self-assign/release a case and/or update its priority through a guarded RPC."""
    if assign_to_me is None and priority is None:
        return False
    if priority is not None and priority not in {"low", "normal", "high", "urgent"}:
        return False
    try:
        response = order_support_client().rpc(
            "update_support_case_routing",
            {
                "p_request_id": int(request_id),
                "p_staff_user_id": staff_user_id,
                "p_assign_to_me": assign_to_me,
                "p_priority": priority,
            },
        ).execute()
        return bool(response.data)
    except Exception as exc:
        logger.error("Support case routing update failed (%s)", type(exc).__name__)
        return False


def list_customer_action_requests(owner_user_id: str, limit: int = 50) -> list[dict] | None:
    """List requests owned by one authenticated customer, with staff updates."""
    if not owner_user_id:
        return None
    try:
        client = order_support_client()
        response = (
            client.table("action_requests")
            .select("id, action_type, order_id, reason, request_details, status, created_at, confirmed_at")
            .eq("owner_user_id", owner_user_id)
            .order("created_at", desc=True)
            .limit(max(1, min(int(limit), 100)))
            .execute()
        )
        requests = response.data or []
        request_ids = [request["id"] for request in requests]
        if not request_ids:
            return []
        updates_response = (
            client.table("action_request_reviews")
            .select("request_id, new_status, note, created_at")
            .in_("request_id", request_ids)
            .order("created_at", desc=True)
            .execute()
        )
        updates_by_request: dict[int, list[dict]] = {}
        for update in updates_response.data or []:
            updates_by_request.setdefault(update["request_id"], []).append(update)
        for request in requests:
            request["updates"] = updates_by_request.get(request["id"], [])
        return requests
    except Exception as exc:
        logger.error("Customer support request history load failed (%s)", type(exc).__name__)
        return None


def create_new_order_request(
    owner_user_id: str,
    product_id: int,
    quantity: int,
    notes: str = "",
) -> dict | None:
    """Request a catalog item; this does not create or charge an order."""
    details = (notes or "").strip()
    try:
        normalized_product_id = int(product_id)
        normalized_quantity = int(quantity)
    except (TypeError, ValueError):
        return None
    if (
        not owner_user_id
        or normalized_product_id < 1
        or not 1 <= normalized_quantity <= 10
        or len(details) > 500
    ):
        return None

    try:
        product_response = (
            order_support_client()
            .table("product_catalog")
            .select("id, sku, name")
            .eq("id", normalized_product_id)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        products = product_response.data or []
        if not products:
            return None
        product = products[0]
    except Exception as exc:
        logger.error("Product catalog verification failed (%s)", type(exc).__name__)
        return None

    item = product["name"]
    request_key = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "request_key": request_key,
        "action_type": "new_order_request",
        "order_id": None,
        "requested_product_id": normalized_product_id,
        "reason": "Customer request to arrange a new order",
        "request_details": {
            "handoff_summary": f"Customer asks support to arrange {normalized_quantity} × {item}.",
            "item_description": item,
            "product_sku": product.get("sku"),
            "quantity": normalized_quantity,
            "notes": persisted_message("user", details) if details else "",
        },
        "owner_user_id": owner_user_id,
        "status": "submitted",
        "confirmed_at": now,
    }
    try:
        response = order_support_client().table("action_requests").insert(row).execute()
        rows = response.data or []
        return rows[0] if rows else None
    except Exception as exc:
        logger.error("New-order support request submission failed (%s)", type(exc).__name__)
        return None


def get_action_request_status(order_id: int) -> dict | None:
    """Return the latest minimal support-request status for a verified order."""
    try:
        response = (
            order_support_client()
            .table("action_requests")
            .select("action_type, status, created_at")
            .eq("order_id", int(order_id))
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        return rows[0] if rows else {}
    except Exception as exc:
        logger.error("Support request status lookup failed (%s)", type(exc).__name__)
        return None


def review_action_request(request_id: int, staff_user_id: str, status: str, note: str = "") -> bool:
    """Atomically record a staff review; the DB RPC rechecks active staff role."""
    if status not in {"reviewed", "rejected"} or len((note or "").strip()) > 500:
        return False
    try:
        response = order_support_client().rpc(
            "review_action_request",
            {
                "p_request_id": int(request_id),
                "p_staff_user_id": staff_user_id,
                "p_new_status": status,
                "p_note": (note or "").strip() or None,
            },
        ).execute()
        return bool(response.data)
    except Exception as exc:
        logger.error("Support request review failed (%s)", type(exc).__name__)
        return False
