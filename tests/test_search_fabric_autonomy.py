from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from yonerai_discord.browser_sandbox.models import ExtractText, Navigate, Screenshot
from yonerai_discord.modules.web_runtime.search import WebSearchSource
from yonerai_discord.provider_registry.domain import ArtifactKind, ArtifactRef
from yonerai_discord.search_fabric.autonomy import (
    AutonomyAuthorizationError,
    AutonomyBinding,
    AutonomyCheckpoint,
    AutonomyCheckpointError,
    AutonomyPlanStatus,
    AutonomyPolicy,
    AutonomyStep,
    AutonomyTaskTemplate,
    AutonomyTerminalKind,
    BoundedAutonomyService,
    BrowserResearchResult,
    OfficialPageInspection,
    SEARXNG_OFFICIAL_RESEARCH_V1,
    ScopedAutonomyArtifact,
)
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


def _binding(*, task_id: str = "m12-task-1", channel_id: int = 20) -> AutonomyBinding:
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
            title="SearXNG settings",
            url=OFFICIAL_URL,
            snippet="Official configuration reference",
            source_id="official-settings",
        ),
        source_class=SearchSourceClass.PRIMARY_OFFICIAL,
        fetch_state=SearchFetchState.FETCHED,
        published=None,
        retrieved="2026-07-29T00:00:00Z",
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
        evidence_text="content used only inside the typed pipeline",
    )


def _screenshot() -> ArtifactRef:
    return ArtifactRef(
        "screen-1",
        ArtifactKind.SCREENSHOT,
        "image/png",
        100,
        "b" * 64,
    )


def _comparison() -> ArtifactRef:
    return ArtifactRef(
        "comparison-1",
        ArtifactKind.DOCUMENT,
        "text/markdown",
        200,
        "c" * 64,
    )


def _scoped(
    artifact: ArtifactRef,
    *,
    binding: AutonomyBinding | None = None,
    plan_digest: str = SEARXNG_OFFICIAL_RESEARCH_V1.digest,
) -> ScopedAutonomyArtifact:
    return ScopedAutonomyArtifact.bind(
        artifact,
        binding=binding or _binding(),
        plan_digest=plan_digest,
    )


class _Search:
    def __init__(self, *, failures: int = 0, delay: float = 0.0) -> None:
        self.failures = failures
        self.delay = delay
        self.calls = 0

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
        assert query == SEARXNG_OFFICIAL_RESEARCH_V1.query
        assert (intent, language, high_stakes) == (SearchIntent.OFFICIAL, "en", False)
        assert callable(authorization_current)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.calls <= self.failures:
            raise RuntimeError("content must not enter the receipt")
        return _search_outcome(request_id)


class _Browser:
    def __init__(self, *, cancel_once: bool = False, failures: int = 0) -> None:
        self.cancel_once = cancel_once
        self.failures = failures
        self.calls = 0

    async def inspect(self, request, *, authorization_current):
        self.calls += 1
        assert request.binding == _binding(task_id=request.binding.task_id)
        assert request.urls == (OFFICIAL_URL,)
        assert len(request.sessions) == 1
        assert [type(action) for action in request.sessions[0].actions] == [
            Navigate,
            ExtractText,
            Screenshot,
        ]
        assert callable(authorization_current)
        if self.cancel_once and self.calls == 1:
            raise asyncio.CancelledError
        if self.calls <= self.failures:
            raise ConnectionError("browser unavailable")
        return BrowserResearchResult(
            (
                OfficialPageInspection(
                    OFFICIAL_URL,
                    "Settings",
                    "Typed inspection text.",
                    _scoped(_screenshot(), binding=request.binding),
                ),
            )
        )


class _Artifacts:
    def __init__(self, artifact: object | None = None) -> None:
        self.artifact = artifact
        self.calls = 0

    async def create(self, request, *, authorization_current):
        self.calls += 1
        assert request.binding.task_id == request.search.result.request_id
        assert request.browser.pages[0].screenshot.artifact.kind is ArtifactKind.SCREENSHOT
        assert request.columns == (
            "official_page",
            "topic",
            "documented_behavior",
            "evidence",
        )
        assert callable(authorization_current)
        if self.artifact is not None:
            return self.artifact
        return _scoped(_comparison(), binding=request.binding)


