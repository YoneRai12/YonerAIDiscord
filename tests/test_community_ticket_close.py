from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from types import SimpleNamespace

import discord

from yonerai_discord.modules.community import ActorPolicy, CommunityRepository, Ticket, TicketStatus
from yonerai_discord.modules.community import commands as community_commands
from yonerai_discord.modules.community.commands import TicketGroup


class SyntheticHTTPException(discord.HTTPException):
    def __init__(self) -> None:
        Exception.__init__(self, "synthetic Discord failure")


@dataclass(frozen=True)
class FakeRole:
    id: int


@dataclass(frozen=True)
class FakeMember:
    id: int


class FakeChannel:
    def __init__(
        self,
        channel_id: int,
        name: str,
        overwrites: dict[object, discord.PermissionOverwrite],
        *,
        manage_channels: bool = True,
        fail_calls: set[int] | None = None,
        edit_started: asyncio.Event | None = None,
        edit_release: asyncio.Event | None = None,
    ) -> None:
        self.id = channel_id
        self.name = name
        self.overwrites = overwrites
        self.manage_channels = manage_channels
        self.fail_calls = fail_calls or set()
        self.edits: list[dict[str, object]] = []
        self.permission_edits: list[tuple[object, object]] = []
        self.edit_started = edit_started
        self.edit_release = edit_release

    def permissions_for(self, _member: object) -> SimpleNamespace:
        return SimpleNamespace(manage_channels=self.manage_channels)

    async def edit(self, **kwargs: object) -> None:
        call_number = len(self.edits) + 1
        self.edits.append(kwargs)
        if self.edit_started is not None:
            self.edit_started.set()
        if self.edit_release is not None:
            await self.edit_release.wait()
        await asyncio.sleep(0)
        if call_number in self.fail_calls:
            raise SyntheticHTTPException()
        self.name = str(kwargs["name"])
        self.overwrites = dict(kwargs["overwrites"])  # type: ignore[arg-type]

    async def set_permissions(self, member: object, *, overwrite: object, reason: str) -> None:
        self.permission_edits.append((member, overwrite))


class FakeGuild:
    def __init__(self, default_role: FakeRole, bot: FakeMember, members: tuple[FakeMember, ...]) -> None:
        self.id = 1
        self.owner_id = 999
        self.default_role = default_role
        self.me = bot
        self._members = {member.id: member for member in members}
        self.channel: FakeChannel | None = None

    def get_member(self, member_id: int) -> FakeMember | None:
        return self._members.get(member_id)

    async def fetch_member(self, member_id: int) -> FakeMember:
        member = self.get_member(member_id)
        if member is None:
            raise AssertionError("unexpected missing member")
        return member

    async def fetch_channel(self, channel_id: int) -> FakeChannel:
        assert self.channel is not None and self.channel.id == channel_id
        return self.channel


class Guard:
    def __init__(self, decisions: list[bool] | None = None) -> None:
        self.decisions = decisions or [True]
        self.calls: list[str] = []

    async def evaluate_fresh_member(self, capability_id: str, *, guild: object, member: object) -> object:
        self.calls.append(capability_id)
        allowed = self.decisions.pop(0) if len(self.decisions) > 1 else self.decisions[0]
        return SimpleNamespace(allowed=allowed)


def ticket_group(repository: CommunityRepository, guard: Guard | None = None) -> TicketGroup:
    return TicketGroup(repository, SimpleNamespace(capability_guard=guard or Guard()))


class Response:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.deferred = False

    async def send_message(self, message: str, **_kwargs: object) -> None:
        self.messages.append(message)

    async def defer(self, **_kwargs: object) -> None:
        self.deferred = True


class Followup:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str, **_kwargs: object) -> None:
        self.messages.append(message)


def make_interaction(guild: FakeGuild, channel: object, actor: FakeMember) -> SimpleNamespace:
    if isinstance(channel, FakeChannel):
        guild.channel = channel
    return SimpleNamespace(
        guild_id=1,
        channel_id=getattr(channel, "id", 100),
        guild=guild,
        channel=channel,
        user=actor,
        response=Response(),
        followup=Followup(),
    )


async def invoke_close(group: TicketGroup, interaction: object) -> None:
    await TicketGroup.close.callback(group, interaction)  # type: ignore[arg-type]


