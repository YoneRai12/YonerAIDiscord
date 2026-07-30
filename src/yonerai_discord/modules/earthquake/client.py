from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

import aiohttp


API_BASE_URL = "https://api.p2pquake.net/v2"
HISTORY_URL = f"{API_BASE_URL}/history"
WEBSOCKET_URL = "wss://api.p2pquake.net/v2/ws"
ALLOWED_CODES = frozenset({551, 556})
DEFAULT_MAX_HISTORY_RESPONSE_BYTES = 1_048_576
_USER_AGENT = "YonerAI-Discord-Suite/0.1 (P2PQuake-client)"


class HistoryResponseTooLargeError(ValueError):
    pass


class P2PQuakeClient:
    """固定endpointだけを扱う、テスト時にsessionを差し替え可能なtransport。"""

    def __init__(
        self,
        *,
        session: Any | None = None,
        timeout_seconds: float = 10.0,
        heartbeat_seconds: float | None = None,
        max_history_response_bytes: int = DEFAULT_MAX_HISTORY_RESPONSE_BYTES,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if heartbeat_seconds is not None and heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive or None")
        if (
            isinstance(max_history_response_bytes, bool)
            or not isinstance(max_history_response_bytes, int)
            or not 1 <= max_history_response_bytes <= 8_388_608
        ):
            raise ValueError("max_history_response_bytes must be between 1 and 8388608")
        self._session = session
        self._owns_session = session is None
        self.timeout_seconds = float(timeout_seconds)
        self.heartbeat_seconds = heartbeat_seconds
        self.max_history_response_bytes = max_history_response_bytes

    @property
    def session(self) -> Any | None:
        return self._session

    async def start(self) -> None:
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            trace_config = aiohttp.TraceConfig()
            trace_config.on_request_redirect.append(_reject_aiohttp_redirect)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                trace_configs=[trace_config],
                trust_env=False,
                headers={"User-Agent": _USER_AGENT},
            )

    async def close(self) -> None:
        session = self._session
        if session is not None and self._owns_session:
            close = getattr(session, "close", None)
            if callable(close):
                result = close()
                if hasattr(result, "__await__"):
                    await result
        self._session = None

    async def fetch_history(
        self,
        *,
        codes: Sequence[int] = (551, 556),
        limit: int = 20,
    ) -> tuple[Mapping[str, Any], ...]:
        normalized_codes = _codes(codes)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        session = self._required_session()
        params = [("codes", str(code)) for code in normalized_codes]
        params.append(("limit", str(limit)))
        response = await session.get(
            HISTORY_URL,
            params=params,
            allow_redirects=False,
        )
        try:
            status = getattr(response, "status", None)
            if getattr(response, "history", ()) or (isinstance(status, int) and 300 <= status < 400):
                raise ValueError("P2PQuake history redirect was rejected")
            response.raise_for_status()
            payload = await self._read_bounded_json(response)
        finally:
            release = getattr(response, "release", None)
            if callable(release):
                release()
        if not isinstance(payload, list):
            raise ValueError("P2PQuake history response must be an array")
        return tuple(item for item in payload[:limit] if isinstance(item, Mapping))

    async def _read_bounded_json(self, response: Any) -> object:
        content_length = getattr(response, "content_length", None)
        if (
            isinstance(content_length, int)
            and not isinstance(content_length, bool)
            and content_length > self.max_history_response_bytes
        ):
            raise HistoryResponseTooLargeError("P2PQuake history response exceeds the byte limit")
        content = getattr(response, "content", None)
        iter_chunked = getattr(content, "iter_chunked", None)
        if not callable(iter_chunked):
            raise ValueError("P2PQuake history response is not stream-readable")
        body = bytearray()
        chunk_size = min(65_536, self.max_history_response_bytes + 1)
        async for chunk in iter_chunked(chunk_size):
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise ValueError("P2PQuake history response contained a non-byte chunk")
            if len(body) + len(chunk) > self.max_history_response_bytes:
                raise HistoryResponseTooLargeError("P2PQuake history response exceeds the byte limit")
            body.extend(chunk)
        try:
            text = body.decode("utf-8")
            payload = json.loads(text, parse_constant=_reject_json_constant)
            _reject_non_finite_json(payload)
            return payload
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise ValueError("P2PQuake history response is not valid JSON") from exc

    @asynccontextmanager
    async def websocket(self) -> AsyncIterator[Any]:
        session = self._required_session()
        kwargs: dict[str, Any] = {}
        if self.heartbeat_seconds is not None:
            kwargs["heartbeat"] = self.heartbeat_seconds
        kwargs["max_msg_size"] = self.max_history_response_bytes
        async with session.ws_connect(WEBSOCKET_URL, **kwargs) as websocket:
            yield websocket

    async def iter_messages(self, websocket: Any) -> AsyncIterator[Mapping[str, Any]]:
        async for message in websocket:
            if message.type == aiohttp.WSMsgType.TEXT:
                try:
                    payload = json.loads(message.data, parse_constant=_reject_json_constant)
                    _reject_non_finite_json(payload)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(payload, Mapping):
                    yield payload
            elif message.type == aiohttp.WSMsgType.ERROR:
                error = getattr(websocket, "exception", lambda: None)()
                raise ConnectionError(type(error).__name__ if error is not None else "websocket error")
            elif message.type in {
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
            }:
                break

    def _required_session(self) -> Any:
        if self._session is None:
            raise RuntimeError("P2PQuake client is not started")
        return self._session


def _codes(codes: Sequence[int]) -> tuple[int, ...]:
    normalized: list[int] = []
    for code in codes:
        if isinstance(code, bool) or not isinstance(code, int) or code not in ALLOWED_CODES:
            raise ValueError("only P2PQuake codes 551 and 556 are supported")
        if code not in normalized:
            normalized.append(code)
    if not normalized:
        raise ValueError("at least one code is required")
    return tuple(normalized)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


async def _reject_aiohttp_redirect(_session: object, _trace_context: object, _params: object) -> None:
    """aiohttpのWebSocket handshakeを含む全redirectを追従前に拒否する。"""

    raise ConnectionError("P2PQuake endpoint redirect was rejected")


def _reject_non_finite_json(payload: object) -> None:
    stack = [payload]
    nodes = 0
    while stack:
        value = stack.pop()
        nodes += 1
        if nodes > 200_000:
            raise ValueError("P2PQuake response is too complex")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("P2PQuake response contains a non-finite number")
        if isinstance(value, Mapping):
            stack.extend(value.keys())
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
