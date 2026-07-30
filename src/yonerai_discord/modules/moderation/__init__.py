"""手動モデレーションの安全なpreview/confirm境界。"""

from __future__ import annotations

from typing import Any

from .domain import (
    ConfirmDestructiveCommand,
    DestructiveAction,
    DestructiveCommand,
    DestructivePreview,
    DestructiveResult,
    InvalidConfirmation,
)
from .ports import DestructiveModerationPort
from .service import DestructiveModerationService


class ModerationPlugin:
    def __init__(self) -> None:
        self.bot: Any | None = None

    async def start(self, bot: Any) -> None:
        self.bot = bot

    async def stop(self) -> None:
        self.bot = None


def setup(registry: Any) -> None:
    register = getattr(registry, "register_plugin", None) or getattr(registry, "register", None)
    if register is None:
        raise TypeError("registry must provide register_plugin() or register()")
    register("moderation", ModerationPlugin)


__all__ = [
    "ConfirmDestructiveCommand",
    "DestructiveAction",
    "DestructiveCommand",
    "DestructiveModerationPort",
    "DestructiveModerationService",
    "DestructivePreview",
    "DestructiveResult",
    "InvalidConfirmation",
    "setup",
]
