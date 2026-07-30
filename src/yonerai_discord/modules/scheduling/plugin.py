from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path
from typing import Any

from ...capabilities import COMMAND_CAPABILITIES
from .repository import SqliteReminderRepository
from .service import ReminderDispatcher, ReminderWorker
from .adapter import DiscordReminderSender, ScheduleGroup
from .focus_runtime import FocusTimerRuntime


logger = logging.getLogger(__name__)


class SchedulingPlugin:
    """sender adapter未設定時は永続層だけ準備し、安全に待機する。"""

    def __init__(self) -> None:
        self.repository: SqliteReminderRepository | None = None
        self.worker: ReminderWorker | None = None
        self._task: asyncio.Task[None] | None = None
        self._bot: Any | None = None
        self._stopping = True
        self._published = False
        self.group: Any | None = None
        self.focus_runtime: FocusTimerRuntime | None = None
        self.focus_service: Any | None = None
        self.focus_overlay_store: Any | None = None
        self.focus_executor: Any | None = None

    @property
    def bot(self) -> Any | None:
        return self._bot

    @property
    def closing(self) -> bool:
        return self._stopping

    async def start(self, bot: Any) -> None:
        self._stopping = False
        self._bot = bot
        command_registered = False
        try:
            database_path = Path(bot.settings.database_path)
            self.repository = SqliteReminderRepository(database_path)
            self.repository.open()
            group = ScheduleGroup(self.repository, bot)
            bot.tree.add_command(group)
            command_registered = True

            sender = getattr(bot, "scheduling_reminder_sender", None) or DiscordReminderSender(bot)
            capability_id = COMMAND_CAPABILITIES["schedule remind"]

            def delivery_policy(delivery: Any) -> bool:
                if self._stopping:
                    return False
                guard = getattr(bot, "capability_guard", None)
                currently_allowed = getattr(guard, "currently_allowed", None)
                if not callable(currently_allowed):
                    return False
                try:
                    return bool(
                        currently_allowed(
                            capability_id,
                            guild_id=delivery.meeting.guild_id,
                            user_id=0,
                        )
                    )
                except Exception:
                    return False

            dispatcher = ReminderDispatcher(self.repository, sender, delivery_policy=delivery_policy)
            poll_seconds = float(getattr(bot.settings, "scheduling_poll_seconds", 30.0))
            self.worker = ReminderWorker(dispatcher, poll_seconds)
            self._task = asyncio.create_task(self.worker.run(), name="scheduling-reminder-worker")
        except BaseException:
            with suppress(Exception):
                if command_registered:
                    bot.tree.remove_command("schedule")
            with suppress(Exception):
                if self.repository is not None:
                    self.repository.close()
            self.repository = None
            self.worker = None
            self._close_focus_runtime()
            self._bot = None
            self._stopping = True
            raise
        setattr(bot, "scheduling_repository", self.repository)
        setattr(bot, "scheduling_plugin", self)
        self.group = group
        self._published = True
        self._start_focus_runtime(database_path)

    async def begin_close(self) -> None:
        """新規deliveryをfail-closedにし、workerへ停止を先行通知する。"""

        self._stopping = True
        if self.focus_runtime is not None:
            self.focus_runtime.begin_close()
        if self.worker is not None:
            self.worker.request_stop()

    async def stop(self) -> None:
        await self.begin_close()
        bot, repository = self._bot, self.repository
        try:
            if self._task is not None:
                await self._task
        finally:
            try:
                self._close_focus_runtime()
                if repository is not None:
                    repository.close()
            finally:
                self._task = None
                self.repository = None
                self.worker = None
                self.group = None
                self._bot = None
                try:
                    if bot is not None:
                        bot.tree.remove_command("schedule")
                finally:
                    if self._published and bot is not None:
                        if getattr(bot, "scheduling_repository", None) is repository:
                            delattr(bot, "scheduling_repository")
                        if getattr(bot, "scheduling_plugin", None) is self:
                            delattr(bot, "scheduling_plugin")
                    self._published = False

    def _start_focus_runtime(self, database_path: Path) -> None:
        bot = self._bot
        if bot is None or self._stopping:
            return
        runtime = FocusTimerRuntime.open(plugin=self, bot=bot, path=database_path)
        if runtime is None:
            return
        self.focus_runtime = runtime
        self.focus_service = runtime.service
        self.focus_overlay_store = runtime.overlays
        self.focus_executor = runtime.executor
        if not runtime.publish():
            self._close_focus_runtime()
            return

    def _close_focus_runtime(self) -> None:
        runtime, self.focus_runtime = self.focus_runtime, None
        self.focus_service = None
        self.focus_overlay_store = None
        self.focus_executor = None
        if runtime is None:
            return
        try:
            runtime.close()
        except Exception:
            logger.warning("scheduling_focus_runtime_cleanup_failed")
