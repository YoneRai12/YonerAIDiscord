"""Discordへ外部リンクを短い番号として安全に表示する共通helper。"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit


_MARKDOWN_LABEL = re.compile(r"([\\`*_{}\[\]()#+\-.!|>~])")


def numbered_link(index: int, url: str) -> str:
    """HTTP(S) URLを本文へ露出させず、``[1]``形式のリンクへする。"""

    if isinstance(index, bool) or not isinstance(index, int) or index <= 0:
        raise ValueError("link index must be a positive integer")
    safe_url = safe_http_url(url)
    return f"[{index}]({safe_url})"


def numbered_reference(index: int, url: str, label: str, *, maximum: int = 160) -> str:
    """番号リンクと、外部入力をMarkdownとして解釈しない短い説明を返す。"""

    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 500:
        raise ValueError("maximum must be between 1 and 500")
    normalized = " ".join(str(label).split())[:maximum]
    normalized = "".join(
        " " if unicodedata.category(character).startswith("C") else character for character in normalized
    )
    normalized = normalized.replace("@", "＠")
    escaped_label = _MARKDOWN_LABEL.sub(r"\\\1", normalized).strip() or f"出典 {index}"
    return f"{numbered_link(index, url)} {escaped_label}"


def safe_http_url(url: str) -> str:
    if not isinstance(url, str) or not url or len(url) > 2_048:
        raise ValueError("URL must be a non-empty bounded string")
    if any(unicodedata.category(character).startswith("C") for character in url):
        raise ValueError("URL must not contain control characters")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("URL must be an absolute public HTTP(S) URL")
    return url.replace("(", "%28").replace(")", "%29")


__all__ = ["numbered_link", "numbered_reference", "safe_http_url"]
