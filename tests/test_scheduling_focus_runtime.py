from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yonerai_discord.modules.jobs.domain import Attempt, ExecutionContext
from yonerai_discord.modules.jobs.executors import ExplicitExecutorRegistry
from yonerai_discord.modules.jobs.repository import SqliteJobRepository
from yonerai_discord.modules.jobs.service import DurableJobService
from yonerai_discord.modules.scheduling.focus import (
    FOCUS_TIMER_JOB_KIND,
    FocusDeliveryDisposition,
    FocusTextPreparationDisposition,
    FocusTimerBinding,
    FocusTimerCurrentState,
    FocusTimerRequest,
)
from yonerai_discord.modules.scheduling.plugin import SchedulingPlugin
from yonerai_discord.modules.scheduling.focus_runtime import (
    FocusTimerDiscordTextDelivery,
)


class _Tree:
    def __init__(self) -> None:
        self.commands: dict[str, object] = {}

    def add_command(self, command: Any) -> None:
        self.commands[command.name] = command

    def remove_command(self, name: str, **_kwargs: Any) -> object | None:
        return self.commands.pop(name, None)


class _Channel:
    def __init__(self, channel_id: int, guild: _Guild) -> None:
        self.id = channel_id
        self.guild = guild
        self.messages: list[tuple[str, Any]] = []

    async def send(self, content: str, *, allowed_mentions: Any) -> None:
        self.messages.append((content, allowed_mentions))

    @staticmethod
    def permissions_for(_member: Any) -> Any:
        return SimpleNamespace(
            view_channel=True,
            read_message_history=True,
            send_messages=True,
            connect=True,
            speak=True,
        )


class _Member:
    def __init__(self, member_id: int, guild: _Guild) -> None:
        self.id = member_id
        self.guild = guild
        self.bot = False


class _Guild:
    def __init__(self) -> None:
        self.id = 101
        self.channels: dict[int, _Channel] = {}
        self.members: dict[int, _Member] = {}

    async def fetch_member(self, member_id: int) -> _Member:
        member = self.members.get(member_id)
        if member is None:
            raise LookupError("missing")
        return member


class _Guard:
    def __init__(self) -> None:
        self.registry = object()
        self.allowed = True
        self.evaluations = 0

    async def evaluate_fresh_member(self, _capability_id: str, *, guild: _Guild, member: _Member) -> Any:
        self.evaluations += 1
        assert member.guild is guild
        return SimpleNamespace(allowed=self.allowed, actor_level="guild_admin", required_level="everyone")

    def currently_allowed(self, _capability_id: str, **_kwargs: Any) -> bool:
        return self.allowed


class _RejectedRegistry:
    def register(self, _kind: str, _executor: object) -> None:
        raise RuntimeError("registration unavailable")

    def unregister_if_current(self, _kind: str, _executor: object) -> bool:
        return False


class _VoiceDelivery:
    def __init__(self) -> None:
        self.available = True
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    async def speak(self, *args: Any, **kwargs: Any) -> FocusDeliveryDisposition:
        self.calls.append((args, kwargs))
        return FocusDeliveryDisposition.DELIVERED


def _timer(*, speak: bool = False) -> FocusTimerRequest:
    return FocusTimerRequest(
        binding=FocusTimerBinding(
            timer_id="focus-room",
            owner_id=77,
            guild_id=101,
            source_channel_id=201,
            destination_channel_id=202,
            revision=3,
        ),
        expires_at=datetime.now(UTC) + timedelta(minutes=2),
        speak_on_complete=speak,
    )


def _bot(tmp_path: Path, *, registry: Any | None = None) -> tuple[Any, SqliteJobRepository, _Guard, _Channel]:
    jobs_repository = SqliteJobRepository(tmp_path / "jobs.sqlite3")
    jobs_repository.open()
    executor_registry = registry if registry is not None else ExplicitExecutorRegistry()
    jobs = DurableJobService(jobs_repository, executor_registry)
    guard = _Guard()
    guild = _Guild()
    source, destination = _Channel(201, guild), _Channel(202, guild)
    guild.channels = {source.id: source, destination.id: destination}
    guild.members[77] = _Member(77, guild)
    bot_member = _Member(999, guild)
    bot_member.bot = True
    guild.members[bot_member.id] = bot_member

    async def fetch_channel(channel_id: int) -> _Channel:
        channel = guild.channels.get(channel_id)
        if channel is None:
            raise LookupError("missing")
        return channel

    bot = SimpleNamespace(
        settings=SimpleNamespace(database_path=tmp_path / "bot.sqlite3", scheduling_poll_seconds=3600),
        tree=_Tree(),
        capability_guard=guard,
        durable_jobs=jobs,
        durable_job_executor_registry=executor_registry,
        music_read_aloud_service=_VoiceDelivery(),
        get_guild=lambda guild_id: guild if guild_id == guild.id else None,
        fetch_channel=fetch_channel,
        user=SimpleNamespace(id=bot_member.id),
        is_closing=False,
    )
    return bot, jobs_repository, guard, source


