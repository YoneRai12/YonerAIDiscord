from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import discord
import pytest

from yonerai_discord.config import SAFE_DEFAULT_PLUGINS, Settings
from yonerai_discord.capabilities import MODEL_TOOL_CAPABILITY_BINDINGS
from yonerai_discord.modules.nasa_apod import (
    NASA_APOD_CAPABILITY_ID,
    ApodConfigurationError,
    ApodItem,
    DiscordNasaApodAdapter,
    NasaApiApodSource,
    NasaApodPlugin,
    StaticApodSource,
    render_apod_embed,
    render_apod_text,
    setup,
)
from yonerai_discord.runtime_manifests.web_search import OPENAI_PAID_WEB_SEARCH_CAPABILITY_ID


def apod_item(*, media_type: str = "image") -> ApodItem:
    return ApodItem(
        day=date(2026, 7, 23),
        title="星空 @everyone **強調**",
        explanation="NASAの公開metadataです。",
        media_type=media_type,  # type: ignore[arg-type]
        url="https://example.invalid/media",
        copyright="Example photographer",
    )


class Response:
    def __init__(self) -> None:
        self.deferred = False
        self.messages: list[tuple[object, dict[str, object]]] = []

    async def defer(self, **kwargs: object) -> None:
        self.deferred = True
        self.defer_kwargs = kwargs

    async def send_message(self, content: object, **kwargs: object) -> None:
        self.messages.append((content, kwargs))


class Followup:
    def __init__(self) -> None:
        self.messages: list[tuple[object, dict[str, object]]] = []

    async def send(self, content: object = None, **kwargs: object) -> None:
        self.messages.append((content, kwargs))


class Tree:
    def __init__(self) -> None:
        self.commands: dict[str, object] = {}

    def add_command(self, command: object) -> None:
        self.commands[command.name] = command  # type: ignore[attr-defined]

    def remove_command(self, name: str) -> object | None:
        return self.commands.pop(name, None)


class CountingService:
    def __init__(self) -> None:
        self.calls = 0

    async def get(self, _value: object = None) -> ApodItem:
        self.calls += 1
        return apod_item()


def interaction() -> SimpleNamespace:
    async def fetch_member(user_id: int) -> SimpleNamespace:
        return SimpleNamespace(id=user_id, guild_permissions=SimpleNamespace())

    guild = SimpleNamespace(id=123, fetch_member=fetch_member)
    return SimpleNamespace(
        response=Response(),
        followup=Followup(),
        guild=guild,
        guild_id=123,
        user=SimpleNamespace(id=456),
    )


@pytest.mark.asyncio
async def test_adapter_uses_custom_card_numbered_links_and_mentions_none() -> None:
    service = CountingService()
    adapter = DiscordNasaApodAdapter(service, capability_check=lambda *_args: True)  # type: ignore[arg-type]
    target = interaction()

    await adapter.apod(target, "2026-07-23")

    assert service.calls == 1
    assert target.response.deferred is True
    _, kwargs = target.followup.messages[0]
    embed = kwargs["embed"]
    assert isinstance(embed, discord.Embed)
    assert "[1](" in (embed.description or "") and "[2](" in (embed.description or "")
    assert embed.image.url == "https://example.invalid/media"
    assert "Example photographer" in embed.fields[0].value
    assert "@everyone" not in embed.title
    mentions = kwargs["allowed_mentions"]
    assert isinstance(mentions, discord.AllowedMentions)
    assert mentions.everyone is False and mentions.users is False and mentions.roles is False
    assert kwargs["ephemeral"] is True

    video = render_apod_embed(apod_item(media_type="video"))
    assert not video.image.url
    assert "動画" in (video.description or "")


@pytest.mark.asyncio
async def test_adapter_rechecks_same_capability_before_and_after_fetch() -> None:
    service = CountingService()
    decisions = iter((True, False))
    checked: list[str] = []

    def check(capability_id: str, _interaction: object) -> bool:
        checked.append(capability_id)
        return next(decisions)

    adapter = DiscordNasaApodAdapter(service, capability_check=check)  # type: ignore[arg-type]
    target = interaction()
    await adapter.apod(target)

    assert service.calls == 1
    assert checked == [NASA_APOD_CAPABILITY_ID, NASA_APOD_CAPABILITY_ID]
    assert target.followup.messages == []

    denied_service = CountingService()
    denied = DiscordNasaApodAdapter(denied_service, capability_check=lambda *_args: False)  # type: ignore[arg-type]
    denied_target = interaction()
    await denied.apod(denied_target)
    assert denied_service.calls == 0
    assert denied_target.response.messages

    closing_service = CountingService()
    closing: DiscordNasaApodAdapter

    async def close_during_check(*_args: object) -> bool:
        closing.begin_close()
        return True

    closing = DiscordNasaApodAdapter(
        closing_service,
        capability_check=close_during_check,
    )
    closing_target = interaction()
    await closing.apod(closing_target)
    assert closing_service.calls == 0
    assert closing_target.response.messages


