from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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
    async def evaluate_fresh_member(self, capability_id: str, *, guild: object, member: object) -> object:
        return SimpleNamespace(allowed=True)


def bot_for(registry: Registry) -> SimpleNamespace:
    return SimpleNamespace(capability_registry=registry, capability_guard=Guard())


class Message:
    def __init__(
        self,
        registry: Registry,
        *,
        disable_after_delete: bool = False,
        fail_delete: bool = False,
        after_delete: Callable[[], None] | None = None,
    ) -> None:
        self.registry = registry
        self.disable_after_delete = disable_after_delete
        self.deleted = False
        self.fail_delete = fail_delete
        self.after_delete = after_delete

    async def delete(self) -> None:
        if self.fail_delete:
            raise RuntimeError("synthetic Discord failure")
        self.deleted = True
        if self.disable_after_delete:
            self.registry.enabled = False
        if self.after_delete is not None:
            self.after_delete()


class Channel:
    def __init__(
        self,
        messages: list[Message],
        *,
        channel_id: int = 456,
        actor_id: int = 999,
        bot_id: int = 1000,
        actor_manage_messages: bool = True,
        bot_view_channel: bool = True,
        bot_read_message_history: bool = True,
        bot_manage_messages: bool = True,
    ) -> None:
        self.id = channel_id
        self.messages = messages
        self.actor_id = actor_id
        self.bot_id = bot_id
        self.actor_manage_messages = actor_manage_messages
        self.bot_view_channel = bot_view_channel
        self.bot_read_message_history = bot_read_message_history
        self.bot_manage_messages = bot_manage_messages

    def permissions_for(self, member: object) -> SimpleNamespace:
        if getattr(member, "id", None) == self.bot_id:
            return SimpleNamespace(
                view_channel=self.bot_view_channel,
                read_message_history=self.bot_read_message_history,
                manage_messages=self.bot_manage_messages,
            )
        assert getattr(member, "id", None) == self.actor_id
        return SimpleNamespace(
            view_channel=True,
            read_message_history=True,
            manage_messages=self.actor_manage_messages,
        )

    async def history(self, *, limit: int):
        assert limit >= len(self.messages)
        for message in self.messages:
            yield message


class Guild:
    def __init__(
        self,
        actor: object,
        target: object | None = None,
        *,
        channel: Channel | None = None,
        bot: object | None = None,
    ) -> None:
        self.bot = bot or SimpleNamespace(id=1000, marker="fresh-bot")
        self.me = SimpleNamespace(id=getattr(self.bot, "id"), marker="cached-bot")
        self.members = {getattr(actor, "id"): actor}
        if target is not None:
            self.members[getattr(target, "id")] = target
        self.members[getattr(self.bot, "id")] = self.bot
        self.channel = channel
        self.fetch_channel_calls = 0

    async def fetch_member(self, actor_id: int) -> object:
        return self.members[actor_id]

    async def fetch_channel(self, channel_id: int) -> Channel:
        self.fetch_channel_calls += 1
        assert self.channel is not None and self.channel.id == channel_id
        return self.channel


class Response:
    def __init__(self) -> None:
        self.done = False
        self.messages: list[str] = []

    def is_done(self) -> bool:
        return self.done

    async def send_message(self, message: str, **kwargs: object) -> None:
        assert kwargs.get("ephemeral") is True
        self.done = True
        self.messages.append(message)


class Followup:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str, **kwargs: object) -> None:
        assert kwargs.get("ephemeral") is True
        self.messages.append(message)


class Repository:
    def __init__(self, *, fail_record: bool = False, update_result: bool = True) -> None:
        self.records: list[dict[str, object]] = []
        self.updates: list[dict[str, object]] = []
        self.fail_record = fail_record
        self.update_result = update_result

    def record_case(self, **values: object) -> SimpleNamespace:
        if self.fail_record:
            raise RuntimeError("synthetic ledger failure")
        self.records.append(values)
        return SimpleNamespace(id=42)

    def update_case(self, guild_id: int, case_id: int, **values: object) -> bool:
        self.updates.append({"guild_id": guild_id, "case_id": case_id, **values})
        return self.update_result


