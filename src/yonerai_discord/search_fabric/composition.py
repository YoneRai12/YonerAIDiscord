"""Code-owned local Search Fabric composition.

Only the literal loopback gateway is composed here.  The gateway process owns
SearXNG access; evidence pages are fetched through the shared public-address
browser policy and a resolve-then-pin HTTP transport.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

import aiohttp

from yonerai_discord.browser_sandbox.models import BrowserPolicyError
from yonerai_discord.browser_sandbox.policy import BrowserSandboxPolicy

from .cache import BoundedEvidenceCache
from .classifier import SourceClass, SourceClassRule, SourceClassifier
from .contracts import MAX_SEARCH_JSON_BYTES, SearchIntent
from .evidence_fetcher import (
    AiohttpSafeEvidenceTransport,
    EvidenceFetchLimits,
    EvidenceFetcher,
)
from .gateway import LoopbackAddress, LoopbackSearchFabricGateway
from .document import (
    AuthorizationCurrent,
    SearchDocumentExcerpt,
    SearchDocumentFetchResult,
    SearchDocumentFindResult,
    SearchDocumentScope,
    SearchDocumentService,
)
from .official_adapters import (
    AiohttpPublicMetadataTransport,
    CrossrefMetadataAdapter,
    PubMedMetadataAdapter,
)
from .orchestrator import (
    SearchAuthorizationError,
    SearchOrchestrator,
    SearchOrchestratorOutcome,
    SearchOrchestratorPolicy,
)
from .transport import AiohttpLoopbackSearchPostTransport


class SearchCompositionError(RuntimeError):
    """Fixed configuration/runtime failure without URL or query content."""


class SearchHealthProbe(Protocol):
    async def probe(self) -> bool: ...


class _SocketDnsResolver:
    """Resolve public evidence hosts; BrowserSandboxPolicy validates every IP."""

    def resolve(self, hostname: str) -> tuple[str, ...]:
        try:
            records = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        except OSError:
            raise BrowserPolicyError("hostname did not resolve") from None
        addresses = tuple(dict.fromkeys(str(record[4][0]) for record in records if record[4]))
        if not addresses:
            raise BrowserPolicyError("hostname did not resolve")
        return addresses


@dataclass(frozen=True, slots=True)
class AiohttpLoopbackSearchHealthProbe:
    host: LoopbackAddress
    port: int
    timeout_seconds: float
    _session_factory: Callable[..., Any] = field(default=aiohttp.ClientSession, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "host", LoopbackAddress(self.host))
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65_535:
            raise ValueError("health port is outside the TCP range")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0.1 <= float(self.timeout_seconds) <= 30.0
        ):
            raise ValueError("health timeout is outside the allowed range")
        if not callable(self._session_factory):
            raise TypeError("session factory must be callable")

    async def probe(self) -> bool:
        host = self.host.value
        authority = f"[{host}]" if ":" in host else host
        timeout = aiohttp.ClientTimeout(total=float(self.timeout_seconds))
        connector = aiohttp.TCPConnector(
            use_dns_cache=False,
            limit=1,
            force_close=True,
            enable_cleanup_closed=True,
        )
        try:
            async with self._session_factory(
                connector=connector,
                connector_owner=True,
                timeout=timeout,
                trust_env=False,
            ) as session:
                async with session.get(
                    f"http://{authority}:{self.port}/healthz",
                    allow_redirects=False,
                    proxy=None,
                    headers={"accept": "application/json"},
                ) as response:
                    body = await _read_health_body(response)
                    return response.status == 200 and _decode_health(body) is True
        except asyncio.CancelledError:
            raise
        except (TimeoutError, aiohttp.ClientError, OSError, SearchCompositionError):
            return False
        finally:
            if not connector.closed:
                await connector.close()


class LocalSearchFabricRuntime:
    """One local runtime identity used by readiness and every search boundary."""

    def __init__(
        self,
        *,
        orchestrator: SearchOrchestrator,
        health_probe: SearchHealthProbe,
        document_service: SearchDocumentService | None = None,
        timeout_seconds: float = 12.0,
    ) -> None:
        if not isinstance(orchestrator, SearchOrchestrator):
            raise TypeError("orchestrator must be SearchOrchestrator")
        if not callable(getattr(health_probe, "probe", None)):
            raise TypeError("health_probe must implement probe()")
        if document_service is not None and not isinstance(document_service, SearchDocumentService):
            raise TypeError("document_service must be SearchDocumentService")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 1.0 <= float(timeout_seconds) <= 30.0
        ):
            raise ValueError("runtime timeout is outside the allowed range")
        self._orchestrator = orchestrator
        self._health_probe = health_probe
        self._document_service = document_service
        self._timeout_seconds = float(timeout_seconds)
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    async def fetch(
        self,
        url: str,
        *,
        scope: SearchDocumentScope,
        authorization_current: AuthorizationCurrent,
    ) -> SearchDocumentFetchResult:
        """Fetch one readable document through this runtime's existing fetcher."""
        service = self._document_service
        if service is None:
            raise SearchCompositionError("Search Fabric document runtime is unavailable")
        return await service.fetch(url, scope=scope, authorization_current=authorization_current)

    async def read(
        self,
        reference: str,
        *,
        scope: SearchDocumentScope,
        authorization_current: AuthorizationCurrent,
        offset: int = 0,
    ) -> SearchDocumentExcerpt:
        service = self._document_service
        if service is None:
            raise SearchCompositionError("Search Fabric document runtime is unavailable")
        return await service.read(
            reference,
            scope=scope,
            authorization_current=authorization_current,
            offset=offset,
        )

    async def find(
        self,
        reference: str,
        query: str,
        *,
        scope: SearchDocumentScope,
        authorization_current: AuthorizationCurrent,
        offset: int = 0,
    ) -> SearchDocumentFindResult:
        service = self._document_service
        if service is None:
            raise SearchCompositionError("Search Fabric document runtime is unavailable")
        return await service.find(
            reference,
            query,
            scope=scope,
            authorization_current=authorization_current,
            offset=offset,
        )

    async def probe(self) -> bool:
        try:
            ready = await self._health_probe.probe() is True
        except asyncio.CancelledError:
            raise
        except Exception:
            ready = False
        self._ready = ready
        return ready

    async def search(
        self,
        query: str,
        *,
        request_id: str,
        intent: SearchIntent,
        language: str,
        high_stakes: bool,
        authorization_current: Callable[[], Awaitable[bool]],
    ) -> SearchOrchestratorOutcome:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                outcome = await self._orchestrator.search(
                    query,
                    request_id=request_id,
                    intent=intent,
                    language=language,
                    high_stakes=high_stakes,
                    authorization_current=authorization_current,
                )
        except asyncio.CancelledError:
            raise
        except SearchAuthorizationError:
            # A request-scoped authorization change says nothing about backend
            # health.  Keeping the last probe result avoids one caller
            # poisoning global Search Fabric readiness for every guild.
            raise
        except TimeoutError:
            self._ready = False
            raise SearchCompositionError("Search Fabric request timed out") from None
        except Exception:
            self._ready = False
            raise
        self._ready = True
        return outcome


