from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest

from yonerai_discord.modules.jp_information import (
    DiscordJpInformationAdapter,
    ForecastPeriod,
    JpInformationPlugin,
    Region,
    WarningItem,
    WarningReport,
    WeatherForecast,
    escape_discord_text,
    render_warning,
    render_weather,
    setup,
)


NOW = datetime(2026, 7, 21, 3, 0, tzinfo=UTC)


def malicious_forecast() -> WeatherForecast:
    return WeatherForecast(
        region=Region("130000", "<script>@everyone **東京**</script>"),
        publishing_office="気象庁 @here",
        issued_at=NOW,
        retrieved_at=NOW,
        headline="[危険](https://example.invalid) @everyone",
        periods=(ForecastPeriod(NOW, "東京`地方", "晴れ **強調**"),),
        source_url="https://www.jma.go.jp/bosai/forecast/data/forecast/130000.json",
    )


def test_renderers_escape_html_markdown_and_mentions_and_show_provenance() -> None:
    weather = render_weather(malicious_forecast())
    assert "<script>" not in weather
    assert "@everyone" not in weather
    assert "**東京**" not in weather
    assert "発表時刻:" in weather and "取得時刻:" in weather
    assert "気象庁の公開JSONを要約・整形" in weather

    warning = render_warning(
        WarningReport(
            region=Region("130000", "東京都"),
            publishing_office="気象庁",
            issued_at=NOW,
            retrieved_at=NOW,
            headline=None,
            warnings=(WarningItem("東京", "大雨警報 @everyone", "発表"),),
            source_url="https://www.jma.go.jp/bosai/warning/data/warning/130000.json",
        )
    )
    assert "@everyone" not in warning
    assert "独自予報ではありません" in warning
    assert escape_discord_text("<b> **x** @here") != "<b> **x** @here"


class Response:
    def __init__(self) -> None:
        self.deferred = False
        self.messages = []

    async def defer(self, **kwargs):
        self.deferred = True
        self.defer_kwargs = kwargs

    async def send_message(self, content, **kwargs):
        self.messages.append((content, kwargs))


class Followup:
    def __init__(self) -> None:
        self.messages = []

    async def send(self, content, **kwargs):
        self.messages.append((content, kwargs))


class StubService:
    async def get_weather(self, _region):
        return malicious_forecast()


class BlockingService:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def get_weather(self, _region):
        self.entered.set()
        await self.release.wait()
        return malicious_forecast()


@pytest.mark.asyncio
async def test_adapter_uses_allowed_mentions_none() -> None:
    adapter = DiscordJpInformationAdapter(StubService())  # type: ignore[arg-type]
    interaction = SimpleNamespace(response=Response(), followup=Followup(), guild_id=1)
    await adapter.weather(interaction, "東京都")
    assert interaction.response.deferred is True
    _, kwargs = interaction.followup.messages[0]
    mentions = kwargs["allowed_mentions"]
    assert isinstance(mentions, discord.AllowedMentions)
    assert mentions.everyone is False
    assert mentions.users is False
    assert mentions.roles is False
    assert mentions.replied_user is False


@pytest.mark.asyncio
async def test_adapter_rechecks_capability_before_provider_result_followup() -> None:
    service = BlockingService()
    allowed = True

    def capability_check(*_args):
        return allowed

    adapter = DiscordJpInformationAdapter(service, capability_check=capability_check)  # type: ignore[arg-type]
    interaction = SimpleNamespace(response=Response(), followup=Followup(), guild_id=1)
    task = asyncio.create_task(adapter.weather(interaction, "東京都"))
    await service.entered.wait()
    allowed = False
    service.release.set()
    await task
    assert interaction.response.deferred is True
    assert interaction.followup.messages == []


class Tree:
    def __init__(self) -> None:
        self.commands = {}

    def add_command(self, command):
        self.commands[command.name] = command

    def remove_command(self, name):
        return self.commands.pop(name, None)


class SharedSession:
    async def get(self, *_args, **_kwargs):
        raise AssertionError("plugin start must not perform network I/O")


@pytest.mark.asyncio
async def test_plugin_installs_commands_without_resident_worker_or_startup_network() -> None:
    tree = Tree()
    bot = SimpleNamespace(
        settings=SimpleNamespace(),
        tree=tree,
        jp_information_http_session=SharedSession(),
        capability_registry=SimpleNamespace(
            capability_status=lambda *_args: SimpleNamespace(executable=True),
        ),
    )
    plugin = JpInformationPlugin()
    await plugin.start(bot)
    try:
        assert set(tree.commands) == {"weather", "warning", "holiday"}
        assert {command.qualified_name for command in tree.commands["holiday"].walk_commands()} == {
            "holiday next",
            "holiday year",
        }
        assert not hasattr(plugin, "worker")
        assert not hasattr(plugin, "_task")
        assert bot.jp_information_service is plugin.service
    finally:
        await plugin.stop()
    assert tree.commands == {}
    assert not hasattr(bot, "jp_information_service")


@pytest.mark.asyncio
async def test_plugin_begin_close_blocks_registry_and_is_idempotent() -> None:
    tree = Tree()
    bot = SimpleNamespace(
        settings=SimpleNamespace(),
        tree=tree,
        jp_information_http_session=SharedSession(),
        is_closing=False,
        capability_registry=SimpleNamespace(
            capability_status=lambda *_args: SimpleNamespace(executable=True),
        ),
    )
    plugin = JpInformationPlugin()
    await plugin.start(bot)
    assert plugin.adapter is not None
    interaction = SimpleNamespace(guild_id=1)
    assert await plugin.adapter._allowed(plugin.capability_ids.weather, interaction) is True
    await plugin.begin_close()
    await plugin.begin_close()
    assert await plugin.adapter._allowed(plugin.capability_ids.weather, interaction) is False
    await plugin.stop()


def test_setup_registers_stable_plugin_factory() -> None:
    calls = []
    setup(SimpleNamespace(register=lambda name, factory: calls.append((name, factory))))
    assert calls == [("jp_information", JpInformationPlugin)]
