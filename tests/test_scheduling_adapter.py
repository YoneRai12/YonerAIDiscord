from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from yonerai_discord.capabilities import COMMAND_CAPABILITIES
from yonerai_discord.modules.scheduling.adapter import (
    DiscordReminderSender,
    ScheduleCancelReceipt,
    ScheduleGroup,
    schedule_cancel_receipt_digest,
)
from yonerai_discord.modules.scheduling.domain import AllowedMentions, Meeting, Reminder, ReminderDelivery
from yonerai_discord.modules.scheduling.service import DeliveryAbortedError
from yonerai_discord.modules.scheduling.repository import SqliteReminderRepository

from datetime import UTC, datetime, timedelta


def test_every_schedule_command_is_registered_in_central_manifest(tmp_path) -> None:
    group = ScheduleGroup(SqliteReminderRepository(tmp_path / "schedule.sqlite3"), object())  # type: ignore[arg-type]
    paths = {command.qualified_name for command in group.walk_commands()}
    assert paths == {
        "schedule cancel",
        "schedule create",
        "schedule list",
        "schedule remind",
        "schedule resolve",
        "schedule rsvp",
        "schedule show",
        "schedule uncertain",
    }
    assert paths <= set(COMMAND_CAPABILITIES)


@pytest.mark.asyncio
async def test_mention_cancel_uses_final_fresh_management_authority_and_keeps_success_when_audit_fails() -> None:
    now = datetime(2026, 7, 25, tzinfo=UTC)
    meeting = Meeting("MEET-ABCDEF12", 1, 2, 3, "会議", now, now + timedelta(hours=1), "UTC")
    calls: list[tuple[str, int, int, bool]] = []
    repository = SimpleNamespace(
        get_meeting=lambda meeting_id: meeting if meeting_id == meeting.id else None,
        cancel_meeting=lambda meeting_id, guild_id, actor_id, *, can_manage: (
            calls.append((meeting_id, guild_id, actor_id, can_manage)) or True
        ),
    )
    bot = SimpleNamespace(
        database=SimpleNamespace(append_audit=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError()))
    )
    group = ScheduleGroup(repository, bot)  # type: ignore[arg-type]
    receipt = ScheduleCancelReceipt(1, 20, 2, 3, 4, meeting.id)
    receipt = replace(receipt, digest=schedule_cancel_receipt_digest(receipt))

    assert not await group.cancel_mention_meeting(
        SimpleNamespace(id=1), receipt, authorization_current=lambda: (True, False, repository)
    )
    assert calls == []
    assert await group.cancel_mention_meeting(
        SimpleNamespace(id=1), receipt, authorization_current=lambda: (True, True, repository)
    )
    assert calls == [(meeting.id, 1, 20, True)]


@pytest.mark.asyncio
async def test_member_fetch_policy_change_aborts_before_discord_send() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    sent: list[str] = []

    class Member:
        bot = False

        async def send(self, *_args, **_kwargs) -> None:
            sent.append("sent")

    class Guild:
        def get_member(self, _member_id):
            return None

        async def fetch_member(self, _member_id):
            started.set()
            await release.wait()
            return Member()

    now = datetime(2026, 7, 22, tzinfo=UTC)
    delivery = ReminderDelivery(
        Reminder("r1", "m1", "race", now, 42),
        Meeting("m1", 1, 2, 3, "race", now, now + timedelta(hours=1), "UTC"),
        AllowedMentions(user_ids=(42,)),
    )
    allowed = True
    sender = DiscordReminderSender(SimpleNamespace(get_guild=lambda _guild_id: Guild()))
    task = asyncio.create_task(sender.send(delivery, still_allowed=lambda: allowed))
    await asyncio.wait_for(started.wait(), timeout=2)
    allowed = False
    release.set()

    with pytest.raises(DeliveryAbortedError):
        await task
    assert sent == []
