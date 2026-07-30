from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from yonerai_discord.modules.servertools import adapter
from yonerai_discord.modules.servertools.adapter import ServerGroup
from yonerai_discord.modules.servertools.domain import GuildServerConfig


DENIED = "現在の機能設定・権限・対象を再確認できないため、何も変更していません。"


def permissions(**overrides: bool) -> SimpleNamespace:
    values = {
        "administrator": False,
        "manage_guild": False,
        "manage_channels": False,
        "manage_nicknames": False,
        "manage_roles": False,
        "send_messages": False,
        "mention_everyone": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeRole:
    def __init__(
        self,
        role_id: int,
        position: int,
        *,
        name: str = "role",
        managed: bool = False,
        default: bool = False,
    ) -> None:
        self.id = role_id
        self.position = position
        self.name = name
        self.managed = managed
        self._default = default

    def is_default(self) -> bool:
        return self._default


class FakeMember:
    def __init__(
        self,
        member_id: int,
        position: int,
        guild_permissions: SimpleNamespace,
    ) -> None:
        self.id = member_id
        self.top_role = SimpleNamespace(position=position)
        self.guild_permissions = guild_permissions
        self.edits: list[dict[str, object]] = []
        self.added_roles: list[FakeRole] = []
        self.removed_roles: list[FakeRole] = []

    async def edit(self, **kwargs: object) -> None:
        self.edits.append(kwargs)

    async def add_roles(self, role: FakeRole, **kwargs: object) -> None:
        self.added_roles.append(role)

    async def remove_roles(self, role: FakeRole, **kwargs: object) -> None:
        self.removed_roles.append(role)

    def __str__(self) -> str:
        return f"member-{self.id}"


class FakeChannel:
    def __init__(self, channel_id: int, *, name: str = "general") -> None:
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"
        self.edits: list[dict[str, object]] = []
        self.permission_edits: list[tuple[FakeRole, object, str]] = []
        self.messages: list[str] = []

    def permissions_for(self, member: FakeMember) -> SimpleNamespace:
        return member.guild_permissions

    def overwrites_for(self, role: FakeRole) -> SimpleNamespace:
        return SimpleNamespace(send_messages=None)

    async def edit(self, **kwargs: object) -> None:
        self.edits.append(kwargs)

    async def set_permissions(
        self,
        role: FakeRole,
        *,
        overwrite: object,
        reason: str,
    ) -> None:
        self.permission_edits.append((role, overwrite, reason))

    async def send(self, message: str, **kwargs: object) -> None:
        self.messages.append(message)


class Response:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, message: str, **kwargs: object) -> None:
        self.messages.append(message)


class Registry:
    def __init__(self) -> None:
        self.enabled = True
        self.calls: list[tuple[str, int]] = []

    def is_capability_enabled(self, capability_id: str, guild_id: int) -> bool:
        self.calls.append((capability_id, guild_id))
        return self.enabled


class Guard:
    def __init__(self) -> None:
        self.allowed = True
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None
        self.calls: list[tuple[str, FakeMember]] = []

    async def evaluate_fresh_member(
        self,
        capability_id: str,
        *,
        guild: object,
        member: FakeMember,
    ) -> object:
        self.calls.append((capability_id, member))
        if self.entered is not None and self.release is not None:
            self.entered.set()
            await self.release.wait()
        return SimpleNamespace(allowed=self.allowed)


class Repository:
    def __init__(self, *, log_channel_id: int | None = None) -> None:
        self.log_channel_id = log_channel_id

    def get(self, guild_id: int) -> GuildServerConfig:
        return GuildServerConfig(guild_id=guild_id, log_channel_id=self.log_channel_id)


class Guild:
    def __init__(
        self,
        *,
        actor: FakeMember,
        bot_member: FakeMember,
        target: FakeMember,
        channel: FakeChannel,
        role: FakeRole,
    ) -> None:
        self.id = 123
        self.owner_id = 9999
        self.default_role = FakeRole(1, 0, name="@everyone", default=True)
        self.me = bot_member
        self.members = {actor.id: actor, bot_member.id: bot_member, target.id: target}
        self.channels = {channel.id: channel}
        self.roles = [self.default_role, role]
        self._blocks: dict[tuple[str, int], tuple[asyncio.Event, asyncio.Event]] = {}

    def block(self, kind: str, item_id: int = 0) -> tuple[asyncio.Event, asyncio.Event]:
        entered = asyncio.Event()
        release = asyncio.Event()
        self._blocks[(kind, item_id)] = (entered, release)
        return entered, release

    async def _wait(self, kind: str, item_id: int = 0) -> None:
        block = self._blocks.get((kind, item_id))
        if block is None:
            return
        entered, release = block
        entered.set()
        await release.wait()

    async def fetch_channel(self, channel_id: int) -> FakeChannel:
        await self._wait("channel", channel_id)
        return self.channels[channel_id]

    async def fetch_member(self, member_id: int) -> FakeMember:
        await self._wait("member", member_id)
        return self.members[member_id]

    async def fetch_roles(self) -> list[FakeRole]:
        await self._wait("roles")
        return list(self.roles)


@pytest.fixture(autouse=True)
def fake_discord_types(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter.discord, "TextChannel", FakeChannel)
    monkeypatch.setattr(adapter.discord, "Member", FakeMember)
    monkeypatch.setattr(adapter.discord, "Role", FakeRole)


