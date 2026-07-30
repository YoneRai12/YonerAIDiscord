from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from yonerai_discord.capabilities import EVENT_CAPABILITIES
from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.modules.community import views as community_views
from yonerai_discord.modules.community.views import PollView, SelfRoleView
from yonerai_discord.modules.operations import SafeInteractionView


class StubRepository:
    pass


class FakeGuard:
    def __init__(self, result: bool) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    async def check_capability(
        self,
        _: Any,
        capability_id: str,
        *,
        surface: str,
    ) -> bool:
        self.calls.append((capability_id, surface))
        return self.result

    async def actor(self, _: Any) -> object:
        return SimpleNamespace(level=RbacLevel.EVERYONE)

    def currently_allowed(self, capability_id: str, **_: Any) -> bool:
        return self.result


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, message: str, **_: Any) -> None:
        self.messages.append(message)


class BlockingGuard(FakeGuard):
    def __init__(self) -> None:
        super().__init__(True)
        self.actor_calls = 0
        self.second_check_started = asyncio.Event()
        self.release_second_check = asyncio.Event()

    async def actor(self, _: Any) -> object:
        self.actor_calls += 1
        if self.actor_calls == 2:
            self.second_check_started.set()
            await self.release_second_check.wait()
        return SimpleNamespace(level=RbacLevel.EVERYONE)


class CallbackRepository:
    def __init__(self, role_ids: tuple[int, ...] = ()) -> None:
        self.role_ids = role_ids
        self.votes = 0

    def vote(self, guild_id: int, poll_id: str, user_id: int, option_index: int) -> bool:
        self.votes += 1
        return True

    def selfroles(self, guild_id: int) -> tuple[int, ...]:
        return self.role_ids


class FakePermissions:
    def __init__(
        self,
        *,
        administrator: bool = False,
        manage_guild: bool = False,
        manage_roles: bool = False,
        manage_channels: bool = False,
        ban_members: bool = False,
        kick_members: bool = False,
        moderate_members: bool = False,
    ) -> None:
        self.administrator = administrator
        self.manage_guild = manage_guild
        self.manage_roles = manage_roles
        self.manage_channels = manage_channels
        self.ban_members = ban_members
        self.kick_members = kick_members
        self.moderate_members = moderate_members


class FakeRole:
    def __init__(
        self,
        role_id: int,
        name: str,
        position: int,
        *,
        permissions: FakePermissions | None = None,
    ) -> None:
        self.id = role_id
        self.name = name
        self.position = position
        self.managed = False
        self.permissions = permissions or FakePermissions()

    def is_default(self) -> bool:
        return False

    def __ge__(self, other: object) -> bool:
        return self.position >= getattr(other, "position")


class FakeMember:
    def __init__(
        self, member_id: int, *, roles: list[FakeRole] | None = None, top_role: FakeRole | None = None
    ) -> None:
        self.id = member_id
        self.roles = roles or []
        self.top_role = top_role
        self.role_changes = 0
        self.added_roles: list[FakeRole] = []
        self.removed_roles: list[FakeRole] = []

    async def add_roles(self, role: FakeRole, *, reason: str) -> None:
        self.role_changes += 1
        self.added_roles.append(role)

    async def remove_roles(self, role: FakeRole, *, reason: str) -> None:
        self.role_changes += 1
        self.removed_roles.append(role)


class FakeGuild:
    def __init__(
        self,
        role: FakeRole,
        bot: FakeMember,
        *,
        fresh_roles: list[FakeRole] | None = None,
        fresh_members: dict[int, FakeMember] | None = None,
    ) -> None:
        self.id = 42
        self.role = role
        self.me = bot
        self.fresh_roles = fresh_roles or [role]
        self.fresh_members = fresh_members or {bot.id: bot}
        self.fetch_roles_calls = 0

    def get_role(self, role_id: int) -> FakeRole | None:
        return self.role if role_id == self.role.id else None

    async def fetch_member(self, member_id: int) -> FakeMember:
        return self.fresh_members[member_id]

    async def fetch_roles(self) -> list[FakeRole]:
        self.fetch_roles_calls += 1
        return list(self.fresh_roles)


