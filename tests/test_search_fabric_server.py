from __future__ import annotations

from typing import Any

from aiohttp.test_utils import TestClient, TestServer
import pytest

from yonerai_discord.modules.web_runtime.search import (
    HttpsResponse,
    JsonSearchDocumentDecoder,
    WebSearchSource,
)
from yonerai_discord.search_fabric.contracts import (
    SearchCorroborationState,
    SearchEvidenceV1,
    SearchFabricJsonCodec,
    SearchFetchState,
    SearchFreshnessState,
    SearchIntent,
    SearchResultV1,
    SearchSourceClass,
)
from yonerai_discord.search_fabric.gateway import (
    LoopbackAddress,
    LoopbackSearchPostRequest,
)
from yonerai_discord.search_fabric.searxng import (
    SEARXNG_BACKEND_ID,
    SearxngDiscoveryError,
    SearxngSearchQuery,
)
from yonerai_discord.search_fabric.server import SearchGatewayServer, build_application
from yonerai_discord.search_fabric.transport import (
    AiohttpLoopbackSearchPostTransport,
    LoopbackSearchTransportError,
)


class FakeBackend:
    def __init__(self, *, ready: bool = True, fail: bool = False) -> None:
        self.ready = ready
        self.fail = fail
        self.queries: list[SearxngSearchQuery] = []

    async def health(self) -> bool:
        return self.ready

    async def search(self, query: SearxngSearchQuery) -> SearchResultV1:
        self.queries.append(query)
        if self.fail:
            raise SearxngDiscoveryError("fixed backend failure")
        return SearchResultV1(
            request_id=query.request_id,
            query_digest=query.query_digest,
            intent=query.intent,
            language=query.language,
            evidence=(),
            backend_ids=(SEARXNG_BACKEND_ID,),
            engine_errors=(),
            candidate_count=0,
            cache_hits=0,
            latency_ms=1,
        )


def request_body(query: str = "安全な検索") -> bytes:
    return SearchFabricJsonCodec.encode_request(
        request_id="request-1",
        query=query,
        intent=SearchIntent.GENERAL,
        language="ja-JP",
        limit=5,
    )


async def test_server_exposes_exact_existing_provider_neutral_projection() -> None:
    class CompatBackend(FakeBackend):
        async def search(self, query: SearxngSearchQuery) -> SearchResultV1:
            result = await super().search(query)
            evidence = SearchEvidenceV1(
                source=WebSearchSource(
                    title="Fetched official source",
                    url="https://example.org/source",
                    snippet="Fetched snippet",
                    source_id="src_fetched",
                ),
                source_class=SearchSourceClass.PRIMARY_OFFICIAL,
                fetch_state=SearchFetchState.FETCHED,
                published=None,
                retrieved="2026-07-29T00:00:00Z",
                content_hash="sha256:" + ("a" * 64),
                freshness_state=SearchFreshnessState.CURRENT,
                corroboration=SearchCorroborationState.UNKNOWN,
                verification_reasons=("direct_fetch",),
            )
            return SearchResultV1(
                request_id=result.request_id,
                query_digest=result.query_digest,
                intent=result.intent,
                language=result.language,
                evidence=(evidence,),
                backend_ids=result.backend_ids,
                engine_errors=result.engine_errors,
                candidate_count=1,
                cache_hits=0,
                latency_ms=result.latency_ms,
            )

    client = TestClient(TestServer(SearchGatewayServer(CompatBackend()).create_application()))
    await client.start_server()
    try:
        response = await client.post(
            "/v1/search/compat",
            data=request_body(),
            headers={"content-type": "application/json"},
        )
        body = await response.read()
    finally:
        await client.close()

    assert response.status == 200
    assert await response.json() == {
        "sources": [
            {
                "snippet": "Fetched snippet",
                "source_id": "src_fetched",
                "title": "Fetched official source",
                "url": "https://example.org/source",
            }
        ]
    }
    decoded = JsonSearchDocumentDecoder().decode(
        HttpsResponse(status=200, body=body, media_type="application/json"),
        limit=5,
    )
    assert decoded == (
        WebSearchSource(
            title="Fetched official source",
            url="https://example.org/source",
            snippet="Fetched snippet",
            source_id="src_fetched",
        ),
    )


