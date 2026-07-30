from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

import pytest

from yonerai_discord.search_fabric.contracts import (
    SearchFetchState,
    SearchIntent,
    query_digest,
)
from yonerai_discord.search_fabric.searxng import (
    AiohttpSearxngTransport,
    SEARXNG_BACKEND_ID,
    SEARXNG_INTERNAL_ORIGIN,
    SearxngDiscoveryError,
    SearxngPostRequest,
    SearxngPostResponse,
    SearxngSearchAdapter,
    SearxngSearchQuery,
)


class FakeTransport:
    def __init__(self, document: object, *, ready: bool = True) -> None:
        self.document = document
        self.ready = ready
        self.requests: list[SearxngPostRequest] = []

    async def health(self) -> bool:
        return self.ready

    async def post_search(self, request: SearxngPostRequest) -> SearxngPostResponse:
        self.requests.append(request)
        return SearxngPostResponse(
            status=200,
            body=json.dumps(self.document, ensure_ascii=False).encode(),
            media_type="application/json; charset=utf-8",
        )


def search_query(
    *,
    intent: SearchIntent = SearchIntent.GENERAL,
    query: str = "YonerAI 検索",
    limit: int = 5,
) -> SearxngSearchQuery:
    return SearxngSearchQuery(
        request_id="req-1",
        query=query,
        query_digest=query_digest(query),
        intent=intent,
        language="ja-JP",
        limit=limit,
    )


async def test_searxng_adapter_posts_form_body_and_returns_metadata_only_evidence() -> None:
    transport = FakeTransport(
        {
            "results": [
                {
                    "title": "公式資料",
                    "url": "https://docs.example.test/guide#section",
                    "content": "概要",
                },
                {
                    "title": "同じURL",
                    "url": "https://docs.example.test/guide",
                    "content": "重複",
                },
                {
                    "title": "private",
                    "url": "http://127.0.0.1/private",
                    "content": "除外",
                },
            ],
            "unresponsive_engines": [],
        }
    )
    result = await SearxngSearchAdapter(transport).search(search_query(intent=SearchIntent.OFFICIAL))

    assert result.backend_ids == (SEARXNG_BACKEND_ID,)
    assert result.candidate_count == 3
    assert len(result.evidence) == 1
    assert result.evidence[0].fetch_state is SearchFetchState.METADATA_ONLY
    assert result.evidence[0].source.url == "https://docs.example.test/guide"
    assert result.evidence[0].source.source_id.startswith("src_")
    request = transport.requests[0]
    assert request.origin == SEARXNG_INTERNAL_ORIGIN
    assert request.path == "/search"
    fields = parse_qs(request.body.decode())
    assert fields == {
        "q": ["YonerAI 検索"],
        "categories": ["general"],
        "language": ["ja-JP"],
        "format": ["json"],
        "pageno": ["1"],
    }
    assert b"YonerAI" not in SEARXNG_INTERNAL_ORIGIN.encode()


@pytest.mark.parametrize(
    ("intent", "category"),
    [
        (SearchIntent.GENERAL, "general"),
        (SearchIntent.NEWS, "news"),
        (SearchIntent.SCHOLARLY, "science"),
        (SearchIntent.CODE, "it"),
    ],
)
async def test_searxng_intent_uses_code_owned_category(intent: SearchIntent, category: str) -> None:
    transport = FakeTransport({"results": [], "unresponsive_engines": []})
    await SearxngSearchAdapter(transport).search(search_query(intent=intent))
    assert parse_qs(transport.requests[0].body.decode())["categories"] == [category]


async def test_unresponsive_engines_are_aggregated_without_raw_engine_detail() -> None:
    transport = FakeTransport(
        {
            "results": [],
            "unresponsive_engines": [["engine-with-secret-detail", "timeout body"]],
        }
    )
    result = await SearxngSearchAdapter(transport).search(search_query())
    assert [item.to_mapping() for item in result.engine_errors] == [
        {
            "backend_id": "searxng.local",
            "code": "partial_engine_failure",
        }
    ]
    assert "engine-with-secret-detail" not in repr(result)


