from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from yonerai_discord.control_plane import CapabilitySpec, ModuleSpec, RbacLevel, Registry as ControlRegistry
from yonerai_discord.discord_guard import CapabilityGuard
from yonerai_discord.modules.modtools.adapter import ModGroup
from yonerai_discord.modules.modtools.domain import ModAction


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
        self.calls: list[tuple[str, object, object]] = []

    async def evaluate_fresh_member(self, capability_id: str, *, guild: object, member: object) -> object:
        self.calls.append((capability_id, guild, member))
        return SimpleNamespace(allowed=self.allowed)


class Response:
    def __init__(self) -> None:
        self.done = False
        self.messages: list[str] = []

    def is_done(self) -> bool:
        return self.done

    async def send_message(self, message: str, **kwargs: object) -> None:
        self.done = True
        self.messages.append(message)


class Followup:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str, **kwargs: object) -> None:
        self.messages.append(message)


class Repository:
    def __init__(
        self,
        *,
        update_result: bool = True,
        flip_registry: Registry | None = None,
        flip_guard: Guard | None = None,
    ) -> None:
        self.records: list[dict[str, object]] = []
        self.updates: list[dict[str, object]] = []
        self.update_result = update_result
        self.flip_registry = flip_registry
        self.flip_guard = flip_guard

    def record_case(self, **values: object) -> SimpleNamespace:
        self.records.append(values)
        if self.flip_registry is not None:
            self.flip_registry.enabled = False
        if self.flip_guard is not None:
            self.flip_guard.allowed = False
        return SimpleNamespace(id=73)

    def update_case(self, guild_id: int, case_id: int, **values: object) -> bool:
        self.updates.append({"guild_id": guild_id, "case_id": case_id, **values})
        return self.update_result


class Guild:
    def __init__(
        self,
        actor: object,
        targets: list[object],
        *,
        registry: Registry | None = None,
        flip_after_actor: bool = False,
        target_error: Exception | None = None,
    ) -> None:
        self.actor = actor
        self.targets = list(targets)
        self.registry = registry
        self.flip_after_actor = flip_after_actor
        self.target_error = target_error
        self.fetches: list[int] = []
        self.unbans: list[tuple[int, str]] = []

    async def fetch_member(self, member_id: int) -> object:
        self.fetches.append(member_id)
        if member_id == 555:
            if self.target_error is not None:
                raise self.target_error
            return self.targets.pop(0)
        if self.flip_after_actor and self.registry is not None:
            self.registry.enabled = False
        return self.actor

    async def unban(self, user: discord.Object, *, reason: str) -> None:
        self.unbans.append((user.id, reason))


def interaction(guild: Guild) -> SimpleNamespace:
    actor = SimpleNamespace(id=999, marker="cached-actor")
    return SimpleNamespace(
        guild_id=123,
        guild=guild,
        user=actor,
        response=Response(),
        followup=Followup(),
    )


def group(repository: Repository, registry: Registry, guard: Guard | None = None) -> ModGroup:
    return ModGroup(
        repository,
        SimpleNamespace(capability_registry=registry, capability_guard=guard or Guard()),
    )  # type: ignore[arg-type]


async def test_fresh_gate_fetches_target_before_actor_and_passes_both_to_authorizer() -> None:
    registry = Registry()
    actor = SimpleNamespace(id=999, marker="fresh-actor")
    target = SimpleNamespace(id=555, marker="fresh-target")
    guild = Guild(actor, [target])
    command = group(Repository(), registry)
    authorize = AsyncMock(return_value=True)
    command._authorize = authorize  # type: ignore[method-assign]
    current = interaction(guild)
    cached_target = SimpleNamespace(id=555, marker="cached-target")

    result = await command._fresh_authorized_members(current, ModAction.KICK, cached_target)

    assert result == (actor, target)
    assert guild.fetches == [555, 999]
    assert authorize.await_args.kwargs["actor_override"] is actor
    assert authorize.await_args.kwargs["target_override"] is target


