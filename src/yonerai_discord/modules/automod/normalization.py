from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)
JAPANESE_URL_PUNCTUATION = str.maketrans({"。": ".", "｡": "."})
DOT_OBFUSCATION = re.compile(r"\s*(?:\[\s*\.\s*\]|\(\s*\.\s*\)|\b(?:dot|ドット)\b)\s*", re.IGNORECASE)
SCHEME_OBFUSCATION = re.compile(r"\bhxxps?\b", re.IGNORECASE)
URL_PATTERN = re.compile(
    r"(?:(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,63})(?::\d{1,5})?(?:/[^\s<>]*)?",
    re.IGNORECASE,
)


def normalize_text(value: str, *, compact: bool = True) -> str:
    normalized = (
        unicodedata.normalize("NFKC", value).translate(ZERO_WIDTH).translate(JAPANESE_URL_PUNCTUATION).casefold()
    )
    normalized = DOT_OBFUSCATION.sub(".", normalized)
    normalized = SCHEME_OBFUSCATION.sub(
        lambda match: "https" if match.group(0).casefold().endswith("s") else "http", normalized
    )
    normalized = re.sub(r"\s*([:/\.])\s*", r"\1", normalized)
    normalized = re.sub(r"\s+", " " if not compact else "", normalized).strip()
    return normalized


def extract_domains(value: str) -> frozenset[str]:
    normalized = normalize_text(value, compact=False)
    domains: set[str] = set()
    for match in URL_PATTERN.finditer(normalized):
        candidate = match.group(0).rstrip(".,。、)）]】")
        parsed = urlsplit(candidate if "://" in candidate else f"https://{candidate}")
        if parsed.hostname:
            domains.add(parsed.hostname.rstrip(".").casefold())
    return frozenset(domains)


def domain_is_allowed(domain: str, allowed_domains: frozenset[str]) -> bool:
    domain = domain.casefold().rstrip(".")
    return any(
        domain == allowed or domain.endswith(f".{allowed}")
        for allowed in (item.casefold().rstrip(".") for item in allowed_domains)
    )
