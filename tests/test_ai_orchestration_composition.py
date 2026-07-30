from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yonerai_discord.capabilities import COMMAND_CAPABILITIES, EVENT_CAPABILITIES
from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.modules.ai.action_router import (
    ActionEffect,
    ActionRegistry,
    ActionResult,
    ActionSpec,
    ActionStatus,
    NaturalActionRouter,
)
from yonerai_discord.modules.ai.models import AIRequest
from yonerai_discord.modules.ai.orchestration import (
    OrchestrationPlan,
    OrchestrationPolicy,
    OrchestrationStep,
    PlanStatus,
)
from yonerai_discord.modules.ai.orchestration_composition import (
    DurableOrchestrationConfiguration,
    DurableOrchestrationConsumer,
    DurableOrchestrationRuntime,
    OrchestrationCompositionError,
    compose_durable_orchestration,
)
from yonerai_discord.modules.ai.orchestration_repository import (
    OrchestrationRepositoryError,
    StartupReconciliationStatus,
)


GUILD_ID = 101
CHANNEL_ID = 202
USER_ID = 303
REQUEST_ID = "request-404"


class _Guard:
    def __init__(self, registry: Any) -> None:
        self.registry = registry

    async def evaluate_fresh_member(self, capability_id: str, *, guild: Any, member: Any) -> Any:
        assert capability_id in {
            COMMAND_CAPABILITIES["tools dice"],
            EVENT_CAPABILITIES["ai_mention_message"],
        }
        assert guild.id == GUILD_ID
        assert member.id == USER_ID
        return SimpleNamespace(allowed=True, actor_level=RbacLevel.BOT_OWNER)

    def currently_allowed(self, *_: Any, **__: Any) -> bool:
        return True


def _environment(executor: Any) -> tuple[NaturalActionRouter, Any, AIRequest]:
    capability_id = COMMAND_CAPABILITIES["tools dice"]
    mention_capability_id = EVENT_CAPABILITIES["ai_mention_message"]
    capability_ids = frozenset((capability_id, mention_capability_id))
    capability_registry = SimpleNamespace(
        capability_status=lambda candidate, _guild_id=None: SimpleNamespace(executable=candidate in capability_ids),
        runtime_available=lambda candidate: candidate in capability_ids,
    )
    guard = _Guard(capability_registry)
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
    bot = SimpleNamespace(
        is_closing=False,
        capability_guard=guard,
        capability_registry=capability_registry,
        runtime_capability_readiness=dict.fromkeys(capability_ids, True),
    )
    spec = ActionSpec(
        "composition.read",
        "tools dice",
        capability_id,
        RbacLevel.EVERYONE,
        lambda _: None,
        executor,
        effect=ActionEffect.READ_ONLY,
    )
    return (
        NaturalActionRouter(bot, registry=ActionRegistry((spec,))),
        message,
        AIRequest("plan", GUILD_ID, USER_ID, CHANNEL_ID),
    )


def _configuration(path: Path, **changes: Any) -> DurableOrchestrationConfiguration:
    values = {
        "enabled": True,
        "repository_path": path,
        "lease_seconds": 2.0,
        "max_runs": 32,
    }
    values.update(changes)
    return DurableOrchestrationConfiguration(**values)


def _plan() -> OrchestrationPlan:
    return OrchestrationPlan(
        REQUEST_ID,
        GUILD_ID,
        CHANNEL_ID,
        USER_ID,
        "composition-idempotency",
        (
            OrchestrationStep(
                "read",
                "composition.read",
                effect=ActionEffect.READ_ONLY,
            ),
        ),
    )


async def _execute(runtime: DurableOrchestrationRuntime, message: Any, request: AIRequest):
    return await runtime.engine.execute_outcome(
        _plan(),
        message=message,
        request=request,
        request_id=REQUEST_ID,
    )


def test_explicit_off_returns_none_without_touching_the_path(tmp_path: Path) -> None:
    async def executor(_context: Any, _parameters: Any) -> ActionResult:
        raise AssertionError("executor must not run")

    router, _, _ = _environment(executor)
    path = tmp_path / "disabled" / "orchestration.sqlite3"
    configuration = DurableOrchestrationConfiguration(
        enabled=False,
        repository_path=path,
    )

    assert compose_durable_orchestration(router, configuration) is None
    assert not path.parent.exists()
    assert str(path) not in repr(configuration)


def test_enabled_configuration_requires_a_typed_path() -> None:
    with pytest.raises(ValueError, match="repository_path is required"):
        DurableOrchestrationConfiguration(enabled=True)
    with pytest.raises(TypeError, match="repository_path must be a Path"):
        DurableOrchestrationConfiguration(enabled=True, repository_path="state.sqlite3")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="repository_path must be absolute"):
        DurableOrchestrationConfiguration(enabled=True, repository_path=Path("state.sqlite3"))


