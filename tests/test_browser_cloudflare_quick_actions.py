from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from yonerai_discord.browser_sandbox.cloudflare_quick_actions import CloudflareQuickActionsScreenshotProvider
from yonerai_discord.browser_sandbox.models import (
    BrowserAdapterContractError,
    BrowserSessionRequest,
    Click,
    CssSelector,
    Navigate,
    Screenshot,
    ScreenshotFormat,
)
from yonerai_discord.browser_sandbox.policy import BrowserSandboxLimits, BrowserSandboxPolicy, StaticDnsResolver


_PNG = b"\x89PNG\r\n\x1a\nrendered"
_JPEG = b"\xff\xd8\xffrendered\xff\xd9"
_ACCOUNT_ID = "a" * 32


class _Content:
    def __init__(self, chunks: tuple[object, ...]) -> None:
        self.chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self.chunks:
            yield chunk


class _Response:
    def __init__(
        self,
        body: bytes = _PNG,
        *,
        status: int = 200,
        content_type: str = "image/png",
        history=(),
        chunks: tuple[object, ...] | None = None,
    ) -> None:
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.history = history
        self.content_length = len(body)
        self.content = _Content(chunks if chunks is not None else (body,))
        self.released = False

    def release(self) -> None:
        self.released = True


class _Session:
    def __init__(self, responses: list[_Response], *, block: bool = False) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []
        self.block = block
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def post(self, url: str, **kwargs: object) -> _Response:
        self.calls.append({"url": url, **kwargs})
        self.started.set()
        if self.block:
            await self.release.wait()
        return self.responses.pop(0)


def _policy(*, max_bytes: int = 1024 * 1024) -> BrowserSandboxPolicy:
    return BrowserSandboxPolicy(
        resolver=StaticDnsResolver({"example.com": ("93.184.216.34",)}),
        allowed_domains=("example.com",),
        limits=BrowserSandboxLimits(max_total_bytes=max_bytes),
    )


def _adapter(session: _Session, *, max_output_bytes: int = 8 * 1024 * 1024, max_concurrency: int = 1):
    return CloudflareQuickActionsScreenshotProvider(
        account_id=_ACCOUNT_ID,
        api_token="token-not-for-repr",
        session=session,
        max_output_bytes=max_output_bytes,
        max_concurrency=max_concurrency,
    )


def _request(image_format: ScreenshotFormat = ScreenshotFormat.PNG) -> BrowserSessionRequest:
    return BrowserSessionRequest((Navigate("https://example.com/path"), Screenshot(image_format=image_format)))


@pytest.mark.asyncio
async def test_posts_only_fixed_cloudflare_screenshot_request_and_returns_valid_png() -> None:
    session = _Session([_Response(chunks=(_PNG[:4], _PNG[4:]))])
    adapter = _adapter(session)
    result = await adapter.capture_screenshot(_request(), policy=_policy())

    assert result.outputs[0].data == _PNG
    assert result.outputs[0].step_index == 1
    call = session.calls[0]
    assert call["url"] == f"https://api.cloudflare.com/client/v4/accounts/{_ACCOUNT_ID}/browser-rendering/screenshot"
    assert call["params"] == {"cacheTTL": "0"}
    assert call["allow_redirects"] is False
    assert call["headers"] == {
        "Accept": "image/png, image/jpeg",
        "Authorization": "Bearer token-not-for-repr",
        "Content-Type": "application/json",
    }
    body = call["json"]
    assert body == {
        "url": "https://example.com/path",
        "gotoOptions": {"timeout": 45_000, "waitUntil": "domcontentloaded"},
        "actionTimeout": 45_000,
        "rejectResourceTypes": ["eventsource", "font", "manifest", "media", "websocket"],
        "screenshotOptions": {"fullPage": False, "type": "png"},
    }
    assert "token-not-for-repr" not in repr(adapter)
    await adapter.close()


