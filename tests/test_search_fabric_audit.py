from __future__ import annotations

import json

import pytest

from yonerai_discord.modules.web_runtime.search import WebSearchSource
from yonerai_discord.search_fabric.audit import append_search_outcome_audit
from yonerai_discord.search_fabric.contracts import (
    SearchCorroborationState,
    SearchEvidenceV1,
    SearchFetchState,
    SearchIntent,
    SearchResultV1,
    SearchSourceClass,
    query_digest,
)
from yonerai_discord.search_fabric.orchestrator import (
    SearchOrchestratorOutcome,
    SearchVerificationState,
)
from yonerai_discord.search_fabric.receipts import SearchReceiptV1


class _AuditDatabase:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.after_append = None

    def append_audit(self, event: str, **kwargs: object) -> int:
        self.calls.append((event, dict(kwargs)))
        if self.after_append is not None:
            self.after_append()
        return len(self.calls)


def _outcome() -> SearchOrchestratorOutcome:
    result = SearchResultV1(
        request_id="discord-mention-123456",
        query_digest=query_digest("private search phrase"),
        intent=SearchIntent.OFFICIAL,
        language="ja-JP",
        evidence=(
            SearchEvidenceV1(
                source=WebSearchSource(
                    title="Official documentation",
                    url="https://docs.example.org/private-path",
                    snippet="private search phrase",
                    source_id="src_official",
                ),
                source_class=SearchSourceClass.PRIMARY_OFFICIAL,
                fetch_state=SearchFetchState.FETCHED,
                published=None,
                retrieved="2026-07-29T00:00:00Z",
                content_hash="sha256:" + "a" * 64,
                corroboration=SearchCorroborationState.INDEPENDENT,
                verification_reasons=("official_domain", "direct_fetch"),
            ),
        ),
        backend_ids=("searxng.local",),
        engine_errors=(),
        candidate_count=1,
        cache_hits=0,
        latency_ms=12,
    )
    return SearchOrchestratorOutcome(
        result=result,
        receipt=SearchReceiptV1.from_result(result),
        verification_state=SearchVerificationState.PARTIAL,
        evidence_text="private search phrase",
    )


@pytest.mark.asyncio
async def test_search_audit_persists_only_content_free_receipt_metadata() -> None:
    database = _AuditDatabase()

    assert await append_search_outcome_audit(
        database,
        _outcome(),
        actor_id=7,
        guild_id=8,
        database_current=lambda: database,
    )

    assert len(database.calls) == 1
    event, kwargs = database.calls[0]
    assert event == "ai.search.evidence.completed"
    assert kwargs["actor_id"] == 7
    assert kwargs["guild_id"] == 8
    assert kwargs["plugin"] == "ai"
    encoded = json.dumps(kwargs["details"], ensure_ascii=False, sort_keys=True)
    assert "private search phrase" not in encoded
    assert "docs.example.org" not in encoded
    assert "discord-mention" not in encoded
    assert "sha256:" not in encoded
    assert '"paid_fallback_used": false' in encoded
    assert '"vendor_fee_class": "zero_per_query"' in encoded


@pytest.mark.asyncio
async def test_search_audit_identity_swap_after_write_fails_closed() -> None:
    database = _AuditDatabase()
    current: list[object] = [database]
    database.after_append = lambda: current.__setitem__(0, object())

    assert not await append_search_outcome_audit(
        database,
        _outcome(),
        actor_id=7,
        guild_id=8,
        database_current=lambda: current[0],
    )
    assert len(database.calls) == 1


@pytest.mark.asyncio
async def test_search_audit_missing_or_failing_port_fails_closed() -> None:
    class Failing:
        def append_audit(self, *_args: object, **_kwargs: object) -> int:
            raise RuntimeError("must not leak")

    database = Failing()
    assert not await append_search_outcome_audit(
        database,
        _outcome(),
        actor_id=7,
        guild_id=8,
        database_current=lambda: database,
    )
    assert not await append_search_outcome_audit(
        database,
        _outcome(),
        actor_id=7,
        guild_id=8,
        database_current=lambda: object(),
    )
