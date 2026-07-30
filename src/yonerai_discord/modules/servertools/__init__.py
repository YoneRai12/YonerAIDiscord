"""Discordサーバー運用プラグイン。"""

from __future__ import annotations

from typing import Any

from .domain import (
    ActorPermissions,
    GuildServerConfig,
    HierarchyContext,
    PolicyViolation,
    ServerAction,
)
from .plugin import ServerToolsPlugin
from .policy import (
    require_action_permission,
    require_member_hierarchy,
    require_role_hierarchy,
    validate_announcement,
)
from .rendering import MemberTemplateValues, render_member_message, render_message_delete, render_message_edit
from .repository import SqliteServerToolsRepository


def setup(registry: Any) -> None:
    register = getattr(registry, "register_plugin", None) or getattr(registry, "register", None)
    if register is None:
        raise TypeError("registry must provide register_plugin() or register()")
    register("servertools", ServerToolsPlugin)


__all__ = [
    "ActorPermissions",
    "GuildServerConfig",
    "HierarchyContext",
    "MemberTemplateValues",
    "PolicyViolation",
    "ServerAction",
    "ServerToolsPlugin",
    "SqliteServerToolsRepository",
    "render_member_message",
    "render_message_delete",
    "render_message_edit",
    "require_action_permission",
    "require_member_hierarchy",
    "require_role_hierarchy",
    "setup",
    "validate_announcement",
]