@pytest.mark.parametrize(
    "body",
    [
        b'{"results":[],"results":[]}',
        b'{"results":"not-an-array"}',
        b'{"results":[{"title":"x","url":7}]}',
        b"\xff",
    ],
)
async def test_malformed_searxng_json_fails_without_query_or_body_in_error(body: bytes) -> None:
    class MalformedTransport(FakeTransport):
        async def post_search(self, request: SearxngPostRequest) -> SearxngPostResponse:
            self.requests.append(request)
            return SearxngPostResponse(status=200, body=body, media_type="application/json")

    private_query = "private-query-text"
    with pytest.raises(SearxngDiscoveryError) as error:
        await SearxngSearchAdapter(MalformedTransport({})).search(search_query(query=private_query))
    assert private_query not in str(error.value)
    assert repr(body) not in str(error.value)


async def test_adapter_health_is_fail_closed() -> None:
    transport = FakeTransport({"results": []}, ready=False)
    assert await SearxngSearchAdapter(transport).health() is False
    assert transport.requests == []


async def test_adapter_health_requires_a_real_engine_result_not_only_config() -> None:
    unavailable = FakeTransport(
        {
            "results": [],
            "unresponsive_engines": [["engine", "timeout"]],
        }
    )
    assert await SearxngSearchAdapter(unavailable).health() is False
    assert len(unavailable.requests) == 1

    ready = FakeTransport(
        {
            "results": [
                {
                    "title": "SearXNG documentation",
                    "url": "https://docs.searxng.org/",
                    "content": "Official documentation",
                }
            ],
            "unresponsive_engines": [],
        }
    )
    assert await SearxngSearchAdapter(ready).health() is True
    fields = parse_qs(ready.requests[0].body.decode())
    assert fields["q"] == ["SearXNG documentation"]
    assert fields["format"] == ["json"]


def test_query_digest_and_internal_origin_are_exact() -> None:
    with pytest.raises(ValueError):
        SearxngSearchQuery(
            request_id="req-1",
            query="query",
            query_digest=query_digest("other"),
            intent=SearchIntent.GENERAL,
            language="ja-JP",
            limit=5,
        )
    with pytest.raises(ValueError):
        SearxngPostRequest(
            body=b"q=query",
            timeout_seconds=8,
            origin="http://attacker:8080",
        )


class _HealthContent:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, _size: int):
        yield self._body


class _HealthResponse:
    def __init__(self, body: bytes) -> None:
        self.status = 200
        self.content = _HealthContent(body)

    async def __aenter__(self) -> _HealthResponse:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _HealthSession:
    def __init__(self, body: bytes, calls: list[dict[str, object]]) -> None:
        self._body = body
        self._calls = calls

    async def __aenter__(self) -> _HealthSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def get(self, url: str, **kwargs: object) -> _HealthResponse:
        self._calls.append({"url": url, **kwargs})
        return _HealthResponse(self._body)


async def test_real_searxng_health_uses_bounded_config_without_proxy_or_redirect() -> None:
    calls: list[dict[str, object]] = []
    session_options: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> _HealthSession:
        session_options.append(kwargs)
        return _HealthSession(b'{"brand":"private"}', calls)

    assert await AiohttpSearxngTransport(session_factory=factory).health() is True
    assert calls == [
        {
            "url": "http://searxng:8080/config",
            "allow_redirects": False,
            "proxy": None,
        }
    ]
    assert session_options[0]["trust_env"] is False


async def test_real_searxng_health_rejects_malformed_config() -> None:
    def factory(**_kwargs: Any) -> _HealthSession:
        return _HealthSession(b'{"duplicate":1,"duplicate":2}', [])

    assert await AiohttpSearxngTransport(session_factory=factory).health() is False
