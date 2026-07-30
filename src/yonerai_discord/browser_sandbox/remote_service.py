"""Lifecycle wrapper for a narrow, remotely-executed screenshot provider.

This is deliberately *not* ``BrowserSandboxService`` and its provider is not an
``IsolatedBrowserAdapter``.  A remote provider may execute in infrastructure we
do not control, so this class only offers the small ``Navigate -> Screenshot``
contract and makes no local-isolation, redirect-containment, or DNS-pinning
claim.
"""

from __future__ import annotations

import asyncio
import inspect
from enum import StrEnum
from typing import Awaitable, Callable, Protocol, runtime_checkable

from .models import (
    BrowserAdapterContractError,
    BrowserOutput,
    BrowserOutputKind,
    BrowserPolicyError,
    BrowserSandboxError,
    BrowserSandboxUnavailableError,
    BrowserSessionRequest,
    BrowserSessionResult,
    Navigate,
    Screenshot,
)
from .policy import BrowserSandboxPolicy


AuthorizationCurrent = Callable[[], bool | Awaitable[bool]]
_MAX_DISCORD_SCREENSHOT_BYTES = 8 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_START = b"\xff\xd8\xff"
_JPEG_END = b"\xff\xd9"


@runtime_checkable
class RemoteScreenshotProvider(Protocol):
    """Contract for a remotely-run screenshot backend, separate from isolation."""

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def capture_screenshot(
        self, request: BrowserSessionRequest, *, policy: BrowserSandboxPolicy
    ) -> BrowserSessionResult: ...


class _Lifecycle(StrEnum):
    NEW = "new"
    STARTING = "starting"
    STARTED = "started"
    CLOSING = "closing"
    CLOSED = "closed"


class RemoteBrowserScreenshotService:
    """Fail-closed service for one authorized remote screenshot operation."""

    def __init__(self, *, policy: BrowserSandboxPolicy, provider: RemoteScreenshotProvider | None = None) -> None:
        if not isinstance(policy, BrowserSandboxPolicy):
            raise TypeError("policy must be BrowserSandboxPolicy")
        if provider is not None and not isinstance(provider, RemoteScreenshotProvider):
            raise TypeError("provider must implement remote screenshot lifecycle")
        self._policy = policy
        self._provider = provider
        self._started_provider: RemoteScreenshotProvider | None = None
        self._cleanup_provider: RemoteScreenshotProvider | None = None
        self._lifecycle = _Lifecycle.NEW
        self._lifecycle_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return self._provider is not None

    @property
    def started(self) -> bool:
        return self._lifecycle is _Lifecycle.STARTED

    async def start(self) -> None:
        async with self._lifecycle_lock:
            provider = self._provider
            if provider is None:
                raise BrowserSandboxUnavailableError("remote screenshot provider is not configured")
            if self._lifecycle is _Lifecycle.STARTED:
                return
            if self._lifecycle is not _Lifecycle.NEW:
                raise BrowserSandboxUnavailableError("remote screenshot service is not available")
            if self._cleanup_provider is not None:
                raise BrowserSandboxUnavailableError("remote screenshot provider cleanup is pending")
            self._lifecycle = _Lifecycle.STARTING
        try:
            await provider.start()
            async with self._lifecycle_lock:
                if self._provider is not provider or self._lifecycle is not _Lifecycle.STARTING:
                    raise BrowserSandboxUnavailableError("remote screenshot provider changed during start")
                self._started_provider = provider
                self._cleanup_provider = provider
                self._lifecycle = _Lifecycle.STARTED
        except asyncio.CancelledError:
            closed = False
            try:
                closed = await _best_effort_close(provider)
            finally:
                await self._reset_failed_start(provider, closed=closed)
            raise
        except Exception:
            closed = False
            try:
                closed = await _best_effort_close(provider)
            finally:
                await self._reset_failed_start(provider, closed=closed)
            raise BrowserAdapterContractError("remote screenshot provider failed to start") from None

    async def _reset_failed_start(self, provider: RemoteScreenshotProvider, *, closed: bool) -> None:
        async with self._lifecycle_lock:
            if self._lifecycle in {_Lifecycle.STARTING, _Lifecycle.STARTED}:
                if self._started_provider is provider:
                    self._started_provider = None
                self._cleanup_provider = None if closed else provider
                self._lifecycle = _Lifecycle.NEW

    async def close(self) -> None:
        async with self._lifecycle_lock:
            provider = self._started_provider or self._cleanup_provider
            if self._lifecycle is _Lifecycle.CLOSED:
                return
            if self._lifecycle is _Lifecycle.CLOSING:
                raise BrowserSandboxUnavailableError("remote screenshot service is closing")
            self._lifecycle = _Lifecycle.CLOSING
        try:
            if provider is not None:
                await provider.close()
        except asyncio.CancelledError:
            async with self._lifecycle_lock:
                self._started_provider = None
                self._cleanup_provider = provider
                self._lifecycle = _Lifecycle.NEW
            raise
        except Exception:
            async with self._lifecycle_lock:
                self._started_provider = None
                self._cleanup_provider = provider
                self._lifecycle = _Lifecycle.NEW
            raise BrowserAdapterContractError("remote screenshot provider failed to close") from None
        async with self._lifecycle_lock:
            self._started_provider = None
            self._cleanup_provider = None
            self._lifecycle = _Lifecycle.CLOSED

    async def capture_screenshot(
        self, request: BrowserSessionRequest, *, authorization_current: AuthorizationCurrent
    ) -> BrowserSessionResult:
        if not isinstance(request, BrowserSessionRequest):
            raise TypeError("request must be BrowserSessionRequest")
        if not callable(authorization_current):
            raise TypeError("authorization_current is required")
        _require_exact_request(request)
        try:
            # This deadline covers validation, authorization checks, provider
            # execution, and result validation rather than only the API call.
            async with asyncio.timeout(float(self._policy.limits.max_duration_seconds)):
                provider = await self._active_provider()
                # Policy DNS resolution is synchronous; never let a remote lookup
                # block Discord's event loop before the provider API call.
                await asyncio.to_thread(self._policy.validate_session, request)
                await _require_authorized(authorization_current)
                await self._require_same_active_provider(provider)
                result = await provider.capture_screenshot(request, policy=self._policy)
                await _require_authorized(authorization_current)
                await self._require_same_active_provider(provider)
                _validate_result(result, policy=self._policy)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise BrowserAdapterContractError("remote screenshot provider timed out") from None
        except BrowserSandboxError:
            raise
        except Exception:
            raise BrowserAdapterContractError("remote screenshot provider failed safely") from None
        return result

    async def _active_provider(self) -> RemoteScreenshotProvider:
        async with self._lifecycle_lock:
            if self._provider is None:
                raise BrowserSandboxUnavailableError("remote screenshot provider is not configured")
            if self._lifecycle is not _Lifecycle.STARTED or self._provider is not self._started_provider:
                raise BrowserSandboxUnavailableError("remote screenshot provider changed or service is not started")
            return self._provider

    async def _require_same_active_provider(self, provider: RemoteScreenshotProvider) -> None:
        async with self._lifecycle_lock:
            if (
                self._lifecycle is not _Lifecycle.STARTED
                or self._provider is not provider
                or self._started_provider is not provider
            ):
                raise BrowserSandboxUnavailableError("remote screenshot provider changed or is closing")