@pytest.mark.asyncio
async def test_focus_runtime_registers_and_withdraws_only_its_exact_executor(tmp_path: Path) -> None:
    bot, jobs_repository, _guard, _source = _bot(tmp_path)
    plugin = SchedulingPlugin()
    try:
        await plugin.start(bot)
        assert plugin.focus_runtime is not None
        assert bot.scheduling_focus_timer_service is plugin.focus_service
        assert bot.scheduling_focus_overlay_store is plugin.focus_overlay_store
        assert bot.scheduling_focus_executor is plugin.focus_executor
        assert bot.durable_job_executor_registry.get(FOCUS_TIMER_JOB_KIND) is plugin.focus_executor

        await plugin.begin_close()
        assert bot.durable_job_executor_registry.get(FOCUS_TIMER_JOB_KIND) is None
        assert not hasattr(bot, "scheduling_focus_timer_service")
        assert not hasattr(bot, "scheduling_focus_overlay_store")
        assert not hasattr(bot, "scheduling_focus_executor")
    finally:
        await plugin.stop()
        jobs_repository.close()


@pytest.mark.asyncio
async def test_focus_runtime_registration_failure_rolls_back_without_breaking_scheduling(tmp_path: Path) -> None:
    bot, jobs_repository, _guard, _source = _bot(tmp_path, registry=_RejectedRegistry())
    plugin = SchedulingPlugin()
    try:
        await plugin.start(bot)
        assert plugin.repository is not None
        assert plugin.focus_runtime is None
        assert plugin.focus_service is None
        assert not hasattr(bot, "scheduling_focus_timer_service")
        assert not hasattr(bot, "scheduling_focus_overlay_store")
        assert not hasattr(bot, "scheduling_focus_executor")
    finally:
        await plugin.stop()
        jobs_repository.close()


@pytest.mark.asyncio
async def test_focus_runtime_is_not_ready_without_jobs_but_scheduling_still_starts(tmp_path: Path) -> None:
    bot = SimpleNamespace(
        settings=SimpleNamespace(database_path=tmp_path / "bot.sqlite3", scheduling_poll_seconds=3600),
        tree=_Tree(),
        capability_guard=_Guard(),
    )
    plugin = SchedulingPlugin()
    try:
        await plugin.start(bot)
        assert plugin.repository is not None
        assert plugin.focus_runtime is None
        assert "schedule" in bot.tree.commands
        assert not hasattr(bot, "scheduling_focus_timer_service")
    finally:
        await plugin.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ("revoked", "store_swap", "registry_swap"))
async def test_focus_runtime_fails_closed_before_text_send_when_runtime_changes(
    tmp_path: Path,
    mutation: str,
) -> None:
    bot, jobs_repository, guard, source = _bot(tmp_path)
    plugin = SchedulingPlugin()
    try:
        await plugin.start(bot)
        assert plugin.focus_service is not None and plugin.focus_executor is not None
        timer = _timer()
        await plugin.focus_service.schedule(timer)
        job = bot.durable_jobs.repository.get(timer.job_id)
        assert job is not None
        if mutation == "revoked":
            guard.allowed = False
        elif mutation == "store_swap":
            bot.scheduling_focus_overlay_store = object()
        else:
            bot.durable_job_executor_registry = ExplicitExecutorRegistry()

        outcome = await plugin.focus_executor.execute(
            job,
            Attempt(job.id, 1, datetime.now(UTC)),
            ExecutionContext(lambda: True),
        )
        assert outcome.receipt is None
        assert source.messages == []
    finally:
        await plugin.stop()
        jobs_repository.close()


@pytest.mark.asyncio
async def test_focus_runtime_text_delivery_is_exactly_once_and_uses_safe_mentions(tmp_path: Path) -> None:
    bot, jobs_repository, _guard, source = _bot(tmp_path)
    plugin = SchedulingPlugin()
    try:
        await plugin.start(bot)
        assert plugin.focus_service is not None and plugin.focus_executor is not None
        timer = _timer()
        await plugin.focus_service.schedule(timer)
        job = bot.durable_jobs.repository.get(timer.job_id)
        assert job is not None
        outcome = await plugin.focus_executor.execute(
            job,
            Attempt(job.id, 1, datetime.now(UTC)),
            ExecutionContext(lambda: True),
        )
        assert outcome.receipt is not None
        assert len(source.messages) == 1
        assert source.messages[0][0] == "集中タイマーが終了しました。"
        assert source.messages[0][1].everyone is False

        duplicate = await plugin.focus_executor.execute(
            job,
            Attempt(job.id, 2, datetime.now(UTC)),
            ExecutionContext(lambda: True),
        )
        assert duplicate.receipt is None
        assert len(source.messages) == 1
    finally:
        await plugin.stop()
        jobs_repository.close()


