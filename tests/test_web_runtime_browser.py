from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from yonerai_discord.browser_sandbox.models import (
    BrowserOutput,
    BrowserOutputKind,
    BrowserSessionRequest,
    BrowserSessionResult,
    CssSelector,
    ExtractText,
    Navigate,
    Screenshot,
    TypeText,
)
from yonerai_discord.browser_sandbox.policy import (
    BrowserSandboxLimits,
    BrowserSandboxPolicy,
    StaticDnsResolver,
)
from yonerai_discord.browser_sandbox.worker_contract import (
    BROWSER_WORKER_PROTOCOL_REVISION,
    BROWSER_WORKER_SUPPORTED_ACTIONS,
    BrowserWorkerExecuteRequest,
    BrowserWorkerExecutionResult,
    BrowserWorkerHandshakeRequest,
    BrowserWorkerHandshakeResponse,
    BrowserWorkerTerminateRequest,
    BrowserWorkerTerminationReceipt,
)
from yonerai_discord.modules.web_runtime.browser import (
    AllowedOriginPolicy,
    BoundedBrowserRunner,
    BrowserBackendUnavailableError,
    BrowserCancellation,
    BrowserCheckpoint,
    BrowserPlanSegment,
    BrowserRunCancelledError,
    BrowserRunCleanupUnconfirmedError,
    BrowserRunContractError,
    BrowserRunLimits,
    BrowserRunPlan,
    BrowserRunScope,
    ExistingWorkerSessionFactory,
    playwright_worker_availability,
)
from yonerai_discord.modules.web_runtime.search import WebBackendBlockerCode


class FakeWorkerTransport:
    def __init__(
        self,
        request_id: str,
        *,
        block_execute: bool = False,
        confirm_terminate: bool = True,
    ) -> None:
        self.request_id = request_id
        self.block_execute = block_execute
        self.confirm_terminate = confirm_terminate
        self.execute_started = asyncio.Event()
        self.close_requests: list[BrowserWorkerTerminateRequest] = []
        self.terminate_requests: list[BrowserWorkerTerminateRequest] = []

    async def handshake(self, request: BrowserWorkerHandshakeRequest) -> BrowserWorkerHandshakeResponse:
        return BrowserWorkerHandshakeResponse(
            protocol_revision=BROWSER_WORKER_PROTOCOL_REVISION,
            request_id=request.request_id,
            session_id=request.session_id,
            nonce=request.nonce,
            isolation_contract_digest=request.isolation_contract_digest,
            policy_snapshot_digest=request.policy_snapshot_digest,
            supported_actions=BROWSER_WORKER_SUPPORTED_ACTIONS,
            external_worker_identity="typed-fake-worker",
            ephemeral_profile=True,
            context_reuse_enabled=False,
            downloads_enabled=False,
            uploads_enabled=False,
            script_evaluation_enabled=False,
            developer_protocol_enabled=False,
            host_mount_enabled=False,
            clipboard_enabled=False,
            credential_import_enabled=False,
        )

    async def execute(self, request: BrowserWorkerExecuteRequest) -> BrowserWorkerExecutionResult:
        self.execute_started.set()
        if self.block_execute:
            await asyncio.Event().wait()
        outputs: list[BrowserOutput] = []
        for index, action in enumerate(request.actions):
            if isinstance(action, Screenshot):
                outputs.append(
                    BrowserOutput(
                        step_index=index,
                        kind=BrowserOutputKind.SCREENSHOT,
                        data=f"png:{self.request_id}:{index}".encode(),
                        media_type="image/png",
                    )
                )
            elif isinstance(action, ExtractText):
                outputs.append(
                    BrowserOutput(
                        step_index=index,
                        kind=BrowserOutputKind.TEXT,
                        data="YouTube typed fake result".encode(),
                        media_type="text/plain; charset=utf-8",
                    )
                )
        consumed = sum(output.byte_length for output in outputs)
        handshake = request.handshake
        return BrowserWorkerExecutionResult(
            request_id=handshake.request_id,
            session_id=handshake.session_id,
            nonce=handshake.nonce,
            isolation_contract_digest=handshake.isolation_contract_digest,
            policy_snapshot_digest=handshake.policy_snapshot_digest,
            external_worker_identity=request.external_worker_identity,
            outputs=tuple(outputs),
            completed_actions=len(request.actions),
            network_request_count=sum(isinstance(action, Navigate) for action in request.actions),
            redirect_count=0,
            consumed_bytes=consumed,
        )

    async def close(self, request: BrowserWorkerTerminateRequest) -> BrowserWorkerTerminationReceipt:
        self.close_requests.append(request)
        return _termination_receipt(request)

    async def terminate(self, request: BrowserWorkerTerminateRequest) -> BrowserWorkerTerminationReceipt:
        self.terminate_requests.append(request)
        return _termination_receipt(request, confirmed=self.confirm_terminate)


