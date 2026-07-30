"""YouTube動画参照だけを受けるcanonical URL境界。"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .domain import MediaInspectionInputError


_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com"})
_SHORT_HOST = "youtu.be"
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def canonicalize_youtube_url(value: object) -> str:
    """追跡queryを破棄し、対応する公開HTTPS動画URLだけを返す。"""

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 2_048
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise MediaInspectionInputError("video URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise MediaInspectionInputError("video URL is invalid") from exc
    hostname = parsed.hostname
    if (
        parsed.scheme.casefold() != "https"
        or not isinstance(hostname, str)
        or hostname != hostname.casefold()
        or parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
        or port is not None
        or parsed.fragment
    ):
        raise MediaInspectionInputError("video URL is invalid")

    if hostname in _YOUTUBE_HOSTS:
        return _canonical_long_url(parsed.path, parsed.query)
    if hostname == _SHORT_HOST:
        return _canonical_short_url(parsed.path)
    raise MediaInspectionInputError("video URL host is unsupported")


def _canonical_long_url(path: str, query: str) -> str:
    if path == "/watch":
        try:
            values = parse_qs(query, keep_blank_values=True, strict_parsing=False).get("v", ())
        except ValueError as exc:
            raise MediaInspectionInputError("video URL is invalid") from exc
        if len(values) != 1 or _VIDEO_ID_RE.fullmatch(values[0]) is None:
            raise MediaInspectionInputError("video URL is invalid")
        return urlunsplit(("https", "www.youtube.com", "/watch", urlencode({"v": values[0]}), ""))

    parts = tuple(part for part in path.split("/") if part)
    if len(parts) != 2 or parts[0] != "shorts" or _VIDEO_ID_RE.fullmatch(parts[1]) is None:
        raise MediaInspectionInputError("video URL is invalid")
    return urlunsplit(("https", "www.youtube.com", f"/shorts/{parts[1]}", "", ""))


def _canonical_short_url(path: str) -> str:
    parts = tuple(part for part in path.split("/") if part)
    if len(parts) != 1 or _VIDEO_ID_RE.fullmatch(parts[0]) is None:
        raise MediaInspectionInputError("video URL is invalid")
    return urlunsplit(("https", _SHORT_HOST, f"/{parts[0]}", "", ""))


__all__ = ["canonicalize_youtube_url"]
