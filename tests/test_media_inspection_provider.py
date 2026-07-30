from __future__ import annotations

import json
from typing import Any

import pytest

from yonerai_discord.modules.media_inspection import (
    GEMINI_INTERACTIONS_ENDPOINT,
    GEMINI_MEDIA_INSPECTION_MODEL,
    GeminiMediaInspectionProvider,
    MediaInspectionResponseError,
    MediaInspectionUnavailableError,
)


class _Content:
    def __init__(self, body: bytes, *, chunk_size: int | None = None) -> None:
        self.body = body
        self.chunk_size = chunk_size or len(body) or 1

    async def iter_chunked(self, _requested_size: int):
        for start in range(0, len(self.body), self.chunk_size):
            yield self.body[start : start + self.chunk_size]


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "application/json; charset=UTF-8",
        chunk_size: int | None = None,
    ) -> None:
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.content = _Content(body, chunk_size=chunk_size)

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Session:
    def __init__(self, response: _Response | BaseException) -> None:
        self.response = response
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.posts.append((url, kwargs))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response

    async def close(self) -> None:
        self.closed = True


def _body(*, status: str = "completed", outputs: list[dict[str, Any]] | None = None) -> bytes:
    data = {
        "status": status,
        "outputs": outputs
        or [
            {
                "type": "model_output",
                "content": [{"type": "text", "text": "解析結果"}],
            }
        ],
    }
    return json.dumps(data, ensure_ascii=False).encode()


@pytest.mark.asyncio
async def test_provider_posts_exact_interactions_contract_once_and_uses_last_model_output() -> None:
    response = _Response(
        _body(
            outputs=[
                {"type": "model_output", "content": [{"type": "text", "text": "古い結果"}]},
                {"type": "tool_result", "content": [{"type": "text", "text": "無視"}]},
                {
                    "type": "model_output",
                    "content": [
                        {"type": "text", "text": "字幕"},
                        {"type": "image", "uri": "ignored"},
                        {"type": "text", "text": "と映像"},
                    ],
                },
            ]
        )
    )
    session = _Session(response)
    provider = GeminiMediaInspectionProvider(
        api_key="test-api-key",
        call_reserver=lambda: True,
        session_factory=lambda **_kwargs: session,
    )

    result = await provider.inspect(
        "https://youtube.com/shorts/ABCDEFGHIJK?si=secret-tracking",
        "字幕と映像から内容を説明して",
    )

    assert result.text == "字幕と映像"
    assert len(session.posts) == 1
    url, kwargs = session.posts[0]
    assert url == GEMINI_INTERACTIONS_ENDPOINT
    assert kwargs["allow_redirects"] is False
    assert kwargs["headers"] == {
        "x-goog-api-key": "test-api-key",
        "Content-Type": "application/json",
    }
    assert kwargs["json"] == {
        "model": GEMINI_MEDIA_INSPECTION_MODEL,
        "input": [
            {"type": "text", "text": "字幕と映像から内容を説明して"},
            {"type": "video", "uri": "https://www.youtube.com/shorts/ABCDEFGHIJK"},
        ],
    }
    await provider.close()
    assert session.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    (
        _Response(_body(), status=429),
        _Response(_body(), content_type="text/html"),
        _Response(_body(status="running")),
        _Response(json.dumps({"status": "completed", "outputs": []}).encode()),
        _Response(_body(outputs=[{"type": "model_output", "content": [{"type": "image", "uri": "ignored"}]}])),
    ),
)
async def test_provider_fails_closed_for_http_and_shape_errors(response: _Response) -> None:
    session = _Session(response)
    provider = GeminiMediaInspectionProvider(
        api_key="test-api-key",
        call_reserver=lambda: True,
        session_factory=lambda **_kwargs: session,
    )

    with pytest.raises((MediaInspectionResponseError, MediaInspectionUnavailableError)):
        await provider.inspect("https://youtu.be/ABCDEFGHIJK", "内容を説明して")

    assert len(session.posts) == 1
    await provider.close()


@pytest.mark.asyncio
async def test_provider_rejects_oversized_response_without_retry() -> None:
    oversized = b'{"status":"completed","outputs":"' + (b"x" * 2_000) + b'"}'
    session = _Session(_Response(oversized, chunk_size=300))
    provider = GeminiMediaInspectionProvider(
        api_key="test-api-key",
        max_response_bytes=1_024,
        call_reserver=lambda: True,
        session_factory=lambda **_kwargs: session,
    )

    with pytest.raises(MediaInspectionResponseError, match="too large"):
        await provider.inspect("https://youtu.be/ABCDEFGHIJK", "内容を説明して")

    assert len(session.posts) == 1
    await provider.close()


@pytest.mark.asyncio
async def test_provider_does_not_retry_transport_failure_or_expose_sensitive_values() -> None:
    session = _Session(OSError("transport unavailable"))
    provider = GeminiMediaInspectionProvider(
        api_key="super-secret-api-key",
        call_reserver=lambda: True,
        session_factory=lambda **_kwargs: session,
    )

    with pytest.raises(MediaInspectionUnavailableError) as captured:
        await provider.inspect(
            "https://youtu.be/ABCDEFGHIJK?si=private-query",
            "private prompt",
        )

    rendered = repr(captured.value)
    assert "super-secret-api-key" not in rendered
    assert "private-query" not in rendered
    assert "private prompt" not in rendered
    assert len(session.posts) == 1
    await provider.close()


@pytest.mark.asyncio
async def test_provider_reserves_daily_quota_before_any_remote_post() -> None:
    session = _Session(_Response(_body()))
    provider = GeminiMediaInspectionProvider(
        api_key="test-api-key",
        call_reserver=lambda: False,
        session_factory=lambda **_kwargs: session,
    )

    with pytest.raises(MediaInspectionUnavailableError, match="daily call limit"):
        await provider.inspect("https://youtu.be/ABCDEFGHIJK", "内容を説明して")

    assert session.posts == []
    await provider.close()
