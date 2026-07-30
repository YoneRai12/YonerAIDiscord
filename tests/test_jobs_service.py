from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from yonerai_discord.modules.jobs import (
    DurableJobService,
    ExplicitExecutorRegistry,
    JobStatus,
    NonRetryableJobError,
    Outcome,
    Receipt,
    RetryableJobError,
    SqliteJobRepository,
)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 20, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


class SequenceExecutor:
    def __init__(self, *values) -> None:
        self.values = list(values)
        self.calls = 0

    async def execute(self, job, attempt, context):
        self.calls += 1
        assert context.complete_without_side_effect()
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FinalizeFaultRepository(SqliteJobRepository):
    def __init__(self, path) -> None:
        super().__init__(path)
        self.fail_finalize = True

    def finalize_success(self, claim, receipt, finished_at):
        if self.fail_finalize:
            self.fail_finalize = False
            raise OSError("injected finalize failure")
        return super().finalize_success(claim, receipt, finished_at)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def repo(tmp_path) -> SqliteJobRepository:
    value = SqliteJobRepository(tmp_path / "jobs.sqlite3")
    value.open()
    yield value
    value.close()


@pytest.mark.asyncio
async def test_retryable_failure_uses_exponential_backoff_then_succeeds(repo, clock) -> None:
    executor = SequenceExecutor(
        Outcome.retryable_failure("temporary"),
        RetryableJobError("network"),
        Outcome.succeeded(Receipt("external-1")),
    )
    service = DurableJobService(
        repo,
        {"discord": executor},
        backoff_base_seconds=5,
        clock=clock,
    )
    created = service.submit(action_key="send:1", revision=1, kind="discord", payload={})
    assert await service.run_once() == ((created.id, JobStatus.PENDING),)
    clock.now += timedelta(seconds=4)
    assert await service.run_once() == ()
    clock.now += timedelta(seconds=1)
    assert await service.run_once() == ((created.id, JobStatus.PENDING),)
    clock.now += timedelta(seconds=10)
    assert await service.run_once() == ((created.id, JobStatus.SUCCEEDED),)
    assert executor.calls == 3
    assert repo.get(created.id).attempts == 3


@pytest.mark.asyncio
async def test_nonretryable_error_fails_without_retry(repo, clock) -> None:
    executor = SequenceExecutor(NonRetryableJobError("bad request"))
    service = DurableJobService(repo, {"ai": executor}, clock=clock)
    created = service.submit(action_key="ai:1", revision=1, kind="ai", payload={})
    assert await service.run_once() == ((created.id, JobStatus.FAILED),)
    clock.now += timedelta(hours=1)
    assert await service.run_once() == ()
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_max_attempts_stops_retry(repo, clock) -> None:
    executor = SequenceExecutor(
        Outcome.retryable_failure("temporary"),
        Outcome.retryable_failure("temporary"),
    )
    service = DurableJobService(repo, {"voice": executor}, backoff_base_seconds=1, clock=clock)
    created = service.submit(
        action_key="voice:1",
        revision=1,
        kind="voice",
        payload={},
        max_attempts=2,
    )
    await service.run_once()
    clock.now += timedelta(seconds=1)
    assert await service.run_once() == ((created.id, JobStatus.FAILED),)
    assert repo.status_of(created.id) is JobStatus.FAILED


@pytest.mark.asyncio
async def test_success_then_finalize_fault_becomes_uncertain_and_never_reexecutes(tmp_path, clock) -> None:
    repo = FinalizeFaultRepository(tmp_path / "jobs.sqlite3")
    repo.open()
    try:
        executor = SequenceExecutor(Outcome.succeeded(Receipt("minecraft-op-1")))
        service = DurableJobService(
            repo,
            {"minecraft": executor},
            lease_seconds=5,
            execution_timeout_seconds=4,
            clock=clock,
        )
        created = service.submit(action_key="minecraft:1", revision=1, kind="minecraft", payload={})
        assert await service.run_once() == ((created.id, JobStatus.UNCERTAIN),)
        assert repo.status_of(created.id) is JobStatus.UNCERTAIN
        clock.now += timedelta(hours=1)
        assert await service.run_once() == ()
        assert executor.calls == 1
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_missing_executor_fails_closed_without_execution(repo, clock) -> None:
    service = DurableJobService(repo, {}, clock=clock)
    created = service.submit(action_key="unknown:1", revision=1, kind="unknown", payload={})
    assert await service.run_once() == ((created.id, JobStatus.FAILED),)
    assert repo.status_of(created.id) is JobStatus.FAILED


def test_backoff_is_capped(repo, clock) -> None:
    service = DurableJobService(
        repo,
        {},
        backoff_base_seconds=10,
        backoff_max_seconds=100,
        clock=clock,
    )
    assert service.backoff_seconds(1) == 10
    assert service.backoff_seconds(2) == 20
    assert service.backoff_seconds(10) == 100


def test_duplicate_submit_returns_original_durable_job(repo, clock) -> None:
    service = DurableJobService(repo, {}, clock=clock)
    first = service.submit(
        action_key="discord:dedupe",
        revision=1,
        kind="discord",
        payload={"content": "first"},
        job_id="first-id",
    )
    duplicate = service.submit(
        action_key="discord:dedupe",
        revision=1,
        kind="discord",
        payload={"content": "different retry payload"},
        job_id="second-id",
    )
    assert duplicate == first
    assert repo.get("second-id") is None


