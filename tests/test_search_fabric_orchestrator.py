from __future__ import annotations

from dataclasses import replace

import pytest

from yonerai_discord.modules.web_runtime.search import WebSearchSource
from yonerai_discord.search_fabric.cache import BoundedEvidenceCache
from yonerai_discord.search_fabric.classifier import SourceClass, SourceClassifier, SourceClassRule
from yonerai_discord.search_fabric.contracts import (
    SearchCorroborationState,
    SearchEvidenceV1,
    SearchFetchState,
    SearchIntent,
    SearchResultV1,
    SearchSourceClass,
    query_digest,
)
from yonerai_discord.search_fabric.evidence_fetcher import EvidenceFetchError, FetchedEvidence
from yonerai_discord.search_fabric.orchestrator import (
    SearchAuthorizationError,
    SearchOrchestrator,
    SearchVerificationState,
)
from yonerai_discord.search_fabric.official_adapters import (
    OfficialMetadataRecord,
    ScholarlyMetadataRequest,
)
from yonerai_discord.search_fabric.receipts import SearchGatewayOutcome, SearchReceiptV1


def _metadata(*, source_id: str, url: str) -> SearchEvidenceV1:
    return SearchEvidenceV1(
        source=WebSearchSource(
            title=f"Title {source_id}",
            url=url,
            snippet=f"Snippet {source_id}",
            source_id=source_id,
        ),
        source_class=SearchSourceClass.UNKNOWN,
        fetch_state=SearchFetchState.METADATA_ONLY,
        published=None,
        retrieved="2026-07-29T00:00:00Z",
        content_hash=None,
        corroboration=SearchCorroborationState.UNKNOWN,
        verification_reasons=("fetch_not_performed",),
    )


class _Gateway:
    def __init__(self, result: SearchResultV1) -> None:
        self.result = result
        self.calls = 0

    async def search(self, request, *, request_id, intent, language):
        self.calls += 1
        assert request.query == "bounded query"
        assert (request_id, intent, language) == ("request-1", SearchIntent.OFFICIAL, "ja-JP")
        return SearchGatewayOutcome(self.result, SearchReceiptV1.from_result(self.result))


class _Fetcher:
    def __init__(self, values: dict[str, FetchedEvidence | Exception]) -> None:
        self.values = values
        self.calls: list[str] = []

    async def fetch(self, url: str) -> FetchedEvidence:
        self.calls.append(url)
        value = self.values[url]
        if isinstance(value, Exception):
            raise value
        return value


class _MetadataAdapter:
    provider_id = "pubmed.public"

    def __init__(self, record: OfficialMetadataRecord) -> None:
        self.record = record
        self.calls: list[ScholarlyMetadataRequest] = []

    async def search(
        self,
        request: ScholarlyMetadataRequest,
        *,
        authorization_current=None,
    ) -> tuple[OfficialMetadataRecord, ...]:
        assert authorization_current is not None
        self.calls.append(request)
        return (self.record,)


def _fetched(url: str, *, host: str, text: str, digest: str) -> FetchedEvidence:
    return FetchedEvidence(
        canonical_url=url,
        hostname=host,
        media_type="text/html",
        title="Fetched title",
        text=text,
        content_hash=f"sha256:{digest * 64}",
        redirect_count=0,
    )


def _result() -> SearchResultV1:
    items = (
        _metadata(source_id="official", url="https://docs.example.test/a"),
        _metadata(source_id="secondary", url="https://news.example.test/b"),
        _metadata(source_id="failed", url="https://failed.example.test/c"),
    )
    return SearchResultV1(
        request_id="request-1",
        query_digest=query_digest("bounded query"),
        intent=SearchIntent.OFFICIAL,
        language="ja-JP",
        evidence=items,
        backend_ids=("searxng.local",),
        engine_errors=(),
        candidate_count=3,
        cache_hits=0,
        latency_ms=7,
    )


