from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yonerai_discord.capabilities import build_capability_registry
from yonerai_discord.config import Settings
from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.db import Database
from yonerai_discord.discord_guard import CapabilityCommandTree, CapabilityGuard
from yonerai_discord.modules.operations import GateDecision


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def is_done(self) -> bool:
        return False

    async def send_message(self, message: str, **_: Any) -> None:
        self.messages.append(message)


class FakeFollowup:
    async def send(self, *_: Any, **__: Any) -> None:
        raise AssertionError("followup should not be used")


class FakeBot:
    is_closing = False

    async def is_owner(self, _: Any) -> bool:
        return False


def interaction(path: tuple[str, str]) -> SimpleNamespace:
    permissions = SimpleNamespace(
        administrator=False,
        manage_guild=False,
        moderate_members=False,
        manage_messages=False,
        kick_members=False,
        ban_members=False,
    )
    user = SimpleNamespace(id=100, guild_permissions=permissions, roles=())
    return SimpleNamespace(
        data={"name": path[0], "options": [{"type": 1, "name": path[1]}]},
        user=user,
        guild=SimpleNamespace(owner_id=999),
        guild_id=123,
        response=FakeResponse(),
        followup=FakeFollowup(),
    )


@pytest.mark.asyncio
async def test_guard_allows_everyone_entry_and_audits_denial_without_body(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    settings = Settings.from_env(
        {
            "DISCORD_TOKEN": "unused-test-token",
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
        }
    )
    database = Database(tmp_path / "guard.sqlite3")
    database.open()
    database.migrate()
    try:
        registry = build_capability_registry(settings, database)
        guard = CapabilityGuard(FakeBot(), settings, registry, database)

        ping = interaction(("system", "ping"))
        assert await guard.check(ping)
        assert not ping.response.messages

        ask = interaction(("ai", "ask"))
        assert not await guard.check(ask)
        assert "insufficient_level" in ask.response.messages[0]
        audit = database.list_audit()
        assert audit[-1].event == "capability.denied"
        assert audit[-1].details == {
            "capability_id": "cap-can-0161",
            "command": "ai ask",
            "decision_code": "insufficient_level",
        }
    finally:
        database.close()


@pytest.mark.asyncio
async def test_guard_rejects_unmapped_command_fail_closed(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    settings = Settings.from_env(
        {
            "DISCORD_TOKEN": "unused-test-token",
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
        }
    )
    database = Database(tmp_path / "guard.sqlite3")
    database.open()
    database.migrate()
    try:
        guard = CapabilityGuard(FakeBot(), settings, build_capability_registry(settings, database), database)
        unknown = interaction(("prototype", "danger"))
        assert not await guard.check(unknown)
        assert "未登録" in unknown.response.messages[0]
        assert database.list_audit()[-1].details["decision_code"] == "unmapped_command"
    finally:
        database.close()


def test_event_guard_is_fail_closed_and_rate_limited(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    settings = Settings.from_env(
        {
            "DISCORD_TOKEN": "unused-test-token",
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
        }
    )
    database = Database(tmp_path / "event-guard.sqlite3")
    database.open()
    database.migrate()
    try:
        registry = build_capability_registry(settings, database)
        capability_id = "cap-run-automod-message-create"
        database.set_capability_override(capability_id, True, updated_by=1)
        guard = CapabilityGuard(FakeBot(), settings, registry, database)

        for event_id in range(1, 31):
            assert guard.event_allowed(
                capability_id,
                surface="automod_message_create",
                guild_id=1,
                channel_id=2,
                event_id=event_id,
                user_id=3,
            )
        assert not guard.event_allowed(
            capability_id,
            surface="automod_message_create",
            guild_id=1,
            channel_id=2,
            event_id=31,
            user_id=3,
        )
        for event_id in range(32, 132):
            assert not guard.event_allowed(
                capability_id,
                surface="automod_message_create",
                guild_id=1,
                channel_id=2,
                event_id=event_id,
                user_id=3,
            )
        denied = [record for record in database.list_audit(limit=200) if record.event == "capability.event_denied"]
        assert len(denied) == 1
        assert denied[0].details["decision_code"] == "input_gate.rate_limited"
    finally:
        database.close()


def test_actor_triggered_event_applies_configurable_rbac(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    settings = Settings.from_env(
        {
            "DISCORD_TOKEN": "unused-test-token",
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
            "AI_MENTION_ENABLED": "true",
            "AI_MENTION_GUILD_IDS": "1",
            "AI_ALLOW_REMOTE": "true",
            "OPENAI_API_KEY": "offline-test-key",
            "AI_SAFETY_IDENTIFIER_SECRET": "v7Kp2mQ9xL4sN8dR5tW1yH6cF3zB0jUa",
        }
    )
    database = Database(tmp_path / "event-rbac.sqlite3")
    database.open()
    database.migrate()
    try:
        capability_id = "cap-run-ai-mention-chat"
        database.set_level_override(capability_id, RbacLevel.BOT_OWNER, guild_id=1, updated_by=1)
        registry = build_capability_registry(settings, database)
        registry.set_runtime_availability(capability_id, True)
        guard = CapabilityGuard(FakeBot(), settings, registry, database)

        assert not guard.event_allowed(
            capability_id,
            surface="ai_mention_message",
            guild_id=1,
            channel_id=2,
            event_id=1,
            user_id=3,
            actor_level=RbacLevel.EVERYONE,
        )
        assert guard.event_allowed(
            capability_id,
            surface="ai_mention_message",
            guild_id=1,
            channel_id=2,
            event_id=2,
            user_id=4,
            actor_level=RbacLevel.BOT_OWNER,
        )
        denied = [record for record in database.list_audit() if record.event == "capability.event_denied"]
        assert denied[-1].details["decision_code"] == "insufficient_level"
    finally:
        database.close()


def test_owner_direct_access_and_delegated_actor_access_stay_separate(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    settings = Settings.from_env(
        {
            "DISCORD_TOKEN": "unused-test-token",
            "BOT_OWNER_IDS": "1",
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
        }
    )
    database = Database(tmp_path / "actor-grants.sqlite3")
    database.open()
    database.migrate()
    capability_id = "cap-run-site-auto-publish"
    try:
        database.set_owner_managed_capability_enabled(
            capability_id,
            True,
            10,
            updated_by=1,
        )
        database.set_capability_actor_grant(
            capability_id,
            2,
            True,
            10,
            granted_by=1,
        )
        registry = build_capability_registry(settings, database)
        registry.set_runtime_availability(capability_id, True)
        guard = CapabilityGuard(FakeBot(), settings, registry, database)

        assert guard.currently_allowed(
            capability_id,
            guild_id=10,
            user_id=1,
            actor_level=RbacLevel.BOT_OWNER,
            floor=RbacLevel.TRUSTED,
        )
        assert guard.currently_allowed(
            capability_id,
            guild_id=10,
            user_id=2,
            actor_level=RbacLevel.EVERYONE,
            floor=RbacLevel.TRUSTED,
        )
        assert not guard.currently_allowed(
            capability_id,
            guild_id=10,
            user_id=3,
            actor_level=RbacLevel.EVERYONE,
            floor=RbacLevel.TRUSTED,
        )
        assert not guard.currently_allowed(
            capability_id,
            guild_id=11,
            user_id=2,
            actor_level=RbacLevel.EVERYONE,
            floor=RbacLevel.TRUSTED,
        )

        database.set_capability_actor_grant(capability_id, 2, False, 10, granted_by=1)
        assert not guard.currently_allowed(
            capability_id,
            guild_id=10,
            user_id=2,
            actor_level=RbacLevel.EVERYONE,
            floor=RbacLevel.TRUSTED,
        )

        database.set_capability_actor_grant(capability_id, 2, True, 10, granted_by=1)
        registry.set_runtime_availability(capability_id, False)
        assert not guard.currently_allowed(
            capability_id,
            guild_id=10,
            user_id=2,
            actor_level=RbacLevel.EVERYONE,
            floor=RbacLevel.TRUSTED,
        )
        registry.set_runtime_availability(capability_id, True)
        database.set_owner_managed_capability_enabled(capability_id, False, 10, updated_by=1)
        assert not guard.currently_allowed(
            capability_id,
            guild_id=10,
            user_id=2,
            actor_level=RbacLevel.EVERYONE,
            floor=RbacLevel.TRUSTED,
        )

        unrelated = "cap-run-site-publish"
        database.set_capability_actor_grant(unrelated, 2, True, 10, granted_by=1)
        assert not guard.currently_allowed(
            unrelated,
            guild_id=10,
            user_id=2,
            actor_level=RbacLevel.EVERYONE,
            floor=RbacLevel.TRUSTED,
        )
    finally:
        database.close()


def test_actor_triggered_dm_event_keeps_none_scope_and_uses_input_gate(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    settings = Settings.from_env(
        {
            "DISCORD_TOKEN": "unused-test-token",
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
        }
    )
    database = Database(tmp_path / "dm-event.sqlite3")
    database.open()
    database.migrate()
    try:
        capability_id = "cap-run-ai-attachment-understand"
        database.set_capability_override(capability_id, True, updated_by=1)
        registry = build_capability_registry(settings, database)
        registry.set_runtime_availability(capability_id, True)
        guard = CapabilityGuard(FakeBot(), settings, registry, database)

        assert guard.event_allowed(
            capability_id,
            surface="ai_attachment_understanding",
            guild_id=None,
            channel_id=2,
            event_id=1,
            user_id=3,
            actor_level=RbacLevel.EVERYONE,
        )
        assert not guard.event_allowed(
            capability_id,
            surface="ai_attachment_understanding",
            guild_id=None,
            channel_id=2,
            event_id=1,
            user_id=3,
            actor_level=RbacLevel.EVERYONE,
        )
        denied = [record for record in database.list_audit() if record.event == "capability.event_denied"]
        assert denied[-1].details["decision_code"] == "input_gate.duplicate"
    finally:
        database.close()


@pytest.mark.asyncio
async def test_shutdown_gate_blocks_tree_interaction_and_event_without_consuming_input_gate(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    settings = Settings.from_env(
        {
            "DISCORD_TOKEN": "unused-test-token",
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
        }
    )
    database = Database(tmp_path / "shutdown-guard.sqlite3")
    database.open()
    database.migrate()
    try:
        bot = FakeBot()
        bot.is_closing = True
        registry = build_capability_registry(settings, database)
        guard = CapabilityGuard(bot, settings, registry, database)

        class CountingGate:
            def __init__(self) -> None:
                self.calls = 0

            def evaluate(self, *_args: object, **_kwargs: object) -> GateDecision:
                self.calls += 1
                return GateDecision.ALLOW

        counting_gate = CountingGate()
        guard.input_gate = counting_gate  # type: ignore[assignment]
        command = interaction(("system", "ping"))
        command.channel_id = 2
        command.id = 3
        assert await guard.check(command) is False
        assert "停止処理中" in command.response.messages[0]

        direct = interaction(("system", "ping"))
        assert (
            await guard.check_capability(
                direct,
                "cap-can-0001",
                surface="system ping",
            )
            is False
        )
        assert (
            guard.event_allowed(
                "cap-run-automod-message-create",
                surface="automod_message_create",
                guild_id=1,
                channel_id=2,
                event_id=4,
                user_id=5,
            )
            is False
        )
        assert guard.currently_allowed("cap-can-0001", guild_id=1, user_id=5) is False
        assert counting_gate.calls == 0

        tree_interaction = interaction(("system", "ping"))
        tree = SimpleNamespace(client=bot)
        assert await CapabilityCommandTree.interaction_check(tree, tree_interaction) is False  # type: ignore[arg-type]
        assert "停止処理中" in tree_interaction.response.messages[0]
    finally:
        database.close()