@pytest.mark.asyncio
async def test_execution_policy_defers_without_spending_attempt(repo, clock) -> None:
    executor = SequenceExecutor(Outcome.succeeded())
    service = DurableJobService(
        repo,
        {"discord": executor},
        execution_policy=lambda _job: False,
        disabled_defer_seconds=30,
        clock=clock,
    )
    created = service.submit(
        action_key="disabled:1",
        revision=1,
        kind="discord",
        payload={},
        guild_id=123,
    )
    assert await service.run_once() == ((created.id, JobStatus.PENDING),)
    deferred = repo.get(created.id)
    assert deferred is not None
    assert deferred.attempts == 0
    assert deferred.available_at == clock.now + timedelta(seconds=30)
    assert executor.calls == 0


@pytest.mark.asyncio
async def test_execution_policy_is_rechecked_at_side_effect_boundary(repo, clock) -> None:
    executor = SequenceExecutor(Outcome.succeeded())
    decisions = iter((True, False))
    service = DurableJobService(
        repo,
        {"discord": executor},
        execution_policy=lambda _job: next(decisions),
        disabled_defer_seconds=30,
        clock=clock,
    )
    created = service.submit(action_key="toggle:1", revision=1, kind="discord", payload={})
    assert await service.run_once() == ((created.id, JobStatus.PENDING),)
    deferred = repo.get(created.id)
    assert deferred is not None
    assert deferred.attempts == 0
    assert executor.calls == 0


@pytest.mark.asyncio
async def test_ambiguous_transport_failure_becomes_uncertain(repo, clock) -> None:
    executor = SequenceExecutor(ConnectionError("reset"))
    service = DurableJobService(repo, {"discord": executor}, clock=clock)
    created = service.submit(action_key="transport:1", revision=1, kind="discord", payload={})
    assert await service.run_once() == ((created.id, JobStatus.UNCERTAIN),)
    clock.now += timedelta(hours=1)
    assert await service.run_once() == ()


@pytest.mark.asyncio
async def test_execution_timeout_is_bounded_and_uncertain(repo, clock) -> None:
    class SlowExecutor:
        async def execute(self, _job, _attempt, context):
            assert context.begin_side_effect()
            await asyncio.sleep(60)

    service = DurableJobService(
        repo,
        {"discord": SlowExecutor()},
        lease_seconds=1,
        execution_timeout_seconds=0.01,
        clock=clock,
    )
    created = service.submit(action_key="timeout:1", revision=1, kind="discord", payload={})
    assert await service.run_once() == ((created.id, JobStatus.UNCERTAIN),)


@pytest.mark.asyncio
async def test_only_explicit_safe_builtin_is_registered(repo, clock) -> None:
    registry = ExplicitExecutorRegistry()
    assert registry.kinds() == ("internal.noop",)
    service = DurableJobService(repo, registry, clock=clock)
    created = service.submit(action_key="noop:1", revision=1, kind="internal.noop", payload={})
    assert await service.run_once() == ((created.id, JobStatus.SUCCEEDED),)


@pytest.mark.asyncio
async def test_executor_ignoring_execution_context_cannot_report_success(repo, clock) -> None:
    class IgnoringExecutor:
        async def execute(self, _job, _attempt, _context):
            return Outcome.succeeded()

    service = DurableJobService(repo, {"unsafe": IgnoringExecutor()}, clock=clock)
    created = service.submit(action_key="unsafe:1", revision=1, kind="unsafe", payload={})

    assert await service.run_once() == ((created.id, JobStatus.UNCERTAIN),)
    assert repo.status_of(created.id) is JobStatus.UNCERTAIN


@pytest.mark.asyncio
async def test_policy_change_during_executor_preflight_prevents_side_effect(repo, clock) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    effects: list[str] = []
    allowed = True

    class DelayedExecutor:
        async def execute(self, _job, _attempt, context):
            started.set()
            await release.wait()
            if not context.begin_side_effect():
                return Outcome.skipped("disabled before side effect")
            effects.append("effect")
            return Outcome.succeeded()

    service = DurableJobService(
        repo,
        {"delayed": DelayedExecutor()},
        execution_policy=lambda _job: allowed,
        disabled_defer_seconds=30,
        clock=clock,
    )
    created = service.submit(action_key="delayed:1", revision=1, kind="delayed", payload={})
    task = asyncio.create_task(service.run_once())
    await asyncio.wait_for(started.wait(), timeout=2)
    allowed = False
    release.set()

    assert await task == ((created.id, JobStatus.PENDING),)
    assert effects == []
    current = repo.get(created.id)
    assert current is not None and current.attempts == 0


@pytest.mark.asyncio
async def test_retryable_result_after_side_effect_boundary_is_uncertain_and_not_retried(repo, clock) -> None:
    class AmbiguousExecutor:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, _job, _attempt, context):
            self.calls += 1
            assert context.begin_side_effect()
            return Outcome.retryable_failure("transport")

    executor = AmbiguousExecutor()
    service = DurableJobService(repo, {"ambiguous": executor}, clock=clock)
    created = service.submit(action_key="ambiguous:1", revision=1, kind="ambiguous", payload={})

    assert await service.run_once() == ((created.id, JobStatus.UNCERTAIN),)
    clock.now += timedelta(days=1)
    assert await service.run_once() == ()
    assert executor.calls == 1


def test_submit_rejects_dynamic_kind_and_oversized_or_non_json_payload(repo, clock) -> None:
    service = DurableJobService(repo, {}, clock=clock)
    with pytest.raises(ValueError):
        service.submit(action_key="bad-kind", revision=1, kind="os.system()", payload={})
    with pytest.raises(ValueError):
        service.submit(action_key="large", revision=1, kind="internal.noop", payload={"x": "a" * 20_000})
    with pytest.raises(ValueError):
        service.submit(action_key="object", revision=1, kind="internal.noop", payload={"x": object()})
