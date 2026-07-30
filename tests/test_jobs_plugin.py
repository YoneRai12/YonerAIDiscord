from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from yonerai_discord.modules.jobs import DurableJobService, JobStatus, setup
from yonerai_discord.modules.jobs.plugin import JobsPlugin


class Tree:
    def __init__(self) -> None:
        self.commands = {}

    def add_command(self, command) -> None:
        self.commands[command.name] = command

    def remove_command(self, name, **_kwargs):
        return self.commands.pop(name, None)


class CapabilityGuard:
    def __init__(self, executable: bool) -> None:
        self.executable = executable
        self.calls = []

    def currently_allowed(self, capability_id, *, guild_id, user_id):
        self.calls.append((capability_id, guild_id))
        assert user_id == 0
        return self.executable


def test_setup_registers_core_plugin_contract() -> None:
    calls = []
    setup(SimpleNamespace(register=lambda name, factory: calls.append((name, factory))))
    assert calls == [("jobs", JobsPlugin)]


@pytest.mark.asyncio
async def test_plugin_uses_core_database_path_and_attaches_service(tmp_path) -> None:
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            database_path=Path(tmp_path / "core.sqlite3"),
            shutdown_timeout_seconds=2,
        ),
        durable_job_executors={},
        tree=Tree(),
    )
    plugin = JobsPlugin()
    await plugin.start(bot)
    await asyncio.sleep(0)
    try:
        assert isinstance(bot.durable_jobs, DurableJobService)
        assert "jobs" in bot.tree.commands
        assert plugin.worker.snapshot().running
        tables = (
            plugin.repository._required().execute("SELECT name FROM sqlite_master WHERE name='durable_jobs'").fetchall()
        )
        assert tables
    finally:
        await plugin.stop()
    assert bot.durable_jobs is None
    assert "jobs" not in bot.tree.commands


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("executable", "expected"),
    [(True, JobStatus.SUCCEEDED), (False, JobStatus.PENDING)],
)
async def test_worker_rechecks_central_execution_capability(tmp_path, executable, expected) -> None:
    guard = CapabilityGuard(executable)
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            database_path=Path(tmp_path / "core.sqlite3"),
            shutdown_timeout_seconds=2,
            jobs_poll_seconds=0.01,
            jobs_disabled_defer_seconds=30,
        ),
        durable_job_executors={},
        capability_guard=guard,
        tree=Tree(),
    )
    plugin = JobsPlugin()
    await plugin.start(bot)
    try:
        created = bot.durable_jobs.submit(
            action_key=f"policy:{executable}",
            revision=1,
            kind="internal.noop",
            payload={},
            guild_id=123,
        )
        async with asyncio.timeout(2):
            while True:
                current = plugin.repository.get(created.id)
                if current is not None and current.status is expected:
                    if executable or current.available_at > created.available_at:
                        break
                await asyncio.sleep(0.005)
        assert guard.calls
        assert guard.calls[-1] == ("cap-run-jobs-execute", 123)
        if not executable:
            assert plugin.repository.get(created.id).attempts == 0
    finally:
        await plugin.stop()


@pytest.mark.asyncio
async def test_plugin_begin_close_stops_worker_and_rejects_later_execution(tmp_path) -> None:
    class RecordingExecutor:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, _job, _attempt, context):
            self.calls += 1
            assert context.complete_without_side_effect()
            raise AssertionError("quiesced executor must not run")

    executor = RecordingExecutor()
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            database_path=Path(tmp_path / "core.sqlite3"),
            shutdown_timeout_seconds=2,
            jobs_poll_seconds=60,
            jobs_disabled_defer_seconds=30,
        ),
        durable_job_executors={"quiesce.probe": executor},
        capability_guard=CapabilityGuard(True),
        tree=Tree(),
    )
    plugin = JobsPlugin()
    await plugin.start(bot)
    await asyncio.sleep(0)
    service = plugin.service
    task = plugin._task
    assert service is not None and task is not None

    await plugin.begin_close()
    await asyncio.wait_for(asyncio.shield(task), timeout=1)
    created = service.submit(
        action_key="quiesce:probe",
        revision=1,
        kind="quiesce.probe",
        payload={},
        guild_id=123,
    )
    assert await service.run_once(limit=1) == ((created.id, JobStatus.PENDING),)
    assert plugin.repository is not None
    assert plugin.repository.get(created.id).attempts == 0  # type: ignore[union-attr]
    assert executor.calls == 0
    await plugin.stop()
