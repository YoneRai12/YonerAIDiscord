from __future__ import annotations

import hashlib

import pytest
from PIL import Image

from yonerai_discord.browser_sandbox.models import (
    BrowserOutput,
    BrowserOutputKind,
    BrowserSessionResult,
)
from yonerai_discord.browser_sandbox.policy import BrowserSandboxPolicy, StaticDnsResolver
from yonerai_discord.browser_sandbox.service import BrowserSandboxService
from yonerai_discord.db import Database
from yonerai_discord.modules.media_pipeline.artifacts import MediaArtifactStore, canonicalize_image
from yonerai_discord.modules.media_pipeline.domain import ArtifactScope as MediaArtifactScope
from yonerai_discord.modules.web_runtime.search import WebSearchSource
from yonerai_discord.provider_registry.domain import ArtifactKind, ArtifactRef
from yonerai_discord.search_fabric.autonomy import (
    AutonomyAuthorizationError,
    AutonomyBinding,
    AutonomyCheckpointError,
    AutonomyContractError,
    AutonomyPlanStatus,
    BrowserResearchRequest,
    BrowserResearchResult,
    ComparisonTableRequest,
    OfficialPageInspection,
    SEARXNG_OFFICIAL_RESEARCH_V1,
    ScopedAutonomyArtifact,
)
from yonerai_discord.search_fabric.autonomy_runtime import (
    IsolatedBrowserAutonomyAdapter,
    LocalSearchAutonomyAdapter,
    MarkdownComparisonArtifactAdapter,
    MediaPipelineAutonomyArtifactWriter,
    compose_bounded_autonomy_runtime,
)
from yonerai_discord.search_fabric.composition import LocalSearchFabricRuntime
from yonerai_discord.search_fabric.contracts import (
    SearchCorroborationState,
    SearchEvidenceV1,
    SearchFetchState,
    SearchFreshnessState,
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


OFFICIAL_URL = "https://docs.searxng.org/admin/settings/settings.html"


def _binding() -> AutonomyBinding:
    return AutonomyBinding(
        task_id="autonomy-runtime-1",
        actor_ref="discord:user:30",
        actor_id=30,
        guild_id=10,
        channel_id=20,
        user_id=30,
    )


def _evidence(
    *,
    source_id: str,
    url: str,
    source_class: SearchSourceClass,
    fetched: bool = True,
) -> SearchEvidenceV1:
    return SearchEvidenceV1(
        source=WebSearchSource(
            title=f"title {source_id}",
            url=url,
            snippet=f"snippet {source_id}",
            source_id=source_id,
        ),
        source_class=source_class,
        fetch_state=SearchFetchState.FETCHED if fetched else SearchFetchState.METADATA_ONLY,
        published=None,
        retrieved="2026-07-30T00:00:00Z",
        content_hash=f"sha256:{'a' * 64}" if fetched else None,
        corroboration=SearchCorroborationState.NONE if fetched else SearchCorroborationState.UNKNOWN,
        verification_reasons=("official_domain_rule", "no_corroboration")
        if fetched
        else ("fetch_not_performed", "corroboration_unknown"),
        publisher="docs.searxng.org" if source_class is SearchSourceClass.PRIMARY_OFFICIAL else "example.org",
        freshness_state=SearchFreshnessState.UNKNOWN,
        corroboration_group="cg_official" if fetched else None,
    )


def _outcome(*evidence: SearchEvidenceV1) -> SearchOrchestratorOutcome:
    result = SearchResultV1(
        request_id="autonomy-runtime-1",
        query_digest=query_digest(SEARXNG_OFFICIAL_RESEARCH_V1.query),
        intent=SearchIntent.OFFICIAL,
        language="en",
        evidence=tuple(evidence),
        backend_ids=("searxng.local",),
        engine_errors=(),
        candidate_count=len(evidence),
        cache_hits=0,
        latency_ms=5,
    )
    return SearchOrchestratorOutcome(
        result=result,
        receipt=SearchReceiptV1.from_result(result),
        verification_state=SearchVerificationState.PARTIAL,
        evidence_text="unfiltered text must not be reused",
    )


class _Runtime(LocalSearchFabricRuntime):
    def __init__(self, outcome: SearchOrchestratorOutcome, *, ready: bool = True) -> None:
        self._ready = ready
        self.outcome = outcome
        self.calls = 0

    async def search(self, *args, **kwargs):
        del args
        self.calls += 1
        assert kwargs["request_id"] == "autonomy-runtime-1"
        return self.outcome


class _BrowserWorker:
    def __init__(self, *, outputs: tuple[BrowserOutput, ...] | None = None) -> None:
        self.calls = 0
        self.outputs = outputs

    async def execute(self, request, *, network_guard, isolation_contract):
        del network_guard
        assert isolation_contract.profile_mode == "ephemeral"
        self.calls += 1
        outputs = self.outputs
        if outputs is None:
            outputs = (
                BrowserOutput(
                    1,
                    BrowserOutputKind.TEXT,
                    b"SearXNG settings documentation.",
                    "text/plain; charset=utf-8",
                ),
                BrowserOutput(2, BrowserOutputKind.SCREENSHOT, _png(), "image/png"),
            )
        return BrowserSessionResult(outputs)


class _Writer:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, ArtifactKind, str]] = []

    async def write(self, data, *, binding, plan_digest, kind, media_type):
        assert binding == _binding()
        assert plan_digest == SEARXNG_OFFICIAL_RESEARCH_V1.digest
        self.calls.append((data, kind, media_type))
        return ArtifactRef(
            artifact_id=f"artifact-{len(self.calls)}",
            kind=kind,
            media_type=media_type,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )


