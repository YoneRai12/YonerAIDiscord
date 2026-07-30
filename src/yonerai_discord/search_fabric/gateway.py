"""Injected, loopback-only POST gateway for Search Fabric v1."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from yonerai_discord.modules.web_runtime.search import WebSearchRequest

from .contracts import (
    MAX_SEARCH_BACKENDS,
    MAX_SEARCH_JSON_BYTES,
    SearchFabricContractError,
    SearchFabricJsonCodec,
    SearchIntent,
    query_digest,
    validate_backend_id,
)
from .receipts import SearchGatewayOutcome, SearchReceiptV1


class LoopbackAddress(StrEnum):
    IPV4 = "127.0.0.1"
    IPV6 = "::1"


@dataclass(frozen=True, slots=True)
class LoopbackSearchPostRequest:
    host: LoopbackAddress
    port: int
    body: bytes = field(repr=False)
    timeout_seconds: float
    max_response_bytes: int = MAX_SEARCH_JSON_BYTES
    method: str = "POST"
    path: str = "/v1/search"
    allow_redirects: bool = False
    use_proxy: bool = False
    resolve_dns: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "host", LoopbackAddress(self.host))
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65_535:
            raise ValueError("port is outside the TCP range")
        if type(self.body) is not bytes or not 1 <= len(self.body) <= MAX_SEARCH_JSON_BYTES:
            raise ValueError("body is outside the request byte limit")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0.1 <= float(self.timeout_seconds) <= 30.0
        ):
            raise ValueError("timeout_seconds is outside the allowed range")
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or not 1_024 <= self.max_response_bytes <= MAX_SEARCH_JSON_BYTES
        ):
            raise ValueError("max_response_bytes is outside the allowed range")
        if self.method != "POST" or self.path != "/v1/search":
            raise ValueError("loopback Search Fabric method and path are fixed")
        for label, value in (
            ("allow_redirects", self.allow_redirects),
            ("use_proxy", self.use_proxy),
            ("resolve_dns", self.resolve_dns),
        ):
            if type(value) is not bool or value:
                raise ValueError(f"{label} is fixed false")


@dataclass(frozen=True, slots=True)
class LoopbackSearchPostResponse:
    status: int
    body: bytes = field(repr=False)
    media_type: str = "application/json"

    def __post_init__(self) -> None:
        if isinstance(self.status, bool) or not isinstance(self.status, int) or not 100 <= self.status <= 599:
            raise ValueError("status is outside the HTTP range")
        if type(self.body) is not bytes:
            raise TypeError("body must be bytes")
        if not isinstance(self.media_type, str) or len(self.media_type) > 100:
            raise ValueError("media_type is invalid")


class LoopbackSearchPostTransport(Protocol):
    async def post(self, request: LoopbackSearchPostRequest) -> LoopbackSearchPostResponse: ...


class SearchFabricGatewayError(RuntimeError):
    """Fixed non-content-bearing Search Fabric gateway failure."""


class LoopbackSearchFabricGateway:
    """No-network implementation seam; the injected transport owns actual I/O."""

    def __init__(
        self,
        *,
        backend_ids: tuple[str, ...],
        host: LoopbackAddress,
        port: int,
        transport: LoopbackSearchPostTransport,
        timeout_seconds: float = 8.0,
        max_response_bytes: int = MAX_SEARCH_JSON_BYTES,
    ) -> None:
        if not callable(getattr(transport, "post", None)):
            raise TypeError("transport must implement post")
        if (
            not isinstance(backend_ids, tuple)
            or not 1 <= len(backend_ids) <= MAX_SEARCH_BACKENDS
            or len(set(backend_ids)) != len(backend_ids)
        ):
            raise ValueError("backend_ids are invalid")
        for backend_id in backend_ids:
            validate_backend_id(backend_id)
        # The request type owns exact IP, port, and bound checks.
        LoopbackSearchPostRequest(
            host=host,
            port=port,
            body=b"{}",
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )
        self._backend_ids = backend_ids
        self._host = LoopbackAddress(host)
        self._port = port
        self._transport = transport
        self._timeout_seconds = float(timeout_seconds)
        self._max_response_bytes = max_response_bytes

    async def search(
        self,
        request: WebSearchRequest,
        *,
        request_id: str,
        intent: SearchIntent = SearchIntent.GENERAL,
        language: str = "ja-JP",
    ) -> SearchGatewayOutcome:
        if not isinstance(request, WebSearchRequest):
            raise TypeError("request must be a WebSearchRequest")
        normalized_intent = SearchIntent(intent)
        digest = query_digest(request.query)
        body = SearchFabricJsonCodec.encode_request(
            request_id=request_id,
            query=request.query,
            intent=normalized_intent,
            language=language,
            limit=request.limit,
        )
        post_request = LoopbackSearchPostRequest(
            host=self._host,
            port=self._port,
            body=body,
            timeout_seconds=self._timeout_seconds,
            max_response_bytes=self._max_response_bytes,
        )
        try:
            response = await self._transport.post(post_request)
        except Exception:
            raise SearchFabricGatewayError("loopback Search Fabric transport failed") from None
        if not isinstance(response, LoopbackSearchPostResponse):
            raise SearchFabricGatewayError("loopback Search Fabric transport returned an invalid response")
        if response.status != 200:
            raise SearchFabricGatewayError("loopback Search Fabric returned a non-success status")
        if len(response.body) > self._max_response_bytes:
            raise SearchFabricGatewayError("loopback Search Fabric response exceeded the byte limit")
        if response.media_type.partition(";")[0].strip().casefold() != "application/json":
            raise SearchFabricGatewayError("loopback Search Fabric returned an invalid media type")
        result = SearchFabricJsonCodec.decode_result(
            response.body,
            expected_request_id=request_id,
            expected_query_digest=digest,
            expected_intent=normalized_intent,
            expected_language=language,
            limit=request.limit,
        )
        if result.backend_ids != self._backend_ids:
            raise SearchFabricContractError("search result backend binding is invalid")
        return SearchGatewayOutcome(
            result=result,
            receipt=SearchReceiptV1.from_result(result),
        )


__all__ = [
    "LoopbackAddress",
    "LoopbackSearchFabricGateway",
    "LoopbackSearchPostRequest",
    "LoopbackSearchPostResponse",
    "LoopbackSearchPostTransport",
    "SearchFabricGatewayError",
]
