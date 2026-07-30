from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from yonerai_discord.control_plane import (
    ActorContext,
    CapabilitySpec,
    ModuleSpec,
    PolicyEngine,
    RbacLevel,
    Registry,
)
from yonerai_discord.modules.ai import capability_rag
from yonerai_discord.modules.ai.capability_rag import (
    MAX_AUTHORIZATION_CANDIDATES,
    DiscordCapabilityProjection,
    authorized_capability_ids_for_discord_actor,
    project_authorized_capabilities_for_discord_actor,
)


GUILD_ID = 101
CHANNEL_ID = 202
USER_ID = 303
TRUSTED_ROLE_ID = 404

EVERYONE_ID = "cap.test.everyone"
TRUSTED_ID = "cap.test.trusted"
RUNTIME_ID = "cap.test.runtime"
UNKNOWN_ID = "cap.test.unknown"


class _Policy:
    def __init__(self, registry: Registry) -> None:
        self._inner = PolicyEngine(registry)
        self.calls: list[str] = []
        self.raise_ids: set[str] = set()

    def evaluate(self, capability_id: str, actor: ActorContext) -> object:
        self.calls.append(capability_id)
        if capability_id in self.raise_ids:
            raise RuntimeError("policy unavailable")
        return self._inner.evaluate(capability_id, actor)


class _Guard:
    def __init__(self, registry: Registry, *, trusted: bool = False) -> None:
        self.registry = registry
        self.policy = _Policy(registry)
        self.settings = SimpleNamespace(
            bot_owner_ids=frozenset(),
            moderator_role_ids=frozenset(),
            trusted_role_ids=frozenset({TRUSTED_ROLE_ID}) if trusted else frozenset(),
        )
        self.bot = SimpleNamespace(is_closing=False, owner_checks=0)

        async def is_owner(_member: object) -> bool:
            self.bot.owner_checks += 1
            return False

        self.bot.is_owner = is_owner
        self.current_calls: list[str] = []
        self.currently_denied: set[str] = set()
        self.currently_raises: set[str] = set()

    def currently_allowed(
        self,
        capability_id: str,
        *,
        guild_id: int,
        user_id: int,
        actor_level: RbacLevel,
        floor: RbacLevel,
    ) -> bool:
        self.current_calls.append(capability_id)
        if capability_id in self.currently_raises:
            raise RuntimeError("current authorization unavailable")
        if capability_id in self.currently_denied or self.bot.is_closing:
            return False
        return (
            guild_id == GUILD_ID
            and user_id == USER_ID
            and actor_level >= floor
            and self.registry.capability_status(capability_id, guild_id).executable
            and actor_level >= self.registry.required_level(capability_id, guild_id)
        )


def _registry() -> Registry:
    registry = Registry()
    registry.register_module(ModuleSpec("module.test"))
    registry.register_capability(
        CapabilitySpec(
            EVERYONE_ID,
            "module.test",
            required_level=RbacLevel.EVERYONE,
        )
    )
    registry.register_capability(
        CapabilitySpec(
            TRUSTED_ID,
            "module.test",
            required_level=RbacLevel.TRUSTED,
        )
    )
    registry.register_capability(
        CapabilitySpec(
            RUNTIME_ID,
            "module.test",
            required_level=RbacLevel.EVERYONE,
        )
    )
    return registry


def _environment(
    *,
    trusted: bool = False,
    view_channel: bool = True,
    read_message_history: bool = True,
) -> tuple[_Guard, object, object]:
    registry = _registry()
    guard = _Guard(registry, trusted=trusted)
    guild = SimpleNamespace(id=GUILD_ID, owner_id=999, fetch_count=0)
    roles = (SimpleNamespace(id=TRUSTED_ROLE_ID),) if trusted else ()
    member = SimpleNamespace(
        id=USER_ID,
        bot=False,
        guild=guild,
        roles=roles,
        guild_permissions=SimpleNamespace(
            administrator=False,
            manage_guild=False,
            moderate_members=False,
            manage_messages=False,
            kick_members=False,
            ban_members=False,
        ),
    )

    async def fetch_member(user_id: int) -> object:
        guild.fetch_count += 1
        assert user_id == USER_ID
        return member

    guild.fetch_member = fetch_member
    channel = SimpleNamespace(
        id=CHANNEL_ID,
        guild=guild,
        permission_checks=0,
    )

    def permissions_for(subject: object) -> object:
        channel.permission_checks += 1
        assert subject is member
        return SimpleNamespace(
            view_channel=view_channel,
            read_message_history=read_message_history,
        )

    channel.permissions_for = permissions_for
    return guard, guild, channel


