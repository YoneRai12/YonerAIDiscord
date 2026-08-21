from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

import aiohttp

from yonerai_discord.secret_policy import is_loopback_endpoint

from .core_contract import (
    CoreCancelOutcomeV01,
    CoreMessageRequestV01,
    CoreRunReferenceV01,
    CoreRunRequest,
    CoreToolResultV01,
)


CORE_MESSAGES_PATH = "/v1/messages"
MAX_CORE_REQUEST_BYTES = 512 * 1024
MAX_CORE_RUN_RESPONSE_BYTES = 16 * 1024
MAX_CORE_RESULT_REQUEST_BYTES_V01 = 128 * 1024
MAX_CORE_SSE_LINE_BYTES = 16 * 1024
MAX_CORE_SSE_EVENT_BYTES = 64 * 1024
MAX_CORE_SSE_BODY_BYTES = 2 * 1024 * 1024
MAX_CORE_SSE_EVENTS = 256

_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SNOWFLAKE_RE = re.compile(r"[1-9][0-9]{0,19}\Z")
_TERMINAL_EVENTS = frozenset({"final", "error"})
_ATTACHMENT_TYPES = frozenset({"file_ref", "image_ref"})
_CONTEXT_KINDS = frozenset({"dm", "channel", "thread"})
_TRIGGERS = frozenset({"mention", "reply", "dm", "slash"})
_VISIBILITIES = frozenset({"dm", "guild_channel", "guild_thread"})


class CoreHttpTransportError(RuntimeError):
    """Core HTTP/SSE seamが固定契約を満たさなかったことを表す安全なエラー。"""


@dataclass(frozen=True, slots=True)
class CoreHttpResponse:
    status_code: int
    content_type: str
    body: bytes = field(repr=False)


class CoreHttpEventStream(Protocol):
    status_code: int
    content_type: str

    def iter_bytes(self) -> AsyncIterator[bytes]: ...

    async def close(self) -> None: ...


class AiohttpCoreHttpTransport:
    """固定 origin のみに接続する最小限の aiohttp transport。"""

    __slots__ = ("_authorization", "_origin", "_post_timeout", "_stream_timeout")

    def __init__(
        self,
        origin: str,
        bearer_token: str,
        *,
        timeout_seconds: float = 20.0,
        allow_unauthenticated_loopback: bool = False,
    ) -> None:
        self._origin = _validated_origin(origin)
        if type(allow_unauthenticated_loopback) is not bool:
            raise TypeError("allow_unauthenticated_loopback must be a boolean")
        if bearer_token == "" and allow_unauthenticated_loopback and is_loopback_endpoint(self._origin):
            self._authorization: str | None = None
        else:
            self._authorization = _bearer_authorization(bearer_token)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > 120
        ):
            raise ValueError("timeout_seconds must be between 0 and 120")
        timeout = float(timeout_seconds)
        self._post_timeout = aiohttp.ClientTimeout(total=timeout)
        self._stream_timeout = aiohttp.ClientTimeout(
            total=None,
            connect=timeout,
            sock_connect=timeout,
            sock_read=timeout,
        )

    def __repr__(self) -> str:
        return "AiohttpCoreHttpTransport()"

    async def post_json(
        self,
        path: str,
        *,
        body: bytes,
        allow_redirects: bool,
    ) -> CoreHttpResponse:
        url = self._request_url(path, allow_redirects=allow_redirects)
        if not isinstance(body, bytes):
            raise CoreHttpTransportError("Core request body is invalid")
        try:
            async with aiohttp.ClientSession(timeout=self._post_timeout) as session:
                headers = {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                }
                if self._authorization is not None:
                    headers["Authorization"] = self._authorization
                async with session.post(
                    url,
                    data=body,
                    headers=headers,
                    allow_redirects=False,
                ) as response:
                    response_body = await _read_bounded_response(
                        response.content,
                        maximum=MAX_CORE_RUN_RESPONSE_BYTES,
                    )
                    return CoreHttpResponse(
                        status_code=response.status,
                        content_type=response.headers.get("Content-Type", ""),
                        body=response_body,
                    )
        except asyncio.CancelledError:
            raise
        except CoreHttpTransportError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError):
            raise CoreHttpTransportError("Core HTTP request failed") from None

    async def get_event_stream(
        self,
        path: str,
        *,
        allow_redirects: bool,
    ) -> CoreHttpEventStream:
        url = self._request_url(path, allow_redirects=allow_redirects)
        session = aiohttp.ClientSession(timeout=self._stream_timeout)
        try:
            headers = {"Accept": "text/event-stream"}
            if self._authorization is not None:
                headers["Authorization"] = self._authorization
            response = await session.get(
                url,
                headers=headers,
                allow_redirects=False,
            )
            return _AiohttpCoreEventStream(session, response)
        except asyncio.CancelledError:
            await session.close()
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError):
            await session.close()
            raise CoreHttpTransportError("Core event stream request failed") from None

    def _request_url(self, path: str, *, allow_redirects: bool) -> str:
        if allow_redirects is not False:
            raise CoreHttpTransportError("Core redirects are forbidden")
        return f"{self._origin}{_validated_relative_path(path)}"


