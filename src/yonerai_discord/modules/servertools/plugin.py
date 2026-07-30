from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .repository import SqliteServerToolsRepository


class ServerToolsPlugin:
    def __init__(self) -> None:
        self.repository: SqliteServerToolsRepository | None = None
        self._bot: Any | None = None
        self._listeners: tuple[tuple[Any, str], ...] = ()
        self.group: Any | None = None
        self._closing = False
        self.announcement_lock = asyncio.Lock()

    @property
    def closing(self) -> bool:
        return self._closing

    async def start(self, bot: Any) -> None:
        if self._bot is not None:
            return
        self._closing = False
        repository = SqliteServerToolsRepository(Path(bot.settings.database_path))
        repository.open()
        try:
            # Discord is a runtime dependency; keeping this import here lets the
            # repository/policy layer run in minimal test environments.
            from .adapter import DiscordServerToolsListeners, ServerGroup

            listener_owner = DiscordServerToolsListeners(repository)
            bind_bot = getattr(listener_owner, "bind_bot", None)
            if callable(bind_bot):
                bind_bot(bot)
            listeners = (
                (listener_owner.on_member_join, "on_member_join"),
                (listener_owner.on_member_remove, "on_member_remove"),
                (listener_owner.on_message_delete, "on_message_delete"),
                (listener_owner.on_message_edit, "on_message_edit"),
            )
            group = ServerGroup(repository, bot)
            bot.tree.add_command(group)
            for callback, name in listeners:
                bot.add_listener(callback, name)
        except BaseException:
            repository.close()
            raise
        self.repository = repository
        self._bot = bot
        self._listeners = listeners
        self.group = group
        setattr(bot, "servertools_repository", repository)
        setattr(bot, "servertools_plugin", self)

    async def stop(self) -> None:
        # A confirmed mention announcement holds this lock through its final
        # fresh check and send.  Stop therefore linearizes either before that
        # operation (which makes it fail closed) or immediately after it.
        async with self.announcement_lock:
            self._closing = True
            bot = self._bot
            if bot is not None:
                for callback, name in self._listeners:
                    bot.remove_listener(callback, name)
                bot.tree.remove_command("server")
                if getattr(bot, "servertools_repository", None) is self.repository:
                    delattr(bot, "servertools_repository")
                if getattr(bot, "servertools_plugin", None) is self:
                    delattr(bot, "servertools_plugin")
            if self.repository is not None:
                self.repository.close()
            self.repository = None
            self._listeners = ()
            self.group = None
            self._bot = None