async def test_server_health_and_exact_search_route() -> None:
    backend = FakeBackend()
    client = TestClient(TestServer(SearchGatewayServer(backend).create_application()))
    await client.start_server()
    try:
        health = await client.get("/healthz")
        response = await client.post(
            "/v1/search",
            data=request_body(),
            headers={"content-type": "application/json"},
        )
        wrong_method = await client.get("/v1/search")
        health_document = await health.json()
        document = await response.json()
    finally:
        await client.close()

    assert health.status == 200
    assert health_document == {
        "backend_id": "searxng.local",
        "ready": True,
        "schema": "yonerai.search-health.v1",
    }
    assert response.status == 200
    assert len(backend.queries) == 1
    assert document["request_id"] == "request-1"
    assert wrong_method.status == 405
    for actual in (health, response, wrong_method):
        assert actual.headers["Cache-Control"] == "no-store"
        assert actual.headers["X-Content-Type-Options"] == "nosniff"


async def test_health_is_not_ready_when_searxng_is_unavailable() -> None:
    client = TestClient(TestServer(SearchGatewayServer(FakeBackend(ready=False)).create_application()))
    await client.start_server()
    try:
        response = await client.get("/healthz")
        document = await response.json()
    finally:
        await client.close()
    assert response.status == 503
    assert document["ready"] is False


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (b'{"schema":"yonerai.search-request.v1","unexpected":true}', "application/json"),
        (b'{"schema":"x","schema":"y"}', "application/json"),
        (request_body(), "text/plain"),
    ],
)
async def test_invalid_request_is_consumed_before_backend(
    body: bytes,
    content_type: str,
) -> None:
    backend = FakeBackend()
    client = TestClient(TestServer(SearchGatewayServer(backend).create_application()))
    await client.start_server()
    try:
        response = await client.post(
            "/v1/search",
            data=body,
            headers={"content-type": content_type},
        )
    finally:
        await client.close()
    assert response.status in {400, 415}
    assert backend.queries == []


async def test_backend_failure_does_not_expose_query_or_exception() -> None:
    backend = FakeBackend(fail=True)
    private_query = "private user query"
    client = TestClient(TestServer(SearchGatewayServer(backend).create_application()))
    await client.start_server()
    try:
        response = await client.post(
            "/v1/search",
            data=request_body(private_query),
            headers={"content-type": "application/json"},
        )
        text = await response.text()
    finally:
        await client.close()
    assert response.status == 503
    assert private_query not in text
    assert "fixed backend failure" not in text


def test_build_application_rejects_arbitrary_searxng_origin() -> None:
    with pytest.raises(ValueError):
        build_application(searxng_origin="http://127.0.0.1:8080")


class FakeContent:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, _size: int):
        yield self._body


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.status = 200
        self.headers = {"content-type": "application/json"}
        self.content = FakeContent(body)

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class FakeSession:
    def __init__(self, response: FakeResponse, calls: list[dict[str, Any]]) -> None:
        self._response = response
        self._calls = calls

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self._calls.append({"url": url, **kwargs})
        return self._response


async def test_real_loopback_transport_disables_proxy_redirect_and_dns() -> None:
    calls: list[dict[str, Any]] = []
    session_options: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> FakeSession:
        session_options.append(kwargs)
        return FakeSession(FakeResponse(b'{"ok":true}'), calls)

    response = await AiohttpLoopbackSearchPostTransport(session_factory=factory).post(
        LoopbackSearchPostRequest(
            host=LoopbackAddress.IPV4,
            port=8768,
            body=b"{}",
            timeout_seconds=2,
            max_response_bytes=1_024,
        )
    )
    assert response.body == b'{"ok":true}'
    assert calls[0]["url"] == "http://127.0.0.1:8768/v1/search"
    assert calls[0]["allow_redirects"] is False
    assert calls[0]["proxy"] is None
    assert session_options[0]["trust_env"] is False
    assert session_options[0]["connector"]._use_dns_cache is False


async def test_loopback_transport_enforces_response_limit() -> None:
    def factory(**_kwargs: Any) -> FakeSession:
        return FakeSession(FakeResponse(b"x" * 1_025), [])

    with pytest.raises(LoopbackSearchTransportError):
        await AiohttpLoopbackSearchPostTransport(session_factory=factory).post(
            LoopbackSearchPostRequest(
                host=LoopbackAddress.IPV4,
                port=8768,
                body=b"{}",
                timeout_seconds=2,
                max_response_bytes=1_024,
            )
        )
