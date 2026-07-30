"""サーバー情報と安全な小物コマンド。"""

from __future__ import annotations

from typing import Any

from .adapter import InfoGroup, ToolsGroup
from .domain import (
    DiceExpression,
    color_from_hex,
    discord_timestamp,
    parse_choices,
    parse_dice,
    sha256_text,
    snowflake_created_at,
)


class UtilityPlugin:
    def __init__(self) -> None:
        self._bot: Any | None = None

    async def start(self, bot: Any) -> None:
        self._bot = bot
        bot.tree.add_command(InfoGroup())
        bot.tree.add_command(ToolsGroup())

    async def stop(self) -> None:
        if self._bot is not None:
            self._bot.tree.remove_command("info")
            self._bot.tree.remove_command("tools")
        self._bot = None


def setup(registry: Any) -> None:
    register = getattr(registry, "register_plugin", None) or getattr(registry, "register", None)
    if register is None:
        raise TypeError("registry must provide register_plugin() or register()")
    register("utility", UtilityPlugin)


__all__ = [
    "DiceExpression",
    "color_from_hex",
    "discord_timestamp",
    "parse_choices",
    "parse_dice",
    "sha256_text",
    "snowflake_created_at",
    "setup",
]
