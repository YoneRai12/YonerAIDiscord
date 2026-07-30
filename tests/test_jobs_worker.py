from __future__ import annotations

import asyncio

import pytest

from yonerai_discord.modules.jobs import (
    DurableJobService,
    DurableJobWorker,
    ExplicitExecutorRegistry,
    JobStatus,
    Outcome,
    SqliteJobRepository,
)


class BlockingExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.effects = 0

    async def execute(self, _job, _attempt, context):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        if not context.begin_side_effect():
            return Outcome.skipped("worker stopped before side effect")
        self.effects += 1
        return Outcome.succeeded()


@pytest.fixture
def repo(tmp_path):
    value = SqliteJobRepository(tmp_path / "worker.sqlite3")
    value.open()
    yield value
    value.close()


@pytest.mark.asyncio
async def test_worker_polls_and_stops_without_claiming_more(repo) -> None:
    service = DurableJobService(repo, ExplicitExecutorRegistry())
    created = service.submit(action_key="noop:worker", revision=1, kind="internal.noop", payload={})
    worker = DurableJobWorker(service, poll_seconds=0.01, batch_size=2)
    task = asyncio.create_task(worker.run())
    async with asyncio.timeout(2):
        while repo.status_of(created.id) is not JobStatus.SUCCEEDED:
            await asyncio.sleep(0.005)
    worker.request_stop()
    await task
    snapshot = worker.snapshot()
    assert not snapshot.running
    assert snapshot.cycles >= 1
    assert snapshot.processed == 1


@pytest.mark.asyncio
async def test_graceful_drain_cancels_pre_side_effect_and_defers_rest(repo) -> None:
    executor = BlockingExecutor()
    service = DurableJobService(
        repo,
        {"safe.side-effect": executor},
        lease_seconds=5,
        execution_timeout_seconds=4,
        disabled_defer_seconds=30,
    )
    first = service.submit(
        action_key="drain:1",
        revision=1,
        kind="safe.side-effect",
        payload={},
        job_id="a-job",
    )
    second = service.submit(
        action_key="drain:2",
        revision=1,
        kind="safe.side-effect",
        payload={},
        job_id="b-job",
    )
    worker = DurableJobWorker(service, poll_seconds=60, batch_size=2)
    task = asyncio.create_task(worker.run())
    async with asyncio.timeout(2):
        await executor.started.wait()
    worker.request_stop()
    executor.release.set()
    await task

    assert repo.status_of(first.id) is JobStatus.PENDING
    deferred = repo.get(second.id)
    assert deferred is not None
    assert deferred.status is JobStatus.PENDING
    assert deferred.attempts == 0
    assert executor.calls == 1
    assert executor.effects == 0


@pytest.mark.asyncio
async def test_forced_cancellation_marks_started_side_effect_uncertain(repo) -> None:
    executor = BlockingExecutor()
    service = DurableJobService(
        repo,
        {"safe.side-effect": executor},
        lease_seconds=5,
        execution_timeout_seconds=4,
    )
    created = service.submit(
        action_key="cancel:1",
        revision=1,
        kind="safe.side-effect",
        payload={},
    )
    task = asyncio.create_task(service.run_once())
    async with asyncio.timeout(2):
        await executor.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert repo.status_of(created.id) is JobStatus.UNCERTAIN


@pytest.mark.asyncio
async def test_stop_after_side_effect_boundary_marks_job_uncertain(repo) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    effects: list[str] = []

    class StartedExecutor:
        async def execute(self, _job, _attempt, context):
            assert context.begin_side_effect()
            effects.append("effect")
            started.set()
            await release.wait()
            return Outcome.succeeded()

    service = DurableJobService(
        repo,
        {"started": StartedExecutor()},
        lease_seconds=5,
        execution_timeout_seconds=4,
    )
    created = service.submit(action_key="started:1", revision=1, kind="started", payload={})
    worker = DurableJobWorker(service, poll_seconds=60, batch_size=1)
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(started.wait(), timeout=2)
    worker.request_stop()
    release.set()
    await task

    assert effects == ["effect"]
    assert repo.status_of(created.id) is JobStatus.UNCERTAIN


@pytest.mark.asyncio
async def test_forced_cancel_defers_unstarted_batch_claim_without_spending_attempt(repo) -> None:
    class HungExecutor:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.calls = 0

        async def execute(self, _job, _attempt, _context):
            self.calls += 1
            self.started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    executor = HungExecutor()
    service = DurableJobService(
        repo,
        {"hung": executor},
        lease_seconds=5,
        execution_timeout_seconds=4,
        disabled_defer_seconds=30,
    )
    first = service.submit(action_key="forced:1", revision=1, kind="hung", payload={}, job_id="a-job")
    second = service.submit(action_key="forced:2", revision=1, kind="hung", payload={}, job_id="b-job")
    task = asyncio.create_task(service.run_once(limit=2))
    await asyncio.wait_for(executor.started.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert executor.calls == 1
    assert repo.status_of(first.id) is JobStatus.UNCERTAIN
    untouched = repo.get(second.id)
    assert untouched is not None
    assert untouched.status is JobStatus.PENDING
    assert untouched.attempts == 0
