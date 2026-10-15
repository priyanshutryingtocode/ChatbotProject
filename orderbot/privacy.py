"""Minimize personal data copied into support-request reasons."""

import re
from .identity import extract_info_from_query

VERIFICATION_PLACEHOLDER = "[Verification details redacted]"

ORDER_REPLY_PLACEHOLDER = "[Order details redacted]"

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d().\s-]{7,}\d)(?!\w)")

_ORDER_REFERENCE_RE = re.compile(r"(?:\border\s*(?:id|number)?\s*[:#]?\s*|#)\d+", re.IGNORECASE)

_LEGACY_TRANSCRIPT_PLACEHOLDERS = {
    VERIFICATION_PLACEHOLDER,
    ORDER_REPLY_PLACEHOLDER,
    "[Historical message redacted]",
}


def is_legacy_transcript_placeholder(content: str) -> bool:
    """Identify whole-message placeholders saved by older transcript redaction."""
    return (content or "").strip() in _LEGACY_TRANSCRIPT_PLACEHOLDERS


def persisted_message(role: str, content: str, order_context: bool = False) -> str:
    """Return a transcript-safe message while keeping public conversation text."""
    if order_context and role == "assistant":
        return ORDER_REPLY_PLACEHOLDER

    if role == "assistant":
        extracted = extract_info_from_query(content)
        if extracted["order_ids"] or extracted["names"] or _ORDER_REFERENCE_RE.search(content):
            return ORDER_REPLY_PLACEHOLDER

    if role == "user":
        if order_context:
            return VERIFICATION_PLACEHOLDER
        extracted = extract_info_from_query(content)
        if extracted["order_ids"] or extracted["names"] or _ORDER_REFERENCE_RE.search(content):
            return VERIFICATION_PLACEHOLDER

    redacted = _EMAIL_RE.sub("[redacted email]", content)
    return _PHONE_RE.sub("[redacted phone]", redacted)
