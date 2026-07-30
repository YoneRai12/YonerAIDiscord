"""既定OFFの read-only Minecraft Java Status Ping プラグイン。"""

from __future__ import annotations

from typing import Any

from .adapter import MinecraftGroup
from .client import (
    MinecraftConfigurationError,
    MinecraftProtocolError,
    MinecraftStatus,
    MinecraftStatusClient,
    MinecraftTarget,
    MinecraftUnavailableError,
    address_is_allowed,
    normalize_host,
)


class MinecraftPlugin:
    def __init__(self) -> None:
        self._bot: Any | None = None
        self.client: MinecraftStatusClient | None = None

    async def start(self, bot: Any) -> None:
        if self._bot is not None:
            return
        settings = bot.settings
        target = MinecraftTarget(
            host=getattr(settings, "minecraft_host", "127.0.0.1"),
            port=getattr(settings, "minecraft_port", 25_565),
            timeout_seconds=getattr(settings, "minecraft_timeout_seconds", 3.0),
            allow_public=getattr(settings, "minecraft_allow_public", False),
            max_packet_bytes=getattr(settings, "minecraft_max_packet_bytes", 32_768),
        )
        client = MinecraftStatusClient(target)
        bot.tree.add_command(MinecraftGroup(client))
        self.client = client
        self._bot = bot
        setattr(bot, "minecraft_status_client", client)

    async def stop(self) -> None:
        if self._bot is not None:
            self._bot.tree.remove_command("minecraft")
            if getattr(self._bot, "minecraft_status_client", None) is self.client:
                delattr(self._bot, "minecraft_status_client")
        self.client = None
        self._bot = None


def setup(registry: Any) -> None:
    register = getattr(registry, "register_plugin", None) or getattr(registry, "register", None)
    if register is None:
        raise TypeError("registry must provide register_plugin() or register()")
    register("minecraft", MinecraftPlugin)


__all__ = [
    "MinecraftConfigurationError",
    "MinecraftPlugin",
    "MinecraftProtocolError",
    "MinecraftStatus",
    "MinecraftStatusClient",
    "MinecraftTarget",
    "MinecraftUnavailableError",
    "address_is_allowed",
    "normalize_host",
    "setup",
]
