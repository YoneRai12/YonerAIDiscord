from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp


class WebRuntimeError(RuntimeError):
    """Provider-neutral Web runtime error."""


class WebBackendBlockerCode(StrEnum):
    DISABLED = "disabled"
    UNCONFIGURED = "unconfigured"
    DEPENDENCY_MISSING = "dependency_missing"


class WebBackendUnavailableError(WebRuntimeError):
    """The backend is intentionally unavailable and no network call was made."""

    def __init__(self, code: WebBackendBlockerCode, detail: str) -> None:
        super().__init__(detail)
        self.code = WebBackendBlockerCode(code)


class HttpsTransportError(WebRuntimeError):
    """HTTPS transport failed without exposing credentials or response bodies."""


class HttpsTransientError(HttpsTransportError):
    """A bounded retry may be attempted."""


class HttpsResponseLimitError(HttpsTransportError):
    """The response exceeded the configured byte limit."""


class WebSearchContractError(WebRuntimeError):
    """The configured backend returned a response outside the search contract."""


class HttpsMethod(StrEnum):
    GET = "GET"
    HEAD = "HEAD"


@dataclass(frozen=True, slots=True)
class HttpsRequest:
    method: HttpsMethod
    url: str = field(repr=False)
    timeout_seconds: float
    max_response_bytes: int
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", HttpsMethod(self.method))
        _require_https_url(self.url, allow_query=True)
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0.1 <= float(self.timeout_seconds) <= 60.0
        ):
            raise ValueError("timeout_seconds is outside the allowed range")
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or not 0 <= self.max_response_bytes <= 4 * 1024 * 1024
        ):
            raise ValueError("max_response_bytes is outside the allowed range")
        if self.method is HttpsMethod.GET and self.max_response_bytes < 1_024:
            raise ValueError("GET requires at least 1024 response bytes")
        if not isinstance(self.headers, Mapping):
            raise TypeError("headers must be a mapping")
        normalized_headers: dict[str, str] = {}
        for name, value in self.headers.items():
            if (
                not isinstance(name, str)
                or not isinstance(value, str)
                or not name
                or any(character.isspace() or ord(character) < 33 for character in name)
                or "\r" in value
                or "\n" in value
            ):
                raise ValueError("HTTPS headers are invalid")
            normalized_headers[name] = value
        object.__setattr__(self, "headers", MappingProxyType(normalized_headers))


@dataclass(frozen=True, slots=True)
class HttpsResponse:
    status: int
    body: bytes = field(repr=False)
    media_type: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.status, bool) or not isinstance(self.status, int) or not 100 <= self.status <= 599:
            raise ValueError("status is outside the HTTP range")
        if type(self.body) is not bytes:
            raise TypeError("body must be bytes")
        if self.media_type is not None and not isinstance(self.media_type, str):
            raise TypeError("media_type must be a string or None")


class HttpsTransport(Protocol):
    async def request(self, request: HttpsRequest) -> HttpsResponse: ...


class AiohttpHttpsTransport:
    """Small real HTTPS transport; callers own endpoint and response semantics."""

    async def request(self, request: HttpsRequest) -> HttpsResponse:
        if not isinstance(request, HttpsRequest):
            raise TypeError("request must be an HttpsRequest")
        timeout = aiohttp.ClientTimeout(total=float(request.timeout_seconds))
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    request.method.value,
                    request.url,
                    headers=request.headers,
                    allow_redirects=False,
                ) as response:
                    if request.method is HttpsMethod.HEAD:
                        body = b""
                    else:
                        chunks: list[bytes] = []
                        consumed = 0
                        async for chunk in response.content.iter_chunked(16 * 1024):
                            consumed += len(chunk)
                            if consumed > request.max_response_bytes:
                                raise HttpsResponseLimitError("HTTPS response exceeded the configured byte limit")
                            chunks.append(bytes(chunk))
                        body = b"".join(chunks)
                    content_type = response.headers.get("content-type")
                    return HttpsResponse(status=response.status, body=body, media_type=content_type)
        except asyncio.CancelledError:
            raise
        except HttpsResponseLimitError:
            raise
        except (TimeoutError, aiohttp.ClientConnectionError, aiohttp.ServerDisconnectedError) as exc:
            raise HttpsTransientError("HTTPS request failed transiently") from exc
        except aiohttp.ClientError as exc:
            raise HttpsTransportError("HTTPS request failed") from exc


@dataclass(frozen=True, slots=True)
class WebSearchSource:
    title: str
    url: str
    snippet: str = ""
    source_id: str = ""

    def __post_init__(self) -> None:
        for label, value, maximum in (
            ("title", self.title, 500),
            ("snippet", self.snippet, 4_000),
            ("source_id", self.source_id, 200),
        ):
            if not isinstance(value, str) or len(value) > maximum or any(ord(character) < 9 for character in value):
                raise ValueError(f"{label} is invalid")
        if not self.title.strip():
            raise ValueError("title must not be blank")
        _require_source_url(self.url)


