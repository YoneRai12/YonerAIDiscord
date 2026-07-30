from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Mapping
from typing import Any

import aiohttp

from .models import (
    BrowserAdapterContractError,
    BrowserOutput,
    BrowserOutputKind,
    BrowserSessionRequest,
    BrowserSessionResult,
    Screenshot,
    ScreenshotFormat,
    Navigate,
)
from .policy import BrowserSandboxPolicy


_API_ORIGIN = "https://api.cloudflare.com"
_API_PATH_TEMPLATE = "/client/v4/accounts/{account_id}/browser-rendering/screenshot"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_START = b"\xff\xd8\xff"
_JPEG_END = b"\xff\xd9"
_ALLOWED_MEDIA_TYPES = frozenset({"image/png", "image/jpeg"})
_DEFAULT_TIMEOUT_SECONDS = 45.0
_DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024
_CLOUDFLARE_ACCOUNT_ID = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)


class CloudflareQuickActionsScreenshotProvider:
    """Narrow remote screenshot provider, not an ``IsolatedBrowserAdapter``.

    It accepts exactly one ``Navigate`` followed by one ``Screenshot``.  Cloudflare
    Quick Actions execute remotely, so this adapter deliberately makes no claim
    about local OS/process isolation, redirect containment, or subresource DNS
    pinning.
    """

    def __init__(
        self,
        *,
        account_id: str,
        api_token: str,
        session: Any | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
        max_concurrency: int = 1,
    ) -> None:
        self._account_id = _identifier(account_id, "account_id")
        self._api_token = _token(api_token)
        self._timeout_seconds = _timeout(timeout_seconds, maximum=60.0)
        if (
            isinstance(max_output_bytes, bool)
            or not isinstance(max_output_bytes, int)
            or not 1 <= max_output_bytes <= _DEFAULT_MAX_OUTPUT_BYTES
        ):
            raise ValueError("max_output_bytes is out of range")
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or not 1 <= max_concurrency <= 8:
            raise ValueError("max_concurrency is out of range")
        self._max_output_bytes = max_output_bytes
        self._session = session
        self._owns_session = session is None
        self._closed = False
        self._lock = asyncio.Lock()
        self._max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(configured=True, max_output_bytes={self._max_output_bytes!r})"

    async def start(self) -> None:
        async with self._lock:
            if self._closed:
                raise BrowserAdapterContractError("remote screenshot adapter is closed")
            if self._session is None:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
                    trust_env=False,
                )

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
        acquired = 0
        try:
            # Drain every permit so an owned aiohttp session is never closed
            # underneath an in-flight capture. Waiting callers resume only to
            # observe the closed flag and fail without I/O.
            for _ in range(self._max_concurrency):
                await self._semaphore.acquire()
                acquired += 1
            async with self._lock:
                session = self._session
                if session is None:
                    return
                if not self._owns_session:
                    self._session = None
                    return
                close = getattr(session, "close", None)
                if callable(close):
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                # Detach only after successful close. Cancellation or failure
                # leaves the session reachable for a later cleanup retry.
                if self._session is session:
                    self._session = None
        finally:
            for _ in range(acquired):
                self._semaphore.release()

    async def capture_screenshot(
        self,
        request: BrowserSessionRequest,
        *,
        policy: BrowserSandboxPolicy,
    ) -> BrowserSessionResult:
        if not isinstance(request, BrowserSessionRequest) or not isinstance(policy, BrowserSandboxPolicy):
            raise BrowserAdapterContractError("remote screenshot request is invalid")
        navigate, screenshot = _exact_actions(request)
        timeout_seconds = min(self._timeout_seconds, float(policy.limits.max_duration_seconds))
        try:
            # The single deadline includes semaphore wait, synchronous DNS/policy
            # validation, and all remote I/O. This still does not assert
            # Cloudflare-side redirect or subresource containment.
            async with asyncio.timeout(timeout_seconds):
                async with self._semaphore:
                    if self._closed:
                        raise BrowserAdapterContractError("remote screenshot adapter is closed")
                    session = self._session
                    if session is None:
                        raise BrowserAdapterContractError("remote screenshot adapter is not started")
                    await asyncio.to_thread(policy.validate_session, request)
                    response: Any | None = None
                    try:
                        response = await session.post(
                            _endpoint(self._account_id),
                            params={"cacheTTL": "0"},
                            json=_body(navigate, screenshot, timeout_seconds),
                            headers={
                                "Accept": "image/png, image/jpeg",
                                "Authorization": f"Bearer {self._api_token}",
                                "Content-Type": "application/json",
                            },
                            allow_redirects=False,
                            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                        )
                        _require_success_image(response)
                        image = await _read_image(
                            response,
                            maximum_bytes=min(
                                self._max_output_bytes,
                                policy.limits.max_total_bytes,
                                _DEFAULT_MAX_OUTPUT_BYTES,
                            ),
                        )
                        media_type = _media_type(response)
                    finally:
                        release = getattr(response, "release", None)
                        if callable(release):
                            try:
                                released = release()
                                if inspect.isawaitable(released):
                                    await released
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                pass
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise BrowserAdapterContractError("remote screenshot request timed out") from None
        except BrowserAdapterContractError:
            raise
        except Exception:
            raise BrowserAdapterContractError("remote screenshot request failed safely") from None
        _validate_image(image, media_type)
        return BrowserSessionResult(outputs=(BrowserOutput(1, BrowserOutputKind.SCREENSHOT, image, media_type),))


