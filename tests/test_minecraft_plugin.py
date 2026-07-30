from __future__ import annotations

from types import SimpleNamespace

import pytest

from yonerai_discord.modules.minecraft import MinecraftPlugin, setup


def test_setup_registers_minecraft_plugin() -> None:
    calls = []
    setup(SimpleNamespace(register=lambda name, factory: calls.append((name, factory))))
    assert calls == [("minecraft", MinecraftPlugin)]


@pytest.mark.asyncio
async def test_plugin_registers_only_configured_status_group() -> None:
    added = []
    removed = []
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            minecraft_host="127.0.0.1",
            minecraft_port=25_565,
            minecraft_timeout_seconds=1.0,
            minecraft_allow_public=False,
            minecraft_max_packet_bytes=16_384,
        ),
        tree=SimpleNamespace(add_command=added.append, remove_command=removed.append),
    )
    plugin = MinecraftPlugin()

    await plugin.start(bot)

    assert len(added) == 1
    assert added[0].name == "minecraft"
    assert [command.name for command in added[0].commands] == ["status"]
    assert bot.minecraft_status_client.target.host == "127.0.0.1"

    await plugin.stop()
    assert removed == ["minecraft"]
    assert not hasattr(bot, "minecraft_status_client")
    await plugin.stop()