def _browser_service(worker: _BrowserWorker | None = None) -> BrowserSandboxService:
    return BrowserSandboxService(
        policy=BrowserSandboxPolicy(
            resolver=StaticDnsResolver({"docs.searxng.org": ("104.21.20.212",)}),
            allowed_domains=("docs.searxng.org",),
        ),
        adapter=worker,
    )


@pytest.mark.asyncio
async def test_composition_uses_only_current_existing_runtime_identities(tmp_path) -> None:
    runtime = _Runtime(
        _outcome(
            _evidence(
                source_id="official",
                url=OFFICIAL_URL,
                source_class=SearchSourceClass.PRIMARY_OFFICIAL,
            )
        )
    )
    browser = _browser_service(_BrowserWorker())
    root = tmp_path / "artifacts"
    root.mkdir()
    store = MediaArtifactStore(root)
    database = Database(tmp_path / "control.sqlite3")
    database.open()
    database.migrate()
    current: dict[str, object] = {
        "search": runtime,
        "browser": browser,
        "store": store,
        "database": database,
    }
    try:
        composed = compose_bounded_autonomy_runtime(
            search_runtime=runtime,
            browser_service=browser,
            media_store=store,
            database=database,
            search_runtime_current=lambda: current["search"],
            browser_service_current=lambda: current["browser"],
            media_store_current=lambda: current["store"],
            database_current=lambda: current["database"],
            closing_current=lambda: False,
        )
        assert composed is not None and composed.ready

        outcome = await composed.service.execute(
            _binding(),
            authorization_current=_allowed,
        )
        assert outcome.receipt.status is AutonomyPlanStatus.COMPLETED
        assert outcome.artifact is not None
        resolved = await composed.artifacts.resolve(
            outcome.artifact,
            binding=_binding(),
            plan_digest=SEARXNG_OFFICIAL_RESEARCH_V1.digest,
        )
        assert resolved.kind.value == "document"

        current["browser"] = object()
        assert composed.ready is False
    finally:
        database.close()
        store.close()


def test_composition_is_unavailable_without_a_trusted_browser_worker(tmp_path) -> None:
    runtime = _Runtime(
        _outcome(
            _evidence(
                source_id="official",
                url=OFFICIAL_URL,
                source_class=SearchSourceClass.PRIMARY_OFFICIAL,
            )
        )
    )
    browser = _browser_service()
    root = tmp_path / "artifacts"
    root.mkdir()
    store = MediaArtifactStore(root)
    database = Database(tmp_path / "control.sqlite3")
    database.open()
    database.migrate()
    try:
        assert (
            compose_bounded_autonomy_runtime(
                search_runtime=runtime,
                browser_service=browser,
                media_store=store,
                database=database,
                search_runtime_current=lambda: runtime,
                browser_service_current=lambda: browser,
                media_store_current=lambda: store,
                database_current=lambda: database,
                closing_current=lambda: False,
            )
            is None
        )
    finally:
        database.close()
        store.close()