def _exact_actions(request: BrowserSessionRequest) -> tuple[Navigate, Screenshot]:
    if (
        len(request.actions) != 2
        or type(request.actions[0]) is not Navigate
        or type(request.actions[1]) is not Screenshot
    ):
        raise BrowserAdapterContractError("remote screenshot supports exactly Navigate followed by Screenshot")
    return request.actions[0], request.actions[1]


def _endpoint(account_id: str) -> str:
    return _API_ORIGIN + _API_PATH_TEMPLATE.format(account_id=account_id)


def _body(navigate: Navigate, screenshot: Screenshot, timeout_seconds: float) -> dict[str, object]:
    image_type = "png" if screenshot.image_format is ScreenshotFormat.PNG else "jpeg"
    timeout_milliseconds = max(1, int(timeout_seconds * 1000))
    return {
        "url": navigate.url,
        "gotoOptions": {"timeout": min(timeout_milliseconds, 60_000), "waitUntil": "domcontentloaded"},
        "actionTimeout": min(timeout_milliseconds, 120_000),
        "rejectResourceTypes": ["eventsource", "font", "manifest", "media", "websocket"],
        "screenshotOptions": {"fullPage": screenshot.full_page, "type": image_type},
    }


def _require_success_image(response: Any) -> None:
    status = getattr(response, "status", None)
    if getattr(response, "history", ()) or (isinstance(status, int) and 300 <= status < 400):
        raise BrowserAdapterContractError("remote screenshot redirect was rejected")
    if status == 429:
        raise BrowserAdapterContractError("remote screenshot rate limit was reached")
    if status != 200 or _media_type(response) not in _ALLOWED_MEDIA_TYPES:
        raise BrowserAdapterContractError("remote screenshot response was rejected")


async def _read_image(response: Any, *, maximum_bytes: int) -> bytes:
    content_length = getattr(response, "content_length", None)
    if isinstance(content_length, int) and not isinstance(content_length, bool) and content_length > maximum_bytes:
        raise BrowserAdapterContractError("remote screenshot exceeds the byte limit")
    iterator = getattr(getattr(response, "content", None), "iter_chunked", None)
    if not callable(iterator):
        raise BrowserAdapterContractError("remote screenshot response is not stream-readable")
    body = bytearray()
    async for chunk in iterator(min(_CHUNK_BYTES, maximum_bytes + 1)):
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise BrowserAdapterContractError("remote screenshot response was invalid")
        if len(body) + len(chunk) > maximum_bytes:
            raise BrowserAdapterContractError("remote screenshot exceeds the byte limit")
        body.extend(chunk)
    if not body:
        raise BrowserAdapterContractError("remote screenshot response was empty")
    return bytes(body)


def _media_type(response: Any) -> str:
    headers = getattr(response, "headers", None)
    value = headers.get("Content-Type", "") if isinstance(headers, Mapping) else ""
    return value.split(";", 1)[0].strip().casefold() if isinstance(value, str) else ""


def _validate_image(data: bytes, media_type: str) -> None:
    if media_type == "image/png" and data.startswith(_PNG_SIGNATURE):
        return
    if media_type == "image/jpeg" and data.startswith(_JPEG_START) and data.endswith(_JPEG_END):
        return
    raise BrowserAdapterContractError("remote screenshot image signature was invalid")


def _identifier(value: object, label: str) -> str:
    if label == "account_id" and isinstance(value, str) and _CLOUDFLARE_ACCOUNT_ID.fullmatch(value):
        return value.lower()
    raise ValueError(f"{label} is invalid")


def _token(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(character.isspace() or ord(character) < 33 for character in value)
    ):
        raise ValueError("api_token is invalid")
    return value


def _timeout(value: object, *, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.1 <= float(value) <= min(60.0, maximum):
        raise ValueError("timeout_seconds is out of range")
    return float(value)


__all__ = ["CloudflareQuickActionsScreenshotProvider"]
