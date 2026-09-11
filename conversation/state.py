"""Explicit, session-scoped verification state."""

from dataclasses import dataclass
from time import monotonic


@dataclass
class VerificationState:
    order_id: str | None = None
    email: str | None = None
    phone: str | None = None
    name: str | None = None

    @property
    def complete(self) -> bool:
        return self.order_id is not None and self.has_secondary

    @property
    def has_secondary(self) -> bool:
        return bool(self.email or self.phone or self.name)

    @property
    def has_any(self) -> bool:
        return bool(self.order_id or self.email or self.phone or self.name)

    @property
    def name_only(self) -> bool:
        return bool(self.order_id and self.name and not self.email and not self.phone)

    def reset(self) -> None:
        self.order_id = self.email = self.phone = self.name = None

    def as_criteria(self) -> dict[str, str]:
        return {
            key: value
            for key, value in {
                "order_id": self.order_id,
                "email": self.email,
                "phone": self.phone,
                "name": self.name,
            }.items()
            if value
        }

    @classmethod
    def from_criteria(cls, criteria: dict[str, str]) -> "VerificationState":
        return cls(
            order_id=criteria.get("order_id"),
            email=criteria.get("email"),
            phone=criteria.get("phone"),
            name=criteria.get("name"),
        )

    def merge(self, extracted: dict[str, list[str]]) -> bool:
        """Merge parser output, resetting secondary fields for a new order."""
        changed = False
        if extracted["order_ids"]:
            order_id = extracted["order_ids"][0]
            if self.order_id != order_id:
                if self.order_id is not None:
                    self.reset()
                self.order_id = order_id
                changed = True
        for attribute, source_key in (("email", "emails"), ("phone", "phones"), ("name", "names")):
            values = extracted[source_key]
            if values and getattr(self, attribute) != values[0]:
                setattr(self, attribute, values[0])
                changed = True
        return changed


@dataclass
class VerificationAttemptGuard:
    """Session-local abuse guard; no customer data is retained."""

    max_failures: int = 5
    cooldown_seconds: int = 300
    failures: int = 0
    cooldown_until: float = 0.0

    @property
    def locked(self) -> bool:
        return monotonic() < self.cooldown_until

    @property
    def remaining_seconds(self) -> int:
        return max(0, int(self.cooldown_until - monotonic()))

    def record_failure(self) -> bool:
        self.failures += 1
        if self.failures < self.max_failures:
            return False
        self.cooldown_until = monotonic() + self.cooldown_seconds
        self.failures = 0
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.cooldown_until = 0.0