def purge_interaction(actor: object, cached_channel: Channel, fresh_channel: Channel) -> SimpleNamespace:
    guild = Guild(actor, channel=fresh_channel)
    return SimpleNamespace(
        guild_id=123,
        channel_id=cached_channel.id,
        guild=guild,
        channel=cached_channel,
        user=actor,
        response=Response(),
        followup=Followup(),
    )


async def test_purge_stops_between_messages_when_central_capability_turns_off() -> None:
    registry = Registry()
    first = Message(registry, disable_after_delete=True)
    second = Message(registry)
    repository = Repository()
    group = ModGroup(repository, bot_for(registry))  # type: ignore[arg-type]
    group._authorize = AsyncMock(return_value=True)  # type: ignore[method-assign]
    actor = SimpleNamespace(id=999)
    channel = Channel([first, second])
    interaction = purge_interaction(actor, channel, channel)

    await group._purge(
        interaction,
        amount=2,
        reason="cleanup",
        dry_run=False,
        confirm="PURGE",
        action=ModAction.PURGE,
        target=None,
        predicate=lambda message: True,
    )

    assert first.deleted is True
    assert second.deleted is False
    assert registry.calls and all(item == ("cap-run-mod-purge", 123) for item in registry.calls)
    assert repository.records[0]["status"] == "pending"
    assert repository.updates[0]["status"] == "partial"
    assert repository.updates[0]["metadata"] == {
        "requested": 2,
        "deleted": 1,
        "stopped": True,
        "stop_reason": "authorization_changed",
    }
    assert any("途中" in message for message in interaction.followup.messages)


async def test_purge_records_partial_result_when_discord_delete_fails() -> None:
    registry = Registry()
    first = Message(registry, disable_after_delete=False)
    second = Message(registry, fail_delete=True)
    repository = Repository()
    group = ModGroup(repository, bot_for(registry))  # type: ignore[arg-type]
    group._authorize = AsyncMock(return_value=True)  # type: ignore[method-assign]
    actor = SimpleNamespace(id=999)
    channel = Channel([first, second])
    interaction = purge_interaction(actor, channel, channel)

    await group._purge(
        interaction,
        amount=2,
        reason="cleanup",
        dry_run=False,
        confirm="PURGE",
        action=ModAction.PURGE,
        target=None,
        predicate=lambda message: True,
    )

    assert first.deleted is True
    assert second.deleted is False
    assert repository.records[0]["status"] == "pending"
    assert repository.updates[0]["status"] == "partial"
    assert repository.updates[0]["metadata"] == {
        "requested": 2,
        "deleted": 1,
        "stopped": True,
        "stop_reason": "discord_delete_failed",
    }


async def test_purge_first_delete_failure_finalizes_failed_case() -> None:
    registry = Registry()
    message = Message(registry, fail_delete=True)
    repository = Repository()
    group = ModGroup(repository, bot_for(registry))  # type: ignore[arg-type]
    group._authorize = AsyncMock(return_value=True)  # type: ignore[method-assign]
    actor = SimpleNamespace(id=999)
    channel = Channel([message])
    interaction = purge_interaction(actor, channel, channel)

    await group._purge(
        interaction,
        amount=1,
        reason="cleanup",
        dry_run=False,
        confirm="PURGE",
        action=ModAction.PURGE,
        target=None,
        predicate=lambda _: True,
    )

    assert message.deleted is False
    assert repository.updates[0]["status"] == "failed"
    assert repository.updates[0]["metadata"]["deleted"] == 0


async def test_purge_user_rechecks_target_from_rest_before_each_side_effect() -> None:
    registry = Registry()
    repository = Repository()
    group = ModGroup(repository, bot_for(registry))  # type: ignore[arg-type]
    authorize = AsyncMock(return_value=True)
    group._authorize = authorize  # type: ignore[method-assign]
    actor = SimpleNamespace(id=999)
    cached_target = SimpleNamespace(id=555, marker="cached")
    fresh_target = SimpleNamespace(id=555, marker="fresh")
    interaction = SimpleNamespace(
        guild_id=123,
        guild=Guild(actor, fresh_target),
        user=actor,
        response=Response(),
        followup=Followup(),
    )

    assert await group._side_effect_still_allowed(
        interaction,
        ModAction.PURGE_USER,
        cached_target,
    )
    assert authorize.await_args.kwargs["actor_override"] is actor
    assert authorize.await_args.kwargs["target_override"] is fresh_target


