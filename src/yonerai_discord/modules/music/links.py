"""音源を取得せず、固定された公式検索ページURLだけを組み立てる。"""

from __future__ import annotations

import unicodedata
from urllib.parse import quote_plus


_YOUTUBE_SEARCH_BASE = "https://www.youtube.com/results?search_query="
_MAX_QUERY_CHARS = 200


def youtube_search_url(query: str) -> str:
    """正規化済み検索語を公式YouTube検索URLへ安全にpercent encodeする。"""

    if not isinstance(query, str):
        raise ValueError("query is invalid")
    normalized = unicodedata.normalize("NFKC", query)
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise ValueError("query contains control characters")
    normalized = " ".join(normalized.split())
    if not 1 <= len(normalized) <= _MAX_QUERY_CHARS:
        raise ValueError("query is invalid")
    return _YOUTUBE_SEARCH_BASE + quote_plus(normalized, safe="")


__all__ = ["youtube_search_url"]
