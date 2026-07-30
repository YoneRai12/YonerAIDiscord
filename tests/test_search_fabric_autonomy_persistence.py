from __future__ import annotations

import asyncio
import sqlite3

import pytest

from yonerai_discord.db import Database
from yonerai_discord.modules.web_runtime.search import WebSearchSource
from yonerai_discord.provider_registry.domain import ArtifactKind, ArtifactRef
from yonerai_discord.search_fabric.autonomy import (
    AutonomyBinding,
    AutonomyCheckpoint,
    AutonomyCheckpointError,
    AutonomyPlanStatus,
    BoundedAutonomyService,
    BrowserResearchResult,
    OfficialPageInspection,
    SEARXNG_OFFICIAL_RESEARCH_V1,
    ScopedAutonomyArtifact,
    StepAttempt,
)
from yonerai_discord.search_fabric.autonomy_persistence import SqliteAutonomyJournal
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
STORE_DIGEST = "d" * 64


def _binding(*, task_id: str = "durable-task-1", channel_id: int = 20) -> AutonomyBinding:
    return AutonomyBinding(
        task_id=task_id,
        actor_ref="discord:user:30",
        actor_id=40,
        guild_id=10,
        channel_id=channel_id,
        user_id=30,
    )


def _search_outcome(task_id: str) -> SearchOrchestratorOutcome:
    evidence = SearchEvidenceV1(
        source=WebSearchSource(
            title="private title must not persist",
            url=OFFICIAL_URL,
            snippet="private snippet must not persist",
            source_id="official-settings",
        ),
        source_class=SearchSourceClass.PRIMARY_OFFICIAL,
        fetch_state=SearchFetchState.FETCHED,
        published=None,
        retrieved="2026-07-30T00:00:00Z",
        content_hash=f"sha256:{'a' * 64}",
        corroboration=SearchCorroborationState.NONE,
        verification_reasons=("official_domain_rule", "no_corroboration"),
        publisher="docs.searxng.org",
        freshness_state=SearchFreshnessState.UNKNOWN,
        corroboration_group="cg_official",
    )
    result = SearchResultV1(
        request_id=task_id,
        query_digest=query_digest(SEARXNG_OFFICIAL_RESEARCH_V1.query),
        intent=SearchIntent.OFFICIAL,
        language="en",
        evidence=(evidence,),
        backend_ids=("searxng.local",),
        engine_errors=(),
        candidate_count=1,
        cache_hits=0,
        latency_ms=4,
    )
    return SearchOrchestratorOutcome(
        result=result,
        receipt=SearchReceiptV1.from_result(result),
        verification_state=SearchVerificationState.PARTIAL,
        evidence_text="private evidence text must not persist",
    )


def _scoped(
    artifact: ArtifactRef,
    *,
    binding: AutonomyBinding,
) -> ScopedAutonomyArtifact:
    return ScopedAutonomyArtifact.bind(
        artifact,
        binding=binding,
        plan_digest=SEARXNG_OFFICIAL_RESEARCH_V1.digest,
    )


class _Search:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def search(
        self,
        query,
        *,
        request_id,
        intent,
        language,
        high_stakes,
        authorization_current,
    ):
        self.calls += 1
        if self.fail:
            raise RuntimeError("private exception text")
        return _search_outcome(request_id)


class _FailOnceSearch(_Search):
    async def search(
        self,
        query,
        *,
        request_id,
        intent,
        language,
        high_stakes,
        authorization_current,
    ):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("private first failure")
        return _search_outcome(request_id)


class _Browser:
    def __init__(self, *, cancel: bool = False) -> None:
        self.calls = 0
        self.cancel = cancel

    async def inspect(self, request, *, authorization_current):
        self.calls += 1
        if self.cancel:
            raise asyncio.CancelledError
        screenshot = ArtifactRef(
            "durable-screen",
            ArtifactKind.SCREENSHOT,
            "image/png",
            100,
            "b" * 64,
        )
        return BrowserResearchResult(
            (
                OfficialPageInspection(
                    OFFICIAL_URL,
                    "private page title",
                    "private browser text",
                    _scoped(screenshot, binding=request.binding),
                ),
            )
        )


