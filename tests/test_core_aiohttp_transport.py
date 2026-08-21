from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from yonerai_discord.execution_gateway.core_http_transport import (
    MAX_CORE_RUN_RESPONSE_BYTES,
    AiohttpCoreHttpTransport,
    CoreHttpTransportError,
)


class _Content:
    def __init__(self, body: bytes = b"", chunks: tuple[bytes, ...] = ()) -> None:
        self.body = body
        self.chunks = chunks
        self.chunk_sizes: list[int] = []

    def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        async def iterate() -> AsyncIterator[bytes]:
            self.chunk_sizes.append(size)
            for chunk in self.chunks or (self.body,):
                yield chunk

        return iterate()


class _Response:
    def __init__(
        self,
        content: _Content,
        *,
        status: int = 202,
        content_type: str = "application/json",
        release_error: Exception | None = None,
    ) -> None:
        self.content = content
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.released = 0
        self.release_error = release_error

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def release(self) -> None:
        self.released += 1
        if self.release_error is not None:
            raise self.release_error


class _Session:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.posts: list[tuple[str, dict[str, object]]] = []
        self.gets: list[tuple[str, dict[str, object]]] = []
        self.closed = 0

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    def post(self, url: str, **kwargs: object) -> _Response:
        self.posts.append((url, kwargs))
        return self.response

    async def get(self, url: str, **kwargs: object) -> _Response:
        self.gets.append((url, kwargs))
        return self.response

    async def close(self) -> None:
        self.closed += 1


def _patch_session(monkeypatch: pytest.MonkeyPatch, *sessions: _Session) -> list[dict[str, object]]:
    remaining = iter(sessions)
    created: list[dict[str, object]] = []

    def create_session(**kwargs: object) -> _Session:
        created.append(kwargs)
        return next(remaining)

    monkeypatch.setattr(
        "yonerai_discord.execution_gateway.core_http_transport.aiohttp.ClientSession",
        create_session,
    )
    return created


@pytest.mark.asyncio
async def test_post_uses_fixed_origin_bearer_redirect_ban_and_bounded_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _Content(chunks=(b'{"run_', b'id":"run-1"}'))
    session = _Session(_Response(content))
    timeouts = _patch_session(monkeypatch, session)
    transport = AiohttpCoreHttpTransport("https://core.example", "test-token")

    response = await transport.post_json("/v1/messages", body=b"{}", allow_redirects=False)

    assert response.body == b'{"run_id":"run-1"}'
    assert content.chunk_sizes == [8192]
    assert session.closed == 1
    url, kwargs = session.posts[0]
    assert url == "https://core.example/v1/messages"
    assert kwargs["allow_redirects"] is False
    assert kwargs["headers"] == {
        "Accept": "application/json",
        "Authorization": "Bearer test-token",
        "Content-Type": "application/json",
    }
    assert timeouts[0]["timeout"].total == 20.0


@pytest.mark.asyncio
async def test_stream_closes_response_and_its_owned_session(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _Response(_Content(chunks=(b"event: final\n", b"data: {}\n\n")), content_type="text/event-stream")
    session = _Session(response)
    timeouts = _patch_session(monkeypatch, session)
    transport = AiohttpCoreHttpTransport("https://core.example/", "test-token")

    stream = await transport.get_event_stream("/v1/runs/run-1/events", allow_redirects=False)
    assert [chunk async for chunk in stream.iter_bytes()] == [b"event: final\n", b"data: {}\n\n"]
    await stream.close()
    await stream.close()

    assert session.closed == 1
    assert response.released == 1
    assert session.gets[0][0] == "https://core.example/v1/runs/run-1/events"
    assert session.gets[0][1]["allow_redirects"] is False
    timeout = timeouts[0]["timeout"]
    assert timeout.total is None
    assert timeout.connect == timeout.sock_connect == timeout.sock_read == 20.0


@pytest.mark.asyncio
async def test_stream_close_still_closes_session_when_response_release_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _Response(
        _Content(),
        content_type="text/event-stream",
        release_error=RuntimeError("internal-only"),
    )
    session = _Session(response)
    _patch_session(monkeypatch, session)
    transport = AiohttpCoreHttpTransport("https://core.example", "test-token")

    stream = await transport.get_event_stream("/v1/runs/run-1/events", allow_redirects=False)
    with pytest.raises(CoreHttpTransportError, match="close failed"):
        await stream.close()

    assert response.released == 1
    assert session.closed == 1


@pytest.mark.asyncio
async def test_post_rejects_oversized_response_before_return(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Session(_Response(_Content(b"x" * (MAX_CORE_RUN_RESPONSE_BYTES + 1))))
    _patch_session(monkeypatch, session)
    transport = AiohttpCoreHttpTransport("https://core.example", "test-token")

    with pytest.raises(CoreHttpTransportError, match="size limit"):
        await transport.post_json("/v1/messages", body=b"{}", allow_redirects=False)

    assert session.closed == 1


def test_origin_path_and_redirect_validation_fail_closed() -> None:
    for invalid_origin in (
        "http://core.example",
        "http://localhost.example",
        "https://core.example/base",
        "https://user@core.example",
        "https://core.example?target=x",
        "ftp://core.example",
    ):
        with pytest.raises(ValueError):
            AiohttpCoreHttpTransport(invalid_origin, "test-token")

    transport = AiohttpCoreHttpTransport("https://core.example", "test-token")
    with pytest.raises(CoreHttpTransportError, match="redirect"):
        transport._request_url("/v1/messages", allow_redirects=True)
    with pytest.raises(CoreHttpTransportError, match="path"):
        transport._request_url("//other.example/path", allow_redirects=False)


def test_exact_localhost_is_an_explicit_unauthenticated_loopback() -> None:
    transport = AiohttpCoreHttpTransport(
        "http://localhost:8001",
        "",
        allow_unauthenticated_loopback=True,
    )

    assert transport._origin == "http://localhost:8001"
    assert transport._authorization is None


@pytest.mark.asyncio
async def test_explicit_unauthenticated_mode_is_loopback_only_and_omits_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(_Response(_Content(b'{"run_id":"run-1"}')))
    _patch_session(monkeypatch, session)
    transport = AiohttpCoreHttpTransport(
        "http://127.0.0.1:8001",
        "",
        allow_unauthenticated_loopback=True,
    )

    await transport.post_json("/v1/messages", body=b"{}", allow_redirects=False)

    assert session.posts[0][1]["headers"] == {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    with pytest.raises(ValueError):
        AiohttpCoreHttpTransport(
            "https://core.example",
            "",
            allow_unauthenticated_loopback=True,
        )
    with pytest.raises(ValueError):
        AiohttpCoreHttpTransport("http://127.0.0.1:8001", "")
