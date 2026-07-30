"""本人同意・本人管理・guild分離を強制する個人AIメモリ。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from yonerai_discord.capabilities import COMMAND_CAPABILITIES
from yonerai_discord.modules.ai.state_repository import AIStateRepository
from yonerai_discord.v0_runtime.command_service import V0CommandService
from yonerai_discord.v0_runtime.integration import ExplicitMemoryCommandAdapter
from yonerai_discord.v0_runtime.memory_repository import V0ExplicitMemoryRepository

from .adapter import MemoryGroup
from .domain import MemoryItem, MemoryKind
from .repository import SqlitePersonalMemoryRepository
from .service import MemoryDisabledError, PersonalMemoryService, SensitiveMemoryError


class PersonalMemoryPlugin:
    def __init__(self) -> None:
        self._bot: Any | None = None
        self.repository: SqlitePersonalMemoryRepository | None = None
        self.service: PersonalMemoryService | None = None
        self.v0_state: AIStateRepository | None = None
        self.v0_commands: V0CommandService | None = None

    async def start(self, bot: Any) -> None:
        if self._bot is not None:
            return
        repository = SqlitePersonalMemoryRepository(Path(bot.settings.database_path))
        repository.open()

        def current_policy(operation: str, guild_id: int, user_id: int) -> bool:
            capability_id = COMMAND_CAPABILITIES.get(f"memory {operation}")
            guard = getattr(bot, "capability_guard", None)
            currently_allowed = getattr(guard, "currently_allowed", None)
            if capability_id is None or not callable(currently_allowed):
                return False
            try:
                return bool(
                    currently_allowed(
                        capability_id,
                        guild_id=guild_id,
                        user_id=user_id,
                    )
                )
            except Exception:
                return False

        service = PersonalMemoryService(repository, current_policy=current_policy)
        v0_state = AIStateRepository(Path(bot.settings.database_path))
        v0_memory = V0ExplicitMemoryRepository(v0_state)
        v0_commands = V0CommandService(memory=ExplicitMemoryCommandAdapter(v0_memory))
        try:
            bot.tree.add_command(MemoryGroup(service, v0_commands=v0_commands))
        except BaseException:
            v0_state.close()
            repository.close()
            raise
        setattr(bot, "personal_memory_service", service)
        self._bot = bot
        self.repository = repository
        self.service = service
        self.v0_state = v0_state
        self.v0_commands = v0_commands

    async def begin_close(self) -> None:
        if self.service is not None:
            self.service.close()

    async def stop(self) -> None:
        if self.service is not None:
            self.service.close()
        if self._bot is not None:
            self._bot.tree.remove_command("memory")
            if getattr(self._bot, "personal_memory_service", None) is self.service:
                delattr(self._bot, "personal_memory_service")
        if self.repository is not None:
            self.repository.close()
        if self.v0_state is not None:
            self.v0_state.close()
        self._bot = None
        self.repository = None
        self.service = None
        self.v0_state = None
        self.v0_commands = None


def setup(registry: Any) -> None:
    register = getattr(registry, "register_plugin", None) or getattr(registry, "register", None)
    if register is None:
        raise TypeError("registry must provide register_plugin() or register()")
    register("personal_memory", PersonalMemoryPlugin)


__all__ = [
    "MemoryDisabledError",
    "MemoryItem",
    "MemoryKind",
    "PersonalMemoryPlugin",
    "PersonalMemoryService",
    "SensitiveMemoryError",
    "SqlitePersonalMemoryRepository",
    "setup",
]