@pytest.mark.asyncio
async def test_projection_resolves_one_fresh_member_and_one_actor_context(monkeypatch: pytest.MonkeyPatch) -> None:
    guard, guild, channel = _environment(trusted=True)
    actor_resolutions = 0
    original = capability_rag.actor_context_for_member

    async def counted_actor_context(**kwargs: Any) -> ActorContext:
        nonlocal actor_resolutions
        actor_resolutions += 1
        return await original(**kwargs)

    monkeypatch.setattr(capability_rag, "actor_context_for_member", counted_actor_context)
    projection = await project_authorized_capabilities_for_discord_actor(
        guard=guard,
        guild=guild,
        channel=channel,
        user_id=USER_ID,
        capability_ids=(TRUSTED_ID, EVERYONE_ID),
    )

    assert projection == DiscordCapabilityProjection(
        frozenset({EVERYONE_ID, TRUSTED_ID}),
        RbacLevel.TRUSTED,
    )
    assert guild.fetch_count == 1
    assert channel.permission_checks == 1
    assert actor_resolutions == 1
    assert guard.bot.owner_checks == 1
    assert str(GUILD_ID) not in repr(projection)
    assert str(USER_ID) not in repr(projection)
    assert str(TRUSTED_ROLE_ID) not in repr(projection)


@pytest.mark.asyncio
async def test_projection_applies_policy_rbac_and_caller_floor_to_same_actor() -> None:
    guard, guild, channel = _environment(trusted=True)
    projection = await project_authorized_capabilities_for_discord_actor(
        guard=guard,
        guild=guild,
        channel=channel,
        user_id=USER_ID,
        capability_ids=(EVERYONE_ID, TRUSTED_ID),
        minimum_levels={
            EVERYONE_ID: RbacLevel.MODERATOR,
            TRUSTED_ID: RbacLevel.TRUSTED,
        },
    )

    assert projection.actor_level is RbacLevel.TRUSTED
    assert projection.allowed_capability_ids == frozenset({TRUSTED_ID})

    everyone_guard, everyone_guild, everyone_channel = _environment()
    assert await authorized_capability_ids_for_discord_actor(
        guard=everyone_guard,
        guild=everyone_guild,
        channel=everyone_channel,
        user_id=USER_ID,
        capability_ids=(EVERYONE_ID, TRUSTED_ID),
    ) == frozenset({EVERYONE_ID})


@pytest.mark.asyncio
async def test_projection_excludes_runtime_revoked_and_unknown_capabilities() -> None:
    guard, guild, channel = _environment()
    guard.registry.set_runtime_availability(RUNTIME_ID, False)

    projection = await project_authorized_capabilities_for_discord_actor(
        guard=guard,
        guild=guild,
        channel=channel,
        user_id=USER_ID,
        capability_ids=(UNKNOWN_ID, RUNTIME_ID, EVERYONE_ID),
    )

    assert projection.allowed_capability_ids == frozenset({EVERYONE_ID})
    assert projection.actor_level is RbacLevel.EVERYONE


@pytest.mark.asyncio
async def test_projection_fails_closed_for_module_disable_and_shutdown() -> None:
    guard, guild, channel = _environment()
    guard.registry.state_store.set_module_override("module.test", False, GUILD_ID)

    disabled = await project_authorized_capabilities_for_discord_actor(
        guard=guard,
        guild=guild,
        channel=channel,
        user_id=USER_ID,
        capability_ids=(EVERYONE_ID, RUNTIME_ID),
    )
    assert disabled == DiscordCapabilityProjection(frozenset(), RbacLevel.EVERYONE)

    fetches_before_shutdown = guild.fetch_count
    guard.bot.is_closing = True
    shutdown = await project_authorized_capabilities_for_discord_actor(
        guard=guard,
        guild=guild,
        channel=channel,
        user_id=USER_ID,
        capability_ids=(EVERYONE_ID,),
    )
    assert shutdown == DiscordCapabilityProjection(frozenset(), None)
    assert guild.fetch_count == fetches_before_shutdown


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("view_channel", "read_message_history"),
    [(False, True), (True, False), (False, False)],
)
async def test_projection_requires_channel_view_and_read_permissions(
    view_channel: bool,
    read_message_history: bool,
) -> None:
    guard, guild, channel = _environment(
        view_channel=view_channel,
        read_message_history=read_message_history,
    )

    projection = await project_authorized_capabilities_for_discord_actor(
        guard=guard,
        guild=guild,
        channel=channel,
        user_id=USER_ID,
        capability_ids=(EVERYONE_ID,),
    )

    assert projection == DiscordCapabilityProjection(frozenset(), None)
    assert guild.fetch_count == 1
    assert guard.bot.owner_checks == 0