def _png() -> bytes:
    with Image.new("RGB", (2, 2), (20, 40, 60)) as image:
        return canonicalize_image(image).data


async def _allowed() -> bool:
    return True


@pytest.mark.asyncio
async def test_search_adapter_projects_only_fetched_official_evidence() -> None:
    runtime = _Runtime(
        _outcome(
            _evidence(
                source_id="official",
                url=OFFICIAL_URL,
                source_class=SearchSourceClass.PRIMARY_OFFICIAL,
            ),
            _evidence(
                source_id="community",
                url="https://example.org/community",
                source_class=SearchSourceClass.COMMUNITY,
            ),
        )
    )
    adapter = LocalSearchAutonomyAdapter(runtime, lambda: runtime, lambda: False)

    outcome = await adapter.search(
        SEARXNG_OFFICIAL_RESEARCH_V1.query,
        request_id="autonomy-runtime-1",
        intent=SearchIntent.OFFICIAL,
        language="en",
        high_stakes=False,
        authorization_current=_allowed,
    )

    assert adapter.ready is True
    assert runtime.calls == 1
    assert tuple(item.source.source_id for item in outcome.result.evidence) == ("official",)
    assert "community" not in outcome.evidence_text


@pytest.mark.asyncio
async def test_search_adapter_unready_and_identity_replacement_execute_nothing() -> None:
    runtime = _Runtime(
        _outcome(
            _evidence(
                source_id="official",
                url=OFFICIAL_URL,
                source_class=SearchSourceClass.PRIMARY_OFFICIAL,
            )
        ),
        ready=False,
    )
    current: object = runtime
    adapter = LocalSearchAutonomyAdapter(runtime, lambda: current, lambda: False)
    with pytest.raises(AutonomyContractError):
        await adapter.search(
            SEARXNG_OFFICIAL_RESEARCH_V1.query,
            request_id="autonomy-runtime-1",
            intent=SearchIntent.OFFICIAL,
            language="en",
            high_stakes=False,
            authorization_current=_allowed,
        )
    current = object()
    with pytest.raises(AutonomyAuthorizationError):
        await adapter.search(
            SEARXNG_OFFICIAL_RESEARCH_V1.query,
            request_id="autonomy-runtime-1",
            intent=SearchIntent.OFFICIAL,
            language="en",
            high_stakes=False,
            authorization_current=_allowed,
        )
    assert runtime.calls == 0


@pytest.mark.asyncio
async def test_browser_adapter_persists_canonical_screenshot_and_binds_scope() -> None:
    worker = _BrowserWorker()
    service = _browser_service(worker)
    writer = _Writer()
    adapter = IsolatedBrowserAutonomyAdapter(
        service,
        writer,
        lambda: service,
        lambda: writer,
        lambda: False,
    )

    result = await adapter.inspect(
        BrowserResearchRequest(_binding(), (OFFICIAL_URL,)),
        authorization_current=_allowed,
    )

    assert adapter.ready is True
    assert worker.calls == 1
    assert writer.calls[0][1:] == (ArtifactKind.SCREENSHOT, "image/png")
    assert result.pages[0].screenshot.task_id == _binding().task_id
    assert result.pages[0].screenshot.artifact.sha256 == hashlib.sha256(writer.calls[0][0]).hexdigest()


@pytest.mark.asyncio
async def test_browser_adapter_rejects_unconfigured_or_bad_outputs_before_write() -> None:
    unconfigured = _browser_service()
    writer = _Writer()
    adapter = IsolatedBrowserAutonomyAdapter(
        unconfigured,
        writer,
        lambda: unconfigured,
        lambda: writer,
        lambda: False,
    )
    assert adapter.ready is False
    with pytest.raises(AutonomyContractError):
        await adapter.inspect(
            BrowserResearchRequest(_binding(), (OFFICIAL_URL,)),
            authorization_current=_allowed,
        )

    bad_worker = _BrowserWorker(
        outputs=(BrowserOutput(1, BrowserOutputKind.TEXT, b"text", "text/plain; charset=utf-8"),)
    )
    bad_service = _browser_service(bad_worker)
    bad_adapter = IsolatedBrowserAutonomyAdapter(
        bad_service,
        writer,
        lambda: bad_service,
        lambda: writer,
        lambda: False,
    )
    with pytest.raises(AutonomyContractError):
        await bad_adapter.inspect(
            BrowserResearchRequest(_binding(), (OFFICIAL_URL,)),
            authorization_current=_allowed,
        )
    assert writer.calls == []