def test_lease_must_exceed_total_timeout_before_database_creation(tmp_path: Path) -> None:
    async def executor(_context: Any, _parameters: Any) -> ActionResult:
        raise AssertionError("executor must not run")

    router, _, _ = _environment(executor)
    path = tmp_path / "short-lease" / "orchestration.sqlite3"

    with pytest.raises(ValueError, match="lease must exceed"):
        compose_durable_orchestration(
            router,
            _configuration(path, lease_seconds=1.0),
            policy=OrchestrationPolicy(total_timeout_seconds=1.0),
        )
    assert not path.parent.exists()


async def test_factory_and_consumer_publish_one_shared_identity(tmp_path: Path) -> None:
    async def executor(_context: Any, _parameters: Any) -> ActionResult:
        return ActionResult(ActionStatus.COMPLETED, "ok")

    router, _, _ = _environment(executor)
    runtime = compose_durable_orchestration(
        router,
        _configuration(tmp_path / "state.sqlite3"),
        policy=OrchestrationPolicy(total_timeout_seconds=1.0),
    )
    assert runtime is not None
    assert runtime.engine.repository is runtime.repository
    assert runtime.ready is False

    owner = SimpleNamespace(is_closing=False)
    consumer = DurableOrchestrationConsumer(owner)
    await consumer.start(runtime)

    assert consumer.ready is True
    assert consumer.engine is runtime.engine
    assert owner.ai_orchestration_runtime is runtime
    assert owner.ai_orchestration_engine is runtime.engine
    assert owner.ai_orchestration_repository is runtime.repository
    assert owner.ai_orchestration_startup_reconciliation is runtime.startup_reconciliation
    assert consumer.startup_reconciliation is runtime.startup_reconciliation
    assert runtime.startup_reconciliation is not None
    assert runtime.startup_reconciliation.status is StartupReconciliationStatus.COMPLETED

    await consumer.stop()
    assert consumer.ready is False
    assert runtime.closing is True
    assert runtime.closed is True
    assert not hasattr(owner, "ai_orchestration_runtime")
    assert not hasattr(owner, "ai_orchestration_engine")
    assert not hasattr(owner, "ai_orchestration_repository")
    assert not hasattr(owner, "ai_orchestration_startup_reconciliation")
    await consumer.stop()


async def test_startup_reconciliation_failure_publishes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def executor(_context: Any, _parameters: Any) -> ActionResult:
        return ActionResult(ActionStatus.COMPLETED, "must not run")

    router, _, _ = _environment(executor)
    runtime = compose_durable_orchestration(
        router,
        _configuration(tmp_path / "startup-failure.sqlite3"),
        policy=OrchestrationPolicy(total_timeout_seconds=1.0),
    )
    assert runtime is not None
    owner = SimpleNamespace(is_closing=False)
    consumer = DurableOrchestrationConsumer(owner)

    def fail_reconciliation() -> None:
        raise OrchestrationRepositoryError("startup reconciliation failed")

    monkeypatch.setattr(runtime.repository, "reconcile_startup", fail_reconciliation)
    with pytest.raises(OrchestrationRepositoryError, match="startup reconciliation failed"):
        await consumer.start(runtime)

    assert vars(owner) == {"is_closing": False}
    assert consumer.runtime is None
    assert consumer.startup_reconciliation is None
    assert consumer.ready is False
    assert runtime.ready is False
    assert runtime.startup_reconciliation is None
    await runtime.close()


async def test_cancelled_start_waits_for_reconciliation_worker_and_publishes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def executor(_context: Any, _parameters: Any) -> ActionResult:
        return ActionResult(ActionStatus.COMPLETED, "must not run")

    router, _, _ = _environment(executor)
    runtime = compose_durable_orchestration(
        router,
        _configuration(tmp_path / "startup-cancel.sqlite3"),
        policy=OrchestrationPolicy(total_timeout_seconds=1.0),
    )
    assert runtime is not None
    owner = SimpleNamespace(is_closing=False)
    consumer = DurableOrchestrationConsumer(owner)
    worker_started = threading.Event()
    worker_release = threading.Event()
    worker_finished = threading.Event()
    original_reconciliation = runtime.repository.reconcile_startup
    reconciliation_calls = 0

    def delayed_reconciliation():
        nonlocal reconciliation_calls
        reconciliation_calls += 1
        if reconciliation_calls == 1:
            worker_started.set()
            if not worker_release.wait(timeout=5.0):
                raise TimeoutError("startup reconciliation test worker timed out")
            try:
                return original_reconciliation()
            finally:
                worker_finished.set()
        return original_reconciliation()

    monkeypatch.setattr(runtime.repository, "reconcile_startup", delayed_reconciliation)
    starting = asyncio.create_task(consumer.start(runtime))
    assert await asyncio.to_thread(worker_started.wait, 2.0)
    starting.cancel()
    try:
        await asyncio.sleep(0)
        assert starting.done() is False
        assert vars(owner) == {"is_closing": False}
        assert consumer.runtime is None
        assert runtime.ready is False
        assert runtime.startup_reconciliation is None
    finally:
        worker_release.set()

    with pytest.raises(asyncio.CancelledError):
        await starting
    assert worker_finished.is_set()
    assert vars(owner) == {"is_closing": False}
    assert consumer.runtime is None
    assert runtime.startup_reconciliation is None

    await consumer.start(runtime)
    assert reconciliation_calls == 2
    assert consumer.ready is True
    await consumer.stop()


