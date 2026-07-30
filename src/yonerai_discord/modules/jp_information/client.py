from __future__ import annotations

import inspect
import json
import re
from collections.abc import Collection, Mapping
from typing import Any

import aiohttp

from .errors import (
    InvalidPayloadError,
    RedirectRejectedError,
    ResponseTooLargeError,
    TransportError,
    UnknownRegionError,
)


JMA_AREA_URL = "https://www.jma.go.jp/bosai/common/const/area.json"
JMA_FORECAST_URL_TEMPLATE = "https://www.jma.go.jp/bosai/forecast/data/forecast/{code}.json"
JMA_WARNING_URL_TEMPLATE = "https://www.jma.go.jp/bosai/warning/data/warning/{code}.json"
CAO_HOLIDAY_CSV_URL = "https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv"

# 互換性のある短い別名も公開する。値はすべて固定で、利用者入力を URL として受け取らない。
AREA_URL = JMA_AREA_URL
FORECAST_URL_TEMPLATE = JMA_FORECAST_URL_TEMPLATE
WARNING_URL_TEMPLATE = JMA_WARNING_URL_TEMPLATE
HOLIDAY_CSV_URL = CAO_HOLIDAY_CSV_URL

MAX_JMA_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_HOLIDAY_RESPONSE_BYTES = 1024 * 1024
DEFAULT_TOTAL_TIMEOUT_SECONDS = 10.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 3.0
_REGION_CODE = re.compile(r"^[0-9]{6}$")
_USER_AGENT = "YonerAI-Discord-Suite/0.1 (official-public-information-client)"


class _FixedEndpointClient:
    def __init__(
        self,
        *,
        session: Any | None,
        total_timeout_seconds: float,
        connect_timeout_seconds: float,
    ) -> None:
        total = _positive_finite(total_timeout_seconds, "total_timeout_seconds")
        connect = _positive_finite(connect_timeout_seconds, "connect_timeout_seconds")
        if connect > total:
            raise ValueError("connect_timeout_seconds must not exceed total_timeout_seconds")
        self._session = session
        self._owns_session = session is None
        self.timeout = aiohttp.ClientTimeout(total=total, connect=connect, sock_connect=connect)

    @property
    def session(self) -> Any | None:
        return self._session

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers={"User-Agent": _USER_AGENT, "Accept": "application/json,text/csv;q=0.9"},
                trust_env=False,
            )

    async def close(self) -> None:
        session = self._session
        try:
            if session is not None and self._owns_session:
                close = getattr(session, "close", None)
                if callable(close):
                    result = close()
                    if inspect.isawaitable(result):
                        await result
        finally:
            self._session = None

    def _required_session(self) -> Any:
        if self._session is None:
            raise RuntimeError("official information client is not started")
        return self._session

    async def _get_bytes(self, url: str, *, maximum_bytes: int) -> bytes:
        session = self._required_session()
        try:
            response = await session.get(
                url,
                allow_redirects=False,
                timeout=self.timeout,
                headers={"Accept": "application/json,text/csv;q=0.9"},
            )
        except Exception as exc:
            raise TransportError("official provider request failed") from exc
        try:
            status = getattr(response, "status", None)
            history = getattr(response, "history", ())
            if history or (isinstance(status, int) and 300 <= status < 400):
                raise RedirectRejectedError("official endpoint redirect was rejected")
            if isinstance(status, int):
                if status != 200:
                    raise TransportError("official provider returned a non-success status")
            else:
                raise_for_status = getattr(response, "raise_for_status", None)
                if callable(raise_for_status):
                    try:
                        raise_for_status()
                    except Exception as exc:
                        raise TransportError("official provider returned a non-success status") from exc
            return await _read_stream_bounded(response, maximum_bytes=maximum_bytes)
        finally:
            release = getattr(response, "release", None)
            if callable(release):
                result = release()
                if inspect.isawaitable(result):
                    await result


