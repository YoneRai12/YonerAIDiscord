from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from yonerai_discord.v0_runtime.command_service import (
    AICommand,
    AICommandInput,
    CommandActor,
    CommandPreference,
    CommandResult,
    CommandScope,
    EffectiveRoute,
    MemoryCommand,
    MemoryCommandInput,
    V0CommandService,
)
from yonerai_discord.v0_runtime.renderer import render_command_result


@dataclass
class Preferences:
    value: CommandPreference = field(default_factory=CommandPreference)

    def get(self, _scope: CommandScope, _user_id: int) -> CommandPreference:
        return self.value

    def set_model(self, _scope: CommandScope, _user_id: int, value: str | None) -> None:
        self.value = CommandPreference(value, self.value.provider_id)

    def set_provider(self, _scope: CommandScope, _user_id: int, value: str | None) -> None:
        self.value = CommandPreference(self.value.model_alias, value)


@dataclass
class Reset:
    calls: list[tuple[CommandScope, int]] = field(default_factory=list)

    async def reset_conversation(self, scope: CommandScope, user_id: int) -> None:
        self.calls.append((scope, user_id))


class Route:
    def resolve(self, _scope: CommandScope, _user_id: int, preference: CommandPreference) -> EffectiveRoute:
        if preference.model_alias is None and preference.provider_id is None:
            return EffectiveRoute("ai.auto", "legacy.openai-compatible", True, "existing_default_path")
        return EffectiveRoute(None, None, False, "provider_not_in_catalog")


class Memory:
    def __init__(self) -> None:
        self.items = ["memory.one"]
        self.visibility = "user_private"

    def remember(self, _actor: CommandActor, _text: str) -> str:
        self.items.append("memory.two")
        return "memory.two"

    def list_metadata(self, _actor: CommandActor) -> tuple[dict[str, object], ...]:
        return tuple(
            {"memory_id": memory_id, "visibility": self.visibility, "created_at": 1} for memory_id in self.items
        )

    def forget(self, _actor: CommandActor, memory_id: str) -> bool:
        if memory_id not in self.items:
            return False
        self.items.remove(memory_id)
        return True

    def clear(self, _actor: CommandActor) -> int:
        count = len(self.items)
        self.items.clear()
        return count

    def privacy(self, actor: CommandActor, mode: str | None) -> str:
        if mode in {"channel", "guild"} and not actor.can_share_memory:
            raise PermissionError
        if mode is not None:
            self.visibility = mode
        return self.visibility

    def preview_metadata(self, _actor: CommandActor) -> dict[str, object]:
        return {"count": len(self.items), "visibility": self.visibility}


def actor(*, self_authorized: bool = True, dm: bool = False, can_share: bool = False) -> CommandActor:
    scope = CommandScope(None, dm_channel_id=30) if dm else CommandScope(10, channel_id=20)
    return CommandActor(10, scope, self_authorized, can_share)


def allow_commit(_actor: CommandActor, _operation: str) -> bool:
    return True


def service(memory: Memory | None = None):
    preferences, reset = Preferences(), Reset()
    return (
        V0CommandService(
            preferences=preferences,
            conversation_reset=reset,
            route_availability=Route(),
            memory=memory,
        ),
        preferences,
        reset,
    )


@pytest.mark.asyncio
async def test_self_only_and_allowlisted_ai_inputs_with_route_reason() -> None:
    instance, _, _ = service()
    assert (
        await instance.execute_ai(AICommandInput(actor(self_authorized=False), AICommand.MODEL_LIST))
    ).code == "actor_not_authorized"
    assert (await instance.execute_ai(AICommandInput(actor(), AICommand.MODEL_SET, "gpt-4o"))).code == "invalid_input"
    selected = await instance.execute_ai(
        AICommandInput(actor(), AICommand.MODEL_SET, "ai.quality"), commit_check=allow_commit
    )
    assert selected.data == {"selected": "ai.quality"}
    route = await instance.execute_ai(AICommandInput(actor(), AICommand.ROUTE))
    assert route.data["effective_provider"] is None
    assert route.data["reason"] == "provider_not_in_catalog"
    assert "状態: 実行不可" in render_command_result(route)


@pytest.mark.asyncio
@pytest.mark.parametrize("dm", [False, True])
async def test_reset_only_calls_exact_conversation_scope_and_keeps_preferences(dm: bool) -> None:
    instance, preferences, reset = service()
    request_actor = actor(dm=dm)
    await instance.execute_ai(
        AICommandInput(request_actor, AICommand.MODEL_SET, "ai.quality"), commit_check=allow_commit
    )
    assert (await instance.execute_ai(AICommandInput(request_actor, AICommand.RESET), commit_check=allow_commit)).ok
    assert reset.calls == [(request_actor.scope, request_actor.user_id)]
    assert preferences.value.model_alias == "ai.quality"


