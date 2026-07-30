"""Discordから直接利用できる /mod コマンドplugin。"""

from __future__ import annotations

from typing import Any

from .domain import ModAction, ModerationCase, PermissionDecision, PermissionSnapshot, Warning
from .repository import ModtoolsRepository
from .validation import decide_permission, validate_purge, validate_reason, validate_timeout_minutes


def setup(registry: Any) -> None:
    register = getattr(registry, "register_plugin", None) or getattr(registry, "register", None)
    if register is None:
        raise TypeError("registry must provide register_plugin() or register()")

    def factory() -> Any:
        # Discord adapterは有効化時まで遅延importする。
        from .plugin import ModtoolsPlugin

        return ModtoolsPlugin()

    register("modtools", factory)


__all__ = [
    "ModAction",
    "ModerationCase",
    "ModtoolsRepository",
    "PermissionDecision",
    "PermissionSnapshot",
    "Warning",
    "decide_permission",
    "setup",
    "validate_purge",
    "validate_reason",
    "validate_timeout_minutes",
]