async def test_registry_is_rechecked_after_rest_awaits() -> None:
    registry = Registry()
    repository = Repository()
    group = ModGroup(repository, bot_for(registry))  # type: ignore[arg-type]
    authorize = AsyncMock(return_value=True)
    group._authorize = authorize  # type: ignore[method-assign]
    actor = SimpleNamespace(id=999)

    class FlippingGuild(Guild):
        async def fetch_member(self, actor_id: int) -> object:
            member = await super().fetch_member(actor_id)
            registry.enabled = False
            return member

    interaction = SimpleNamespace(
        guild_id=123,
        guild=FlippingGuild(actor),
        user=actor,
        response=Response(),
        followup=Followup(),
    )

    assert not await group._side_effect_still_allowed(
        interaction,
        ModAction.PURGE,
    )
    authorize.assert_not_awaited()


async def test_purge_record_failure_deletes_nothing() -> None:
    registry = Registry()
    message = Message(registry)
    repository = Repository(fail_record=True)
    group = ModGroup(repository, bot_for(registry))  # type: ignore[arg-type]
    group._authorize = AsyncMock(return_value=True)  # type: ignore[method-assign]
    actor = SimpleNamespace(id=999)
    channel = Channel([message])
    interaction = purge_interaction(actor, channel, channel)

    await group._purge(
        interaction,
        amount=1,
        reason="cleanup",
        dry_run=False,
        confirm="PURGE",
        action=ModAction.PURGE,
        target=None,
        predicate=lambda _: True,
    )

    assert message.deleted is False
    assert repository.records == []
    assert repository.updates == []
    assert any("台帳を開始できない" in message for message in interaction.response.messages)


async def test_purge_finalize_failure_reports_deleted_and_unconfirmed_ledger() -> None:
    registry = Registry()
    message = Message(registry)
    repository = Repository(update_result=False)
    group = ModGroup(repository, bot_for(registry))  # type: ignore[arg-type]
    group._authorize = AsyncMock(return_value=True)  # type: ignore[method-assign]
    actor = SimpleNamespace(id=999)
    channel = Channel([message])
    interaction = purge_interaction(actor, channel, channel)

    await group._purge(
        interaction,
        amount=1,
        reason="cleanup",
        dry_run=False,
        confirm="PURGE",
        action=ModAction.PURGE,
        target=None,
        predicate=lambda _: True,
    )

    assert message.deleted is True
    assert repository.records[0]["status"] == "pending"
    assert repository.updates[0]["status"] == "completed"
    messages = interaction.response.messages + interaction.followup.messages
    assert any("Discord削除済み・台帳未確定・要監査" in item for item in messages)


async def test_purge_zero_matches_finalizes_completed_case() -> None:
    registry = Registry()
    repository = Repository()
    group = ModGroup(repository, bot_for(registry))  # type: ignore[arg-type]
    group._authorize = AsyncMock(return_value=True)  # type: ignore[method-assign]
    actor = SimpleNamespace(id=999)
    channel = Channel([])
    interaction = purge_interaction(actor, channel, channel)

    await group._purge(
        interaction,
        amount=1,
        reason="cleanup",
        dry_run=False,
        confirm="PURGE",
        action=ModAction.PURGE,
        target=None,
        predicate=lambda _: True,
    )

    assert repository.records[0]["status"] == "pending"
    assert repository.updates[0]["status"] == "completed"
    assert repository.updates[0]["metadata"]["deleted"] == 0
    assert any("0件を削除" in item for item in interaction.response.messages)


async def test_purge_respects_rest_channel_overwrite_deny_for_actor() -> None:
    registry = Registry()
    message = Message(registry)
    cached_channel = Channel([message], actor_manage_messages=True)
    fresh_channel = Channel([], channel_id=cached_channel.id, actor_manage_messages=False)
    repository = Repository()
    group = ModGroup(repository, bot_for(registry))  # type: ignore[arg-type]
    group._authorize = AsyncMock(return_value=True)  # type: ignore[method-assign]
    actor = SimpleNamespace(id=999)
    interaction = purge_interaction(actor, cached_channel, fresh_channel)

    await group._purge(
        interaction,
        amount=1,
        reason="cleanup",
        dry_run=False,
        confirm="PURGE",
        action=ModAction.PURGE,
        target=None,
        predicate=lambda _: True,
    )

    assert message.deleted is False
    assert repository.records == []
    assert interaction.guild.fetch_channel_calls >= 1
    messages = interaction.response.messages + interaction.followup.messages
    assert any("チャンネル" in item and "権限" in item for item in messages)