class _AiohttpCoreEventStream:
    __slots__ = ("_closed", "_response", "_session")

    def __init__(self, session: aiohttp.ClientSession, response: aiohttp.ClientResponse) -> None:
        self._session = session
        self._response = response
        self._closed = False

    @property
    def status_code(self) -> int:
        return self._response.status

    @property
    def content_type(self) -> str:
        return self._response.headers.get("Content-Type", "")

    def iter_bytes(self) -> AsyncIterator[bytes]:
        async def iterate() -> AsyncIterator[bytes]:
            async for chunk in self._response.content.iter_chunked(8192):
                yield bytes(chunk)

        return iterate()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        failed = False
        try:
            self._response.release()
        except asyncio.CancelledError:
            raise
        except Exception:
            failed = True
        finally:
            try:
                await self._session.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                failed = True
        if failed:
            raise CoreHttpTransportError("Core event stream close failed")


async def _read_bounded_response(content: object, *, maximum: int) -> bytes:
    iterator = getattr(content, "iter_chunked", None)
    if not callable(iterator):
        raise CoreHttpTransportError("Core HTTP response body is invalid")
    rendered = bytearray()
    try:
        async for chunk in iterator(min(8192, maximum + 1)):
            if not isinstance(chunk, bytes) or len(rendered) + len(chunk) > maximum:
                raise CoreHttpTransportError("Core HTTP response exceeds the size limit")
            rendered.extend(chunk)
    except asyncio.CancelledError:
        raise
    except CoreHttpTransportError:
        raise
    except (aiohttp.ClientError, TimeoutError, TypeError, ValueError):
        raise CoreHttpTransportError("Core HTTP response body is invalid") from None
    return bytes(rendered)