async def test_central_off_during_rest_refresh_fails_closed_before_ledger_or_action() -> None:
    registry = Registry()
    actor = SimpleNamespace(id=999, marker="fresh-actor")
    target = SimpleNamespace(id=555, marker="fresh-target")
    guild = Guild(actor, [target], registry=registry, flip_after_actor=True)
    repository = Repository()
    command = group(repository, registry)
    command._authorize = AsyncMock(return_value=True)  # type: ignore[method-assign]
    execute = AsyncMock()

    await command._member_action(
        interaction(guild),
        SimpleNamespace(id=555),
        "reason",
        ModAction.KICK,
        execute,
    )

    execute.assert_not_awaited()
    assert repository.records == []


async def test_fresh_actor_role_downgrade_stops_before_ledger_or_action() -> None:
    registry = Registry()
    actor = SimpleNamespace(id=999, marker="downgraded")
    target = SimpleNamespace(id=555, marker="fresh-target")
    guild = Guild(actor, [target])
    repository = Repository()
    command = group(repository, registry)

    async def authorize(*args: object, actor_override: object | None = None, **kwargs: object) -> bool:
        return getattr(actor_override, "marker", None) != "downgraded"

    command._authorize = authorize  # type: ignore[method-assign]
    execute = AsyncMock()

    await command._member_action(
        interaction(guild),
        SimpleNamespace(id=555),
        "reason",
        ModAction.KICK,
        execute,
    )

    execute.assert_not_awaited()
    assert repository.records == []


async def test_role_downgrade_after_pending_ledger_stops_before_discord_action() -> None:
    registry = Registry()
    actor = SimpleNamespace(id=999, marker="fresh-actor")
    targets = [SimpleNamespace(id=555, marker=index) for index in range(2)]
    guild = Guild(actor, targets)
    repository = Repository()
    command = group(repository, registry)
    command._authorize = AsyncMock(side_effect=[True, True, False])  # type: ignore[method-assign]
    execute = AsyncMock()

    await command._member_action(
        interaction(guild),
        SimpleNamespace(id=555),
        "reason",
        ModAction.KICK,
        execute,
    )

    execute.assert_not_awaited()
    assert repository.records[0]["status"] == "pending"
    assert repository.updates[0]["status"] == "aborted"


async def test_central_off_after_pending_ledger_stops_before_discord_action() -> None:
    registry = Registry()
    actor = SimpleNamespace(id=999, marker="fresh-actor")
    target = SimpleNamespace(id=555, marker="fresh-target")
    guild = Guild(actor, [target])
    repository = Repository(flip_registry=registry)
    command = group(repository, registry)
    command._authorize = AsyncMock(return_value=True)  # type: ignore[method-assign]
    execute = AsyncMock()

    await command._member_action(
        interaction(guild),
        SimpleNamespace(id=555),
        "reason",
        ModAction.KICK,
        execute,
    )

    execute.assert_not_awaited()
    assert repository.records[0]["status"] == "pending"
    assert repository.updates[0]["status"] == "aborted"


async def test_dynamic_required_level_flip_after_pending_stops_before_discord_action() -> None:
    registry = Registry()
    guard = Guard()
    actor = SimpleNamespace(id=999, marker="fresh-actor")
    targets = [SimpleNamespace(id=555, marker=index) for index in range(2)]
    guild = Guild(actor, targets)
    repository = Repository(flip_guard=guard)
    command = group(repository, registry, guard)
    command._authorize = AsyncMock(return_value=True)  # type: ignore[method-assign]
    execute = AsyncMock()

    await command._member_action(
        interaction(guild),
        SimpleNamespace(id=555),
        "reason",
        ModAction.KICK,
        execute,
    )

    execute.assert_not_awaited()
    assert len(guard.calls) == 2
    assert repository.updates[0]["status"] == "aborted"


