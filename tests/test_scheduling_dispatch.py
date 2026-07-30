from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from yonerai_discord.modules.scheduling import (
    Meeting,
    Reminder,
    ReminderDelivery,
    ReminderDispatcher,
    ReminderStatus,
    ReminderWorker,
    RetryableDeliveryError,
    SchedulingPlugin,
    SqliteReminderRepository,
)


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


class RecordingSender:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.deliveries: list[ReminderDelivery] = []

    async def send(self, delivery: ReminderDelivery, *, still_allowed) -> None:
        assert still_allowed()
        self.deliveries.append(delivery)
        if self.fail:
            raise RuntimeError("secret-bearing adapter failure must not persist")


def prepared_repository(path) -> SqliteReminderRepository:
    repository = SqliteReminderRepository(path)
    repository.open()
    repository.save_meeting(Meeting("m1", 1, 2, 3, "会議", NOW, NOW + timedelta(hours=1), "UTC"))
    repository.schedule(Reminder("r1", "m1", "dispatch-once", NOW, 42))
    return repository


def test_success_is_sent_once_with_explicit_safe_mentions(tmp_path) -> None:
    async def scenario() -> None:
        repository = prepared_repository(tmp_path / "schedule.sqlite3")
        sender = RecordingSender()
        dispatcher = ReminderDispatcher(
            repository,
            sender,
            clock=lambda: NOW,
            delivery_policy=lambda _delivery: True,
        )
        try:
            assert await dispatcher.dispatch_due() == 1
            assert await dispatcher.dispatch_due() == 0
            assert len(sender.deliveries) == 1
            mentions = sender.deliveries[0].allowed_mentions
            assert mentions.user_ids == (42,)
            assert mentions.role_ids == ()
            assert not mentions.everyone
            assert not mentions.replied_user
        finally:
            repository.close()

    asyncio.run(scenario())


def test_ambiguous_sender_failure_is_quarantined_without_automatic_retry(tmp_path) -> None:
    async def scenario() -> None:
        repository = prepared_repository(tmp_path / "schedule.sqlite3")
        failing = RecordingSender(fail=True)
        try:
            dispatcher = ReminderDispatcher(
                repository,
                failing,
                clock=lambda: NOW,
                delivery_policy=lambda _delivery: True,
            )
            assert await dispatcher.dispatch_due() == 0
            assert repository.status_of("r1") is ReminderStatus.CLAIMED
            intent = repository.get_delivery_intent("r1")
            assert intent is not None
            assert intent.state.value == "uncertain"

            succeeding = RecordingSender()
            retry_dispatcher = ReminderDispatcher(
                repository,
                succeeding,
                clock=lambda: NOW + timedelta(hours=1),
                delivery_policy=lambda _delivery: True,
            )
            assert await retry_dispatcher.dispatch_due() == 0
            assert succeeding.deliveries == []
        finally:
            repository.close()

    asyncio.run(scenario())


def test_disabled_capability_defers_without_consuming_delivery_attempt(tmp_path) -> None:
    async def scenario() -> None:
        repository = prepared_repository(tmp_path / "schedule.sqlite3")
        sender = RecordingSender()
        try:
            blocked = ReminderDispatcher(
                repository,
                sender,
                clock=lambda: NOW,
            )
            assert await blocked.dispatch_due() == 0
            assert repository.status_of("r1") is ReminderStatus.PENDING
            assert sender.deliveries == []
            assert (
                repository.claim_due(
                    NOW + timedelta(minutes=4),
                    timedelta(minutes=1),
                )
                == ()
            )

            allowed = ReminderDispatcher(
                repository,
                sender,
                clock=lambda: NOW + timedelta(minutes=5),
                delivery_policy=lambda _delivery: True,
            )
            assert await allowed.dispatch_due() == 1
            assert sender.deliveries[0].reminder.attempts == 1
        finally:
            repository.close()

    asyncio.run(scenario())


