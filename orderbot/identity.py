import re

from .config import MAX_ORDER_ID, MIN_ORDER_ID
from .database import normalize_phone

# An order-number reference whose value falls outside the supported 1..1000
# range. Still a lookup attempt, so it deserves a grounded "not found".
_OUT_OF_RANGE_ORDER_RE = re.compile(r"(?:\border\s*(?:id|number)?\s*[:#]?\s*|#)\d+", re.IGNORECASE)


def extract_info_from_query(query):
    info = {"order_ids": [], "emails": [], "phones": [], "names": []}

    order_patterns = [
        # Broad opener: "order 42", "order number 42", "order id 42", "order: 42",
        # "order #42", "order42", plus bare "number 4", "no 4", "no. 4".
        # (\d{1,4} is range-bounded; anything above 1000 is filtered below.)
        r"\b(?:order\s*(?:number|id)?|number|no\.?)\s*(?:is|:)?\s*[:#]?\s*(\d{1,4})\b",
        # "it's 4", "it is 4", "its 4"
        r"\b(?:it'?s|it is)\s+(\d{1,4})\b",
        # "order 42", "order#42", "order :42", "order #:42" (multi-separator forms)
        r"order[:\s#]+(\d+)",
        # "#42"
        r"#(\d+)",
        # Any standalone 4+ digit number (range-filtered below to 1..1000)
        r"\b(\d{4,})\b",
    ]

    for pattern in order_patterns:
        matches = re.findall(pattern, query, re.IGNORECASE)
        for match in matches:
            try:
                order_id = int(match)
                if MIN_ORDER_ID <= order_id <= MAX_ORDER_ID and str(order_id) not in info["order_ids"]:
                    info["order_ids"].append(str(order_id))
            except ValueError:
                continue

    email_pattern = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
    info["emails"] = re.findall(email_pattern, query)

    phone_patterns = [
        r"\b\d{10}\b",  # 1234567890
        r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b",  # 123-456-7890, 123.456.7890, 123 456 7890
    ]

    for pattern in phone_patterns:
        matches = re.findall(pattern, query)
        for phone in matches:
            clean_phone = normalize_phone(phone)
            if len(clean_phone) == 10 and clean_phone.isdigit():
                info["phones"].append(clean_phone)

    # Name captures are word-capped (max three words) so surrounding prose can
    # never be swallowed into an identity value, and delimiter-anchored so a
    # bare "name …" mention doesn't grab whatever follows it.
    name_word = r"([A-Za-z]+(?:\s+[A-Za-z]+){0,2})"
    # Words that end a name rather than belong to it. "im gaurang and my order
    # number is 13" captured "gaurang and my" before this existed, which matched
    # no customer and read as a wrong name to the verification gate.
    name_stopwords = {
        "a",
        "about",
        "am",
        "an",
        "and",
        "any",
        "anybody",
        "anyone",
        "anything",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "can",
        "could",
        "details",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "here",
        "him",
        "his",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "just",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "please",
        "she",
        "should",
        "so",
        "some",
        "status",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
    # Hard terminators: a name never continues past these, so capture stops
    # rather than relying on the word cap. "im gaurang and my order number is 13"
    # yields "gaurang" with no stopword trimming needed.
    name_terminator = r"(?=\s+(?:order|number|no\b|#|email|phone|mobile|contact|call|address|and\b|the\b|for\b|with\b|please\b|can\b|could\b|do\b|does\b|check\b|track\b|help\b)|[,.;!?]|$)"

    name_patterns = [
        rf"\b(?:full\s+)?name\b\s*(?::|\bis\b)\s*{name_word}{name_terminator}",  # "name: Jane Doe"
        rf"\bcustomer\b\s*(?::|\bis\b)\s*{name_word}{name_terminator}",  # "customer: John Doe"
        # Introductory forms are the weakest anchor, so they stop at the first
        # terminator rather than at the three-word cap.
        rf"(?:my name is|my name's|i'?m|i am|this is)\s+{name_word}{name_terminator}",
        rf"{name_word}\s+is my name",  # "Jane Doe is my name"
        # Prose bridge: "the name on it should be gaurang", "name was wrong: it's Ana"
        rf"\bname\b[^.!?\n]{{0,30}}?\b(?:should\s+be|is|was)\s*{name_word}{name_terminator}",
    ]

    for pattern in name_patterns:
        matches = re.findall(pattern, query, re.IGNORECASE)
        for name in matches:
            words = re.sub(r"^(?:my|i|is|am|are)\s+", "", name.strip(), flags=re.IGNORECASE).split()
            # Drop trailing function words, but never reduce a real name to
            # nothing: a single-word name like "Madonna" must survive.
            while len(words) > 1 and words[-1].lower() in name_stopwords:
                words.pop()
            clean_name = " ".join(words)
            if len(clean_name) > 2 and not any(c.isdigit() for c in clean_name):
                info["names"].append(clean_name)

    # Patterns intentionally overlap (e.g. "name is X" hits two rules);
    # dedupe case-insensitively while preserving order.
    seen = set()
    unique_names = []
    for name in info["names"]:
        key = name.lower()
        if key not in seen:
            seen.add(key)
            unique_names.append(name)
    info["names"] = unique_names

    return info


def count_lookup_fields(query, info: dict | None = None) -> int:
    """Number of distinct identity field types present (order id, email, phone, name).

    Pass `info` from an earlier extract_info_from_query() call to skip the
    regex sweep when the message has already been parsed.
    """
    info = info if info is not None else extract_info_from_query(query)
    return sum(1 for values in info.values() if values)


def has_lookup_identifier(query: str, info: dict | None = None) -> bool:
    """Whether a message explicitly asks the database to identify an order/customer.

    Pass `info` from an earlier extract_info_from_query() call to skip the
    regex sweep. The raw text is still required for the out-of-range check.
    """
    info = info if info is not None else extract_info_from_query(query)
    if any(info.values()):
        return True
    # Treat an out-of-range order number as a lookup too, so it produces a
    # grounded "not found" response rather than a free-form model answer.
    return bool(_OUT_OF_RANGE_ORDER_RE.search(query))
