from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping

from .core_http_transport import (
    CORE_MESSAGES_PATH,
    MAX_CORE_RUN_RESPONSE_BYTES,
    AiohttpCoreHttpTransport,
    CoreHttpEventStream,
    CoreHttpResponse,
    CoreHttpTransport,
    CoreHttpTransportError,
    _iter_sse_events,
)


_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_RESULT_PATH_RE = re.compile(r"/v1/runs/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}/results\Z")
_EVENTS_PATH_RE = re.compile(r"/v1/runs/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}/events\Z")
_RUN_STATUSES = frozenset(
    {
        "queued",
        "in_progress",
        "requires_action",
        "cancelling",
        "cancelled",
        "failed",
        "completed",
        "expired",
        "done",
    }
)


class OraCoreHttpTransport:
    """実YonerAI Coreの既知wireをstrict Internal Run v0.1へ縮約するadapter。"""

    __slots__ = ("_inner",)

    def __init__(
        self,
        origin: str | None = None,
        bearer_token: str | None = None,
        *,
        timeout_seconds: float = 20.0,
        inner: CoreHttpTransport | None = None,
    ) -> None:
        if inner is not None:
            if origin is not None or bearer_token is not None:
                raise TypeError("inner transport cannot be combined with connection settings")
            candidate = inner
        else:
            if not isinstance(origin, str) or not isinstance(bearer_token, str):
                raise TypeError("origin and bearer_token must be strings")
            candidate = AiohttpCoreHttpTransport(
                origin,
                bearer_token,
                timeout_seconds=timeout_seconds,
                allow_unauthenticated_loopback=True,
            )
        if not callable(getattr(candidate, "post_json", None)) or not callable(
            getattr(candidate, "get_event_stream", None)
        ):
            raise TypeError("inner transport must expose the Core HTTP port")
        self._inner = candidate

    def __repr__(self) -> str:
        return "OraCoreHttpTransport()"

    async def post_json(
        self,
        path: str,
        *,
        body: bytes,
        allow_redirects: bool,
    ) -> CoreHttpResponse:
        message_path = path == CORE_MESSAGES_PATH
        result_path = isinstance(path, str) and _RESULT_PATH_RE.fullmatch(path) is not None
        if not message_path and not result_path:
            raise CoreHttpTransportError("ORA Core POST path is outside the fixed v0.1 contract")
        response = await self._inner.post_json(
            path,
            body=body,
            allow_redirects=allow_redirects,
        )
        if message_path:
            return _message_receipt(response)
        if result_path:
            return _result_receipt(response)
        raise AssertionError("unreachable")

    async def get_event_stream(self, path: str, *, allow_redirects: bool):
        if not isinstance(path, str) or _EVENTS_PATH_RE.fullmatch(path) is None:
            raise CoreHttpTransportError("ORA Core GET path is outside the fixed v0.1 contract")
        stream = await self._inner.get_event_stream(path, allow_redirects=allow_redirects)
        return _OraCoreEventStream(stream)


