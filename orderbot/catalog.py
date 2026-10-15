"""Read services for the customer-facing product catalog."""

import logging
from .config import order_support_client

logger = logging.getLogger(__name__)


def list_active_products() -> list[dict] | None:
    """Return a bounded active catalog, or None if its source is unavailable."""
    try:
        response = (
            order_support_client()
            .table("product_catalog")
            .select("id, sku, name, description, category, unit_price, currency")
            .eq("is_active", True)
            .order("name")
            .limit(500)
            .execute()
        )
        return response.data or []
    except Exception as exc:
        logger.error("Product catalog lookup failed (%s)", type(exc).__name__)
        return None
