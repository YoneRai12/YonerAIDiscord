from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yonerai_discord.ai_control import RiskLevel
from yonerai_discord.capabilities import COMMAND_CAPABILITIES, COMMAND_RBAC_FLOORS
from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.modules.ai.action_router import (
    ActionEffect,
    ActionRegistry,
    ActionResult,
    ActionSpec,
    ActionStatus,
    NaturalActionRouter,
    PlannerActionContract,
)
from yonerai_discord.modules.ai.models import AIRequest
from yonerai_discord.modules.ai.orchestration import (
    OrchestrationEngine,
    OrchestrationPlan,
    OrchestrationPolicy,
    OrchestrationStep,
    PlanExecutionInProgressError,
    PlanEventType,
    PlanIdempotencyConflictError,
    PlanStatus,
    StepStatus,
)
from yonerai_discord.modules.ai.orchestration_repository import (
    DurableClaimKind,
    DurableStepDefinition,
    DurableStepRecord,
    OrchestrationRepositoryError,
    SqliteOrchestrationRepository,
    StartupReconciliationReceipt,
    StartupReconciliationStatus,
)
from yonerai_discord.modules.media_pipeline.domain import ArtifactKind, ArtifactRef, ArtifactScope


GUILD_ID = 101
CHANNEL_ID = 202
USER_ID = 303
REQUEST_ID = "request-404"


class _Clock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value


class _Guard:
    async def evaluate_fresh_member(self, capability_id: str, *, guild: Any, member: Any) -> Any:
        return SimpleNamespace(allowed=True, actor_level=RbacLevel.BOT_OWNER)

    def currently_allowed(self, *_: Any, **__: Any) -> bool:
        return True


def _contract(*, artifact: bool = False, retry_safe: bool = False) -> PlannerActionContract:
    return PlannerActionContract(
        "durable test action",
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
            "required": [],
        },
        ("durable",),
        ("test",),
        RiskLevel.LOW,
        {"type": "artifact_ref", "kind": "qr_code"} if artifact else None,
        retry_safe=retry_safe,
    )


def _spec(
    action_id: str,
    executor: Any,
    *,
    effect: ActionEffect,
    artifact: bool = False,
    retry_safe: bool = False,
) -> ActionSpec:
    command_path = "tools dice" if effect is ActionEffect.READ_ONLY else "music shuffle"
    return ActionSpec(
        action_id,
        command_path,
        COMMAND_CAPABILITIES[command_path],
        COMMAND_RBAC_FLOORS.get(command_path, RbacLevel.EVERYONE),
        lambda _: None,
        executor,
        effect=effect,
        planner_contract=_contract(artifact=artifact, retry_safe=retry_safe),
    )


def _environment(
    specs: tuple[ActionSpec, ...],
    repository: SqliteOrchestrationRepository,
    *,
    policy: OrchestrationPolicy | None = None,
) -> tuple[OrchestrationEngine, Any, AIRequest]:
    guard = _Guard()
    member = SimpleNamespace(id=USER_ID)

    async def fetch_member(user_id: int) -> Any:
        assert user_id == USER_ID
        return member

    guild = SimpleNamespace(id=GUILD_ID, fetch_member=fetch_member)
    channel = SimpleNamespace(id=CHANNEL_ID)
    channel.permissions_for = lambda _: SimpleNamespace(view_channel=True, read_message_history=True)
    message = SimpleNamespace(
        id=404,
        guild=guild,
        channel=channel,
        author=SimpleNamespace(id=USER_ID),
    )
    capability_ids = frozenset(capability_id for spec in specs for _, capability_id, _ in spec.capability_requirements)
    capability_registry = SimpleNamespace(
        capability_status=lambda capability_id, _guild_id=None: SimpleNamespace(
            executable=capability_id in capability_ids
        ),
        runtime_available=lambda capability_id: capability_id in capability_ids,
    )
    guard.registry = capability_registry
    bot = SimpleNamespace(
        is_closing=False,
        capability_guard=guard,
        capability_registry=capability_registry,
        runtime_capability_readiness=dict.fromkeys(capability_ids, True),
    )
    router = NaturalActionRouter(bot, registry=ActionRegistry(specs))
    return (
        OrchestrationEngine(router, repository=repository, policy=policy),
        message,
        AIRequest("plan", GUILD_ID, USER_ID, CHANNEL_ID),
    )


def _plan(
    *steps: OrchestrationStep,
    idempotency_key: str = "durable-key",
) -> OrchestrationPlan:
    return OrchestrationPlan(
        REQUEST_ID,
        GUILD_ID,
        CHANNEL_ID,
        USER_ID,
        idempotency_key,
        steps,
    )


async def _execute(
    engine: OrchestrationEngine,
    plan: OrchestrationPlan,
    message: Any,
    request: AIRequest,
):
    return await engine.execute_outcome(
        plan,
        message=message,
        request=request,
        request_id=REQUEST_ID,
    )


def _artifact(marker: str = "a") -> ArtifactRef:
    scope = ArtifactScope(REQUEST_ID, GUILD_ID, CHANNEL_ID, USER_ID)
    return ArtifactRef(
        "mp-" + marker * 64,
        scope.digest,
        "b" * 64,
        "c" * 64,
        ArtifactKind.QR_CODE,
        32,
        32,
        128,
    )


@pytest.fixture
def repository(tmp_path: Path) -> SqliteOrchestrationRepository:
    return SqliteOrchestrationRepository(tmp_path / "orchestration.sqlite3")


def test_engine_rejects_repository_lease_not_longer_than_plan_timeout(tmp_path: Path) -> None:
    repository = SqliteOrchestrationRepository(
        tmp_path / "short-lease.sqlite3",
        lease_seconds=1.0,
    )
    spec = _spec(
        "durable.read",
        lambda _context, _parameters: None,
        effect=ActionEffect.READ_ONLY,
    )
    with pytest.raises(ValueError, match="lease"):
        _environment(
            (spec,),
            repository,
            policy=OrchestrationPolicy(total_timeout_seconds=1.0),
        )