def build_local_search_fabric_runtime(settings: object) -> LocalSearchFabricRuntime | None:
    if getattr(settings, "web_search_enabled", False) is not True:
        return None
    if (
        getattr(settings, "web_search_backend", None) != "yonerai_search_gateway"
        or getattr(settings, "yonerai_search_gateway_mode", None) != "loopback"
        or getattr(settings, "search_allow_paid_fallback", None) is not False
    ):
        raise SearchCompositionError("local Search Fabric settings are invalid")
    host, port = _loopback_origin(getattr(settings, "yonerai_search_gateway_url", None))
    timeout_seconds = _bounded_number(
        getattr(settings, "search_timeout_seconds", None),
        minimum=1.0,
        maximum=30.0,
        label="search timeout",
    )
    response_bytes = _bounded_integer(
        getattr(settings, "search_max_response_bytes", None),
        minimum=16 * 1024,
        maximum=MAX_SEARCH_JSON_BYTES,
        label="search response limit",
    )
    fetch_bytes = _bounded_integer(
        getattr(settings, "search_fetch_max_bytes", None),
        minimum=64 * 1024,
        maximum=16 * 1024 * 1024,
        label="evidence fetch limit",
    )
    max_results = _bounded_integer(
        getattr(settings, "search_max_results", None),
        minimum=1,
        maximum=20,
        label="search result limit",
    )
    max_fetches = _bounded_integer(
        getattr(settings, "search_max_fetches", None),
        minimum=1,
        maximum=min(10, max_results),
        label="evidence fetch count",
    )
    cache_ttl = _bounded_integer(
        getattr(settings, "search_cache_ttl_seconds", None),
        minimum=1,
        maximum=86_400,
        label="search cache TTL",
    )
    corroboration = getattr(settings, "search_require_corroboration_for_high_stakes", None)
    if type(corroboration) is not bool:
        raise SearchCompositionError("search corroboration setting is invalid")

    gateway = LoopbackSearchFabricGateway(
        backend_ids=("searxng.local",),
        host=host,
        port=port,
        transport=AiohttpLoopbackSearchPostTransport(),
        timeout_seconds=timeout_seconds,
        max_response_bytes=response_bytes,
    )
    public_policy = BrowserSandboxPolicy(resolver=_SocketDnsResolver())
    fetcher = EvidenceFetcher(
        policy=public_policy,
        transport=AiohttpSafeEvidenceTransport(),
        limits=EvidenceFetchLimits(
            max_compressed_bytes=min(fetch_bytes, 512 * 1024),
            max_decompressed_bytes=fetch_bytes,
            max_text_chars=min(200_000, fetch_bytes),
            timeout_seconds=timeout_seconds,
        ),
    )
    metadata_transport = AiohttpPublicMetadataTransport(public_policy)
    orchestrator = SearchOrchestrator(
        gateway=gateway,
        fetcher=fetcher,
        classifier=SourceClassifier(rules=_SOURCE_CLASS_RULES),
        cache=BoundedEvidenceCache(
            ttl_seconds=float(cache_ttl),
            max_entries=max(32, max_results * 16),
            max_bytes=min(128 * 1024 * 1024, max(8 * 1024 * 1024, fetch_bytes * max_fetches)),
        ),
        policy=SearchOrchestratorPolicy(
            max_results=max_results,
            max_fetches=max_fetches,
            require_corroboration_for_high_stakes=corroboration,
        ),
        scholarly_metadata_adapters=(
            CrossrefMetadataAdapter(metadata_transport),
            PubMedMetadataAdapter(metadata_transport),
        ),
    )
    return LocalSearchFabricRuntime(
        orchestrator=orchestrator,
        health_probe=AiohttpLoopbackSearchHealthProbe(
            host=host,
            port=port,
            timeout_seconds=min(timeout_seconds, 5.0),
        ),
        document_service=SearchDocumentService(fetcher=fetcher),
        timeout_seconds=timeout_seconds,
    )


