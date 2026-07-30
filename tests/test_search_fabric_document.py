from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from yonerai_discord.browser_sandbox.policy import BrowserSandboxPolicy, StaticDnsResolver
from yonerai_discord.search_fabric.composition import LocalSearchFabricRuntime
from yonerai_discord.search_fabric.document import (
    BoundedSearchDocumentStore,
    SearchDocumentAuthorizationError,
    SearchDocumentFindResult,
    SearchDocumentLimitError,
    SearchDocumentNotFoundError,
    SearchDocumentScope,
    SearchDocumentService,
    SearchDocumentStoreLimits,
    SearchDocumentUnsupportedError,
)
from yonerai_discord.search_fabric.evidence_fetcher import (
    EvidenceFetcher,
    EvidenceHttpRequest,
    EvidenceHttpResponse,
)
from yonerai_discord.search_fabric.orchestrator import SearchOrchestrator


_PUBLIC_IP = "93.184.216.34"


@dataclass
class _Transport:
    bodies: list[bytes]
    calls: list[EvidenceHttpRequest] = field(default_factory=list)
    after_fetch: object | None = None

    async def fetch(self, request: EvidenceHttpRequest) -> EvidenceHttpResponse:
        self.calls.append(request)
        if not self.bodies:
            raise AssertionError("unexpected fetch")
        callback = self.after_fetch
        if callable(callback):
            callback()
        body = self.bodies.pop(0)
        return EvidenceHttpResponse(
            status=200,
            body=body,
            peer_address=_PUBLIC_IP,
            content_type="text/plain; charset=utf-8",
            content_length=len(body),
        )


def _fetcher(transport: _Transport) -> EvidenceFetcher:
    return EvidenceFetcher(
        policy=BrowserSandboxPolicy(resolver=StaticDnsResolver({"example.test": (_PUBLIC_IP,)})),
        transport=transport,
    )


def _scope(
    *, request: str = "request-a", guild: str = "guild-a", channel: str = "channel-a", user: str = "user-a"
) -> SearchDocumentScope:
    return SearchDocumentScope(request_id=request, guild_id=guild, channel_id=channel, user_id=user)


async def _allowed() -> bool:
    return True


async def test_fetch_stores_bounded_preview_and_opaque_reference_without_sensitive_repr() -> None:
    transport = _Transport([("alpha " * 300).encode("utf-8")])
    service = SearchDocumentService(
        fetcher=_fetcher(transport),
        limits=SearchDocumentStoreLimits(max_excerpt_chars=120),
    )

    result = await service.fetch("https://example.test/article", scope=_scope(), authorization_current=_allowed)

    assert len(transport.calls) == 1
    assert len(result.preview) == 120
    assert result.next_offset == 120
    assert result.media_type == "text/plain"
    assert len(result.reference) == 32
    rendered = repr(result)
    assert "example.test" not in rendered
    assert "alpha" not in rendered
    assert result.reference not in rendered
    assert result.content_hash not in rendered


async def test_continuation_allows_new_request_but_rejects_other_guild_channel_or_user() -> None:
    service = SearchDocumentService(fetcher=_fetcher(_Transport([b"one two three"])))
    fetched = await service.fetch("https://example.test/article", scope=_scope(), authorization_current=_allowed)

    same_access_new_request = await service.read(
        fetched.reference,
        scope=_scope(request="request-b"),
        authorization_current=_allowed,
    )
    assert same_access_new_request.text == "one two three"
    for denied_scope in (_scope(guild="other"), _scope(channel="other"), _scope(user="other")):
        with pytest.raises(SearchDocumentNotFoundError, match=r"\Adocument is unavailable\Z"):
            await service.read(fetched.reference, scope=denied_scope, authorization_current=_allowed)


async def test_find_is_bounded_has_continuation_and_rejects_invalid_query() -> None:
    text = " ".join(f"needle-{index}" for index in range(12))
    service = SearchDocumentService(
        fetcher=_fetcher(_Transport([text.encode("utf-8")])),
        limits=SearchDocumentStoreLimits(max_find_hits=3, max_find_context_chars=80),
    )
    fetched = await service.fetch("https://example.test/article", scope=_scope(), authorization_current=_allowed)

    first = await service.find(
        fetched.reference, "needle", scope=_scope(request="next"), authorization_current=_allowed
    )
    assert isinstance(first, SearchDocumentFindResult)
    assert len(first.hits) == 3
    assert first.next_offset is not None
    assert all(len(hit.text) <= 80 for hit in first.hits)
    second = await service.find(
        fetched.reference,
        "needle",
        scope=_scope(request="later"),
        authorization_current=_allowed,
        offset=first.next_offset,
    )
    assert second.hits[0].offset > first.hits[-1].offset
    no_hit = await service.find(
        fetched.reference,
        "absent",
        scope=_scope(request="later-again"),
        authorization_current=_allowed,
    )
    assert no_hit.hits == ()
    assert no_hit.next_offset is None
    with pytest.raises(SearchDocumentLimitError, match=r"\Adocument query is invalid\Z"):
        await service.find(fetched.reference, "", scope=_scope(), authorization_current=_allowed)
    with pytest.raises(SearchDocumentLimitError, match=r"\Adocument query is invalid\Z"):
        await service.find(fetched.reference, "bad\x00query", scope=_scope(), authorization_current=_allowed)


