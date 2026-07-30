from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Mapping


class ModAction(str, Enum):
    WARN = "warn"
    WARNINGS = "warnings"
    TIMEOUT = "timeout"
    UNTIMEOUT = "untimeout"
    KICK = "kick"
    BAN = "ban"
    UNBAN = "unban"
    PURGE = "purge"
    PURGE_USER = "purge_user"
    PURGE_LINKS = "purge_links"
    CASE = "case"


@dataclass(frozen=True, slots=True)
class PermissionSnapshot:
    guild_owner_id: int
    bot_user_id: int
    actor_id: int
    actor_permissions: frozenset[str]
    bot_permissions: frozenset[str]
    actor_top_role: int
    bot_top_role: int
    target_id: int | None = None
    target_top_role: int | None = None


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    allowed: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ModerationCase:
    id: int
    guild_id: int
    action: ModAction
    target_id: int | None
    moderator_id: int
    reason: str
    status: str
    metadata: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Warning:
    id: int
    case_id: int
    guild_id: int
    target_id: int
    moderator_id: int
    reason: str
    active: bool
    created_at: datetime
