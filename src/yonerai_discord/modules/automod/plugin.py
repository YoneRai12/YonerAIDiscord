from __future__ import annotations

from pathlib import Path
from typing import Any

from .repository import SqliteAutomodRepository


class AutomodPlugin:
    def __init__(self) -> None:
        self.repository: SqliteAutomodRepository | None = None
        self._bot: Any | None = None
        self._group: Any | None = None
        self._listeners: tuple[tuple[Any, str], ...] = ()

    async def start(self, bot: Any) -> None:
        if self._bot is not None:
            return
        repository = SqliteAutomodRepository(Path(bot.settings.database_path))
        repository.open()
        group: Any | None = None
        listeners: tuple[tuple[Any, str], ...] = ()
        try:
            # Discordはruntime依存のため、domain/repository単体試験から切り離す。
            from .adapter import AutomodGroup, DiscordAutomodListeners

            listener_owner = DiscordAutomodListeners(repository, bot)
            group = AutomodGroup(repository, bot)
            listeners = (
                (listener_owner.on_message, "on_message"),
                (listener_owner.on_message_edit, "on_message_edit"),
            )
            bot.tree.add_command(group)
            for callback, name in listeners:
                bot.add_listener(callback, name)
        except BaseException:
            if group is not None:
                bot.tree.remove_command("automod")
            for callback, name in listeners:
                bot.remove_listener(callback, name)
            repository.close()
            raise
        self.repository = repository
        self._bot = bot
        self._group = group
        self._listeners = listeners
        setattr(bot, "automod_repository", repository)

    async def stop(self) -> None:
        bot = self._bot
        if bot is not None:
            for callback, name in self._listeners:
                bot.remove_listener(callback, name)
            bot.tree.remove_command("automod")
            if getattr(bot, "automod_repository", None) is self.repository:
                delattr(bot, "automod_repository")
        if self.repository is not None:
            self.repository.close()
        self.repository = None
        self._bot = None
        self._group = None
        self._listeners = ()