_SOURCE_CLASS_RULES = (
    SourceClassRule("docs.searxng.org", SourceClass.PRIMARY_OFFICIAL),
    SourceClassRule("docs.github.com", SourceClass.PRIMARY_OFFICIAL),
    SourceClassRule("docs.python.org", SourceClass.PRIMARY_OFFICIAL),
    SourceClassRule("www.ncbi.nlm.nih.gov", SourceClass.PRIMARY_OFFICIAL),
    SourceClassRule("api.crossref.org", SourceClass.SCHOLARLY_METADATA),
    SourceClassRule("api.openalex.org", SourceClass.SCHOLARLY_METADATA),
    SourceClassRule("pubmed.ncbi.nlm.nih.gov", SourceClass.SCHOLARLY_METADATA),
    SourceClassRule("arxiv.org", SourceClass.SCHOLARLY_METADATA),
    SourceClassRule("export.arxiv.org", SourceClass.SCHOLARLY_METADATA),
    SourceClassRule("apnews.com", SourceClass.REPUTABLE_SECONDARY),
    SourceClassRule("reuters.com", SourceClass.REPUTABLE_SECONDARY),
    SourceClassRule("reddit.com", SourceClass.COMMUNITY),
    SourceClassRule("*.reddit.com", SourceClass.COMMUNITY),
    SourceClassRule("stackoverflow.com", SourceClass.COMMUNITY),
)


async def _read_health_body(response: Any) -> bytes:
    chunks: list[bytes] = []
    consumed = 0
    async for chunk in response.content.iter_chunked(512):
        consumed += len(chunk)
        if consumed > 1_024:
            raise SearchCompositionError("Search Fabric health response exceeded its limit")
        chunks.append(bytes(chunk))
    return b"".join(chunks)


def _decode_health(body: bytes) -> bool:
    try:
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, SearchCompositionError):
        raise SearchCompositionError("Search Fabric health response is invalid") from None
    if not isinstance(value, dict) or set(value) != {"schema", "backend_id", "ready"}:
        raise SearchCompositionError("Search Fabric health response is invalid")
    return (
        value["schema"] == "yonerai.search-health.v1"
        and value["backend_id"] == "searxng.local"
        and value["ready"] is True
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise SearchCompositionError("Search Fabric health response is invalid")
        value[key] = item
    return value


def _reject_nonfinite(_: str) -> object:
    raise SearchCompositionError("Search Fabric health response is invalid")


def _loopback_origin(value: object) -> tuple[LoopbackAddress, int]:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise SearchCompositionError("Search Fabric loopback origin is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise SearchCompositionError("Search Fabric loopback origin is invalid") from None
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.hostname not in {LoopbackAddress.IPV4.value, LoopbackAddress.IPV6.value}
        or port is None
    ):
        raise SearchCompositionError("Search Fabric loopback origin is invalid")
    return LoopbackAddress(parsed.hostname), port


def _bounded_integer(value: object, *, minimum: int, maximum: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise SearchCompositionError(f"{label} is invalid")
    return value


def _bounded_number(value: object, *, minimum: float, maximum: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= float(value) <= maximum:
        raise SearchCompositionError(f"{label} is invalid")
    return float(value)


__all__ = [
    "AiohttpLoopbackSearchHealthProbe",
    "LocalSearchFabricRuntime",
    "SearchCompositionError",
    "SearchHealthProbe",
    "build_local_search_fabric_runtime",
]
