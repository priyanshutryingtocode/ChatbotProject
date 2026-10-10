"""Human-reviewed order support requests; this module never changes order state."""

import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .config import supabase_server_client
from .privacy import persisted_message

logger = logging.getLogger(__name__)

CONFIRMATION_TTL = timedelta(minutes=5)


def stage_action(action: str, order_id: int, reason: str, conversation_id: str | None) -> dict:
    """Create a short-lived in-session confirmation payload; performs no write."""
    now = datetime.now(timezone.utc)
    return {
        "request_key": str(uuid4()),
        "action": action,
        "order_id": int(order_id),
        "reason": persisted_message("user", (reason or "Customer request").strip())[:300],
        "conversation_id": conversation_id,
        "created_at": now,
        "expires_at": now + CONFIRMATION_TTL,
    }


def submit_confirmed_action(pending: dict) -> dict | None:
    """Persist a confirmed support request idempotently; never mutates an order."""
    if pending["expires_at"] <= datetime.now(timezone.utc):
        return None

    row = {
        "request_key": pending["request_key"],
        "action_type": pending["action"],
        "order_id": pending["order_id"],
        "reason": pending["reason"],
        "conversation_id": pending.get("conversation_id"),
        "status": "submitted",
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        response = supabase_server_client().table("action_requests").insert(row).execute()
        data = response.data or []
        if data:
            return data[0]
    except Exception as exc:
        # Resolve retries after a lost response using the unique request key.
        # Do not log the submitted reason or database response.
        logger.error("Action request submission failed (%s)", type(exc).__name__)

    try:
        response = (
            supabase_server_client()
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
