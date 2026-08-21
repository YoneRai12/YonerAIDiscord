from __future__ import annotations

import pytest

from yonerai_discord.execution_gateway.core_contract import CoreToolResultV01
from yonerai_discord.execution_gateway.core_http_transport import (
    MAX_CORE_RUN_RESPONSE_BYTES,
    CoreHttpResponse,
    CoreHttpTransportError,
    YonerAIInternalRunHttpPortV01,
)
from yonerai_discord.execution_gateway.ora_core_transport import OraCoreHttpTransport


class _InnerTransport:
    def __init__(
        self,
        responses: dict[str, CoreHttpResponse],
        *,
        stream_chunks: tuple[bytes, ...] = (),
    ) -> None:
        self.responses = responses
        self.posts: list[tuple[str, bytes, bool]] = []
        self.gets: list[tuple[str, bool]] = []
        self.stream = _Stream(stream_chunks)

    async def post_json(
        self,
        path: str,
        *,
        body: bytes,
        allow_redirects: bool,
    ) -> CoreHttpResponse:
        self.posts.append((path, body, allow_redirects))
        return self.responses[path]

    async def get_event_stream(self, path: str, *, allow_redirects: bool) -> object:
        self.gets.append((path, allow_redirects))
        return self.stream


class _Stream:
    status_code = 200
    content_type = "text/event-stream; charset=utf-8"

    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks
        self.close_calls = 0

    def iter_bytes(self):
        async def iterate():
            for chunk in self.chunks:
                yield chunk

        return iterate()

    async def close(self) -> None:
        self.close_calls += 1


@pytest.mark.asyncio
async def test_actual_ora_core_message_response_is_projected_to_strict_v01_receipt() -> None:
    inner = _InnerTransport(
        {
            "/v1/messages": CoreHttpResponse(
                200,
                "application/json",
                (b'{"conversation_id":"conversation-1","message_id":"message-1","run_id":"run-1","status":"queued"}'),
            )
        }
    )
    transport = OraCoreHttpTransport(inner=inner)

    response = await transport.post_json(
        "/v1/messages",
        body=b'{"content":"hello"}',
        allow_redirects=False,
    )

    assert response == CoreHttpResponse(200, "application/json", b'{"run_id":"run-1"}')
    assert inner.posts == [("/v1/messages", b'{"content":"hello"}', False)]
    assert repr(transport) == "OraCoreHttpTransport()"


@pytest.mark.asyncio
async def test_actual_ora_core_result_receipt_is_projected_only_when_accepted() -> None:
    accepted = _InnerTransport(
        {
            "/v1/runs/run-1/results": CoreHttpResponse(
                200,
                "application/json; charset=utf-8",
                b'{"status":"ok","accepted":true,"continuation_only":true}',
            )
        }
    )
    transport = OraCoreHttpTransport(inner=accepted)

    response = await transport.post_json(
        "/v1/runs/run-1/results",
        body=b"{}",
        allow_redirects=False,
    )

    assert response.body == b'{"accepted":true}'

    strict_accepted = _InnerTransport(
        {
            "/v1/runs/run-1/results": CoreHttpResponse(
                202,
                "application/json",
                b'{"accepted":true}',
            )
        }
    )
    strict_response = await OraCoreHttpTransport(inner=strict_accepted).post_json(
        "/v1/runs/run-1/results",
        body=b"{}",
        allow_redirects=False,
    )
    assert strict_response == CoreHttpResponse(202, "application/json", b'{"accepted":true}')

    rejected = _InnerTransport(
        {
            "/v1/runs/run-1/results": CoreHttpResponse(
                200,
                "application/json",
                b'{"status":"ok","accepted":false,"continuation_only":true}',
            )
        }
    )
    with pytest.raises(CoreHttpTransportError, match="result response"):
        await OraCoreHttpTransport(inner=rejected).post_json(
            "/v1/runs/run-1/results",
            body=b"{}",
            allow_redirects=False,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type",
    (
        "",
        "application/json",
        "application/json; charset=utf-8",
        " Application/JSON ; Charset=UTF-8 ",
        'application/json ; CHARSET="UTF-8"',
    ),
)
async def test_empty_result_receipt_accepts_supported_204_content_types(
    content_type: str,
) -> None:
    expected = CoreHttpResponse(204, content_type, b"")
    inner = _InnerTransport({"/v1/runs/run-1/results": expected})

    actual = await OraCoreHttpTransport(inner=inner).post_json(
        "/v1/runs/run-1/results",
        body=b"{}",
        allow_redirects=False,
    )

    assert actual == CoreHttpResponse(
        204,
        "" if content_type == "" else "application/json",
        b"",
    )