@pytest.mark.parametrize(
    "missing_permission",
    ["view_channel", "read_message_history", "manage_messages"],
)
async def test_purge_requires_all_native_bot_channel_permissions(missing_permission: str) -> None:
    registry = Registry()
    message = Message(registry)
    cached_channel = Channel([message])
    permission_values = {
        "bot_view_channel": True,
        "bot_read_message_history": True,
        "bot_manage_messages": True,
    }
    permission_values[f"bot_{missing_permission}"] = False
    fresh_channel = Channel([], channel_id=cached_channel.id, **permission_values)  # type: ignore[arg-type]
    repository = Repository()
    group = ModGroup(repository, bot_for(registry))  # type: ignore[arg-type]
    group._authorize = AsyncMock(return_value=True)  # type: ignore[method-assign]
    actor = SimpleNamespace(id=999)
    interaction = purge_interaction(actor, cached_channel, fresh_channel)

    await group._purge(
        interaction,
        amount=1,
        reason="cleanup",
        dry_run=False,
        confirm="PURGE",
        action=ModAction.PURGE,
        target=None,
        predicate=lambda _: True,
    )

    assert message.deleted is False
    assert repository.records == []
    messages = interaction.response.messages + interaction.followup.messages
    assert any("Bot" in item and "権限" in item for item in messages)


@pytest.mark.parametrize("lost_permission", ["actor_manage_messages", "bot_manage_messages"])
async def test_purge_permission_loss_between_messages_preserves_partial_ledger(lost_permission: str) -> None:
    registry = Registry()
    fresh_channel = Channel([])

    def revoke_permission() -> None:
        setattr(fresh_channel, lost_permission, False)

    first = Message(registry, after_delete=revoke_permission)
    second = Message(registry)
    cached_channel = Channel([first, second], channel_id=fresh_channel.id)
    repository = Repository()
    group = ModGroup(repository, bot_for(registry))  # type: ignore[arg-type]
    group._authorize = AsyncMock(return_value=True)  # type: ignore[method-assign]
    actor = SimpleNamespace(id=999)
    interaction = purge_interaction(actor, cached_channel, fresh_channel)

    await group._purge(
        interaction,
        amount=2,
        reason="cleanup",
        dry_run=False,
        confirm="PURGE",
        action=ModAction.PURGE,
        target=None,
        predicate=lambda _: True,
    )

    assert first.deleted is True
    assert second.deleted is False
    assert repository.records[0]["status"] == "pending"
    assert repository.updates[0]["status"] == "partial"
    assert repository.updates[0]["metadata"] == {
        "requested": 2,
        "deleted": 1,
        "stopped": True,
        "stop_reason": "authorization_changed",
    }


async def test_purge_with_fresh_effective_permissions_completes_normally() -> None:
    registry = Registry()
    first = Message(registry)
    second = Message(registry)
    cached_channel = Channel([first, second])
    fresh_channel = Channel([], channel_id=cached_channel.id)
    repository = Repository()
    group = ModGroup(repository, bot_for(registry))  # type: ignore[arg-type]
    group._authorize = AsyncMock(return_value=True)  # type: ignore[method-assign]
    actor = SimpleNamespace(id=999)
    interaction = purge_interaction(actor, cached_channel, fresh_channel)

    await group._purge(
        interaction,
        amount=2,
        reason="cleanup",
        dry_run=False,
        confirm="PURGE",
        action=ModAction.PURGE,
        target=None,
        predicate=lambda _: True,
    )

    assert first.deleted is True
    assert second.deleted is True
    assert interaction.guild.fetch_channel_calls == 3
    assert repository.records[0]["status"] == "pending"
    assert repository.updates[0]["status"] == "completed"
    assert repository.updates[0]["metadata"] == {
        "requested": 2,
        "deleted": 2,
        "stopped": False,
        "stop_reason": None,
    }