class _Checkpoints:
    def __init__(self, initial: AutonomyCheckpoint | None = None) -> None:
        self.value = initial
        self.saved: list[AutonomyCheckpoint] = []

    async def load(self, task_id):
        return self.value

    async def save(self, task_id, checkpoint):
        assert task_id == _binding(task_id=task_id).task_id
        self.value = checkpoint
        self.saved.append(checkpoint)


class _CommittedThenSlowCheckpoints(_Checkpoints):
    async def save(self, task_id, checkpoint):
        self.value = checkpoint
        self.saved.append(checkpoint)
        if checkpoint.terminal_emitted:
            await asyncio.sleep(0.1)


class _Terminal:
    def __init__(self) -> None:
        self.notices = []
        self.keys = set()

    async def publish_once(self, notice):
        if notice.idempotency_key not in self.keys:
            self.keys.add(notice.idempotency_key)
            self.notices.append(notice)


class _DeadlineTerminal(_Terminal):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def publish_once(self, notice):
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(0.1)
            return
        await super().publish_once(notice)


class _CommittedThenSlowTerminal(_Terminal):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def publish_once(self, notice):
        self.calls += 1
        await super().publish_once(notice)
        if self.calls == 1:
            await asyncio.sleep(0.1)


class _SlowCompletedStatusCheckpoints(_Checkpoints):
    async def save(self, task_id, checkpoint):
        self.value = checkpoint
        self.saved.append(checkpoint)
        if checkpoint.status is AutonomyPlanStatus.COMPLETED and checkpoint.terminal_emitted is False:
            await asyncio.sleep(0.1)


class _Authorization:
    def __init__(self, *, deny_at: int | None = None) -> None:
        self.calls = 0
        self.deny_at = deny_at

    async def __call__(self) -> bool:
        self.calls += 1
        return self.deny_at is None or self.calls < self.deny_at


class _MutableAuthorization:
    def __init__(self) -> None:
        self.allowed = True

    async def __call__(self) -> bool:
        return self.allowed


class _RevokingArtifacts(_Artifacts):
    def __init__(self, authorization: _MutableAuthorization, *, cancel: bool = False) -> None:
        super().__init__()
        self.authorization = authorization
        self.cancel = cancel

    async def create(self, request, *, authorization_current):
        result = await super().create(request, authorization_current=authorization_current)
        if self.cancel:
            task = asyncio.current_task()
            assert task is not None
            asyncio.get_running_loop().call_soon(task.cancel)
        else:
            self.authorization.allowed = False
        return result


def _service(
    *,
    search: _Search | None = None,
    browser: _Browser | None = None,
    artifacts: _Artifacts | None = None,
    checkpoints: _Checkpoints | None = None,
    terminal: _Terminal | None = None,
    policy: AutonomyPolicy = AutonomyPolicy(),
) -> tuple[BoundedAutonomyService, _Search, _Browser, _Artifacts, _Checkpoints, _Terminal]:
    search = search or _Search()
    browser = browser or _Browser()
    artifacts = artifacts or _Artifacts()
    checkpoints = checkpoints or _Checkpoints()
    terminal = terminal or _Terminal()
    return (
        BoundedAutonomyService(
            search=search,
            browser=browser,
            artifacts=artifacts,
            checkpoints=checkpoints,
            terminal=terminal,
            policy=policy,
        ),
        search,
        browser,
        artifacts,
        checkpoints,
        terminal,
    )