@dataclass(frozen=True, slots=True)
class WebSearchRequest:
    query: str = field(repr=False)
    limit: int = 5

    def __post_init__(self) -> None:
        if (
            not isinstance(self.query, str)
            or not self.query
            or self.query != self.query.strip()
            or len(self.query) > 1_000
            or any(ord(character) < 32 and character not in "\t" for character in self.query)
        ):
            raise ValueError("query is invalid")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or not 1 <= self.limit <= 20:
            raise ValueError("limit is outside the allowed range")


@dataclass(frozen=True, slots=True)
class WebSearchResult:
    query: str = field(repr=False)
    backend_id: str
    sources: tuple[WebSearchSource, ...]
    attempts: int

    def __post_init__(self) -> None:
        if not isinstance(self.query, str) or not self.query:
            raise ValueError("query is invalid")
        _require_identifier(self.backend_id, "backend_id")
        sources = tuple(self.sources)
        if any(not isinstance(source, WebSearchSource) for source in sources):
            raise TypeError("sources must contain WebSearchSource values")
        if len(sources) > 20:
            raise ValueError("too many sources")
        object.__setattr__(self, "sources", sources)
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int) or not 1 <= self.attempts <= 4:
            raise ValueError("attempts is outside the allowed range")


@dataclass(frozen=True, slots=True)
class WebSearchLimits:
    timeout_seconds: float = 8.0
    max_results: int = 10
    max_response_bytes: int = 512 * 1024
    retries: int = 1
    retry_backoff_seconds: float = 0.05

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0.1 <= float(self.timeout_seconds) <= 60.0
        ):
            raise ValueError("timeout_seconds is outside the allowed range")
        if (
            isinstance(self.max_results, bool)
            or not isinstance(self.max_results, int)
            or not 1 <= self.max_results <= 20
        ):
            raise ValueError("max_results is outside the allowed range")
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or not 1_024 <= self.max_response_bytes <= 4 * 1024 * 1024
        ):
            raise ValueError("max_response_bytes is outside the allowed range")
        if isinstance(self.retries, bool) or not isinstance(self.retries, int) or not 0 <= self.retries <= 3:
            raise ValueError("retries is outside the allowed range")
        if (
            isinstance(self.retry_backoff_seconds, bool)
            or not isinstance(self.retry_backoff_seconds, (int, float))
            or not 0 <= float(self.retry_backoff_seconds) <= 1.0
        ):
            raise ValueError("retry_backoff_seconds is outside the allowed range")


class SearchDocumentDecoder(Protocol):
    def decode(self, response: HttpsResponse, *, limit: int) -> tuple[WebSearchSource, ...]: ...


