from __future__ import annotations

import json
from types import SimpleNamespace

import aiohttp
import pytest

from yonerai_discord.modules.earthquake import HISTORY_URL, WEBSOCKET_URL, P2PQuakeClient
from yonerai_discord.modules.earthquake.client import HistoryResponseTooLargeError


class FakeContent:
    def __init__(self, chunks) -> None:
        self.chunks = list(chunks)
        self.requested_sizes = []

    async def iter_chunked(self, size):
        self.requested_sizes.append(size)
        for chunk in self.chunks:
            yield chunk


class FakeResponse:
    def __init__(self, payload=None, *, chunks=None, content_length="auto", status=200, history=()) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8") if chunks is None else None
        self.content = FakeContent([encoded] if chunks is None else chunks)
        self.content_length = len(encoded) if content_length == "auto" and encoded is not None else content_length
        self.status = status
        self.history = history
        self.released = False

    def raise_for_status(self) -> None:
        return None

    def release(self) -> None:
        self.released = True


class FakeWebSocket:
    def __init__(self, messages) -> None:
        self.messages = list(messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)

    def exception(self):
        return None


class WebSocketContext:
    def __init__(self, websocket) -> None:
        self.websocket = websocket
        self.exited = False

    async def __aenter__(self):
        return self.websocket

    async def __aexit__(self, *_args):
        self.exited = True


class FakeSession:
    def __init__(self, history=None, messages=(), *, response=None) -> None:
        self.response = response or FakeResponse([] if history is None else history)
        self.websocket_context = WebSocketContext(FakeWebSocket(messages))
        self.get_calls = []
        self.ws_calls = []

    async def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.response

    def ws_connect(self, url, **kwargs):
        self.ws_calls.append((url, kwargs))
        return self.websocket_context


class FakeOwnedSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_owned_session_rejects_http_and_websocket_redirects(monkeypatch) -> None:
    captured = {}
    owned = FakeOwnedSession()

    def build_session(**kwargs):
        captured.update(kwargs)
        return owned

    monkeypatch.setattr(aiohttp, "ClientSession", build_session)
    client = P2PQuakeClient()
    await client.start()
    try:
        assert captured["trust_env"] is False
        trace_configs = captured["trace_configs"]
        assert len(trace_configs) == 1
        callbacks = list(trace_configs[0].on_request_redirect)
        assert len(callbacks) == 1
        with pytest.raises(ConnectionError, match="redirect"):
            await callbacks[0](None, None, None)
    finally:
        await client.close()
    assert owned.closed is True


@pytest.mark.asyncio
async def test_history_uses_fixed_endpoint_and_bounded_query() -> None:
    session = FakeSession([{"id": "1"}, "ignored"])
    client = P2PQuakeClient(session=session)
    await client.start()
    rows = await client.fetch_history(codes=(556, 551), limit=5)
    assert rows == ({"id": "1"},)
    assert session.get_calls == [
        (
            HISTORY_URL,
            {
                "params": [("codes", "556"), ("codes", "551"), ("limit", "5")],
                "allow_redirects": False,
            },
        ),
    ]
    assert session.response.released is True
    await client.close()


@pytest.mark.asyncio
async def test_history_stream_limit_does_not_trust_missing_content_length() -> None:
    response = FakeResponse(chunks=[b"[", b"{}", b",{}", b"]"], content_length=None)
    session = FakeSession(response=response)
    client = P2PQuakeClient(session=session, max_history_response_bytes=5)
    await client.start()
    with pytest.raises(HistoryResponseTooLargeError):
        await client.fetch_history(limit=2)
    assert response.released is True


@pytest.mark.asyncio
async def test_history_rejects_declared_oversize_before_reading() -> None:
    response = FakeResponse(chunks=[b"[]"], content_length=100)
    session = FakeSession(response=response)
    client = P2PQuakeClient(session=session, max_history_response_bytes=8)
    await client.start()
    with pytest.raises(HistoryResponseTooLargeError):
        await client.fetch_history(limit=1)
    assert response.content.requested_sizes == []


@pytest.mark.asyncio
async def test_history_rejects_redirect_without_reading_body() -> None:
    response = FakeResponse(chunks=[b"[]"], status=302)
    session = FakeSession(response=response)
    client = P2PQuakeClient(session=session)
    await client.start()
    with pytest.raises(ValueError, match="redirect"):
        await client.fetch_history(limit=1)
    assert response.content.requested_sizes == []
    assert response.released is True


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"[NaN]", b"[1e999]"])
async def test_history_rejects_non_finite_json(body: bytes) -> None:
    response = FakeResponse(chunks=[body], content_length=len(body))
    client = P2PQuakeClient(session=FakeSession(response=response))
    await client.start()
    with pytest.raises(ValueError, match="valid JSON"):
        await client.fetch_history(limit=1)
    assert response.released is True


@pytest.mark.asyncio
async def test_websocket_reads_only_text_json_objects() -> None:
    messages = [
        SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data="not-json"),
        SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps([1, 2, 3])),
        SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps({"id": "ok"})),
        SimpleNamespace(type=aiohttp.WSMsgType.CLOSED, data=None),
    ]
    session = FakeSession([], messages)
    client = P2PQuakeClient(session=session, heartbeat_seconds=30)
    await client.start()
    async with client.websocket() as websocket:
        rows = [row async for row in client.iter_messages(websocket)]
    assert rows == [{"id": "ok"}]
    assert session.ws_calls == [
        (WEBSOCKET_URL, {"heartbeat": 30, "max_msg_size": 1_048_576}),
    ]
    assert session.websocket_context.exited is True


@pytest.mark.asyncio
async def test_client_rejects_unbounded_or_unknown_history_requests() -> None:
    client = P2PQuakeClient(session=FakeSession([]))
    await client.start()
    with pytest.raises(ValueError):
        await client.fetch_history(codes=(999,), limit=1)
    with pytest.raises(ValueError):
        await client.fetch_history(limit=101)
