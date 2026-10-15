"""Application-level order lookup shared by chat, tools, and the sidebar."""

from typing import Any, Mapping
from .contracts import OrderCriteria, OrderLookup, OrderLookupStatus
from .database import DatabaseLookupError, build_criteria, find_orders, validate_order_id
from .state import VerificationState

LookupStatus = OrderLookupStatus


def lookup_order(criteria: OrderCriteria | Mapping[str, Any]) -> OrderLookup:
    """Validate verification criteria and return the matching order rows."""
    if not isinstance(criteria, OrderCriteria):
        criteria = OrderCriteria.from_mapping(criteria)
    criteria = build_criteria(
        criteria.order_id,
        criteria.email,
        criteria.phone,
        criteria.name,
    )
    if "order_id" not in criteria:
        return OrderLookup("need_more_info", criteria=criteria)
    try:
        criteria["order_id"] = validate_order_id(criteria["order_id"])
    except ValueError:
        return OrderLookup("invalid_order_id", criteria=criteria)
    if len(criteria) < 2:
        return OrderLookup("need_more_info", criteria=criteria)
    try:
        orders = find_orders(criteria)
    except ValueError:
        return OrderLookup("invalid_order_id", criteria=criteria)
    except DatabaseLookupError:
        return OrderLookup("unavailable", criteria=criteria)
    if not orders:
        return OrderLookup("not_found", criteria=criteria)
    return OrderLookup("found", orders, VerificationState.from_criteria(criteria).name_only, criteria)
