"""Google Gemini Developer API Interactions向けbounded provider。"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import aiohttp

from .domain import (
    GEMINI_INTERACTIONS_ENDPOINT,
    GEMINI_MEDIA_INSPECTION_MODEL,
    MAX_INSPECTION_INSTRUCTION_CHARS,
    MAX_INSPECTION_OUTPUT_CHARS,
    MediaInspectionInputError,
    MediaInspectionRequest,
    MediaInspectionResponseError,
    MediaInspectionResult,
    MediaInspectionUnavailableError,
)
from .urls import canonicalize_youtube_url


_MAX_OUTPUT_STEPS = 64
_MAX_CONTENT_ITEMS = 64
CallReserver = Callable[[], bool | Awaitable[bool]]


class GeminiMediaInspectionProvider:
    """1要求につきPOSTを1回だけ行うInteractions API provider。"""

    @property
    def requires_external_ai_consent(self) -> bool:
        return True

    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float = 60.0,
        max_response_bytes: int = 256 * 1024,
        call_reserver: CallReserver,
        session_factory: Callable[..., Any] = aiohttp.ClientSession,
    ) -> None:
        if (
            not isinstance(api_key, str)
            or not api_key
            or len(api_key) > 512
            or any(character.isspace() or ord(character) < 33 for character in api_key)
        ):
            raise ValueError("api_key is invalid")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0.1 <= float(timeout_seconds) <= 60.0
        ):
            raise ValueError("timeout_seconds is outside the bounded contract")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or not 1_024 <= max_response_bytes <= 1024 * 1024
        ):
            raise ValueError("max_response_bytes is outside the bounded contract")
        if not callable(session_factory):
            raise TypeError("session_factory must be callable")
        if not callable(call_reserver):
            raise TypeError("call_reserver must be callable")
        self._api_key = api_key
        self._timeout_seconds = float(timeout_seconds)
        self._max_response_bytes = max_response_bytes
        self._session_factory = session_factory
        self._call_reserver = call_reserver
        self._session: Any | None = None
        self._session_lock = asyncio.Lock()
        self._closing = False

    @property
    def closing(self) -> bool:
        return self._closing

    def begin_close(self) -> None:
        self._closing = True

    async def close(self) -> None:
        self.begin_close()
        async with self._session_lock:
            session, self._session = self._session, None
        close = getattr(session, "close", None)
        if callable(close):
            result = close()
            if hasattr(result, "__await__"):
                await result

    async def inspect(self, url: str, instruction: str) -> MediaInspectionResult:
        if self._closing:
            raise MediaInspectionUnavailableError("media inspection provider is unavailable")
        request = MediaInspectionRequest(
            video_uri=canonicalize_youtube_url(url),
            instruction=_validate_instruction(instruction),
        )
        if not await self._reserve_call():
            raise MediaInspectionUnavailableError("media inspection daily call limit was reached")
        session = await self._get_session()
        payload = {
            "model": GEMINI_MEDIA_INSPECTION_MODEL,
            "input": [
                {"type": "text", "text": request.instruction},
                {"type": "video", "uri": request.video_uri},
            ],
        }
        headers = {
            "x-goog-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        try:
            async with session.post(
                GEMINI_INTERACTIONS_ENDPOINT,
                headers=headers,
                json=payload,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                if getattr(response, "status", None) != 200:
                    raise MediaInspectionUnavailableError("media inspection request failed")
                content_type = str(getattr(response, "headers", {}).get("Content-Type", "")).casefold()
                if not content_type.startswith("application/json"):
                    raise MediaInspectionResponseError("media inspection response is invalid")
                raw = await _read_bounded(response, self._max_response_bytes)
        except asyncio.CancelledError:
            raise
        except (MediaInspectionResponseError, MediaInspectionUnavailableError):
            raise
        except Exception as exc:
            raise MediaInspectionUnavailableError("media inspection request failed") from exc
        return _decode_result(raw)

    async def _get_session(self) -> Any:
        async with self._session_lock:
            if self._closing:
                raise MediaInspectionUnavailableError("media inspection provider is unavailable")
            if self._session is None:
                self._session = self._session_factory(
                    timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
                )
            return self._session

    async def _reserve_call(self) -> bool:
        if self._closing:
            return False
        try:
            reserved = self._call_reserver()
            if inspect.isawaitable(reserved):
                reserved = await reserved
            return not self._closing and reserved is True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False


def _validate_instruction(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_INSPECTION_INSTRUCTION_CHARS
        or any(ord(character) == 0 for character in value)
    ):
        raise MediaInspectionInputError("inspection instruction is invalid")
    return value


async def _read_bounded(response: Any, maximum: int) -> bytes:
    content = getattr(response, "content", None)
    iterator = getattr(content, "iter_chunked", None)
    if not callable(iterator):
        raise MediaInspectionResponseError("media inspection response is invalid")
    body = bytearray()
    async for chunk in iterator(min(64 * 1024, maximum + 1)):
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise MediaInspectionResponseError("media inspection response is invalid")
        body.extend(chunk)
        if len(body) > maximum:
            raise MediaInspectionResponseError("media inspection response is too large")
    if not body:
        raise MediaInspectionResponseError("media inspection response is invalid")
    return bytes(body)


def _decode_result(raw: bytes) -> MediaInspectionResult:
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MediaInspectionResponseError("media inspection response is invalid") from exc
    if not isinstance(data, Mapping) or data.get("status") != "completed":
        raise MediaInspectionResponseError("media inspection response is incomplete")
    outputs = data.get("outputs")
    if not isinstance(outputs, list) or not 1 <= len(outputs) <= _MAX_OUTPUT_STEPS:
        raise MediaInspectionResponseError("media inspection response is invalid")
    model_outputs = [step for step in outputs if isinstance(step, Mapping) and step.get("type") == "model_output"]
    if not model_outputs:
        raise MediaInspectionResponseError("media inspection response has no model output")
    content = model_outputs[-1].get("content")
    if not isinstance(content, list) or not 1 <= len(content) <= _MAX_CONTENT_ITEMS:
        raise MediaInspectionResponseError("media inspection response is invalid")
    pieces: list[str] = []
    for item in content:
        if not isinstance(item, Mapping) or item.get("type") != "text":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            raise MediaInspectionResponseError("media inspection response is invalid")
        pieces.append(text)
    result = "".join(pieces).strip()
    if not result or len(result) > MAX_INSPECTION_OUTPUT_CHARS or "\x00" in result:
        raise MediaInspectionResponseError("media inspection response text is invalid")
    try:
        result.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise MediaInspectionResponseError("media inspection response text is invalid") from exc
    return MediaInspectionResult(result)


__all__ = ["GeminiMediaInspectionProvider"]
