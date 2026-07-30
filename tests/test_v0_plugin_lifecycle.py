from __future__ import annotations

from types import SimpleNamespace

import pytest

from yonerai_discord.modules.ai import AIPlugin
from yonerai_discord.modules.personal_memory import PersonalMemoryPlugin
from yonerai_discord.v0_contracts import Scope
from yonerai_discord.v0_runtime.memory_repository import V0ExplicitMemoryRepository


class _Tree:
    def __init__(self) -> None:
        self.commands: dict[str, object] = {}

    def add_command(self, command: object) -> None:
        self.commands[str(getattr(command, "name"))] = command

    def remove_command(self, name: str) -> object | None:
        return self.commands.pop(name, None)


def _bot(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(
            database_path=tmp_path / "state.sqlite3",
            ai_conversation_ttl_seconds=7_200,
            ai_conversation_max_turns=12,
            ai_conversation_max_sessions=128,
            ai_conversation_max_total_binary_bytes=64 * 1024 * 1024,
            ai_attachment_max_file_bytes=8 * 1024 * 1024,
            ai_attachment_max_total_bytes=16 * 1024 * 1024,
            ai_attachment_max_files=4,
            ai_base_url="",
            ai_mention_enabled=False,
        ),
        tree=_Tree(),
        is_closing=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("startup_order", [("ai", "memory"), ("memory", "ai")])
async def test_v0_memory_connections_survive_plugin_start_and_stop_order(tmp_path, startup_order) -> None:
    bot = _bot(tmp_path)
    ai = AIPlugin()
    memory = PersonalMemoryPlugin()
    plugins = {"ai": ai, "memory": memory}
    for name in startup_order:
        await plugins[name].start(bot)

    assert ai._state_repository is not None
    assert memory.v0_state is not None
    assert ai._state_repository is not memory.v0_state
    ai_repository = V0ExplicitMemoryRepository(ai._state_repository)
    memory_repository = V0ExplicitMemoryRepository(memory.v0_state)
    scope = Scope(10, 20)
    memory_repository.remember(scope, "memory plugin write")
    assert [record.content for record in ai_repository.list(scope)] == ["memory plugin write"]

    first, second = startup_order
    await plugins[first].stop()
    survivor = memory_repository if second == "memory" else ai_repository
    survivor.remember(scope, f"{second} survives")
    assert {record.content for record in survivor.list(scope)} == {
        "memory plugin write",
        f"{second} survives",
    }

    await plugins[second].stop()
    assert bot.tree.commands == {}
    assert not hasattr(bot, "v0_explicit_memory_repository")