def test_persistent_custom_ids_are_stable_and_scoped() -> None:
    repository = StubRepository()
    poll = PollView(repository, "poll123", ("A", "B"))  # type: ignore[arg-type]
    assert isinstance(poll, SafeInteractionView)
    assert poll.timeout is None
    assert [item.custom_id for item in poll.children] == [
        "community:poll:poll123:0",
        "community:poll:poll123:1",
    ]

    roles = SelfRoleView(repository, 42, (100, 200))  # type: ignore[arg-type]
    assert isinstance(roles, SafeInteractionView)
    assert roles.timeout is None
    assert [item.custom_id for item in roles.children] == [
        "community:selfrole:42:100",
        "community:selfrole:42:200",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("view", "event_name"),
    [
        (PollView(StubRepository(), "poll123", ("A", "B")), "component.poll-vote"),  # type: ignore[arg-type]
        (SelfRoleView(StubRepository(), 42, (100,)), "component.selfrole-toggle"),  # type: ignore[arg-type]
    ],
)
async def test_persistent_components_use_central_capability_guard(view: Any, event_name: str) -> None:
    guard = FakeGuard(False)
    interaction = SimpleNamespace(client=SimpleNamespace(capability_guard=guard))

    assert not await view.interaction_check(interaction)
    assert guard.calls == [(EVENT_CAPABILITIES[event_name], event_name)]


@pytest.mark.asyncio
async def test_persistent_component_fails_closed_without_guard() -> None:
    response = FakeResponse()
    interaction = SimpleNamespace(client=SimpleNamespace(), response=response)
    view = PollView(StubRepository(), "poll123", ("A", "B"))  # type: ignore[arg-type]

    assert not await view.interaction_check(interaction)
    assert response.messages == ["Botの認可基盤が準備できていません。"]


@pytest.mark.asyncio
async def test_poll_vote_rechecks_after_wait_and_does_not_write_when_capability_turns_off(monkeypatch) -> None:
    monkeypatch.setattr(community_views.discord, "Member", FakeMember)
    repository = CallbackRepository()
    guard = BlockingGuard()
    user = FakeMember(100)
    response = FakeResponse()
    interaction = SimpleNamespace(
        guild_id=42,
        user=user,
        client=SimpleNamespace(capability_guard=guard),
        response=response,
    )
    view = PollView(repository, "poll123", ("A", "B"))  # type: ignore[arg-type]

    assert await view.interaction_check(interaction)
    task = asyncio.create_task(view.children[0].callback(interaction))
    await guard.second_check_started.wait()
    guard.result = False
    guard.release_second_check.set()
    await task

    assert repository.votes == 0
    assert response.messages == ["操作待機中に機能または権限が変更されたため、何も変更していません。"]


@pytest.mark.asyncio
async def test_selfrole_rechecks_after_wait_and_does_not_change_role_when_capability_turns_off(monkeypatch) -> None:
    monkeypatch.setattr(community_views.discord, "Member", FakeMember)
    role = FakeRole(500, "Member", 1)
    bot_role = FakeRole(999, "Bot", 10)
    bot = FakeMember(999, top_role=bot_role)
    guild = FakeGuild(role, bot)
    repository = CallbackRepository((role.id,))
    guard = BlockingGuard()
    user = FakeMember(100)
    response = FakeResponse()
    interaction = SimpleNamespace(
        guild_id=guild.id,
        guild=guild,
        user=user,
        client=SimpleNamespace(capability_guard=guard),
        response=response,
    )
    view = SelfRoleView(repository, guild.id, (role.id,))  # type: ignore[arg-type]

    assert await view.interaction_check(interaction)
    task = asyncio.create_task(view.children[0].callback(interaction))
    await guard.second_check_started.wait()
    guard.result = False
    guard.release_second_check.set()
    await task

    assert user.role_changes == 0
    assert response.messages == ["操作待機中に機能または権限が変更されたため、何も変更していません。"]


@pytest.mark.asyncio
async def test_selfrole_rejects_role_that_became_administrator_in_rest_state(monkeypatch) -> None:
    monkeypatch.setattr(community_views.discord, "Member", FakeMember)
    cached_role = FakeRole(500, "Member", 1)
    fresh_role = FakeRole(
        cached_role.id,
        "Administrator",
        1,
        permissions=FakePermissions(administrator=True),
    )
    fresh_bot_role = FakeRole(999, "Bot", 10, permissions=FakePermissions(manage_roles=True))
    cached_bot = FakeMember(999, roles=[fresh_bot_role], top_role=fresh_bot_role)
    fresh_bot = FakeMember(999, roles=[fresh_bot_role], top_role=fresh_bot_role)
    cached_user = FakeMember(100)
    fresh_user = FakeMember(100)
    guild = FakeGuild(
        cached_role,
        cached_bot,
        fresh_roles=[fresh_role, fresh_bot_role],
        fresh_members={cached_user.id: fresh_user, cached_bot.id: fresh_bot},
    )
    repository = CallbackRepository((cached_role.id,))
    response = FakeResponse()
    interaction = SimpleNamespace(
        guild_id=guild.id,
        guild=guild,
        user=cached_user,
        client=SimpleNamespace(capability_guard=FakeGuard(True)),
        response=response,
    )
    view = SelfRoleView(repository, guild.id, (cached_role.id,))  # type: ignore[arg-type]

    await view.children[0].callback(interaction)

    assert guild.fetch_roles_calls == 1
    assert cached_user.role_changes == 0
    assert fresh_user.role_changes == 0
    assert response.messages == ["このロールは権限が強すぎるため付与できません。"]