def test_all_memory_commands_return_allowlisted_metadata_without_raw_content() -> None:
    memory = Memory()
    instance, _, _ = service(memory)
    request_actor = actor(can_share=True)
    remembered = instance.execute_memory(
        MemoryCommandInput(request_actor, MemoryCommand.REMEMBER, "private input"),
        commit_check=allow_commit,
    )
    listed = instance.execute_memory(MemoryCommandInput(request_actor, MemoryCommand.LIST))
    preview = instance.execute_memory(MemoryCommandInput(request_actor, MemoryCommand.PREVIEW))
    privacy = instance.execute_memory(
        MemoryCommandInput(request_actor, MemoryCommand.PRIVACY, "channel"),
        commit_check=allow_commit,
    )
    forgotten = instance.execute_memory(
        MemoryCommandInput(request_actor, MemoryCommand.FORGET, "memory.one"),
        commit_check=allow_commit,
    )
    cleared = instance.execute_memory(
        MemoryCommandInput(request_actor, MemoryCommand.CLEAR, "CLEAR_MY_MEMORY"),
        commit_check=allow_commit,
    )

    assert remembered.data == {"memory_id": "memory.two"}
    assert listed.data["count"] == 2
    assert preview.data == {"count": 2, "visibility": "user_private"}
    assert privacy.data == {"visibility": "channel"}
    assert forgotten.ok and cleared.data == {"deleted": 1}
    rendered = "\n".join(render_command_result(item) for item in (listed, preview, privacy))
    assert "private input" not in rendered
    assert "internal" not in rendered


def test_shared_privacy_requires_permission_and_dm_scope_is_supported() -> None:
    memory = Memory()
    instance, _, _ = service(memory)
    denied = instance.execute_memory(
        MemoryCommandInput(actor(), MemoryCommand.PRIVACY, "guild"), commit_check=allow_commit
    )
    dm = instance.execute_memory(
        MemoryCommandInput(actor(dm=True), MemoryCommand.REMEMBER, "dm value"),
        commit_check=allow_commit,
    )
    assert denied.code == "memory_rejected"
    assert dm.ok


def test_result_rejects_non_public_fields() -> None:
    for key in ("prompt", "memory", "content", "score", "secret", "chain_of_thought", "reasoning"):
        with pytest.raises(ValueError):
            CommandResult(True, "x", {key: "x"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "value"),
    [
        (AICommand.MODEL_SET, "ai.quality"),
        (AICommand.MODEL_AUTO, None),
        (AICommand.RESET, None),
    ],
)
async def test_ai_mutations_recheck_authorization_immediately_before_side_effect(command, value) -> None:
    instance, preferences, reset = service()
    calls: list[tuple[CommandActor, str]] = []

    result = await instance.execute_ai(
        AICommandInput(actor(), command, value),
        commit_check=lambda checked_actor, operation: calls.append((checked_actor, operation)) or False,
    )

    assert result.code == "authorization_changed"
    assert calls == [(actor(), command.value)]
    assert preferences.value == CommandPreference()
    assert reset.calls == []


@pytest.mark.parametrize(
    ("command", "value"),
    [
        (MemoryCommand.REMEMBER, "private input"),
        (MemoryCommand.FORGET, "memory.one"),
        (MemoryCommand.CLEAR, "CLEAR_MY_MEMORY"),
        (MemoryCommand.PRIVACY, "private"),
    ],
)
def test_memory_mutations_recheck_authorization_and_leave_state_unchanged(command, value) -> None:
    memory = Memory()
    instance, _, _ = service(memory)
    calls: list[tuple[CommandActor, str]] = []

    result = instance.execute_memory(
        MemoryCommandInput(actor(), command, value),
        commit_check=lambda checked_actor, operation: calls.append((checked_actor, operation)) or False,
    )

    assert result.code == "authorization_changed"
    assert calls == [(actor(), command.value)]
    assert memory.items == ["memory.one"]
    assert memory.visibility == "user_private"


@pytest.mark.asyncio
async def test_read_only_commands_do_not_consume_commit_authorization() -> None:
    instance, _, _ = service(Memory())

    def unexpected(_actor: CommandActor, _operation: str) -> bool:
        raise AssertionError("read-only command must not call commit_check")

    assert (await instance.execute_ai(AICommandInput(actor(), AICommand.ROUTE), commit_check=unexpected)).ok
    assert instance.execute_memory(MemoryCommandInput(actor(), MemoryCommand.LIST), commit_check=unexpected).ok
    assert instance.execute_memory(MemoryCommandInput(actor(), MemoryCommand.PREVIEW), commit_check=unexpected).ok


@pytest.mark.asyncio
async def test_mutations_fail_closed_when_commit_callback_is_omitted() -> None:
    memory = Memory()
    instance, preferences, reset = service(memory)

    model = await instance.execute_ai(AICommandInput(actor(), AICommand.MODEL_SET, "ai.quality"))
    conversation = await instance.execute_ai(AICommandInput(actor(), AICommand.RESET))
    remembered = instance.execute_memory(MemoryCommandInput(actor(), MemoryCommand.REMEMBER, "must not persist"))

    assert {model.code, conversation.code, remembered.code} == {"authorization_changed"}
    assert preferences.value == CommandPreference()
    assert reset.calls == []
    assert memory.items == ["memory.one"]
