from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from yonerai_discord.modules.ai.adapter import AIGroup
from yonerai_discord.modules.personal_memory.adapter import MemoryGroup
from yonerai_discord.v0_runtime.command_service import AICommand, MemoryCommand


class _Response:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, object]]] = []

    def is_done(self) -> bool:
        return False

    async def send_message(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))


class _Followup:
    async def send(self, _content: str, **_kwargs: object) -> None:
        raise AssertionError("initial response path was expected")


class _Guard:
    def currently_allowed(self, _capability_id: str, **_kwargs: object) -> bool:
        return True

    async def evaluate_fresh_member(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("fetch failure must stop before policy evaluation")


class _Guild:
    id = 10
    owner_id = 999

    async def fetch_member(self, _user_id: int) -> object:
        response = SimpleNamespace(status=403, reason="Forbidden")
        raise discord.Forbidden(response, "fresh member unavailable")  # type: ignore[arg-type]


def _interaction() -> SimpleNamespace:
    settings = SimpleNamespace(bot_owner_ids=frozenset(), moderator_role_ids=frozenset(), trusted_role_ids=frozenset())
    user = SimpleNamespace(
        id=20,
        roles=(),
        guild_permissions=SimpleNamespace(manage_guild=False, administrator=False),
    )
    return SimpleNamespace(
        user=user,
        guild=_Guild(),
        guild_id=10,
        channel_id=30,
        client=SimpleNamespace(is_closing=False, settings=settings, capability_guard=_Guard()),
        response=_Response(),
        followup=_Followup(),
    )


@pytest.mark.asyncio
async def test_ai_reset_fetch_failure_is_fail_closed_before_reset_port() -> None:
    calls = 0

    class Commands:
        async def execute_ai(self, *_args: object, **_kwargs: object) -> object:
            nonlocal calls
            calls += 1
            raise AssertionError("reset port must not run")

    interaction = _interaction()
    group = AIGroup(SimpleNamespace(available=False), v0_commands=Commands())  # type: ignore[arg-type]

    await group._run_v0(interaction, AICommand.RESET, path="ai reset")  # type: ignore[arg-type]

    assert calls == 0
    assert interaction.response.messages[0][1]["ephemeral"] is True
    assert "変更しませんでした" in interaction.response.messages[0][0]


@pytest.mark.asyncio
async def test_memory_write_fetch_failure_is_fail_closed_before_repository() -> None:
    calls = 0

    class Commands:
        def execute_memory(self, *_args: object, **_kwargs: object) -> object:
            nonlocal calls
            calls += 1
            raise AssertionError("memory repository must not run")

    interaction = _interaction()
    group = MemoryGroup(SimpleNamespace(), v0_commands=Commands())  # type: ignore[arg-type]

    await group._run_v0(  # type: ignore[arg-type]
        interaction,
        MemoryCommand.REMEMBER,
        "must not persist",
        path="memory remember",
    )

    assert calls == 0
    assert interaction.response.messages[0][1]["ephemeral"] is True
    assert "変更しませんでした" in interaction.response.messages[0][0]