@pytest.mark.asyncio
async def test_code_owned_template_runs_typed_pipeline_and_emits_content_free_final_once() -> None:
    service, search, browser, artifacts, checkpoints, terminal = _service()
    authorization = _Authorization()

    outcome = await service.execute(_binding(), authorization_current=authorization)

    assert outcome.artifact is not None
    assert outcome.artifact.artifact == _comparison()
    assert outcome.receipt.status is AutonomyPlanStatus.COMPLETED
    assert [step.completed for step in outcome.receipt.steps] == [True, True, True]
    assert [step.attempts for step in outcome.receipt.steps] == [1, 1, 1]
    assert (search.calls, browser.calls, artifacts.calls) == (1, 1, 1)
    assert checkpoints.value is not None and checkpoints.value.terminal_emitted is True
    assert len(terminal.notices) == 1
    assert terminal.notices[0].kind is AutonomyTerminalKind.FINAL

    audit_json = json.dumps(outcome.receipt.to_audit_projection(), sort_keys=True)
    assert SEARXNG_OFFICIAL_RESEARCH_V1.query not in audit_json
    assert OFFICIAL_URL not in audit_json
    assert "Typed inspection text" not in audit_json
    assert "discord:user:30" not in audit_json
    assert authorization.calls >= 2 * (3 + 3)

    replay = await service.execute(_binding(), authorization_current=authorization)
    assert replay.receipt == outcome.receipt
    assert len(terminal.notices) == 1
    assert (search.calls, browser.calls, artifacts.calls) == (1, 1, 1)


@pytest.mark.asyncio
async def test_one_port_failure_is_retried_once_then_pipeline_completes() -> None:
    service, search, _, _, checkpoints, terminal = _service(search=_Search(failures=1))

    outcome = await service.execute(_binding(), authorization_current=_Authorization())

    assert outcome.receipt.status is AutonomyPlanStatus.COMPLETED
    assert outcome.receipt.steps[0].attempts == 2
    assert outcome.receipt.steps[0].failure_code is None
    assert search.calls == 2
    assert any(item.attempts[0].last_failure_code == "runtimeerror" for item in checkpoints.saved if item.attempts)
    assert [notice.kind for notice in terminal.notices] == [AutonomyTerminalKind.FINAL]


@pytest.mark.asyncio
async def test_second_failure_stops_downstream_and_emits_one_content_free_error() -> None:
    service, search, browser, artifacts, _, terminal = _service(search=_Search(failures=2))

    outcome = await service.execute(_binding(), authorization_current=_Authorization())

    assert outcome.receipt.status is AutonomyPlanStatus.FAILED
    assert outcome.receipt.failure_code == "runtimeerror"
    assert outcome.receipt.steps[0].attempts == 2
    assert outcome.receipt.steps[0].failure_code == "runtimeerror"
    assert search.calls == 2
    assert (browser.calls, artifacts.calls) == (0, 0)
    assert [notice.kind for notice in terminal.notices] == [AutonomyTerminalKind.ERROR]
    error_json = json.dumps(terminal.notices[0].receipt.to_audit_projection())
    assert "content must not enter the receipt" not in error_json


@pytest.mark.asyncio
async def test_cancel_after_search_checkpoint_resumes_without_repeating_search() -> None:
    checkpoints = _Checkpoints()
    search = _Search()
    first_browser = _Browser(cancel_once=True)
    service, _, _, _, _, terminal = _service(
        search=search,
        browser=first_browser,
        checkpoints=checkpoints,
    )

    with pytest.raises(asyncio.CancelledError):
        await service.execute(_binding(), authorization_current=_Authorization())

    assert checkpoints.value is not None
    assert checkpoints.value.completed_steps == (AutonomyStep.SEARCH,)
    assert search.calls == 1
    assert terminal.notices == []

    resumed, _, browser, artifacts, _, terminal = _service(
        search=search,
        browser=_Browser(),
        checkpoints=checkpoints,
        terminal=terminal,
    )
    outcome = await resumed.execute(_binding(), authorization_current=_Authorization())

    assert outcome.receipt.status is AutonomyPlanStatus.COMPLETED
    assert search.calls == 1
    assert (browser.calls, artifacts.calls) == (1, 1)
    assert len(terminal.notices) == 1


@pytest.mark.asyncio
async def test_checkpoint_is_bound_to_actor_guild_channel_and_user() -> None:
    original = _binding()
    initial = AutonomyCheckpoint(
        template_id=SEARXNG_OFFICIAL_RESEARCH_V1.template_id,
        template_version=SEARXNG_OFFICIAL_RESEARCH_V1.version,
        plan_digest=SEARXNG_OFFICIAL_RESEARCH_V1.digest,
        binding_digest=original.digest,
        status=AutonomyPlanStatus.RUNNING,
    )
    service, search, browser, artifacts, _, terminal = _service(checkpoints=_Checkpoints(initial))

    with pytest.raises(AutonomyCheckpointError):
        await service.execute(
            _binding(channel_id=21),
            authorization_current=_Authorization(),
        )

    assert (search.calls, browser.calls, artifacts.calls) == (0, 0, 0)
    assert terminal.notices == []