async def invoke_add(group: TicketGroup, interaction: object, member: FakeMember) -> None:
    await TicketGroup.add.callback(group, interaction, member)  # type: ignore[arg-type]


def setup_ticket(repository: CommunityRepository) -> None:
    assert repository.create_ticket(Ticket("ticket-1", 1, 10, "help"))
    assert repository.bind_ticket_channel(1, "ticket-1", 100)
    assert repository.add_ticket_participant(1, "ticket-1", 20)


def patch_discord_types(monkeypatch) -> None:
    monkeypatch.setattr(community_commands.discord, "TextChannel", FakeChannel)
    monkeypatch.setattr(community_commands.discord, "Member", FakeMember)
    monkeypatch.setattr(
        community_commands,
        "actor_policy",
        lambda interaction: ActorPolicy(interaction.user.id, guild_owner_id=999),
    )
    monkeypatch.setattr(
        community_commands,
        "member_policy",
        lambda _guild, member: ActorPolicy(member.id, guild_owner_id=999),
    )


async def test_close_makes_owner_and_participant_read_only_before_db_commit(tmp_path, monkeypatch) -> None:
    patch_discord_types(monkeypatch)
    repository = CommunityRepository(tmp_path / "community.sqlite3")
    repository.open()
    try:
        setup_ticket(repository)
        default, support = FakeRole(1), FakeRole(2)
        owner, participant, bot = FakeMember(10), FakeMember(20), FakeMember(999)
        original = {
            default: discord.PermissionOverwrite(view_channel=False),
            support: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            owner: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            participant: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            bot: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        channel = FakeChannel(100, "ticket-help", original)
        guild = FakeGuild(default, bot, (owner, participant, bot))
        interaction = make_interaction(guild, channel, owner)

        await invoke_close(ticket_group(repository), interaction)

        assert len(channel.edits) == 1
        assert channel.name == "closed-ticket-help"
        assert channel.overwrites[default].send_messages is False
        assert channel.overwrites[support].send_messages is False
        assert channel.overwrites[owner].send_messages is False
        assert channel.overwrites[owner].send_messages_in_threads is False
        assert channel.overwrites[participant].send_messages is False
        assert channel.overwrites[bot].send_messages is True
        assert repository.ticket_by_channel(1, 100).status is TicketStatus.CLOSED  # type: ignore[union-attr]
        assert interaction.followup.messages == ["チケットを閉じました。"]
    finally:
        repository.close()


async def test_discord_failure_keeps_db_open(tmp_path, monkeypatch) -> None:
    patch_discord_types(monkeypatch)
    repository = CommunityRepository(tmp_path / "community.sqlite3")
    repository.open()
    try:
        setup_ticket(repository)
        default = FakeRole(1)
        owner, participant, bot = FakeMember(10), FakeMember(20), FakeMember(999)
        channel = FakeChannel(
            100,
            "ticket-help",
            {default: discord.PermissionOverwrite(), owner: discord.PermissionOverwrite(send_messages=True)},
            fail_calls={1},
        )
        interaction = make_interaction(FakeGuild(default, bot, (owner, participant, bot)), channel, owner)

        await invoke_close(ticket_group(repository), interaction)

        assert repository.ticket_by_channel(1, 100).status is TicketStatus.OPEN  # type: ignore[union-attr]
        assert len(channel.edits) == 1
        assert "閉じていません" in interaction.followup.messages[-1]
    finally:
        repository.close()


async def test_db_failure_restores_channel_and_keeps_ticket_open(tmp_path, monkeypatch) -> None:
    patch_discord_types(monkeypatch)
    repository = CommunityRepository(tmp_path / "community.sqlite3")
    repository.open()
    try:
        setup_ticket(repository)
        default = FakeRole(1)
        owner, participant, bot = FakeMember(10), FakeMember(20), FakeMember(999)
        original = {default: discord.PermissionOverwrite(), owner: discord.PermissionOverwrite(send_messages=True)}
        channel = FakeChannel(100, "ticket-help", original)
        interaction = make_interaction(FakeGuild(default, bot, (owner, participant, bot)), channel, owner)

        monkeypatch.setattr(
            repository,
            "close_ticket",
            lambda _guild_id, _ticket_id: (_ for _ in ()).throw(sqlite3.OperationalError("synthetic")),
        )
        await invoke_close(ticket_group(repository), interaction)

        assert len(channel.edits) == 2
        assert channel.name == "ticket-help"
        assert channel.overwrites[owner].send_messages is True
        assert repository.ticket_by_channel(1, 100).status is TicketStatus.OPEN  # type: ignore[union-attr]
        assert "元に戻しました" in interaction.followup.messages[-1]
        assert "開いたまま" in interaction.followup.messages[-1]
    finally:
        repository.close()


async def test_db_and_rollback_failure_reports_uncertain_state(tmp_path, monkeypatch) -> None:
    patch_discord_types(monkeypatch)
    repository = CommunityRepository(tmp_path / "community.sqlite3")
    repository.open()
    try:
        setup_ticket(repository)
        default = FakeRole(1)
        owner, participant, bot = FakeMember(10), FakeMember(20), FakeMember(999)
        channel = FakeChannel(
            100,
            "ticket-help",
            {default: discord.PermissionOverwrite(), owner: discord.PermissionOverwrite(send_messages=True)},
            fail_calls={2},
        )
        interaction = make_interaction(FakeGuild(default, bot, (owner, participant, bot)), channel, owner)
        monkeypatch.setattr(repository, "close_ticket", lambda _guild_id, _ticket_id: False)

        await invoke_close(ticket_group(repository), interaction)

        assert len(channel.edits) == 2
        assert channel.name == "closed-ticket-help"
        assert repository.ticket_by_channel(1, 100).status is TicketStatus.OPEN  # type: ignore[union-attr]
        assert "確認が必要" in interaction.followup.messages[-1]
    finally:
        repository.close()


async def test_concurrent_close_only_mutates_discord_once(tmp_path, monkeypatch) -> None:
    patch_discord_types(monkeypatch)
    repository = CommunityRepository(tmp_path / "community.sqlite3")
    repository.open()
    try:
        setup_ticket(repository)
        default = FakeRole(1)
        owner, participant, bot = FakeMember(10), FakeMember(20), FakeMember(999)
        channel = FakeChannel(
            100,
            "ticket-help",
            {default: discord.PermissionOverwrite(), owner: discord.PermissionOverwrite(send_messages=True)},
        )
        guild = FakeGuild(default, bot, (owner, participant, bot))
        first = make_interaction(guild, channel, owner)
        second = make_interaction(guild, channel, owner)
        group = ticket_group(repository)

        await asyncio.gather(invoke_close(group, first), invoke_close(group, second))

        assert len(channel.edits) == 1
        messages = first.followup.messages + second.followup.messages
        assert "チケットを閉じました。" in messages
        assert any("状態が変更" in message for message in messages)
        assert not group._close_locks
    finally:
        repository.close()


async def test_missing_manage_channels_permission_fails_before_defer(tmp_path, monkeypatch) -> None:
    patch_discord_types(monkeypatch)
    repository = CommunityRepository(tmp_path / "community.sqlite3")
    repository.open()
    try:
        setup_ticket(repository)
        default = FakeRole(1)
        owner, participant, bot = FakeMember(10), FakeMember(20), FakeMember(999)
        channel = FakeChannel(100, "ticket-help", {}, manage_channels=False)
        interaction = make_interaction(FakeGuild(default, bot, (owner, participant, bot)), channel, owner)

        await invoke_close(ticket_group(repository), interaction)

        assert interaction.response.deferred is False
        assert not channel.edits
        assert repository.ticket_by_channel(1, 100).status is TicketStatus.OPEN  # type: ignore[union-attr]
        assert "権限がない" in interaction.response.messages[-1]
    finally:
        repository.close()


async def test_actor_permission_is_refetched_after_wait(tmp_path, monkeypatch) -> None:
    patch_discord_types(monkeypatch)
    repository = CommunityRepository(tmp_path / "community.sqlite3")
    repository.open()
    try:
        setup_ticket(repository)
        default = FakeRole(1)
        owner, participant, moderator, bot = FakeMember(10), FakeMember(20), FakeMember(30), FakeMember(999)
        channel = FakeChannel(100, "ticket-help", {})
        guild = FakeGuild(default, bot, (owner, participant, moderator, bot))
        interaction = make_interaction(guild, channel, moderator)
        monkeypatch.setattr(
            community_commands,
            "actor_policy",
            lambda _interaction: ActorPolicy(30, guild_owner_id=999, manage_channels=True),
        )
        monkeypatch.setattr(
            community_commands,
            "member_policy",
            lambda _guild, _member: ActorPolicy(30, guild_owner_id=999),
        )

        await invoke_close(ticket_group(repository), interaction)

        assert not channel.edits
        assert repository.ticket_by_channel(1, 100).status is TicketStatus.OPEN  # type: ignore[union-attr]
        assert "権限が変更" in interaction.followup.messages[-1]
    finally:
        repository.close()


async def test_dynamic_rbac_flip_stops_immediately_before_channel_edit(tmp_path, monkeypatch) -> None:
    patch_discord_types(monkeypatch)
    repository = CommunityRepository(tmp_path / "community.sqlite3")
    repository.open()
    try:
        setup_ticket(repository)
        default = FakeRole(1)
        owner, participant, bot = FakeMember(10), FakeMember(20), FakeMember(999)
        channel = FakeChannel(100, "ticket-help", {})
        interaction = make_interaction(FakeGuild(default, bot, (owner, participant, bot)), channel, owner)
        guard = Guard([False])

        await invoke_close(ticket_group(repository, guard), interaction)

        assert guard.calls == ["cap-run-ticket-close"]
        assert not channel.edits
        assert repository.ticket_by_channel(1, 100).status is TicketStatus.OPEN  # type: ignore[union-attr]
        assert "必要権限" in interaction.followup.messages[-1]
    finally:
        repository.close()


async def test_initial_ticket_lookup_failure_has_fixed_response_and_no_discord_change(tmp_path, monkeypatch) -> None:
    patch_discord_types(monkeypatch)
    repository = CommunityRepository(tmp_path / "community.sqlite3")
    repository.open()
    try:
        default = FakeRole(1)
        owner, bot = FakeMember(10), FakeMember(999)
        channel = FakeChannel(100, "ticket-help", {})
        interaction = make_interaction(FakeGuild(default, bot, (owner, bot)), channel, owner)
        monkeypatch.setattr(
            repository,
            "ticket_by_channel",
            lambda _guild_id, _channel_id: (_ for _ in ()).throw(sqlite3.OperationalError("synthetic")),
        )

        await invoke_close(ticket_group(repository), interaction)

        assert not channel.edits
        assert interaction.response.messages == ["チケットの現在状態を確認できないため、何も変更していません。"]
    finally:
        repository.close()


async def test_close_and_add_share_ticket_lock_so_add_cannot_reopen_closed_permissions(tmp_path, monkeypatch) -> None:
    patch_discord_types(monkeypatch)
    repository = CommunityRepository(tmp_path / "community.sqlite3")
    repository.open()
    try:
        setup_ticket(repository)
        default = FakeRole(1)
        owner, participant, newcomer, bot = (
            FakeMember(10),
            FakeMember(20),
            FakeMember(30),
            FakeMember(999),
        )
        edit_started = asyncio.Event()
        edit_release = asyncio.Event()
        channel = FakeChannel(
            100,
            "ticket-help",
            {},
            edit_started=edit_started,
            edit_release=edit_release,
        )
        guild = FakeGuild(default, bot, (owner, participant, newcomer, bot))
        close_interaction = make_interaction(guild, channel, owner)
        add_interaction = make_interaction(guild, channel, owner)
        group = ticket_group(repository)

        close_task = asyncio.create_task(invoke_close(group, close_interaction))
        await edit_started.wait()
        add_task = asyncio.create_task(invoke_add(group, add_interaction, newcomer))
        await asyncio.sleep(0)
        assert not add_task.done()

        edit_release.set()
        await asyncio.gather(close_task, add_task)

        assert repository.ticket_by_channel(1, 100).status is TicketStatus.CLOSED  # type: ignore[union-attr]
        assert channel.permission_edits == []
        assert "閉じられた" in add_interaction.followup.messages[-1]
        assert not group._close_locks
    finally:
        repository.close()
