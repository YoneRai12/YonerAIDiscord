from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from yonerai_discord.modules.ai.adapter import AIGroup
from yonerai_discord.modules.ai.state_repository import AIStateRepository
from yonerai_discord.modules.personal_memory import PersonalMemoryService, SqlitePersonalMemoryRepository
from yonerai_discord.modules.personal_memory.adapter import MemoryGroup
from yonerai_discord.v0_contracts import MemoryVisibility, Scope
from yonerai_discord.v0_runtime.command_service import AICommand, CommandResult, MemoryCommand, V0CommandService
from yonerai_discord.v0_runtime.integration import ExplicitMemoryCommandAdapter
from yonerai_discord.v0_runtime.memory_repository import V0ExplicitMemoryRepository


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


class _AllowingGuard:
    def currently_allowed(self, _capability_id: str, **_kwargs: object) -> bool:
        return True

    async def evaluate_fresh_member(self, *_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(allowed=True, actor_level="everyone")


class _AvailableGuild:
    id = 10
    owner_id = 999

    def __init__(self, member: object) -> None:
        self._member = member

    async def fetch_member(self, _user_id: int) -> object:
        return self._member


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


def _allowed_interaction(*, guard: object | None = None) -> SimpleNamespace:
    settings = SimpleNamespace(bot_owner_ids=frozenset(), moderator_role_ids=frozenset(), trusted_role_ids=frozenset())
    user = SimpleNamespace(
        id=20,
        roles=(),
        guild_permissions=SimpleNamespace(manage_guild=False, administrator=False),
    )
    return SimpleNamespace(
        user=user,
        guild=_AvailableGuild(user),
        guild_id=10,
        channel_id=30,
        client=SimpleNamespace(
            is_closing=False,
            settings=settings,
            capability_guard=_AllowingGuard() if guard is None else guard,
        ),
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


@pytest.mark.asyncio
async def test_memory_clear_fetch_failure_reaches_neither_store() -> None:
    calls = 0

    class Commands:
        def execute_memory(self, *_args: object, **_kwargs: object) -> object:
            nonlocal calls
            calls += 1
            raise AssertionError("neither memory store may be cleared")

    interaction = _interaction()
    group = MemoryGroup(SimpleNamespace(), v0_commands=Commands())  # type: ignore[arg-type]

    await group._run_v0(  # type: ignore[arg-type]
        interaction,
        MemoryCommand.CLEAR,
        "CLEAR_MY_MEMORY",
        path="memory clear",
    )

    assert calls == 0
    assert interaction.response.messages[0][1]["ephemeral"] is True
    assert "変更しませんでした" in interaction.response.messages[0][0]


@pytest.mark.asyncio
async def test_memory_clear_current_capability_revoke_reaches_neither_store() -> None:
    v0_calls = 0
    legacy_calls = 0

    class RevokingGuard(_AllowingGuard):
        def __init__(self) -> None:
            self.calls = 0

        def currently_allowed(self, _capability_id: str, **_kwargs: object) -> bool:
            self.calls += 1
            return self.calls == 1

    class V0Memory:
        def clear(self, _actor: object) -> int:
            nonlocal v0_calls
            v0_calls += 1
            return 1

    class LegacyService:
        def clear(self, _guild_id: int, _user_id: int) -> int:
            nonlocal legacy_calls
            legacy_calls += 1
            return 1

    interaction = _allowed_interaction(guard=RevokingGuard())
    group = MemoryGroup(LegacyService(), v0_commands=V0CommandService(memory=V0Memory()))  # type: ignore[arg-type]

    await group._run_v0(  # type: ignore[arg-type]
        interaction,
        MemoryCommand.CLEAR,
        "CLEAR_MY_MEMORY",
        path="memory clear",
    )

    assert v0_calls == 0
    assert legacy_calls == 0
    assert "変更しませんでした" in interaction.response.messages[0][0]


@pytest.mark.asyncio
async def test_memory_clear_fresh_revoke_after_v0_reports_partial_without_legacy_delete() -> None:
    v0_calls = 0
    legacy_calls = 0

    class RevokingFreshGuard(_AllowingGuard):
        def __init__(self) -> None:
            self.evaluate_calls = 0

        async def evaluate_fresh_member(self, *_args: object, **_kwargs: object) -> object:
            self.evaluate_calls += 1
            return SimpleNamespace(allowed=self.evaluate_calls == 1, actor_level="everyone")

    class V0Memory:
        def clear(self, _actor: object) -> int:
            nonlocal v0_calls
            v0_calls += 1
            return 1

    class LegacyService:
        def clear(self, _guild_id: int, _user_id: int) -> int:
            nonlocal legacy_calls
            legacy_calls += 1
            return 1

    interaction = _allowed_interaction(guard=RevokingFreshGuard())
    group = MemoryGroup(LegacyService(), v0_commands=V0CommandService(memory=V0Memory()))  # type: ignore[arg-type]

    await group._run_v0(  # type: ignore[arg-type]
        interaction,
        MemoryCommand.CLEAR,
        "CLEAR_MY_MEMORY",
        path="memory clear",
    )

    content = interaction.response.messages[0][0]
    assert v0_calls == 1
    assert legacy_calls == 0
    assert "一部だけ削除された可能性" in content
    assert "変更しませんでした" not in content


@pytest.mark.asyncio
async def test_memory_clear_final_revoke_after_both_deletes_reports_content_free_completion() -> None:
    state = {"legacy_deleted": False}

    class FinalRevokingGuard(_AllowingGuard):
        def currently_allowed(self, _capability_id: str, **_kwargs: object) -> bool:
            return not state["legacy_deleted"]

    class V0Memory:
        def clear(self, _actor: object) -> int:
            return 4

    class LegacyService:
        def clear(self, _guild_id: int, _user_id: int) -> int:
            state["legacy_deleted"] = True
            return 3

    interaction = _allowed_interaction(guard=FinalRevokingGuard())
    group = MemoryGroup(LegacyService(), v0_commands=V0CommandService(memory=V0Memory()))  # type: ignore[arg-type]

    await group._run_v0(  # type: ignore[arg-type]
        interaction,
        MemoryCommand.CLEAR,
        "CLEAR_MY_MEMORY",
        path="memory clear",
    )

    content = interaction.response.messages[0][0]
    assert state["legacy_deleted"] is True
    assert "削除処理は完了" in content
    assert "変更しませんでした" not in content
    assert "7" not in content


@pytest.mark.asyncio
async def test_memory_clear_removes_both_stores_without_cross_scope_deletion(tmp_path: Path) -> None:
    database_path = tmp_path / "memory-clear.sqlite3"
    repository = SqlitePersonalMemoryRepository(database_path)
    repository.open()
    state = AIStateRepository(database_path)
    v0_repository = V0ExplicitMemoryRepository(state)
    service = PersonalMemoryService(repository)
    own_scope = Scope(10, 20, visibility=MemoryVisibility.USER_PRIVATE)
    other_user_scope = Scope(10, 21, visibility=MemoryVisibility.USER_PRIVATE)
    other_guild_scope = Scope(11, 20, visibility=MemoryVisibility.USER_PRIVATE)
    try:
        for guild_id, user_id in ((10, 20), (10, 21), (11, 20)):
            service.enable(guild_id, user_id)
            service.remember(guild_id, user_id, f"legacy-{guild_id}-{user_id}")
        for scope, content in (
            (own_scope, "v0-own"),
            (other_user_scope, "v0-other-user"),
            (other_guild_scope, "v0-other-guild"),
        ):
            v0_repository.remember(scope, content)

        group = MemoryGroup(
            service,
            v0_commands=V0CommandService(memory=ExplicitMemoryCommandAdapter(v0_repository)),
        )
        interaction = _allowed_interaction()

        await group._run_v0(  # type: ignore[arg-type]
            interaction,
            MemoryCommand.CLEAR,
            "CLEAR_MY_MEMORY",
            path="memory clear",
        )

        assert service.list_items(10, 20) == ()
        assert service.search(10, 20, "legacy") == ()
        assert service.export_payload(10, 20)["items"] == []
        assert v0_repository.list(own_scope) == ()
        assert len(service.list_items(10, 21)) == 1
        assert len(service.list_items(11, 20)) == 1
        assert len(v0_repository.list(other_user_scope)) == 1
        assert len(v0_repository.list(other_guild_scope)) == 1
        assert interaction.response.messages[0][1]["ephemeral"] is True
    finally:
        state.close()
        repository.close()


@pytest.mark.asyncio
async def test_memory_clear_partial_failure_is_truthful_and_content_free() -> None:
    class Commands:
        def execute_memory(self, *_args: object, **_kwargs: object) -> CommandResult:
            return CommandResult(True, "memory_cleared", {"deleted": 1})

    class FailingLegacyService:
        def clear(self, _guild_id: int, _user_id: int) -> int:
            raise RuntimeError("SECRET-EXCEPTION-BODY")

    interaction = _allowed_interaction()
    group = MemoryGroup(FailingLegacyService(), v0_commands=Commands())  # type: ignore[arg-type]

    await group._run_v0(  # type: ignore[arg-type]
        interaction,
        MemoryCommand.CLEAR,
        "CLEAR_MY_MEMORY",
        path="memory clear",
    )

    content = interaction.response.messages[0][0]
    assert "一部だけ削除された可能性" in content
    assert "もう一度 `/memory clear`" in content
    assert "SECRET-EXCEPTION-BODY" not in content
