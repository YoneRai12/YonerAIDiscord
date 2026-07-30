from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


DEFAULT_AUDIT_CONTENT_LIMIT = 200
MAX_AUDIT_CONTENT_LIMIT = 500
MAX_TEMPLATE_LENGTH = 1_000


@dataclass(frozen=True, slots=True)
class GuildServerConfig:
    guild_id: int
    welcome_channel_id: int | None = None
    welcome_message: str | None = None
    goodbye_channel_id: int | None = None
    goodbye_message: str | None = None
    log_channel_id: int | None = None
    audit_include_content: bool = False
    audit_content_limit: int = DEFAULT_AUDIT_CONTENT_LIMIT

    def __post_init__(self) -> None:
        if self.guild_id <= 0:
            raise ValueError("guild_id must be positive")
        for name, value in (
            ("welcome_channel_id", self.welcome_channel_id),
            ("goodbye_channel_id", self.goodbye_channel_id),
            ("log_channel_id", self.log_channel_id),
        ):
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive")
        for name, value in (
            ("welcome_message", self.welcome_message),
            ("goodbye_message", self.goodbye_message),
        ):
            if value is not None and (not value.strip() or len(value) > MAX_TEMPLATE_LENGTH):
                raise ValueError(f"{name} must be 1..{MAX_TEMPLATE_LENGTH} characters")
        if not 1 <= self.audit_content_limit <= MAX_AUDIT_CONTENT_LIMIT:
            raise ValueError(f"audit_content_limit must be 1..{MAX_AUDIT_CONTENT_LIMIT}")


class ServerAction(StrEnum):
    SLOWMODE = "slowmode"
    LOCK = "lock"
    UNLOCK = "unlock"
    NICK = "nick"
    ROLE_ADD = "role_add"
    ROLE_REMOVE = "role_remove"
    ANNOUNCE = "announce"
    CONFIGURE = "configure"


@dataclass(frozen=True, slots=True)
class ActorPermissions:
    administrator: bool = False
    manage_guild: bool = False
    manage_channels: bool = False
    manage_nicknames: bool = False
    manage_roles: bool = False


@dataclass(frozen=True, slots=True)
class HierarchyContext:
    actor_top_role: int
    bot_top_role: int
    target_member_top_role: int
    target_role_position: int | None = None
    target_is_owner: bool = False
    actor_is_owner: bool = False


class PolicyViolation(PermissionError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