def setup_case() -> tuple[
    ServerGroup,
    SimpleNamespace,
    Guild,
    FakeMember,
    FakeMember,
    FakeChannel,
    FakeRole,
    Registry,
    Guard,
]:
    actor = FakeMember(
        10,
        90,
        permissions(
            administrator=True,
            manage_guild=True,
            manage_channels=True,
            manage_nicknames=True,
            manage_roles=True,
        ),
    )
    bot_member = FakeMember(
        20,
        100,
        permissions(
            administrator=True,
            manage_guild=True,
            manage_channels=True,
            manage_nicknames=True,
            manage_roles=True,
            send_messages=True,
            mention_everyone=True,
        ),
    )
    target = FakeMember(30, 10, permissions())
    channel = FakeChannel(40)
    role = FakeRole(50, 5, name="member")
    guild = Guild(
        actor=actor,
        bot_member=bot_member,
        target=target,
        channel=channel,
        role=role,
    )
    registry = Registry()
    guard = Guard()
    bot = SimpleNamespace(
        user=SimpleNamespace(id=bot_member.id),
        capability_registry=registry,
        capability_guard=guard,
    )
    group = ServerGroup(Repository(), bot)  # type: ignore[arg-type]
    interaction = SimpleNamespace(
        guild_id=guild.id,
        guild=guild,
        user=actor,
        channel=channel,
        response=Response(),
    )
    return group, interaction, guild, actor, target, channel, role, registry, guard


async def wait_until(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=1)


def assert_denied(interaction: SimpleNamespace) -> None:
    assert interaction.response.messages == [DENIED]


async def test_slowmode_capability_off_during_channel_refresh_has_zero_effects() -> None:
    group, interaction, guild, _, _, channel, _, registry, _ = setup_case()
    entered, release = guild.block("channel", channel.id)

    task = asyncio.create_task(group.slowmode.callback(group, interaction, 15, channel))
    await wait_until(entered)
    registry.enabled = False
    release.set()
    await task

    assert channel.edits == []
    assert_denied(interaction)


async def test_lock_actor_permission_downgrade_during_refresh_has_zero_effects() -> None:
    group, interaction, guild, actor, _, channel, _, _, _ = setup_case()
    entered, release = guild.block("member", actor.id)

    task = asyncio.create_task(group.lock.callback(group, interaction, channel))
    await wait_until(entered)
    guild.members[actor.id] = FakeMember(actor.id, 90, permissions())
    release.set()
    await task

    assert channel.permission_edits == []
    assert_denied(interaction)


async def test_nick_target_hierarchy_change_during_refresh_has_zero_effects() -> None:
    group, interaction, guild, _, target, _, _, _, _ = setup_case()
    entered, release = guild.block("member", target.id)

    task = asyncio.create_task(group.nick.callback(group, interaction, target, "new-name"))
    await wait_until(entered)
    raised_target = FakeMember(target.id, 95, permissions())
    guild.members[target.id] = raised_target
    release.set()
    await task

    assert target.edits == []
    assert raised_target.edits == []
    assert_denied(interaction)


async def test_role_add_managed_change_during_refresh_has_zero_effects() -> None:
    group, interaction, guild, _, target, _, role, _, _ = setup_case()
    entered, release = guild.block("roles")

    task = asyncio.create_task(group.role_add.callback(group, interaction, target, role))
    await wait_until(entered)
    guild.roles = [guild.default_role, FakeRole(role.id, role.position, managed=True)]
    release.set()
    await task

    assert target.added_roles == []
    assert_denied(interaction)


async def test_role_remove_hierarchy_change_during_refresh_has_zero_effects() -> None:
    group, interaction, guild, _, target, _, role, _, _ = setup_case()
    entered, release = guild.block("roles")

    task = asyncio.create_task(group.role_remove.callback(group, interaction, target, role))
    await wait_until(entered)
    guild.roles = [guild.default_role, FakeRole(role.id, 95)]
    release.set()
    await task

    assert target.removed_roles == []
    assert_denied(interaction)


async def test_announce_actor_permission_downgrade_during_refresh_has_zero_effects() -> None:
    group, interaction, guild, actor, _, channel, _, _, _ = setup_case()
    entered, release = guild.block("member", actor.id)

    task = asyncio.create_task(group.announce.callback(group, interaction, channel, "maintenance", False, ""))
    await wait_until(entered)
    guild.members[actor.id] = FakeMember(actor.id, 90, permissions())
    release.set()
    await task

    assert channel.messages == []
    assert_denied(interaction)


async def test_dynamic_required_level_change_while_guard_waits_has_zero_effects() -> None:
    group, interaction, _, _, _, channel, _, _, guard = setup_case()
    guard.entered = asyncio.Event()
    guard.release = asyncio.Event()

    task = asyncio.create_task(group.slowmode.callback(group, interaction, 15, channel))
    await wait_until(guard.entered)
    guard.allowed = False
    guard.release.set()
    await task

    assert channel.edits == []
    assert_denied(interaction)


async def test_success_uses_fresh_channel_instead_of_cached_channel() -> None:
    group, interaction, guild, _, _, cached_channel, _, _, _ = setup_case()
    fresh_channel = FakeChannel(cached_channel.id)
    guild.channels[cached_channel.id] = fresh_channel

    await group.slowmode.callback(group, interaction, 20, cached_channel)

    assert cached_channel.edits == []
    assert fresh_channel.edits[0]["slowmode_delay"] == 20
    assert interaction.response.messages == [f"{fresh_channel.mention} の低速モードを 20 秒に設定しました。"]
