from __future__ import annotations

import ipaddress
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Literal
from urllib.parse import urlsplit

from .errors import ApodDateError, ApodResponseError


APOD_FIRST_DATE = date(1995, 6, 16)
APOD_SOURCE_BASE_URL = "https://apod.nasa.gov/apod/"
_APOD_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z", re.ASCII)


@dataclass(frozen=True, slots=True)
class ApodItem:
    day: date
    title: str
    explanation: str
    media_type: Literal["image", "video"]
    url: str | None
    hdurl: str | None = None
    thumbnail_url: str | None = None
    copyright: str | None = None

    @property
    def source_page_url(self) -> str:
        return f"{APOD_SOURCE_BASE_URL}ap{self.day:%y%m%d}.html"


def parse_apod_payload(payload: object) -> ApodItem:
    if not isinstance(payload, Mapping):
        raise ApodResponseError("NASA APOD response must be an object")
    day = _required_date(payload.get("date"))
    if day < APOD_FIRST_DATE:
        raise ApodResponseError("NASA APOD response date predates the public archive")
    media_type = payload.get("media_type")
    if media_type not in {"image", "video"}:
        raise ApodResponseError("NASA APOD media_type is unsupported")
    return ApodItem(
        day=day,
        title=_required_text(payload.get("title"), "title", maximum=300),
        explanation=_required_text(payload.get("explanation"), "explanation", maximum=8_000),
        media_type=media_type,
        url=_safe_https_url(payload.get("url")),
        hdurl=_safe_https_url(payload.get("hdurl")),
        thumbnail_url=_safe_https_url(payload.get("thumbnail_url")),
        copyright=_optional_text(payload.get("copyright"), maximum=300),
    )


def canonicalize_apod_item(value: object) -> ApodItem:
    """直接constructされたfixtureもAPI応答と同じdomain境界へ通す。"""

    if not isinstance(value, ApodItem) or type(value.day) is not date:
        raise ValueError("items must contain valid ApodItem values")
    try:
        return parse_apod_payload(
            {
                "date": value.day.isoformat(),
                "title": value.title,
                "explanation": value.explanation,
                "media_type": value.media_type,
                "url": value.url,
                "hdurl": value.hdurl,
                "thumbnail_url": value.thumbnail_url,
                "copyright": value.copyright,
            }
        )
    except ApodResponseError:
        raise ValueError("items must contain valid ApodItem values") from None


def validate_apod_request_date(value: object, *, today: date) -> date:
    if type(value) is not date or type(today) is not date:
        raise ApodDateError("APOD date must be a calendar date")
    if not APOD_FIRST_DATE <= value <= today:
        raise ApodDateError("APOD date is outside the public range")
    return value


def _required_date(value: object) -> date:
    if not isinstance(value, str) or _APOD_DATE_PATTERN.fullmatch(value) is None:
        raise ApodResponseError("NASA APOD date must be an ISO calendar date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ApodResponseError("NASA APOD date must be an ISO calendar date") from exc


def _required_text(value: object, label: str, *, maximum: int) -> str:
    text = _optional_text(value, maximum=maximum)
    if text is None:
        raise ApodResponseError(f"NASA APOD {label} is missing or invalid")
    return text


def _optional_text(value: object, *, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if (
        not text
        or len(text) > maximum
        or any(unicodedata.category(character).startswith("C") and character not in {"\n", "\t"} for character in text)
    ):
        return None
    return text


def _safe_https_url(value: object) -> str | None:
    # Discord本文とcustom Embedの上限内で、番号リンクを途中切断せず表示できる長さに固定する。
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1_024
        or any(character.isspace() or character == "\\" for character in value)
    ):
        return None
    if any(unicodedata.category(character).startswith("C") for character in value):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    hostname = parsed.hostname or ""
    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None
    if (
        parsed.scheme != "https"
        or not hostname
        or hostname.casefold() == "localhost"
        or hostname.casefold().endswith(".localhost")
        or (literal_ip is not None and not literal_ip.is_global)
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        return None
    return value


__all__ = [
    "APOD_FIRST_DATE",
    "APOD_SOURCE_BASE_URL",
    "ApodItem",
    "canonicalize_apod_item",
    "parse_apod_payload",
    "validate_apod_request_date",
]