def test_definitely_pre_send_failure_uses_bounded_retry_delay(tmp_path) -> None:
    class PreSendFailure:
        async def send(self, delivery: ReminderDelivery, *, still_allowed) -> None:
            assert still_allowed()
            raise RetryableDeliveryError("preflight unavailable")

    async def scenario() -> None:
        repository = prepared_repository(tmp_path / "schedule.sqlite3")
        try:
            first = ReminderDispatcher(
                repository,
                PreSendFailure(),
                clock=lambda: NOW,
                delivery_policy=lambda _delivery: True,
            )
            assert await first.dispatch_due() == 0
            assert repository.status_of("r1") is ReminderStatus.PENDING
            assert repository.get_delivery_intent("r1") is None

            succeeding = RecordingSender()
            too_early = ReminderDispatcher(
                repository,
                succeeding,
                clock=lambda: NOW + timedelta(seconds=29),
                delivery_policy=lambda _delivery: True,
            )
            assert await too_early.dispatch_due() == 0
            on_time = ReminderDispatcher(
                repository,
                succeeding,
                clock=lambda: NOW + timedelta(seconds=30),
                delivery_policy=lambda _delivery: True,
            )
            assert await on_time.dispatch_due() == 1
        finally:
            repository.close()

    asyncio.run(scenario())


def test_finalize_rejection_leaves_durable_uncertain_intent(tmp_path) -> None:
    class RejectFinalizeRepository(SqliteReminderRepository):
        def complete_delivery(self, reminder_id: str, claim_token: str, sent_at: datetime) -> bool:
            return False

    async def scenario() -> None:
        repository = RejectFinalizeRepository(tmp_path / "schedule.sqlite3")
        repository.open()
        repository.save_meeting(Meeting("m1", 1, 2, 3, "会議", NOW, NOW + timedelta(hours=1), "UTC"))
        repository.schedule(Reminder("r1", "m1", "finalize-failure", NOW, None))
        sender = RecordingSender()
        try:
            dispatcher = ReminderDispatcher(
                repository,
                sender,
                clock=lambda: NOW,
                delivery_policy=lambda _delivery: True,
            )
            assert await dispatcher.dispatch_due() == 0
            assert len(sender.deliveries) == 1
            intent = repository.get_delivery_intent("r1")
            assert intent is not None
            assert intent.state.value == "uncertain"
            assert repository.claim_due(NOW + timedelta(days=1), timedelta(minutes=1)) == ()
        finally:
            repository.close()

    asyncio.run(scenario())


def test_worker_stop_aborts_blocked_pre_send_and_defers_all_claims(tmp_path) -> None:
    class BlockingSender:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.resume = asyncio.Event()
            self.ids: list[str] = []

        async def send(self, delivery: ReminderDelivery, *, still_allowed) -> None:
            self.started.set()
            await self.resume.wait()
            if not still_allowed():
                raise RetryableDeliveryError("worker stopped before send")
            self.ids.append(delivery.reminder.id)

    async def scenario() -> None:
        repository = prepared_repository(tmp_path / "schedule.sqlite3")
        repository.schedule(Reminder("r2", "m1", "second", NOW, None))
        sender = BlockingSender()
        dispatcher = ReminderDispatcher(
            repository,
            sender,
            clock=lambda: NOW,
            delivery_policy=lambda _delivery: True,
        )
        worker = ReminderWorker(dispatcher, poll_seconds=3600)
        task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(sender.started.wait(), timeout=2)
            worker.request_stop()
            sender.resume.set()
            await asyncio.wait_for(task, timeout=2)
            assert sender.ids == []
            assert repository.status_of("r1") is ReminderStatus.PENDING
            assert repository.status_of("r2") is ReminderStatus.PENDING
            first = repository.claim_due(NOW + timedelta(seconds=30), timedelta(minutes=1))
            assert {claim.reminder.id for claim in first} == {"r1", "r2"}
            assert all(claim.reminder.attempts == 1 for claim in first)
        finally:
            worker.request_stop()
            sender.resume.set()
            if not task.done():
                await task
            repository.close()

    asyncio.run(scenario())