async def test_fetch_rechecks_authorization_after_network_read_and_does_not_return_reference() -> None:
    states = iter((True, True, False))

    async def authorization_current() -> bool:
        return next(states)

    transport = _Transport([b"private visible text"])
    store = BoundedSearchDocumentStore(SearchDocumentStoreLimits())
    service = SearchDocumentService(fetcher=_fetcher(transport), store=store)

    with pytest.raises(SearchDocumentAuthorizationError, match=r"\Adocument is not currently authorized\Z"):
        await service.fetch("https://example.test/article", scope=_scope(), authorization_current=authorization_current)
    assert len(transport.calls) == 1
    assert store._entries == {}  # type: ignore[attr-defined]


async def test_read_rechecks_after_store_read_and_before_result_return() -> None:
    service = SearchDocumentService(fetcher=_fetcher(_Transport([b"private visible text"])))
    fetched = await service.fetch("https://example.test/article", scope=_scope(), authorization_current=_allowed)
    states = iter((True, True, False))

    async def authorization_current() -> bool:
        return next(states)

    with pytest.raises(SearchDocumentAuthorizationError, match=r"\Adocument is not currently authorized\Z"):
        await service.read(
            fetched.reference, scope=_scope(request="later"), authorization_current=authorization_current
        )


async def test_read_and_find_fail_closed_on_store_identity_replacement_or_close() -> None:
    service = SearchDocumentService(fetcher=_fetcher(_Transport([b"alpha beta"])))
    fetched = await service.fetch("https://example.test/article", scope=_scope(), authorization_current=_allowed)
    service._store = BoundedSearchDocumentStore(SearchDocumentStoreLimits())  # type: ignore[attr-defined]

    with pytest.raises(SearchDocumentNotFoundError, match=r"\Adocument is unavailable\Z"):
        await service.read(fetched.reference, scope=_scope(request="read"), authorization_current=_allowed)

    service.close()
    with pytest.raises(SearchDocumentAuthorizationError, match=r"\Adocument is not currently authorized\Z"):
        await service.find(fetched.reference, "alpha", scope=_scope(), authorization_current=_allowed)


async def test_ttl_expiry_and_auth_callback_exception_do_not_leak_document() -> None:
    now = [100.0]
    limits = SearchDocumentStoreLimits(ttl_seconds=2.0)
    store = BoundedSearchDocumentStore(limits, clock=lambda: now[0])
    service = SearchDocumentService(fetcher=_fetcher(_Transport([b"alpha beta"])), limits=limits, store=store)
    fetched = await service.fetch("https://example.test/article", scope=_scope(), authorization_current=_allowed)
    now[0] = 103.0
    with pytest.raises(SearchDocumentNotFoundError, match=r"\Adocument is unavailable\Z"):
        await service.read(fetched.reference, scope=_scope(request="later"), authorization_current=_allowed)

    async def raising() -> bool:
        raise RuntimeError("must not leak")

    with pytest.raises(SearchDocumentAuthorizationError, match=r"\Adocument is not currently authorized\Z"):
        await service.fetch("https://example.test/again", scope=_scope(), authorization_current=raising)


async def test_runtime_exposes_same_identity_narrow_fetch_read_and_find_without_readiness_poisoning() -> None:
    service = SearchDocumentService(fetcher=_fetcher(_Transport([b"alpha beta alpha"])))
    runtime = LocalSearchFabricRuntime(
        orchestrator=object.__new__(SearchOrchestrator),
        health_probe=type("_Probe", (), {"probe": _allowed})(),
        document_service=service,
    )
    runtime._ready = True  # type: ignore[attr-defined]
    fetched = await runtime.fetch("https://example.test/article", scope=_scope(), authorization_current=_allowed)
    result = await runtime.find(
        fetched.reference, "alpha", scope=_scope(request="find"), authorization_current=_allowed
    )
    assert len(result.hits) == 2

    async def revoked() -> bool:
        return False

    with pytest.raises(SearchDocumentAuthorizationError):
        await runtime.read(fetched.reference, scope=_scope(request="read"), authorization_current=revoked)
    assert runtime.ready is True


async def test_fetch_keeps_external_cancellation_distinct() -> None:
    gate = asyncio.Event()

    class _BlockingTransport(_Transport):
        async def fetch(self, request: EvidenceHttpRequest) -> EvidenceHttpResponse:
            self.calls.append(request)
            await gate.wait()
            return await super().fetch(request)

    transport = _BlockingTransport([b"never"])
    service = SearchDocumentService(fetcher=_fetcher(transport))
    task = asyncio.create_task(
        service.fetch("https://example.test/article", scope=_scope(), authorization_current=_allowed)
    )
    while not transport.calls:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_pdf_content_type_is_typed_unsupported_without_exposing_target() -> None:
    transport = _Transport([b"%PDF-1.7"])

    async def pdf_response(request: EvidenceHttpRequest) -> EvidenceHttpResponse:
        transport.calls.append(request)
        return EvidenceHttpResponse(
            status=200,
            body=b"%PDF-1.7",
            peer_address=_PUBLIC_IP,
            content_type="application/pdf",
            content_length=8,
        )

    transport.fetch = pdf_response  # type: ignore[method-assign]
    service = SearchDocumentService(fetcher=_fetcher(transport))
    with pytest.raises(SearchDocumentUnsupportedError, match=r"\Adocument representation is unsupported\Z") as exc_info:
        await service.fetch("https://example.test/document.pdf", scope=_scope(), authorization_current=_allowed)
    assert "example.test" not in str(exc_info.value)