def _orchestrator(fetcher: _Fetcher, *, cache: BoundedEvidenceCache | None = None) -> SearchOrchestrator:
    return SearchOrchestrator(
        gateway=_Gateway(_result()),
        fetcher=fetcher,  # type: ignore[arg-type]
        classifier=SourceClassifier(
            rules=(
                SourceClassRule("docs.example.test", SourceClass.PRIMARY_OFFICIAL),
                SourceClassRule("news.example.test", SourceClass.REPUTABLE_SECONDARY),
            )
        ),
        cache=cache or BoundedEvidenceCache(),
        clock=lambda: 1_785_283_200.0,
    )


@pytest.mark.asyncio
async def test_orchestrator_fetches_classifies_corroborates_and_keeps_paid_fallback_false() -> None:
    fetcher = _Fetcher(
        {
            "https://docs.example.test/a": _fetched(
                "https://docs.example.test/a",
                host="docs.example.test",
                text="official evidence",
                digest="a",
            ),
            "https://news.example.test/b": _fetched(
                "https://news.example.test/b",
                host="news.example.test",
                text="independent evidence",
                digest="b",
            ),
            "https://failed.example.test/c": EvidenceFetchError("private upstream detail"),
        }
    )

    outcome = await _orchestrator(fetcher).search(
        "bounded query",
        request_id="request-1",
        intent=SearchIntent.OFFICIAL,
        high_stakes=True,
        authorization_current=_allowed,
    )

    assert outcome.verification_state is SearchVerificationState.INSUFFICIENT
    assert [item.fetch_state for item in outcome.result.evidence] == [
        SearchFetchState.FETCHED,
        SearchFetchState.FETCHED,
        SearchFetchState.FAILED,
    ]
    assert outcome.result.evidence[0].source_class is SearchSourceClass.PRIMARY_OFFICIAL
    assert outcome.result.evidence[1].source_class is SearchSourceClass.REPUTABLE_SECONDARY
    assert outcome.result.evidence[0].freshness_state.value == "unknown"
    assert outcome.result.evidence[0].corroboration_group.startswith("cg_")
    assert outcome.receipt.paid_fallback_used is False
    assert outcome.receipt.vendor_fee_class.value == "zero_per_query"
    assert "Snippet failed" not in outcome.evidence_text
    assert "private upstream detail" not in repr(outcome)
    assert "bounded query" not in repr(outcome)


@pytest.mark.asyncio
async def test_scholarly_intent_routes_public_metadata_adapter_before_discovery_fetch() -> None:
    metadata_url = "https://pubmed.ncbi.nlm.nih.gov/12345/"
    record = OfficialMetadataRecord(
        source=WebSearchSource(
            title="Metadata title",
            url=metadata_url,
            snippet="Public metadata only",
            source_id="src_pubmed_public_12345",
        ),
        provider_id="pubmed.public",
        publisher="NCBI",
        published_at="2026-07-01",
    )
    adapter = _MetadataAdapter(record)
    result = replace(
        _result(),
        intent=SearchIntent.SCHOLARLY,
        evidence=(),
        candidate_count=0,
    )

    class ScholarlyGateway:
        async def search(self, request, *, request_id, intent, language):
            assert request.query == "bounded query"
            assert (request_id, intent, language) == ("request-1", SearchIntent.SCHOLARLY, "ja-JP")
            return SearchGatewayOutcome(result, SearchReceiptV1.from_result(result))

    orchestrator = SearchOrchestrator(
        gateway=ScholarlyGateway(),
        fetcher=_Fetcher(
            {
                metadata_url: _fetched(
                    metadata_url,
                    host="pubmed.ncbi.nlm.nih.gov",
                    text="Fetched PubMed metadata page",
                    digest="e",
                )
            }
        ),
        classifier=SourceClassifier(
            rules=(SourceClassRule("pubmed.ncbi.nlm.nih.gov", SourceClass.SCHOLARLY_METADATA),)
        ),
        cache=BoundedEvidenceCache(),
        scholarly_metadata_adapters=(adapter,),
        clock=lambda: 1_785_283_200.0,
    )

    outcome = await orchestrator.search(
        "bounded query",
        request_id="request-1",
        intent=SearchIntent.SCHOLARLY,
        authorization_current=_allowed,
    )

    assert len(adapter.calls) == 1
    assert adapter.calls[0].limit == 5
    assert outcome.result.backend_ids == ("searxng.local", "pubmed.public")
    assert outcome.result.evidence[0].source.url == metadata_url
    assert outcome.result.evidence[0].fetch_state is SearchFetchState.FETCHED