def test_startup_reconciliation_leaves_active_lease_untouched(tmp_path: Path) -> None:
    clock = _Clock()
    repository = SqliteOrchestrationRepository(
        tmp_path / "startup-active.sqlite3",
        lease_seconds=10.0,
        clock=clock,
    )
    claim = repository.claim(
        idempotency_key="startup-active",
        plan_digest="a" * 64,
        request_id=REQUEST_ID,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        user_id=USER_ID,
        steps=(DurableStepDefinition("read", "durable.read", "read_only"),),
    )
    assert claim.owner_token is not None
    repository.mark_step_started(
        idempotency_key="startup-active",
        plan_digest="a" * 64,
        owner_token=claim.owner_token,
        step_id="read",
    )
    with sqlite3.connect(repository.path) as connection:
        before = connection.execute(
            """
            SELECT owner_token, lease_until, updated_at
            FROM ai_orchestration_runs
            WHERE idempotency_key = 'startup-active'
            """
        ).fetchone()

    receipt = repository.reconcile_startup()

    with sqlite3.connect(repository.path) as connection:
        after = connection.execute(
            """
            SELECT owner_token, lease_until, updated_at
            FROM ai_orchestration_runs
            WHERE idempotency_key = 'startup-active'
            """
        ).fetchone()
        step_state = connection.execute(
            """
            SELECT state
            FROM ai_orchestration_steps
            WHERE idempotency_key = 'startup-active' AND step_id = 'read'
            """
        ).fetchone()[0]
    assert after == before
    assert step_state == "started"
    assert receipt == StartupReconciliationReceipt(
        StartupReconciliationStatus.COMPLETED,
        running_runs=1,
        active_leases=1,
        expired_leases=0,
        released_read_only_runs=0,
        quarantined_side_effect_runs=0,
        terminalized_failed_runs=0,
        cancellation_requested_runs=0,
    )
    assert "startup-active" not in repr(receipt)
    assert str(repository.path) not in repr(receipt)
    with pytest.raises(FrozenInstanceError):
        receipt.running_runs = 2  # type: ignore[misc]


def test_startup_reconciliation_samples_clock_after_begin_immediate_wait(tmp_path: Path) -> None:
    clock = _Clock()
    begin_tracking_enabled = threading.Event()
    begin_attempted = threading.Event()

    class _BeginTrackingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def execute(self, sql: str, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
            if begin_tracking_enabled.is_set() and sql.strip().upper() == "BEGIN IMMEDIATE":
                begin_attempted.set()
            return self._connection.execute(sql, *args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._connection, name)

    def tracked_connect(*args: Any, **kwargs: Any) -> _BeginTrackingConnection:
        return _BeginTrackingConnection(sqlite3.connect(*args, **kwargs))

    repository = SqliteOrchestrationRepository(
        tmp_path / "startup-clock-wait.sqlite3",
        lease_seconds=1.0,
        clock=clock,
        connect=tracked_connect,
    )
    claim = repository.claim(
        idempotency_key="startup-clock-wait",
        plan_digest="e" * 64,
        request_id=REQUEST_ID,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        user_id=USER_ID,
        steps=(DurableStepDefinition("read", "durable.read", "read_only"),),
    )
    assert claim.owner_token is not None
    repository.mark_step_started(
        idempotency_key="startup-clock-wait",
        plan_digest="e" * 64,
        owner_token=claim.owner_token,
        step_id="read",
    )

    blocker = sqlite3.connect(repository.path, timeout=5.0, isolation_level=None)
    receipts: list[StartupReconciliationReceipt] = []
    failures: list[BaseException] = []

    def reconcile() -> None:
        try:
            receipts.append(repository.reconcile_startup())
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=reconcile)
    blocker.execute("BEGIN IMMEDIATE")
    begin_tracking_enabled.set()
    worker.start()
    try:
        assert begin_attempted.wait(timeout=2.0)
        clock.value += 2.0
        blocker.commit()
    finally:
        if blocker.in_transaction:
            blocker.rollback()
        blocker.close()
        worker.join(timeout=5.0)

    assert worker.is_alive() is False
    assert failures == []
    assert len(receipts) == 1
    assert receipts[0].active_leases == 0
    assert receipts[0].expired_leases == 1
    assert receipts[0].released_read_only_runs == 1


def test_startup_reconciliation_releases_expired_read_only_for_exact_replay(tmp_path: Path) -> None:
    clock = _Clock()
    repository = SqliteOrchestrationRepository(
        tmp_path / "startup-read-only.sqlite3",
        lease_seconds=1.0,
        clock=clock,
    )
    steps = (
        DurableStepDefinition("first", "durable.first", "read_only"),
        DurableStepDefinition("second", "durable.second", "read_only"),
    )
    claim = repository.claim(
        idempotency_key="startup-read-only",
        plan_digest="b" * 64,
        request_id=REQUEST_ID,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        user_id=USER_ID,
        steps=steps,
    )
    assert claim.owner_token is not None
    repository.mark_step_started(
        idempotency_key="startup-read-only",
        plan_digest="b" * 64,
        owner_token=claim.owner_token,
        step_id="first",
    )
    repository.checkpoint_step(
        idempotency_key="startup-read-only",
        plan_digest="b" * 64,
        owner_token=claim.owner_token,
        step_id="first",
        state="completed",
        action_status="completed",
        failure_code=None,
        public_text="checkpoint",
    )
    repository.mark_step_started(
        idempotency_key="startup-read-only",
        plan_digest="b" * 64,
        owner_token=claim.owner_token,
        step_id="second",
    )
    clock.value += 2.0

    receipt = repository.reconcile_startup()
    replay = repository.claim(
        idempotency_key="startup-read-only",
        plan_digest="b" * 64,
        request_id=REQUEST_ID,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        user_id=USER_ID,
        steps=steps,
    )

    assert receipt.expired_leases == 1
    assert receipt.released_read_only_runs == 1
    assert replay.kind is DurableClaimKind.RESUME
    assert replay.owner_token is not None
    assert [(step.state, step.public_text) for step in replay.record.steps] == [
        ("completed", "checkpoint"),
        ("pending", None),
    ]


