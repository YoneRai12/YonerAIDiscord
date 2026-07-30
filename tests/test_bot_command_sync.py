from __future__ import annotations

from types import SimpleNamespace

import pytest

from yonerai_discord.bot import YonerAIBot
from yonerai_discord.config import Settings


def _settings(**overrides: str) -> Settings:
    return Settings.from_env({"DISCORD_TOKEN": "offline-test-token"} | overrides)


@pytest.mark.asyncio
async def test_command_sync_targets_each_guild_and_overrides_global_sync(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = YonerAIBot(
        _settings(
            DISCORD_GUILD_ID="200",
            COMMAND_SYNC_GUILD_IDS="300,200,100,300",
            SYNC_GLOBAL_COMMANDS="true",
        )
    )
    copied_guild_ids: list[int] = []
    synced_guild_ids: list[int | None] = []

    def copy_global_to(*, guild: object) -> None:
        copied_guild_ids.append(guild.id)  # type: ignore[attr-defined]

    async def sync(*, guild: object | None = None) -> list[object]:
        synced_guild_ids.append(None if guild is None else guild.id)  # type: ignore[attr-defined]
        return [object(), object()]

    monkeypatch.setattr(bot.tree, "copy_global_to", copy_global_to)
    monkeypatch.setattr(bot.tree, "sync", sync)
    bot.surface_inventory = SimpleNamespace(actual_command_paths=frozenset({"system health"}))
    caplog.set_level("INFO", logger="yonerai_discord.bot")

    try:
        await bot._sync_application_commands()
    finally:
        await bot.close()

    assert copied_guild_ids == [100, 200, 300]
    assert synced_guild_ids == [100, 200, 300]
    assert "global_command_sync_skipped_for_guild_targets" in caplog.messages
    assert caplog.messages.count("commands_sync_finished") == 3


@pytest.mark.asyncio
async def test_command_sync_logs_every_guild_and_fails_after_partial_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = YonerAIBot(_settings(COMMAND_SYNC_GUILD_IDS="100,200,300"))
    synced_guild_ids: list[int] = []

    def copy_global_to(*, guild: object) -> None:
        return None

    async def sync(*, guild: object | None = None) -> list[object]:
        assert guild is not None
        synced_guild_ids.append(guild.id)  # type: ignore[attr-defined]
        if guild.id == 200:  # type: ignore[attr-defined]
            raise RuntimeError("synthetic Discord failure")
        return [object()]

    monkeypatch.setattr(bot.tree, "copy_global_to", copy_global_to)
    monkeypatch.setattr(bot.tree, "sync", sync)
    bot.surface_inventory = SimpleNamespace(actual_command_paths=frozenset())
    caplog.set_level("INFO", logger="yonerai_discord.bot")

    try:
        with pytest.raises(RuntimeError, match="200"):
            await bot._sync_application_commands()
    finally:
        await bot.close()

    assert synced_guild_ids == [100, 200, 300]
    assert caplog.messages.count("commands_sync_finished") == 2
    assert caplog.messages.count("commands_sync_failed") == 1