def test_plugin_begin_close_stops_worker_and_rejects_later_delivery(tmp_path) -> None:
    class Tree:
        def __init__(self) -> None:
            self.commands = {}

        def add_command(self, command) -> None:
            self.commands[command.name] = command

        def remove_command(self, name):
            return self.commands.pop(name, None)

    class Guard:
        def currently_allowed(self, _capability_id, **_kwargs) -> bool:
            return True

    async def scenario() -> None:
        sender = RecordingSender()
        bot = SimpleNamespace(
            settings=SimpleNamespace(
                database_path=tmp_path / "schedule.sqlite3",
                scheduling_poll_seconds=3600,
            ),
            tree=Tree(),
            capability_guard=Guard(),
            scheduling_reminder_sender=sender,
        )
        plugin = SchedulingPlugin()
        await plugin.start(bot)
        await asyncio.sleep(0)
        assert plugin.repository is not None and plugin.worker is not None and plugin._task is not None
        assert bot.scheduling_repository is plugin.repository
        assert bot.scheduling_plugin is plugin
        plugin.repository.save_meeting(Meeting("quiesce-m", 1, 2, 3, "停止確認", NOW, NOW + timedelta(hours=1), "UTC"))
        plugin.repository.schedule(Reminder("quiesce-r", "quiesce-m", "quiesce", NOW, None))

        await plugin.begin_close()
        await asyncio.wait_for(asyncio.shield(plugin._task), timeout=1)
        assert await plugin.worker.dispatcher.dispatch_due() == 0
        reminder = plugin.repository.claim_due(
            datetime.now(UTC) + timedelta(minutes=6),
            timedelta(minutes=1),
        )
        assert len(reminder) == 1
        assert reminder[0].reminder.attempts == 1
        assert sender.deliveries == []
        await plugin.stop()
        assert not hasattr(bot, "scheduling_repository")
        assert not hasattr(bot, "scheduling_plugin")

    asyncio.run(scenario())


def test_plugin_start_failure_removes_registered_command(tmp_path) -> None:
    class Tree:
        def __init__(self) -> None:
            self.commands = {}

        def add_command(self, command) -> None:
            self.commands[command.name] = command

        def remove_command(self, name):
            return self.commands.pop(name, None)

    async def scenario() -> None:
        bot = SimpleNamespace(
            settings=SimpleNamespace(
                database_path=tmp_path / "schedule.sqlite3",
                scheduling_poll_seconds=0,
            ),
            tree=Tree(),
        )
        plugin = SchedulingPlugin()

        with pytest.raises(ValueError, match="poll_seconds"):
            await plugin.start(bot)

        assert bot.tree.commands == {}
        assert plugin.repository is None
        assert plugin.bot is None
        assert plugin.closing is True
        assert not hasattr(bot, "scheduling_repository")
        assert not hasattr(bot, "scheduling_plugin")

    asyncio.run(scenario())


def test_plugin_stop_with_failed_worker_still_withdraws_resources(tmp_path) -> None:
    class Tree:
        def __init__(self) -> None:
            self.commands = {}

        def add_command(self, command) -> None:
            self.commands[command.name] = command

        def remove_command(self, name):
            return self.commands.pop(name, None)

    async def scenario() -> None:
        bot = SimpleNamespace(
            settings=SimpleNamespace(
                database_path=tmp_path / "schedule.sqlite3",
                scheduling_poll_seconds=3600,
            ),
            tree=Tree(),
            capability_guard=SimpleNamespace(currently_allowed=lambda *_args, **_kwargs: True),
            scheduling_reminder_sender=RecordingSender(),
        )
        plugin = SchedulingPlugin()
        await plugin.start(bot)
        original_task = plugin._task
        await plugin.begin_close()
        assert original_task is not None
        await original_task

        async def failed_worker() -> None:
            raise RuntimeError("worker failed")

        plugin._task = asyncio.create_task(failed_worker())
        with pytest.raises(RuntimeError, match="worker failed"):
            await plugin.stop()

        assert bot.tree.commands == {}
        assert plugin.repository is None
        assert plugin.bot is None
        assert not hasattr(bot, "scheduling_repository")
        assert not hasattr(bot, "scheduling_plugin")

    asyncio.run(scenario())