async def test_fresh_guard_rechecks_dynamic_level_and_owner_lookup_failure_does_not_elevate() -> None:
    registry = ControlRegistry()
    registry.register_module(ModuleSpec("moderation.actions"))
    registry.register_capability(
        CapabilitySpec(
            "cap-run-mod-kick",
            "moderation.actions",
            required_level=RbacLevel.MODERATOR,
            minimum_level=RbacLevel.MODERATOR,
        )
    )

    class Bot:
        async def is_owner(self, member: object) -> bool:
            raise RuntimeError("application owner lookup failed")

    settings = SimpleNamespace(
        bot_owner_ids=frozenset(),
        moderator_role_ids=frozenset(),
        trusted_role_ids=frozenset(),
    )
    guard = CapabilityGuard(Bot(), settings, registry, SimpleNamespace())  # type: ignore[arg-type]
    permissions = SimpleNamespace(
        administrator=False,
        manage_guild=False,
        moderate_members=False,
        manage_messages=False,
        kick_members=True,
        ban_members=False,
    )
    member = SimpleNamespace(id=999, guild_permissions=permissions, roles=())
    guild = SimpleNamespace(id=123, owner_id=111)

    assert (await guard.evaluate_fresh_member("cap-run-mod-kick", guild=guild, member=member)).allowed

    registry.state_store.set_level_override("cap-run-mod-kick", RbacLevel.BOT_OWNER, guild_id=123)
    decision = await guard.evaluate_fresh_member("cap-run-mod-kick", guild=guild, member=member)

    assert not decision.allowed
    assert decision.required_level is RbacLevel.BOT_OWNER
    assert decision.actor_level is RbacLevel.MODERATOR


async def test_member_action_executes_only_the_second_fresh_target() -> None:
    registry = Registry()
    actor = SimpleNamespace(id=999, marker="fresh-actor")
    ledger_target = SimpleNamespace(id=555, marker="ledger-target")
    execution_target = SimpleNamespace(id=555, marker="execution-target")
    guild = Guild(actor, [ledger_target, execution_target])
    repository = Repository()
    command = group(repository, registry)
    command._authorize = AsyncMock(return_value=True)  # type: ignore[method-assign]
    execute = AsyncMock()

    await command._member_action(
        interaction(guild),
        SimpleNamespace(id=555, marker="cached-target"),
        "reason",
        ModAction.KICK,
        execute,
    )

    execute.assert_awaited_once_with(execution_target, reason="reason")
    assert repository.records[0]["status"] == "pending"
    assert repository.updates[0]["status"] == "completed"


async def test_successful_action_finalizes_audit_even_if_policy_changes_after_discord() -> None:
    registry = Registry()
    actor = SimpleNamespace(id=999, marker="fresh-actor")
    targets = [SimpleNamespace(id=555, marker=index) for index in range(2)]
    guild = Guild(actor, targets)
    repository = Repository()
    command = group(repository, registry)
    command._authorize = AsyncMock(return_value=True)  # type: ignore[method-assign]

    async def execute(target: object, *, reason: str) -> None:
        registry.enabled = False

    await command._member_action(
        interaction(guild),
        SimpleNamespace(id=555),
        "reason",
        ModAction.KICK,
        execute,
    )

    assert repository.updates[0]["status"] == "completed"


async def test_discord_forbidden_is_failed_not_uncertain() -> None:
    registry = Registry()
    actor = SimpleNamespace(id=999, marker="fresh-actor")
    targets = [SimpleNamespace(id=555, marker=index) for index in range(2)]
    guild = Guild(actor, targets)
    repository = Repository()
    command = group(repository, registry)
    command._authorize = AsyncMock(return_value=True)  # type: ignore[method-assign]
    response = SimpleNamespace(status=403, reason="Forbidden")
    error = discord.Forbidden(response, "denied")  # type: ignore[arg-type]
    execute = AsyncMock(side_effect=error)

    await command._member_action(
        interaction(guild),
        SimpleNamespace(id=555),
        "reason",
        ModAction.KICK,
        execute,
    )

    assert repository.updates[0]["status"] == "failed"
    assert repository.updates[0]["metadata"]["discord_result"] == "rejected"


