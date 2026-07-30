"""秘密値そのものを表示せず、用途別secretの最低強度だけを検証する。"""

from __future__ import annotations

import ipaddress
import math
import unicodedata
from collections import Counter
from urllib.parse import urlparse


_PLACEHOLDER_MARKERS = (
    "changeme",
    "change-me",
    "placeholder",
    "replace-me",
    "your-secret",
    "secret-here",
    "example-secret",
    "test-secret",
)


def strong_safety_identifier_secret(value: str) -> bool:
    """remote HMAC専用secretとしてUTF-8長・多様性・明白な仮値を検査する。"""

    if not isinstance(value, str) or not value:
        return False
    if len(value.encode("utf-8")) < 32:
        return False
    if any(unicodedata.category(character).startswith("C") for character in value):
        return False
    lowered = value.casefold()
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
        return False
    if len(set(value)) < 8 or _entropy_bits_per_character(value) < 3.0:
        return False
    for width in range(1, min(8, len(value) // 2) + 1):
        if len(value) % width == 0 and value == value[:width] * (len(value) // width):
            return False
    return True


def is_loopback_endpoint(value: str) -> bool:
    try:
        hostname = urlparse(value).hostname
    except ValueError:
        return False
    if hostname is None:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _entropy_bits_per_character(value: str) -> float:
    length = len(value)
    counts = Counter(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


__all__ = ["is_loopback_endpoint", "strong_safety_identifier_secret"]
