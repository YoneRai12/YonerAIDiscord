"""入力境界で共有する、値を保持しない秘密らしさの検査。"""

from __future__ import annotations

import re


_SECRET_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in (
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----",
        r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b",
        r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\bmfa\.[A-Za-z0-9_-]{20,}\b",
        r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{20,}\b",
        r"\b(?:Bearer|Bot)\s+[A-Za-z0-9._~+/=-]{16,}\b",
        r"(?:api[_ -]?key|client[_ -]?secret|access[_ -]?token|refresh[_ -]?token|"
        r"password|passwd|private[_ -]?key|discord[_ -]?token|\btoken\b)\s*[\"']?\s*[:=]\s*[\"']?[^\s\"']{8,}",
    )
)


def contains_secret_like(value: str) -> bool:
    return any(pattern.search(value) is not None for pattern in _SECRET_PATTERNS)


__all__ = ["contains_secret_like"]