class _OraCoreEventStream:
    __slots__ = ("_inner",)

    def __init__(self, inner: CoreHttpEventStream) -> None:
        if (
            not callable(getattr(inner, "iter_bytes", None))
            or not callable(getattr(inner, "close", None))
            or isinstance(getattr(inner, "status_code", None), bool)
            or not isinstance(getattr(inner, "status_code", None), int)
            or not isinstance(getattr(inner, "content_type", None), str)
        ):
            raise CoreHttpTransportError("ORA Core event stream is invalid")
        self._inner = inner

    @property
    def status_code(self) -> int:
        return self._inner.status_code

    @property
    def content_type(self) -> str:
        return self._inner.content_type

    def iter_bytes(self):
        async def iterate():
            chunks = self._inner.iter_bytes()
            if not hasattr(chunks, "__aiter__"):
                raise CoreHttpTransportError("ORA Core event stream is invalid")
            async for event in _iter_sse_events(chunks):
                normalized = _event_envelope(event)
                rendered = json.dumps(
                    normalized,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8", errors="strict")
                yield b"data: " + rendered + b"\n\n"

        return iterate()

    async def close(self) -> None:
        await self._inner.close()


def _event_envelope(event: Mapping[str, object]) -> dict[str, object]:
    copied = dict(event)
    if set(copied) != {"event", "data"} or not isinstance(copied["event"], str):
        raise CoreHttpTransportError("ORA Core event envelope is invalid")
    kind = copied["event"].strip()
    data = copied["data"]
    if not kind or not isinstance(data, Mapping):
        raise CoreHttpTransportError("ORA Core event envelope is invalid")
    normalized = dict(data)
    if kind == "final":
        normalized = _final_event_data(normalized)
    elif kind == "error":
        normalized = _error_event_data(normalized)
    return {"event": kind, "data": normalized}


def _final_event_data(data: dict[str, object]) -> dict[str, object]:
    keys = set(data)
    if keys.issubset({"text", "artifacts"}):
        return data
    if keys in ({"output_text"}, {"output_text", "downloads"}):
        text = data["output_text"]
        if not isinstance(text, str):
            raise CoreHttpTransportError("ORA Core final event is invalid")
        downloads = data.get("downloads", [])
        if (
            not isinstance(downloads, list)
            or len(downloads) > 32
            or any(not isinstance(item, Mapping) for item in downloads)
        ):
            raise CoreHttpTransportError("ORA Core final event is invalid")
        return {"text": text}
    if keys == {"text", "message_id", "model"}:
        text = data["text"]
        if not isinstance(text, str):
            raise CoreHttpTransportError("ORA Core final event is invalid")
        _bounded_text(data["message_id"], maximum=128)
        _bounded_text(data["model"], maximum=256)
        return {"text": text}
    raise CoreHttpTransportError("ORA Core final event is invalid")


def _error_event_data(data: dict[str, object]) -> dict[str, object]:
    keys = set(data)
    if keys.issubset({"code", "error_type"}):
        return data
    if keys == {"error_code", "user_safe_message"}:
        code = _bounded_text(data["error_code"], maximum=128)
        _bounded_text(data["user_safe_message"], maximum=512)
        return {"code": code}
    if keys == {"text"}:
        _bounded_text(data["text"], maximum=512)
        return {"error_type": "core_error"}
    raise CoreHttpTransportError("ORA Core error event is invalid")


def _message_receipt(response: CoreHttpResponse) -> CoreHttpResponse:
    payload = _response_object(response, label="message response")
    if set(payload) == {"run_id"}:
        run_id = payload["run_id"]
    elif set(payload) == {"conversation_id", "message_id", "run_id", "status"}:
        _bounded_text(payload["conversation_id"], maximum=512)
        _bounded_text(payload["message_id"], maximum=128)
        status = payload["status"]
        if status not in _RUN_STATUSES:
            raise CoreHttpTransportError("ORA Core message response is invalid")
        run_id = payload["run_id"]
    else:
        raise CoreHttpTransportError("ORA Core message response is invalid")
    if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
        raise CoreHttpTransportError("ORA Core message response is invalid")
    return CoreHttpResponse(
        response.status_code,
        "application/json",
        json.dumps({"run_id": run_id}, separators=(",", ":"), sort_keys=True).encode("ascii"),
    )


def _result_receipt(response: CoreHttpResponse) -> CoreHttpResponse:
    if (
        isinstance(response, CoreHttpResponse)
        and not isinstance(response.status_code, bool)
        and isinstance(response.status_code, int)
        and response.status_code == 204
    ):
        if (
            isinstance(response.body, bytes)
            and response.body == b""
            and isinstance(response.content_type, str)
            and (response.content_type == "" or _json_content_type(response.content_type))
        ):
            return CoreHttpResponse(
                204,
                "" if response.content_type == "" else "application/json",
                b"",
            )
        raise CoreHttpTransportError("ORA Core result response is invalid")
    payload = _response_object(response, label="result response")
    if set(payload) == {"accepted"} and payload["accepted"] is True:
        return response
    if not (
        set(payload) == {"status", "accepted", "continuation_only"}
        and payload["status"] == "ok"
        and payload["accepted"] is True
        and payload["continuation_only"] is True
    ):
        raise CoreHttpTransportError("ORA Core result response is invalid")
    return CoreHttpResponse(response.status_code, "application/json", b'{"accepted":true}')


def _response_object(response: CoreHttpResponse, *, label: str) -> dict[str, object]:
    if (
        not isinstance(response, CoreHttpResponse)
        or isinstance(response.status_code, bool)
        or not isinstance(response.status_code, int)
        or not 200 <= response.status_code < 300
        or not _json_content_type(response.content_type)
        or not isinstance(response.body, bytes)
        or len(response.body) > MAX_CORE_RUN_RESPONSE_BYTES
    ):
        raise CoreHttpTransportError(f"ORA Core {label} is invalid")
    try:
        payload = json.loads(
            response.body.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        _reject_non_finite(payload)
    except (UnicodeDecodeError, TypeError, ValueError):
        raise CoreHttpTransportError(f"ORA Core {label} is invalid") from None
    if not isinstance(payload, dict):
        raise CoreHttpTransportError(f"ORA Core {label} is invalid")
    return payload


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(value)


def _reject_non_finite(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite value")
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_non_finite(item)
    elif isinstance(value, list):
        for item in value:
            _reject_non_finite(item)


def _bounded_text(value: object, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise CoreHttpTransportError("ORA Core message response is invalid")
    return value


def _json_content_type(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parts = [part.strip().casefold() for part in value.split(";")]
    return (
        bool(parts)
        and parts[0] == "application/json"
        and all(part in {"charset=utf-8", 'charset="utf-8"'} for part in parts[1:])
    )


__all__ = ["OraCoreHttpTransport"]