def _validated_origin(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("origin must be an absolute HTTP(S) origin")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("origin must be an absolute HTTP(S) origin")
    normalized = f"{parsed.scheme}://{parsed.netloc}"
    if parsed.scheme == "http" and not is_loopback_endpoint(normalized):
        raise ValueError("plain HTTP origin must be loopback")
    return normalized


def _bearer_authorization(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise ValueError("bearer_token is invalid")
    return f"Bearer {value}"


def _validated_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        raise CoreHttpTransportError("Core request path is invalid")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or ".." in parsed.path.split("/"):
        raise CoreHttpTransportError("Core request path is invalid")
    return parsed.path


class CoreHttpTransport(Protocol):
    """認証を内側に保持する、注入専用のHTTP transport。"""

    async def post_json(
        self,
        path: str,
        *,
        body: bytes,
        allow_redirects: bool,
    ) -> CoreHttpResponse: ...

    async def get_event_stream(
        self,
        path: str,
        *,
        allow_redirects: bool,
    ) -> CoreHttpEventStream: ...


class CoreHttpRunPort:
    """固定Core endpointを注入HTTP transportへ適合するoffline RunPort。"""

    __slots__ = ("_transport",)

    def __init__(self, transport: CoreHttpTransport) -> None:
        if not callable(getattr(transport, "post_json", None)) or not callable(
            getattr(transport, "get_event_stream", None)
        ):
            raise TypeError("transport must expose post_json and get_event_stream")
        self._transport = transport

    def __repr__(self) -> str:
        return "CoreHttpRunPort()"

    async def run(self, request: CoreRunRequest) -> AsyncIterator[Mapping[str, object]]:
        try:
            if not isinstance(request, CoreRunRequest):
                raise TypeError("request must be a CoreRunRequest")
            body = _canonical_request_body(request)
            response = await self._transport.post_json(
                CORE_MESSAGES_PATH,
                body=body,
                allow_redirects=False,
            )
            run_id = _run_id_from_response(response)
            stream = await self._transport.get_event_stream(
                f"/v1/runs/{run_id}/events",
                allow_redirects=False,
            )
            closed = False
            try:
                _validate_stream_response(stream)
                chunks = stream.iter_bytes()
                if not hasattr(chunks, "__aiter__"):
                    raise CoreHttpTransportError("Core event stream is invalid")
                async for event in _iter_sse_events(chunks):
                    if _is_terminal_event(event):
                        await _close_stream(stream)
                        closed = True
                        yield event
                        return
                    yield event
                raise CoreHttpTransportError("Core event stream ended without a terminal event")
            finally:
                if not closed:
                    await _close_stream(stream)
        except asyncio.CancelledError:
            raise
        except CoreHttpTransportError:
            raise
        except Exception:
            raise CoreHttpTransportError("Core HTTP transport failed") from None


class YonerAIInternalRunHttpPortV01:
    """Internal Run API v0.1の3 endpointだけを扱うstrict HTTP port。

    v0.1にはremote cancel endpointがないため、cancelはHTTP成功を発明せず
    typed unsupportedを返す。
    """

    __slots__ = ("_stream_total_timeout_seconds", "_transport")

    def __init__(
        self,
        transport: CoreHttpTransport,
        *,
        stream_total_timeout_seconds: float = 120.0,
    ) -> None:
        if not callable(getattr(transport, "post_json", None)) or not callable(
            getattr(transport, "get_event_stream", None)
        ):
            raise TypeError("transport must expose post_json and get_event_stream")
        if (
            isinstance(stream_total_timeout_seconds, bool)
            or not isinstance(stream_total_timeout_seconds, (int, float))
            or not math.isfinite(stream_total_timeout_seconds)
            or stream_total_timeout_seconds <= 0
            or stream_total_timeout_seconds > 600
        ):
            raise ValueError("stream_total_timeout_seconds must be between 0 and 600")
        self._transport = transport
        self._stream_total_timeout_seconds = float(stream_total_timeout_seconds)

    def __repr__(self) -> str:
        return "YonerAIInternalRunHttpPortV01()"

    async def start(self, request: CoreMessageRequestV01) -> CoreRunReferenceV01:
        try:
            body = _canonical_v01_message_body(request)
            response = await self._transport.post_json(
                CORE_MESSAGES_PATH,
                body=body,
                allow_redirects=False,
            )
            return CoreRunReferenceV01(_run_id_from_response(response))
        except asyncio.CancelledError:
            raise
        except CoreHttpTransportError:
            raise
        except Exception:
            raise CoreHttpTransportError("Core v0.1 message submission failed") from None

    def events(self, run_id: str) -> AsyncIterator[Mapping[str, object]]:
        async def stream_events() -> AsyncIterator[Mapping[str, object]]:
            try:
                async with asyncio.timeout(self._stream_total_timeout_seconds):
                    normalized_run_id = _identifier_text(run_id)
                    stream = await self._transport.get_event_stream(
                        f"/v1/runs/{normalized_run_id}/events",
                        allow_redirects=False,
                    )
                    closed = False
                    try:
                        _validate_stream_response(stream)
                        chunks = stream.iter_bytes()
                        if not hasattr(chunks, "__aiter__"):
                            raise CoreHttpTransportError("Core event stream is invalid")
                        async for event in _iter_sse_events(chunks):
                            if _is_terminal_event(event):
                                await _close_stream(stream)
                                closed = True
                                yield event
                                return
                            yield event
                        raise CoreHttpTransportError("Core event stream ended without a terminal event")
                    finally:
                        if not closed:
                            await _close_stream(stream)
            except asyncio.CancelledError:
                raise
            except CoreHttpTransportError:
                raise
            except Exception:
                raise CoreHttpTransportError("Core v0.1 event stream failed") from None

        return stream_events()

    async def submit_result(self, run_id: str, result: CoreToolResultV01) -> None:
        try:
            normalized_run_id = _identifier_text(run_id)
            body = _canonical_v01_result_body(result)
            response = await self._transport.post_json(
                f"/v1/runs/{normalized_run_id}/results",
                body=body,
                allow_redirects=False,
            )
            _validate_v01_result_response(response)
        except asyncio.CancelledError:
            raise
        except CoreHttpTransportError:
            raise
        except Exception:
            raise CoreHttpTransportError("Core v0.1 result submission failed") from None

    async def cancel(self, run_id: str) -> CoreCancelOutcomeV01:
        _identifier_text(run_id)
        return CoreCancelOutcomeV01.unsupported()


def _canonical_request_body(request: CoreRunRequest) -> bytes:
    _validate_core_request_contract(request)
    _validate_ref_only_attachments(request.attachments)
    try:
        rendered = json.dumps(
            request.to_mapping(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise CoreHttpTransportError("Core request JSON is invalid") from None
    if len(rendered) > MAX_CORE_REQUEST_BYTES:
        raise CoreHttpTransportError("Core request JSON exceeds the size limit")
    return rendered


def _canonical_v01_message_body(request: CoreMessageRequestV01) -> bytes:
    if not isinstance(request, CoreMessageRequestV01):
        raise CoreHttpTransportError("Core v0.1 message request is invalid")
    mapping = request.to_mapping()
    if set(mapping) != {
        "content",
        "conversation_id",
        "user_identity",
        "attachments",
        "idempotency_key",
        "preferred_model",
        "history_override",
    }:
        raise CoreHttpTransportError("Core v0.1 message request has unknown fields")
    return _canonical_json_bytes(
        mapping,
        maximum=MAX_CORE_REQUEST_BYTES,
        label="Core v0.1 message request",
    )


def _canonical_v01_result_body(result: CoreToolResultV01) -> bytes:
    if not isinstance(result, CoreToolResultV01):
        raise CoreHttpTransportError("Core v0.1 result request is invalid")
    mapping = result.to_mapping()
    if set(mapping) != {"tool", "result", "tool_call_id"}:
        raise CoreHttpTransportError("Core v0.1 result request has unknown fields")
    return _canonical_json_bytes(
        mapping,
        maximum=MAX_CORE_RESULT_REQUEST_BYTES_V01,
        label="Core v0.1 result request",
    )


def _canonical_json_bytes(
    mapping: Mapping[str, object],
    *,
    maximum: int,
    label: str,
) -> bytes:
    try:
        rendered = json.dumps(
            mapping,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise CoreHttpTransportError(f"{label} JSON is invalid") from None
    if len(rendered) > maximum:
        raise CoreHttpTransportError(f"{label} exceeds the size limit")
    return rendered


def _validate_v01_result_response(response: object) -> None:
    try:
        status_code = response.status_code
        content_type = response.content_type
        body = response.body
    except Exception:
        raise CoreHttpTransportError("Core v0.1 result response is invalid") from None
    if status_code == 204 and content_type in {"", "application/json"} and body == b"":
        return
    if not _successful_status(status_code) or not _content_type_is(content_type, "application/json"):
        raise CoreHttpTransportError("Core v0.1 result response is invalid")
    if not isinstance(body, bytes) or len(body) > MAX_CORE_RUN_RESPONSE_BYTES:
        raise CoreHttpTransportError("Core v0.1 result response is invalid")
    payload = _strict_json_object(body)
    if payload != {"accepted": True}:
        raise CoreHttpTransportError("Core v0.1 result response is invalid")


def _validate_core_request_contract(request: CoreRunRequest) -> None:
    if not isinstance(request.content, str) or not request.content.strip():
        raise CoreHttpTransportError("Core request content is invalid")
    try:
        content_size = len(request.content.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        raise CoreHttpTransportError("Core request content is invalid") from None
    if content_size > MAX_CORE_REQUEST_BYTES:
        raise CoreHttpTransportError("Core request content exceeds the size limit")
    _bounded_text(request.idempotency_key, maximum=512)

    context_binding = _exact_mapping(
        request.context_binding,
        {"provider", "kind", "external_id"},
    )
    user_identity = _exact_mapping(request.user_identity, {"provider", "id"})
    client_context = _exact_mapping(
        request.client_context,
        {
            "guild_id",
            "channel_id",
            "thread_id",
            "message_id",
            "reply_to_message_id",
            "trigger",
            "visibility",
        },
    )
    request_meta = _exact_mapping(request.request_meta, {"request_id", "trace_id", "origin"})
    route_hint = _exact_mapping(request.route_hint, {"mode"})

    if context_binding["provider"] != "discord" or user_identity["provider"] != "discord":
        raise CoreHttpTransportError("Core Discord identity is invalid")
    user_id = _snowflake_text(user_identity["id"])
    guild_id = _optional_snowflake_text(client_context["guild_id"])
    channel_id = _snowflake_text(client_context["channel_id"])
    thread_id = _optional_snowflake_text(client_context["thread_id"])
    _snowflake_text(client_context["message_id"])
    _optional_snowflake_text(client_context["reply_to_message_id"])

    trigger = client_context["trigger"]
    visibility = client_context["visibility"]
    if trigger not in _TRIGGERS or visibility not in _VISIBILITIES:
        raise CoreHttpTransportError("Core Discord client context is invalid")
    if guild_id is None:
        expected_kind = "dm"
        expected_external_id = f"dm:{channel_id}:user:{user_id}"
        if thread_id is not None or visibility != "dm":
            raise CoreHttpTransportError("Core Discord client context is invalid")
    elif thread_id is None:
        expected_kind = "channel"
        expected_external_id = f"guild:{guild_id}:channel:{channel_id}:user:{user_id}"
        if visibility != "guild_channel":
            raise CoreHttpTransportError("Core Discord client context is invalid")
    else:
        expected_kind = "thread"
        expected_external_id = f"guild:{guild_id}:channel:{channel_id}:thread:{thread_id}"
        if visibility != "guild_thread":
            raise CoreHttpTransportError("Core Discord client context is invalid")
    if (
        context_binding["kind"] not in _CONTEXT_KINDS
        or context_binding["kind"] != expected_kind
        or context_binding["external_id"] != expected_external_id
    ):
        raise CoreHttpTransportError("Core context binding is invalid")

    _identifier_text(request_meta["request_id"])
    trace_id = request_meta["trace_id"]
    if trace_id is not None:
        _identifier_text(trace_id)
    if request_meta["origin"] != "discord-gateway":
        raise CoreHttpTransportError("Core request metadata is invalid")
    _identifier_text(route_hint["mode"])


def _exact_mapping(value: object, expected_keys: set[str]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise CoreHttpTransportError("Core request mapping is invalid")
    copied = dict(value)
    if set(copied) != expected_keys:
        raise CoreHttpTransportError("Core request mapping is invalid")
    return copied


def _bounded_text(value: object, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise CoreHttpTransportError("Core request field is invalid")
    return value


def _identifier_text(value: object) -> str:
    if not isinstance(value, str) or _RUN_ID_RE.fullmatch(value) is None:
        raise CoreHttpTransportError("Core request identifier is invalid")
    return value


def _snowflake_text(value: object) -> str:
    if not isinstance(value, str) or _SNOWFLAKE_RE.fullmatch(value) is None:
        raise CoreHttpTransportError("Core Discord identifier is invalid")
    return value


def _optional_snowflake_text(value: object) -> str | None:
    if value is None:
        return None
    return _snowflake_text(value)


def _validate_ref_only_attachments(attachments: tuple[Mapping[str, str], ...]) -> None:
    seen_ids: set[str] = set()
    for attachment in attachments:
        copied = dict(attachment)
        if set(copied) != {"type", "attachment_id"} or copied.get("type") not in _ATTACHMENT_TYPES:
            raise CoreHttpTransportError("Core attachment reference is invalid")
        attachment_id = copied.get("attachment_id")
        if (
            not isinstance(attachment_id, str)
            or attachment_id != attachment_id.strip()
            or not attachment_id
            or len(attachment_id) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in attachment_id)
            or attachment_id in seen_ids
        ):
            raise CoreHttpTransportError("Core attachment reference is invalid")
        seen_ids.add(attachment_id)


def _run_id_from_response(response: object) -> str:
    try:
        status_code = response.status_code
        content_type = response.content_type
        body = response.body
    except Exception:
        raise CoreHttpTransportError("Core run response is invalid") from None
    if not _successful_status(status_code) or not _content_type_is(content_type, "application/json"):
        raise CoreHttpTransportError("Core run response is invalid")
    if not isinstance(body, bytes) or len(body) > MAX_CORE_RUN_RESPONSE_BYTES:
        raise CoreHttpTransportError("Core run response is invalid")
    payload = _strict_json_object(body)
    if set(payload) != {"run_id"}:
        raise CoreHttpTransportError("Core run response is invalid")
    run_id = payload["run_id"]
    if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
        raise CoreHttpTransportError("Core run response is invalid")
    return run_id


def _validate_stream_response(stream: object) -> None:
    try:
        status_code = stream.status_code
        content_type = stream.content_type
        iter_bytes = stream.iter_bytes
        close = stream.close
    except Exception:
        raise CoreHttpTransportError("Core event stream is invalid") from None
    if (
        not _successful_status(status_code)
        or not _content_type_is(content_type, "text/event-stream")
        or not callable(iter_bytes)
        or not callable(close)
    ):
        raise CoreHttpTransportError("Core event stream is invalid")


async def _iter_sse_events(chunks: AsyncIterator[bytes]) -> AsyncIterator[Mapping[str, object]]:
    buffer = bytearray()
    total_bytes = 0
    event_count = 0
    event_name: str | None = None
    data_lines: list[str] = []
    event_bytes = 0

    def consume_line(line: bytes) -> Mapping[str, object] | None:
        nonlocal event_name, data_lines, event_bytes
        event_bytes += len(line) + 1
        if event_bytes > MAX_CORE_SSE_EVENT_BYTES:
            raise CoreHttpTransportError("Core SSE event exceeds the size limit")
        if not line:
            if not data_lines:
                event_name = None
                event_bytes = 0
                return None
            payload = _strict_json_object("\n".join(data_lines).encode("utf-8"))
            name = event_name
            event_name = None
            data_lines = []
            event_bytes = 0
            if name:
                return {"event": name, "data": payload}
            return payload

        try:
            decoded = line.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise CoreHttpTransportError("Core SSE is not valid UTF-8") from None
        if "\x00" in decoded or decoded.startswith(":"):
            if "\x00" in decoded:
                raise CoreHttpTransportError("Core SSE line is invalid")
            return None
        field_name, separator, value = decoded.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field_name == "event":
            if event_name is not None:
                raise CoreHttpTransportError("Core SSE event field is duplicated")
            event_name = value
        elif field_name == "data":
            data_lines.append(value)
        return None

    async for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise CoreHttpTransportError("Core SSE chunk is invalid")
        total_bytes += len(chunk)
        if total_bytes > MAX_CORE_SSE_BODY_BYTES:
            raise CoreHttpTransportError("Core SSE body exceeds the size limit")
        buffer.extend(chunk)
        while True:
            line = _pop_sse_line(buffer)
            if line is None:
                if len(buffer) > MAX_CORE_SSE_LINE_BYTES + 1:
                    raise CoreHttpTransportError("Core SSE line exceeds the size limit")
                break
            if len(line) > MAX_CORE_SSE_LINE_BYTES:
                raise CoreHttpTransportError("Core SSE line exceeds the size limit")
            event = consume_line(line)
            if event is not None:
                event_count += 1
                if event_count > MAX_CORE_SSE_EVENTS:
                    raise CoreHttpTransportError("Core SSE event count exceeds the limit")
                yield event

    if buffer:
        line = _pop_sse_line(buffer, eof=True)
        if line is None or len(line) > MAX_CORE_SSE_LINE_BYTES:
            raise CoreHttpTransportError("Core SSE line exceeds the size limit")
        event = consume_line(line)
        if event is not None:
            event_count += 1
            if event_count > MAX_CORE_SSE_EVENTS:
                raise CoreHttpTransportError("Core SSE event count exceeds the limit")
            yield event
    event = consume_line(b"")
    if event is not None:
        event_count += 1
        if event_count > MAX_CORE_SSE_EVENTS:
            raise CoreHttpTransportError("Core SSE event count exceeds the limit")
        yield event


def _pop_sse_line(buffer: bytearray, *, eof: bool = False) -> bytes | None:
    for index, value in enumerate(buffer):
        if value == 0x0A:
            end = index - 1 if index and buffer[index - 1] == 0x0D else index
            line = bytes(buffer[:end])
            del buffer[: index + 1]
            return line
        if value == 0x0D:
            if index + 1 == len(buffer) and not eof:
                return None
            consume = index + 2 if index + 1 < len(buffer) and buffer[index + 1] == 0x0A else index + 1
            line = bytes(buffer[:index])
            del buffer[:consume]
            return line
    if eof and buffer:
        line = bytes(buffer)
        buffer.clear()
        return line
    return None


def _strict_json_object(body: bytes) -> dict[str, object]:
    if not isinstance(body, bytes):
        raise CoreHttpTransportError("Core JSON is invalid")
    try:
        text = body.decode("utf-8", errors="strict")
        payload = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
        _reject_non_finite_json(payload)
    except (UnicodeDecodeError, ValueError, TypeError):
        raise CoreHttpTransportError("Core JSON is invalid") from None
    if not isinstance(payload, dict):
        raise CoreHttpTransportError("Core JSON must be an object")
    return payload


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)


def _reject_non_finite_json(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite number")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_non_finite_json(key)
            _reject_non_finite_json(item)
    elif isinstance(value, list):
        for item in value:
            _reject_non_finite_json(item)


def _successful_status(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and 200 <= value < 300


def _content_type_is(value: object, expected: str) -> bool:
    if not isinstance(value, str):
        return False
    parts = [part.strip().casefold() for part in value.split(";")]
    if not parts or parts[0] != expected:
        return False
    return all(part in {"charset=utf-8", 'charset="utf-8"'} for part in parts[1:])


def _is_terminal_event(event: Mapping[str, object]) -> bool:
    kind = event.get("event") if "event" in event else event.get("kind")
    return isinstance(kind, str) and kind.strip() in _TERMINAL_EVENTS


async def _close_stream(stream: CoreHttpEventStream) -> None:
    try:
        result = stream.close()
        if not hasattr(result, "__await__"):
            raise TypeError("close must be awaitable")
        await result
    except asyncio.CancelledError:
        raise
    except CoreHttpTransportError:
        raise
    except Exception:
        raise CoreHttpTransportError("Core event stream close failed") from None


__all__ = [
    "AiohttpCoreHttpTransport",
    "CORE_MESSAGES_PATH",
    "MAX_CORE_REQUEST_BYTES",
    "MAX_CORE_RESULT_REQUEST_BYTES_V01",
    "MAX_CORE_RUN_RESPONSE_BYTES",
    "MAX_CORE_SSE_BODY_BYTES",
    "MAX_CORE_SSE_EVENT_BYTES",
    "MAX_CORE_SSE_EVENTS",
    "MAX_CORE_SSE_LINE_BYTES",
    "CoreHttpEventStream",
    "CoreHttpResponse",
    "CoreHttpRunPort",
    "CoreHttpTransport",
    "CoreHttpTransportError",
    "YonerAIInternalRunHttpPortV01",
]
