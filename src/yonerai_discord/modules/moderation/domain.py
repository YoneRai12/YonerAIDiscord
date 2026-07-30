from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class DestructiveAction(str, Enum):
    PURGE_MESSAGES = "purge_messages"
    TIMEOUT_MEMBERS = "timeout_members"
    KICK_MEMBERS = "kick_members"
    BAN_MEMBERS = "ban_members"


@dataclass(frozen=True, slots=True)
class DestructiveCommand:
    guild_id: int
    requested_by: int
    action: DestructiveAction
    target_ids: tuple[int, ...]
    reason: str
    channel_id: int | None = None
    duration_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class DestructivePreview:
    nonce: str
    command: DestructiveCommand
    target_count: int
    summary: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ConfirmDestructiveCommand:
    nonce: str
    guild_id: int
    confirmed_by: int
    action: DestructiveAction


@dataclass(frozen=True, slots=True)
class DestructiveResult:
    action_key: str
    action: DestructiveAction
    requested_count: int
    succeeded_count: int
    failed_target_ids: tuple[int, ...] = ()


class InvalidConfirmation(ValueError):
    pass
