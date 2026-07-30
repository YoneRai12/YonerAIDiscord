from __future__ import annotations

from pathlib import Path
from typing import Any

import discord

from .adapter import ModGroup
from .repository import ModtoolsRepository


class ModtoolsPlugin:
    def __init__(self) -> None:
        self.bot: Any | None = None
        self.repository: ModtoolsRepository | None = None
        self.group: ModGroup | None = None
        self._closing = False
        self._published = False

    @property
    def closing(self) -> bool:
        return self._closing

    async def start(self, bot: Any) -> None:
        self.bot = bot
        self._closing = False
        try:
            self.repository = ModtoolsRepository(Path(bot.settings.database_path))
            self.repository.open()
            self.group = ModGroup(self.repository, bot)
            bot.tree.add_command(self.group)
        except BaseException:
            if self.repository is not None:
                self.repository.close()
            self.repository = None
            self.group = None
            self.bot = None
            raise
        setattr(bot, "modtools_repository", self.repository)
        setattr(bot, "modtools_plugin", self)
        self._published = True

    async def stop(self) -> None:
        self._closing = True
        bot, repository, group = self.bot, self.repository, self.group
        try:
            if bot is not None and group is not None:
                bot.tree.remove_command(group.name, type=discord.AppCommandType.chat_input)
            if repository is not None:
                repository.close()
        finally:
            if self._published and bot is not None:
                if getattr(bot, "modtools_repository", None) is repository:
                    delattr(bot, "modtools_repository")
                if getattr(bot, "modtools_plugin", None) is self:
                    delattr(bot, "modtools_plugin")
            self._published = False
            self.group = None
            self.repository = None
            self.bot = None
