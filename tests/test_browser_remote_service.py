from __future__ import annotations

import asyncio

import pytest

from yonerai_discord.browser_sandbox.models import (
    BrowserAdapterContractError,
    BrowserOutput,
    BrowserOutputKind,
    BrowserPolicyError,
    BrowserSandboxUnavailableError,
    BrowserSessionRequest,
    BrowserSessionResult,
    Navigate,
    Screenshot,
)
from yonerai_discord.browser_sandbox.policy import BrowserSandboxLimits, BrowserSandboxPolicy, StaticDnsResolver
from yonerai_discord.browser_sandbox.remote_service import RemoteBrowserScreenshotService


def _policy(*, duration: float = 1.0) -> BrowserSandboxPolicy:
    return BrowserSandboxPolicy(
        resolver=StaticDnsResolver({"example.com": ("93.184.216.34",)}),
        allowed_domains=("example.com",),
        limits=BrowserSandboxLimits(max_duration_seconds=duration),
    )


def _request() -> BrowserSessionRequest:
    return BrowserSessionRequest((Navigate("https://example.com/path"), Screenshot()))


def _result() -> BrowserSessionResult:
    return BrowserSessionResult(
        (BrowserOutput(1, BrowserOutputKind.SCREENSHOT, b"\x89PNG\r\n\x1a\nimage", "image/png"),)
    )


class Provider:
    def __init__(self, result: object | None = None) -> None:
        self.result = _result() if result is None else result
        self.started = False
        self.closed = False
        self.calls = 0

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def capture_screenshot(
        self, request: BrowserSessionRequest, *, policy: BrowserSandboxPolicy
    ) -> BrowserSessionResult:
        del request, policy
        self.calls += 1
        return self.result  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_unconfigured_and_lifecycle_are_fail_closed() -> None:
    service = RemoteBrowserScreenshotService(policy=_policy())
    assert not service.configured
    with pytest.raises(BrowserSandboxUnavailableError, match="not configured"):
        await service.start()

    provider = Provider()
    service = RemoteBrowserScreenshotService(policy=_policy(), provider=provider)
    with pytest.raises(BrowserSandboxUnavailableError, match="not started"):
        await service.capture_screenshot(_request(), authorization_current=lambda: True)
    await service.start()
    assert service.started and provider.started
    await service.close()
    assert provider.closed
    with pytest.raises(BrowserSandboxUnavailableError):
        await service.capture_screenshot(_request(), authorization_current=lambda: True)


@pytest.mark.asyncio
async def test_authorization_is_checked_before_api_and_before_delivery() -> None:
    provider = Provider()
    service = RemoteBrowserScreenshotService(policy=_policy(), provider=provider)
    await service.start()
    with pytest.raises(BrowserPolicyError, match="no longer current"):
        await service.capture_screenshot(_request(), authorization_current=lambda: False)
    assert provider.calls == 0

    checks = iter((True, False))
    with pytest.raises(BrowserPolicyError, match="no longer current"):
        await service.capture_screenshot(_request(), authorization_current=lambda: next(checks))
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_timeout_bad_result_provider_swap_and_cancellation_are_safe() -> None:
    class SlowProvider(Provider):
        async def capture_screenshot(
            self, request: BrowserSessionRequest, *, policy: BrowserSandboxPolicy
        ) -> BrowserSessionResult:
            del request, policy
            await asyncio.sleep(10)
            return _result()

    service = RemoteBrowserScreenshotService(policy=_policy(duration=1.0), provider=SlowProvider())
    await service.start()
    with pytest.raises(BrowserAdapterContractError, match="timed out"):
        await service.capture_screenshot(_request(), authorization_current=lambda: True)

    bad = RemoteBrowserScreenshotService(policy=_policy(), provider=Provider(BrowserSessionResult()))
    await bad.start()
    with pytest.raises(BrowserAdapterContractError, match="output count"):
        await bad.capture_screenshot(_request(), authorization_current=lambda: True)

    invalid_image = RemoteBrowserScreenshotService(
        policy=_policy(),
        provider=Provider(
            BrowserSessionResult((BrowserOutput(1, BrowserOutputKind.SCREENSHOT, b"not-image", "image/png"),))
        ),
    )
    await invalid_image.start()
    with pytest.raises(BrowserAdapterContractError, match="invalid image"):
        await invalid_image.capture_screenshot(_request(), authorization_current=lambda: True)

    provider = Provider()
    swapped = RemoteBrowserScreenshotService(policy=_policy(), provider=provider)
    await swapped.start()
    swapped._provider = Provider()  # type: ignore[assignment]
    with pytest.raises(BrowserSandboxUnavailableError, match="changed"):
        await swapped.capture_screenshot(_request(), authorization_current=lambda: True)

    class CancelledProvider(Provider):
        async def capture_screenshot(
            self, request: BrowserSessionRequest, *, policy: BrowserSandboxPolicy
        ) -> BrowserSessionResult:
            del request, policy
            raise asyncio.CancelledError

    cancelled = RemoteBrowserScreenshotService(policy=_policy(), provider=CancelledProvider())
    await cancelled.start()
    with pytest.raises(asyncio.CancelledError):
        await cancelled.capture_screenshot(_request(), authorization_current=lambda: True)


@pytest.mark.asyncio
async def test_start_and_close_cancellation_leave_a_retryable_cleanup_path() -> None:
    class StartCancelledProvider(Provider):
        async def start(self) -> None:
            self.started = True
            raise asyncio.CancelledError

    start_cancelled_provider = StartCancelledProvider()
    start_cancelled = RemoteBrowserScreenshotService(policy=_policy(), provider=start_cancelled_provider)
    with pytest.raises(asyncio.CancelledError):
        await start_cancelled.start()
    assert start_cancelled_provider.closed is True
    await start_cancelled.close()

    class CloseCancelledProvider(Provider):
        def __init__(self) -> None:
            super().__init__()
            self.close_attempts = 0

        async def close(self) -> None:
            self.close_attempts += 1
            if self.close_attempts == 1:
                raise asyncio.CancelledError
            self.closed = True

    close_cancelled_provider = CloseCancelledProvider()
    close_cancelled = RemoteBrowserScreenshotService(policy=_policy(), provider=close_cancelled_provider)
    await close_cancelled.start()
    with pytest.raises(asyncio.CancelledError):
        await close_cancelled.close()
    await close_cancelled.close()
    assert close_cancelled_provider.close_attempts == 2
    assert close_cancelled_provider.closed is True


@pytest.mark.asyncio
async def test_failed_start_requires_cleanup_before_the_service_can_be_reused() -> None:
    class StartFailedProvider(Provider):
        def __init__(self) -> None:
            super().__init__()
            self.close_attempts = 0

        async def start(self) -> None:
            raise RuntimeError("start failed")

        async def close(self) -> None:
            self.close_attempts += 1
            if self.close_attempts == 1:
                raise RuntimeError("close failed")
            self.closed = True

    provider = StartFailedProvider()
    service = RemoteBrowserScreenshotService(policy=_policy(), provider=provider)
    with pytest.raises(BrowserAdapterContractError, match="failed to start"):
        await service.start()
    with pytest.raises(BrowserSandboxUnavailableError, match="cleanup is pending"):
        await service.start()

    await service.close()
    assert provider.close_attempts == 2
    assert provider.closed is True