@pytest.mark.asyncio
async def test_browser_adapter_revocation_after_worker_prevents_artifact_write() -> None:
    worker = _BrowserWorker()
    service = _browser_service(worker)
    writer = _Writer()
    checks = iter((True, True, False))

    async def current() -> bool:
        return next(checks)

    adapter = IsolatedBrowserAutonomyAdapter(
        service,
        writer,
        lambda: service,
        lambda: writer,
        lambda: False,
    )
    with pytest.raises(AutonomyAuthorizationError):
        await adapter.inspect(
            BrowserResearchRequest(_binding(), (OFFICIAL_URL,)),
            authorization_current=current,
        )
    assert worker.calls == 1
    assert writer.calls == []


@pytest.mark.asyncio
async def test_comparison_adapter_creates_bounded_markdown_without_internal_reference() -> None:
    worker = _BrowserWorker()
    service = _browser_service(worker)
    writer = _Writer()
    browser = IsolatedBrowserAutonomyAdapter(
        service,
        writer,
        lambda: service,
        lambda: writer,
        lambda: False,
    )
    browser_result = await browser.inspect(
        BrowserResearchRequest(_binding(), (OFFICIAL_URL,)),
        authorization_current=_allowed,
    )
    search = _outcome(
        _evidence(
            source_id="official",
            url=OFFICIAL_URL,
            source_class=SearchSourceClass.PRIMARY_OFFICIAL,
        )
    )
    comparison = MarkdownComparisonArtifactAdapter(writer, lambda: writer, lambda: False)

    result = await comparison.create(
        ComparisonTableRequest(_binding(), search, browser_result),
        authorization_current=_allowed,
    )

    markdown = writer.calls[-1][0].decode("utf-8")
    assert writer.calls[-1][1:] == (ArtifactKind.DOCUMENT, "text/markdown; charset=utf-8")
    assert "| official_page | topic | documented_behavior | evidence |" in markdown
    assert "artifact-1" not in markdown
    assert result.artifact.kind is ArtifactKind.DOCUMENT


@pytest.mark.asyncio
async def test_comparison_adapter_escapes_untrusted_markdown_and_html() -> None:
    writer = _Writer()
    browser_result = BrowserResearchResult(
        (
            OfficialPageInspection(
                url=OFFICIAL_URL,
                title="official",
                text="<img src=x> [unsafe](https://example.invalid/) `code`",
                screenshot=ScopedAutonomyArtifact.bind(
                    ArtifactRef(
                        artifact_id="artifact-1",
                        kind=ArtifactKind.SCREENSHOT,
                        media_type="image/png",
                        size_bytes=len(_png()),
                        sha256=hashlib.sha256(_png()).hexdigest(),
                    ),
                    binding=_binding(),
                    plan_digest=SEARXNG_OFFICIAL_RESEARCH_V1.digest,
                ),
            ),
        )
    )
    comparison = MarkdownComparisonArtifactAdapter(writer, lambda: writer, lambda: False)

    await comparison.create(
        ComparisonTableRequest(
            _binding(),
            _outcome(
                _evidence(
                    source_id="official",
                    url=OFFICIAL_URL,
                    source_class=SearchSourceClass.PRIMARY_OFFICIAL,
                )
            ),
            browser_result,
        ),
        authorization_current=_allowed,
    )

    markdown = writer.calls[-1][0].decode("utf-8")
    assert "<img" not in markdown
    assert "[unsafe](" not in markdown
    assert "`code`" not in markdown
    assert "&lt;img src=x&gt;" in markdown


