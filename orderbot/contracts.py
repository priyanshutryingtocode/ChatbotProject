"""Typed application contracts shared by services and tool adapters.

These types describe outcomes and pending workflows. They do not execute
database writes or grant authorization.
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

OrderLookupStatus = Literal[
    "found",
    "verification_failed",
    "not_found",
    "need_more_info",
    "invalid_order_id",
    "too_many_attempts",
    "unavailable",
]

PolicySearchStatus = Literal["found", "no_match", "unavailable"]


@dataclass(frozen=True)
class OrderCriteria:
    order_id: int | str | None = None
    email: str = ""
    phone: str = ""
    name: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "OrderCriteria":
        return cls(
            order_id=value.get("order_id"),
            email=value.get("email", ""),
            phone=value.get("phone", ""),
            name=value.get("name", value.get("customer_name", "")),
        )


@dataclass(frozen=True)
class OrderLookup:
    status: OrderLookupStatus
    orders: list[dict[str, Any]] = field(default_factory=list)
    name_only: bool = False
    criteria: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PolicyChunk:
    source_title: str
    content: str
    heading: str | None = None
    similarity: float | None = None


@dataclass(frozen=True)
class PolicySearchResult:
    status: PolicySearchStatus
    chunks: list[PolicyChunk] = field(default_factory=list)