async def test_begin_close_withdraws_readiness_before_stop(tmp_path: Path) -> None:
    async def executor(_context: Any, _parameters: Any) -> ActionResult:
        return ActionResult(ActionStatus.COMPLETED, "ok")

    router, _, _ = _environment(executor)
    runtime = compose_durable_orchestration(
        router,
        _configuration(tmp_path / "state.sqlite3"),
        policy=OrchestrationPolicy(total_timeout_seconds=1.0),
    )
    assert runtime is not None
    consumer = DurableOrchestrationConsumer(SimpleNamespace(is_closing=False))
    await consumer.start(runtime)

    await consumer.begin_close()

    assert runtime.closing is True
    assert runtime.ready is False
    assert consumer.ready is False
    await consumer.stop()


async def test_stale_engine_reference_is_rejected_after_runtime_stop(tmp_path: Path) -> None:
    calls = 0

    async def executor(_context: Any, _parameters: Any) -> ActionResult:
        nonlocal calls
        calls += 1
        return ActionResult(ActionStatus.COMPLETED, "ok")

    router, message, request = _environment(executor)
    runtime = compose_durable_orchestration(
        router,
        _configuration(tmp_path / "stale.sqlite3"),
        policy=OrchestrationPolicy(total_timeout_seconds=1.0),
    )
    assert runtime is not None
    consumer = DurableOrchestrationConsumer(SimpleNamespace(is_closing=False))
    await consumer.start(runtime)
    stale_engine = runtime.engine

    await consumer.stop()

    with pytest.raises(OrchestrationCompositionError, match="runtime is closing"):
        await stale_engine.execute_outcome(
            _plan(),
            message=message,
            request=request,
            request_id=REQUEST_ID,
        )
    assert calls == 0