class _Artifacts:
    def __init__(self) -> None:
        self.calls = 0

    async def create(self, request, *, authorization_current):
        self.calls += 1
        artifact = ArtifactRef(
            "durable-comparison",
            ArtifactKind.DOCUMENT,
            "text/markdown; charset=utf-8",
            200,
            "c" * 64,
        )
        return _scoped(artifact, binding=request.binding)


async def _allowed() -> bool:
    return True


def _journal(tmp_path, *, store_digest: str = STORE_DIGEST):
    database = Database(tmp_path / "control.sqlite3")
    database.open()
    assert database.migrate() == 5
    journal = SqliteAutonomyJournal(
        database,
        database_current=lambda: database,
        store_binding_digest=store_digest,
    )
    return database, journal


def _service(journal, *, search=None, browser=None, artifacts=None):
    search = search or _Search()
    browser = browser or _Browser()
    artifacts = artifacts or _Artifacts()
    service = BoundedAutonomyService(
        search=search,
        browser=browser,
        artifacts=artifacts,
        checkpoints=journal,
        terminal=journal,
    )
    return service, search, browser, artifacts


@pytest.mark.asyncio
async def test_partial_restart_recomputes_without_persisting_content(tmp_path) -> None:
    database, journal = _journal(tmp_path)
    first, first_search, _, _ = _service(journal, browser=_Browser(cancel=True))
    with pytest.raises(asyncio.CancelledError):
        await first.execute(_binding(), authorization_current=_allowed)
    assert first_search.calls == 1

    database.close()
    database.open()
    database.migrate()
    second, second_search, second_browser, second_artifacts = _service(journal)
    outcome = await second.execute(_binding(), authorization_current=_allowed)

    assert outcome.receipt.status is AutonomyPlanStatus.COMPLETED
    assert [item.attempts for item in outcome.receipt.steps] == [2, 2, 1]
    assert (second_search.calls, second_browser.calls, second_artifacts.calls) == (1, 1, 1)
    raw = database.path.read_bytes()
    for forbidden in (
        b"private title",
        b"private snippet",
        b"private evidence",
        b"private browser",
        OFFICIAL_URL.encode(),
        SEARXNG_OFFICIAL_RESEARCH_V1.query.encode(),
    ):
        assert forbidden not in raw
    database.close()


@pytest.mark.asyncio
async def test_partial_restart_does_not_reset_consumed_attempt_budget(tmp_path) -> None:
    database, journal = _journal(tmp_path)
    first, _, _, _ = _service(journal, browser=_Browser(cancel=True))
    with pytest.raises(asyncio.CancelledError):
        await first.execute(_binding(), authorization_current=_allowed)

    restarted_search = _Search(fail=True)
    restarted, _, browser, artifacts = _service(journal, search=restarted_search)
    outcome = await restarted.execute(_binding(), authorization_current=_allowed)

    assert outcome.receipt.status is AutonomyPlanStatus.FAILED
    assert outcome.receipt.steps[0].attempts == 3
    assert restarted_search.calls == 2
    assert (browser.calls, artifacts.calls) == (0, 0)
    assert b"private exception text" not in database.path.read_bytes()
    database.close()


