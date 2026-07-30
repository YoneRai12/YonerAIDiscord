from __future__ import annotations

from types import SimpleNamespace

import pytest

from yonerai_discord.modules.yonerai import YonerAIPlugin, setup


def test_setup_registers_yonerai_plugin() -> None:
    calls = []
    setup(SimpleNamespace(register=lambda name, factory: calls.append((name, factory))))
    assert calls == [("yonerai", YonerAIPlugin)]


@pytest.mark.asyncio
async def test_plugin_registers_only_status_and_health() -> None:
    added = []
    removed = []
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            yonerai_enabled=False,
            yonerai_allow_remote=False,
            yonerai_remote_status_opt_in=False,
        ),
        tree=SimpleNamespace(add_command=added.append, remove_command=removed.append),
    )
    plugin = YonerAIPlugin()

    await plugin.start(bot)

    assert len(added) == 1
    assert added[0].name == "yonerai"
    assert [command.name for command in added[0].commands] == ["status", "health"]
    assert bot.yonerai_status_service is plugin.service

    await plugin.stop()
    assert removed == ["yonerai"]
    assert not hasattr(bot, "yonerai_status_service")
    await plugin.stop()