async def test_stop_waits_for_in_flight_execution_and_denies_new_work(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def executor(_context: Any, _parameters: Any) -> ActionResult:
        started.set()
        await release.wait()
        return ActionResult(ActionStatus.COMPLETED, "ok")

    router, message, request = _environment(executor)
    runtime = compose_durable_orchestration(
        router,
        _configuration(tmp_path / "in-flight.sqlite3"),
        policy=OrchestrationPolicy(total_timeout_seconds=1.0),
    )
    assert runtime is not None
    consumer = DurableOrchestrationConsumer(SimpleNamespace(is_closing=False))
    await consumer.start(runtime)
    stale_engine = runtime.engine
    running = asyncio.create_task(_execute(runtime, message, request))
    await started.wait()

    stopping = asyncio.create_task(consumer.stop())
    await asyncio.sleep(0)
    assert stopping.done() is False
    with pytest.raises(OrchestrationCompositionError, match="runtime is closing"):
        await stale_engine.execute_outcome(
            _plan(),
            message=message,
            request=request,
            request_id=REQUEST_ID,
        )

    release.set()
    assert (await running).receipt.status is PlanStatus.COMPLETED
    await stopping
    assert runtime.closed is True


async def test_cancelled_stop_retains_runtime_for_cleanup_retry(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def executor(_context: Any, _parameters: Any) -> ActionResult:
        started.set()
        await release.wait()
        return ActionResult(ActionStatus.COMPLETED, "ok")

    router, message, request = _environment(executor)
    runtime = compose_durable_orchestration(
        router,
        _configuration(tmp_path / "cancel-retry.sqlite3"),
        policy=OrchestrationPolicy(total_timeout_seconds=1.0),
    )
    assert runtime is not None
    consumer = DurableOrchestrationConsumer(SimpleNamespace(is_closing=False))
    await consumer.start(runtime)
    running = asyncio.create_task(_execute(runtime, message, request))
    await started.wait()
    stopping = asyncio.create_task(consumer.stop())
    await asyncio.sleep(0)

    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert consumer.runtime is runtime
    assert runtime.closed is False
    assert consumer.ready is False

    release.set()
    assert (await running).receipt.status is PlanStatus.COMPLETED
    await consumer.stop()
    assert consumer.runtime is None
    assert runtime.closed is True


async def test_start_preserves_foreign_attributes_and_closing_owner(tmp_path: Path) -> None:
    async def executor(_context: Any, _parameters: Any) -> ActionResult:
        return ActionResult(ActionStatus.COMPLETED, "ok")

    router, _, _ = _environment(executor)
    first = compose_durable_orchestration(
        router,
        _configuration(tmp_path / "foreign.sqlite3"),
        policy=OrchestrationPolicy(total_timeout_seconds=1.0),
    )
    second = compose_durable_orchestration(
        router,
        _configuration(tmp_path / "closing.sqlite3"),
        policy=OrchestrationPolicy(total_timeout_seconds=1.0),
    )
    assert first is not None and second is not None
    foreign = object()
    foreign_owner = SimpleNamespace(is_closing=False, ai_orchestration_engine=foreign)

    with pytest.raises(OrchestrationCompositionError, match="already owned"):
        await DurableOrchestrationConsumer(foreign_owner).start(first)
    assert foreign_owner.ai_orchestration_engine is foreign
    assert first.ready is False

    closing_owner = SimpleNamespace(is_closing=True)
    with pytest.raises(OrchestrationCompositionError, match="owner is closing"):
        await DurableOrchestrationConsumer(closing_owner).start(second)
    assert vars(closing_owner) == {"is_closing": True}
    await first.close()
    await second.close()


async def test_stop_never_deletes_a_foreign_replacement(tmp_path: Path) -> None:
    async def executor(_context: Any, _parameters: Any) -> ActionResult:
        return ActionResult(ActionStatus.COMPLETED, "ok")

    router, _, _ = _environment(executor)
    runtime = compose_durable_orchestration(
        router,
        _configuration(tmp_path / "state.sqlite3"),
        policy=OrchestrationPolicy(total_timeout_seconds=1.0),
    )
    assert runtime is not None
    owner = SimpleNamespace(is_closing=False)
    consumer = DurableOrchestrationConsumer(owner)
    await consumer.start(runtime)
    foreign = object()
    owner.ai_orchestration_repository = foreign

    with pytest.raises(OrchestrationCompositionError, match="identity changed"):
        await consumer.stop()

    assert owner.ai_orchestration_repository is foreign
    assert not hasattr(owner, "ai_orchestration_runtime")
    assert not hasattr(owner, "ai_orchestration_engine")
    assert runtime.closed is True
    assert consumer.ready is False


async def test_runtime_rejects_a_second_consumer(tmp_path: Path) -> None:
    async def executor(_context: Any, _parameters: Any) -> ActionResult:
        return ActionResult(ActionStatus.COMPLETED, "ok")

    router, _, _ = _environment(executor)
    runtime = compose_durable_orchestration(
        router,
        _configuration(tmp_path / "state.sqlite3"),
        policy=OrchestrationPolicy(total_timeout_seconds=1.0),
    )
    assert runtime is not None
    first = DurableOrchestrationConsumer(SimpleNamespace(is_closing=False))
    second = DurableOrchestrationConsumer(SimpleNamespace(is_closing=False))
    await first.start(runtime)

    with pytest.raises(OrchestrationCompositionError, match="already has a consumer"):
        await second.start(runtime)

    assert first.ready is True
    assert second.ready is False
    await first.stop()


async def test_process_restart_replays_terminal_receipt_without_execution(tmp_path: Path) -> None:
    calls = 0

    async def executor(_context: Any, _parameters: Any) -> ActionResult:
        nonlocal calls
        calls += 1
        return ActionResult(ActionStatus.COMPLETED, "公開結果")

    path = tmp_path / "durable.sqlite3"
    policy = OrchestrationPolicy(total_timeout_seconds=1.0)
    router_one, message_one, request_one = _environment(executor)
    runtime_one = compose_durable_orchestration(router_one, _configuration(path), policy=policy)
    assert runtime_one is not None
    consumer_one = DurableOrchestrationConsumer(SimpleNamespace(is_closing=False))
    await consumer_one.start(runtime_one)
    first = await _execute(runtime_one, message_one, request_one)
    await consumer_one.stop()

    router_two, message_two, request_two = _environment(executor)
    runtime_two = compose_durable_orchestration(router_two, _configuration(path), policy=policy)
    assert runtime_two is not None
    consumer_two = DurableOrchestrationConsumer(SimpleNamespace(is_closing=False))
    await consumer_two.start(runtime_two)
    replay = await _execute(runtime_two, message_two, request_two)

    assert calls == 1
    assert first.receipt.status is PlanStatus.COMPLETED
    assert replay == first
    assert replay.receipt.steps == first.receipt.steps
    await consumer_two.stop()
