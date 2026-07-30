"""Pinned internal SearXNG discovery adapter for the private Search Sandbox."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit

import aiohttp

from yonerai_discord.modules.web_runtime.search import WebSearchSource

from .contracts import (
    MAX_SEARCH_JSON_BYTES,
    SearchCorroborationState,
    SearchEngineErrorV1,
    SearchEvidenceV1,
    SearchFabricContractError,
    SearchFetchState,
    SearchIntent,
    SearchResultV1,
    SearchSourceClass,
    query_digest,
    validate_language,
    validate_search_binding,
)


SEARXNG_BACKEND_ID = "searxng.local"
SEARXNG_INTERNAL_ORIGIN = "http://searxng:8080"
_MAX_SEARXNG_REQUEST_BYTES = 16 * 1024


class SearxngDiscoveryError(RuntimeError):
    """Fixed, non-content-bearing SearXNG discovery failure."""


@dataclass(frozen=True, slots=True)
class SearxngSearchQuery:
    request_id: str
    query: str = field(repr=False)
    query_digest: str
    intent: SearchIntent
    language: str
    limit: int

    def __post_init__(self) -> None:
        validate_search_binding(self.request_id, self.query_digest)
        if query_digest(self.query) != self.query_digest:
            raise ValueError("query digest does not match the query")
        object.__setattr__(self, "intent", SearchIntent(self.intent))
        validate_language(self.language)
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or not 1 <= self.limit <= 20:
            raise ValueError("limit is outside the allowed range")


@dataclass(frozen=True, slots=True)
class SearxngPostRequest:
    body: bytes = field(repr=False)
    timeout_seconds: float
    max_response_bytes: int = MAX_SEARCH_JSON_BYTES
    origin: str = SEARXNG_INTERNAL_ORIGIN
    path: str = "/search"

    def __post_init__(self) -> None:
        if self.origin != SEARXNG_INTERNAL_ORIGIN or self.path != "/search":
            raise ValueError("SearXNG origin and path are code-owned")
        if type(self.body) is not bytes or not 1 <= len(self.body) <= _MAX_SEARXNG_REQUEST_BYTES:
            raise ValueError("SearXNG request body is outside the byte limit")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0.1 <= float(self.timeout_seconds) <= 30.0
        ):
            raise ValueError("SearXNG timeout is outside the allowed range")
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or not 1_024 <= self.max_response_bytes <= MAX_SEARCH_JSON_BYTES
        ):
            raise ValueError("SearXNG response limit is outside the allowed range")


@dataclass(frozen=True, slots=True)
class SearxngPostResponse:
    status: int
    body: bytes = field(repr=False)
    media_type: str

    def __post_init__(self) -> None:
        if isinstance(self.status, bool) or not isinstance(self.status, int) or not 100 <= self.status <= 599:
            raise ValueError("SearXNG response status is invalid")
        if type(self.body) is not bytes:
            raise TypeError("SearXNG response body must be bytes")
        if not isinstance(self.media_type, str) or len(self.media_type) > 100:
            raise ValueError("SearXNG response media type is invalid")


class SearxngTransport(Protocol):
    async def health(self) -> bool: ...

    async def post_search(self, request: SearxngPostRequest) -> SearxngPostResponse: ...


class AiohttpSearxngTransport:
    """Talk only to the code-owned Docker-network SearXNG service."""

    def __init__(
        self,
        *,
        session_factory: Callable[..., Any] = aiohttp.ClientSession,
    ) -> None:
        if not callable(session_factory):
            raise TypeError("session_factory must be callable")
        self._session_factory = session_factory

    async def health(self) -> bool:
        timeout = aiohttp.ClientTimeout(total=3.0)
        try:
            async with self._session_factory(timeout=timeout, trust_env=False) as session:
                async with session.get(
                    f"{SEARXNG_INTERNAL_ORIGIN}/config",
                    allow_redirects=False,
                    proxy=None,
                ) as response:
                    body = await _read_bounded(response, 64 * 1024)
                    if response.status != 200:
                        return False
                    try:
                        document = json.loads(
                            body.decode("utf-8", errors="strict"),
                            object_pairs_hook=_unique_object,
                            parse_constant=_reject_nonfinite,
                        )
                    except (
                        UnicodeDecodeError,
                        json.JSONDecodeError,
                        RecursionError,
                        SearchFabricContractError,
                    ):
                        return False
                    return isinstance(document, dict)
        except asyncio.CancelledError:
            raise
        except (TimeoutError, aiohttp.ClientError, OSError):
            return False

    async def post_search(self, request: SearxngPostRequest) -> SearxngPostResponse:
        if not isinstance(request, SearxngPostRequest):
            raise TypeError("request must be a SearxngPostRequest")
        timeout = aiohttp.ClientTimeout(total=float(request.timeout_seconds))
        try:
            async with self._session_factory(timeout=timeout, trust_env=False) as session:
                async with session.post(
                    f"{request.origin}{request.path}",
                    data=request.body,
                    headers={
                        "accept": "application/json",
                        "content-type": "application/x-www-form-urlencoded",
                    },
                    allow_redirects=False,
                    proxy=None,
                ) as response:
                    body = await _read_bounded(response, request.max_response_bytes)
                    return SearxngPostResponse(
                        status=response.status,
                        body=body,
                        media_type=response.headers.get("content-type", ""),
                    )
        except asyncio.CancelledError:
            raise
        except SearxngDiscoveryError:
            raise
        except (TimeoutError, aiohttp.ClientError, OSError):
            raise SearxngDiscoveryError("SearXNG discovery request failed") from None


class SearxngSearchAdapter:
    def __init__(
        self,
        transport: SearxngTransport,
        *,
        timeout_seconds: float = 8.0,
        max_response_bytes: int = MAX_SEARCH_JSON_BYTES,
    ) -> None:
        if not callable(getattr(transport, "health", None)) or not callable(getattr(transport, "post_search", None)):
            raise TypeError("transport must implement the SearXNG transport contract")
        SearxngPostRequest(
            body=b"q=x&format=json",
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )
        self._transport = transport
        self._timeout_seconds = float(timeout_seconds)
        self._max_response_bytes = max_response_bytes

    async def health(self) -> bool:
        try:
            if await self._transport.health() is not True:
                return False
            probe_query = "SearXNG documentation"
            result = await self.search(
                SearxngSearchQuery(
                    request_id="health-probe-v1",
                    query=probe_query,
                    query_digest=query_digest(probe_query),
                    intent=SearchIntent.OFFICIAL,
                    language="en-US",
                    limit=1,
                )
            )
            return result.candidate_count > 0 and bool(result.evidence)
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def search(self, query: SearxngSearchQuery) -> SearchResultV1:
        if not isinstance(query, SearxngSearchQuery):
            raise TypeError("query must be a SearxngSearchQuery")
        body = urlencode(
            {
                "q": query.query,
                "categories": _category_for(query.intent),
                "language": query.language,
                "format": "json",
                "pageno": "1",
            }
        ).encode("utf-8")
        request = SearxngPostRequest(
            body=body,
            timeout_seconds=self._timeout_seconds,
            max_response_bytes=self._max_response_bytes,
        )
        started = time.monotonic()
        response = await self._transport.post_search(request)
        latency_ms = min(120_000, max(0, round((time.monotonic() - started) * 1_000)))
        if not isinstance(response, SearxngPostResponse):
            raise SearxngDiscoveryError("SearXNG transport returned an invalid response")
        if response.status != 200:
            raise SearxngDiscoveryError("SearXNG returned a non-success status")
        if response.media_type.partition(";")[0].strip().casefold() != "application/json":
            raise SearxngDiscoveryError("SearXNG returned an invalid media type")
        return _decode_result(response.body, query=query, latency_ms=latency_ms)


def _decode_result(body: bytes, *, query: SearxngSearchQuery, latency_ms: int) -> SearchResultV1:
    if type(body) is not bytes or len(body) > MAX_SEARCH_JSON_BYTES:
        raise SearxngDiscoveryError("SearXNG response exceeded the byte limit")
    try:
        document = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, SearchFabricContractError):
        raise SearxngDiscoveryError("SearXNG returned invalid JSON") from None
    if not isinstance(document, dict) or not isinstance(document.get("results"), list):
        raise SearxngDiscoveryError("SearXNG response did not contain a results array")
    raw_results = document["results"]
    if len(raw_results) > 1_000:
        raise SearxngDiscoveryError("SearXNG returned too many candidates")
    retrieved = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    evidence: list[SearchEvidenceV1] = []
    seen_urls: set[str] = set()
    for item in raw_results:
        if len(evidence) >= query.limit:
            break
        source = _decode_source(item)
        if source is None or source.url in seen_urls:
            continue
        seen_urls.add(source.url)
        evidence.append(
            SearchEvidenceV1(
                source=source,
                source_class=SearchSourceClass.UNKNOWN,
                fetch_state=SearchFetchState.METADATA_ONLY,
                published=None,
                retrieved=retrieved,
                content_hash=None,
                corroboration=SearchCorroborationState.UNKNOWN,
                verification_reasons=("unclassified_domain", "fetch_not_performed"),
            )
        )
    engine_errors: tuple[SearchEngineErrorV1, ...] = ()
    if _has_unresponsive_engines(document.get("unresponsive_engines", [])):
        engine_errors = (
            SearchEngineErrorV1(
                backend_id=SEARXNG_BACKEND_ID,
                code="partial_engine_failure",
            ),
        )
    return SearchResultV1(
        request_id=query.request_id,
        query_digest=query.query_digest,
        intent=query.intent,
        language=query.language,
        evidence=tuple(evidence),
        backend_ids=(SEARXNG_BACKEND_ID,),
        engine_errors=engine_errors,
        candidate_count=len(raw_results),
        cache_hits=0,
        latency_ms=latency_ms,
    )


def _decode_source(item: object) -> WebSearchSource | None:
    if not isinstance(item, dict):
        raise SearxngDiscoveryError("SearXNG result entry is invalid")
    title = item.get("title")
    url = item.get("url")
    snippet = item.get("content", "")
    if not isinstance(title, str) or not isinstance(url, str) or not isinstance(snippet, str):
        raise SearxngDiscoveryError("SearXNG result entry types are invalid")
    title = title.strip()
    snippet = snippet.strip()
    normalized_url = _public_source_url(url)
    if not title or normalized_url is None:
        return None
    source_id = f"src_{hashlib.sha256(normalized_url.encode('utf-8')).hexdigest()[:24]}"
    try:
        return WebSearchSource(
            title=title[:500],
            url=normalized_url,
            snippet=snippet[:4_000],
            source_id=source_id,
        )
    except (TypeError, ValueError):
        return None


def _public_source_url(value: str) -> str | None:
    if not value or value != value.strip() or len(value) > 2_048 or "\\" in value:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or "." not in parsed.hostname
        or parsed.hostname.casefold().endswith(".local")
        or parsed.hostname.casefold().endswith(".internal")
        or parsed.hostname.casefold() == "localhost"
        or any(character.isspace() or ord(character) < 32 for character in value)
        or (port is not None and port != (443 if parsed.scheme == "https" else 80))
    ):
        return None
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def _category_for(intent: SearchIntent) -> str:
    return {
        SearchIntent.GENERAL: "general",
        SearchIntent.NEWS: "news",
        SearchIntent.OFFICIAL: "general",
        SearchIntent.SCHOLARLY: "science",
        SearchIntent.CODE: "it",
    }[intent]


def _has_unresponsive_engines(value: object) -> bool:
    if not isinstance(value, list):
        raise SearxngDiscoveryError("SearXNG unresponsive_engines is invalid")
    return bool(value)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SearchFabricContractError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> None:
    raise SearchFabricContractError("non-finite JSON numbers are forbidden")


async def _read_bounded(response: Any, maximum: int) -> bytes:
    chunks: list[bytes] = []
    consumed = 0
    async for chunk in response.content.iter_chunked(16 * 1024):
        consumed += len(chunk)
        if consumed > maximum:
            raise SearxngDiscoveryError("SearXNG response exceeded the byte limit")
        chunks.append(bytes(chunk))
    return b"".join(chunks)


__all__ = [
    "AiohttpSearxngTransport",
    "SEARXNG_BACKEND_ID",
    "SEARXNG_INTERNAL_ORIGIN",
    "SearxngDiscoveryError",
    "SearxngPostRequest",
    "SearxngPostResponse",
    "SearxngSearchAdapter",
    "SearxngSearchQuery",
    "SearxngTransport",
]