class JsonSearchDocumentDecoder:
    """Strict provider-neutral JSON contract: ``{"sources": [...]}``."""

    def decode(self, response: HttpsResponse, *, limit: int) -> tuple[WebSearchSource, ...]:
        if not isinstance(response, HttpsResponse):
            raise TypeError("response must be an HttpsResponse")
        try:
            document = json.loads(response.body.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebSearchContractError("search backend returned invalid UTF-8 JSON") from exc
        if not isinstance(document, dict) or set(document) != {"sources"} or not isinstance(document["sources"], list):
            raise WebSearchContractError("search backend document must contain only a sources array")
        sources: list[WebSearchSource] = []
        for item in document["sources"][:limit]:
            if (
                not isinstance(item, dict)
                or not {"title", "url"} <= set(item)
                or set(item)
                - {
                    "title",
                    "url",
                    "snippet",
                    "source_id",
                }
            ):
                raise WebSearchContractError("search source did not match the provider-neutral contract")
            try:
                source = WebSearchSource(
                    title=item["title"],
                    url=item["url"],
                    snippet=item.get("snippet", ""),
                    source_id=item.get("source_id", ""),
                )
            except (TypeError, ValueError) as exc:
                raise WebSearchContractError("search source was invalid") from exc
            sources.append(source)
        return tuple(sources)


@dataclass(frozen=True, slots=True)
class WebBackendAvailability:
    available: bool
    blocker: WebBackendBlockerCode | None
    detail: str

    def __post_init__(self) -> None:
        if type(self.available) is not bool:
            raise TypeError("available must be a boolean")
        if self.available != (self.blocker is None):
            raise ValueError("availability and blocker disagree")
        if not isinstance(self.detail, str) or not self.detail:
            raise ValueError("detail must not be blank")


class ProviderNeutralWebSearchAdapter:
    """Bounded GET/HEAD search backend seam with explicit source attribution."""

    def __init__(
        self,
        *,
        backend_id: str,
        enabled: bool = False,
        endpoint: str | None = None,
        transport: HttpsTransport | None = None,
        decoder: SearchDocumentDecoder | None = None,
        limits: WebSearchLimits = WebSearchLimits(),
    ) -> None:
        _require_identifier(backend_id, "backend_id")
        if type(enabled) is not bool:
            raise TypeError("enabled must be a boolean")
        if endpoint is not None:
            _require_https_url(endpoint, allow_query=False)
        if transport is not None and not callable(getattr(transport, "request", None)):
            raise TypeError("transport must implement request")
        if decoder is not None and not callable(getattr(decoder, "decode", None)):
            raise TypeError("decoder must implement decode")
        if not isinstance(limits, WebSearchLimits):
            raise TypeError("limits must be WebSearchLimits")
        self._backend_id = backend_id
        self._enabled = enabled
        self._endpoint = endpoint
        self._transport = transport
        self._decoder = decoder or JsonSearchDocumentDecoder()
        self._limits = limits

    @property
    def availability(self) -> WebBackendAvailability:
        if not self._enabled:
            return WebBackendAvailability(False, WebBackendBlockerCode.DISABLED, "Web search backend is disabled")
        if self._endpoint is None or self._transport is None:
            return WebBackendAvailability(
                False,
                WebBackendBlockerCode.UNCONFIGURED,
                "Web search endpoint or HTTPS transport is not configured",
            )
        return WebBackendAvailability(True, None, "Web search backend is configured")

    async def check_available(self) -> bool:
        self._require_available()
        assert self._endpoint is not None and self._transport is not None
        response, _attempts = await self._request_with_retry(
            HttpsRequest(
                method=HttpsMethod.HEAD,
                url=self._endpoint,
                timeout_seconds=self._limits.timeout_seconds,
                max_response_bytes=0,
            )
        )
        return 200 <= response.status < 300

    async def search(self, request: WebSearchRequest) -> WebSearchResult:
        if not isinstance(request, WebSearchRequest):
            raise TypeError("request must be a WebSearchRequest")
        self._require_available()
        if request.limit > self._limits.max_results:
            raise WebSearchContractError("requested result limit exceeds the backend limit")
        assert self._endpoint is not None and self._transport is not None
        response, attempts = await self._request_with_retry(
            HttpsRequest(
                method=HttpsMethod.GET,
                url=_search_url(self._endpoint, query=request.query, limit=request.limit),
                timeout_seconds=self._limits.timeout_seconds,
                max_response_bytes=self._limits.max_response_bytes,
                headers={"accept": "application/json"},
            )
        )
        if not 200 <= response.status < 300:
            raise WebSearchContractError("search backend returned a non-success status")
        sources = self._decoder.decode(response, limit=request.limit)
        return WebSearchResult(
            query=request.query,
            backend_id=self._backend_id,
            sources=sources,
            attempts=attempts,
        )

    def _require_available(self) -> None:
        availability = self.availability
        if not availability.available:
            assert availability.blocker is not None
            raise WebBackendUnavailableError(availability.blocker, availability.detail)

    async def _request_with_retry(self, request: HttpsRequest) -> tuple[HttpsResponse, int]:
        assert self._transport is not None
        attempts = self._limits.retries + 1
        for attempt in range(1, attempts + 1):
            try:
                response = await self._transport.request(request)
                if not isinstance(response, HttpsResponse):
                    raise WebSearchContractError("HTTPS transport returned an invalid response")
                if response.status == 429 or 500 <= response.status < 600:
                    raise HttpsTransientError("search backend returned a retryable status")
                return response, attempt
            except asyncio.CancelledError:
                raise
            except HttpsTransientError:
                if attempt >= attempts:
                    raise
                if self._limits.retry_backoff_seconds:
                    await asyncio.sleep(float(self._limits.retry_backoff_seconds))
        raise AssertionError("retry loop did not terminate")


def _search_url(endpoint: str, *, query: str, limit: int) -> str:
    parsed = urlsplit(endpoint)
    parameters = parse_qsl(parsed.query, keep_blank_values=True)
    parameters.extend((("q", query), ("limit", str(limit))))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(parameters), ""))


def _require_https_url(value: object, *, allow_query: bool) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 2_048:
        raise ValueError("URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
        or parsed.fragment
        or (parsed.query and not allow_query)
        or "\\" in value
        or any(character.isspace() or ord(character) < 32 for character in value)
        or (port is not None and port != 443)
    ):
        raise ValueError("URL must be a credential-free HTTPS URL")


def _require_source_url(value: object) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 2_048:
        raise ValueError("source URL is invalid")
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError as exc:
        raise ValueError("source URL is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
        or "\\" in value
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ValueError("source URL must be an absolute credential-free HTTP(S) URL")


def _require_identifier(value: object, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 120
        or not value[0].isalnum()
        or any(not (character.isalnum() or character in "._-") for character in value)
    ):
        raise ValueError(f"{label} is invalid")


__all__ = [
    "AiohttpHttpsTransport",
    "HttpsMethod",
    "HttpsRequest",
    "HttpsResponse",
    "HttpsResponseLimitError",
    "HttpsTransientError",
    "HttpsTransport",
    "HttpsTransportError",
    "JsonSearchDocumentDecoder",
    "ProviderNeutralWebSearchAdapter",
    "SearchDocumentDecoder",
    "WebBackendAvailability",
    "WebBackendBlockerCode",
    "WebBackendUnavailableError",
    "WebRuntimeError",
    "WebSearchContractError",
    "WebSearchLimits",
    "WebSearchRequest",
    "WebSearchResult",
    "WebSearchSource",
]
