from __future__ import annotations

import asyncio
import inspect
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import Any, Protocol

import aiohttp

from .domain import (
    ApodItem,
    canonicalize_apod_item,
    parse_apod_payload,
    validate_apod_request_date,
)
from .errors import (
    ApodConfigurationError,
    ApodNotFoundError,
    ApodRateLimitedError,
    ApodResponseError,
    ApodResponseTooLargeError,
    ApodTransportError,
)


NASA_APOD_ENDPOINT = "https://api.nasa.gov/planetary/apod"
MAX_APOD_RESPONSE_BYTES = 256 * 1024
DEFAULT_TOTAL_TIMEOUT_SECONDS = 8.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 3.0
DEFAULT_READ_TIMEOUT_SECONDS = 5.0
_USER_AGENT = "YonerAI-Discord-Suite/0.1 (read-only-nasa-apod-client)"


class ApodSource(Protocol):
    async def fetch(self, day: date | None = None) -> ApodItem: ...


class NasaApiApodSource:
    """固定HTTPS endpointへGETするだけの、明示opt-in用source。"""

    def __init__(
        self,
        api_key: str,
        *,
        session: Any | None = None,
        total_timeout_seconds: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_APOD_RESPONSE_BYTES,
    ) -> None:
        key = api_key.strip() if isinstance(api_key, str) else ""
        if (
            not key
            or key.casefold() == "demo_key"
            or len(key) > 256
            or any(character.isspace() or ord(character) < 33 for character in key)
        ):
            raise ApodConfigurationError("NASA APOD API key is not ready")
        total = _positive_finite(total_timeout_seconds, "total_timeout_seconds")
        connect = _positive_finite(connect_timeout_seconds, "connect_timeout_seconds")
        read = _positive_finite(read_timeout_seconds, "read_timeout_seconds")
        if connect > total or read > total:
            raise ValueError("connect/read timeout must not exceed total timeout")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or not 1 <= max_response_bytes <= MAX_APOD_RESPONSE_BYTES
        ):
            raise ValueError("max_response_bytes is out of range")
        self._api_key = key
        self._session = session
        self._owns_session = session is None
        self.timeout = aiohttp.ClientTimeout(
            total=total,
            connect=connect,
            sock_connect=connect,
            sock_read=read,
        )
        self.max_response_bytes = max_response_bytes

    def __repr__(self) -> str:
        return f"{type(self).__name__}(endpoint={NASA_APOD_ENDPOINT!r}, max_response_bytes={self.max_response_bytes!r})"

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
                trust_env=False,
            )

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None and self._owns_session:
            close = getattr(session, "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result

    async def fetch(self, day: date | None = None) -> ApodItem:
        if day is not None:
            day = validate_apod_request_date(day, today=datetime.now(UTC).date())
        session = self._session
        if session is None:
            raise ApodConfigurationError("NASA APOD source is not started")
        params = {"api_key": self._api_key}
        if day is not None:
            params["date"] = day.isoformat()
        response: Any | None = None
        try:
            response = await session.get(
                NASA_APOD_ENDPOINT,
                params=params,
                allow_redirects=False,
                timeout=self.timeout,
                headers={"Accept": "application/json"},
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        if response is None:
            raise ApodTransportError("NASA APOD request failed")
        try:
            status = getattr(response, "status", None)
            if getattr(response, "history", ()) or (isinstance(status, int) and 300 <= status < 400):
                raise ApodTransportError("NASA APOD redirect was rejected")
            if status == 404:
                raise ApodNotFoundError("NASA APOD entry was not found")
            if status == 429:
                raise ApodRateLimitedError("NASA APOD rate limit was reached")
            if status != 200:
                raise ApodTransportError("NASA APOD returned a non-success status")
            media_type = _content_type(response)
            if media_type != "application/json":
                raise ApodResponseError("NASA APOD response is not JSON")
            body: bytes | None = None
            try:
                body = await _read_bounded(response, maximum_bytes=self.max_response_bytes)
            except asyncio.CancelledError:
                raise
            except (ApodResponseError, ApodTransportError):
                raise
            except Exception:
                pass
            if body is None:
                raise ApodTransportError("NASA APOD response stream failed")
        finally:
            release = getattr(response, "release", None)
            if callable(release):
                try:
                    result = release()
                    if inspect.isawaitable(result):
                        await result
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
        decode_failed = False
        try:
            payload = json.loads(
                body.decode("utf-8", errors="strict"),
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
            decode_failed = True
            payload = None
        if decode_failed:
            raise ApodResponseError("NASA APOD returned invalid UTF-8 JSON")
        if not isinstance(payload, Mapping):
            raise ApodResponseError("NASA APOD response must be an object")
        return parse_apod_payload(payload)


class StaticApodSource:
    """検証済みApodItemだけを注入するoffline source。file/url importは行わない。"""

    def __init__(self, items: Sequence[ApodItem]) -> None:
        raw_values = tuple(items)
        if not raw_values:
            raise ValueError("items must contain validated ApodItem values")
        values = tuple(canonicalize_apod_item(item) for item in raw_values)
        by_day = {item.day: item for item in values}
        if len(by_day) != len(values):
            raise ValueError("APOD fixture dates must be unique")
        self._items = MappingProxyType(by_day)

    async def fetch(self, day: date | None = None) -> ApodItem:
        target = day if day is not None else max(self._items)
        try:
            return self._items[target]
        except KeyError:
            raise ApodNotFoundError("NASA APOD fixture entry was not found") from None


def _content_type(response: Any) -> str:
    headers = getattr(response, "headers", None)
    raw = headers.get("Content-Type", "") if isinstance(headers, Mapping) else ""
    return raw.split(";", 1)[0].strip().casefold() if isinstance(raw, str) else ""


async def _read_bounded(response: Any, *, maximum_bytes: int) -> bytes:
    content_length = getattr(response, "content_length", None)
    if isinstance(content_length, int) and not isinstance(content_length, bool) and content_length > maximum_bytes:
        raise ApodResponseTooLargeError("NASA APOD response exceeds the byte limit")
    iterator = getattr(getattr(response, "content", None), "iter_chunked", None)
    if not callable(iterator):
        raise ApodTransportError("NASA APOD response is not stream-readable")
    body = bytearray()
    async for chunk in iterator(min(65_536, maximum_bytes + 1)):
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise ApodTransportError("NASA APOD response contained a non-byte chunk")
        if len(body) + len(chunk) > maximum_bytes:
            raise ApodResponseTooLargeError("NASA APOD response exceeds the byte limit")
        body.extend(chunk)
    return bytes(body)


def _positive_finite(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a positive finite number")
    normalized = float(value)
    if normalized <= 0 or not math.isfinite(normalized):
        raise ValueError(f"{label} must be a positive finite number")
    return normalized


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON value")


__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_READ_TIMEOUT_SECONDS",
    "DEFAULT_TOTAL_TIMEOUT_SECONDS",
    "MAX_APOD_RESPONSE_BYTES",
    "NASA_APOD_ENDPOINT",
    "ApodSource",
    "NasaApiApodSource",
    "StaticApodSource",
]