class RecordingTransportFactory:
    def __init__(self, *, block_execute: bool = False, confirm_terminate: bool = True) -> None:
        self.block_execute = block_execute
        self.confirm_terminate = confirm_terminate
        self.transports: list[FakeWorkerTransport] = []

    def __call__(self, request_id: str, policy: BrowserSandboxPolicy) -> FakeWorkerTransport:
        assert isinstance(policy, BrowserSandboxPolicy)
        transport = FakeWorkerTransport(
            request_id,
            block_execute=self.block_execute,
            confirm_terminate=self.confirm_terminate,
        )
        self.transports.append(transport)
        return transport


class CancellingCheckpointStore:
    def __init__(self, cancellation: BrowserCancellation) -> None:
        self.cancellation = cancellation
        self.checkpoints: list[BrowserCheckpoint] = []

    async def save(self, checkpoint: BrowserCheckpoint) -> None:
        self.checkpoints.append(checkpoint)
        if len(self.checkpoints) == 1:
            self.cancellation.cancel()


class StaticResultSession:
    cleanup_confirmed = True

    def __init__(self, result: BrowserSessionResult) -> None:
        self.result = result

    async def execute(self, _request: BrowserSessionRequest) -> BrowserSessionResult:
        return self.result


class StaticResultSessionFactory:
    configured = True

    def __init__(self, result: BrowserSessionResult) -> None:
        self.result = result

    def create(self, *, request_id: str, policy: BrowserSandboxPolicy) -> StaticResultSession:
        assert request_id
        assert isinstance(policy, BrowserSandboxPolicy)
        return StaticResultSession(self.result)


def _termination_receipt(
    request: BrowserWorkerTerminateRequest,
    *,
    confirmed: bool = True,
) -> BrowserWorkerTerminationReceipt:
    return BrowserWorkerTerminationReceipt(
        protocol_revision=request.protocol_revision,
        request_id=request.request_id,
        session_id=request.session_id,
        nonce=request.nonce,
        isolation_contract_digest=request.isolation_contract_digest,
        policy_snapshot_digest=request.policy_snapshot_digest,
        external_worker_identity=request.external_worker_identity,
        reason=request.reason,
        worker_terminated=confirmed,
        profile_destroyed=confirmed,
    )


def _policy() -> BrowserSandboxPolicy:
    return BrowserSandboxPolicy(
        resolver=StaticDnsResolver({"www.youtube.com": ("142.250.72.206",)}),
        allowed_domains=("www.youtube.com",),
        limits=BrowserSandboxLimits(
            max_steps=20,
            max_redirects=3,
            max_network_requests=40,
            max_duration_seconds=5,
            max_total_bytes=1_000_000,
            max_total_wait_milliseconds=1_000,
        ),
    )


def _scope(*, request_id: str = "discord-request", user_id: int = 300) -> BrowserRunScope:
    return BrowserRunScope(
        request_id=request_id,
        guild_id=100,
        channel_id=200,
        user_id=user_id,
    )


