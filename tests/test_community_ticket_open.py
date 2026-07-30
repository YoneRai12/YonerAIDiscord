from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Callable

import discord

from yonerai_discord.capabilities import COMMAND_CAPABILITIES
from yonerai_discord.modules.community import CommunityRepository, Ticket, TicketStatus
from yonerai_discord.modules.community import commands as community_commands
from yonerai_discord.modules.community.commands import TicketGroup


class SyntheticHTTPException(discord.HTTPException):
    def __init__(self) -> None:
        Exception.__init__(self, "synthetic Discord failure")


class FakePermissions:
    def __init__(self, *, manage_channels: bool = False) -> None:
        self.administrator = False
        self.manage_guild = False
        self.manage_channels = manage_channels
        self.manage_roles = False
        self.manage_messages = False


@dataclass(frozen=True)
class FakeRole:
    id: int


class FakeMember:
    def __init__(self, member_id: int, *, manage_channels: bool = False) -> None:
        self.id = member_id
        self.guild_permissions = FakePermissions(manage_channels=manage_channels)


class FakeCreatedChannel:
    def __init__(self, channel_id: int = 777) -> None:
        self.id = channel_id
        self.mention = f"<#${channel_id}>"
        self.messages: list[str] = []
        self.delete_calls = 0

    async def send(self, message: str, **_kwargs: object) -> None:
        self.messages.append(message)

    async def delete(self, **_kwargs: object) -> None:
        self.delete_calls += 1


class FakeGuild:
    def __init__(
        self,
        *,
        cached_actor: FakeMember,
        fresh_actor: FakeMember,
        cached_bot: FakeMember,
        fresh_bot: FakeMember,
        fail_create: bool = False,
    ) -> None:
        self.id = 1
        self.owner_id = 9999
        self.default_role = FakeRole(1)
        self.me = cached_bot
        self._fresh_members = {
            fresh_actor.id: fresh_actor,
            fresh_bot.id: fresh_bot,
        }
        self.fail_create = fail_create
        self.created_channel = FakeCreatedChannel()
        self.create_calls: list[dict[str, object]] = []
        self.fetch_member_calls: list[int] = []

    async def fetch_member(self, member_id: int) -> FakeMember:
        self.fetch_member_calls.append(member_id)
        return self._fresh_members[member_id]

    async def create_text_channel(self, name: str, **kwargs: object) -> FakeCreatedChannel:
        self.create_calls.append({"name": name, **kwargs})
        if self.fail_create:
            raise SyntheticHTTPException()
        return self.created_channel


class Guard:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[str, int]] = []

    async def evaluate_fresh_member(self, capability_id: str, *, guild: object, member: FakeMember) -> object:
        self.calls.append((capability_id, member.id))
        return SimpleNamespace(allowed=self.allowed)


class Response:
    def __init__(self, on_defer: Callable[[], None] | None = None) -> None:
        self.messages: list[str] = []
        self.deferred = False
        self.on_defer = on_defer

    async def send_message(self, message: str, **_kwargs: object) -> None:
        self.messages.append(message)

    async def defer(self, **_kwargs: object) -> None:
        self.deferred = True
        if self.on_defer is not None:
            self.on_defer()


class Followup:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str, **_kwargs: object) -> None:
        self.messages.append(message)


def patch_discord_member(monkeypatch) -> None:
    monkeypatch.setattr(community_commands.discord, "Member", FakeMember)


def make_runtime(
    repository: CommunityRepository,
    *,
    guard_allowed: bool = True,
    cached_bot_can_manage: bool = True,
    fresh_bot_can_manage: bool = True,
    fail_create: bool = False,
    on_defer: Callable[[], None] | None = None,
) -> tuple[TicketGroup, SimpleNamespace, FakeGuild, Guard, FakeMember, FakeMember, SimpleNamespace]:
    cached_actor = FakeMember(10)
    fresh_actor = FakeMember(10)
    cached_bot = FakeMember(999, manage_channels=cached_bot_can_manage)
    fresh_bot = FakeMember(999, manage_channels=fresh_bot_can_manage)
    guild = FakeGuild(
        cached_actor=cached_actor,
        fresh_actor=fresh_actor,
        cached_bot=cached_bot,
        fresh_bot=fresh_bot,
        fail_create=fail_create,
    )
    guard = Guard(guard_allowed)
    bot = SimpleNamespace(capability_guard=guard, is_closing=False)
    interaction = SimpleNamespace(
        guild_id=guild.id,
        guild=guild,
        user=cached_actor,
        response=Response(on_defer),
        followup=Followup(),
    )
    return TicketGroup(repository, bot), interaction, guild, guard, cached_actor, fresh_actor, bot