@pytest.mark.asyncio
async def test_plugin_static_source_installs_nasa_group_and_stops_cleanly() -> None:
    item = apod_item()
    tree = Tree()
    checked: list[str] = []
    registry = SimpleNamespace(
        capability_status=lambda *_args: SimpleNamespace(executable=True),
    )

    async def evaluate(capability_id: str, **_kwargs: object) -> SimpleNamespace:
        checked.append(capability_id)
        return SimpleNamespace(allowed=True, actor_level="everyone")

    guard = SimpleNamespace(
        evaluate_fresh_member=evaluate,
        currently_allowed=lambda *_args, **_kwargs: True,
    )
    bot = SimpleNamespace(
        tree=tree,
        settings=SimpleNamespace(nasa_apod_allow_remote=False, nasa_apod_api_key=""),
        capability_registry=registry,
        capability_guard=guard,
        is_closing=False,
    )
    plugin = NasaApodPlugin(source_factory=lambda _bot: StaticApodSource((item,)))

    await plugin.start(bot)
    try:
        assert set(tree.commands) == {"nasa"}
        assert {command.qualified_name for command in tree.commands["nasa"].walk_commands()} == {"nasa apod"}
        assert bot.nasa_apod_service is plugin.service
    finally:
        await plugin.stop()
    assert tree.commands == {}
    assert not hasattr(bot, "nasa_apod_service")
    check = plugin._capability_check(bot)
    assert await check(NASA_APOD_CAPABILITY_ID, interaction()) is True
    assert checked[-1] == NASA_APOD_CAPABILITY_ID