@pytest.mark.asyncio
async def test_success_after_retry_preserves_total_without_charging_recompute_budget(tmp_path) -> None:
    database, journal = _journal(tmp_path)
    first_search = _FailOnceSearch()
    first, _, _, _ = _service(
        journal,
        search=first_search,
        browser=_Browser(cancel=True),
    )
    with pytest.raises(asyncio.CancelledError):
        await first.execute(_binding(), authorization_current=_allowed)
    assert first_search.calls == 2

    restarted, second_search, second_browser, second_artifacts = _service(journal)
    outcome = await restarted.execute(_binding(), authorization_current=_allowed)

    assert outcome.receipt.status is AutonomyPlanStatus.COMPLETED
    assert [item.attempts for item in outcome.receipt.steps] == [3, 2, 1]
    assert (second_search.calls, second_browser.calls, second_artifacts.calls) == (1, 1, 1)
    database.close()


@pytest.mark.asyncio
async def test_running_artifact_checkpoint_reauthorizes_without_regeneration(tmp_path) -> None:
    database, journal = _journal(tmp_path)
    artifacts = _Artifacts()

    async def cancel_after_artifact() -> bool:
        if artifacts.calls:
            raise asyncio.CancelledError
        return True

    first, search, browser, _ = _service(journal, artifacts=artifacts)
    with pytest.raises(asyncio.CancelledError):
        await first.execute(_binding(), authorization_current=cancel_after_artifact)
    assert (search.calls, browser.calls, artifacts.calls) == (1, 1, 1)
    with sqlite3.connect(database.path) as connection:
        row = connection.execute(
            """
            SELECT status, completed_prefix, artifact_id
            FROM autonomy_checkpoint_journal WHERE task_id = ?
            """,
            (_binding().task_id,),
        ).fetchone()
    assert row == ("running", 3, "durable-comparison")

    restarted, search2, browser2, artifacts2 = _service(journal)
    outcome = await restarted.execute(_binding(), authorization_current=_allowed)

    assert outcome.receipt.status is AutonomyPlanStatus.COMPLETED
    assert outcome.receipt.artifact_id == "durable-comparison"
    assert (search2.calls, browser2.calls, artifacts2.calls) == (0, 0, 0)
    database.close()


@pytest.mark.asyncio
async def test_active_sqlite_lease_rejects_duplicate_execution(tmp_path) -> None:
    database, first_journal = _journal(tmp_path)
    second_journal = SqliteAutonomyJournal(
        database,
        database_current=lambda: database,
        store_binding_digest=STORE_DIGEST,
    )
    started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingSearch(_Search):
        async def search(self, *args, **kwargs):
            self.calls += 1
            started.set()
            await release.wait()
            return _search_outcome(kwargs["request_id"])

    blocking = _BlockingSearch()
    first, _, _, _ = _service(first_journal, search=blocking)
    second, second_search, second_browser, second_artifacts = _service(second_journal)
    first_task = asyncio.create_task(first.execute(_binding(), authorization_current=_allowed))
    await started.wait()
    with pytest.raises(AutonomyCheckpointError, match="already leased"):
        await second.execute(_binding(), authorization_current=_allowed)
    assert (second_search.calls, second_browser.calls, second_artifacts.calls) == (0, 0, 0)
    release.set()
    assert (await first_task).receipt.status is AutonomyPlanStatus.COMPLETED
    assert blocking.calls == 1
    database.close()


@pytest.mark.asyncio
async def test_terminal_projection_rejects_artifact_replacement(tmp_path) -> None:
    database, journal = _journal(tmp_path)
    service, _, _, _ = _service(journal)
    outcome = await service.execute(_binding(), authorization_current=_allowed)
    assert outcome.artifact is not None

    assert await journal.acquire_execution(
        _binding(),
        SEARXNG_OFFICIAL_RESEARCH_V1,
        lease_seconds=10,
    )
    replacement = _scoped(
        ArtifactRef(
            "replacement-artifact",
            ArtifactKind.DOCUMENT,
            "text/markdown; charset=utf-8",
            200,
            "e" * 64,
        ),
        binding=_binding(),
    )
    checkpoint = AutonomyCheckpoint(
        template_id=SEARXNG_OFFICIAL_RESEARCH_V1.template_id,
        template_version=SEARXNG_OFFICIAL_RESEARCH_V1.version,
        plan_digest=SEARXNG_OFFICIAL_RESEARCH_V1.digest,
        binding_digest=_binding().digest,
        status=AutonomyPlanStatus.COMPLETED,
        completed_steps=SEARXNG_OFFICIAL_RESEARCH_V1.steps,
        attempts=tuple(StepAttempt(item.step, item.attempts, item.failure_code) for item in outcome.receipt.steps),
        artifact=replacement,
        terminal_emitted=True,
        content_compacted=True,
    )
    with pytest.raises(AutonomyCheckpointError, match="projection is immutable"):
        await journal.save(_binding().task_id, checkpoint)
    await journal.release_execution()
    database.close()


