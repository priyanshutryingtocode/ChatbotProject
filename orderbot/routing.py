"""Vocabulary and heuristics for classifying an incoming message.

Pure data and pure predicates: nothing here touches the database, the model, or
any handler state. Keeping it apart from the orchestrator means the routing
rules can be read, tested, and tuned on their own.
"""

import re

FOLLOW_UP_WORDS = (
    "track",
    "tracking",
    "status",
    "where is",
    "where's",
    "when",
    "arrive",
    "delivery",
    "deliver",
    "payment",
    "paid",
    "refund",
)

FALLBACK_MESSAGE = "I'm sorry, there was an error processing your request. Please try again or contact support."

POLICY_WORDS = (
    "policy",
    "policies",
    "return",
    "returns",
    "refund",
    "refunded",
    "exchange",
    "warranty",
    "damaged",
    "damage",
    "cancellation",
    "shipping cost",
    "delivery charge",
    "delivery slot",
    "time slot",
)

POLICY_PHRASES = (
    re.compile(r"\bhow\s+(?:do|can)\s+i\s+cancel\b"),
    re.compile(r"\bcancel(?:\s+my|\s+the)?\s+order\b"),
    re.compile(r"\breturn\s+(?:my|the|an)?\s*(?:order|item)\b"),
)

ORDER_REFUND_STATUS_RE = re.compile(
    r"\b(?:refund\s+(?:status|is|was|has|been|pending|processed|received|arrived)|"
    r"(?:status|when|where)\s+(?:is\s+)?(?:my\s+)?refund|my\s+refund)\b",
    re.IGNORECASE,
)

ORDER_DAMAGE_RE = re.compile(
    r"\b(?:my|i)\b[^.!?\n]{0,40}?\b(?:order|item|items|parcel|package|delivery|box|product)s?\b"
    r"[^.!?\n]{0,40}?\b(?:damaged|damage|broken|crushed|defective|torn|spoiled|leaking)\b"
    r"|\b(?:my|i)\b[^.!?\n]{0,40}?"
    r"\b(?:arrived|arrives|came|come|received|receive|was|is|got|get|have|has)\s+(?:in\s+)?(?:a\s+)?"
    r"(?:damaged|broken|crushed|defective|torn)\b",
    re.IGNORECASE,
)

BARE_NAME_STOPWORDS = {
    "hello",
    "hi",
    "hey",
    "thanks",
    "thank",
    "thank you",
    "yes",
    "yep",
    "yeah",
    "no",
    "nope",
    "sure",
    "ok",
    "okay",
    "good",
    "great",
    "right",
    "alright",
    "see",
    "nothing",
    "never",
    "cancel",
    "stop",
    "done",
    "ok done",
}

BARE_NAME_QUESTION_WORDS = {
    "where",
    "what",
    "when",
    "how",
    "why",
    "who",
    "which",
    "can",
    "could",
    "would",
    "will",
    "shall",
    "is",
    "are",
    "do",
    "does",
    "did",
    "please",
    "wait",
    "hold",
    "let",
    "i",
    "my",
    "we",
    "you",
    "it",
    "there",
    "cancel",
}

BARE_CANCEL_RE = re.compile(r"^(?:ok[,\s]*)?(?:please\s+)?cancel(?:\s+(?:it|that|this))?\s*(?:please)?$")

BARE_ORDER_NUMBER_RE = re.compile(r"(?:order\s*(?:number|id|no)?\s*[:#]?\s*)?#?\s*(\d{1,4})\s*[.!]?")


def looks_like_bare_cancel(message_lower: str) -> bool:
    """Whether a message only aborts the current flow."""
    return bool(BARE_CANCEL_RE.fullmatch(message_lower.strip()))


def looks_like_bare_name(text: str) -> bool:
    """Whether a short, all-letter message is plausibly the customer's name."""
    candidate = text.strip().strip(".,;:!?")
    if not 2 <= len(candidate) <= 60:
        return False
    if not re.fullmatch(r"[A-Za-z][A-Za-z \-'\.]*", candidate):
        return False
    words = candidate.split()
    if not 1 <= len(words) <= 3:
        return False
    lowered = candidate.lower()
    if lowered in BARE_NAME_STOPWORDS or words[0].lower() in BARE_NAME_QUESTION_WORDS:
        return False
    return True


def is_policy_question(user_input: str) -> bool:
    """Whether a message is a public-policy / FAQ question."""
    message = user_input.lower()
    if any(word in message for word in POLICY_WORDS):
        return True
    return any(pattern.search(message) for pattern in POLICY_PHRASES)


def is_order_specific_question(user_input: str, has_verified_order: bool) -> bool:
    """Whether wording is about a verified order rather than public policy.

    Only meaningful once a lookup has succeeded, so callers pass that state in
    rather than this module reaching for it.
    """
    if not has_verified_order:
        return False
    return bool(ORDER_REFUND_STATUS_RE.search(user_input) or ORDER_DAMAGE_RE.search(user_input))