@pytest.mark.asyncio
async def test_plugin_capability_check_fails_closed_after_rbac_floor_change() -> None:
    decisions = iter((True, False))

    async def evaluate(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(allowed=next(decisions), actor_level="everyone")

    guard = SimpleNamespace(
        evaluate_fresh_member=evaluate,
        currently_allowed=lambda *_args, **_kwargs: True,
    )
    bot = SimpleNamespace(capability_guard=guard, is_closing=False)
    check = NasaApodPlugin._capability_check(bot)
    target = interaction()

    assert await check(NASA_APOD_CAPABILITY_ID, target) is True
    assert await check(NASA_APOD_CAPABILITY_ID, target) is False


@pytest.mark.asyncio
async def test_plugin_api_source_is_fail_closed_without_every_opt_in() -> None:
    capability_off = SimpleNamespace(
        tree=Tree(),
        settings=SimpleNamespace(
            nasa_apod_allow_remote=True,
            nasa_apod_api_key="valid-key",
        ),
        capability_registry=SimpleNamespace(
            capability_status=lambda *_args: SimpleNamespace(executable=False),
        ),
    )
    with pytest.raises(ApodConfigurationError):
        await NasaApodPlugin().start(capability_off)

    bot = SimpleNamespace(
        tree=Tree(),
        settings=SimpleNamespace(nasa_apod_allow_remote=False, nasa_apod_api_key=""),
        capability_registry=SimpleNamespace(
            capability_status=lambda *_args: SimpleNamespace(executable=True),
        ),
    )
    with pytest.raises(ApodConfigurationError):
        await NasaApodPlugin().start(bot)
    assert bot.tree.commands == {}


@pytest.mark.asyncio
async def test_plugin_api_source_starts_with_all_explicit_gates_without_request() -> None:
    tree = Tree()
    session = SimpleNamespace(calls=[])

    async def evaluate(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(allowed=True, actor_level="everyone")

    bot = SimpleNamespace(
        tree=tree,
        settings=SimpleNamespace(
            nasa_apod_allow_remote=True,
            nasa_apod_api_key="valid-key",
        ),
        capability_registry=SimpleNamespace(
            capability_status=lambda *_args: SimpleNamespace(executable=True),
        ),
        capability_guard=SimpleNamespace(
            evaluate_fresh_member=evaluate,
            currently_allowed=lambda *_args, **_kwargs: True,
        ),
        nasa_apod_http_session=session,
        is_closing=False,
    )
    plugin = NasaApodPlugin()

    await plugin.start(bot)
    try:
        assert isinstance(plugin.source, NasaApiApodSource)
        assert set(tree.commands) == {"nasa"}
        assert session.calls == []
    finally:
        await plugin.stop()


@pytest.mark.asyncio
async def test_plugin_stop_closes_source_even_if_command_uninstall_fails() -> None:
    class FailingTree(Tree):
        def remove_command(self, name: str) -> None:
            raise RuntimeError(f"cannot remove {name}")

    class ClosingStaticSource(StaticApodSource):
        def __init__(self, items: tuple[ApodItem, ...]) -> None:
            super().__init__(items)
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    async def evaluate(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(allowed=True, actor_level="everyone")

    source = ClosingStaticSource((apod_item(),))
    bot = SimpleNamespace(
        tree=FailingTree(),
        settings=SimpleNamespace(nasa_apod_allow_remote=False, nasa_apod_api_key=""),
        capability_registry=SimpleNamespace(
            capability_status=lambda *_args: SimpleNamespace(executable=True),
        ),
        capability_guard=SimpleNamespace(
            evaluate_fresh_member=evaluate,
            currently_allowed=lambda *_args, **_kwargs: True,
        ),
        is_closing=False,
    )
    plugin = NasaApodPlugin(source_factory=lambda _bot: source)
    await plugin.start(bot)

    with pytest.raises(RuntimeError, match="cannot remove nasa"):
        await plugin.stop()

    assert source.closed is True
    assert not hasattr(bot, "nasa_apod_service")
    assert not hasattr(bot, "nasa_apod_adapter")


@pytest.mark.asyncio
async def test_plugin_capability_check_denies_shutdown_started_during_fresh_evaluation() -> None:
    bot = SimpleNamespace(is_closing=False)

    async def evaluate(*_args: object, **_kwargs: object) -> SimpleNamespace:
        bot.is_closing = True
        return SimpleNamespace(allowed=True, actor_level="everyone")

    bot.capability_guard = SimpleNamespace(
        evaluate_fresh_member=evaluate,
        currently_allowed=lambda *_args, **_kwargs: True,
    )

    assert (
        await NasaApodPlugin._capability_check(bot)(
            NASA_APOD_CAPABILITY_ID,
            interaction(),
        )
        is False
    )


def test_manifest_config_and_setup_keep_nasa_explicit_only() -> None:
    settings = Settings.from_env(
        {
            "DISCORD_TOKEN": "unused-test-token",
            "NASA_APOD_ALLOW_REMOTE": "true",
            "NASA_APOD_API_KEY": "private-test-value",
        }
    )
    assert settings.nasa_apod_allow_remote is True
    assert settings.nasa_apod_api_key == "private-test-value"
    assert "private-test-value" not in repr(settings)
    assert "nasa_apod" not in SAFE_DEFAULT_PLUGINS
    assert "nasa_apod" not in settings.startup_plugins
    explicit = Settings.from_env(
        {
            "DISCORD_TOKEN": "unused-test-token",
            "ENABLED_PLUGINS": "nasa_apod",
        }
    )
    assert explicit.startup_plugins == frozenset({"nasa_apod"})
    assert dict(MODEL_TOOL_CAPABILITY_BINDINGS) == {
        "web_search": OPENAI_PAID_WEB_SEARCH_CAPABILITY_ID,
    }

    registered: list[tuple[str, object]] = []
    setup(SimpleNamespace(register=lambda name, factory: registered.append((name, factory))))
    assert registered == [("nasa_apod", NasaApodPlugin)]


def test_renderers_bound_adversarial_markdown_without_cutting_links() -> None:
    base = "https://example.invalid/"
    item = ApodItem(
        day=date(2026, 7, 23),
        title="*" * 300,
        explanation="[" * 8_000,
        media_type="video",
        url=base + ("a" * (2_048 - len(base))),
        copyright="_" * 300,
    )
    text = render_apod_text(item)
    embed = render_apod_embed(item)

    assert len(text) <= 1_900
    assert text.count("](") == 1
    assert "ap260723.html)" in text
    assert len(embed.title or "") <= 256
    assert len(embed.description or "") <= 4_096
    assert (embed.description or "").count("](") == 2