@pytest.mark.asyncio
async def test_orchestrator_cache_avoids_second_fetch_and_receipt_counts_hits() -> None:
    cache = BoundedEvidenceCache()
    fetcher = _Fetcher(
        {
            "https://docs.example.test/a": _fetched(
                "https://docs.example.test/a",
                host="docs.example.test",
                text="official evidence",
                digest="a",
            ),
            "https://news.example.test/b": _fetched(
                "https://news.example.test/b",
                host="news.example.test",
                text="secondary evidence",
                digest="b",
            ),
            "https://failed.example.test/c": EvidenceFetchError("unavailable"),
        }
    )
    orchestrator = _orchestrator(fetcher, cache=cache)

    await orchestrator.search(
        "bounded query",
        request_id="request-1",
        intent=SearchIntent.OFFICIAL,
        authorization_current=_allowed,
    )
    calls_after_first = list(fetcher.calls)
    second = await orchestrator.search(
        "bounded query",
        request_id="request-1",
        intent=SearchIntent.OFFICIAL,
        authorization_current=_allowed,
    )

    assert fetcher.calls.count("https://docs.example.test/a") == 1
    assert fetcher.calls.count("https://news.example.test/b") == 1
    assert calls_after_first[-1] == "https://failed.example.test/c"
    assert second.receipt.cache_hits == 2


@pytest.mark.asyncio
async def test_non_high_stakes_duplicate_pages_never_become_verified_by_count() -> None:
    fetcher = _Fetcher(
        {
            "https://docs.example.test/a": _fetched(
                "https://docs.example.test/a",
                host="docs.example.test",
                text="same syndicated text",
                digest="d",
            ),
            "https://news.example.test/b": _fetched(
                "https://news.example.test/b",
                host="news.example.test",
                text="same syndicated text",
                digest="d",
            ),
            "https://failed.example.test/c": EvidenceFetchError("unavailable"),
        }
    )
    outcome = await _orchestrator(fetcher).search(
        "bounded query",
        request_id="request-1",
        intent=SearchIntent.OFFICIAL,
        authorization_current=_allowed,
    )
    assert outcome.verification_state is SearchVerificationState.PARTIAL
    assert all(item.corroboration is not SearchCorroborationState.INDEPENDENT for item in outcome.result.evidence)


@pytest.mark.asyncio
async def test_orchestrator_revocation_after_discovery_returns_no_evidence() -> None:
    gateway = _Gateway(_result())
    fetcher = _Fetcher({})
    checks = iter((True, False))

    async def authorization() -> bool:
        return next(checks)

    orchestrator = SearchOrchestrator(
        gateway=gateway,
        fetcher=fetcher,  # type: ignore[arg-type]
        classifier=SourceClassifier(),
        cache=BoundedEvidenceCache(),
    )
    with pytest.raises(SearchAuthorizationError):
        await orchestrator.search(
            "bounded query",
            request_id="request-1",
            intent=SearchIntent.OFFICIAL,
            authorization_current=authorization,
        )
    assert gateway.calls == 1
    assert fetcher.calls == []


@pytest.mark.asyncio
async def test_high_stakes_single_unknown_source_is_insufficient() -> None:
    result = replace(_result(), evidence=(_result().evidence[2],), candidate_count=1)
    gateway = _Gateway(result)
    fetcher = _Fetcher({"https://failed.example.test/c": EvidenceFetchError("unavailable")})
    orchestrator = SearchOrchestrator(
        gateway=gateway,
        fetcher=fetcher,  # type: ignore[arg-type]
        classifier=SourceClassifier(),
        cache=BoundedEvidenceCache(),
    )

    outcome = await orchestrator.search(
        "bounded query",
        request_id="request-1",
        intent=SearchIntent.OFFICIAL,
        high_stakes=True,
        authorization_current=_allowed,
    )

    assert outcome.verification_state is SearchVerificationState.INSUFFICIENT
    assert "検証状態: insufficient" in outcome.evidence_text


async def _allowed() -> bool:
    return True