async def _require_authorized(authorization_current: AuthorizationCurrent) -> None:
    try:
        allowed = authorization_current()
        if inspect.isawaitable(allowed):
            allowed = await allowed
    except asyncio.CancelledError:
        raise
    except Exception:
        raise BrowserPolicyError("remote screenshot authorization check failed") from None
    if allowed is not True:
        raise BrowserPolicyError("remote screenshot authorization is no longer current")


def _require_exact_request(request: BrowserSessionRequest) -> None:
    if (
        len(request.actions) != 2
        or type(request.actions[0]) is not Navigate
        or type(request.actions[1]) is not Screenshot
    ):
        raise BrowserAdapterContractError("remote screenshot supports exactly Navigate followed by Screenshot")


def _validate_result(result: object, *, policy: BrowserSandboxPolicy) -> None:
    if type(result) is not BrowserSessionResult:
        raise BrowserAdapterContractError("remote screenshot provider returned an invalid result")
    if len(result.outputs) != 1:
        raise BrowserAdapterContractError("remote screenshot provider returned an invalid output count")
    output = result.outputs[0]
    if type(output) is not BrowserOutput:
        raise BrowserAdapterContractError("remote screenshot provider returned an invalid output type")
    if output.step_index != 1 or output.kind is not BrowserOutputKind.SCREENSHOT:
        raise BrowserAdapterContractError("remote screenshot provider returned an invalid output step")
    if output.media_type not in {"image/png", "image/jpeg"}:
        raise BrowserAdapterContractError("remote screenshot provider returned an invalid media type")
    if output.byte_length > min(policy.limits.max_total_bytes, _MAX_DISCORD_SCREENSHOT_BYTES):
        raise BrowserAdapterContractError("remote screenshot provider exceeded the byte limit")
    if output.media_type == "image/png" and output.data.startswith(_PNG_SIGNATURE):
        return
    if output.media_type == "image/jpeg" and output.data.startswith(_JPEG_START) and output.data.endswith(_JPEG_END):
        return
    raise BrowserAdapterContractError("remote screenshot provider returned an invalid image")


async def _best_effort_close(provider: RemoteScreenshotProvider) -> bool:
    """Close a possibly-partially-started provider without hiding cancellation."""

    try:
        await provider.close()
    except asyncio.CancelledError:
        raise
    except Exception:
        return False
    return True


__all__ = ["AuthorizationCurrent", "RemoteBrowserScreenshotService", "RemoteScreenshotProvider"]