async def test_target_rest_failure_has_no_ledger_or_discord_side_effect() -> None:
    registry = Registry()
    response = SimpleNamespace(status=404, reason="Not Found")
    error = discord.NotFound(response, "missing")  # type: ignore[arg-type]
    guild = Guild(SimpleNamespace(id=999), [], target_error=error)
    repository = Repository()
    command = group(repository, registry)
    command._authorize = AsyncMock(return_value=True)  # type: ignore[method-assign]
    execute = AsyncMock()

    await command._member_action(
        interaction(guild),
        SimpleNamespace(id=555),
        "reason",
        ModAction.KICK,
        execute,
    )

    execute.assert_not_awaited()
    assert repository.records == []


async def test_unban_allows_missing_target_member_but_refreshes_actor_for_every_effect() -> None:
    registry = Registry()
    guild = Guild(SimpleNamespace(id=999, marker="fresh-actor"), [])
    repository = Repository()
    command = group(repository, registry)
    command._authorize = AsyncMock(return_value=True)  # type: ignore[method-assign]

    await command.unban.callback(command, interaction(guild), "555", "appeal accepted")

    assert guild.fetches == [999, 999]
    assert guild.unbans == [(555, "appeal accepted")]
    assert repository.records[0]["target_id"] == 555
    assert repository.updates[0]["status"] == "completed"


async def test_completed_discord_action_is_reported_truthfully_when_ledger_finalize_fails() -> None:
    registry = Registry()
    actor = SimpleNamespace(id=999, marker="fresh-actor")
    targets = [SimpleNamespace(id=555, marker=index) for index in range(3)]
    guild = Guild(actor, targets)
    repository = Repository(update_result=False)
    command = group(repository, registry)
    command._authorize = AsyncMock(return_value=True)  # type: ignore[method-assign]
    execute = AsyncMock()
    current = interaction(guild)

    await command._member_action(
        current,
        SimpleNamespace(id=555),
        "reason",
        ModAction.KICK,
        execute,
    )

    execute.assert_awaited_once()
    messages = current.response.messages + current.followup.messages
    assert any("Discord" in message and "台帳未確定・要監査" in message for message in messages)
    assert repository.records[0]["status"] == "pending"


async def test_forbidden_with_finalize_failure_reports_unconfirmed_ledger() -> None:
    registry = Registry()
    actor = SimpleNamespace(id=999, marker="fresh-actor")
    targets = [SimpleNamespace(id=555, marker=index) for index in range(2)]
    guild = Guild(actor, targets)
    repository = Repository(update_result=False)
    command = group(repository, registry)
    command._authorize = AsyncMock(return_value=True)  # type: ignore[method-assign]
    response = SimpleNamespace(status=403, reason="Forbidden")
    execute = AsyncMock(side_effect=discord.Forbidden(response, "denied"))  # type: ignore[arg-type]
    current = interaction(guild)

    await command._member_action(
        current,
        SimpleNamespace(id=555),
        "reason",
        ModAction.KICK,
        execute,
    )

    messages = current.response.messages + current.followup.messages
    assert any("台帳未確定・要監査" in message for message in messages)
    assert repository.records[0]["status"] == "pending"


async def test_unban_unknown_failure_with_finalize_failure_reports_unconfirmed_ledger() -> None:
    registry = Registry()
    guild = Guild(SimpleNamespace(id=999, marker="fresh-actor"), [])

    async def fail_unban(user: object, *, reason: str) -> None:
        raise RuntimeError("synthetic")

    guild.unban = fail_unban  # type: ignore[method-assign]
    repository = Repository(update_result=False)
    command = group(repository, registry)
    command._authorize = AsyncMock(return_value=True)  # type: ignore[method-assign]
    current = interaction(guild)

    await command.unban.callback(command, current, "555", "appeal accepted")

    messages = current.response.messages + current.followup.messages
    assert any("台帳未確定・要監査" in message for message in messages)
    assert repository.records[0]["status"] == "pending"