@pytest.mark.asyncio
async def test_authorization_is_rechecked_before_browser_boundary_and_fails_closed() -> None:
    service, search, browser, artifacts, checkpoints, terminal = _service()

    with pytest.raises(AutonomyAuthorizationError):
        await service.execute(
            _binding(),
            authorization_current=_Authorization(deny_at=11),
        )

    assert search.calls == 1
    assert (browser.calls, artifacts.calls) == (0, 0)
    assert checkpoints.value is not None
    assert checkpoints.value.completed_steps == (AutonomyStep.SEARCH,)
    assert terminal.notices == []


@pytest.mark.asyncio
async def test_step_timeout_is_bounded_and_becomes_terminal_error() -> None:
    service, search, browser, artifacts, _, terminal = _service(
        search=_Search(delay=0.1),
        policy=AutonomyPolicy(
            total_timeout_seconds=0.3,
            step_timeout_seconds=0.05,
            failure_retries=0,
        ),
    )

    outcome = await service.execute(_binding(), authorization_current=_Authorization())

    assert outcome.receipt.status is AutonomyPlanStatus.FAILED
    assert outcome.receipt.failure_code == "timeouterror"
    assert search.calls == 1
    assert (browser.calls, artifacts.calls) == (0, 0)
    assert [notice.kind for notice in terminal.notices] == [AutonomyTerminalKind.ERROR]


def test_policy_rejects_template_when_max_steps_is_too_small() -> None:
    with pytest.raises(ValueError, match="max_steps"):
        _service(policy=AutonomyPolicy(max_steps=2))


@pytest.mark.asyncio
async def test_non_document_artifact_is_fail_closed_without_claiming_live_success() -> None:
    invalid = ArtifactRef("not-table", ArtifactKind.SCREENSHOT, "image/png")
    service, _, _, artifacts, _, terminal = _service(artifacts=_Artifacts(invalid))

    outcome = await service.execute(_binding(), authorization_current=_Authorization())

    assert artifacts.calls == 1
    assert outcome.artifact is None
    assert outcome.receipt.status is AutonomyPlanStatus.FAILED
    assert outcome.receipt.failure_code == "autonomycontracterror"
    assert [notice.kind for notice in terminal.notices] == [AutonomyTerminalKind.ERROR]


def test_only_the_exact_sealed_code_owned_template_is_accepted() -> None:
    clone = AutonomyTaskTemplate(
        template_id=SEARXNG_OFFICIAL_RESEARCH_V1.template_id,
        version=SEARXNG_OFFICIAL_RESEARCH_V1.version,
        query="site:docs.searxng.org caller supplied query",
        language="en",
        allowed_hosts=SEARXNG_OFFICIAL_RESEARCH_V1.allowed_hosts,
        steps=SEARXNG_OFFICIAL_RESEARCH_V1.steps,
    )

    with pytest.raises(ValueError, match="sealed code-owned"):
        BoundedAutonomyService(
            search=_Search(),
            browser=_Browser(),
            artifacts=_Artifacts(),
            checkpoints=_Checkpoints(),
            terminal=_Terminal(),
            template=clone,
        )


@pytest.mark.parametrize(
    "artifact",
    [
        ArtifactRef("missing-size", ArtifactKind.SCREENSHOT, "image/png", sha256="d" * 64),
        ArtifactRef("missing-hash", ArtifactKind.SCREENSHOT, "image/png", size_bytes=1),
    ],
)
def test_scoped_artifact_requires_exact_size_and_hash(artifact: ArtifactRef) -> None:
    with pytest.raises(ValueError, match="exact positive size and sha256"):
        _scoped(artifact)