@pytest.mark.asyncio
async def test_completed_terminal_replays_without_duplicate_execution(tmp_path) -> None:
    database, journal = _journal(tmp_path)
    first, search, browser, artifacts = _service(journal)
    completed = await first.execute(_binding(), authorization_current=_allowed)
    with sqlite3.connect(database.path) as connection:
        first_revision = connection.execute(
            "SELECT revision FROM autonomy_checkpoint_journal WHERE task_id = ?",
            (_binding().task_id,),
        ).fetchone()[0]

    second, search2, browser2, artifacts2 = _service(journal)
    replay = await second.execute(_binding(), authorization_current=_allowed)
    with sqlite3.connect(database.path) as connection:
        replay_revision = connection.execute(
            "SELECT revision FROM autonomy_checkpoint_journal WHERE task_id = ?",
            (_binding().task_id,),
        ).fetchone()[0]

    assert replay == completed
    assert (search.calls, browser.calls, artifacts.calls) == (1, 1, 1)
    assert (search2.calls, browser2.calls, artifacts2.calls) == (0, 0, 0)
    assert replay_revision == first_revision
    database.close()


@pytest.mark.asyncio
async def test_cross_scope_and_store_binding_changes_fail_closed(tmp_path) -> None:
    database, journal = _journal(tmp_path)
    service, _, _, _ = _service(journal)
    await service.execute(_binding(), authorization_current=_allowed)

    other_scope, search, browser, artifacts = _service(journal)
    with pytest.raises(AutonomyCheckpointError):
        await other_scope.execute(
            _binding(channel_id=999),
            authorization_current=_allowed,
        )
    assert (search.calls, browser.calls, artifacts.calls) == (0, 0, 0)

    changed_store = SqliteAutonomyJournal(
        database,
        database_current=lambda: database,
        store_binding_digest="e" * 64,
    )
    changed_service, search2, browser2, artifacts2 = _service(changed_store)
    with pytest.raises(AutonomyCheckpointError):
        await changed_service.execute(_binding(), authorization_current=_allowed)
    assert (search2.calls, browser2.calls, artifacts2.calls) == (0, 0, 0)
    database.close()


@pytest.mark.asyncio
async def test_digest_tampering_and_database_identity_swap_fail_closed(tmp_path) -> None:
    database, journal = _journal(tmp_path)
    service, _, _, _ = _service(journal)
    await service.execute(_binding(), authorization_current=_allowed)
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            "UPDATE autonomy_checkpoint_journal SET plan_digest = ? WHERE task_id = ?",
            ("f" * 64, _binding().task_id),
        )

    tampered, search, browser, artifacts = _service(journal)
    with pytest.raises(AutonomyCheckpointError):
        await tampered.execute(_binding(), authorization_current=_allowed)
    assert (search.calls, browser.calls, artifacts.calls) == (0, 0, 0)

    replacement = Database(tmp_path / "other.sqlite3")
    swapped = SqliteAutonomyJournal(
        database,
        database_current=lambda: replacement,
        store_binding_digest=STORE_DIGEST,
    )
    with pytest.raises(AutonomyCheckpointError):
        await swapped.load(_binding().task_id)
    database.close()