@pytest.mark.asyncio
async def test_projection_fails_closed_for_dm_scope_mismatch_and_missing_methods() -> None:
    guard, guild, channel = _environment()

    assert await project_authorized_capabilities_for_discord_actor(
        guard=guard,
        guild=None,
        channel=channel,
        user_id=USER_ID,
        capability_ids=(EVERYONE_ID,),
    ) == DiscordCapabilityProjection(frozenset(), None)

    wrong_guild_channel = SimpleNamespace(
        guild=SimpleNamespace(id=GUILD_ID + 1),
        permissions_for=channel.permissions_for,
    )
    assert await project_authorized_capabilities_for_discord_actor(
        guard=guard,
        guild=guild,
        channel=wrong_guild_channel,
        user_id=USER_ID,
        capability_ids=(EVERYONE_ID,),
    ) == DiscordCapabilityProjection(frozenset(), None)

    missing_guard = SimpleNamespace(
        bot=guard.bot,
        settings=guard.settings,
        registry=guard.registry,
        policy=guard.policy,
    )
    assert await project_authorized_capabilities_for_discord_actor(
        guard=missing_guard,
        guild=guild,
        channel=channel,
        user_id=USER_ID,
        capability_ids=(EVERYONE_ID,),
    ) == DiscordCapabilityProjection(frozenset(), None)


@pytest.mark.asyncio
async def test_projection_fails_closed_for_fetch_actor_and_per_capability_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, guild, channel = _environment()

    async def fetch_failure(_user_id: int) -> object:
        raise RuntimeError("Discord unavailable")

    guild.fetch_member = fetch_failure
    assert await project_authorized_capabilities_for_discord_actor(
        guard=guard,
        guild=guild,
        channel=channel,
        user_id=USER_ID,
        capability_ids=(EVERYONE_ID,),
    ) == DiscordCapabilityProjection(frozenset(), None)

    guard, guild, channel = _environment()

    async def actor_failure(**_kwargs: Any) -> ActorContext:
        raise RuntimeError("actor resolution unavailable")

    monkeypatch.setattr(capability_rag, "actor_context_for_member", actor_failure)
    assert await project_authorized_capabilities_for_discord_actor(
        guard=guard,
        guild=guild,
        channel=channel,
        user_id=USER_ID,
        capability_ids=(EVERYONE_ID,),
    ) == DiscordCapabilityProjection(frozenset(), None)

    monkeypatch.undo()
    guard, guild, channel = _environment()
    guard.policy.raise_ids.add(TRUSTED_ID)
    guard.currently_raises.add(RUNTIME_ID)
    projection = await project_authorized_capabilities_for_discord_actor(
        guard=guard,
        guild=guild,
        channel=channel,
        user_id=USER_ID,
        capability_ids=(RUNTIME_ID, TRUSTED_ID, EVERYONE_ID),
    )
    assert projection.allowed_capability_ids == frozenset({EVERYONE_ID})


@pytest.mark.asyncio
async def test_projection_is_deterministic_and_deduplicates_before_evaluation() -> None:
    first_guard, first_guild, first_channel = _environment(trusted=True)
    first = await project_authorized_capabilities_for_discord_actor(
        guard=first_guard,
        guild=first_guild,
        channel=first_channel,
        user_id=USER_ID,
        capability_ids=(TRUSTED_ID, EVERYONE_ID, TRUSTED_ID, RUNTIME_ID),
    )
    second_guard, second_guild, second_channel = _environment(trusted=True)
    second = await project_authorized_capabilities_for_discord_actor(
        guard=second_guard,
        guild=second_guild,
        channel=second_channel,
        user_id=USER_ID,
        capability_ids=(RUNTIME_ID, TRUSTED_ID, EVERYONE_ID),
    )

    expected_order = sorted((EVERYONE_ID, RUNTIME_ID, TRUSTED_ID))
    assert first == second
    assert first_guard.policy.calls == expected_order
    assert second_guard.policy.calls == expected_order
    assert first_guard.current_calls == expected_order
    assert second_guard.current_calls == expected_order


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capability_ids", "minimum_levels"),
    [
        (EVERYONE_ID, None),
        ((EVERYONE_ID, "NOT CANONICAL"), None),
        ((EVERYONE_ID,), {UNKNOWN_ID: RbacLevel.EVERYONE}),
        ((EVERYONE_ID,), {EVERYONE_ID: "everyone"}),
        (tuple(EVERYONE_ID for _ in range(MAX_AUTHORIZATION_CANDIDATES + 1)), None),
    ],
)
async def test_projection_rejects_unbounded_or_noncanonical_candidate_inputs(
    capability_ids: object,
    minimum_levels: object,
) -> None:
    guard, guild, channel = _environment()
    projection = await project_authorized_capabilities_for_discord_actor(
        guard=guard,
        guild=guild,
        channel=channel,
        user_id=USER_ID,
        capability_ids=capability_ids,  # type: ignore[arg-type]
        minimum_levels=minimum_levels,  # type: ignore[arg-type]
    )

    assert projection == DiscordCapabilityProjection(frozenset(), None)
    assert guild.fetch_count == 0