@pytest.mark.asyncio
async def test_parameterized_empty_204_remains_valid_through_the_v01_port() -> None:
    inner = _InnerTransport(
        {
            "/v1/runs/run-1/results": CoreHttpResponse(
                204,
                " Application/JSON ; Charset=UTF-8 ",
                b"",
            )
        }
    )
    port = YonerAIInternalRunHttpPortV01(OraCoreHttpTransport(inner=inner))

    await port.submit_result(
        "run-1",
        CoreToolResultV01(
            tool="discord.read.v1",
            result={"output": "ok"},
            tool_call_id="call-1",
        ),
    )

    assert [post[0] for post in inner.posts] == ["/v1/runs/run-1/results"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    (
        CoreHttpResponse(204.0, "application/json", b""),
        CoreHttpResponse(204, "application/json", b'{"accepted":true}'),
        CoreHttpResponse(204, "text/plain", b""),
        CoreHttpResponse(204, "application/json; charset=iso-8859-1", b""),
        CoreHttpResponse(204, "application/json; profile=result", b""),
        CoreHttpResponse(204, "application/json", bytearray()),
        CoreHttpResponse(200, "application/json", b""),
        CoreHttpResponse(202, "application/json; charset=utf-8", b""),
        CoreHttpResponse(200, "application/json", b'{"detail":"private-result-fragment"'),
        CoreHttpResponse(200, "application/json", b'{"accepted":1}'),
        CoreHttpResponse(200, "application/json", b'{"accepted":1.0}'),
        CoreHttpResponse(
            200,
            "application/json",
            b'{"status":"ok","accepted":1,"continuation_only":true}',
        ),
        CoreHttpResponse(
            200,
            "application/json",
            b'{"status":"ok","accepted":1.0,"continuation_only":true}',
        ),
        CoreHttpResponse(
            200,
            "application/json",
            b'{"status":"ok","accepted":true,"continuation_only":1}',
        ),
        CoreHttpResponse(
            200,
            "application/json",
            b'{"status":"ok","accepted":true,"continuation_only":1.0}',
        ),
        CoreHttpResponse(
            200,
            "application/json",
            b"x" * (MAX_CORE_RUN_RESPONSE_BYTES + 1),
        ),
    ),
)
async def test_result_receipt_rejects_invalid_http_shapes_without_leaking_body(
    response: CoreHttpResponse,
) -> None:
    inner = _InnerTransport({"/v1/runs/run-1/results": response})

    with pytest.raises(CoreHttpTransportError, match="result response") as captured:
        await OraCoreHttpTransport(inner=inner).post_json(
            "/v1/runs/run-1/results",
            body=b"{}",
            allow_redirects=False,
        )

    assert "private-result-fragment" not in str(captured.value)


@pytest.mark.asyncio
async def test_adapter_rejects_unknown_response_shapes_and_never_echoes_body() -> None:
    private = "private-response-fragment"
    invalid_bodies = (
        b'{"run_id":"run-1","extra":true}',
        b'{"run_id":"run-1","run_id":"run-2"}',
        b'{"conversation_id":"c","message_id":"m","run_id":"run/escape","status":"queued"}',
        (f'{{"detail":"{private}"}}').encode(),
    )
    for body in invalid_bodies:
        inner = _InnerTransport({"/v1/messages": CoreHttpResponse(200, "application/json", body)})
        with pytest.raises(CoreHttpTransportError) as captured:
            await OraCoreHttpTransport(inner=inner).post_json(
                "/v1/messages",
                body=b"{}",
                allow_redirects=False,
            )
        assert private not in str(captured.value)


@pytest.mark.asyncio
async def test_event_stream_is_delegated_without_shape_widening() -> None:
    private_download = "https://private.example/download"
    inner = _InnerTransport(
        {},
        stream_chunks=(
            (
                b'data: {"event":"final","data":{"output_text":"done","downloads":'
                + f'[{{"url":"{private_download}"}}]'.encode()
                + b"}}\n\n"
            ),
        ),
    )
    transport = OraCoreHttpTransport(inner=inner)

    stream = await transport.get_event_stream(
        "/v1/runs/run-1/events",
        allow_redirects=False,
    )

    chunks = [chunk async for chunk in stream.iter_bytes()]

    assert stream is not inner.stream
    assert chunks == [b'data: {"data":{"text":"done"},"event":"final"}\n\n']
    assert private_download.encode() not in b"".join(chunks)
    assert inner.gets == [("/v1/runs/run-1/events", False)]

    await stream.close()
    assert inner.stream.close_calls == 1

    with pytest.raises(CoreHttpTransportError, match="GET path"):
        await transport.get_event_stream("/health", allow_redirects=False)
    with pytest.raises(CoreHttpTransportError, match="POST path"):
        await transport.post_json("/v1/admin", body=b"{}", allow_redirects=False)
    assert inner.gets == [("/v1/runs/run-1/events", False)]
    assert inner.posts == []


@pytest.mark.asyncio
async def test_actual_ora_core_error_is_reduced_to_safe_v01_error_shape() -> None:
    inner = _InnerTransport(
        {},
        stream_chunks=(
            b'data: {"event":"error","data":{"error_code":"core_runtime_error",'
            b'"user_safe_message":"private upstream detail"}}\n\n',
        ),
    )
    stream = await OraCoreHttpTransport(inner=inner).get_event_stream(
        "/v1/runs/run-1/events",
        allow_redirects=False,
    )

    chunks = [chunk async for chunk in stream.iter_bytes()]

    assert chunks == [b'data: {"data":{"code":"core_runtime_error"},"event":"error"}\n\n']
    assert b"private upstream detail" not in b"".join(chunks)


def test_adapter_rejects_an_inner_transport_without_the_fixed_port_methods() -> None:
    with pytest.raises(TypeError):
        OraCoreHttpTransport(inner=object())