def test_startup_reconciliation_quarantines_expired_side_effect_without_new_owner(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    repository = SqliteOrchestrationRepository(
        tmp_path / "startup-side-effect.sqlite3",
        lease_seconds=1.0,
        clock=clock,
    )
    steps = (DurableStepDefinition("write", "durable.write", "side_effect"),)
    claim = repository.claim(
        idempotency_key="startup-side-effect",
        plan_digest="c" * 64,
        request_id=REQUEST_ID,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        user_id=USER_ID,
        steps=steps,
    )
    assert claim.owner_token is not None
    repository.mark_step_started(
        idempotency_key="startup-side-effect",
        plan_digest="c" * 64,
        owner_token=claim.owner_token,
        step_id="write",
    )
    clock.value += 2.0

    receipt = repository.reconcile_startup()
    replay = repository.claim(
        idempotency_key="startup-side-effect",
        plan_digest="c" * 64,
        request_id=REQUEST_ID,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        user_id=USER_ID,
        steps=steps,
    )

    assert receipt.quarantined_side_effect_runs == 1
    assert replay.kind is DurableClaimKind.REPLAY
    assert replay.owner_token is None
    assert replay.record.state == "failed"
    assert replay.record.steps[0].failure_code == "side_effect_uncertain"


def test_startup_reconciliation_preserves_cancellation_truth(tmp_path: Path) -> None:
    clock = _Clock()
    repository = SqliteOrchestrationRepository(
        tmp_path / "startup-cancel.sqlite3",
        lease_seconds=1.0,
        clock=clock,
    )
    exact = {
        "idempotency_key": "startup-cancel",
        "plan_digest": "d" * 64,
        "request_id": REQUEST_ID,
        "guild_id": GUILD_ID,
        "channel_id": CHANNEL_ID,
        "user_id": USER_ID,
    }
    steps = (DurableStepDefinition("read", "durable.read", "read_only"),)
    claim = repository.claim(**exact, steps=steps)
    assert claim.owner_token is not None
    assert repository.request_cancel(**exact)
    clock.value += 2.0

    receipt = repository.reconcile_startup()
    replay = repository.claim(**exact, steps=steps)

    assert receipt.cancellation_requested_runs == 1
    assert replay.kind is DurableClaimKind.RESUME
    assert replay.owner_token is not None
    assert repository.cancellation_requested(**exact)
    assert (
        repository.mark_step_started(
            idempotency_key="startup-cancel",
            plan_digest="d" * 64,
            owner_token=replay.owner_token,
            step_id="read",
        )
        is False
    )


@pytest.mark.asyncio
async def test_completed_outcome_replays_after_engine_restart_without_reexecution(repository) -> None:
    calls = 0

    async def executor(_context: Any, _parameters: Mapping[str, str]) -> ActionResult:
        nonlocal calls
        calls += 1
        return ActionResult(ActionStatus.COMPLETED, "公開結果")

    spec = _spec("durable.read", executor, effect=ActionEffect.READ_ONLY)
    plan = _plan(OrchestrationStep("read", spec.action_id, effect=ActionEffect.READ_ONLY))
    first, message, request = _environment((spec,), repository)
    first_outcome = await _execute(first, plan, message, request)
    restarted, message, request = _environment((spec,), repository)
    replay = await _execute(restarted, plan, message, request)

    assert calls == 1
    assert replay == first_outcome
    assert replay.public_outputs[0].text == "公開結果"


@pytest.mark.asyncio
async def test_durable_idempotency_rejects_changed_digest_or_scope(repository) -> None:
    async def executor(_context: Any, _parameters: Mapping[str, str]) -> ActionResult:
        return ActionResult(ActionStatus.COMPLETED, "ok")

    spec = _spec("durable.read", executor, effect=ActionEffect.READ_ONLY)
    first_plan = _plan(OrchestrationStep("read", spec.action_id, effect=ActionEffect.READ_ONLY))
    first, message, request = _environment((spec,), repository)
    await _execute(first, first_plan, message, request)

    changed = _plan(
        OrchestrationStep("different", spec.action_id, effect=ActionEffect.READ_ONLY),
    )
    restarted, message, request = _environment((spec,), repository)
    with pytest.raises(PlanIdempotencyConflictError):
        await _execute(restarted, changed, message, request)


@pytest.mark.asyncio
async def test_unexpired_atomic_owner_rejects_a_second_engine(repository) -> None:
    spec = _spec(
        "durable.read",
        lambda _context, _parameters: None,
        effect=ActionEffect.READ_ONLY,
    )
    plan = _plan(OrchestrationStep("read", spec.action_id, effect=ActionEffect.READ_ONLY))
    repository.claim(
        idempotency_key=plan.idempotency_key,
        plan_digest=plan.digest,
        request_id=plan.request_id,
        guild_id=plan.guild_id,
        channel_id=plan.channel_id,
        user_id=plan.user_id,
        steps=(DurableStepDefinition("read", spec.action_id, "read_only"),),
    )
    engine, message, request = _environment((spec,), repository)
    with pytest.raises(PlanExecutionInProgressError):
        await _execute(engine, plan, message, request)


@pytest.mark.asyncio
async def test_stale_read_only_checkpoint_resumes_without_reexecuting_completed_step(tmp_path: Path) -> None:
    clock = _Clock()
    repository = SqliteOrchestrationRepository(
        tmp_path / "resume.sqlite3",
        lease_seconds=1.0,
        clock=clock,
    )
    calls: list[str] = []

    async def first_executor(_context: Any, _parameters: Mapping[str, str]) -> ActionResult:
        calls.append("first")
        return ActionResult(ActionStatus.COMPLETED, "first")

    async def second_executor(_context: Any, _parameters: Mapping[str, str]) -> ActionResult:
        calls.append("second")
        return ActionResult(ActionStatus.COMPLETED, "second")

    first_spec = _spec("durable.first", first_executor, effect=ActionEffect.READ_ONLY)
    second_spec = _spec("durable.second", second_executor, effect=ActionEffect.READ_ONLY)
    plan = _plan(
        OrchestrationStep("first", first_spec.action_id, effect=ActionEffect.READ_ONLY),
        OrchestrationStep(
            "second",
            second_spec.action_id,
            depends_on=("first",),
            effect=ActionEffect.READ_ONLY,
        ),
    )
    claim = repository.claim(
        idempotency_key=plan.idempotency_key,
        plan_digest=plan.digest,
        request_id=plan.request_id,
        guild_id=plan.guild_id,
        channel_id=plan.channel_id,
        user_id=plan.user_id,
        steps=(
            DurableStepDefinition("first", first_spec.action_id, "read_only"),
            DurableStepDefinition("second", second_spec.action_id, "read_only"),
        ),
    )
    assert claim.owner_token is not None
    repository.mark_step_started(
        idempotency_key=plan.idempotency_key,
        plan_digest=plan.digest,
        owner_token=claim.owner_token,
        step_id="first",
    )
    repository.checkpoint_step(
        idempotency_key=plan.idempotency_key,
        plan_digest=plan.digest,
        owner_token=claim.owner_token,
        step_id="first",
        state="completed",
        action_status="completed",
        failure_code=None,
        public_text="first",
    )
    clock.value += 2.0

    engine, message, request = _environment(
        (first_spec, second_spec),
        repository,
        policy=OrchestrationPolicy(total_timeout_seconds=0.5),
    )
    outcome = await _execute(engine, plan, message, request)

    assert calls == ["second"]
    assert outcome.receipt.status is PlanStatus.COMPLETED
    assert [item.text for item in outcome.public_outputs] == ["first", "second"]


@pytest.mark.asyncio
async def test_stale_started_side_effect_is_never_retried(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    repository = SqliteOrchestrationRepository(
        tmp_path / "uncertain.sqlite3",
        lease_seconds=1.0,
        clock=clock,
    )
    calls = 0

    async def executor(_context: Any, _parameters: Mapping[str, str]) -> ActionResult:
        nonlocal calls
        calls += 1
        return ActionResult(ActionStatus.COMPLETED, "done")

    side_effect = _spec("durable.side-effect", executor, effect=ActionEffect.SIDE_EFFECT)
    plan = _plan(OrchestrationStep("write", side_effect.action_id, effect=ActionEffect.SIDE_EFFECT))
    claim = repository.claim(
        idempotency_key=plan.idempotency_key,
        plan_digest=plan.digest,
        request_id=plan.request_id,
        guild_id=plan.guild_id,
        channel_id=plan.channel_id,
        user_id=plan.user_id,
        steps=(DurableStepDefinition("write", side_effect.action_id, "side_effect"),),
    )
    assert claim.owner_token is not None
    repository.mark_step_started(
        idempotency_key=plan.idempotency_key,
        plan_digest=plan.digest,
        owner_token=claim.owner_token,
        step_id="write",
    )
    clock.value += 2.0

    engine, message, request = _environment(
        (side_effect,),
        repository,
        policy=OrchestrationPolicy(total_timeout_seconds=0.5),
    )
    outcome = await _execute(engine, plan, message, request)

    assert calls == 0
    assert outcome.receipt.status is PlanStatus.FAILED
    assert outcome.receipt.steps[0].status is StepStatus.FAILED
    assert outcome.receipt.steps[0].failure_code == "side_effect_uncertain"


@pytest.mark.parametrize("effect", [ActionEffect.READ_ONLY, ActionEffect.SIDE_EFFECT])
@pytest.mark.asyncio
async def test_stale_failed_checkpoint_replays_without_reexecution(
    tmp_path: Path,
    effect: ActionEffect,
) -> None:
    clock = _Clock()
    repository = SqliteOrchestrationRepository(
        tmp_path / f"failed-{effect.value}.sqlite3",
        lease_seconds=1.0,
        clock=clock,
    )
    calls = 0

    async def executor(_context: Any, _parameters: Mapping[str, str]) -> ActionResult:
        nonlocal calls
        calls += 1
        return ActionResult(ActionStatus.COMPLETED, "unreachable")

    failed_spec = _spec(f"durable.failed-{effect.value}", executor, effect=effect)
    pending_spec = _spec("durable.pending", executor, effect=ActionEffect.READ_ONLY)
    plan = _plan(
        OrchestrationStep("failed", failed_spec.action_id, effect=effect),
        OrchestrationStep(
            "pending",
            pending_spec.action_id,
            depends_on=("failed",),
            effect=ActionEffect.READ_ONLY,
        ),
        idempotency_key=f"failed-{effect.value}",
    )
    claim = repository.claim(
        idempotency_key=plan.idempotency_key,
        plan_digest=plan.digest,
        request_id=plan.request_id,
        guild_id=plan.guild_id,
        channel_id=plan.channel_id,
        user_id=plan.user_id,
        steps=(
            DurableStepDefinition("failed", failed_spec.action_id, effect.value),
            DurableStepDefinition("pending", pending_spec.action_id, "read_only"),
        ),
    )
    assert claim.owner_token is not None
    repository.mark_step_started(
        idempotency_key=plan.idempotency_key,
        plan_digest=plan.digest,
        owner_token=claim.owner_token,
        step_id="failed",
    )
    repository.checkpoint_step(
        idempotency_key=plan.idempotency_key,
        plan_digest=plan.digest,
        owner_token=claim.owner_token,
        step_id="failed",
        state="failed",
        action_status="denied",
        failure_code="authorization_denied",
    )
    clock.value += 2.0

    engine, message, request = _environment(
        (failed_spec, pending_spec),
        repository,
        policy=OrchestrationPolicy(total_timeout_seconds=0.5),
    )
    outcome = await _execute(engine, plan, message, request)
    replay = await _execute(engine, plan, message, request)

    assert calls == 0
    assert replay == outcome
    assert outcome.receipt.status is PlanStatus.FAILED
    assert outcome.receipt.steps[0].status is StepStatus.FAILED
    assert outcome.receipt.steps[0].action_status is ActionStatus.DENIED
    assert outcome.receipt.steps[0].failure_code == "authorization_denied"
    assert outcome.receipt.steps[1].status is StepStatus.NOT_RUN
    assert outcome.receipt.steps[1].failure_code == "not_started"


@pytest.mark.asyncio
async def test_engine_cancellation_quarantines_started_side_effect(repository) -> None:
    started = asyncio.Event()
    calls = 0

    async def executor(_context: Any, _parameters: Mapping[str, str]) -> ActionResult:
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    spec = _spec("durable.cancel-side", executor, effect=ActionEffect.SIDE_EFFECT)
    plan = _plan(OrchestrationStep("write", spec.action_id, effect=ActionEffect.SIDE_EFFECT))
    engine, message, request = _environment((spec,), repository)
    caller = asyncio.create_task(_execute(engine, plan, message, request))
    await asyncio.wait_for(started.wait(), timeout=1)
    internal = engine._running[plan.idempotency_key][1]
    internal.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    replay = await _execute(engine, plan, message, request)
    assert calls == 1
    assert replay.receipt.status is PlanStatus.FAILED
    assert replay.receipt.steps[0].failure_code == "side_effect_uncertain"


@pytest.mark.asyncio
async def test_cancel_abandon_releases_read_only_but_quarantines_side_effect(tmp_path: Path) -> None:
    repository = SqliteOrchestrationRepository(tmp_path / "cancel.sqlite3")
    read_claim = repository.claim(
        idempotency_key="cancel-read",
        plan_digest="a" * 64,
        request_id=REQUEST_ID,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        user_id=USER_ID,
        steps=(DurableStepDefinition("read", "durable.read", "read_only"),),
    )
    assert read_claim.owner_token is not None
    repository.abandon(
        idempotency_key="cancel-read",
        plan_digest="a" * 64,
        owner_token=read_claim.owner_token,
    )
    resumed = repository.claim(
        idempotency_key="cancel-read",
        plan_digest="a" * 64,
        request_id=REQUEST_ID,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        user_id=USER_ID,
        steps=(DurableStepDefinition("read", "durable.read", "read_only"),),
    )
    assert resumed.kind is DurableClaimKind.RESUME

    side_claim = repository.claim(
        idempotency_key="cancel-side",
        plan_digest="b" * 64,
        request_id=REQUEST_ID,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        user_id=USER_ID,
        steps=(DurableStepDefinition("write", "durable.write", "side_effect"),),
    )
    assert side_claim.owner_token is not None
    repository.abandon(
        idempotency_key="cancel-side",
        plan_digest="b" * 64,
        owner_token=side_claim.owner_token,
    )
    quarantined = repository.claim(
        idempotency_key="cancel-side",
        plan_digest="b" * 64,
        request_id=REQUEST_ID,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        user_id=USER_ID,
        steps=(DurableStepDefinition("write", "durable.write", "side_effect"),),
    )
    assert quarantined.kind is DurableClaimKind.REPLAY
    assert quarantined.record.state == "failed"
    assert quarantined.record.steps[0].failure_code == "restart_unsafe"


@pytest.mark.asyncio
async def test_artifact_metadata_replays_without_raw_bytes_or_paths(repository) -> None:
    calls = 0
    artifact = _artifact()

    async def executor(_context: Any, _parameters: Mapping[str, str]) -> ActionResult:
        nonlocal calls
        calls += 1
        return ActionResult(ActionStatus.COMPLETED, "not persisted", artifact=artifact)

    spec = _spec("durable.artifact", executor, effect=ActionEffect.READ_ONLY, artifact=True)
    plan = _plan(OrchestrationStep("artifact", spec.action_id, effect=ActionEffect.READ_ONLY))
    first, message, request = _environment((spec,), repository)
    await _execute(first, plan, message, request)
    restarted, message, request = _environment((spec,), repository)
    replay = await _execute(restarted, plan, message, request)

    assert calls == 1
    assert replay.artifact_outputs[0].artifact == artifact
    assert replay.public_outputs == ()
    database_bytes = repository.path.read_bytes()
    assert b"not persisted" not in database_bytes
    assert b"raw bytes" not in database_bytes


@pytest.mark.asyncio
async def test_partial_read_only_artifact_checkpoint_survives_safe_resume(tmp_path: Path) -> None:
    clock = _Clock()
    repository = SqliteOrchestrationRepository(
        tmp_path / "partial-artifact.sqlite3",
        lease_seconds=1.0,
        clock=clock,
    )
    artifact = _artifact("d")
    producer_calls = 0
    consumer_calls = 0

    async def producer(_context: Any, _parameters: Mapping[str, str]) -> ActionResult:
        nonlocal producer_calls
        producer_calls += 1
        return ActionResult(ActionStatus.COMPLETED, "hidden", artifact=artifact)

    async def consumer(_context: Any, _parameters: Mapping[str, str]) -> ActionResult:
        nonlocal consumer_calls
        consumer_calls += 1
        return ActionResult(ActionStatus.COMPLETED, "continued")

    producer_spec = _spec(
        "durable.producer",
        producer,
        effect=ActionEffect.READ_ONLY,
        artifact=True,
    )
    consumer_spec = _spec("durable.consumer", consumer, effect=ActionEffect.READ_ONLY)
    plan = _plan(
        OrchestrationStep("producer", producer_spec.action_id, effect=ActionEffect.READ_ONLY),
        OrchestrationStep(
            "consumer",
            consumer_spec.action_id,
            depends_on=("producer",),
            effect=ActionEffect.READ_ONLY,
        ),
        idempotency_key="partial-artifact",
    )
    claim = repository.claim(
        idempotency_key=plan.idempotency_key,
        plan_digest=plan.digest,
        request_id=plan.request_id,
        guild_id=plan.guild_id,
        channel_id=plan.channel_id,
        user_id=plan.user_id,
        steps=(
            DurableStepDefinition("producer", producer_spec.action_id, "read_only"),
            DurableStepDefinition("consumer", consumer_spec.action_id, "read_only"),
        ),
    )
    assert claim.owner_token is not None
    repository.mark_step_started(
        idempotency_key=plan.idempotency_key,
        plan_digest=plan.digest,
        owner_token=claim.owner_token,
        step_id="producer",
    )
    repository.checkpoint_step(
        idempotency_key=plan.idempotency_key,
        plan_digest=plan.digest,
        owner_token=claim.owner_token,
        step_id="producer",
        state="completed",
        action_status="completed",
        failure_code=None,
        artifact=artifact,
    )
    clock.value += 2.0

    engine, message, request = _environment(
        (producer_spec, consumer_spec),
        repository,
        policy=OrchestrationPolicy(total_timeout_seconds=0.5),
    )
    outcome = await _execute(engine, plan, message, request)
    assert producer_calls == 0
    assert consumer_calls == 1
    assert outcome.artifact_outputs[0].artifact == artifact
    assert outcome.public_outputs[0].text == "continued"


def test_repository_bounds_public_output_and_has_no_prompt_parameter_columns(repository) -> None:
    with sqlite3.connect(repository.path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(ai_orchestration_steps)")}
    assert "prompt" not in columns
    assert "parameters" not in columns
    assert "raw_bytes" not in columns

    claim = repository.claim(
        idempotency_key="bounded",
        plan_digest="c" * 64,
        request_id=REQUEST_ID,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        user_id=USER_ID,
        steps=(DurableStepDefinition("read", "durable.read", "read_only"),),
    )
    assert claim.owner_token is not None
    with pytest.raises(OrchestrationRepositoryError, match="public output"):
        repository.checkpoint_step(
            idempotency_key="bounded",
            plan_digest="c" * 64,
            owner_token=claim.owner_token,
            step_id="read",
            state="completed",
            action_status="completed",
            failure_code=None,
            public_text="x" * 1_901,
        )
    with pytest.raises(OrchestrationRepositoryError, match="secret-like"):
        repository.checkpoint_step(
            idempotency_key="bounded",
            plan_digest="c" * 64,
            owner_token=claim.owner_token,
            step_id="read",
            state="completed",
            action_status="completed",
            failure_code=None,
            public_text="api_key=sk-test-secret-value",
        )


def test_repository_prunes_only_terminal_rows_at_the_configured_bound(tmp_path: Path) -> None:
    repository = SqliteOrchestrationRepository(tmp_path / "bounded.sqlite3", max_runs=1)
    claim = repository.claim(
        idempotency_key="first",
        plan_digest="d" * 64,
        request_id=REQUEST_ID,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        user_id=USER_ID,
        steps=(DurableStepDefinition("read", "durable.read", "read_only"),),
    )
    assert claim.owner_token is not None
    repository.complete(
        idempotency_key="first",
        plan_digest="d" * 64,
        owner_token=claim.owner_token,
        plan_status="failed",
        steps=(
            DurableStepRecord(
                "read",
                "durable.read",
                "read_only",
                "failed",
                failure_code="test",
            ),
        ),
    )
    second = repository.claim(
        idempotency_key="second",
        plan_digest="e" * 64,
        request_id=REQUEST_ID,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        user_id=USER_ID,
        steps=(DurableStepDefinition("read", "durable.read", "read_only"),),
    )
    assert second.kind is DurableClaimKind.NEW
    with sqlite3.connect(repository.path) as connection:
        keys = [row[0] for row in connection.execute("SELECT idempotency_key FROM ai_orchestration_runs")]
    assert keys == ["second"]


def test_repository_never_prunes_terminal_side_effect_idempotency(tmp_path: Path) -> None:
    repository = SqliteOrchestrationRepository(tmp_path / "side-effect-bound.sqlite3", max_runs=1)
    claim = repository.claim(
        idempotency_key="side-effect",
        plan_digest="f" * 64,
        request_id=REQUEST_ID,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        user_id=USER_ID,
        steps=(DurableStepDefinition("write", "durable.write", "side_effect"),),
    )
    assert claim.owner_token is not None
    repository.complete(
        idempotency_key="side-effect",
        plan_digest="f" * 64,
        owner_token=claim.owner_token,
        plan_status="completed",
        steps=(
            DurableStepRecord(
                "write",
                "durable.write",
                "side_effect",
                "completed",
                action_status="completed",
            ),
        ),
    )

    with pytest.raises(OrchestrationRepositoryError, match="capacity"):
        repository.claim(
            idempotency_key="other",
            plan_digest="0" * 64,
            request_id=REQUEST_ID,
            guild_id=GUILD_ID,
            channel_id=CHANNEL_ID,
            user_id=USER_ID,
            steps=(DurableStepDefinition("read", "durable.read", "read_only"),),
        )
    replay = repository.claim(
        idempotency_key="side-effect",
        plan_digest="f" * 64,
        request_id=REQUEST_ID,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        user_id=USER_ID,
        steps=(DurableStepDefinition("write", "durable.write", "side_effect"),),
    )
    assert replay.kind is DurableClaimKind.REPLAY


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("idempotency_key", "x" * 129),
        ("plan_digest", "A" * 64),
        ("guild_id", True),
        ("steps", (DurableStepDefinition("x" * 129, "durable.read", "read_only"),)),
    ],
)
def test_direct_claim_rejects_unbounded_or_invalid_metadata_before_insert(
    repository,
    override: str,
    value: Any,
) -> None:
    kwargs: dict[str, Any] = {
        "idempotency_key": "direct-invalid",
        "plan_digest": "f" * 64,
        "request_id": REQUEST_ID,
        "guild_id": GUILD_ID,
        "channel_id": CHANNEL_ID,
        "user_id": USER_ID,
        "steps": (DurableStepDefinition("read", "durable.read", "read_only"),),
    }
    kwargs[override] = value
    with pytest.raises(OrchestrationRepositoryError):
        repository.claim(**kwargs)
    with sqlite3.connect(repository.path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM ai_orchestration_runs").fetchone()[0]
    assert count == 0


def test_cancellation_request_is_scope_bound_idempotent_and_persistent(tmp_path: Path) -> None:
    path = tmp_path / "cancel-request.sqlite3"
    repository = SqliteOrchestrationRepository(path)
    claim = repository.claim(
        idempotency_key="cancel-request",
        plan_digest="1" * 64,
        request_id=REQUEST_ID,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        user_id=USER_ID,
        steps=(DurableStepDefinition("read", "durable.read", "read_only"),),
    )
    assert claim.owner_token is not None

    assert not repository.request_cancel(
        idempotency_key="cancel-request",
        plan_digest="2" * 64,
        request_id=REQUEST_ID,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        user_id=USER_ID,
    )
    assert not repository.request_cancel(
        idempotency_key="cancel-request",
        plan_digest="1" * 64,
        request_id=REQUEST_ID,
        guild_id=GUILD_ID + 1,
        channel_id=CHANNEL_ID,
        user_id=USER_ID,
    )
    exact = {
        "idempotency_key": "cancel-request",
        "plan_digest": "1" * 64,
        "request_id": REQUEST_ID,
        "guild_id": GUILD_ID,
        "channel_id": CHANNEL_ID,
        "user_id": USER_ID,
    }
    assert repository.request_cancel(**exact)
    assert repository.request_cancel(**exact)
    assert repository.cancellation_requested(**exact)
    assert SqliteOrchestrationRepository(path).cancellation_requested(**exact)
    repository.complete(
        idempotency_key="cancel-request",
        plan_digest="1" * 64,
        owner_token=claim.owner_token,
        plan_status="failed",
        steps=(
            DurableStepRecord(
                "read",
                "durable.read",
                "read_only",
                "failed",
                failure_code="cancelled",
            ),
        ),
    )
    assert not repository.request_cancel(**exact)
    assert not repository.cancellation_requested(**exact)

    with sqlite3.connect(path) as connection:
        run_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(ai_orchestration_runs)")}
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    assert run_columns >= {"cancel_requested_at"}
    assert run_columns.isdisjoint({"prompt", "parameters", "secret"})
    assert schema_version == 2


def test_repository_migrates_existing_run_table_to_cancellation_schema(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE ai_orchestration_runs (
                idempotency_key TEXT PRIMARY KEY,
                plan_digest TEXT NOT NULL,
                request_id TEXT NOT NULL,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                state TEXT NOT NULL,
                plan_status TEXT,
                owner_token TEXT,
                lease_until REAL,
                updated_at REAL NOT NULL
            );
            PRAGMA user_version = 1;
            """
        )

    SqliteOrchestrationRepository(path)

    with sqlite3.connect(path) as connection:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(ai_orchestration_runs)")}
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    assert "cancel_requested_at" in columns
    assert version == 2


@pytest.mark.asyncio
async def test_cancel_before_first_step_fails_once_and_replays_without_execution(repository) -> None:
    calls = 0
    plan_started = asyncio.Event()
    release = asyncio.Event()

    async def executor(_context: Any, _parameters: Mapping[str, str]) -> ActionResult:
        nonlocal calls
        calls += 1
        return ActionResult(ActionStatus.COMPLETED, "unreachable")

    async def observer(event: Any) -> None:
        if event.event_type is PlanEventType.PLAN_STARTED:
            plan_started.set()
            await release.wait()

    first = _spec("durable.cancel-first", executor, effect=ActionEffect.READ_ONLY)
    second = _spec("durable.cancel-second", executor, effect=ActionEffect.READ_ONLY)
    plan = _plan(
        OrchestrationStep("first", first.action_id, effect=ActionEffect.READ_ONLY),
        OrchestrationStep(
            "second",
            second.action_id,
            depends_on=("first",),
            effect=ActionEffect.READ_ONLY,
        ),
        idempotency_key="cancel-before-start",
    )
    engine, message, request = _environment((first, second), repository)
    engine.observer = observer
    running = asyncio.create_task(_execute(engine, plan, message, request))
    await asyncio.wait_for(plan_started.wait(), timeout=1)
    assert repository.request_cancel(
        idempotency_key=plan.idempotency_key,
        plan_digest=plan.digest,
        request_id=plan.request_id,
        guild_id=plan.guild_id,
        channel_id=plan.channel_id,
        user_id=plan.user_id,
    )
    release.set()
    outcome = await running

    restarted, message, request = _environment((first, second), SqliteOrchestrationRepository(repository.path))
    replay = await _execute(restarted, plan, message, request)
    assert calls == 0
    assert replay == outcome
    assert outcome.receipt.status is PlanStatus.FAILED
    assert outcome.receipt.steps[0].status is StepStatus.FAILED
    assert outcome.receipt.steps[0].failure_code == "cancelled"
    assert outcome.receipt.steps[1].status is StepStatus.NOT_RUN


@pytest.mark.asyncio
async def test_cancel_accepted_while_durable_step_start_waits_prevents_executor(
    repository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    mark_entered = threading.Event()
    release_mark = threading.Event()
    original_mark = repository.mark_step_started

    def blocked_mark(**kwargs: Any) -> bool:
        mark_entered.set()
        if not release_mark.wait(timeout=2):
            raise TimeoutError("test did not release durable step start")
        return original_mark(**kwargs)

    async def executor(_context: Any, _parameters: Mapping[str, str]) -> ActionResult:
        nonlocal calls
        calls += 1
        return ActionResult(ActionStatus.COMPLETED, "must not execute")

    monkeypatch.setattr(repository, "mark_step_started", blocked_mark)
    spec = _spec("durable.cancel-at-start", executor, effect=ActionEffect.SIDE_EFFECT)
    plan = _plan(
        OrchestrationStep("write", spec.action_id, effect=ActionEffect.SIDE_EFFECT),
        idempotency_key="cancel-at-durable-start",
    )
    engine, message, request = _environment((spec,), repository)
    running = asyncio.create_task(_execute(engine, plan, message, request))
    assert await asyncio.to_thread(mark_entered.wait, 1)
    assert await asyncio.to_thread(
        repository.request_cancel,
        idempotency_key=plan.idempotency_key,
        plan_digest=plan.digest,
        request_id=plan.request_id,
        guild_id=plan.guild_id,
        channel_id=plan.channel_id,
        user_id=plan.user_id,
    )
    release_mark.set()
    outcome = await running

    assert calls == 0
    assert outcome.receipt.status is PlanStatus.FAILED
    assert outcome.receipt.steps[0].status is StepStatus.FAILED
    assert outcome.receipt.steps[0].failure_code == "cancelled"


@pytest.mark.asyncio
async def test_cancel_requested_after_first_retryable_failure_prevents_retry(repository) -> None:
    calls = 0
    plan: OrchestrationPlan

    async def executor(_context: Any, _parameters: Mapping[str, str]) -> ActionResult:
        nonlocal calls
        calls += 1
        assert repository.request_cancel(
            idempotency_key=plan.idempotency_key,
            plan_digest=plan.digest,
            request_id=plan.request_id,
            guild_id=plan.guild_id,
            channel_id=plan.channel_id,
            user_id=plan.user_id,
        )
        raise RuntimeError("retry must observe cancellation")

    spec = _spec(
        "durable.retry-cancel",
        executor,
        effect=ActionEffect.READ_ONLY,
        retry_safe=True,
    )
    plan = _plan(
        OrchestrationStep("read", spec.action_id, effect=ActionEffect.READ_ONLY),
        idempotency_key="cancel-before-retry",
    )
    engine, message, request = _environment((spec,), repository)
    outcome = await _execute(engine, plan, message, request)

    assert calls == 1
    assert outcome.receipt.status is PlanStatus.FAILED
    assert outcome.receipt.steps[0].failure_code == "cancelled"


@pytest.mark.asyncio
async def test_parallel_read_only_results_remain_completed_when_cancellation_arrives_in_flight(repository) -> None:
    calls: list[str] = []
    both_started = asyncio.Event()
    cancellation_written = asyncio.Event()
    plan: OrchestrationPlan

    async def first(_context: Any, _parameters: Mapping[str, str]) -> ActionResult:
        calls.append("first")
        if len(calls) == 2:
            both_started.set()
        await both_started.wait()
        assert await asyncio.to_thread(
            repository.request_cancel,
            idempotency_key=plan.idempotency_key,
            plan_digest=plan.digest,
            request_id=plan.request_id,
            guild_id=plan.guild_id,
            channel_id=plan.channel_id,
            user_id=plan.user_id,
        )
        cancellation_written.set()
        return ActionResult(ActionStatus.COMPLETED, "first completed")

    async def second(_context: Any, _parameters: Mapping[str, str]) -> ActionResult:
        calls.append("second")
        if len(calls) == 2:
            both_started.set()
        await both_started.wait()
        await cancellation_written.wait()
        return ActionResult(ActionStatus.COMPLETED, "second completed")

    first_spec = _spec("durable.parallel-first", first, effect=ActionEffect.READ_ONLY)
    second_spec = _spec("durable.parallel-second", second, effect=ActionEffect.READ_ONLY)
    plan = _plan(
        OrchestrationStep("first", first_spec.action_id, effect=ActionEffect.READ_ONLY),
        OrchestrationStep("second", second_spec.action_id, effect=ActionEffect.READ_ONLY),
        idempotency_key="cancel-parallel-in-flight",
    )
    engine, message, request = _environment((first_spec, second_spec), repository)
    outcome = await _execute(engine, plan, message, request)

    assert sorted(calls) == ["first", "second"]
    assert outcome.receipt.status is PlanStatus.FAILED
    assert [step.status for step in outcome.receipt.steps] == [
        StepStatus.COMPLETED,
        StepStatus.COMPLETED,
    ]
    assert [step.action_status for step in outcome.receipt.steps] == [
        ActionStatus.COMPLETED,
        ActionStatus.COMPLETED,
    ]


@pytest.mark.asyncio
async def test_in_flight_side_effect_completion_is_truthful_then_next_step_cancels(repository) -> None:
    side_effect_calls = 0
    read_calls = 0
    plan: OrchestrationPlan

    async def side_effect(_context: Any, _parameters: Mapping[str, str]) -> ActionResult:
        nonlocal side_effect_calls
        side_effect_calls += 1
        assert repository.request_cancel(
            idempotency_key=plan.idempotency_key,
            plan_digest=plan.digest,
            request_id=plan.request_id,
            guild_id=plan.guild_id,
            channel_id=plan.channel_id,
            user_id=plan.user_id,
        )
        return ActionResult(ActionStatus.COMPLETED, "already committed")

    async def read(_context: Any, _parameters: Mapping[str, str]) -> ActionResult:
        nonlocal read_calls
        read_calls += 1
        return ActionResult(ActionStatus.COMPLETED, "unreachable")

    write_spec = _spec("durable.write-then-cancel", side_effect, effect=ActionEffect.SIDE_EFFECT)
    read_spec = _spec("durable.read-after-cancel", read, effect=ActionEffect.READ_ONLY)
    plan = _plan(
        OrchestrationStep("write", write_spec.action_id, effect=ActionEffect.SIDE_EFFECT),
        OrchestrationStep(
            "read",
            read_spec.action_id,
            depends_on=("write",),
            effect=ActionEffect.READ_ONLY,
        ),
        idempotency_key="cancel-after-side-effect",
    )
    engine, message, request = _environment((write_spec, read_spec), repository)
    outcome = await _execute(engine, plan, message, request)

    assert side_effect_calls == 1
    assert read_calls == 0
    assert outcome.receipt.status is PlanStatus.FAILED
    assert outcome.receipt.steps[0].status is StepStatus.COMPLETED
    assert outcome.receipt.steps[1].status is StepStatus.FAILED
    assert outcome.receipt.steps[1].failure_code == "cancelled"