@pytest.mark.asyncio
async def test_accepts_only_exact_navigation_screenshot_pair_and_rechecks_initial_url() -> None:
    session = _Session([_Response()])
    adapter = _adapter(session)
    invalid = BrowserSessionRequest((Navigate("https://example.com/"), Click(CssSelector("button"))))

    with pytest.raises(BrowserAdapterContractError, match="exactly"):
        await adapter.capture_screenshot(invalid, policy=_policy())
    with pytest.raises(Exception):
        await adapter.capture_screenshot(
            BrowserSessionRequest((Navigate("https://private.example/"), Screenshot())),
            policy=_policy(),
        )
    assert session.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    (
        _Response(status=429, content_type="application/json"),
        _Response(status=500, content_type="application/json"),
        _Response(content_type="application/json"),
        _Response(history=(SimpleNamespace(),)),
        _Response(body=b"not-an-image"),
        _Response(body=_PNG, chunks=("not-bytes",)),
    ),
)
async def test_errors_redirects_json_mime_and_invalid_bytes_fail_closed(response: _Response) -> None:
    session = _Session([response])
    adapter = _adapter(session)

    with pytest.raises(BrowserAdapterContractError):
        await adapter.capture_screenshot(_request(), policy=_policy())
    assert response.released is True


@pytest.mark.asyncio
async def test_jpeg_signature_limit_and_lifecycle_are_fail_closed() -> None:
    jpeg_session = _Session([_Response(_JPEG, content_type="image/jpeg")])
    jpeg = _adapter(jpeg_session)
    result = await jpeg.capture_screenshot(
        _request(ScreenshotFormat.JPEG),
        policy=_policy(),
    )
    assert result.outputs[0].media_type == "image/jpeg"

    oversized = _adapter(_Session([_Response(_PNG)]), max_output_bytes=len(_PNG) - 1)
    with pytest.raises(BrowserAdapterContractError, match="byte limit"):
        await oversized.capture_screenshot(_request(), policy=_policy())

    large_png = _PNG + (b"x" * (64 * 1024))
    policy_limited = _adapter(_Session([_Response(large_png)]))
    with pytest.raises(BrowserAdapterContractError, match="byte limit"):
        await policy_limited.capture_screenshot(_request(), policy=_policy(max_bytes=64 * 1024))

    await jpeg.close()
    with pytest.raises(BrowserAdapterContractError, match="closed"):
        await jpeg.capture_screenshot(_request(), policy=_policy())


@pytest.mark.asyncio
async def test_concurrency_limit_serializes_remote_calls() -> None:
    session = _Session([_Response(), _Response()], block=True)
    adapter = _adapter(session)
    first = asyncio.create_task(adapter.capture_screenshot(_request(), policy=_policy()))
    await asyncio.wait_for(session.started.wait(), timeout=1)
    second = asyncio.create_task(adapter.capture_screenshot(_request(), policy=_policy()))
    await asyncio.sleep(0)
    assert len(session.calls) == 1
    session.release.set()
    await asyncio.gather(first, second)
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_owned_session_close_cancellation_keeps_retryable_cleanup_reference() -> None:
    class CloseRetrySession(_Session):
        def __init__(self) -> None:
            super().__init__([])
            self.close_attempts = 0

        async def close(self) -> None:
            self.close_attempts += 1
            if self.close_attempts == 1:
                raise asyncio.CancelledError

    session = CloseRetrySession()
    adapter = _adapter(session)
    adapter._owns_session = True  # type: ignore[attr-defined]

    with pytest.raises(asyncio.CancelledError):
        await adapter.close()
    assert adapter._session is session  # type: ignore[attr-defined]

    await adapter.close()
    assert session.close_attempts == 2
    assert adapter._session is None  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "account_id",
    ("a" * 31, "g" * 32, "a" * 32 + "/x", "a" * 32 + "?x=1", "a" * 32 + "#x"),
)
def test_account_id_is_exact_hex_and_cannot_change_the_fixed_endpoint(account_id: str) -> None:
    with pytest.raises(ValueError, match="account_id"):
        CloudflareQuickActionsScreenshotProvider(
            account_id=account_id,
            api_token="token-not-for-repr",
            session=_Session([]),
        )
