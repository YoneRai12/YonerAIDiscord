from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from yonerai_discord.execution_gateway.core_contract import (
    CoreMessageRequestV01,
    CoreToolResultV01,
)
from yonerai_discord.execution_gateway.core_http_transport import (
    CORE_MESSAGES_PATH,
    CoreHttpResponse,
    CoreHttpTransportError,
    YonerAIInternalRunHttpPortV01,
)


class _Stream:
    status_code = 200
    content_type = "text/event-stream; charset=utf-8"

    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks
        self.close_calls = 0

    def iter_bytes(self) -> AsyncIterator[bytes]:
        async def iterate() -> AsyncIterator[bytes]:
            for chunk in self.chunks:
                yield chunk

        return iterate()

    async def close(self) -> None:
        self.close_calls += 1


class _Transport:
    def __init__(
        self,
        stream: _Stream,
        *,
        message_response: CoreHttpResponse | None = None,
        result_response: CoreHttpResponse | None = None,
    ) -> None:
        self.stream = stream
        self.message_response = message_response or CoreHttpResponse(
            202,
            "application/json",
            b'{"run_id":"run-v01-1"}',
        )
        self.result_response = result_response or CoreHttpResponse(204, "", b"")
        self.posts: list[tuple[str, bytes, bool]] = []
        self.gets: list[tuple[str, bool]] = []

    async def post_json(
        self,
        path: str,
        *,
        body: bytes,
        allow_redirects: bool,
    ) -> CoreHttpResponse:
        self.posts.append((path, body, allow_redirects))
        if path == CORE_MESSAGES_PATH:
            return self.message_response
        return self.result_response

    async def get_event_stream(
        self,
        path: str,
        *,
        allow_redirects: bool,
    ) -> _Stream:
        self.gets.append((path, allow_redirects))
        return self.stream


def _message() -> CoreMessageRequestV01:
    return CoreMessageRequestV01(
        content="hello",
        conversation_id="guild:100:channel:200:user:300",
        user_identity={"provider": "discord", "id": "300"},
        attachments=(),
        idempotency_key="discord:message_create:400",
        preferred_model=None,
        history_override=None,
    )


@pytest.mark.asyncio
async def test_http_port_uses_only_three_v01_endpoints_and_exact_bodies() -> None:
    stream = _Stream((b'event: final\ndata: {"text":"done"}\n\n',))
    transport = _Transport(stream)
    port = YonerAIInternalRunHttpPortV01(transport)

    reference = await port.start(_message())
    events = [event async for event in port.events(reference.run_id)]
    result = CoreToolResultV01(
        tool="discord.read.v1",
        result={"output": "ok"},
        tool_call_id="call-1",
    )
    await port.submit_result(reference.run_id, result)
    cancel = await port.cancel(reference.run_id)

    assert reference.run_id == "run-v01-1"
    assert events == [{"event": "final", "data": {"text": "done"}}]
    assert transport.gets == [("/v1/runs/run-v01-1/events", False)]
    assert [post[0] for post in transport.posts] == [
        "/v1/messages",
        "/v1/runs/run-v01-1/results",
    ]
    assert all(post[2] is False for post in transport.posts)
    message_body = json.loads(transport.posts[0][1])
    assert set(message_body) == {
        "content",
        "conversation_id",
        "user_identity",
        "attachments",
        "idempotency_key",
        "preferred_model",
        "history_override",
    }
    assert not set(message_body) & {
        "schema",
        "source",
        "context_binding",
        "client_context",
        "request_meta",
        "route_hint",
    }
    assert json.loads(transport.posts[1][1]) == {
        "tool": "discord.read.v1",
        "result": {"output": "ok"},
        "tool_call_id": "call-1",
    }
    assert cancel.disposition.value == "unsupported"
    assert len(transport.posts) == 2
    assert stream.close_calls == 1


@pytest.mark.asyncio
async def test_http_port_treats_422_and_ambiguous_result_receipt_as_failure() -> None:
    rejected = _Transport(
        _Stream((b"event: final\ndata: {}\n\n",)),
        message_response=CoreHttpResponse(
            422,
            "application/json",
            b'{"detail":"private request body fragment"}',
        ),
    )
    with pytest.raises(CoreHttpTransportError) as captured:
        await YonerAIInternalRunHttpPortV01(rejected).start(_message())
    assert "private request body fragment" not in str(captured.value)

    ambiguous = _Transport(
        _Stream((b"event: final\ndata: {}\n\n",)),
        result_response=CoreHttpResponse(
            200,
            "application/json",
            b'{"accepted":true,"receipt":"unversioned"}',
        ),
    )
    port = YonerAIInternalRunHttpPortV01(ambiguous)
    reference = await port.start(_message())
    with pytest.raises(CoreHttpTransportError):
        await port.submit_result(
            reference.run_id,
            CoreToolResultV01("discord.read.v1", {"output": "ok"}, "call-1"),
        )


@pytest.mark.asyncio
async def test_v01_stream_has_an_overall_deadline_and_closes_on_timeout() -> None:
    class WaitingStream(_Stream):
        def iter_bytes(self) -> AsyncIterator[bytes]:
            async def iterate() -> AsyncIterator[bytes]:
                await asyncio.Event().wait()
                yield b""

            return iterate()

    stream = WaitingStream(())
    port = YonerAIInternalRunHttpPortV01(
        _Transport(stream),
        stream_total_timeout_seconds=0.01,
    )
    reference = await port.start(_message())

    with pytest.raises(CoreHttpTransportError, match="event stream failed"):
        assert [event async for event in port.events(reference.run_id)]
    assert stream.close_calls == 1