@pytest.mark.asyncio
async def test_selfrole_rejects_rest_role_that_moved_above_bot(monkeypatch) -> None:
    monkeypatch.setattr(community_views.discord, "Member", FakeMember)
    cached_role = FakeRole(500, "Member", 1)
    fresh_role = FakeRole(cached_role.id, "Member", 20)
    fresh_bot_role = FakeRole(999, "Bot", 10, permissions=FakePermissions(manage_roles=True))
    cached_bot = FakeMember(999, roles=[fresh_bot_role], top_role=fresh_bot_role)
    fresh_bot = FakeMember(999, roles=[fresh_bot_role], top_role=fresh_bot_role)
    cached_user = FakeMember(100)
    fresh_user = FakeMember(100)
    guild = FakeGuild(
        cached_role,
        cached_bot,
        fresh_roles=[fresh_role, fresh_bot_role],
        fresh_members={cached_user.id: fresh_user, cached_bot.id: fresh_bot},
    )
    interaction = SimpleNamespace(
        guild_id=guild.id,
        guild=guild,
        user=cached_user,
        client=SimpleNamespace(capability_guard=FakeGuard(True)),
        response=FakeResponse(),
    )
    view = SelfRoleView(CallbackRepository((cached_role.id,)), guild.id, (cached_role.id,))  # type: ignore[arg-type]

    await view.children[0].callback(interaction)

    assert fresh_user.role_changes == 0
    assert interaction.response.messages == ["Botがこのロールを操作できません。"]


@pytest.mark.asyncio
async def test_selfrole_uses_rest_fresh_member_and_role_for_legitimate_grant(monkeypatch) -> None:
    monkeypatch.setattr(community_views.discord, "Member", FakeMember)
    cached_role = FakeRole(500, "Old name", 1)
    fresh_role = FakeRole(cached_role.id, "Member", 1)
    fresh_bot_role = FakeRole(999, "Bot", 10, permissions=FakePermissions(manage_roles=True))
    cached_bot = FakeMember(999, roles=[fresh_bot_role], top_role=fresh_bot_role)
    fresh_bot = FakeMember(999, roles=[fresh_bot_role], top_role=fresh_bot_role)
    cached_user = FakeMember(100)
    fresh_user = FakeMember(100)
    guild = FakeGuild(
        cached_role,
        cached_bot,
        fresh_roles=[fresh_role, fresh_bot_role],
        fresh_members={cached_user.id: fresh_user, cached_bot.id: fresh_bot},
    )
    interaction = SimpleNamespace(
        guild_id=guild.id,
        guild=guild,
        user=cached_user,
        client=SimpleNamespace(capability_guard=FakeGuard(True)),
        response=FakeResponse(),
    )
    view = SelfRoleView(CallbackRepository((cached_role.id,)), guild.id, (cached_role.id,))  # type: ignore[arg-type]

    await view.children[0].callback(interaction)

    assert cached_user.role_changes == 0
    assert fresh_user.added_roles == [fresh_role]
    assert interaction.response.messages == ["Member を付与しました。"]


@pytest.mark.asyncio
async def test_selfrole_uses_rest_fresh_member_and_role_for_legitimate_remove(monkeypatch) -> None:
    monkeypatch.setattr(community_views.discord, "Member", FakeMember)
    cached_role = FakeRole(500, "Old name", 1)
    fresh_role = FakeRole(cached_role.id, "Member", 1)
    fresh_bot_role = FakeRole(999, "Bot", 10, permissions=FakePermissions(manage_roles=True))
    cached_bot = FakeMember(999, roles=[fresh_bot_role], top_role=fresh_bot_role)
    fresh_bot = FakeMember(999, roles=[fresh_bot_role], top_role=fresh_bot_role)
    cached_user = FakeMember(100)
    fresh_user = FakeMember(100, roles=[fresh_role])
    guild = FakeGuild(
        cached_role,
        cached_bot,
        fresh_roles=[fresh_role, fresh_bot_role],
        fresh_members={cached_user.id: fresh_user, cached_bot.id: fresh_bot},
    )
    interaction = SimpleNamespace(
        guild_id=guild.id,
        guild=guild,
        user=cached_user,
        client=SimpleNamespace(capability_guard=FakeGuard(True)),
        response=FakeResponse(),
    )
    view = SelfRoleView(CallbackRepository((cached_role.id,)), guild.id, (cached_role.id,))  # type: ignore[arg-type]

    await view.children[0].callback(interaction)

    assert cached_user.role_changes == 0
    assert fresh_user.removed_roles == [fresh_role]
    assert interaction.response.messages == ["Member を外しました。"]
