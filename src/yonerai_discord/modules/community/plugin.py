from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Any

import discord

from .commands import PollGroup, SelfRoleGroup, SuggestGroup, TicketGroup
from .repository import CommunityRepository
from .views import PollView, SelfRoleView


class CommunityPlugin:
    command_names = ("ticket", "poll", "suggest", "selfrole")

    def __init__(self) -> None:
        self.repository: CommunityRepository | None = None
        self.bot: Any | None = None
        self._closing = True
        self._published = False

    @property
    def closing(self) -> bool:
        return self._closing

    async def start(self, bot: Any) -> None:
        self._closing = False
        self.bot = bot
        try:
            self.repository = CommunityRepository(Path(bot.settings.database_path))
            self.repository.open()
            bot.tree.add_command(TicketGroup(self.repository, bot))
            bot.tree.add_command(PollGroup(self.repository))
            bot.tree.add_command(SuggestGroup(self.repository))
            bot.tree.add_command(SelfRoleGroup(self.repository))

            # message_id付きで復元し、再起動後も既存の投票ボタンを受け付ける。
            for poll in self.repository.open_polls():
                if poll.message_id is not None:
                    bot.add_view(PollView(self.repository, poll.id, poll.options), message_id=poll.message_id)
            # custom_idにguild/roleを含むため、パネル個別のmessage_idがなくても復元可能。
            for guild_id, role_ids in self.repository.all_selfrole_sets():
                if role_ids:
                    bot.add_view(SelfRoleView(self.repository, guild_id, role_ids))
        except BaseException:
            for name in self.command_names:
                with suppress(Exception):
                    bot.tree.remove_command(name, type=discord.AppCommandType.chat_input)
            with suppress(Exception):
                if self.repository is not None:
                    self.repository.close()
            self.repository = None
            self.bot = None
            self._closing = True
            raise
        setattr(bot, "community_repository", self.repository)
        setattr(bot, "community_plugin", self)
        self._published = True

    async def begin_close(self) -> None:
        self._closing = True
        self._unpublish()

    async def stop(self) -> None:
        await self.begin_close()
        bot, repository = self.bot, self.repository
        try:
            if bot is not None:
                for name in self.command_names:
                    bot.tree.remove_command(name, type=discord.AppCommandType.chat_input)
        finally:
            try:
                if repository is not None:
                    repository.close()
            finally:
                self.repository = None
                self.bot = None

    def _unpublish(self) -> None:
        if self._published and self.bot is not None:
            if getattr(self.bot, "community_repository", None) is self.repository:
                delattr(self.bot, "community_repository")
            if getattr(self.bot, "community_plugin", None) is self:
                delattr(self.bot, "community_plugin")
        self._published = False
