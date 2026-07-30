from __future__ import annotations

import json

import pytest

from yonerai_discord.search_fabric.contracts import SearchSourceClass
from yonerai_discord.search_fabric.official_adapters import (
    CrossrefMetadataAdapter,
    OfficialMetadataError,
    PublicMetadataHttpRequest,
    PublicMetadataHttpResponse,
    PubMedMetadataAdapter,
    ScholarlyMetadataRequest,
)
from yonerai_discord.search_fabric.source_policy import (
    DEFAULT_SOURCE_POLICY,
    SourcePolicyError,
    SourcePolicyRegistry,
    SourcePolicyRule,
)


class _FakeTransport:
    def __init__(self, *responses: PublicMetadataHttpResponse) -> None:
        self.responses = list(responses)
        self.requests: list[PublicMetadataHttpRequest] = []

    async def get(self, request: PublicMetadataHttpRequest) -> PublicMetadataHttpResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected metadata request")
        return self.responses.pop(0)


def _response(document: object, *, status: int = 200) -> PublicMetadataHttpResponse:
    return PublicMetadataHttpResponse(
        status=status,
        body=json.dumps(document, separators=(",", ":")).encode("utf-8"),
        media_type="application/json",
    )


@pytest.mark.asyncio
async def test_crossref_returns_bounded_metadata_not_content_truth() -> None:
    transport = _FakeTransport(
        _response(
            {
                "message": {
                    "items": [
                        {
                            "DOI": "10.1000/example",
                            "title": ["Verified metadata title"],
                            "publisher": "Example Publisher",
                            "container-title": ["Example Journal"],
                            "published": {"date-parts": [[2026, 7, 29]]},
                        }
                    ]
                }
            }
        )
    )
    records = await CrossrefMetadataAdapter(transport).search(ScholarlyMetadataRequest(query="search fabric"))

    assert len(records) == 1
    assert records[0].source_class is SearchSourceClass.SCHOLARLY_METADATA
    assert records[0].source.url == "https://api.crossref.org/works/10.1000%2Fexample"
    assert records[0].published_at == "2026-07-29"
    assert transport.requests[0].url.startswith("https://api.crossref.org/works?")
    assert "search+fabric" in transport.requests[0].url
    assert "search fabric" not in repr(transport.requests[0])


@pytest.mark.asyncio
async def test_crossref_rejects_non_success_and_malformed_payload() -> None:
    with pytest.raises(OfficialMetadataError):
        await CrossrefMetadataAdapter(_FakeTransport(_response({}, status=429))).search(
            ScholarlyMetadataRequest(query="x")
        )
    with pytest.raises(OfficialMetadataError):
        await CrossrefMetadataAdapter(_FakeTransport(_response({"message": {"items": "bad"}}))).search(
            ScholarlyMetadataRequest(query="x")
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_kind", ("crossref", "pubmed"))
async def test_official_metadata_adapters_reject_non_json_media_type(adapter_kind: str) -> None:
    response = PublicMetadataHttpResponse(
        status=200,
        body=b'{"message":{"items":[]},"esearchresult":{"idlist":[]}}',
        media_type="text/html; charset=utf-8",
    )
    transport = _FakeTransport(response)
    adapter = CrossrefMetadataAdapter(transport) if adapter_kind == "crossref" else PubMedMetadataAdapter(transport)

    with pytest.raises(OfficialMetadataError, match="media type"):
        await adapter.search(ScholarlyMetadataRequest(query="x"))


@pytest.mark.asyncio
async def test_pubmed_uses_two_fixed_eutilities_calls_and_returns_metadata() -> None:
    transport = _FakeTransport(
        _response({"esearchresult": {"idlist": ["12345", "67890"]}}),
        _response(
            {
                "result": {
                    "uids": ["12345", "67890"],
                    "12345": {
                        "title": "A PubMed metadata record",
                        "source": "Test Journal",
                        "sortfirstauthor": "Researcher",
                        "pubdate": "2026 Jul 29",
                    },
                    "67890": {"title": "Second record", "source": "Other Journal"},
                }
            }
        ),
    )
    records = await PubMedMetadataAdapter(transport).search(ScholarlyMetadataRequest(query="evidence safety", limit=2))

    assert [record.source.url for record in records] == [
        "https://pubmed.ncbi.nlm.nih.gov/12345/",
        "https://pubmed.ncbi.nlm.nih.gov/67890/",
    ]
    assert all(record.source_class is SearchSourceClass.SCHOLARLY_METADATA for record in records)
    assert len(transport.requests) == 2
    assert all(
        request.url.startswith("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/") for request in transport.requests
    )
    assert "evidence safety" not in repr(transport.requests[0])


@pytest.mark.asyncio
async def test_pubmed_rechecks_fresh_authorization_before_second_external_call() -> None:
    transport = _FakeTransport(
        _response({"esearchresult": {"idlist": ["12345"]}}),
        _response({"result": {"uids": ["12345"], "12345": {"title": "must not be read"}}}),
    )
    decisions = iter((True, False))

    async def authorization_current() -> bool:
        return next(decisions)

    with pytest.raises(PermissionError, match="authorization"):
        await PubMedMetadataAdapter(transport).search(
            ScholarlyMetadataRequest(query="bounded"),
            authorization_current=authorization_current,
        )

    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_pubmed_rejects_duplicate_or_invalid_ids_before_summary() -> None:
    transport = _FakeTransport(_response({"esearchresult": {"idlist": ["10", "10"]}}))
    with pytest.raises(OfficialMetadataError):
        await PubMedMetadataAdapter(transport).search(ScholarlyMetadataRequest(query="x"))
    assert len(transport.requests) == 1


def test_metadata_request_bounds_and_query_is_not_represented() -> None:
    with pytest.raises(ValueError):
        ScholarlyMetadataRequest(query=" ")
    with pytest.raises(ValueError):
        ScholarlyMetadataRequest(query="x", limit=11)
    request = ScholarlyMetadataRequest(query="private phrase")
    assert "private phrase" not in repr(request)


def test_policy_is_fixed_https_exact_host_and_path_only() -> None:
    assert DEFAULT_SOURCE_POLICY.allows("https://api.crossref.org/works?rows=1")
    assert DEFAULT_SOURCE_POLICY.allows("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed")
    assert not DEFAULT_SOURCE_POLICY.allows("http://api.crossref.org/works")
    assert not DEFAULT_SOURCE_POLICY.allows("https://api.crossref.org/anything")
    assert not DEFAULT_SOURCE_POLICY.allows("https://evil.example/works")
    with pytest.raises(SourcePolicyError):
        DEFAULT_SOURCE_POLICY.require("https://api.crossref.org/anything")


def test_policy_registry_does_not_support_model_added_domains() -> None:
    policy = SourcePolicyRegistry((SourcePolicyRule("docs.example.org", ("/api/",)),))
    assert policy.allows("https://docs.example.org/api/v1")
    assert not policy.allows("https://sub.docs.example.org/api/v1")
    assert not policy.allows("https://docs.example.org/other")
    with pytest.raises(ValueError):
        SourcePolicyRule("example.org", ("../bad",))


def test_response_and_url_contract_reject_credentials_redirect_targets_and_large_values() -> None:
    with pytest.raises(ValueError):
        PublicMetadataHttpRequest(url="https://user:pass@api.crossref.org/works")
    with pytest.raises(ValueError):
        PublicMetadataHttpRequest(url="https://api.crossref.org/works#fragment")
    with pytest.raises(TypeError):
        PublicMetadataHttpResponse(status=200, body="not-bytes")  # type: ignore[arg-type]