def _youtube_plan() -> BrowserRunPlan:
    return BrowserRunPlan(
        plan_id="youtube-sequence",
        scope=_scope(),
        segments=(
            BrowserPlanSegment(
                segment_id="search",
                actions=(
                    Navigate("https://www.youtube.com/results?search_query=yonerai"),
                    TypeText(CssSelector("input#search"), "YonerAI", clear_first=True),
                    Screenshot(full_page=False),
                ),
            ),
            BrowserPlanSegment(
                segment_id="watch",
                actions=(
                    Navigate("https://www.youtube.com/watch?v=typedfake01"),
                    ExtractText(CssSelector("h1")),
                    Screenshot(full_page=True),
                ),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_youtube_typed_fake_sequence_checkpoints_and_resumes() -> None:
    cancellation = BrowserCancellation()
    store = CancellingCheckpointStore(cancellation)
    transport_factory = RecordingTransportFactory()
    runner = BoundedBrowserRunner(
        policy=_policy(),
        origin_policy=AllowedOriginPolicy(("https://www.youtube.com",)),
        session_factory=ExistingWorkerSessionFactory(transport_factory),
        checkpoint_store=store,
        limits=BrowserRunLimits(max_steps=10, max_segments=2, total_timeout_seconds=5),
        enabled=True,
    )
    plan = _youtube_plan()

    with pytest.raises(BrowserRunCancelledError):
        await runner.run(plan, cancellation=cancellation)

    checkpoint = store.checkpoints[-1]
    assert checkpoint.next_segment_index == 1
    assert len(checkpoint.artifacts) == 1
    assert transport_factory.transports[0].close_requests

    resumed = await runner.run(plan, checkpoint=checkpoint)

    assert resumed.checkpoint.next_segment_index == 2
    assert len(resumed.checkpoint.artifacts) == 2
    assert len(resumed.screenshot_artifacts) == 1
    assert resumed.outputs[0].global_step_index == 4
    assert resumed.outputs[0].output.data.decode() == "YouTube typed fake result"
    assert all(reference.source_origin == "https://www.youtube.com" for reference in resumed.checkpoint.artifacts)
    assert len(transport_factory.transports) == 2
    assert transport_factory.transports[1].close_requests


@pytest.mark.asyncio
async def test_active_cancellation_terminates_worker_and_confirms_profile_cleanup() -> None:
    cancellation = BrowserCancellation()
    transport_factory = RecordingTransportFactory(block_execute=True)
    runner = BoundedBrowserRunner(
        policy=_policy(),
        origin_policy=AllowedOriginPolicy(("https://www.youtube.com",)),
        session_factory=ExistingWorkerSessionFactory(transport_factory),
        limits=BrowserRunLimits(total_timeout_seconds=5),
        enabled=True,
    )
    plan = BrowserRunPlan(
        plan_id="cancel-active",
        scope=_scope(request_id="cancel-active-request"),
        segments=(
            BrowserPlanSegment(
                segment_id="only",
                actions=(Navigate("https://www.youtube.com/"), Screenshot()),
            ),
        ),
    )

    task = asyncio.create_task(runner.run(plan, cancellation=cancellation))
    while not transport_factory.transports:
        await asyncio.sleep(0)
    await asyncio.wait_for(transport_factory.transports[0].execute_started.wait(), timeout=1)
    cancellation.cancel()

    with pytest.raises(BrowserRunCancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert len(transport_factory.transports[0].terminate_requests) == 1
    assert transport_factory.transports[0].terminate_requests[0].reason.value == "cancelled"


@pytest.mark.asyncio
async def test_cancel_and_timeout_fail_closed_when_worker_cleanup_is_unconfirmed() -> None:
    cancellation = BrowserCancellation()
    cancelled_factory = RecordingTransportFactory(block_execute=True, confirm_terminate=False)
    cancelled_runner = BoundedBrowserRunner(
        policy=_policy(),
        origin_policy=AllowedOriginPolicy(("https://www.youtube.com",)),
        session_factory=ExistingWorkerSessionFactory(cancelled_factory),
        limits=BrowserRunLimits(total_timeout_seconds=5),
        enabled=True,
    )
    plan = BrowserRunPlan(
        plan_id="cleanup-unconfirmed",
        scope=_scope(request_id="cleanup-unconfirmed-request"),
        segments=(
            BrowserPlanSegment(
                segment_id="only",
                actions=(Navigate("https://www.youtube.com/"), Screenshot()),
            ),
        ),
    )

    task = asyncio.create_task(cancelled_runner.run(plan, cancellation=cancellation))
    while not cancelled_factory.transports:
        await asyncio.sleep(0)
    await asyncio.wait_for(cancelled_factory.transports[0].execute_started.wait(), timeout=1)
    cancellation.cancel()
    with pytest.raises(BrowserRunCleanupUnconfirmedError, match="cleanup"):
        await asyncio.wait_for(task, timeout=1)

    timeout_factory = RecordingTransportFactory(block_execute=True, confirm_terminate=False)
    timeout_runner = BoundedBrowserRunner(
        policy=_policy(),
        origin_policy=AllowedOriginPolicy(("https://www.youtube.com",)),
        session_factory=ExistingWorkerSessionFactory(timeout_factory),
        limits=BrowserRunLimits(total_timeout_seconds=1),
        enabled=True,
    )
    with pytest.raises(BrowserRunCleanupUnconfirmedError, match="timeout"):
        await timeout_runner.run(plan)
    assert len(timeout_factory.transports[0].terminate_requests) == 1


@pytest.mark.asyncio
async def test_external_task_cancellation_is_not_replaced_by_cleanup_error() -> None:
    transport_factory = RecordingTransportFactory(block_execute=True, confirm_terminate=False)
    runner = BoundedBrowserRunner(
        policy=_policy(),
        origin_policy=AllowedOriginPolicy(("https://www.youtube.com",)),
        session_factory=ExistingWorkerSessionFactory(transport_factory),
        limits=BrowserRunLimits(total_timeout_seconds=5),
        enabled=True,
    )
    task = asyncio.create_task(runner.run(_youtube_plan()))
    while not transport_factory.transports:
        await asyncio.sleep(0)
    await asyncio.wait_for(transport_factory.transports[0].execute_started.wait(), timeout=1)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_checkpoint_scope_and_artifact_bindings_reject_replay_and_tampering() -> None:
    cancellation = BrowserCancellation()
    store = CancellingCheckpointStore(cancellation)
    transport_factory = RecordingTransportFactory()
    runner = BoundedBrowserRunner(
        policy=_policy(),
        origin_policy=AllowedOriginPolicy(("https://www.youtube.com",)),
        session_factory=ExistingWorkerSessionFactory(transport_factory),
        checkpoint_store=store,
        limits=BrowserRunLimits(max_steps=10, max_segments=2, total_timeout_seconds=5),
        enabled=True,
    )
    plan = _youtube_plan()
    with pytest.raises(BrowserRunCancelledError):
        await runner.run(plan, cancellation=cancellation)
    checkpoint = store.checkpoints[-1]
    artifact = checkpoint.artifacts[0]
    assert artifact.scope == plan.scope

    replayed_plan = replace(plan, scope=_scope(request_id="other-request", user_id=301))
    with pytest.raises(BrowserRunContractError, match="checkpoint"):
        await runner.run(replayed_plan, checkpoint=checkpoint)

    with pytest.raises(ValueError, match="steps"):
        replace(
            checkpoint,
            artifacts=(
                artifact,
                replace(artifact, artifact_id="shot-duplicate-binding"),
            ),
        )
    with pytest.raises(ValueError, match="scope"):
        replace(
            checkpoint,
            artifacts=(replace(artifact, scope=replayed_plan.scope),),
        )

    tampered = replace(
        checkpoint,
        artifacts=(replace(artifact, artifact_id=f"shot-{'0' * 32}"),),
    )
    with pytest.raises(BrowserRunContractError, match="binding"):
        await runner.run(plan, checkpoint=tampered)


@pytest.mark.asyncio
async def test_each_screenshot_binds_the_most_recent_declared_navigation_origin() -> None:
    transport_factory = RecordingTransportFactory()
    policy = BrowserSandboxPolicy(
        resolver=StaticDnsResolver(
            {
                "www.youtube.com": ("142.250.72.206",),
                "example.com": ("93.184.216.34",),
            }
        ),
        allowed_domains=("www.youtube.com", "example.com"),
        limits=BrowserSandboxLimits(max_duration_seconds=5),
    )
    runner = BoundedBrowserRunner(
        policy=policy,
        origin_policy=AllowedOriginPolicy(("https://www.youtube.com", "https://example.com")),
        session_factory=ExistingWorkerSessionFactory(transport_factory),
        enabled=True,
    )
    plan = BrowserRunPlan(
        plan_id="multi-origin",
        scope=_scope(request_id="multi-origin-request"),
        segments=(
            BrowserPlanSegment(
                segment_id="only",
                actions=(
                    Navigate("https://www.youtube.com/results"),
                    Screenshot(),
                    Navigate("https://example.com/result"),
                    Screenshot(),
                ),
            ),
        ),
    )

    result = await runner.run(plan)

    assert [item.reference.source_origin for item in result.screenshot_artifacts] == [
        "https://www.youtube.com",
        "https://example.com",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("missing", "duplicate", "wrong_action"))
async def test_runner_rejects_outputs_not_exactly_bound_to_declared_actions(mode: str) -> None:
    screenshot = BrowserOutput(1, BrowserOutputKind.SCREENSHOT, b"png", "image/png")
    text = BrowserOutput(2, BrowserOutputKind.TEXT, b"text", "text/plain; charset=utf-8")
    if mode == "missing":
        outputs = (screenshot,)
    elif mode == "duplicate":
        outputs = (screenshot, screenshot, text)
    else:
        outputs = (
            BrowserOutput(1, BrowserOutputKind.TEXT, b"text", "text/plain; charset=utf-8"),
            text,
        )
    runner = BoundedBrowserRunner(
        policy=_policy(),
        origin_policy=AllowedOriginPolicy(("https://www.youtube.com",)),
        session_factory=StaticResultSessionFactory(BrowserSessionResult(outputs=outputs)),
        limits=BrowserRunLimits(total_timeout_seconds=5),
        enabled=True,
    )
    plan = BrowserRunPlan(
        plan_id=f"invalid-output-{mode}",
        scope=_scope(request_id=f"invalid-output-{mode}"),
        segments=(
            BrowserPlanSegment(
                segment_id="only",
                actions=(
                    Navigate("https://www.youtube.com/"),
                    Screenshot(),
                    ExtractText(CssSelector("h1")),
                ),
            ),
        ),
    )

    with pytest.raises(BrowserRunContractError, match="output"):
        await runner.run(plan)


@pytest.mark.asyncio
async def test_runner_and_playwright_availability_report_distinct_exact_blockers(monkeypatch) -> None:
    runner = BoundedBrowserRunner(
        policy=_policy(),
        origin_policy=AllowedOriginPolicy(("https://www.youtube.com",)),
        session_factory=ExistingWorkerSessionFactory(),
        enabled=True,
    )
    assert runner.availability.blocker is WebBackendBlockerCode.UNCONFIGURED
    with pytest.raises(BrowserBackendUnavailableError) as error:
        await runner.run(_youtube_plan())
    assert error.value.code is WebBackendBlockerCode.UNCONFIGURED

    monkeypatch.setattr(
        "yonerai_discord.modules.web_runtime.browser.importlib.util.find_spec",
        lambda _name: None,
    )
    dependency = playwright_worker_availability(worker_transport_configured=False)
    assert dependency.available is False
    assert dependency.blocker is WebBackendBlockerCode.DEPENDENCY_MISSING


@pytest.mark.asyncio
async def test_disallowed_origin_is_rejected_before_worker_creation() -> None:
    transport_factory = RecordingTransportFactory()
    network_policy = BrowserSandboxPolicy(
        resolver=StaticDnsResolver(
            {
                "www.youtube.com": ("142.250.72.206",),
                "example.com": ("93.184.216.34",),
            }
        ),
        allowed_domains=("www.youtube.com", "example.com"),
        limits=BrowserSandboxLimits(max_duration_seconds=5),
    )
    runner = BoundedBrowserRunner(
        policy=network_policy,
        origin_policy=AllowedOriginPolicy(("https://www.youtube.com",)),
        session_factory=ExistingWorkerSessionFactory(transport_factory),
        enabled=True,
    )
    plan = BrowserRunPlan(
        plan_id="wrong-origin",
        scope=_scope(request_id="wrong-origin-request"),
        segments=(
            BrowserPlanSegment(
                segment_id="search",
                actions=(Navigate("https://example.com/"), Screenshot()),
            ),
        ),
    )

    with pytest.raises(BrowserRunContractError, match="origin"):
        await runner.run(plan)
    assert transport_factory.transports == []