@pytest.mark.asyncio
async def test_cross_scope_screenshot_is_rejected_before_artifact_creation() -> None:
    class _CrossScopeBrowser(_Browser):
        async def inspect(self, request, *, authorization_current):
            self.calls += 1
            return BrowserResearchResult(
                (
                    OfficialPageInspection(
                        OFFICIAL_URL,
                        "Settings",
                        "Typed inspection text.",
                        _scoped(
                            _screenshot(),
                            binding=_binding(task_id=request.binding.task_id, channel_id=999),
                        ),
                    ),
                )
            )

    service, _, browser, artifacts, _, terminal = _service(browser=_CrossScopeBrowser())
    outcome = await service.execute(_binding(), authorization_current=_Authorization())

    assert browser.calls == 1
    assert artifacts.calls == 0
    assert outcome.receipt.status is AutonomyPlanStatus.FAILED
    assert outcome.receipt.failure_code == "autonomycontracterror"
    assert [notice.kind for notice in terminal.notices] == [AutonomyTerminalKind.ERROR]


@pytest.mark.asyncio
async def test_cross_scope_final_artifact_is_rejected() -> None:
    wrong_scope = _scoped(_comparison(), binding=_binding(channel_id=999))
    service, _, _, artifacts, _, terminal = _service(artifacts=_Artifacts(wrong_scope))

    outcome = await service.execute(_binding(), authorization_current=_Authorization())

    assert artifacts.calls == 1
    assert outcome.artifact is None
    assert outcome.receipt.status is AutonomyPlanStatus.FAILED
    assert outcome.receipt.failure_code == "autonomycontracterror"
    assert [notice.kind for notice in terminal.notices] == [AutonomyTerminalKind.ERROR]


@pytest.mark.asyncio
async def test_resume_revalidates_nested_checkpoint_screenshot_scope() -> None:
    binding = _binding()
    browser_result = BrowserResearchResult(
        (
            OfficialPageInspection(
                OFFICIAL_URL,
                "Settings",
                "Typed inspection text.",
                _scoped(_screenshot(), binding=_binding(channel_id=999)),
            ),
        )
    )
    initial = AutonomyCheckpoint(
        template_id=SEARXNG_OFFICIAL_RESEARCH_V1.template_id,
        template_version=SEARXNG_OFFICIAL_RESEARCH_V1.version,
        plan_digest=SEARXNG_OFFICIAL_RESEARCH_V1.digest,
        binding_digest=binding.digest,
        status=AutonomyPlanStatus.RUNNING,
        completed_steps=(AutonomyStep.SEARCH, AutonomyStep.BROWSER_INSPECT),
        search=_search_outcome(binding.task_id),
        browser=browser_result,
    )
    service, search, browser, artifacts, _, terminal = _service(checkpoints=_Checkpoints(initial))

    with pytest.raises(AutonomyCheckpointError, match="screenshot"):
        await service.execute(binding, authorization_current=_Authorization())

    assert (search.calls, browser.calls, artifacts.calls) == (0, 0, 0)
    assert terminal.notices == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("query_digest", f"sha256:{'f' * 64}"),
        ("language", "ja-JP"),
    ],
)
@pytest.mark.asyncio
async def test_resume_rejects_search_with_nonsealed_query_or_language(field: str, value: str) -> None:
    binding = _binding()
    search = _search_outcome(binding.task_id)
    invalid_result = replace(search.result, **{field: value})
    invalid_search = replace(
        search,
        result=invalid_result,
        receipt=SearchReceiptV1.from_result(invalid_result),
    )
    initial = AutonomyCheckpoint(
        template_id=SEARXNG_OFFICIAL_RESEARCH_V1.template_id,
        template_version=SEARXNG_OFFICIAL_RESEARCH_V1.version,
        plan_digest=SEARXNG_OFFICIAL_RESEARCH_V1.digest,
        binding_digest=binding.digest,
        status=AutonomyPlanStatus.RUNNING,
        completed_steps=(AutonomyStep.SEARCH,),
        search=invalid_search,
    )
    service, search_port, browser, artifacts, _, terminal = _service(checkpoints=_Checkpoints(initial))

    with pytest.raises(AutonomyCheckpointError, match="search result"):
        await service.execute(binding, authorization_current=_Authorization())

    assert (search_port.calls, browser.calls, artifacts.calls) == (0, 0, 0)
    assert terminal.notices == []


