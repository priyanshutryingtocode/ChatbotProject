import re
from .config import MAX_ORDER_ID, MIN_ORDER_ID
from .database import normalize_phone

_OUT_OF_RANGE_ORDER_RE = re.compile(r"(?:\border\s*(?:id|number)?\s*[:#]?\s*|#)\d+", re.IGNORECASE)


def extract_info_from_query(query):
    info = {"order_ids": [], "emails": [], "phones": [], "names": []}

    order_patterns = [
        r"\b(?:order\s*(?:number|id)?|number|no\.?)\s*(?:is|:)?\s*[:#]?\s*(\d{1,4})\b",
        r"\b(?:it'?s|it is)\s+(\d{1,4})\b",
        r"order[:\s#]+(\d+)",
        r"#(\d+)",
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
        r"\b\d{10}\b",
        r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b",
    ]

    for pattern in phone_patterns:
        matches = re.findall(pattern, query)
        for phone in matches:
            clean_phone = normalize_phone(phone)
            if len(clean_phone) == 10 and clean_phone.isdigit():
                info["phones"].append(clean_phone)

    name_word = r"([A-Za-z]+(?:\s+[A-Za-z]+){0,2})"
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
    name_terminator = r"(?=\s+(?:order|number|no\b|#|email|phone|mobile|contact|call|address|and\b|the\b|for\b|with\b|please\b|can\b|could\b|do\b|does\b|check\b|track\b|help\b)|[,.;!?]|$)"

    name_patterns = [
        rf"\b(?:full\s+)?name\b\s*(?::|\bis\b)\s*{name_word}{name_terminator}",
        rf"\bcustomer\b\s*(?::|\bis\b)\s*{name_word}{name_terminator}",
        rf"(?:my name is|my name's|i'?m|i am|this is)\s+{name_word}{name_terminator}",
        rf"{name_word}\s+is my name",
        rf"\bname\b[^.!?\n]{{0,30}}?\b(?:should\s+be|is|was)\s*{name_word}{name_terminator}",
    ]

    for pattern in name_patterns:
        matches = re.findall(pattern, query, re.IGNORECASE)
        for name in matches:
            words = re.sub(r"^(?:my|i|is|am|are)\s+", "", name.strip(), flags=re.IGNORECASE).split()
            while len(words) > 1 and words[-1].lower() in name_stopwords:
                words.pop()
            clean_name = " ".join(words)
            if len(clean_name) > 2 and not any(c.isdigit() for c in clean_name):
                info["names"].append(clean_name)

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
    return bool(_OUT_OF_RANGE_ORDER_RE.search(query))
