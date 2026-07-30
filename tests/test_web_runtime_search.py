from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from yonerai_discord.modules.web_runtime.search import (
    HttpsMethod,
    HttpsRequest,
    HttpsResponse,
    HttpsTransientError,
    ProviderNeutralWebSearchAdapter,
    WebBackendBlockerCode,
    WebBackendUnavailableError,
    WebSearchContractError,
    WebSearchLimits,
    WebSearchRequest,
)


class FakeHttpsTransport:
    def __init__(self, responses: list[HttpsResponse | Exception]) -> None:
        self.responses = list(responses)
        self.requests: list[HttpsRequest] = []

    async def request(self, request: HttpsRequest) -> HttpsResponse:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _document(*sources: dict[str, str]) -> bytes:
    return json.dumps({"sources": list(sources)}, ensure_ascii=False).encode("utf-8")


@pytest.mark.asyncio
async def test_disabled_and_unconfigured_backends_fail_closed_before_https() -> None:
    transport = FakeHttpsTransport([])
    disabled = ProviderNeutralWebSearchAdapter(
        backend_id="neutral-search",
        enabled=False,
        endpoint="https://search.example.test/v1/search",
        transport=transport,
    )
    with pytest.raises(WebBackendUnavailableError) as disabled_error:
        await disabled.search(WebSearchRequest("YonerAI"))
    assert disabled_error.value.code is WebBackendBlockerCode.DISABLED
    assert transport.requests == []

    unconfigured = ProviderNeutralWebSearchAdapter(backend_id="neutral-search", enabled=True)
    with pytest.raises(WebBackendUnavailableError) as unconfigured_error:
        await unconfigured.search(WebSearchRequest("YonerAI"))
    assert unconfigured_error.value.code is WebBackendBlockerCode.UNCONFIGURED


@pytest.mark.asyncio
async def test_get_search_preserves_source_attribution_and_enforces_limit() -> None:
    transport = FakeHttpsTransport(
        [
            HttpsResponse(
                200,
                _document(
                    {
                        "title": "YonerAI overview",
                        "url": "https://docs.example.test/yonerai",
                        "snippet": "Provider-neutral result.",
                        "source_id": "doc-1",
                    },
                    {
                        "title": "Second source",
                        "url": "https://example.test/second",
                        "snippet": "Second result.",
                    },
                ),
                "application/json",
            )
        ]
    )
    adapter = ProviderNeutralWebSearchAdapter(
        backend_id="neutral-search",
        enabled=True,
        endpoint="https://search.example.test/v1/search",
        transport=transport,
        limits=WebSearchLimits(max_results=5, retry_backoff_seconds=0),
    )

    result = await adapter.search(WebSearchRequest("YonerAI current facts", limit=1))

    assert result.backend_id == "neutral-search"
    assert result.attempts == 1
    assert result.sources[0].title == "YonerAI overview"
    assert result.sources[0].url == "https://docs.example.test/yonerai"
    assert result.sources[0].source_id == "doc-1"
    assert len(result.sources) == 1
    request = transport.requests[0]
    assert request.method is HttpsMethod.GET
    assert request.headers == {"accept": "application/json"}
    assert parse_qs(urlsplit(request.url).query) == {
        "q": ["YonerAI current facts"],
        "limit": ["1"],
    }


@pytest.mark.asyncio
async def test_head_healthcheck_and_bounded_retry_are_provider_neutral() -> None:
    transport = FakeHttpsTransport(
        [
            HttpsTransientError("temporary"),
            HttpsResponse(204, b""),
        ]
    )
    adapter = ProviderNeutralWebSearchAdapter(
        backend_id="neutral-search",
        enabled=True,
        endpoint="https://search.example.test/health",
        transport=transport,
        limits=WebSearchLimits(retries=1, retry_backoff_seconds=0),
    )

    assert await adapter.check_available() is True
    assert [request.method for request in transport.requests] == [HttpsMethod.HEAD, HttpsMethod.HEAD]
    assert all(request.max_response_bytes == 0 for request in transport.requests)


@pytest.mark.asyncio
async def test_retry_budget_exhaustion_and_non_success_document_are_typed() -> None:
    transient_transport = FakeHttpsTransport(
        [
            HttpsResponse(503, b""),
            HttpsResponse(503, b""),
        ]
    )
    retrying = ProviderNeutralWebSearchAdapter(
        backend_id="neutral-search",
        enabled=True,
        endpoint="https://search.example.test/v1/search",
        transport=transient_transport,
        limits=WebSearchLimits(retries=1, retry_backoff_seconds=0),
    )
    with pytest.raises(HttpsTransientError):
        await retrying.search(WebSearchRequest("bounded retry"))
    assert len(transient_transport.requests) == 2

    rejected = ProviderNeutralWebSearchAdapter(
        backend_id="neutral-search",
        enabled=True,
        endpoint="https://search.example.test/v1/search",
        transport=FakeHttpsTransport([HttpsResponse(403, b"denied")]),
    )
    with pytest.raises(WebSearchContractError, match="non-success"):
        await rejected.search(WebSearchRequest("no fallback"))


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://search.example.test/v1/search",
        "https://user:secret@search.example.test/v1/search",
        "https://search.example.test/v1/search?token=secret",
        "https://search.example.test:444/v1/search",
    ],
)
def test_backend_endpoint_requires_credential_free_https(endpoint: str) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        ProviderNeutralWebSearchAdapter(
            backend_id="neutral-search",
            enabled=True,
            endpoint=endpoint,
            transport=FakeHttpsTransport([]),
        )
