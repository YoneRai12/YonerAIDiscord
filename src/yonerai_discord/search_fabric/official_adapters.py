"""Bounded public scholarly-metadata adapters.

The adapters return metadata records only.  They do not fetch a paper's full
text and therefore never label a result as peer reviewed or as verified
content.  Their fixed HTTPS origins are intentionally separate from SearXNG
candidate discovery.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import quote, urlencode, urlsplit

import aiohttp

from yonerai_discord.browser_sandbox.models import BrowserPolicyError
from yonerai_discord.browser_sandbox.policy import BrowserSandboxPolicy
from yonerai_discord.modules.web_runtime.search import WebSearchSource

from .contracts import SearchSourceClass
from .evidence_fetcher import _PinnedResolver, _peer_matches, _response_peer_address
from .source_policy import DEFAULT_SOURCE_POLICY, SourcePolicyRegistry


_MAX_QUERY_CHARS = 1_000
_MAX_RESPONSE_BYTES = 512 * 1024
_IDENTIFIER = "abcdefghijklmnopqrstuvwxyz0123456789._-"


class OfficialMetadataError(RuntimeError):
    """Fixed failure without a query, response body, or endpoint in its text."""


class OfficialMetadataAuthorizationError(PermissionError):
    """Fresh authorization changed between bounded public metadata calls."""


AuthorizationCurrent = Callable[[], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class ScholarlyMetadataRequest:
    query: str = field(repr=False)
    limit: int = 5

    def __post_init__(self) -> None:
        if (
            not isinstance(self.query, str)
            or not self.query
            or self.query != self.query.strip()
            or len(self.query) > _MAX_QUERY_CHARS
            or any(ord(character) < 32 and character != "\t" for character in self.query)
        ):
            raise ValueError("metadata query is invalid")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or not 1 <= self.limit <= 10:
            raise ValueError("metadata result limit is invalid")


@dataclass(frozen=True, slots=True)
class OfficialMetadataRecord:
    source: WebSearchSource
    provider_id: str
    publisher: str
    published_at: str | None
    source_class: SearchSourceClass = SearchSourceClass.SCHOLARLY_METADATA

    def __post_init__(self) -> None:
        if not isinstance(self.source, WebSearchSource):
            raise TypeError("source must be WebSearchSource")
        if (
            not isinstance(self.provider_id, str)
            or not self.provider_id
            or len(self.provider_id) > 80
            or any(character not in _IDENTIFIER for character in self.provider_id)
        ):
            raise ValueError("provider_id is invalid")
        if not isinstance(self.publisher, str) or len(self.publisher) > 500:
            raise ValueError("publisher is invalid")
        if self.published_at is not None and (not isinstance(self.published_at, str) or len(self.published_at) > 32):
            raise ValueError("published_at is invalid")
        if SearchSourceClass(self.source_class) is not SearchSourceClass.SCHOLARLY_METADATA:
            raise ValueError("public metadata adapters must return scholarly_metadata")


@dataclass(frozen=True, slots=True)
class PublicMetadataHttpRequest:
    url: str = field(repr=False)
    timeout_seconds: float = 8.0
    max_response_bytes: int = _MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url or len(self.url) > 4_096:
            raise ValueError("metadata URL is invalid")
        parsed = urlsplit(self.url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or "@" in parsed.netloc
            or "\\" in self.url
        ):
            raise ValueError("metadata URL must be credential-free HTTPS")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0.1 <= float(self.timeout_seconds) <= 30
        ):
            raise ValueError("metadata timeout is invalid")
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or not 1_024 <= self.max_response_bytes <= _MAX_RESPONSE_BYTES
        ):
            raise ValueError("metadata response limit is invalid")


@dataclass(frozen=True, slots=True)
class PublicMetadataHttpResponse:
    status: int
    body: bytes = field(repr=False)
    media_type: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.status, bool) or not isinstance(self.status, int) or not 100 <= self.status <= 599:
            raise ValueError("metadata HTTP status is invalid")
        if type(self.body) is not bytes:
            raise TypeError("metadata HTTP body must be bytes")
        if not isinstance(self.media_type, str) or len(self.media_type) > 256:
            raise ValueError("metadata media type is invalid")


class PublicMetadataTransport(Protocol):
    async def get(self, request: PublicMetadataHttpRequest) -> PublicMetadataHttpResponse: ...


class AiohttpPublicMetadataTransport:
    """Resolve-then-pin, no-proxy metadata reader for code-owned HTTPS origins."""

    def __init__(self, policy: BrowserSandboxPolicy) -> None:
        if not isinstance(policy, BrowserSandboxPolicy):
            raise TypeError("policy must be BrowserSandboxPolicy")
        self._policy = policy

    async def get(self, request: PublicMetadataHttpRequest) -> PublicMetadataHttpResponse:
        if not isinstance(request, PublicMetadataHttpRequest):
            raise TypeError("request must be PublicMetadataHttpRequest")
        try:
            authorized = await asyncio.to_thread(self._policy.authorize_url, request.url)
        except asyncio.CancelledError:
            raise
        except (BrowserPolicyError, TypeError, ValueError):
            raise OfficialMetadataError("metadata request was denied") from None
        resolver = _PinnedResolver(
            authorized.hostname,
            tuple(address.compressed for address in authorized.addresses),
        )
        connector = aiohttp.TCPConnector(
            resolver=resolver,
            use_dns_cache=False,
            ttl_dns_cache=0,
            limit=1,
            force_close=True,
            enable_cleanup_closed=True,
        )
        timeout = aiohttp.ClientTimeout(total=float(request.timeout_seconds))
        try:
            async with aiohttp.ClientSession(
                connector=connector,
                connector_owner=True,
                timeout=timeout,
                auto_decompress=False,
                trust_env=False,
                max_line_size=8_192,
                max_field_size=8_192,
            ) as session:
                async with session.get(
                    authorized.url,
                    allow_redirects=False,
                    proxy=None,
                    headers={
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                        "User-Agent": "YonerAI-SearchFabric/1",
                    },
                ) as response:
                    allowed_addresses = tuple(address.compressed for address in authorized.addresses)
                    if not _peer_matches(_response_peer_address(response), allowed_addresses):
                        raise OfficialMetadataError("metadata peer identity changed")
                    if response.status in {301, 302, 303, 307, 308} or response.headers.get("location"):
                        raise OfficialMetadataError("metadata redirects are not allowed")
                    if response.headers.get("content-encoding", "").strip().lower() not in {"", "identity"}:
                        raise OfficialMetadataError("metadata content encoding is not allowed")
                    if response.content_length is not None and response.content_length > request.max_response_bytes:
                        raise OfficialMetadataError("metadata response exceeded its byte limit")
                    chunks: list[bytes] = []
                    consumed = 0
                    async for chunk in response.content.iter_chunked(16 * 1024):
                        consumed += len(chunk)
                        if consumed > request.max_response_bytes:
                            raise OfficialMetadataError("metadata response exceeded its byte limit")
                        chunks.append(bytes(chunk))
                    return PublicMetadataHttpResponse(
                        status=response.status,
                        body=b"".join(chunks),
                        media_type=response.headers.get("content-type", ""),
                    )
        except asyncio.CancelledError:
            raise
        except OfficialMetadataError:
            raise
        except (TimeoutError, aiohttp.ClientError, OSError):
            raise OfficialMetadataError("metadata request failed") from None
        finally:
            if not connector.closed:
                await connector.close()


class CrossrefMetadataAdapter:
    """Crossref Works metadata search; Crossref metadata is not paper truth."""

    provider_id = "crossref.public"

    def __init__(
        self, transport: PublicMetadataTransport, *, policy: SourcePolicyRegistry = DEFAULT_SOURCE_POLICY
    ) -> None:
        _require_transport(transport)
        if not isinstance(policy, SourcePolicyRegistry):
            raise TypeError("policy must be SourcePolicyRegistry")
        self._transport = transport
        self._policy = policy

    async def search(
        self,
        request: ScholarlyMetadataRequest,
        *,
        authorization_current: AuthorizationCurrent | None = None,
    ) -> tuple[OfficialMetadataRecord, ...]:
        if not isinstance(request, ScholarlyMetadataRequest):
            raise TypeError("request must be ScholarlyMetadataRequest")
        await _require_authorized(authorization_current)
        url = _crossref_url(request)
        self._policy.require(url)
        response = await self._transport.get(PublicMetadataHttpRequest(url=url))
        if not isinstance(response, PublicMetadataHttpResponse) or response.status != 200:
            raise OfficialMetadataError("Crossref metadata search failed")
        _require_json_media_type(response)
        document = _strict_json(response.body)
        message = document.get("message") if isinstance(document, dict) else None
        items = message.get("items") if isinstance(message, dict) else None
        if not isinstance(items, list) or len(items) > 1_000:
            raise OfficialMetadataError("Crossref metadata response is invalid")
        records: list[OfficialMetadataRecord] = []
        for item in items:
            if len(records) >= request.limit:
                break
            record = _crossref_record(item, policy=self._policy)
            if record is not None:
                records.append(record)
        return tuple(records)


class PubMedMetadataAdapter:
    """NCBI E-utilities public PubMed metadata lookup, bounded to two fixed calls."""

    provider_id = "pubmed.public"

    def __init__(
        self, transport: PublicMetadataTransport, *, policy: SourcePolicyRegistry = DEFAULT_SOURCE_POLICY
    ) -> None:
        _require_transport(transport)
        if not isinstance(policy, SourcePolicyRegistry):
            raise TypeError("policy must be SourcePolicyRegistry")
        self._transport = transport
        self._policy = policy

    async def search(
        self,
        request: ScholarlyMetadataRequest,
        *,
        authorization_current: AuthorizationCurrent | None = None,
    ) -> tuple[OfficialMetadataRecord, ...]:
        if not isinstance(request, ScholarlyMetadataRequest):
            raise TypeError("request must be ScholarlyMetadataRequest")
        await _require_authorized(authorization_current)
        search_url = _pubmed_search_url(request)
        self._policy.require(search_url)
        search_response = await self._transport.get(PublicMetadataHttpRequest(url=search_url))
        if not isinstance(search_response, PublicMetadataHttpResponse) or search_response.status != 200:
            raise OfficialMetadataError("PubMed metadata search failed")
        _require_json_media_type(search_response)
        search_document = _strict_json(search_response.body)
        ids = _pubmed_ids(search_document, limit=request.limit)
        if not ids:
            return ()
        # The summary is a second external request.  Authorization can change
        # while the first request is in flight, so re-check at this sink.
        await _require_authorized(authorization_current)
        summary_url = _pubmed_summary_url(ids)
        self._policy.require(summary_url)
        summary_response = await self._transport.get(PublicMetadataHttpRequest(url=summary_url))
        if not isinstance(summary_response, PublicMetadataHttpResponse) or summary_response.status != 200:
            raise OfficialMetadataError("PubMed metadata summary failed")
        _require_json_media_type(summary_response)
        summary_document = _strict_json(summary_response.body)
        result = summary_document.get("result") if isinstance(summary_document, dict) else None
        if not isinstance(result, dict):
            raise OfficialMetadataError("PubMed metadata response is invalid")
        records: list[OfficialMetadataRecord] = []
        for identifier in ids:
            record = _pubmed_record(identifier, result)
            if record is not None:
                records.append(record)
        return tuple(records)


def _crossref_url(request: ScholarlyMetadataRequest) -> str:
    return "https://api.crossref.org/works?" + urlencode(
        {
            "query.bibliographic": request.query,
            "rows": str(request.limit),
            "select": "DOI,title,published,container-title,publisher",
        }
    )


async def _require_authorized(authorization_current: AuthorizationCurrent | None) -> None:
    if authorization_current is None:
        return
    if not callable(authorization_current):
        raise TypeError("authorization_current must be callable")
    try:
        allowed = await authorization_current()
    except asyncio.CancelledError:
        raise
    except Exception:
        raise OfficialMetadataAuthorizationError("metadata authorization is no longer current") from None
    if allowed is not True:
        raise OfficialMetadataAuthorizationError("metadata authorization is no longer current")


def _pubmed_search_url(request: ScholarlyMetadataRequest) -> str:
    return "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urlencode(
        {"db": "pubmed", "term": request.query, "retmax": str(request.limit), "retmode": "json"}
    )


def _pubmed_summary_url(ids: tuple[str, ...]) -> str:
    return "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?" + urlencode(
        {"db": "pubmed", "id": ",".join(ids), "retmode": "json"}
    )


def _crossref_record(item: object, *, policy: SourcePolicyRegistry) -> OfficialMetadataRecord | None:
    if not isinstance(item, dict):
        return None
    doi = item.get("DOI")
    titles = item.get("title")
    if (
        not isinstance(doi, str)
        or not doi
        or not isinstance(titles, list)
        or not titles
        or not isinstance(titles[0], str)
    ):
        return None
    title = _bounded_text(titles[0], 500)
    if not title:
        return None
    url = "https://api.crossref.org/works/" + quote(doi, safe="")
    policy.require(url)
    publisher = _bounded_text(item.get("publisher", "Crossref"), 500) or "Crossref"
    container = item.get("container-title")
    snippet = (
        _bounded_text(container[0], 1_000)
        if isinstance(container, list) and container and isinstance(container[0], str)
        else publisher
    )
    return OfficialMetadataRecord(
        source=WebSearchSource(title=title, url=url, snippet=snippet, source_id=_source_id("crossref", doi)),
        provider_id="crossref.public",
        publisher=publisher,
        published_at=_crossref_published(item.get("published")),
    )


def _pubmed_ids(document: object, *, limit: int) -> tuple[str, ...]:
    result = document.get("esearchresult") if isinstance(document, dict) else None
    raw_ids = result.get("idlist") if isinstance(result, dict) else None
    if not isinstance(raw_ids, list) or len(raw_ids) > 1_000:
        raise OfficialMetadataError("PubMed identifiers are invalid")
    ids = tuple(
        identifier
        for identifier in raw_ids[:limit]
        if isinstance(identifier, str) and identifier.isascii() and identifier.isdigit() and 1 <= len(identifier) <= 12
    )
    if len(ids) != len(set(ids)):
        raise OfficialMetadataError("PubMed identifiers are duplicated")
    return ids


def _pubmed_record(identifier: str, result: dict[object, object]) -> OfficialMetadataRecord | None:
    item = result.get(identifier)
    if not isinstance(item, dict):
        return None
    title = _bounded_text(item.get("title", ""), 500)
    if not title:
        return None
    source = _bounded_text(item.get("source", "PubMed"), 500) or "PubMed"
    author = _bounded_text(item.get("sortfirstauthor", ""), 300)
    snippet = author or source
    return OfficialMetadataRecord(
        source=WebSearchSource(
            title=title,
            url=f"https://pubmed.ncbi.nlm.nih.gov/{identifier}/",
            snippet=snippet,
            source_id=_source_id("pubmed", identifier),
        ),
        provider_id="pubmed.public",
        publisher=source,
        published_at=_bounded_date(item.get("pubdate")),
    )


def _strict_json(body: bytes) -> dict[object, object]:
    if type(body) is not bytes or len(body) > _MAX_RESPONSE_BYTES:
        raise OfficialMetadataError("metadata response is invalid")
    try:
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, OfficialMetadataError):
        raise OfficialMetadataError("metadata response is invalid") from None
    if not isinstance(value, dict):
        raise OfficialMetadataError("metadata response is invalid")
    return value


def _require_json_media_type(response: PublicMetadataHttpResponse) -> None:
    media_type = response.media_type.partition(";")[0].strip().casefold()
    if media_type != "application/json":
        raise OfficialMetadataError("metadata response media type is invalid")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[object, object]:
    result: dict[object, object] = {}
    for key, value in pairs:
        if key in result:
            raise OfficialMetadataError("metadata JSON has duplicate keys")
        result[key] = value
    return result


def _reject_nonfinite(_: str) -> object:
    raise OfficialMetadataError("metadata JSON contains a non-finite value")


def _crossref_published(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    parts = value.get("date-parts")
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], list):
        return None
    date = parts[0]
    if not 1 <= len(date) <= 3 or any(isinstance(item, bool) or not isinstance(item, int) for item in date):
        return None
    try:
        return (
            datetime(date[0], date[1] if len(date) >= 2 else 1, date[2] if len(date) == 3 else 1, tzinfo=UTC)
            .date()
            .isoformat()
        )
    except ValueError:
        return None


def _bounded_date(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized[:32] or None


def _bounded_text(value: object, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    normalized = " ".join(value.split())
    return normalized[:maximum]


def _source_id(provider: str, stable_value: str) -> str:
    return f"src_{provider}_{hashlib.sha256(stable_value.encode('utf-8')).hexdigest()[:24]}"


def _require_transport(transport: object) -> None:
    if not callable(getattr(transport, "get", None)):
        raise TypeError("transport must implement get(request)")


__all__ = [
    "AiohttpPublicMetadataTransport",
    "CrossrefMetadataAdapter",
    "OfficialMetadataError",
    "OfficialMetadataRecord",
    "PublicMetadataHttpRequest",
    "PublicMetadataHttpResponse",
    "PublicMetadataTransport",
    "PubMedMetadataAdapter",
    "ScholarlyMetadataRequest",
]
