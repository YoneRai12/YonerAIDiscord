from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from yonerai_discord.modules.moderation import (
    ConfirmDestructiveCommand,
    DestructiveAction,
    DestructiveCommand,
    DestructiveModerationService,
    DestructiveResult,
    InvalidConfirmation,
)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 20, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


class FakePort:
    def __init__(self) -> None:
        self.calls = []

    async def execute_destructive(self, action_key, command):
        self.calls.append((action_key, command))
        return DestructiveResult(action_key, command.action, len(command.target_ids), len(command.target_ids))


def command() -> DestructiveCommand:
    return DestructiveCommand(
        guild_id=1,
        requested_by=2,
        action=DestructiveAction.BAN_MEMBERS,
        target_ids=(10, 11),
        reason="raid",
    )


@pytest.mark.asyncio
async def test_preview_then_matching_confirm_executes_once() -> None:
    port = FakePort()
    service = DestructiveModerationService(port)
    preview = service.preview(command())
    assert preview.target_count == 2
    confirmation = ConfirmDestructiveCommand(preview.nonce, 1, 2, DestructiveAction.BAN_MEMBERS)
    result = await service.confirm(confirmation)
    assert result.succeeded_count == 2
    with pytest.raises(InvalidConfirmation, match="already consumed"):
        await service.confirm(confirmation)
    assert len(port.calls) == 1


@pytest.mark.asyncio
async def test_nonce_is_bound_to_actor_guild_and_action() -> None:
    service = DestructiveModerationService(FakePort())
    preview = service.preview(command())
    with pytest.raises(InvalidConfirmation, match="does not match"):
        await service.confirm(ConfirmDestructiveCommand(preview.nonce, 1, 999, DestructiveAction.BAN_MEMBERS))


@pytest.mark.asyncio
async def test_expired_nonce_cannot_execute() -> None:
    clock = Clock()
    port = FakePort()
    service = DestructiveModerationService(port, nonce_ttl_seconds=10, clock=clock)
    preview = service.preview(command())
    clock.now += timedelta(seconds=11)
    with pytest.raises(InvalidConfirmation, match="expired"):
        await service.confirm(ConfirmDestructiveCommand(preview.nonce, 1, 2, DestructiveAction.BAN_MEMBERS))
    assert port.calls == []


def test_duplicate_or_empty_targets_rejected_before_preview() -> None:
    service = DestructiveModerationService(FakePort())
    with pytest.raises(ValueError, match="at least one"):
        service.preview(command().__class__(1, 2, DestructiveAction.BAN_MEMBERS, (), "reason"))
    with pytest.raises(ValueError, match="duplicates"):
        service.preview(command().__class__(1, 2, DestructiveAction.BAN_MEMBERS, (10, 10), "reason"))