@pytest.mark.asyncio
async def test_media_pipeline_writer_persists_png_and_markdown_with_exact_refs(tmp_path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    store = MediaArtifactStore(root)
    writer = MediaPipelineAutonomyArtifactWriter(store, lambda: store, lambda: False)
    try:
        screenshot = await writer.write(
            _png(),
            binding=_binding(),
            plan_digest=SEARXNG_OFFICIAL_RESEARCH_V1.digest,
            kind=ArtifactKind.SCREENSHOT,
            media_type="image/png",
        )
        markdown_data = b"# comparison\n"
        document = await writer.write(
            markdown_data,
            binding=_binding(),
            plan_digest=SEARXNG_OFFICIAL_RESEARCH_V1.digest,
            kind=ArtifactKind.DOCUMENT,
            media_type="text/markdown; charset=utf-8",
        )
        resolved = await writer.resolve(
            ScopedAutonomyArtifact.bind(
                document,
                binding=_binding(),
                plan_digest=SEARXNG_OFFICIAL_RESEARCH_V1.digest,
            ),
            binding=_binding(),
            plan_digest=SEARXNG_OFFICIAL_RESEARCH_V1.digest,
        )
    finally:
        store.close()

    assert screenshot.kind is ArtifactKind.SCREENSHOT
    assert screenshot.size_bytes and screenshot.sha256
    assert document.size_bytes == len(markdown_data)
    assert document.sha256 == hashlib.sha256(markdown_data).hexdigest()
    assert resolved.artifact_id == document.artifact_id
    assert resolved.kind.value == "document"
    assert (
        resolved.scope_digest
        == MediaArtifactScope(
            request_id=_binding().task_id,
            guild_id=_binding().guild_id,
            channel_id=_binding().channel_id,
            user_id=_binding().user_id,
        ).digest
    )
    assert "artifacts" not in repr(writer)


@pytest.mark.asyncio
async def test_writer_and_comparison_fail_closed_on_identity_or_authorization_change(tmp_path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    store = MediaArtifactStore(root)
    current: object = store
    writer = MediaPipelineAutonomyArtifactWriter(store, lambda: current, lambda: False)
    current = object()
    try:
        with pytest.raises(AutonomyAuthorizationError):
            await writer.write(
                b"# comparison\n",
                binding=_binding(),
                plan_digest=SEARXNG_OFFICIAL_RESEARCH_V1.digest,
                kind=ArtifactKind.DOCUMENT,
                media_type="text/markdown; charset=utf-8",
            )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_media_pipeline_writer_resolver_rejects_cross_scope_and_digest_tampering(tmp_path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    store = MediaArtifactStore(root)
    writer = MediaPipelineAutonomyArtifactWriter(store, lambda: store, lambda: False)
    try:
        document = await writer.write(
            b"# comparison\n",
            binding=_binding(),
            plan_digest=SEARXNG_OFFICIAL_RESEARCH_V1.digest,
            kind=ArtifactKind.DOCUMENT,
            media_type="text/markdown; charset=utf-8",
        )
        scoped = ScopedAutonomyArtifact.bind(
            document,
            binding=_binding(),
            plan_digest=SEARXNG_OFFICIAL_RESEARCH_V1.digest,
        )
        other = AutonomyBinding(
            task_id=_binding().task_id,
            actor_ref="discord:user:31",
            actor_id=31,
            guild_id=_binding().guild_id,
            channel_id=_binding().channel_id,
            user_id=31,
        )
        with pytest.raises(AutonomyCheckpointError, match="bound"):
            await writer.resolve(
                scoped,
                binding=other,
                plan_digest=SEARXNG_OFFICIAL_RESEARCH_V1.digest,
            )
        tampered = ScopedAutonomyArtifact.bind(
            ArtifactRef(
                artifact_id=document.artifact_id,
                kind=document.kind,
                media_type=document.media_type,
                size_bytes=document.size_bytes,
                sha256="b" * 64,
            ),
            binding=_binding(),
            plan_digest=SEARXNG_OFFICIAL_RESEARCH_V1.digest,
        )
        with pytest.raises(AutonomyContractError, match="resolution failed"):
            await writer.resolve(
                tampered,
                binding=_binding(),
                plan_digest=SEARXNG_OFFICIAL_RESEARCH_V1.digest,
            )
    finally:
        store.close()