async def invoke_open(group: TicketGroup, interaction: object, subject: str = "help") -> None:
    await TicketGroup.open.callback(group, interaction, subject)  # type: ignore[arg-type]


def assert_owner_has_no_open_ticket(repository: CommunityRepository, *, owner_id: int = 10) -> None:
    assert repository.create_ticket(Ticket("probe-ticket", 1, owner_id, "probe"))


async def test_open_stops_after_defer_when_shutdown_begins_and_leaves_no_ticket(tmp_path, monkeypatch) -> None:
    patch_discord_member(monkeypatch)
    repository = CommunityRepository(tmp_path / "community.sqlite3")
    repository.open()
    try:
        runtime: dict[str, object] = {}

        def begin_close() -> None:
            runtime["bot"].is_closing = True  # type: ignore[union-attr]

        group, interaction, guild, guard, _, _, bot = make_runtime(repository, on_defer=begin_close)
        runtime["bot"] = bot

        await invoke_open(group, interaction)

        assert interaction.response.deferred
        assert guild.create_calls == []
        assert guard.calls == []
        assert "停止処理" in interaction.followup.messages[-1]
        assert_owner_has_no_open_ticket(repository)
    finally:
        repository.close()


async def test_open_rechecks_central_policy_with_fresh_actor_before_create(tmp_path, monkeypatch) -> None:
    patch_discord_member(monkeypatch)
    repository = CommunityRepository(tmp_path / "community.sqlite3")
    repository.open()
    try:
        group, interaction, guild, guard, _, fresh_actor, _ = make_runtime(repository, guard_allowed=False)

        await invoke_open(group, interaction)

        assert guard.calls == [(COMMAND_CAPABILITIES["ticket open"], fresh_actor.id)]
        assert guild.create_calls == []
        assert "権限または機能設定" in interaction.followup.messages[-1]
        assert_owner_has_no_open_ticket(repository)
    finally:
        repository.close()


async def test_open_rechecks_fresh_bot_native_permission_before_create(tmp_path, monkeypatch) -> None:
    patch_discord_member(monkeypatch)
    repository = CommunityRepository(tmp_path / "community.sqlite3")
    repository.open()
    try:
        group, interaction, guild, _, _, _, _ = make_runtime(repository, fresh_bot_can_manage=False)

        await invoke_open(group, interaction)

        assert guild.create_calls == []
        assert "Botの権限" in interaction.followup.messages[-1]
        assert_owner_has_no_open_ticket(repository)
    finally:
        repository.close()


async def test_open_create_failure_compensates_unbound_ticket(tmp_path, monkeypatch) -> None:
    patch_discord_member(monkeypatch)
    repository = CommunityRepository(tmp_path / "community.sqlite3")
    repository.open()
    try:
        group, interaction, guild, _, _, _, _ = make_runtime(repository, fail_create=True)

        await invoke_open(group, interaction)

        assert len(guild.create_calls) == 1
        assert "チャンネル作成に失敗" in interaction.followup.messages[-1]
        assert_owner_has_no_open_ticket(repository)
    finally:
        repository.close()


async def test_open_uses_fresh_members_and_preserves_legitimate_creation(tmp_path, monkeypatch) -> None:
    patch_discord_member(monkeypatch)
    repository = CommunityRepository(tmp_path / "community.sqlite3")
    repository.open()
    try:
        group, interaction, guild, guard, cached_actor, fresh_actor, _ = make_runtime(repository)

        await invoke_open(group, interaction, "Need Help")

        assert guild.fetch_member_calls == [fresh_actor.id, guild.me.id]
        assert guard.calls == [(COMMAND_CAPABILITIES["ticket open"], fresh_actor.id)]
        assert len(guild.create_calls) == 1
        overwrites = guild.create_calls[0]["overwrites"]
        assert isinstance(overwrites, dict)
        assert fresh_actor in overwrites
        assert cached_actor not in overwrites
        ticket = repository.ticket_by_channel(guild.id, guild.created_channel.id)
        assert ticket is not None and ticket.status is TicketStatus.OPEN
        assert interaction.followup.messages == [f"チケットを作成しました: {guild.created_channel.mention}"]
    finally:
        repository.close()
