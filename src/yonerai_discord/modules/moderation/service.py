from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Callable

from .domain import (
    ConfirmDestructiveCommand,
    DestructiveCommand,
    DestructivePreview,
    DestructiveResult,
    InvalidConfirmation,
)
from .ports import DestructiveModerationPort


@dataclass(slots=True)
class _PendingConfirmation:
    command: DestructiveCommand
    expires_at: datetime


class DestructiveModerationService:
    """previewと一回限りのconfirmを必須にする破壊的操作境界。"""

    def __init__(
        self,
        port: DestructiveModerationPort,
        *,
        nonce_ttl_seconds: int = 120,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if nonce_ttl_seconds <= 0:
            raise ValueError("nonce_ttl_seconds must be positive")
        self.port = port
        self.nonce_ttl = timedelta(seconds=nonce_ttl_seconds)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._pending: dict[str, _PendingConfirmation] = {}

    def preview(self, command: DestructiveCommand) -> DestructivePreview:
        if not command.target_ids:
            raise ValueError("at least one target is required")
        if len(set(command.target_ids)) != len(command.target_ids):
            raise ValueError("target_ids must not contain duplicates")
        now = self.clock()
        expires_at = now + self.nonce_ttl
        nonce = secrets.token_urlsafe(24)
        self._pending[self._digest(nonce)] = _PendingConfirmation(command, expires_at)
        summary = (
            f"{command.action.value}: {len(command.target_ids)}件"
            f" / 実行者={command.requested_by} / 理由={command.reason[:100]}"
        )
        return DestructivePreview(nonce, command, len(command.target_ids), summary, expires_at)

    async def confirm(self, confirmation: ConfirmDestructiveCommand) -> DestructiveResult:
        digest = self._digest(confirmation.nonce)
        pending = self._pending.pop(digest, None)
        if pending is None:
            raise InvalidConfirmation("nonce is invalid or already consumed")
        if self.clock() > pending.expires_at:
            raise InvalidConfirmation("nonce has expired")
        command = pending.command
        if (
            confirmation.guild_id != command.guild_id
            or confirmation.confirmed_by != command.requested_by
            or confirmation.action is not command.action
        ):
            raise InvalidConfirmation("confirmation does not match preview")
        action_key = (
            "moderation:"
            + sha256(
                (
                    f"{digest}:{command.guild_id}:{command.requested_by}:"
                    f"{command.action.value}:{','.join(map(str, command.target_ids))}"
                ).encode("utf-8")
            ).hexdigest()
        )
        return await self.port.execute_destructive(action_key, command)

    def discard_expired(self) -> int:
        now = self.clock()
        expired = [key for key, value in self._pending.items() if now > value.expires_at]
        for key in expired:
            del self._pending[key]
        return len(expired)

    @staticmethod
    def _digest(nonce: str) -> str:
        return sha256(nonce.encode("utf-8")).hexdigest()