@pytest.mark.asyncio
async def test_authorization_probe_does_not_consume_pending_delivery_lease(
    tmp_path: Path,
) -> None:
    bot, jobs_repository, _guard, source = _bot(tmp_path)
    plugin = SchedulingPlugin()
    try:
        await plugin.start(bot)
        runtime = plugin.focus_runtime
        assert runtime is not None and plugin.focus_service is not None
        timer = _timer()
        await plugin.focus_service.schedule(timer)

        first = await runtime._delivery_current(timer.binding)
        assert isinstance(first, FocusTimerCurrentState)
        assert isinstance(
            await runtime.authorization_current(timer.binding),
            FocusTimerCurrentState,
        )
        prepared = await runtime.text_delivery.prepare(
            timer.binding,
            message_code="focus_timer.completed",
            idempotency_key=timer.binding_digest,
        )
        assert prepared is FocusTextPreparationDisposition.READY

        second = await runtime._delivery_current(timer.binding)
        assert isinstance(second, FocusTimerCurrentState)
        assert isinstance(
            await runtime.authorization_current(timer.binding),
            FocusTimerCurrentState,
        )
        delivered = await runtime.text_delivery.deliver(
            timer.binding,
            message_code="focus_timer.completed",
            idempotency_key=timer.binding_digest,
        )

        assert delivered is FocusDeliveryDisposition.DELIVERED
        assert len(source.messages) == 1
    finally:
        await plugin.stop()
        jobs_repository.close()


@pytest.mark.asyncio
async def test_focus_runtime_optional_voice_uses_existing_read_aloud_adapter(
    tmp_path: Path,
) -> None:
    bot, jobs_repository, _guard, source = _bot(tmp_path)
    plugin = SchedulingPlugin()
    try:
        await plugin.start(bot)
        assert plugin.focus_service is not None and plugin.focus_executor is not None
        timer = _timer(speak=True)
        await plugin.focus_service.schedule(timer)
        job = bot.durable_jobs.repository.get(timer.job_id)
        assert job is not None

        outcome = await plugin.focus_executor.execute(
            job,
            Attempt(job.id, 1, datetime.now(UTC)),
            ExecutionContext(lambda: True),
        )

        assert outcome.receipt is not None
        assert len(source.messages) == 1
        assert len(bot.music_read_aloud_service.calls) == 1
        args, kwargs = bot.music_read_aloud_service.calls[0]
        assert args == (timer.binding,)
        assert kwargs["message_code"] == "focus_timer.completed"
        assert kwargs["idempotency_key"] == timer.binding_digest
    finally:
        await plugin.stop()
        jobs_repository.close()


@pytest.mark.asyncio
async def test_focus_runtime_stop_cleans_up_even_if_reminder_worker_raises(tmp_path: Path) -> None:
    bot, jobs_repository, _guard, _source = _bot(tmp_path)
    plugin = SchedulingPlugin()
    try:
        await plugin.start(bot)
        assert plugin.focus_runtime is not None
        await plugin.begin_close()
        if plugin._task is not None:
            await plugin._task

        async def failed_worker() -> None:
            raise RuntimeError("worker failed")

        plugin._task = asyncio.create_task(failed_worker())
        with pytest.raises(RuntimeError, match="worker failed"):
            await plugin.stop()
        assert plugin.focus_runtime is None
        assert plugin.focus_service is None
        assert not hasattr(bot, "scheduling_focus_timer_service")
        assert not hasattr(bot, "scheduling_focus_overlay_store")
        assert not hasattr(bot, "scheduling_focus_executor")
    finally:
        jobs_repository.close()


def test_focus_text_delivery_migrates_pre_sending_state_schema(tmp_path: Path) -> None:
    path = tmp_path / "focus-delivery.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE scheduling_focus_delivery_intents (
        idempotency_key TEXT PRIMARY KEY,
        guild_id INTEGER NOT NULL,
        source_channel_id INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('prepared', 'delivered')),
        prepared_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        delivered_at TEXT
        )"""
    )
    connection.execute(
        """INSERT INTO scheduling_focus_delivery_intents
        (idempotency_key, guild_id, source_channel_id, state)
        VALUES (?, 101, 201, 'prepared')""",
        ("c" * 64,),
    )
    connection.commit()
    connection.close()
    delivery = FocusTimerDiscordTextDelivery(path, source_channel=lambda _binding: None)

    delivery.open()
    try:
        connection = sqlite3.connect(path)
        schema = connection.execute(
            """SELECT sql FROM sqlite_master
            WHERE type='table' AND name='scheduling_focus_delivery_intents'"""
        ).fetchone()[0]
        row = connection.execute(
            """SELECT state FROM scheduling_focus_delivery_intents
            WHERE idempotency_key=?""",
            ("c" * 64,),
        ).fetchone()
        connection.close()
        assert "'sending'" in schema
        assert row == ("prepared",)
    finally:
        delivery.close()