@pytest.mark.asyncio
async def test_completed_artifact_is_checkpointed_before_post_authorization_and_not_reexecuted() -> None:
    authorization = _MutableAuthorization()
    checkpoints = _Checkpoints()
    artifacts = _RevokingArtifacts(authorization)
    service, _, _, _, _, terminal = _service(
        artifacts=artifacts,
        checkpoints=checkpoints,
    )

    with pytest.raises(AutonomyAuthorizationError):
        await service.execute(_binding(), authorization_current=authorization)

    assert artifacts.calls == 1
    assert checkpoints.value is not None
    assert checkpoints.value.completed_steps == SEARXNG_OFFICIAL_RESEARCH_V1.steps
    assert checkpoints.value.post_authorization_pending is AutonomyStep.COMPARISON_ARTIFACT
    assert terminal.notices == []

    authorization.allowed = True
    resumed, _, _, _, _, terminal = _service(
        artifacts=artifacts,
        checkpoints=checkpoints,
        terminal=terminal,
    )
    outcome = await resumed.execute(_binding(), authorization_current=authorization)

    assert outcome.receipt.status is AutonomyPlanStatus.COMPLETED
    assert artifacts.calls == 1
    assert len(terminal.notices) == 1


@pytest.mark.asyncio
async def test_cancel_after_port_completion_persists_pending_result_for_resume() -> None:
    authorization = _MutableAuthorization()
    checkpoints = _Checkpoints()
    artifacts = _RevokingArtifacts(authorization, cancel=True)
    service, _, _, _, _, terminal = _service(
        artifacts=artifacts,
        checkpoints=checkpoints,
    )

    with pytest.raises(asyncio.CancelledError):
        await service.execute(_binding(), authorization_current=authorization)

    assert artifacts.calls == 1
    assert checkpoints.value is not None
    assert checkpoints.value.completed_steps == SEARXNG_OFFICIAL_RESEARCH_V1.steps
    assert checkpoints.value.post_authorization_pending is AutonomyStep.COMPARISON_ARTIFACT

    resumed, _, _, _, _, terminal = _service(
        artifacts=artifacts,
        checkpoints=checkpoints,
        terminal=terminal,
    )
    outcome = await resumed.execute(_binding(), authorization_current=authorization)

    assert outcome.receipt.status is AutonomyPlanStatus.COMPLETED
    assert artifacts.calls == 1
    assert len(terminal.notices) == 1


@pytest.mark.asyncio
async def test_terminal_ack_timeout_preserves_completed_and_retries_same_final() -> None:
    checkpoints = _Checkpoints()
    terminal = _DeadlineTerminal()
    service, search, browser, artifacts, _, _ = _service(
        checkpoints=checkpoints,
        terminal=terminal,
        policy=AutonomyPolicy(
            total_timeout_seconds=0.05,
            step_timeout_seconds=0.05,
            failure_retries=0,
        ),
    )

    outcome = await service.execute(_binding(), authorization_current=_Authorization())

    assert outcome.receipt.status is AutonomyPlanStatus.COMPLETED
    assert outcome.receipt.failure_code is None
    assert outcome.artifact is not None
    assert search.calls == 1
    assert (browser.calls, artifacts.calls) == (1, 1)
    assert checkpoints.value is not None
    assert checkpoints.value.status is AutonomyPlanStatus.COMPLETED
    assert checkpoints.value.terminal_emitted is False
    assert terminal.notices == []
    assert terminal.calls == 1

    replay = await service.execute(_binding(), authorization_current=_Authorization())
    assert replay.receipt == outcome.receipt
    assert replay.artifact == outcome.artifact
    assert terminal.calls == 2
    assert len(terminal.notices) == 1
    assert terminal.notices[0].kind is AutonomyTerminalKind.FINAL
    assert checkpoints.value is not None
    assert checkpoints.value.terminal_emitted is True