class JmaClient(_FixedEndpointClient):
    """気象庁の固定 HTTPS endpoint だけを読む transport。"""

    def __init__(
        self,
        *,
        session: Any | None = None,
        total_timeout_seconds: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_JMA_RESPONSE_BYTES,
        allowed_region_codes: Collection[str] = (),
    ) -> None:
        super().__init__(
            session=session,
            total_timeout_seconds=total_timeout_seconds,
            connect_timeout_seconds=connect_timeout_seconds,
        )
        self.max_response_bytes = _bounded_size(max_response_bytes, MAX_JMA_RESPONSE_BYTES, "max_response_bytes")
        self._allowed_region_codes: frozenset[str] = frozenset()
        if allowed_region_codes:
            self.set_allowed_region_codes(allowed_region_codes)

    @property
    def allowed_region_codes(self) -> frozenset[str]:
        return self._allowed_region_codes

    def set_allowed_region_codes(self, codes: Collection[str]) -> None:
        normalized = frozenset(codes)
        if not normalized or any(
            not isinstance(code, str) or _REGION_CODE.fullmatch(code) is None for code in normalized
        ):
            raise ValueError("allowed_region_codes must contain six-digit JMA office codes")
        self._allowed_region_codes = normalized

    async def fetch_area_catalog(self) -> Mapping[str, Any]:
        payload = await self._get_json(JMA_AREA_URL)
        if not isinstance(payload, Mapping):
            raise InvalidPayloadError("JMA area response must be an object")
        offices = payload.get("offices")
        if isinstance(offices, Mapping):
            codes = {code for code in offices if isinstance(code, str) and _REGION_CODE.fullmatch(code) is not None}
            if codes:
                self._allowed_region_codes = frozenset(codes)
        return payload

    async def fetch_forecast(self, region_code: str) -> object:
        code = self._allowed_code(region_code)
        return await self._get_json(JMA_FORECAST_URL_TEMPLATE.format(code=code))

    async def fetch_warning(self, region_code: str) -> object:
        code = self._allowed_code(region_code)
        return await self._get_json(JMA_WARNING_URL_TEMPLATE.format(code=code))

    async def _get_json(self, url: str) -> object:
        body = await self._get_bytes(url, maximum_bytes=self.max_response_bytes)
        try:
            text = body.decode("utf-8", errors="strict")
            payload = json.loads(text, parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise InvalidPayloadError("JMA response is not strict UTF-8 finite JSON") from exc
        _reject_non_finite(payload)
        return payload

    def _allowed_code(self, region_code: str) -> str:
        if not isinstance(region_code, str) or region_code not in self._allowed_region_codes:
            raise UnknownRegionError("region code is not in the official JMA area allowlist")
        return region_code


class CabinetOfficeHolidayClient(_FixedEndpointClient):
    """内閣府の固定祝日 CSV だけを読む transport。"""

    def __init__(
        self,
        *,
        session: Any | None = None,
        total_timeout_seconds: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_HOLIDAY_RESPONSE_BYTES,
    ) -> None:
        super().__init__(
            session=session,
            total_timeout_seconds=total_timeout_seconds,
            connect_timeout_seconds=connect_timeout_seconds,
        )
        self.max_response_bytes = _bounded_size(
            max_response_bytes,
            MAX_HOLIDAY_RESPONSE_BYTES,
            "max_response_bytes",
        )

    async def fetch_holiday_csv(self) -> str:
        body = await self._get_bytes(CAO_HOLIDAY_CSV_URL, maximum_bytes=self.max_response_bytes)
        return decode_holiday_csv(body)


# 呼称差を吸収する公開 alias。
HolidayClient = CabinetOfficeHolidayClient
JMAClient = JmaClient


async def _read_stream_bounded(response: Any, *, maximum_bytes: int) -> bytes:
    content_length = getattr(response, "content_length", None)
    if isinstance(content_length, int) and not isinstance(content_length, bool) and content_length > maximum_bytes:
        raise ResponseTooLargeError("official provider response exceeds the byte limit")
    content = getattr(response, "content", None)
    iterator = getattr(content, "iter_chunked", None)
    if not callable(iterator):
        raise TransportError("official provider response is not stream-readable")
    body = bytearray()
    chunk_size = min(65_536, maximum_bytes + 1)
    async for chunk in iterator(chunk_size):
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TransportError("official provider response contained a non-byte chunk")
        if len(body) + len(chunk) > maximum_bytes:
            raise ResponseTooLargeError("official provider response exceeds the byte limit")
        body.extend(chunk)
    return bytes(body)


def decode_holiday_csv(body: bytes) -> str:
    if not isinstance(body, bytes) or not body:
        raise InvalidPayloadError("holiday CSV body is empty")
    if body.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise InvalidPayloadError("UTF-16 holiday CSV is not accepted")
    encodings = ("utf-8-sig",) if body.startswith(b"\xef\xbb\xbf") else ("utf-8", "cp932")
    text: str | None = None
    for encoding in encodings:
        try:
            text = body.decode(encoding, errors="strict")
            break
        except UnicodeDecodeError:
            continue
    if text is None or "\x00" in text or "\ufffd" in text:
        raise InvalidPayloadError("holiday CSV charset is unsupported or invalid")
    if any(ord(char) < 32 and char not in "\r\n\t" for char in text):
        raise InvalidPayloadError("holiday CSV contains unsafe control characters")
    return text


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _reject_non_finite(payload: object) -> None:
    stack = [payload]
    nodes = 0
    while stack:
        value = stack.pop()
        nodes += 1
        if nodes > 200_000:
            raise InvalidPayloadError("JSON response is too complex")
        if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
            raise InvalidPayloadError("JSON response contains a non-finite number")
        if isinstance(value, Mapping):
            stack.extend(value.keys())
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)


def _positive_finite(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite positive number")
    normalized = float(value)
    if normalized <= 0 or normalized != normalized or normalized in {float("inf"), float("-inf")}:
        raise ValueError(f"{label} must be a finite positive number")
    return normalized


def _bounded_size(value: int, maximum: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{label} must be between 1 and {maximum}")
    return value