@pytest.mark.asyncio
async def test_committed_terminal_timeout_preserves_final_and_retries_same_notice() -> None:
    checkpoints = _Checkpoints()
    terminal = _CommittedThenSlowTerminal()
    service, search, browser, artifacts, _, _ = _service(
        checkpoints=checkpoints,
        terminal=terminal,
        policy=AutonomyPolicy(
            total_timeout_seconds=0.05,
            step_timeout_seconds=0.05,
            failure_retries=0,
        ),
    )

    outcome = await service.execute(_binding(), authorization_current=_Authorization())

    assert outcome.receipt.status is AutonomyPlanStatus.COMPLETED
    assert outcome.receipt.failure_code is None
    assert outcome.artifact is not None
    assert (search.calls, browser.calls, artifacts.calls) == (1, 1, 1)
    assert checkpoints.value is not None
    assert checkpoints.value.status is AutonomyPlanStatus.COMPLETED
    assert checkpoints.value.terminal_emitted is False
    assert [notice.kind for notice in terminal.notices] == [AutonomyTerminalKind.FINAL]
    assert terminal.calls == 1

    replay = await service.execute(_binding(), authorization_current=_Authorization())
    assert replay.receipt.status is AutonomyPlanStatus.COMPLETED
    assert replay.artifact == outcome.artifact
    assert terminal.calls == 2
    assert len(terminal.notices) == 1
    assert checkpoints.value is not None
    assert checkpoints.value.terminal_emitted is True


def test_running_checkpoint_cannot_claim_a_terminal_was_emitted() -> None:
    with pytest.raises(ValueError, match="running checkpoint"):
        AutonomyCheckpoint(
            template_id=SEARXNG_OFFICIAL_RESEARCH_V1.template_id,
            template_version=SEARXNG_OFFICIAL_RESEARCH_V1.version,
            plan_digest=SEARXNG_OFFICIAL_RESEARCH_V1.digest,
            binding_digest=_binding().digest,
            status=AutonomyPlanStatus.RUNNING,
            terminal_emitted=True,
        )


@pytest.mark.asyncio
async def test_failed_timeout_never_exposes_completed_artifact_on_first_or_replay() -> None:
    checkpoints = _SlowCompletedStatusCheckpoints()
    service, search, browser, artifacts, _, terminal = _service(
        checkpoints=checkpoints,
        policy=AutonomyPolicy(
            total_timeout_seconds=0.05,
            step_timeout_seconds=0.05,
            failure_retries=0,
        ),
    )

    outcome = await service.execute(_binding(), authorization_current=_Authorization())

    assert (search.calls, browser.calls, artifacts.calls) == (1, 1, 1)
    assert outcome.receipt.status is AutonomyPlanStatus.FAILED
    assert outcome.receipt.failure_code == "plan_timeout"
    assert outcome.receipt.artifact_id is None
    assert outcome.artifact is None
    assert checkpoints.value is not None
    assert checkpoints.value.artifact is not None
    assert [notice.kind for notice in terminal.notices] == [AutonomyTerminalKind.ERROR]

    replay = await service.execute(_binding(), authorization_current=_Authorization())
    assert replay.receipt == outcome.receipt
    assert replay.artifact is None
    assert (search.calls, browser.calls, artifacts.calls) == (1, 1, 1)
    assert len(terminal.notices) == 1


@pytest.mark.asyncio
async def test_committed_final_is_not_rewritten_when_checkpoint_ack_crosses_deadline() -> None:
    checkpoints = _CommittedThenSlowCheckpoints()
    terminal = _Terminal()
    service, search, browser, artifacts, _, _ = _service(
        checkpoints=checkpoints,
        terminal=terminal,
        policy=AutonomyPolicy(
            total_timeout_seconds=0.05,
            step_timeout_seconds=0.05,
            failure_retries=0,
        ),
    )

    outcome = await service.execute(_binding(), authorization_current=_Authorization())

    assert outcome.receipt.status is AutonomyPlanStatus.COMPLETED
    assert outcome.receipt.failure_code is None
    assert (search.calls, browser.calls, artifacts.calls) == (1, 1, 1)
    assert checkpoints.value is not None
    assert checkpoints.value.status is AutonomyPlanStatus.COMPLETED
    assert checkpoints.value.terminal_emitted is True
    assert [notice.kind for notice in terminal.notices] == [AutonomyTerminalKind.FINAL]

    replay = await service.execute(_binding(), authorization_current=_Authorization())
    assert replay.receipt == outcome.receipt
    assert len(terminal.notices) == 1
