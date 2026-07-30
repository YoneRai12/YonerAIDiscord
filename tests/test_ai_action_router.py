from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from yonerai_discord.capabilities import ACTION_CAPABILITIES, COMMAND_CAPABILITIES, EVENT_CAPABILITIES
from yonerai_discord.control_plane import CapabilitySpec, InMemoryStateStore, ModuleSpec, RbacLevel, Registry
from yonerai_discord.db import Database
from yonerai_discord.modules.ai import AIPlugin
from yonerai_discord.modules.ai.action_router import (
    ActionEffect,
    ActionMode,
    ActionOutputMode,
    ActionRegistry,
    ActionResult,
    ActionSpec,
    ActionStatus,
    DISCORD_ACTIVE_REPLY_TRIGGER,
    DISCORD_TRIGGER_METADATA_KEY,
    NaturalActionRouter,
)
from yonerai_discord.modules.ai.mention import AIMentionListener, ListenerClosingError
from yonerai_discord.modules.ai.models import AIRequest
from yonerai_discord.modules.ai.remote_consent import RemoteConsentStore
from yonerai_discord.modules.ai.orchestration import (
    OrchestrationEngine,
    OrchestrationPlan,
    OrchestrationStep,
    PlanStatus,
    StepStatus,
)
from yonerai_discord.modules.audio_core import LoopMode, QueueSnapshot, Track
from yonerai_discord.modules.community.plugin import CommunityPlugin
from yonerai_discord.modules.discovery.service import DiscoveryService
from yonerai_discord.modules.music.models import MusicAuthorizationError, MusicSessionError
from yonerai_discord.modules.media_pipeline.plugin import MediaPipelinePlugin
from yonerai_discord.modules.personal_memory.domain import MemoryKind
from yonerai_discord.modules.personal_memory.service import SensitiveMemoryError


BOT_ID = 999
GUILD_ID = 100
CHANNEL_ID = 200
USER_ID = 300


class Guard:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[dict[str, Any]] = []

    def event_allowed(self, capability_id: str, **kwargs: Any) -> bool:
        self.calls.append({"capability_id": capability_id, **kwargs})
        return self.allowed

    def currently_allowed(self, _capability_id: str, **_kwargs: Any) -> bool:
        return self.allowed

    async def evaluate_fresh_member(self, _capability_id: str, *, guild: Any, member: Any) -> Any:
        assert guild.id == GUILD_ID
        return SimpleNamespace(allowed=self.allowed, actor_level=RbacLevel.GUILD_ADMIN)

    async def actor(self, interaction: Any) -> Any:
        permissions = interaction.user.guild_permissions
        level = RbacLevel.GUILD_ADMIN if permissions.administrator or permissions.manage_guild else RbacLevel.EVERYONE
        return SimpleNamespace(level=level)


class CapabilityGuard(Guard):
    def __init__(
        self,
        *,
        states: dict[str, bool] | None = None,
        admin_only: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__()
        self.states = states if states is not None else {}
        self.admin_only = admin_only
        self.current_calls: list[dict[str, Any]] = []

    def _capability_allowed(self, capability_id: str, actor_level: RbacLevel) -> bool:
        return self.states.get(capability_id, True) and (
            capability_id not in self.admin_only or actor_level >= RbacLevel.GUILD_ADMIN
        )

    def event_allowed(self, capability_id: str, **kwargs: Any) -> bool:
        self.calls.append({"capability_id": capability_id, **kwargs})
        return self._capability_allowed(capability_id, kwargs["actor_level"])

    def currently_allowed(self, capability_id: str, **kwargs: Any) -> bool:
        self.current_calls.append({"capability_id": capability_id, **kwargs})
        return self._capability_allowed(capability_id, kwargs["actor_level"])

    async def evaluate_fresh_member(self, capability_id: str, *, guild: Any, member: Any) -> Any:
        assert guild.id == GUILD_ID
        level = RbacLevel.GUILD_ADMIN
        return SimpleNamespace(allowed=self._capability_allowed(capability_id, level), actor_level=level)


def _bind_media_pipeline_plugin(bot: Any) -> MediaPipelinePlugin:
    plugin = MediaPipelinePlugin()
    plugin._bot = bot
    bot.media_pipeline_plugin = plugin
    return plugin


class Guild:
    def __init__(self, member: Any, *, owner_id: int) -> None:
        self.id = GUILD_ID
        self.owner_id = owner_id
        self.voice_client = None
        self.member = member

    async def fetch_member(self, user_id: int) -> Any:
        if user_id != self.member.id:
            raise LookupError("member not found")
        return self.member


class VoiceChannel:
    id = 777

    def __init__(self) -> None:
        self.connect_calls = 0
        self.connect_kwargs: list[dict[str, Any]] = []
        self.disconnect_calls = 0
        self.voice_client = SimpleNamespace(disconnect=self.disconnect)

    async def connect(self, **kwargs: Any) -> Any:
        self.connect_calls += 1
        self.connect_kwargs.append(dict(kwargs))
        return self.voice_client

    async def disconnect(self, **_: Any) -> None:
        self.disconnect_calls += 1


def _settings(*, owner: bool = False) -> Any:
    return SimpleNamespace(
        bot_owner_ids=frozenset({USER_ID}) if owner else frozenset(),
        moderator_role_ids=frozenset(),
        trusted_role_ids=frozenset(),
    )


def _message(
    text: str,
    *,
    guard: Guard | None = None,
    owner: bool = False,
    voice_channel: VoiceChannel | None = None,
) -> tuple[Any, Any, Guard]:
    actual_guard = guard or Guard()
    permissions = SimpleNamespace(
        administrator=owner,
        manage_guild=owner,
        moderate_members=False,
        manage_messages=False,
        kick_members=False,
        ban_members=False,
    )
    author = SimpleNamespace(
        id=USER_ID,
        bot=False,
        roles=(),
        guild_permissions=permissions,
        voice=SimpleNamespace(channel=voice_channel),
    )
    guild = Guild(author, owner_id=USER_ID if owner else 9999)
    message = SimpleNamespace(
        id=400,
        content=f"<@{BOT_ID}> {text}",
        author=author,
        guild=guild,
        channel=SimpleNamespace(id=CHANNEL_ID),
    )
    bot = SimpleNamespace(
        user=SimpleNamespace(id=BOT_ID),
        settings=_settings(owner=owner),
        capability_guard=actual_guard,
        is_closing=False,
    )
    return bot, message, actual_guard


def _request() -> AIRequest:
    return AIRequest(prompt="test", guild_id=GUILD_ID, user_id=USER_ID)


def _enable_planner_action_runtime(bot: Any, message: Any, router: NaturalActionRouter) -> None:
    capability_ids = frozenset(
        (
            EVENT_CAPABILITIES["ai_mention_message"],
            *(capability_id for spec in router.registry.specs for _, capability_id, _ in spec.capability_requirements),
        )
    )
    registry = SimpleNamespace(
        capability_status=lambda capability_id, _guild_id=None: SimpleNamespace(
            executable=capability_id in capability_ids
        ),
        runtime_available=lambda capability_id: capability_id in capability_ids,
    )
    bot.capability_registry = registry
    bot.runtime_capability_readiness = dict.fromkeys(capability_ids, True)
    bot.capability_guard.registry = registry
    message.channel.permissions_for = lambda _member: SimpleNamespace(
        view_channel=True,
        read_message_history=True,
    )


@pytest.mark.asyncio
async def test_ai_plugin_installs_router_as_replaceable_pre_ai_hook() -> None:
    listeners: list[tuple[Any, str]] = []
    removed_commands: list[str] = []
    tree = SimpleNamespace(add_command=lambda _: None, remove_command=removed_commands.append)
    settings = SimpleNamespace(
        ai_conversation_ttl_seconds=7_200,
        ai_conversation_max_turns=12,
        ai_conversation_max_sessions=128,
        ai_conversation_max_total_binary_bytes=64 * 1024 * 1024,
        ai_attachment_max_file_bytes=8 * 1024 * 1024,
        ai_attachment_max_total_bytes=16 * 1024 * 1024,
        ai_attachment_max_files=4,
        ai_admission_global_concurrency=4,
        ai_admission_max_waiters=32,
        ai_admission_wait_timeout_seconds=2.0,
        ai_admission_drain_timeout_seconds=5.0,
        ai_base_url="",
        ai_mention_enabled=True,
    )
    bot = SimpleNamespace(
        settings=settings,
        tree=tree,
        add_listener=lambda listener, name: listeners.append((listener, name)),
        remove_listener=lambda listener, name: listeners.remove((listener, name)),
    )
    plugin = AIPlugin()

    await plugin.start(bot)

    assert isinstance(bot.ai_action_router, NaturalActionRouter)
    assert plugin._mention_listener is not None
    assert plugin._mention_listener.pre_ai_hook is bot.ai_action_router
    assert len(listeners) == 1
    assert bot.runtime_capability_readiness["cap-run-ai-mention-chat"] is True
    assert bot.runtime_capability_readiness["cap-can-0161"] is False
    router = bot.ai_action_router
    listener = plugin._mention_listener

    await plugin.stop()

    assert not hasattr(bot, "ai_action_router")
    assert listeners == []
    assert removed_commands == ["ai", "web"]
    assert router.closing is True
    assert listener.closing is True


@pytest.mark.asyncio
async def test_ai_plugin_closes_entrypoints_before_provider_and_cleans_up_on_close_error() -> None:
    listeners: list[tuple[Any, str]] = []
    removed_commands: list[str] = []
    tree = SimpleNamespace(add_command=lambda _: None, remove_command=removed_commands.append)
    settings = SimpleNamespace(
        ai_conversation_ttl_seconds=7_200,
        ai_conversation_max_turns=12,
        ai_conversation_max_sessions=128,
        ai_conversation_max_total_binary_bytes=64 * 1024 * 1024,
        ai_attachment_max_file_bytes=8 * 1024 * 1024,
        ai_attachment_max_total_bytes=16 * 1024 * 1024,
        ai_attachment_max_files=4,
        ai_admission_global_concurrency=4,
        ai_admission_max_waiters=32,
        ai_admission_wait_timeout_seconds=2.0,
        ai_admission_drain_timeout_seconds=5.0,
        ai_base_url="",
        ai_mention_enabled=True,
    )
    bot = SimpleNamespace(
        settings=settings,
        tree=tree,
        add_listener=lambda listener, name: listeners.append((listener, name)),
        remove_listener=lambda listener, name: listeners.remove((listener, name)),
    )
    plugin = AIPlugin()
    await plugin.start(bot)
    router = bot.ai_action_router
    listener = plugin._mention_listener
    assert listener is not None

    class FailingOwner:
        async def close(self) -> None:
            assert router.closing is True
            assert listener.closing is True
            assert listeners == []
            assert removed_commands == ["ai", "web"]
            assert bot.runtime_capability_readiness == {}
            raise RuntimeError("close failed")

    plugin._session_owner = FailingOwner()  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="close failed"):
        await plugin.stop()

    assert not hasattr(bot, "ai_service")
    assert not hasattr(bot, "ai_conversation_store")
    assert not hasattr(bot, "ai_action_router")
    assert plugin.service is None
    assert plugin.conversation_store is None
    assert plugin._bot is None
    assert plugin._session_owner is None


def test_default_registry_uses_only_code_owned_command_or_action_bindings_and_explicit_floors() -> None:
    router = NaturalActionRouter(SimpleNamespace())
    bindings = {**COMMAND_CAPABILITIES, **ACTION_CAPABILITIES}

    assert len(router.registry.specs) == 86
    assert all(spec.capability_id == bindings[spec.command_path] for spec in router.registry.specs)
    assert all(isinstance(spec.rbac_floor, RbacLevel) for spec in router.registry.specs)
    music_request = next(spec for spec in router.registry.specs if spec.action_id == "music.request")
    assert music_request.command_path == "music search-youtube"
    assert music_request.capability_id == COMMAND_CAPABILITIES["music search-youtube"]
    assert music_request.planner_contract is None
    music_enqueue = next(spec for spec in router.registry.specs if spec.action_id == "music.enqueue")
    assert music_enqueue.command_path == "music play"
    assert music_enqueue.additional_command_paths == ("music search",)
    assert music_enqueue.effect is ActionEffect.SIDE_EFFECT
    assert music_enqueue.planner_contract is not None
    extractor = music_enqueue.planner_contract.repetition_parameter_extractor
    assert extractor is not None
    assert extractor("混沌ブギを流して") == {"query": "混沌ブギ"}
    assert extractor("千本桜 remixを3回流して") == {"query": "千本桜 remix"}
    assert extractor("Songを2回続けて流して") == {"query": "Song"}
    assert extractor("千本桜を3回キュー追加") is None
    assert extractor("2回流して") is None
    assert music_enqueue.capability_requirements == (
        ("music play", COMMAND_CAPABILITIES["music play"], RbacLevel.EVERYONE),
        ("music search", COMMAND_CAPABILITIES["music search"], RbacLevel.EVERYONE),
    )
    music_radio = next(spec for spec in router.registry.specs if spec.action_id == "music.radio")
    assert music_radio.command_path == "music radio"
    assert music_radio.capability_id == COMMAND_CAPABILITIES["music radio"]
    assert music_radio.effect is ActionEffect.SIDE_EFFECT
    assert music_radio.planner_contract is None
    browser_screenshot = next(spec for spec in router.registry.specs if spec.action_id == "browser.screenshot")
    assert browser_screenshot.command_path == "browser screenshot"
    assert browser_screenshot.rbac_floor is RbacLevel.BOT_OWNER
    assert browser_screenshot.effect is ActionEffect.SIDE_EFFECT
    assert browser_screenshot.planner_contract is not None
    assert browser_screenshot.timeout_seconds == 65.0
    browser_interactive = next(
        spec for spec in router.registry.specs if spec.action_id == "browser.youtube-playback-evidence"
    )
    assert browser_interactive.command_path == "browser interact"
    assert browser_interactive.capability_id == ACTION_CAPABILITIES["browser interact"]
    assert browser_interactive.rbac_floor is RbacLevel.BOT_OWNER
    assert browser_interactive.effect is ActionEffect.SIDE_EFFECT
    assert browser_interactive.planner_contract is None
    assert browser_interactive.timeout_seconds == 65.0
    asset_specs = {
        spec.action_id: spec for spec in router.registry.specs if spec.action_id.startswith("discord.asset-inspect")
    }
    assert set(asset_specs) == {"discord.asset-inspect", "discord.asset-inspect-invalid"}
    assert all(spec.command_path == "media discord-asset-inspect" for spec in asset_specs.values())
    assert all(spec.rbac_floor is RbacLevel.TRUSTED for spec in asset_specs.values())
    assert all(spec.planner_contract is None for spec in asset_specs.values())
    music_preview = next(spec for spec in router.registry.specs if spec.action_id == "music.youtube-preview")
    assert music_preview.rbac_floor is RbacLevel.BOT_OWNER
    assert music_preview.additional_command_paths == ("music search-youtube",)
    assert music_preview.effect is ActionEffect.SIDE_EFFECT
    assert music_preview.planner_contract is not None
    assert music_preview.timeout_seconds == 65.0
    site_permission_actions = {
        spec.action_id: spec
        for spec in router.registry.specs
        if spec.action_id in {"site.permission-grant", "site.permission-revoke"}
    }
    assert set(site_permission_actions) == {"site.permission-grant", "site.permission-revoke"}
    assert all(spec.rbac_floor is RbacLevel.BOT_OWNER for spec in site_permission_actions.values())
    assert all(spec.planner_contract is None for spec in site_permission_actions.values())
    assert music_enqueue.timeout_seconds is None
    assert router.registry.parse("混沌ブギを流して、そのあと千本桜を流して") is None
    assert router.registry.parse("混沌ブギのYouTube画面をスクショして、そのあと千本桜も撮って") is None
    planner_specs = {spec.action_id: spec for spec in router.registry.specs if spec.planner_contract is not None}
    expected_read_only = {
        "earthquake.latest",
        "nasa.apod",
        "music.status",
        "music.search-local",
        "site.status",
        "discovery.list",
        "discovery.search",
        "poll.results",
        "schedule.list",
        "schedule.show",
    }
    assert expected_read_only <= set(planner_specs)
    assert all(planner_specs[action_id].effect is ActionEffect.READ_ONLY for action_id in expected_read_only)
    nasa_schema = planner_specs["nasa.apod"].planner_contract.input_schema
    assert nasa_schema["required"] == ("date",)
    assert nasa_schema["properties"]["date"] == {
        "type": "string",
        "pattern": r"^(?:|[0-9]{4}-[0-9]{2}-[0-9]{2})$",
        "maxLength": 10,
    }
    assert planner_specs["poll.results"].planner_contract.input_schema == {
        "type": "object",
        "additionalProperties": False,
        "properties": {"poll_id": {"type": "string", "pattern": r"^[0-9a-f]{32}$"}},
        "required": ("poll_id",),
    }
    assert planner_specs["poll.results"].planner_contract.grounded_parameters == ("poll_id",)
    assert planner_specs["schedule.show"].planner_contract.input_schema == {
        "type": "object",
        "additionalProperties": False,
        "properties": {"meeting_id": {"type": "string", "pattern": r"^MEET-[A-F0-9]{8}$"}},
        "required": ("meeting_id",),
    }
    assert planner_specs["schedule.show"].planner_contract.grounded_parameters == ("meeting_id",)
    assert {spec.action_id for spec in router.registry.specs if spec.mode is ActionMode.DEFER_TO_SLASH} == {
        "defer.mod-ban",
        "defer.mod-kick",
        "defer.ticket-close",
        "defer.role-add",
        "defer.config",
        "defer.evolution",
    }
    image = next(spec for spec in router.registry.specs if spec.action_id == "image.generate")
    assert image.mode is ActionMode.EXECUTE
    assert image.command_path == "image generate"
    video = next(spec for spec in router.registry.specs if spec.action_id == "video.generate")
    assert video.mode is ActionMode.EXECUTE
    assert video.command_path == "video generate"
    music = next(spec for spec in router.registry.specs if spec.action_id == "music.generate")
    assert music.mode is ActionMode.EXECUTE
    assert music.command_path == "musicgen generate"
    rights_required = next(spec for spec in router.registry.specs if spec.action_id == "music.generate-rights-required")
    assert rights_required.mode is ActionMode.EXECUTE
    snowflake = next(spec for spec in router.registry.specs if spec.action_id == "tools.snowflake")
    assert snowflake.command_path == "tools snowflake"
    sha256 = next(spec for spec in router.registry.specs if spec.action_id == "tools.sha256")
    assert sha256.command_path == "tools sha256"
    color = next(spec for spec in router.registry.specs if spec.action_id == "tools.color")
    assert color.command_path == "tools color"
    poll_results = next(spec for spec in router.registry.specs if spec.action_id == "poll.results")
    assert poll_results.command_path == "poll results"
    discovery = {spec.action_id: spec for spec in router.registry.specs if spec.action_id.startswith("discovery.")}
    assert set(discovery) == {"discovery.list", "discovery.search", "discovery.search-invalid"}
    assert all(spec.command_path == "help" for spec in discovery.values())


def test_action_spec_rejects_unmapped_or_mismatched_capability() -> None:
    async def execute(*_: Any) -> ActionResult:
        return ActionResult(ActionStatus.COMPLETED, "ok")

    with pytest.raises(ValueError, match="code-owned capability mapping"):
        ActionSpec("bad", "not-a-command", "cap-missing", RbacLevel.EVERYONE, lambda _: {}, execute)


@pytest.mark.asyncio
async def test_discord_asset_metadata_routes_locally_with_exact_capability() -> None:
    asset_id = 123456789012345678
    bot, message, _ = _message(f"この絵文字を調べて: <a:party:{asset_id}>", owner=True)
    _bind_media_pipeline_plugin(bot)
    router = NaturalActionRouter(bot)
    _enable_planner_action_runtime(bot, message, router)

    reply = await router(
        message,
        AIRequest(prompt="test", guild_id=GUILD_ID, channel_id=CHANNEL_ID, user_id=USER_ID),
    )

    assert reply is not None
    assert "カスタム絵文字: party" in reply.text
    assert "形式: アニメーション" in reply.text
    assert f"`{asset_id}`" in reply.text
    assert not hasattr(bot, "ai_service")


@pytest.mark.asyncio
async def test_discord_sticker_metadata_requires_exactly_one_current_message_sticker() -> None:
    asset_id = 123456789012345679
    bot, message, _ = _message("このスタンプを調べて", owner=True)
    _bind_media_pipeline_plugin(bot)
    message.stickers = (SimpleNamespace(id=asset_id, name="@everyone sticker", format=SimpleNamespace(name="apng")),)
    router = NaturalActionRouter(bot)
    _enable_planner_action_runtime(bot, message, router)

    request = AIRequest(prompt="test", guild_id=GUILD_ID, channel_id=CHANNEL_ID, user_id=USER_ID)
    reply = await router(message, request)

    assert reply is not None
    assert "スタンプ: @\u200beveryone sticker" in reply.text
    assert "形式: APNG" in reply.text
    assert f"`{asset_id}`" in reply.text

    message.stickers = ()
    denied = await router(message, request)
    assert denied is not None
    assert "1件だけ" in denied.text


@pytest.mark.asyncio
async def test_invalid_explicit_discord_asset_request_is_consumed_without_provider() -> None:
    bot, message, _ = _message("この絵文字を調べて: <:bad:123>", owner=True)
    router = NaturalActionRouter(bot)
    _enable_planner_action_runtime(bot, message, router)

    reply = await router(message, _request())

    assert reply is not None
    assert "この絵文字を調べて" in reply.text
    assert not hasattr(bot, "ai_service")


@pytest.mark.asyncio
async def test_discord_asset_metadata_fails_closed_when_plugin_is_replaced_during_fresh_check() -> None:
    asset_id = 123456789012345680
    bot, message, _ = _message(f"この絵文字を調べて: <:party:{asset_id}>", owner=True)
    _bind_media_pipeline_plugin(bot)
    router = NaturalActionRouter(bot)
    _enable_planner_action_runtime(bot, message, router)
    original_fetch_member = message.guild.fetch_member

    async def fetch_member(user_id: int) -> Any:
        member = await original_fetch_member(user_id)
        bot.media_pipeline_plugin = MediaPipelinePlugin()
        return member

    message.guild.fetch_member = fetch_member

    reply = await router(message, _request())

    assert reply is not None
    assert "party" not in reply.text
    assert not hasattr(bot, "ai_service")


@pytest.mark.asyncio
async def test_discord_asset_metadata_rejects_spoofed_plugin_subclass() -> None:
    class SpoofedMediaPipelinePlugin(MediaPipelinePlugin):
        def is_current_for(self, bot: Any) -> bool:
            return True

    asset_id = 123456789012345681
    bot, message, _ = _message(f"この絵文字を調べて: <:party:{asset_id}>", owner=True)
    bot.media_pipeline_plugin = SpoofedMediaPipelinePlugin()
    router = NaturalActionRouter(bot)
    _enable_planner_action_runtime(bot, message, router)

    reply = await router(message, _request())

    assert reply is not None
    assert "party" not in reply.text
    assert not hasattr(bot, "ai_service")


@pytest.mark.parametrize(
    "additional_paths",
    (
        ("not-a-command",),
        ("music play",),
        ("music search", "music search"),
    ),
)
def test_action_spec_rejects_invalid_additional_command_requirements(
    additional_paths: tuple[str, ...],
) -> None:
    async def execute(*_: Any) -> ActionResult:
        return ActionResult(ActionStatus.COMPLETED, "ok")

    with pytest.raises(ValueError):
        ActionSpec(
            "bad-extra",
            "music play",
            COMMAND_CAPABILITIES["music play"],
            RbacLevel.EVERYONE,
            lambda _: None,
            execute,
            additional_command_paths=additional_paths,
        )


@pytest.mark.asyncio
async def test_ordinary_text_and_other_mentions_fall_through() -> None:
    bot, message, guard = _message("おはよう")
    router = NaturalActionRouter(bot)

    assert await router(message, _request()) is None
    message.content = "地震情報を教えて"
    assert await router(message, _request()) is None
    message.content = f"<@{BOT_ID}> <@12345> の天気を教えて"
    assert await router(message, _request()) is None
    message.content = f"<@{BOT_ID}> 音楽機能を説明して"
    assert await router(message, _request()) is None
    assert guard.calls == []


@pytest.mark.asyncio
async def test_verified_active_reply_runs_real_typed_action_without_new_mention() -> None:
    bot, message, guard = _message("最新の地震情報を教えて")
    message.content = "最新の地震情報を教えて"
    message.reference = SimpleNamespace(message_id=987_654)
    event = SimpleNamespace(scale_label="3", hypocenter_name="千葉県東方沖", magnitude=4.2, depth_km=30)

    class Service:
        async def fetch_latest(self) -> Any:
            return event

    bot.earthquake_service = Service()
    request = AIRequest(
        prompt=message.content,
        guild_id=GUILD_ID,
        user_id=USER_ID,
        metadata={DISCORD_TRIGGER_METADATA_KEY: DISCORD_ACTIVE_REPLY_TRIGGER},
    )

    reply = await NaturalActionRouter(bot)(message, request)

    assert reply is not None
    assert reply.provider == "local-action-router"
    assert "千葉県東方沖" in reply.text
    assert guard.calls[0]["capability_id"] == COMMAND_CAPABILITIES["earthquake latest"]


@pytest.mark.asyncio
async def test_active_reply_marker_without_discord_reference_falls_through() -> None:
    bot, message, guard = _message("最新の地震情報を教えて")
    message.content = "最新の地震情報を教えて"
    request = AIRequest(
        prompt=message.content,
        guild_id=GUILD_ID,
        user_id=USER_ID,
        metadata={DISCORD_TRIGGER_METADATA_KEY: DISCORD_ACTIVE_REPLY_TRIGGER},
    )

    assert await NaturalActionRouter(bot)(message, request) is None
    assert guard.calls == []


@pytest.mark.asyncio
async def test_earthquake_action_passes_common_guard_and_never_calls_provider() -> None:
    bot, message, guard = _message("最新の地震情報を教えて")
    event = SimpleNamespace(scale_label="4", hypocenter_name="東京湾", magnitude=5.1, depth_km=40)

    class Service:
        async def fetch_latest(self) -> Any:
            return event

    bot.earthquake_service = Service()
    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert reply.provider == "local-action-router"
    assert reply.model == "deterministic-v1"
    assert "東京湾" in reply.text
    assert guard.calls == [
        {
            "capability_id": COMMAND_CAPABILITIES["earthquake latest"],
            "surface": "earthquake latest",
            "guild_id": GUILD_ID,
            "channel_id": CHANNEL_ID,
            "event_id": 400,
            "user_id": USER_ID,
            "author_is_bot": False,
            "actor_level": RbacLevel.EVERYONE,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    (
        "command_denied",
        "mention_denied",
        "guard_missing",
        "current_missing",
        "current_raises",
        "identity_missing",
    ),
)
async def test_earthquake_action_rechecks_authorization_after_fetch_before_display(
    mutation: str,
) -> None:
    capability = COMMAND_CAPABILITIES["earthquake latest"]
    mention_capability = EVENT_CAPABILITIES["ai_mention_message"]
    guard = CapabilityGuard(states={capability: True, mention_capability: True})
    bot, message, _ = _message("最新の地震情報を教えて", guard=guard)

    class Service:
        async def fetch_latest(self) -> Any:
            if mutation == "command_denied":
                guard.states[capability] = False
            elif mutation == "mention_denied":
                guard.states[mention_capability] = False
            elif mutation == "guard_missing":
                del bot.capability_guard
            elif mutation == "current_missing":
                bot.capability_guard = SimpleNamespace()
            elif mutation == "current_raises":

                def raise_current(*_args: Any, **_kwargs: Any) -> bool:
                    raise RuntimeError("must not escape")

                bot.capability_guard = SimpleNamespace(currently_allowed=raise_current)
            else:
                message.guild = None
            return SimpleNamespace(
                scale_label="7",
                hypocenter_name="must not display",
                magnitude=9.9,
                depth_km=1,
            )

    bot.earthquake_service = Service()
    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert reply.provider == "local-action-router"
    assert "表示しません" in reply.text
    assert "must not display" not in reply.text


@pytest.mark.asyncio
async def test_natural_today_weather_question_uses_local_official_information() -> None:
    bot, message, guard = _message("今日の東京の天気は")
    calls: list[str] = []

    class Service:
        async def get_weather(self, region: str) -> Any:
            calls.append(region)
            return SimpleNamespace(
                region=SimpleNamespace(name="東京都"),
                headline="晴れる見込みです。",
                periods=(),
                source_url="https://www.jma.go.jp/",
            )

    bot.jp_information_service = Service()

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert reply.provider == "local-action-router"
    assert calls == ["東京"]
    assert "気象庁" in reply.text
    assert guard.calls[0]["capability_id"] == COMMAND_CAPABILITIES["weather"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_date"),
    [
        ("今日のNASAの写真", None),
        ("NASA APOD", None),
        ("2026-07-23のAPOD", "2026-07-23"),
    ],
)
async def test_nasa_apod_exact_actions_use_local_service_without_provider(
    text: str,
    expected_date: str | None,
) -> None:
    bot, message, guard = _message(text)
    calls: list[str | None] = []

    class Service:
        async def get(self, value: str | None = None) -> Any:
            calls.append(value)
            return SimpleNamespace(
                day=SimpleNamespace(isoformat=lambda: "2026-07-23"),
                title="Example APOD",
                explanation="NASA metadata.",
                media_type="image",
                url="https://example.invalid/image.jpg",
                copyright=None,
                source_page_url="https://apod.nasa.gov/apod/ap260723.html",
            )

    bot.nasa_apod_service = Service()
    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert reply.provider == "local-action-router"
    assert calls == [expected_date]
    assert "NASA APOD" in reply.text
    assert guard.calls[0]["capability_id"] == COMMAND_CAPABILITIES["nasa apod"]


@pytest.mark.asyncio
async def test_nasa_unregistered_phrasing_falls_through_without_service_call() -> None:
    bot, message, guard = _message("NASAの写真機能を説明して")
    service = SimpleNamespace(calls=0)

    async def get(_value: str | None = None) -> None:
        service.calls += 1

    service.get = get
    bot.nasa_apod_service = service

    assert await NaturalActionRouter(bot)(message, _request()) is None
    assert service.calls == 0
    assert guard.calls == []


@pytest.mark.asyncio
async def test_nasa_action_rechecks_capability_after_fetch_before_display() -> None:
    capability = COMMAND_CAPABILITIES["nasa apod"]
    guard = CapabilityGuard(states={capability: True})
    bot, message, _ = _message("今日のNASAの写真", guard=guard)

    class Service:
        async def get(self, _value: str | None = None) -> Any:
            guard.states[capability] = False
            return SimpleNamespace(
                day=SimpleNamespace(isoformat=lambda: "2026-07-23"),
                title="must not display",
                explanation="must not display",
                media_type="image",
                url="https://example.invalid/image.jpg",
                copyright=None,
                source_page_url="https://apod.nasa.gov/apod/ap260723.html",
            )

    bot.nasa_apod_service = Service()
    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert "表示しません" in reply.text
    assert "must not display" not in reply.text


@pytest.mark.asyncio
async def test_missing_or_denying_guard_fails_closed_before_service() -> None:
    bot, message, guard = _message("最新の地震情報を教えて", guard=Guard(False))
    service = SimpleNamespace(calls=0)

    async def fetch_latest() -> None:
        service.calls += 1

    service.fetch_latest = fetch_latest
    bot.earthquake_service = service

    denied = await NaturalActionRouter(bot)(message, _request())
    assert denied is not None and "利用できません" in denied.text
    assert service.calls == 0
    assert len(guard.calls) == 1

    del bot.capability_guard
    service.calls = 0
    denied_missing = await NaturalActionRouter(bot)(message, _request())
    assert denied_missing is not None and "利用できません" in denied_missing.text
    assert service.calls == 0


@pytest.mark.asyncio
async def test_router_begin_close_rejects_without_policy_or_service_dispatch() -> None:
    bot, message, guard = _message("最新の地震情報を教えて")
    router = NaturalActionRouter(bot)
    router.begin_close()

    reply = await router(message, _request())

    assert reply is not None and "停止処理中" in reply.text
    assert guard.calls == []


@pytest.mark.asyncio
async def test_router_bot_shutdown_flag_rejects_without_policy_or_service_dispatch() -> None:
    bot, message, guard = _message("最新の地震情報を教えて")
    bot.is_closing = True

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "停止処理中" in reply.text
    assert guard.calls == []


@pytest.mark.asyncio
async def test_router_rechecks_policy_after_semaphore_wait_before_executor() -> None:
    bot, first_message, guard = _message("test")
    _unused, second_message, _unused_guard = _message("test")
    started = asyncio.Event()
    release = asyncio.Event()
    executor_calls = 0

    async def executor(_context: object, _parameters: object) -> ActionResult:
        nonlocal executor_calls
        executor_calls += 1
        started.set()
        await release.wait()
        return ActionResult(ActionStatus.COMPLETED, "done")

    registry = ActionRegistry(
        (
            ActionSpec(
                "test.weather",
                "weather",
                COMMAND_CAPABILITIES["weather"],
                RbacLevel.EVERYONE,
                lambda text: {} if text == "test" else None,
                executor,
            ),
        )
    )
    router = NaturalActionRouter(bot, registry=registry, max_concurrency=1)
    first = asyncio.create_task(router(first_message, _request()))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    second = asyncio.create_task(router(second_message, _request()))
    await asyncio.sleep(0)
    guard.allowed = False
    release.set()
    first_reply, second_reply = await asyncio.gather(first, second)

    assert first_reply is not None and first_reply.text == "done"
    assert second_reply is not None and "変更" in second_reply.text
    assert executor_calls == 1


@pytest.mark.asyncio
async def test_listener_begin_close_rejects_existing_route_before_provider() -> None:
    bot, message, _ = _message("通常のAI質問")
    service = SimpleNamespace(calls=0)

    async def ask(_: AIRequest) -> None:
        service.calls += 1

    service.ask = ask
    listener = AIMentionListener(service, bot)  # type: ignore[arg-type]
    await listener.begin_close()

    with pytest.raises(ListenerClosingError):
        await listener._complete(message, _request())  # type: ignore[arg-type]
    assert service.calls == 0


@pytest.mark.asyncio
async def test_high_impact_command_is_deferred_and_never_executed() -> None:
    bot, message, guard = _message("このチケットを閉じて", owner=True)

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert "自動実行しません" in reply.text
    assert "/ticket close" in reply.text
    assert guard.calls[0]["capability_id"] == COMMAND_CAPABILITIES["ticket close"]


@pytest.mark.asyncio
async def test_image_generation_exact_prefix_directly_dispatches_without_ai_tools() -> None:
    bot, message, guard = _message("画像を生成して: 青い猫", owner=True)

    class Adapter:
        calls: list[dict[str, Any]] = []

        async def generate_for_message(self, received: Any, *, prompt: str, authorization_current: Any) -> bool:
            self.calls.append({"message": received, "prompt": prompt})
            return await authorization_current()

    adapter = Adapter()
    bot.image_generation_adapter = adapter

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert reply.provider == "local-action-router"
    assert "返信に添付" in reply.text
    assert "青い猫" not in reply.text
    assert adapter.calls == [{"message": message, "prompt": "青い猫"}]
    assert guard.calls[0]["capability_id"] == COMMAND_CAPABILITIES["image generate"]

    message.content = f"<@{BOT_ID}> 画像生成について説明して"
    guard.calls.clear()
    assert await NaturalActionRouter(bot)(message, _request()) is None
    assert guard.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    (
        "画像を生成して:",
        "画像を生成して: " + "青" * 1_101,
    ),
)
async def test_missing_or_oversized_explicit_image_request_never_falls_through_to_ai(text: str) -> None:
    bot, message, guard = _message(text, owner=True)

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert reply.provider == "local-action-router"
    assert "画像の内容を指定" in reply.text
    assert guard.calls[0]["capability_id"] == COMMAND_CAPABILITIES["image generate"]


@pytest.mark.asyncio
async def test_image_generation_fresh_member_revocation_prevents_adapter_execution() -> None:
    guard = CapabilityGuard(states={COMMAND_CAPABILITIES["image generate"]: False})
    bot, message, _ = _message("画像を作って: 青い猫", guard=guard, owner=True)
    calls = 0

    class Adapter:
        async def generate_for_message(self, *_args: Any, **_kwargs: Any) -> bool:
            nonlocal calls
            calls += 1
            return True

    bot.image_generation_adapter = Adapter()

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert "利用できません" in reply.text
    assert calls == 0


@pytest.mark.asyncio
async def test_image_editing_exact_prefix_dispatches_only_to_mention_adapter() -> None:
    bot, message, guard = _message("画像を編集して: 背景を青に", owner=True)

    class Adapter:
        calls: list[dict[str, Any]] = []

        async def edit_for_message(
            self, received: Any, *, instruction: str, authorization_current: Any, settings: Any
        ) -> bool:
            self.calls.append({"message": received, "instruction": instruction, "settings": settings})
            return await authorization_current()

    adapter = Adapter()
    bot.image_editing_adapter = adapter
    router = NaturalActionRouter(bot)
    _enable_planner_action_runtime(bot, message, router)

    reply = await router(message, _request())

    assert reply is not None
    assert "返信として送信" in reply.text
    assert adapter.calls == [{"message": message, "instruction": "背景を青に", "settings": bot.settings}]
    assert guard.calls[0]["capability_id"] == ACTION_CAPABILITIES["image edit"]

    message.content = f"<@{BOT_ID}> 画像を編集してみて: 背景を青に"
    assert await router(message, _request()) is None

    message.content = f"<@{BOT_ID}> 画像を編集して"
    reply = await router(message, _request())
    assert reply is not None
    assert "編集指示を添えて" in reply.text
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_image_editing_channel_permission_revocation_prevents_delivery() -> None:
    bot, message, _ = _message("画像を編集して: 背景を青に", owner=True)
    calls = 0

    class Adapter:
        async def edit_for_message(self, *_args: Any, **_kwargs: Any) -> bool:
            nonlocal calls
            calls += 1
            return await _kwargs["authorization_current"]()

    bot.image_editing_adapter = Adapter()
    router = NaturalActionRouter(bot)
    _enable_planner_action_runtime(bot, message, router)
    permission_checks = 0

    def permissions_for(_member: Any) -> Any:
        nonlocal permission_checks
        permission_checks += 1
        allowed = permission_checks == 1
        return SimpleNamespace(view_channel=allowed, read_message_history=allowed)

    message.channel.permissions_for = permissions_for
    reply = await router(message, _request())

    assert reply is not None
    assert "完了できませんでした" in reply.text
    assert permission_checks >= 2
    assert calls == 1


@pytest.mark.asyncio
async def test_browser_screenshot_is_owner_only_and_uses_fresh_authorization() -> None:
    class OwnerGuard(CapabilityGuard):
        async def evaluate_fresh_member(self, capability_id: str, *, guild: Any, member: Any) -> Any:
            assert guild.id == GUILD_ID
            return SimpleNamespace(
                allowed=self._capability_allowed(capability_id, RbacLevel.BOT_OWNER),
                actor_level=RbacLevel.BOT_OWNER,
            )

    guard = OwnerGuard()
    bot, message, _ = _message("https://example.com/path をスクショして", guard=guard, owner=True)

    class Adapter:
        calls: list[dict[str, Any]] = []

        async def capture_for_message(
            self,
            received: Any,
            *,
            url: str,
            authorization_current: Any,
            target_kind: str,
        ) -> bool:
            self.calls.append({"message": received, "url": url, "target_kind": target_kind})
            return await authorization_current()

    adapter = Adapter()
    bot.browser_rendering_adapter = adapter
    router = NaturalActionRouter(bot)
    _enable_planner_action_runtime(bot, message, router)

    request = AIRequest("test", GUILD_ID, USER_ID, CHANNEL_ID)
    reply = await router(message, request)

    assert reply is not None
    assert "返信として送信" in reply.text
    assert adapter.calls == [{"message": message, "url": "https://example.com/path", "target_kind": "url"}]
    assert guard.calls[0]["capability_id"] == ACTION_CAPABILITIES["browser screenshot"]

    not_owner, denied_message, _ = _message(
        "https://example.com/path をスクショして",
        guard=OwnerGuard(),
        owner=False,
    )
    not_owner.browser_rendering_adapter = adapter
    denied_router = NaturalActionRouter(not_owner)
    _enable_planner_action_runtime(not_owner, denied_message, denied_router)
    denied = await denied_router(denied_message, request)
    assert denied is not None and "利用できません" in denied.text
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_browser_youtube_playback_evidence_uses_fixed_recipe_and_fresh_identities() -> None:
    class OwnerGuard(CapabilityGuard):
        async def evaluate_fresh_member(self, capability_id: str, *, guild: Any, member: Any) -> Any:
            assert guild.id == GUILD_ID
            return SimpleNamespace(
                allowed=self._capability_allowed(capability_id, RbacLevel.BOT_OWNER),
                actor_level=RbacLevel.BOT_OWNER,
            )

    guard = OwnerGuard()
    bot, message, _ = _message(
        "ＹｏｕＴｕｂｅで　猫 動画　を検索して、先頭候補を開いて再生し、途中と再生後をスクショして",
        guard=guard,
        owner=True,
    )
    runner = object()
    store = object()

    class Adapter:
        closing = False

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def run_youtube_for_message(
            self,
            received: Any,
            *,
            query: str,
            authorization_current: Any,
            authorization_current_sync: Any,
        ) -> bool:
            assert await authorization_current()
            assert authorization_current_sync() is True
            self.calls.append({"message": received, "query": query})
            return True

    adapter = Adapter()
    plugin = SimpleNamespace(interactive_adapter=adapter, interactive_runner=runner)
    bot.browser_rendering_plugin = plugin
    bot.browser_run_adapter = adapter
    bot.remote_browser_run_service = runner
    bot.media_pipeline_store = store
    bot.remote_browser_interactive_status = SimpleNamespace(ready=True)
    router = NaturalActionRouter(bot)
    _enable_planner_action_runtime(bot, message, router)

    request = AIRequest("test", GUILD_ID, USER_ID, CHANNEL_ID)
    reply = await router(message, request)

    assert reply is not None and "同じ進捗メッセージ" in reply.text
    assert reply.delivery_handled is True
    assert adapter.calls == [{"message": message, "query": "猫 動画"}]
    assert guard.calls[0]["capability_id"] == ACTION_CAPABILITIES["browser interact"]
    assert EVENT_CAPABILITIES["ai_mention_message"] in {call["capability_id"] for call in guard.current_calls}


@pytest.mark.asyncio
async def test_browser_youtube_playback_evidence_consumes_invalid_and_revocation_without_delivery() -> None:
    class OwnerGuard(CapabilityGuard):
        async def evaluate_fresh_member(self, capability_id: str, *, guild: Any, member: Any) -> Any:
            assert guild.id == GUILD_ID
            return SimpleNamespace(
                allowed=self._capability_allowed(capability_id, RbacLevel.BOT_OWNER),
                actor_level=RbacLevel.BOT_OWNER,
            )

    guard = OwnerGuard()
    bot, message, _ = _message(
        f"YouTubeで {'猫' * 201} を検索して、先頭候補を開いて再生し、途中と再生後をスクショして",
        guard=guard,
        owner=True,
    )
    adapter_calls = 0

    class Adapter:
        closing = False

        async def run_youtube_for_message(self, *_args: Any, **_kwargs: Any) -> bool:
            nonlocal adapter_calls
            adapter_calls += 1
            return True

    adapter = Adapter()
    runner = object()
    store = object()
    plugin = SimpleNamespace(interactive_adapter=adapter, interactive_runner=runner)
    bot.browser_rendering_plugin = plugin
    bot.browser_run_adapter = adapter
    bot.remote_browser_run_service = runner
    bot.media_pipeline_store = store
    bot.remote_browser_interactive_status = SimpleNamespace(ready=True)
    router = NaturalActionRouter(bot)
    _enable_planner_action_runtime(bot, message, router)
    request = AIRequest("test", GUILD_ID, USER_ID, CHANNEL_ID)

    invalid = await router(message, request)

    assert invalid is not None and "完全な書式" in invalid.text
    assert adapter_calls == 0

    secret_like_query = "sk-" + "proj-do-not-send-this-value"
    message.content = (
        f"<@{BOT_ID}> YouTubeで {secret_like_query} を検索して、先頭候補を開いて再生し、途中と再生後をスクショして"
    )
    message.id += 1
    secret_like = await router(message, request)
    assert secret_like is not None and "完全な書式" in secret_like.text
    assert adapter_calls == 0

    class RevokingAdapter(Adapter):
        async def run_youtube_for_message(
            self,
            *_args: Any,
            authorization_current: Any,
            authorization_current_sync: Any,
            **_kwargs: Any,
        ) -> bool:
            nonlocal adapter_calls
            adapter_calls += 1
            assert await authorization_current()
            guard.states[ACTION_CAPABILITIES["browser interact"]] = False
            assert await authorization_current() is False
            assert authorization_current_sync() is False
            return False

    revoking = RevokingAdapter()
    plugin.interactive_adapter = revoking
    bot.browser_run_adapter = revoking
    message.content = f"<@{BOT_ID}> YouTubeで 猫 を検索して、先頭候補を開いて再生し、途中と再生後をスクショして"
    message.id += 1

    revoked = await router(message, request)

    assert revoked is not None and "取得できませんでした" in revoked.text
    assert adapter_calls == 1


@pytest.mark.asyncio
async def test_media_url_inspection_requires_owner_consent_and_fresh_authorization() -> None:
    model_evidence = "字幕と映像を根拠にした動画解析\n" + ("根拠データ" * 600)

    class OwnerGuard(CapabilityGuard):
        async def evaluate_fresh_member(self, capability_id: str, *, guild: Any, member: Any) -> Any:
            assert guild.id == GUILD_ID
            return SimpleNamespace(
                allowed=self._capability_allowed(capability_id, RbacLevel.BOT_OWNER),
                actor_level=RbacLevel.BOT_OWNER,
            )

    class Adapter:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def inspect_for_message(
            self,
            received: Any,
            *,
            url: str,
            instruction: str,
            authorization_current: Any,
        ) -> str | None:
            assert await authorization_current()
            self.calls.append({"message": received, "url": url, "instruction": instruction})
            return model_evidence

    prompt = "https://youtube.com/shorts/TG9KgEss-TE これ何？"
    bot, message, guard = _message(prompt, guard=OwnerGuard(), owner=True)
    adapter = Adapter()
    consent = RemoteConsentStore(ttl_seconds=None)
    bot.media_url_inspection_adapter = adapter
    bot.ai_remote_consent_store = consent
    router = NaturalActionRouter(bot)
    _enable_planner_action_runtime(bot, message, router)
    assert router.registry.get("media.url-inspect").effect is ActionEffect.SIDE_EFFECT
    assert router.registry.get("media.url-inspect").output_mode is ActionOutputMode.MODEL_SYNTHESIS
    request = AIRequest("test", GUILD_ID, USER_ID, CHANNEL_ID)

    denied = await router(message, request)

    assert denied is not None and "初回同意" in denied.text
    assert adapter.calls == []
    assert denied.synthesis_action_id is None

    consent.grant(guild_id=GUILD_ID, channel_id=CHANNEL_ID, user_id=USER_ID)
    message.id += 1
    reply = await router(message, request)

    assert reply is not None and reply.text == model_evidence
    assert len(reply.text) > 1_900
    assert reply.synthesis_action_id == "media.url-inspect"
    assert adapter.calls == [
        {
            "message": message,
            "url": "https://youtube.com/shorts/TG9KgEss-TE",
            "instruction": "これ何",
        }
    ]
    assert guard.calls[0]["capability_id"] == ACTION_CAPABILITIES["media url-inspect"]


@pytest.mark.asyncio
async def test_local_media_inspection_skips_remote_consent_but_keeps_fresh_authorization() -> None:
    class OwnerGuard(CapabilityGuard):
        async def evaluate_fresh_member(self, capability_id: str, *, guild: Any, member: Any) -> Any:
            assert guild.id == GUILD_ID
            return SimpleNamespace(
                allowed=self._capability_allowed(capability_id, RbacLevel.BOT_OWNER),
                actor_level=RbacLevel.BOT_OWNER,
            )

    class Adapter:
        requires_external_ai_consent = False

        def __init__(self) -> None:
            self.calls = 0

        async def inspect_for_message(
            self,
            _message: Any,
            *,
            url: str,
            instruction: str,
            authorization_current: Any,
        ) -> str | None:
            assert url and instruction
            assert await authorization_current()
            self.calls += 1
            return "ローカル解析結果"

    bot, message, guard = _message(
        "https://youtube.com/shorts/TG9KgEss-TE を字幕と画像で分析して",
        guard=OwnerGuard(),
        owner=True,
    )
    adapter = Adapter()
    consent = RemoteConsentStore(ttl_seconds=None)
    bot.media_url_inspection_adapter = adapter
    bot.ai_remote_consent_store = consent
    router = NaturalActionRouter(bot)
    _enable_planner_action_runtime(bot, message, router)
    request = AIRequest("test", GUILD_ID, USER_ID, CHANNEL_ID)

    reply = await router(message, request)

    assert reply is not None and reply.text == "ローカル解析結果"
    assert adapter.calls == 1
    assert consent.active(guild_id=GUILD_ID, channel_id=CHANNEL_ID, user_id=USER_ID) is False

    guard.states[ACTION_CAPABILITIES["media url-inspect"]] = False
    message.id += 1
    denied = await router(message, request)

    assert denied is not None and "利用できません" in denied.text
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_music_youtube_preview_requires_both_remote_browser_and_music_search() -> None:
    class OwnerGuard(CapabilityGuard):
        async def evaluate_fresh_member(self, capability_id: str, *, guild: Any, member: Any) -> Any:
            assert guild.id == GUILD_ID
            return SimpleNamespace(
                allowed=self._capability_allowed(capability_id, RbacLevel.BOT_OWNER),
                actor_level=RbacLevel.BOT_OWNER,
            )

    guard = OwnerGuard()
    bot, message, _ = _message("混沌ブギのYouTube画面をスクショして", guard=guard, owner=True)

    class Adapter:
        calls: list[dict[str, str]] = []

        async def capture_for_message(
            self,
            _message: Any,
            *,
            url: str,
            authorization_current: Any,
            target_kind: str,
        ) -> bool:
            assert await authorization_current()
            self.calls.append({"url": url, "target_kind": target_kind})
            return True

    adapter = Adapter()
    bot.browser_rendering_adapter = adapter
    router = NaturalActionRouter(bot)
    _enable_planner_action_runtime(bot, message, router)

    request = AIRequest("test", GUILD_ID, USER_ID, CHANNEL_ID)
    reply = await router(message, request)

    assert reply is not None and "返信として送信" in reply.text
    assert adapter.calls[0]["target_kind"] == "youtube_search"
    assert adapter.calls[0]["url"].startswith("https://www.youtube.com/results?")
    preview = router.registry.get("music.youtube-preview")
    assert preview.capability_requirements == (
        ("browser screenshot", ACTION_CAPABILITIES["browser screenshot"], RbacLevel.BOT_OWNER),
        ("music search-youtube", COMMAND_CAPABILITIES["music search-youtube"], RbacLevel.EVERYONE),
    )

    guard.states[COMMAND_CAPABILITIES["music search-youtube"]] = False
    message.id += 1
    revoked = await router(message, request)
    assert revoked is not None and "利用できません" in revoked.text
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_video_generation_exact_prefix_directly_dispatches_without_ai_tools() -> None:
    bot, message, guard = _message("動画を生成して: 青い海", owner=True)

    class Adapter:
        calls: list[dict[str, Any]] = []

        async def generate_for_message(self, received: Any, *, prompt: str, authorization_current: Any) -> bool:
            self.calls.append({"message": received, "prompt": prompt})
            return await authorization_current()

    adapter = Adapter()
    bot.video_generation_adapter = adapter

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert reply.provider == "local-action-router"
    assert "返信に添付" in reply.text
    assert "青い海" not in reply.text
    assert adapter.calls == [{"message": message, "prompt": "青い海"}]
    assert guard.calls[0]["capability_id"] == COMMAND_CAPABILITIES["video generate"]

    message.content = f"<@{BOT_ID}> 動画生成について説明して"
    guard.calls.clear()
    assert await NaturalActionRouter(bot)(message, _request()) is None
    assert guard.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    (
        "動画を生成して:",
        "動画を生成して: " + "海" * 1_101,
    ),
)
async def test_missing_or_oversized_explicit_video_request_never_falls_through_to_ai(text: str) -> None:
    bot, message, guard = _message(text, owner=True)

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert reply.provider == "local-action-router"
    assert "動画の内容を指定" in reply.text
    assert guard.calls[0]["capability_id"] == COMMAND_CAPABILITIES["video generate"]


@pytest.mark.asyncio
async def test_video_generation_fresh_member_revocation_prevents_adapter_execution() -> None:
    guard = CapabilityGuard(states={COMMAND_CAPABILITIES["video generate"]: False})
    bot, message, _ = _message("動画を作って: 青い海", guard=guard, owner=True)
    calls = 0

    class Adapter:
        async def generate_for_message(self, *_args: Any, **_kwargs: Any) -> bool:
            nonlocal calls
            calls += 1
            return True

    bot.video_generation_adapter = Adapter()

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert "利用できません" in reply.text
    assert calls == 0


@pytest.mark.asyncio
async def test_music_generation_without_explicit_rights_returns_at_mention_guide_without_provider() -> None:
    bot, message, guard = _message("音楽を生成して: 秘密の旋律", owner=True)
    calls = 0

    class Adapter:
        async def generate_for_message(self, *_args: Any, **_kwargs: Any) -> bool:
            nonlocal calls
            calls += 1
            return True

    bot.music_generation_adapter = Adapter()

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert reply.provider == "local-action-router"
    assert "@BOT 権利確認済みで音楽を作って" in reply.text
    assert "秘密の旋律" not in reply.text
    assert calls == 0
    assert guard.calls[0]["capability_id"] == COMMAND_CAPABILITIES["musicgen generate"]

    for text in ("混沌ブギを流して", "音楽生成について説明して"):
        message.content = f"<@{BOT_ID}> {text}"
        guard.calls.clear()
        if "流して" in text:
            routed = await NaturalActionRouter(bot)(message, _request())
            assert routed is not None
            assert "権利確認済み" not in routed.text
        else:
            assert await NaturalActionRouter(bot)(message, _request()) is None
        assert all(call["capability_id"] != COMMAND_CAPABILITIES["musicgen generate"] for call in guard.calls)


@pytest.mark.asyncio
async def test_confirmed_music_generation_directly_dispatches_without_ai_tools() -> None:
    bot, message, guard = _message("権利確認済みで音楽を作って: 静かなピアノ", owner=True)

    class Adapter:
        calls: list[dict[str, Any]] = []

        async def generate_for_message(self, received: Any, *, prompt: str, authorization_current: Any) -> bool:
            self.calls.append({"message": received, "prompt": prompt})
            return await authorization_current()

    adapter = Adapter()
    bot.music_generation_adapter = adapter

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert "返信に添付" in reply.text
    assert "静かなピアノ" not in reply.text
    assert adapter.calls == [{"message": message, "prompt": "静かなピアノ"}]
    assert guard.calls[0]["capability_id"] == COMMAND_CAPABILITIES["musicgen generate"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    (
        "権利確認済みで音楽を作って:",
        "権利確認済みで曲を生成して: " + "音" * 1_101,
    ),
)
async def test_confirmed_music_generation_missing_or_oversized_prompt_never_calls_adapter(text: str) -> None:
    bot, message, _ = _message(text, owner=True)
    calls = 0

    class Adapter:
        async def generate_for_message(self, *_args: Any, **_kwargs: Any) -> bool:
            nonlocal calls
            calls += 1
            return True

    bot.music_generation_adapter = Adapter()
    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert "権利を確認済みなら" in reply.text
    assert calls == 0


@pytest.mark.asyncio
async def test_confirmed_music_generation_fresh_member_revocation_prevents_adapter_execution() -> None:
    guard = CapabilityGuard(states={COMMAND_CAPABILITIES["musicgen generate"]: False})
    bot, message, _ = _message("権利確認済みで曲を作って: 静かなピアノ", guard=guard, owner=True)
    calls = 0

    class Adapter:
        async def generate_for_message(self, *_args: Any, **_kwargs: Any) -> bool:
            nonlocal calls
            calls += 1
            return True

    bot.music_generation_adapter = Adapter()
    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert "利用できません" in reply.text
    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    (
        "音楽を作って: " + "音" * 4_001,
        "曲を生成して:\n静かなピアノ",
    ),
)
async def test_invalid_explicit_music_generation_never_falls_through_to_ai(text: str) -> None:
    bot, message, guard = _message(text, owner=True)

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert reply.provider == "local-action-router"
    assert "@BOT 権利確認済みで音楽を作って" in reply.text
    assert guard.calls[0]["capability_id"] == COMMAND_CAPABILITIES["musicgen generate"]


@pytest.mark.asyncio
async def test_memory_remember_uses_only_request_owner_scope() -> None:
    bot, message, _ = _message("好物はラーメンを覚えておいて")
    calls: list[tuple[int, int, str]] = []

    class Memory:
        def remember(self, guild_id: int, user_id: int, text: str) -> Any:
            calls.append((guild_id, user_id, text))
            return SimpleNamespace(id=12)

    bot.personal_memory_service = Memory()
    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "`12`" in reply.text
    assert calls == [(GUILD_ID, USER_ID, "好物はラーメン")]


@pytest.mark.asyncio
async def test_memory_secret_rejection_is_reported_without_echoing_secret() -> None:
    bot, message, _ = _message("token=super-secret-value-12345を覚えて")

    class Memory:
        def remember(self, *_: Any) -> Any:
            raise SensitiveMemoryError("secret body")

    bot.personal_memory_service = Memory()
    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert "秘密" in reply.text
    assert "super-secret" not in reply.text


@pytest.mark.asyncio
async def test_memory_status_reads_only_callers_scope_and_never_displays_content() -> None:
    bot, message, _ = _message("私のメモリ状態を教えて", owner=True)
    calls: list[tuple[Any, ...]] = []
    items = (
        SimpleNamespace(kind=MemoryKind.FACT, content="表示してはいけない本文"),
        SimpleNamespace(kind=MemoryKind.CONVERSATION, content="会話本文"),
        SimpleNamespace(kind=MemoryKind.CONVERSATION, content="会話本文2"),
    )

    class Memory:
        def is_enabled(self, guild_id: int, user_id: int) -> bool:
            calls.append(("enabled", guild_id, user_id))
            return True

        def list_items(self, guild_id: int, user_id: int, *, limit: int) -> tuple[Any, ...]:
            calls.append(("list", guild_id, user_id, limit))
            return items

    bot.personal_memory_service = Memory()
    router = NaturalActionRouter(bot)

    reply = await router(message, _request())
    assert reply is not None
    assert "個人メモリ: ON" in reply.text
    assert "保存中: 3件（明示メモ 1 / 会話 2）" in reply.text
    assert "会話保持:" in reply.text and "明示メモ:" in reply.text
    assert "表示してはいけない本文" not in reply.text and "会話本文" not in reply.text
    assert ("list", GUILD_ID, USER_ID, 100) in calls

    message.content = f"<@{BOT_ID}> メモリ状態"
    assert (await router(message, _request())) is not None


@pytest.mark.asyncio
async def test_memory_status_off_or_stale_service_hides_read_result() -> None:
    bot, message, _ = _message("メモリ状態", owner=True)

    class OffMemory:
        def is_enabled(self, *_: Any) -> bool:
            return False

        def list_items(self, *_: Any, **__: Any) -> tuple[Any, ...]:
            return (SimpleNamespace(kind=MemoryKind.FACT, content="非表示本文"),)

    bot.personal_memory_service = OffMemory()
    off = await NaturalActionRouter(bot)(message, _request())
    assert off is not None and "個人メモリ: OFF" in off.text and "保存中: 0件" in off.text
    assert "非表示本文" not in off.text

    replacement = OffMemory()

    class ReplacingMemory:
        def is_enabled(self, *_: Any) -> bool:
            return True

        def list_items(self, *_: Any, **__: Any) -> tuple[Any, ...]:
            bot.personal_memory_service = replacement
            return (SimpleNamespace(kind=MemoryKind.FACT, content="差替え前本文"),)

    bot.personal_memory_service = ReplacingMemory()
    stale = await NaturalActionRouter(bot)(message, _request())
    assert stale is not None and "表示しませんでした" in stale.text and "差替え前本文" not in stale.text


class MusicService:
    available = True

    def __init__(self, tracks: tuple[Track, ...]) -> None:
        self.tracks = tracks
        self.join_calls = 0
        self.play_calls = 0
        self.search_calls = 0
        self.close_calls = 0
        self.grant_calls = 0
        self.revoke_calls = 0
        self.leave_calls = 0
        self.playlist_calls: list[tuple[str, str | None]] = []
        self.speech_calls: list[tuple[int, Any, bytes]] = []
        self.control_calls: list[tuple[str, Any]] = []
        self.radio_calls: list[bool] = []
        self.channel_id: int | None = None

    async def search(self, *_: Any, **__: Any) -> tuple[Track, ...]:
        self.search_calls += 1
        return self.tracks

    def session_channel_id(self, _: int) -> int | None:
        return self.channel_id

    async def join(
        self,
        _: int,
        __: Any,
        actor: Any,
        *,
        voice_channel_id: int,
        commit_check: Any = None,
    ) -> None:
        assert commit_check is None or await commit_check() is not None
        self.join_calls += 1
        self.channel_id = voice_channel_id
        assert actor.voice_channel_id == voice_channel_id

    async def play(self, _: int, query: str, __: Any, *, commit_check: Any = None) -> tuple[Track, int]:
        assert commit_check is None or await commit_check() is not None
        self.play_calls += 1
        assert query == self.tracks[0].title
        return self.tracks[0], 1

    async def snapshot(self, _: int) -> QueueSnapshot:
        return QueueSnapshot(self.tracks[0] if self.tracks else None, (), LoopMode.OFF, False, 0.65)

    async def close_guild(self, _: int) -> bool:
        self.close_calls += 1
        self.channel_id = None
        return True

    async def leave(self, _: int, __: Any, *, commit_check: Any = None) -> bool:
        assert commit_check is not None and await commit_check() is not None
        self.leave_calls += 1
        self.channel_id = None
        return True

    async def add_speech_wav(self, guild_id: int, actor: Any, wav: bytes, *, commit_check: Any = None) -> int:
        fresh_actor = await commit_check() if commit_check is not None else None
        if fresh_actor is None:
            raise MusicAuthorizationError("stale")
        if fresh_actor.voice_channel_id != self.channel_id:
            raise MusicSessionError("different voice channel")
        self.speech_calls.append((guild_id, actor, wav))
        return len(self.speech_calls)

    async def _control(self, name: str, actor: Any, value: Any = None, *, commit_check: Any = None) -> Any:
        fresh_actor = await commit_check() if commit_check is not None else None
        if fresh_actor is None:
            raise MusicAuthorizationError("stale")
        if fresh_actor.voice_channel_id != self.channel_id:
            raise MusicSessionError("different voice channel")
        self.control_calls.append((name, value))
        return len(self.tracks)

    async def set_volume(self, _: int, actor: Any, value: float, *, commit_check: Any = None) -> None:
        await self._control("volume", actor, value, commit_check=commit_check)

    async def set_speech_volume(self, _: int, actor: Any, value: float, *, commit_check: Any = None) -> None:
        await self._control("speech_volume", actor, value, commit_check=commit_check)

    async def set_loop(self, _: int, actor: Any, value: LoopMode, *, commit_check: Any = None) -> None:
        await self._control("loop", actor, value, commit_check=commit_check)

    async def set_local_radio(
        self,
        _: int,
        actor: Any,
        enabled: bool,
        *,
        commit_check: Any = None,
    ) -> bool:
        await self._control("radio", actor, enabled, commit_check=commit_check)
        self.radio_calls.append(enabled)
        return enabled

    async def seek(self, _: int, actor: Any, seconds: int, *, commit_check: Any = None) -> Track:
        await self._control("seek", actor, seconds, commit_check=commit_check)
        if not self.tracks:
            raise MusicSessionError("music is not playing")
        return self.tracks[0]

    async def shuffle(self, _: int, actor: Any, *, commit_check: Any = None) -> int:
        return int(await self._control("shuffle", actor, commit_check=commit_check))

    async def remove(self, _: int, actor: Any, position: int, *, commit_check: Any = None) -> Track:
        await self._control("remove", actor, position, commit_check=commit_check)
        if not 1 <= position <= len(self.tracks):
            raise MusicSessionError("queue position is out of range")
        return self.tracks[position - 1]

    async def list_playlists(self, _: int, __: Any) -> tuple[Any, ...]:
        self.playlist_calls.append(("list", None))
        return (SimpleNamespace(name="お気に入り"),)

    async def save_playlist(self, _: int, __: Any, name: str, *, commit_check: Any = None) -> Any:
        assert commit_check is not None and await commit_check() is not None
        self.playlist_calls.append(("save", name))
        return SimpleNamespace(name=name)

    async def load_playlist(self, _: int, __: Any, name: str, *, commit_check: Any = None) -> tuple[int, int]:
        assert commit_check is not None and await commit_check() is not None
        self.playlist_calls.append(("load", name))
        return (2, 0)

    async def delete_playlist(self, _: int, __: Any, name: str, *, commit_check: Any = None) -> bool:
        assert commit_check is not None and await commit_check() is not None
        self.playlist_calls.append(("delete", name))
        return True

    async def grant_track_rights(self, _: int, query: str, actor: Any, *, commit_check: Any = None) -> None:
        assert commit_check is None or await commit_check() is not None
        assert actor.manage_guild
        assert query == self.tracks[0].title
        self.grant_calls += 1

    async def revoke_track_rights(self, _: int, query: str, actor: Any, *, commit_check: Any = None) -> bool:
        assert commit_check is None or await commit_check() is not None
        assert actor.manage_guild
        assert query == self.tracks[0].title
        self.revoke_calls += 1
        return True


class OrderedPlannerMusicService(MusicService):
    def __init__(self, tracks: tuple[Track, ...]) -> None:
        super().__init__(tracks)
        self.played_titles: list[str] = []

    async def search(self, query: str, *_: Any, **__: Any) -> tuple[Track, ...]:
        self.search_calls += 1
        normalized = query.casefold()
        return tuple(track for track in self.tracks if normalized in track.title.casefold())

    async def play(
        self,
        _: int,
        query: str,
        __: Any,
        *,
        commit_check: Any = None,
    ) -> tuple[Track, int]:
        assert commit_check is not None and await commit_check() is not None
        track = next(track for track in self.tracks if track.title == query)
        self.play_calls += 1
        self.played_titles.append(track.title)
        return track, len(self.played_titles)


def _planner_music_plan(*queries: str, idempotency_key: str) -> OrchestrationPlan:
    return OrchestrationPlan(
        "music-plan",
        GUILD_ID,
        CHANNEL_ID,
        USER_ID,
        idempotency_key,
        tuple(
            OrchestrationStep(
                f"music-{index}",
                "music.enqueue",
                {"query": query},
            )
            for index, query in enumerate(queries, start=1)
        ),
    )


@pytest.mark.asyncio
async def test_planner_runs_multiple_youtube_previews_in_order_and_idempotently() -> None:
    class OwnerGuard(CapabilityGuard):
        async def evaluate_fresh_member(self, capability_id: str, *, guild: Any, member: Any) -> Any:
            assert guild.id == GUILD_ID
            return SimpleNamespace(
                allowed=self._capability_allowed(capability_id, RbacLevel.BOT_OWNER),
                actor_level=RbacLevel.BOT_OWNER,
            )

    bot, message, _ = _message("複数曲の検索画面", guard=OwnerGuard(), owner=True)

    class Adapter:
        calls: list[str] = []

        async def capture_for_message(
            self,
            _message: Any,
            *,
            url: str,
            authorization_current: Any,
            target_kind: str,
        ) -> bool:
            assert target_kind == "youtube_search"
            assert await authorization_current()
            self.calls.append(parse_qs(urlparse(url).query)["search_query"][0])
            return True

    adapter = Adapter()
    bot.browser_rendering_adapter = adapter
    router = NaturalActionRouter(bot)
    _enable_planner_action_runtime(bot, message, router)
    engine = OrchestrationEngine(router)
    request = AIRequest("plan", GUILD_ID, USER_ID, CHANNEL_ID)
    plan = OrchestrationPlan(
        "preview-plan",
        GUILD_ID,
        CHANNEL_ID,
        USER_ID,
        "preview-order",
        (
            OrchestrationStep("preview-1", "music.youtube-preview", {"query": "混沌ブギ"}),
            OrchestrationStep("preview-2", "music.youtube-preview", {"query": "千本桜"}),
        ),
    )

    first = await engine.execute_outcome(
        plan,
        message=message,
        request=request,
        request_id=plan.request_id,
    )
    second = await engine.execute_outcome(
        plan,
        message=message,
        request=request,
        request_id=plan.request_id,
    )

    assert first == second
    assert first.receipt.status is PlanStatus.COMPLETED
    assert adapter.calls == ["混沌ブギ", "千本桜"]


@pytest.mark.asyncio
async def test_planner_music_enqueue_preserves_order_connects_once_and_is_idempotent() -> None:
    voice = VoiceChannel()
    bot, message, _ = _message("複数曲", voice_channel=voice)
    tracks = (
        Track("混沌ブギ", Path("library/chaos.mp3"), USER_ID),
        Track("千本桜", Path("library/senbon.mp3"), USER_ID),
    )
    service = OrderedPlannerMusicService(tracks)
    bot.music_service = service
    router = NaturalActionRouter(bot)
    _enable_planner_action_runtime(bot, message, router)
    engine = OrchestrationEngine(router)
    request = AIRequest("plan", GUILD_ID, USER_ID, CHANNEL_ID)
    plan = _planner_music_plan("混沌ブギ", "千本桜", idempotency_key="music-order")

    first = await engine.execute_outcome(
        plan,
        message=message,
        request=request,
        request_id=plan.request_id,
    )
    second = await engine.execute_outcome(
        plan,
        message=message,
        request=request,
        request_id=plan.request_id,
    )

    assert first == second
    assert first.receipt.status is PlanStatus.COMPLETED
    assert service.played_titles == ["混沌ブギ", "千本桜"]
    assert voice.connect_calls == 1
    assert service.join_calls == 1


@pytest.mark.parametrize(
    ("query", "tracks"),
    (
        ("存在しない曲", ()),
        (
            "混沌",
            (
                Track("混沌ブギ", Path("library/chaos-a.mp3"), USER_ID),
                Track("混沌ダンス", Path("library/chaos-b.mp3"), USER_ID),
            ),
        ),
    ),
)
@pytest.mark.asyncio
async def test_planner_music_enqueue_never_falls_back_when_local_match_is_not_unique(
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    tracks: tuple[Track, ...],
) -> None:
    monkeypatch.setattr(
        "yonerai_discord.modules.ai.action_router.youtube_search_url",
        lambda _query: (_ for _ in ()).throw(AssertionError("planner enqueue must not use external fallback")),
    )
    voice = VoiceChannel()
    bot, message, _ = _message("複数曲", voice_channel=voice)
    service = OrderedPlannerMusicService(tracks)
    bot.music_service = service
    router = NaturalActionRouter(bot)
    _enable_planner_action_runtime(bot, message, router)
    engine = OrchestrationEngine(router)
    request = AIRequest("plan", GUILD_ID, USER_ID, CHANNEL_ID)
    plan = _planner_music_plan(query, idempotency_key=f"music-no-match-{len(tracks)}")

    outcome = await engine.execute_outcome(
        plan,
        message=message,
        request=request,
        request_id=plan.request_id,
    )

    assert outcome.receipt.status is PlanStatus.FAILED
    assert outcome.receipt.steps[0].status is StepStatus.FAILED
    assert service.played_titles == []
    assert voice.connect_calls == 0
    assert service.join_calls == 0


@pytest.mark.asyncio
async def test_planner_music_enqueue_stops_after_partial_success_without_rolling_back() -> None:
    voice = VoiceChannel()
    bot, message, _ = _message("複数曲", voice_channel=voice)
    tracks = (
        Track("混沌ブギ", Path("library/chaos-a.mp3"), USER_ID),
        Track("混沌ダンス", Path("library/chaos-b.mp3"), USER_ID),
        Track("千本桜", Path("library/senbon.mp3"), USER_ID),
    )
    service = OrderedPlannerMusicService(tracks)
    bot.music_service = service
    router = NaturalActionRouter(bot)
    _enable_planner_action_runtime(bot, message, router)
    engine = OrchestrationEngine(router)
    request = AIRequest("plan", GUILD_ID, USER_ID, CHANNEL_ID)
    plan = _planner_music_plan("混沌ブギ", "混沌", "千本桜", idempotency_key="music-partial")

    outcome = await engine.execute_outcome(
        plan,
        message=message,
        request=request,
        request_id=plan.request_id,
    )

    assert outcome.receipt.status is PlanStatus.FAILED
    assert [step.status for step in outcome.receipt.steps] == [
        StepStatus.COMPLETED,
        StepStatus.FAILED,
        StepStatus.NOT_RUN,
    ]
    assert service.played_titles == ["混沌ブギ"]
    assert voice.connect_calls == 1
    assert service.close_calls == 0


@pytest.mark.asyncio
async def test_music_local_search_uses_only_local_library_with_fresh_actor() -> None:
    tracks = tuple(Track(f"曲{index}", Path(f"library/{index}.mp3"), USER_ID) for index in range(1, 12))
    bot, message, _ = _message("ローカル曲で　曲　を検索して", owner=True)
    service = MusicService(tracks)
    bot.music_service = service
    bot.ai_service = SimpleNamespace(__getattr__=lambda *_: (_ for _ in ()).throw(AssertionError("AI must not run")))

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and reply.text.startswith("ローカル曲の検索結果:\n")
    assert "1. 曲1" in reply.text and "10. 曲10" in reply.text and "11." not in reply.text
    assert service.search_calls == 1
    assert service.join_calls == service.play_calls == service.leave_calls == 0
    assert service.playlist_calls == [] and service.grant_calls == service.revoke_calls == 0


@pytest.mark.asyncio
async def test_music_local_search_handles_empty_invalid_and_post_search_revocation() -> None:
    bot, message, _ = _message("ローカル曲で なし を検索して", owner=True)
    empty = MusicService(())
    bot.music_service = empty
    router = NaturalActionRouter(bot)

    zero = await router(message, _request())
    assert zero is not None and zero.text == "一致するローカル曲はありません。"
    assert empty.search_calls == 1

    message.content = f"<@{BOT_ID}> ローカル曲で <@123> を検索して"
    assert await router(message, _request()) is None
    message.content = f"<@{BOT_ID}> ローカル曲で {'a' * 201} を検索して"
    assert await router(message, _request()) is None
    assert empty.search_calls == 1

    guard = CapabilityGuard()
    stale_bot, stale_message, _ = _message("ローカル曲で 曲 を検索して", owner=True, guard=guard)

    class RevokingMusicService(MusicService):
        async def search(self, *args: Any, **kwargs: Any) -> tuple[Track, ...]:
            result = await super().search(*args, **kwargs)
            guard.states[COMMAND_CAPABILITIES["music search"]] = False
            return result

    stale = RevokingMusicService((Track("失効後の曲", Path("library/stale.mp3"), USER_ID),))
    stale_bot.music_service = stale
    denied = await NaturalActionRouter(stale_bot)(stale_message, _request())
    assert denied is not None and "表示しませんでした" in denied.text and "失効後の曲" not in denied.text
    assert stale.search_calls == 1 and stale.join_calls == stale.play_calls == stale.leave_calls == 0


@pytest.mark.asyncio
async def test_music_speak_uses_existing_speech_queue_and_ducking_session_without_echoing_text() -> None:
    class SpeechQueue:
        available = True

        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def synthesize(self, request: Any, *, current_policy: Any) -> Any:
            assert await current_policy() is True
            self.requests.append(request)
            return SimpleNamespace(wav=b"RIFF-voice")

    voice = VoiceChannel()
    bot, message, _ = _message("読み上げて： 秘密の本文", owner=True, voice_channel=voice)
    service = MusicService(())
    service.channel_id = voice.id
    queue = SpeechQueue()
    bot.music_service = service
    bot.speech_queue = queue
    bot.ai_service = SimpleNamespace(__getattr__=lambda *_: (_ for _ in ()).throw(AssertionError("AI must not run")))

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "TTS queue 1番" in reply.text and "ducking" in reply.text
    assert "秘密の本文" not in reply.text
    assert len(queue.requests) == 1
    assert (
        queue.requests[0].text,
        queue.requests[0].guild_id,
        queue.requests[0].channel_id,
        queue.requests[0].speaker_id,
    ) == (
        "秘密の本文",
        GUILD_ID,
        CHANNEL_ID,
        3,
    )
    assert len(service.speech_calls) == 1
    assert service.join_calls == service.play_calls == service.leave_calls == service.search_calls == 0


@pytest.mark.asyncio
async def test_music_speak_rejects_invalid_scope_and_post_synthesis_revocation_without_audio_sink() -> None:
    class SpeechQueue:
        available = True

        def __init__(self, after_synthesis: Any = None) -> None:
            self.after_synthesis = after_synthesis
            self.calls = 0

        async def synthesize(self, _: Any, *, current_policy: Any) -> Any:
            assert await current_policy() is True
            self.calls += 1
            if self.after_synthesis is not None:
                self.after_synthesis()
            return SimpleNamespace(wav=b"RIFF-voice")

    voice = VoiceChannel()
    bot, message, _ = _message("読み上げて: 本文", owner=True, voice_channel=voice)
    service = MusicService(())
    service.channel_id = voice.id + 1
    queue = SpeechQueue()
    bot.music_service = service
    bot.speech_queue = queue

    wrong_channel = await NaturalActionRouter(bot)(message, _request())
    assert wrong_channel is not None and "安全に追加できませんでした" in wrong_channel.text
    assert queue.calls == 1 and service.speech_calls == []

    message.content = f"<@{BOT_ID}> 読み上げて: {'a' * 501}"
    too_long = await NaturalActionRouter(bot)(message, _request())
    assert too_long is not None and "1〜500文字" in too_long.text
    message.content = f"<@{BOT_ID}> 読み上げて: 改行\n本文"
    control = await NaturalActionRouter(bot)(message, _request())
    assert control is not None and "1〜500文字" in control.text and "改行" not in control.text
    message.content = f"<@{BOT_ID}> 読み上げて: 制御\u0085本文"
    unicode_control = await NaturalActionRouter(bot)(message, _request())
    assert (
        unicode_control is not None
        and "1〜500文字" in unicode_control.text
        and "\u0085本文" not in unicode_control.text
    )
    assert queue.calls == 1

    guard = CapabilityGuard()
    stale_bot, stale_message, _ = _message("読み上げて: 失効本文", owner=True, voice_channel=voice, guard=guard)
    stale_service = MusicService(())
    stale_service.channel_id = voice.id
    stale_queue = SpeechQueue(lambda: guard.states.update({EVENT_CAPABILITIES["ai_mention_message"]: False}))
    stale_bot.music_service = stale_service
    stale_bot.speech_queue = stale_queue
    stale = await NaturalActionRouter(stale_bot)(stale_message, _request())
    assert stale is not None and "追加しませんでした" in stale.text and "失効本文" not in stale.text
    assert stale_queue.calls == 1 and stale_service.speech_calls == []

    swap_bot, swap_message, _ = _message("読み上げて: 差替え本文", owner=True, voice_channel=voice)
    swap_service = MusicService(())
    swap_service.channel_id = voice.id
    swap_queue = SpeechQueue()
    swap_queue.after_synthesis = lambda: setattr(swap_bot, "speech_queue", SpeechQueue())
    swap_bot.music_service = swap_service
    swap_bot.speech_queue = swap_queue
    swapped = await NaturalActionRouter(swap_bot)(swap_message, _request())
    assert swapped is not None and "追加しませんでした" in swapped.text and "差替え本文" not in swapped.text
    assert swap_queue.calls == 1 and swap_service.speech_calls == []


@pytest.mark.asyncio
async def test_music_rights_mentions_require_admin_and_never_use_ai_provider() -> None:
    track = Track("混沌ブギ", Path("library/song.mp3"), USER_ID)
    bot, message, _ = _message("権利確認済みで曲を許可: 混沌ブギ", owner=True)
    service = MusicService((track,))
    bot.music_service = service
    bot.ai_service = SimpleNamespace(__getattr__=lambda *_: (_ for _ in ()).throw(AssertionError("AI must not run")))

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "再生を許可" in reply.text
    assert service.grant_calls == 1
    assert service.revoke_calls == 0

    denied_bot, denied_message, _ = _message("曲の許可を取り消して: 混沌ブギ", owner=False)
    denied_service = MusicService((track,))
    denied_bot.music_service = denied_service
    denied = await NaturalActionRouter(denied_bot)(denied_message, _request())

    assert denied is not None and "利用できません" in denied.text
    assert denied_service.revoke_calls == 0


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("VCから退出して", ("leave", None)),
        ("プレイリスト一覧", ("list", None)),
        ("プレイリストを保存: お気に入り", ("save", "お気に入り")),
        ("プレイリストを読み込んで: お気に入り", ("load", "お気に入り")),
        ("プレイリストを完全削除: お気に入り", ("delete", "お気に入り")),
    ),
)
@pytest.mark.asyncio
async def test_music_management_mentions_use_existing_service_without_ai(
    text: str,
    expected: tuple[str, str | None],
) -> None:
    bot, message, _ = _message(text, voice_channel=VoiceChannel())
    service = MusicService((Track("混沌ブギ", Path("library/song.mp3"), USER_ID),))
    bot.music_service = service

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and reply.provider == "local-action-router"
    if expected[0] == "leave":
        assert service.leave_calls == 1
    else:
        assert expected in service.playlist_calls


@pytest.mark.asyncio
async def test_playlist_delete_requires_explicit_complete_phrase_and_fresh_capability() -> None:
    track = Track("混沌ブギ", Path("library/song.mp3"), USER_ID)
    bot, message, _ = _message("プレイリストを削除: お気に入り", voice_channel=VoiceChannel())
    service = MusicService((track,))
    bot.music_service = service

    assert await NaturalActionRouter(bot)(message, _request()) is None
    assert service.playlist_calls == []

    guard = CapabilityGuard(states={COMMAND_CAPABILITIES["music playlist delete"]: False})
    denied_bot, denied_message, _ = _message(
        "プレイリストを完全削除: お気に入り",
        guard=guard,
        voice_channel=VoiceChannel(),
    )
    denied_service = MusicService((track,))
    denied_bot.music_service = denied_service
    denied = await NaturalActionRouter(denied_bot)(denied_message, _request())

    assert denied is not None and "利用できません" in denied.text
    assert denied_service.playlist_calls == []


@pytest.mark.asyncio
async def test_mod_reference_mentions_are_scoped_fresh_and_never_use_ai() -> None:
    bot, message, _ = _message("<@456> の警告履歴", owner=True)
    target_id = 456

    async def fetch_member(user_id: int) -> Any:
        if user_id not in {USER_ID, target_id}:
            raise LookupError("missing")
        return SimpleNamespace(id=user_id, guild_permissions=message.author.guild_permissions)

    message.guild.fetch_member = fetch_member
    repository = SimpleNamespace(
        warnings_for=lambda guild_id, member_id: (
            tuple(SimpleNamespace(case_id=index + 1, reason=f"理由 {index}") for index in range(25))
            if (guild_id, member_id) == (GUILD_ID, target_id)
            else ()
        ),
        get_case=lambda guild_id, case_id: (
            SimpleNamespace(
                guild_id=guild_id,
                action=SimpleNamespace(value="warn"),
                status="completed",
                reason="短い理由",
            )
            if (guild_id, case_id) == (GUILD_ID, 123)
            else None
        ),
    )
    plugin = SimpleNamespace(bot=bot, repository=repository, closing=False)
    bot.modtools_repository = repository
    bot.modtools_plugin = plugin

    warnings_reply = await NaturalActionRouter(bot)(message, _request())
    assert warnings_reply is not None and warnings_reply.provider == "local-action-router"
    assert "Case #20: 理由 19" in warnings_reply.text
    assert "理由 20" not in warnings_reply.text

    message.content = f"<@{BOT_ID}> Case 123を表示"
    case_reply = await NaturalActionRouter(bot)(message, _request())
    assert case_reply is not None and "Case 123" in case_reply.text


@pytest.mark.asyncio
async def test_mod_reference_rejects_invalid_target_case_or_stale_repository() -> None:
    bot, message, _ = _message("<@456> の警告履歴", owner=True)
    repository = SimpleNamespace(warnings_for=lambda *_: (_ for _ in ()).throw(AssertionError("must not read")))
    bot.modtools_repository = repository
    bot.modtools_plugin = SimpleNamespace(bot=bot, repository=repository, closing=True)

    denied = await NaturalActionRouter(bot)(message, _request())
    assert denied is not None and "安全に確認" in denied.text

    message.content = f"<@{BOT_ID}> Case 0を表示"
    assert await NaturalActionRouter(bot)(message, _request()) is None

    message.content = "<@456> の警告履歴"
    assert await NaturalActionRouter(bot)(message, _request()) is None


@pytest.mark.asyncio
async def test_scheduling_reference_actions_use_scoped_repository_and_fresh_guards() -> None:
    guard = CapabilityGuard()
    bot, message, _ = _message("予定一覧", owner=True, guard=guard)
    now = datetime.now(UTC) + timedelta(days=1)
    meeting = SimpleNamespace(
        id="MEET-1234ABCD",
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        creator_id=USER_ID,
        title="定例会議",
        starts_at=now,
        ends_at=now + timedelta(hours=1),
        timezone="Asia/Tokyo",
    )
    saved_meetings: list[Any] = []
    saved_rsvps: list[Any] = []
    repository = SimpleNamespace(
        list_meetings=lambda guild_id, _now, limit: (meeting,) if (guild_id, limit) == (GUILD_ID, 25) else (),
        save_meeting=lambda value: saved_meetings.append(value) is None,
        get_meeting=lambda meeting_id: meeting if meeting_id == meeting.id else None,
        save_rsvp=lambda value: saved_rsvps.append(value),
    )
    bot.scheduling_repository = repository
    bot.scheduling_plugin = SimpleNamespace(bot=bot, repository=repository, closing=False)
    router = NaturalActionRouter(bot)

    listed = await router(message, _request())
    assert listed is not None and "MEET-1234ABCD" in listed.text
    assert len(guard.current_calls) >= 4

    message.content = f"<@{BOT_ID}> MEET-1234ABCD の予定を見せて"
    shown = await router(message, _request())
    assert shown is not None and "定例会議" in shown.text and "Timezone: `" in shown.text

    message.content = f"<@{BOT_ID}> 予定 MEET-1234ABCD の詳細を見せて"
    assert (await router(message, _request())) is not None

    message.content = f"<@{BOT_ID}> 予定を作成: 定例会議 | 2026-08-01T10:00+09:00 | 2026-08-01T11:00+09:00 | Asia/Tokyo"
    created = await router(message, _request())
    assert created is not None and "登録しました" in created.text
    assert len(saved_meetings) == 1
    assert saved_meetings[0].guild_id == GUILD_ID
    assert saved_meetings[0].channel_id == CHANNEL_ID
    assert saved_meetings[0].creator_id == USER_ID

    message.content = f"<@{BOT_ID}> 出欠: MEET-1234ABCD | attending"
    rsvp = await router(message, _request())
    assert rsvp is not None and rsvp.text == "参加で回答しました。"
    assert len(saved_rsvps) == 1
    assert saved_rsvps[0].meeting_id == meeting.id
    assert saved_rsvps[0].user_id == USER_ID


@pytest.mark.asyncio
async def test_scheduling_reference_actions_fail_closed_for_stale_repository_or_invalid_typed_input() -> None:
    guard = CapabilityGuard()
    bot, message, _ = _message("予定一覧", owner=True, guard=guard)
    repository = SimpleNamespace(
        list_meetings=lambda *_: guard.states.update({COMMAND_CAPABILITIES["schedule list"]: False}) or ()
    )
    bot.scheduling_repository = repository
    bot.scheduling_plugin = SimpleNamespace(bot=bot, repository=repository, closing=False)

    denied = await NaturalActionRouter(bot)(message, _request())
    assert denied is not None and "安全に確認" in denied.text

    message.content = f"<@{BOT_ID}> 予定を作成: 会議 | 2026-08-01T10:00 | 2026-08-01T11:00+09:00"
    invalid_date = await NaturalActionRouter(bot)(message, _request())
    assert invalid_date is not None and "日時またはtimezoneが不正" in invalid_date.text
    message.content = f"<@{BOT_ID}> 出欠: MEET-1234ABCD | maybe"
    assert await NaturalActionRouter(bot)(message, _request()) is None

    message.content = f"<@{BOT_ID}> MEET-1234ABCD の予定を見せて"
    assert await NaturalActionRouter(bot)(message, _request()) is not None
    message.content = f"<@{BOT_ID}> MEET-abcdef12 の予定を見せて"
    assert await NaturalActionRouter(bot)(message, _request()) is None


@pytest.mark.asyncio
async def test_schedule_show_hides_cross_guild_and_post_read_revocation() -> None:
    guard = CapabilityGuard()
    bot, message, _ = _message("MEET-1234ABCD の予定を見せて", owner=True, guard=guard)
    now = datetime.now(UTC) + timedelta(days=1)
    foreign = SimpleNamespace(
        id="MEET-1234ABCD",
        guild_id=GUILD_ID + 1,
        title="別ギルド予定",
        starts_at=now,
        ends_at=now + timedelta(hours=1),
        timezone="Asia/Tokyo",
    )
    repository = SimpleNamespace(get_meeting=lambda _: foreign)
    bot.scheduling_repository = repository
    bot.scheduling_plugin = SimpleNamespace(bot=bot, repository=repository, closing=False)

    hidden = await NaturalActionRouter(bot)(message, _request())
    assert hidden is not None and "別ギルド予定" not in hidden.text and "表示しませんでした" in hidden.text

    local = SimpleNamespace(
        id="MEET-1234ABCD",
        guild_id=GUILD_ID,
        title="失効後予定",
        starts_at=now,
        ends_at=now + timedelta(hours=1),
        timezone="Asia/Tokyo",
    )

    def revoke_after_read(_: str) -> Any:
        guard.states[COMMAND_CAPABILITIES["schedule show"]] = False
        return local

    repository.get_meeting = revoke_after_read
    revoked = await NaturalActionRouter(bot)(message, _request())
    assert revoked is not None and "失効後予定" not in revoked.text and "表示しませんでした" in revoked.text


@pytest.mark.asyncio
async def test_music_play_uses_only_unique_local_match_and_connects_callers_vc() -> None:
    voice = VoiceChannel()
    bot, message, _ = _message("混沌ブギを流して", voice_channel=voice)
    track = Track("混沌ブギ", Path("library/song.mp3"), USER_ID)
    service = MusicService((track,))
    bot.music_service = service

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "ローカル曲" in reply.text
    assert voice.connect_calls == 1
    assert voice.connect_kwargs == [{"self_deaf": True, "reconnect": False}]
    assert service.join_calls == 1
    assert service.play_calls == 1


@pytest.mark.asyncio
async def test_music_request_unique_local_match_without_voice_adds_waiting_queue_without_join() -> None:
    bot, message, _ = _message("混沌ブギをながして")
    track = Track("混沌ブギ", Path("library/song.mp3"), USER_ID)
    service = MusicService((track,))
    bot.music_service = service

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "待機キュー 1 番" in reply.text
    assert "VCへ参加すると開始します" in reply.text
    assert "再起動後は /music join が必要です" in reply.text
    assert service.search_calls == 1
    assert service.join_calls == 0
    assert service.play_calls == 1


@pytest.mark.asyncio
async def test_music_voice_free_queue_hides_success_after_post_commit_policy_revoke() -> None:
    class BlockingPlayService(MusicService):
        def __init__(self, tracks: tuple[Track, ...]) -> None:
            super().__init__(tracks)
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def play(
            self,
            _: int,
            query: str,
            __: Any,
            *,
            commit_check: Any,
        ) -> tuple[Track, int]:
            assert await commit_check() is not None
            self.play_calls += 1
            self.started.set()
            await self.release.wait()
            assert query == self.tracks[0].title
            return self.tracks[0], 1

    guard = CapabilityGuard()
    bot, message, _ = _message("秘密曲をながして", guard=guard)
    service = BlockingPlayService((Track("秘密曲", Path("library/song.mp3"), USER_ID),))
    bot.music_service = service
    task = asyncio.create_task(NaturalActionRouter(bot)(message, _request()))
    await asyncio.wait_for(service.started.wait(), timeout=1.0)
    bot.capability_guard = CapabilityGuard()
    service.release.set()

    reply = await task

    assert reply is not None and "追加結果を表示しませんでした" in reply.text
    assert "待機キュー" not in reply.text
    assert service.join_calls == 0
    assert service.play_calls == 1


@pytest.mark.parametrize("suffix", ("流して", "ながして", "再生して", "かけて"))
@pytest.mark.asyncio
async def test_music_request_accepts_only_supported_full_suffixes(suffix: str) -> None:
    voice = VoiceChannel()
    bot, message, _ = _message(f"混沌ブギ{suffix}", voice_channel=voice)
    track = Track("混沌ブギ", Path("library/song.mp3"), USER_ID)
    service = MusicService((track,))
    bot.music_service = service

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and reply.provider == "local-action-router"
    assert service.search_calls == 1
    assert service.play_calls == 1


@pytest.mark.asyncio
async def test_music_request_normalizes_nfkc_before_matching_local_title() -> None:
    voice = VoiceChannel()
    bot, message, _ = _message("ＡＢＣをながして", voice_channel=voice)
    track = Track("ABC", Path("library/song.mp3"), USER_ID)
    service = MusicService((track,))
    bot.music_service = service

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "ローカル曲" in reply.text
    assert service.play_calls == 1


@pytest.mark.parametrize(
    "text",
    ("混沌ブギ を 流して", "混沌ブギ　を　流して"),
)
@pytest.mark.asyncio
async def test_music_request_normalizes_spaces_around_particle_and_verb(text: str) -> None:
    voice = VoiceChannel()
    bot, message, _ = _message(text, voice_channel=voice)
    track = Track("混沌ブギ", Path("library/song.mp3"), USER_ID)
    service = MusicService((track,))
    bot.music_service = service

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "ローカル曲" in reply.text
    assert service.search_calls == 1
    assert service.play_calls == 1


@pytest.mark.parametrize("text", ("流して", "ながして", "再生して", "かけて"))
@pytest.mark.asyncio
async def test_music_request_with_empty_query_returns_fixed_guidance_without_service(text: str) -> None:
    bot, message, _ = _message(text)

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert reply.provider == "local-action-router"
    assert reply.text == "曲名を指定してください。例: `@BOT 混沌ブギながして`"


@pytest.mark.parametrize("text", ("を 流して", "を　流して"))
@pytest.mark.asyncio
async def test_music_request_with_only_particle_returns_empty_query_guidance(text: str) -> None:
    bot, message, _ = _message(text)

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert reply.text == "曲名を指定してください。例: `@BOT 混沌ブギながして`"


@pytest.mark.asyncio
async def test_music_play_with_no_local_match_returns_official_youtube_search_link_only() -> None:
    voice = VoiceChannel()
    bot, message, _ = _message("混沌ブギ [live]を流して", voice_channel=voice)
    service = MusicService(())
    bot.music_service = service

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert "YouTube公式検索（リンクのみ）" in reply.text
    assert "[1](https://www.youtube.com/results?search_query=" in reply.text
    assert "https://www.youtube.com/results?search_query=" in reply.text
    assert "%5Blive%5D" in reply.text
    assert "音声抽出・ダウンロード・VC中継は行いません" in reply.text
    assert "再生しました" not in reply.text
    assert "キュー" not in reply.text
    assert "追加しました" not in reply.text
    assert voice.connect_calls == 0
    assert service.join_calls == 0
    assert service.play_calls == 0


@pytest.mark.asyncio
async def test_music_link_only_never_touches_ai_provider_or_remote_consent() -> None:
    class ForbiddenExternalBoundary:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"link-only must not access external boundary: {name}")

    bot, message, _ = _message("混沌ブギをながして")
    service = MusicService(())
    bot.music_service = service
    bot.ai_service = ForbiddenExternalBoundary()
    bot.remote_consent_store = ForbiddenExternalBoundary()

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert reply.provider == "local-action-router"
    assert "YouTube公式検索（リンクのみ）" in reply.text
    assert service.search_calls == 1
    assert service.join_calls == 0
    assert service.play_calls == 0


@pytest.mark.parametrize("explicit_none", (False, True))
@pytest.mark.asyncio
async def test_music_request_link_only_does_not_require_music_service(
    monkeypatch: pytest.MonkeyPatch,
    explicit_none: bool,
) -> None:
    voice = VoiceChannel()
    bot, message, _ = _message("混沌ブギをながして", voice_channel=voice)
    if explicit_none:
        bot.music_service = None
    context_calls = 0
    url_calls: list[str] = []

    def context_spy(_: Any) -> Any:
        nonlocal context_calls
        context_calls += 1
        raise AssertionError("link-only must not require local music context")

    monkeypatch.setattr("yonerai_discord.modules.ai.action_router._music_context", context_spy)
    monkeypatch.setattr(
        "yonerai_discord.modules.ai.action_router.youtube_search_url",
        lambda query: url_calls.append(query) or "https://www.youtube.com/results?search_query=%E6%B7%B7%E6%B2%8C",
    )

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert "YouTube公式検索（リンクのみ）" in reply.text
    assert "失敗" not in reply.text
    assert context_calls == 0
    assert url_calls == ["混沌ブギ"]
    assert voice.connect_calls == 0


@pytest.mark.parametrize("reason", ("library-not-configured", "library-empty", "ffmpeg-unavailable"))
@pytest.mark.asyncio
async def test_music_request_uses_link_only_when_local_playback_runtime_is_unavailable(reason: str) -> None:
    class UnavailableMusicService(MusicService):
        available = False

        def __init__(self) -> None:
            super().__init__(())
            self.reason = reason

        async def search(self, *_: Any, **__: Any) -> tuple[Track, ...]:
            raise AssertionError("unavailable playback service must not be searched")

    bot, message, _ = _message("混沌ブギをながして")
    service = UnavailableMusicService()
    bot.music_service = service

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert "YouTube公式検索（リンクのみ）" in reply.text
    assert "[1](https://www.youtube.com/results?search_query=" in reply.text
    assert service.search_calls == 0
    assert service.join_calls == 0
    assert service.play_calls == 0


@pytest.mark.asyncio
async def test_music_request_skips_local_metadata_when_search_capability_is_off() -> None:
    search_capability = COMMAND_CAPABILITIES["music search"]
    guard = CapabilityGuard(states={search_capability: False})
    bot, message, _ = _message("混沌ブギをながして", guard=guard)
    service = MusicService((Track("混沌ブギ", Path("library/song.mp3"), USER_ID),))
    bot.music_service = service

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "YouTube公式検索（リンクのみ）" in reply.text
    assert service.search_calls == 0
    assert service.join_calls == 0
    assert service.play_calls == 0


@pytest.mark.asyncio
async def test_music_request_discards_exact_local_result_when_search_capability_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingMusicService(MusicService):
        def __init__(self, tracks: tuple[Track, ...]) -> None:
            super().__init__(tracks)
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def search(self, *_: Any, **__: Any) -> tuple[Track, ...]:
            self.search_calls += 1
            self.started.set()
            await self.release.wait()
            return self.tracks

    search_capability = COMMAND_CAPABILITIES["music search"]
    states: dict[str, bool] = {}
    guard = CapabilityGuard(states=states)
    voice = VoiceChannel()
    bot, message, _ = _message("混沌をながして", guard=guard, voice_channel=voice)
    hidden_title = "Hidden Local Candidate"
    service = BlockingMusicService((Track(hidden_title, Path("library/song.mp3"), USER_ID),))
    bot.music_service = service
    url_calls: list[str] = []
    monkeypatch.setattr(
        "yonerai_discord.modules.ai.action_router.youtube_search_url",
        lambda query: url_calls.append(query) or "https://www.youtube.com/results?search_query=%E6%B7%B7%E6%B2%8C",
    )
    task = asyncio.create_task(NaturalActionRouter(bot)(message, _request()))
    await asyncio.wait_for(service.started.wait(), timeout=1.0)
    states[search_capability] = False
    service.release.set()

    reply = await task

    assert reply is not None and "YouTube公式検索（リンクのみ）" in reply.text
    assert hidden_title not in reply.text
    assert url_calls == ["混沌"]
    assert voice.connect_calls == 0
    assert service.join_calls == 0
    assert service.play_calls == 0


@pytest.mark.asyncio
async def test_music_request_does_not_fallback_to_link_when_local_play_capability_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    play_capability = COMMAND_CAPABILITIES["music play"]
    guard = CapabilityGuard(states={play_capability: False})
    voice = VoiceChannel()
    bot, message, _ = _message("混沌ブギをながして", guard=guard, voice_channel=voice)
    service = MusicService((Track("混沌ブギ", Path("library/song.mp3"), USER_ID),))
    bot.music_service = service
    url_calls: list[str] = []
    monkeypatch.setattr(
        "yonerai_discord.modules.ai.action_router.youtube_search_url",
        lambda query: url_calls.append(query) or "https://www.youtube.com/results?search_query=unexpected",
    )

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "接続しませんでした" in reply.text
    assert service.search_calls == 1
    assert url_calls == []
    assert voice.connect_calls == 0
    assert service.join_calls == 0
    assert service.play_calls == 0


@pytest.mark.asyncio
async def test_music_request_primary_link_capability_off_stops_before_service_or_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_capability = COMMAND_CAPABILITIES["music search-youtube"]
    guard = CapabilityGuard(states={primary_capability: False})
    bot, message, _ = _message("混沌ブギをながして", guard=guard)
    service = MusicService(())
    bot.music_service = service
    url_calls: list[str] = []
    monkeypatch.setattr(
        "yonerai_discord.modules.ai.action_router.youtube_search_url",
        lambda query: url_calls.append(query) or "https://www.youtube.com/results?search_query=unexpected",
    )

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "利用できません" in reply.text
    assert service.search_calls == 0
    assert url_calls == []
    assert service.join_calls == 0
    assert service.play_calls == 0


@pytest.mark.asyncio
async def test_music_play_never_guesses_between_multiple_matches() -> None:
    voice = VoiceChannel()
    bot, message, _ = _message("混沌を流して", voice_channel=voice)
    service = MusicService(
        (
            Track("混沌ブギ", Path("library/a.mp3"), USER_ID),
            Track("混沌ダンス", Path("library/b.mp3"), USER_ID),
        )
    )
    bot.music_service = service

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "複数の候補" in reply.text
    assert voice.connect_calls == 0
    assert service.join_calls == 0
    assert service.play_calls == 0


@pytest.mark.parametrize(
    "revoked_capability",
    (COMMAND_CAPABILITIES["music search"], EVENT_CAPABILITIES["ai_mention_message"]),
)
@pytest.mark.asyncio
async def test_music_multiple_candidates_recheck_capabilities_before_exposing_titles(
    revoked_capability: str,
) -> None:
    class BlockingMusicService(MusicService):
        def __init__(self, tracks: tuple[Track, ...]) -> None:
            super().__init__(tracks)
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def search(self, *_: Any, **__: Any) -> tuple[Track, ...]:
            self.search_calls += 1
            self.started.set()
            await self.release.wait()
            return self.tracks

    titles = ("秘密候補A", "秘密候補B")
    tracks = tuple(Track(title, Path(f"library/{index}.mp3"), USER_ID) for index, title in enumerate(titles))
    states: dict[str, bool] = {}
    guard = CapabilityGuard(states=states)
    bot, message, _ = _message("秘密候補をながして", guard=guard)
    service = BlockingMusicService(tracks)
    bot.music_service = service
    task = asyncio.create_task(NaturalActionRouter(bot)(message, _request()))
    await asyncio.wait_for(service.started.wait(), timeout=1.0)
    states[revoked_capability] = False
    service.release.set()

    reply = await task

    assert reply is not None
    if revoked_capability == COMMAND_CAPABILITIES["music search"]:
        assert "YouTube公式検索（リンクのみ）" in reply.text
    else:
        assert "リンクを生成しませんでした" in reply.text
    assert all(title not in reply.text for title in titles)
    assert service.join_calls == 0
    assert service.play_calls == 0


@pytest.mark.asyncio
async def test_music_multiple_candidates_recheck_fresh_member_rbac_before_exposing_titles() -> None:
    class BlockingMusicService(MusicService):
        def __init__(self, tracks: tuple[Track, ...]) -> None:
            super().__init__(tracks)
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def search(self, *_: Any, **__: Any) -> tuple[Track, ...]:
            self.search_calls += 1
            self.started.set()
            await self.release.wait()
            return self.tracks

    titles = ("秘密候補A", "秘密候補B")
    tracks = tuple(Track(title, Path(f"library/{index}.mp3"), USER_ID) for index, title in enumerate(titles))
    search_capability = COMMAND_CAPABILITIES["music search"]
    guard = CapabilityGuard(admin_only=frozenset({search_capability}))
    bot, message, _ = _message("秘密候補をながして", guard=guard, owner=True)
    service = BlockingMusicService(tracks)
    bot.music_service = service
    task = asyncio.create_task(NaturalActionRouter(bot)(message, _request()))
    await asyncio.wait_for(service.started.wait(), timeout=1.0)
    message.author.guild_permissions.administrator = False
    message.author.guild_permissions.manage_guild = False
    service.release.set()

    reply = await task

    assert reply is not None and "YouTube公式検索（リンクのみ）" in reply.text
    assert all(title not in reply.text for title in titles)
    assert service.join_calls == 0
    assert service.play_calls == 0


@pytest.mark.asyncio
async def test_music_multiple_candidates_recheck_search_immediately_before_exposing_titles() -> None:
    class RejectCandidateDisplayGuard(CapabilityGuard):
        def __init__(self) -> None:
            super().__init__()
            self.search_checks = 0

        def currently_allowed(self, capability_id: str, **kwargs: Any) -> bool:
            if capability_id == COMMAND_CAPABILITIES["music search"]:
                self.search_checks += 1
                if self.search_checks >= 3:
                    return False
            return super().currently_allowed(capability_id, **kwargs)

    titles = ("秘密候補A", "秘密候補B")
    tracks = tuple(Track(title, Path(f"library/{index}.mp3"), USER_ID) for index, title in enumerate(titles))
    guard = RejectCandidateDisplayGuard()
    bot, message, _ = _message("秘密候補をながして", guard=guard)
    service = MusicService(tracks)
    bot.music_service = service

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "候補を表示しませんでした" in reply.text
    assert all(title not in reply.text for title in titles)
    assert service.join_calls == 0
    assert service.play_calls == 0


@pytest.mark.parametrize(
    "revoked_capability",
    (COMMAND_CAPABILITIES["music search-youtube"], EVENT_CAPABILITIES["ai_mention_message"]),
)
@pytest.mark.asyncio
async def test_music_link_rechecks_fresh_capabilities_before_building_url(
    monkeypatch: pytest.MonkeyPatch,
    revoked_capability: str,
) -> None:
    class BlockingMusicService(MusicService):
        def __init__(self) -> None:
            super().__init__(())
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def search(self, *_: Any, **__: Any) -> tuple[Track, ...]:
            self.search_calls += 1
            self.started.set()
            await self.release.wait()
            return ()

    url_calls: list[str] = []

    def url_spy(query: str) -> str:
        url_calls.append(query)
        return "https://www.youtube.com/results?search_query=unexpected"

    states: dict[str, bool] = {}
    guard = CapabilityGuard(states=states)
    voice = VoiceChannel()
    bot, message, _ = _message("混沌ブギをながして", guard=guard, voice_channel=voice)
    service = BlockingMusicService()
    bot.music_service = service
    monkeypatch.setattr("yonerai_discord.modules.ai.action_router.youtube_search_url", url_spy)
    task = asyncio.create_task(NaturalActionRouter(bot)(message, _request()))
    await asyncio.wait_for(service.started.wait(), timeout=1.0)
    states[revoked_capability] = False
    service.release.set()

    reply = await task

    assert reply is not None and "リンクを生成しませんでした" in reply.text
    assert url_calls == []
    assert voice.connect_calls == 0
    assert service.join_calls == 0
    assert service.play_calls == 0


@pytest.mark.asyncio
async def test_music_link_rechecks_fresh_member_rbac_before_building_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingMusicService(MusicService):
        def __init__(self) -> None:
            super().__init__(())
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def search(self, *_: Any, **__: Any) -> tuple[Track, ...]:
            self.search_calls += 1
            self.started.set()
            await self.release.wait()
            return ()

    target_capability = COMMAND_CAPABILITIES["music search-youtube"]
    guard = CapabilityGuard(admin_only=frozenset({target_capability}))
    voice = VoiceChannel()
    bot, message, _ = _message(
        "混沌ブギをながして",
        guard=guard,
        owner=True,
        voice_channel=voice,
    )
    service = BlockingMusicService()
    bot.music_service = service
    url_calls: list[str] = []
    monkeypatch.setattr(
        "yonerai_discord.modules.ai.action_router.youtube_search_url",
        lambda query: url_calls.append(query) or "https://www.youtube.com/results?search_query=unexpected",
    )
    task = asyncio.create_task(NaturalActionRouter(bot)(message, _request()))
    await asyncio.wait_for(service.started.wait(), timeout=1.0)
    message.author.guild_permissions.administrator = False
    message.author.guild_permissions.manage_guild = False
    service.release.set()

    reply = await task

    assert reply is not None and "リンクを生成しませんでした" in reply.text
    assert url_calls == []
    assert service.join_calls == 0
    assert service.play_calls == 0


@pytest.mark.asyncio
async def test_music_request_opt_in_denial_stops_before_search_or_url_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mention_capability = EVENT_CAPABILITIES["ai_mention_message"]
    guard = CapabilityGuard(states={mention_capability: False})
    bot, message, _ = _message("混沌ブギをながして", guard=guard)
    service = MusicService(())
    bot.music_service = service
    url_calls: list[str] = []
    monkeypatch.setattr(
        "yonerai_discord.modules.ai.action_router.youtube_search_url",
        lambda query: url_calls.append(query) or "https://www.youtube.com/results?search_query=unexpected",
    )

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "操作を実行しませんでした" in reply.text
    assert service.search_calls == 0
    assert url_calls == []
    assert service.play_calls == 0


@pytest.mark.asyncio
async def test_music_play_rechecks_policy_after_blocking_search_before_voice_connect() -> None:
    class BlockingMusicService(MusicService):
        def __init__(self, tracks: tuple[Track, ...]) -> None:
            super().__init__(tracks)
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def search(self, *_: Any, **__: Any) -> tuple[Track, ...]:
            self.started.set()
            await self.release.wait()
            return self.tracks

    voice = VoiceChannel()
    bot, message, guard = _message("混沌ブギを流して", voice_channel=voice)
    track = Track("混沌ブギ", Path("library/song.mp3"), USER_ID)
    service = BlockingMusicService((track,))
    bot.music_service = service
    task = asyncio.create_task(NaturalActionRouter(bot)(message, _request()))
    await asyncio.wait_for(service.started.wait(), timeout=1.0)
    guard.allowed = False
    service.release.set()

    reply = await task

    assert reply is not None and "リンクを生成しませんでした" in reply.text
    assert voice.connect_calls == 0
    assert service.join_calls == 0
    assert service.play_calls == 0


@pytest.mark.asyncio
async def test_music_play_rolls_back_connection_when_policy_changes_during_connect() -> None:
    class BlockingVoiceChannel(VoiceChannel):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def connect(self, **_: Any) -> Any:
            self.connect_calls += 1
            self.started.set()
            await self.release.wait()
            return self.voice_client

    voice = BlockingVoiceChannel()
    bot, message, guard = _message("混沌ブギを流して", voice_channel=voice)
    track = Track("混沌ブギ", Path("library/song.mp3"), USER_ID)
    service = MusicService((track,))
    bot.music_service = service
    task = asyncio.create_task(NaturalActionRouter(bot)(message, _request()))
    await asyncio.wait_for(voice.started.wait(), timeout=1.0)
    guard.allowed = False
    voice.release.set()

    reply = await task

    assert reply is not None and "接続を取り消しました" in reply.text
    assert voice.connect_calls == 1
    assert voice.disconnect_calls == 1
    assert service.close_calls == 1
    assert service.join_calls == 0
    assert service.play_calls == 0


@pytest.mark.asyncio
async def test_music_play_rolls_back_new_voice_session_on_failure() -> None:
    voice = VoiceChannel()
    bot, message, _ = _message("混沌ブギを流して", voice_channel=voice)
    track = Track("混沌ブギ", Path("library/song.mp3"), USER_ID)

    class FailingMusicService(MusicService):
        async def play(self, *_: Any, **__: Any) -> tuple[Track, int]:
            raise MusicSessionError("failed")

    service = FailingMusicService((track,))
    bot.music_service = service

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "実行できません" in reply.text
    assert service.close_calls == 1
    assert voice.disconnect_calls == 1


@pytest.mark.asyncio
async def test_natural_music_control_rechecks_fresh_policy_before_player_mutation() -> None:
    class BlockingControlService:
        available = True

        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.mutated = False

        async def pause(self, *_: Any, commit_check: Any = None) -> None:
            self.started.set()
            await self.release.wait()
            if commit_check is None or await commit_check() is None:
                raise MusicAuthorizationError("capability policy changed")
            self.mutated = True

    voice = VoiceChannel()
    bot, message, guard = _message("音楽を一時停止して", voice_channel=voice)
    service = BlockingControlService()
    bot.music_service = service
    task = asyncio.create_task(NaturalActionRouter(bot)(message, _request()))
    await asyncio.wait_for(service.started.wait(), timeout=1.0)
    guard.allowed = False
    service.release.set()

    reply = await task

    assert reply is not None and "操作できます" in reply.text
    assert not service.mutated


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("音量を５０%にして", ("volume", 0.5)),
        ("読み上げ音量を５０%にして", ("speech_volume", 0.5)),
        ("再生位置を６０秒にして", ("seek", 60)),
        ("ループをこの曲にして", ("loop", LoopMode.TRACK)),
        ("キューをシャッフルして", ("shuffle", None)),
        ("キューの3番を削除して", ("remove", 3)),
    ),
)
@pytest.mark.asyncio
async def test_music_control_routes_use_exact_existing_service_sinks(text: str, expected: tuple[str, Any]) -> None:
    voice = VoiceChannel()
    tracks = tuple(Track(f"曲{index}", Path(f"library/{index}.mp3"), USER_ID) for index in range(1, 4))
    bot, message, _ = _message(text, voice_channel=voice)
    service = MusicService(tracks)
    service.channel_id = voice.id
    bot.music_service = service
    bot.ai_service = SimpleNamespace(__getattr__=lambda *_: (_ for _ in ()).throw(AssertionError("AI must not run")))

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and reply.provider == "local-action-router"
    assert service.control_calls == [expected]
    assert service.join_calls == service.play_calls == len(service.speech_calls) == 0


@pytest.mark.parametrize(
    ("text", "enabled"),
    (
        ("ローカルラジオを開始して", True),
        ("ローカルラジオを停止して", False),
    ),
)
@pytest.mark.asyncio
async def test_local_radio_mention_uses_exact_capability_and_local_service_only(
    text: str,
    enabled: bool,
) -> None:
    voice = VoiceChannel()
    guard = CapabilityGuard()
    bot, message, _ = _message(text, voice_channel=voice, guard=guard)
    service = MusicService((Track("曲", Path("library/song.mp3"), USER_ID),))
    service.channel_id = voice.id
    bot.music_service = service
    bot.ai_service = SimpleNamespace(__getattr__=lambda *_: (_ for _ in ()).throw(AssertionError("AI must not run")))

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and reply.provider == "local-action-router"
    assert service.radio_calls == [enabled]
    assert service.control_calls == [("radio", enabled)]
    assert guard.calls[0]["capability_id"] == COMMAND_CAPABILITIES["music radio"]
    assert service.join_calls == service.play_calls == service.search_calls == 0


@pytest.mark.asyncio
async def test_invalid_explicit_local_radio_is_consumed_without_ai_or_music_mutation() -> None:
    voice = VoiceChannel()
    bot, message, _ = _message("ローカルラジオを自動で適当にして", voice_channel=voice)
    service = MusicService(())
    service.channel_id = voice.id
    bot.music_service = service
    bot.ai_service = SimpleNamespace(__getattr__=lambda *_: (_ for _ in ()).throw(AssertionError("AI must not run")))

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "開始して" in reply.text and "停止して" in reply.text
    assert service.radio_calls == []
    assert service.control_calls == []


@pytest.mark.asyncio
async def test_local_radio_rechecks_capability_at_service_sink() -> None:
    class BlockingRadioService(MusicService):
        def __init__(self) -> None:
            super().__init__(())
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def set_local_radio(
            self,
            guild_id: int,
            actor: Any,
            enabled: bool,
            *,
            commit_check: Any = None,
        ) -> bool:
            self.started.set()
            await self.release.wait()
            return await super().set_local_radio(
                guild_id,
                actor,
                enabled,
                commit_check=commit_check,
            )

    voice = VoiceChannel()
    capability = COMMAND_CAPABILITIES["music radio"]
    guard = CapabilityGuard(
        states={
            capability: True,
            EVENT_CAPABILITIES["ai_mention_message"]: True,
        }
    )
    bot, message, _ = _message("ローカルラジオを開始して", voice_channel=voice, guard=guard)
    service = BlockingRadioService()
    service.channel_id = voice.id
    bot.music_service = service
    task = asyncio.create_task(NaturalActionRouter(bot)(message, _request()))
    await asyncio.wait_for(service.started.wait(), timeout=1.0)
    guard.states[capability] = False
    service.release.set()

    reply = await task

    assert reply is not None and "操作できます" in reply.text
    assert service.radio_calls == []
    assert service.control_calls == []


@pytest.mark.parametrize(
    "text",
    (
        "音量を201%にして",
        "読み上げ音量を201%にして",
        "再生位置を86401秒にして",
        "ループを無限にして",
        "キューをシャッフルする",
        "キューの0番を削除して",
    ),
)
@pytest.mark.asyncio
async def test_invalid_explicit_music_controls_are_consumed_locally(text: str) -> None:
    bot, message, _ = _message(text)
    service = MusicService(())
    bot.music_service = service
    bot.ai_service = SimpleNamespace(__getattr__=lambda *_: (_ for _ in ()).throw(AssertionError("AI must not run")))

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "入力が不正" in reply.text
    assert service.control_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate", ("capability", "service"))
async def test_music_volume_rechecks_capability_and_service_identity_at_sink(invalidate: str) -> None:
    class BlockingMusicService(MusicService):
        def __init__(self) -> None:
            super().__init__(())
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def set_volume(self, guild_id: int, actor: Any, value: float, *, commit_check: Any = None) -> None:
            self.started.set()
            await self.release.wait()
            await super().set_volume(guild_id, actor, value, commit_check=commit_check)

    voice = VoiceChannel()
    volume_capability = COMMAND_CAPABILITIES["music volume"]
    states = {volume_capability: True, EVENT_CAPABILITIES["ai_mention_message"]: True}
    guard = CapabilityGuard(states=states)
    bot, message, _ = _message("音量を50%にして", guard=guard, voice_channel=voice)
    service = BlockingMusicService()
    service.channel_id = voice.id
    bot.music_service = service
    task = asyncio.create_task(NaturalActionRouter(bot)(message, _request()))
    await asyncio.wait_for(service.started.wait(), timeout=1.0)
    if invalidate == "capability":
        states[volume_capability] = False
    else:
        bot.music_service = MusicService(())
    service.release.set()

    reply = await task

    assert reply is not None and "操作できます" in reply.text
    assert service.control_calls == []


@pytest.mark.parametrize(
    ("text", "expected_capability", "expected_text"),
    (
        ("２ｄ６＋１を振って", "tools dice", "[3, 3] +1 = **7**"),
        ("１から１００でランダムに選んで", "tools random", "42"),
        ("候補から選んで: @everyone, ラーメン", "tools choose", "選択結果: @\u200beveryone"),
        ("２０２６-０７-２５Ｔ２０:００＋０９:００をDiscord時刻にして", "tools timestamp", "<t:1784977200:F>"),
    ),
)
@pytest.mark.asyncio
async def test_utility_actions_route_to_existing_capabilities_without_external_io(
    text: str,
    expected_capability: str,
    expected_text: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "yonerai_discord.modules.ai.action_router.secrets.randbelow", lambda _: 2 if "振って" in text else 41
    )
    monkeypatch.setattr("yonerai_discord.modules.ai.action_router.secrets.choice", lambda values: values[0])
    bot, message, guard = _message(text)
    bot.ai_service = SimpleNamespace(__getattr__=lambda *_: (_ for _ in ()).throw(AssertionError("AI must not run")))

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and reply.text == expected_text
    assert guard.calls[0]["capability_id"] == COMMAND_CAPABILITIES[expected_capability]


@pytest.mark.parametrize(
    "text",
    (
        "Discord ID 175928847299117063 の作成日時を教えて",
        "Snowflake １７５９２８８４７２９９１１７０６３ を時刻にして",
    ),
)
@pytest.mark.asyncio
async def test_snowflake_action_uses_existing_utility_domain_with_two_stage_capability_checks(
    text: str,
) -> None:
    capability = COMMAND_CAPABILITIES["tools snowflake"]
    mention_capability = EVENT_CAPABILITIES["ai_mention_message"]
    guard = CapabilityGuard(states={capability: True, mention_capability: True})
    bot, message, _ = _message(text, guard=guard)
    bot.ai_service = SimpleNamespace(__getattr__=lambda *_: (_ for _ in ()).throw(AssertionError("AI must not run")))

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert reply.text == "作成日時 (UTC): 2016-04-30T11:18:25.796+00:00\n<t:1462015105:F>"
    assert guard.calls[0]["capability_id"] == capability
    assert [call["capability_id"] for call in guard.current_calls] == [capability, mention_capability]


@pytest.mark.parametrize(
    ("text", "command_path", "expected_text"),
    (
        (
            "SHA-256を計算: abc",
            "tools sha256",
            "SHA-256: `ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad`",
        ),
        ("カラー ＃５８６５Ｆ２を確認して", "tools color", "HEXカラー: `#5865F2`"),
    ),
)
@pytest.mark.asyncio
async def test_sha256_and_color_actions_use_existing_utility_domain_with_two_stage_capability_checks(
    text: str,
    command_path: str,
    expected_text: str,
) -> None:
    capability = COMMAND_CAPABILITIES[command_path]
    mention_capability = EVENT_CAPABILITIES["ai_mention_message"]
    guard = CapabilityGuard(states={capability: True, mention_capability: True})
    bot, message, _ = _message(text, guard=guard)
    bot.ai_service = SimpleNamespace(__getattr__=lambda *_: (_ for _ in ()).throw(AssertionError("AI must not run")))

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and reply.text == expected_text
    assert guard.calls[0]["capability_id"] == capability
    assert [call["capability_id"] for call in guard.current_calls] == [capability, mention_capability]


@pytest.mark.parametrize("text", ("SHA-256を計算:", "カラー #ABCを確認して", "カラー #ABC を確認して"))
@pytest.mark.asyncio
async def test_invalid_explicit_sha256_and_color_actions_are_consumed_locally(text: str) -> None:
    bot, message, _ = _message(text)
    bot.ai_service = SimpleNamespace(__getattr__=lambda *_: (_ for _ in ()).throw(AssertionError("AI must not run")))

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and reply.provider == "local-action-router"
    assert reply.text


@pytest.mark.parametrize("text", ("SHA-256を計算: " + "x" * 513, "カラーについて教えて"))
@pytest.mark.asyncio
async def test_long_or_unrelated_sha256_and_color_text_falls_through_to_normal_conversation(text: str) -> None:
    bot, message, _ = _message(text)

    assert await NaturalActionRouter(bot)(message, _request()) is None


@pytest.mark.parametrize(
    ("text", "command_path"),
    (("SHA-256を計算: abc", "tools sha256"), ("カラー #5865F2を確認して", "tools color")),
)
@pytest.mark.asyncio
async def test_sha256_and_color_actions_fail_closed_when_capability_is_revoked_after_entry(
    text: str,
    command_path: str,
) -> None:
    capability = COMMAND_CAPABILITIES[command_path]
    mention_capability = EVENT_CAPABILITIES["ai_mention_message"]

    class RevokingGuard(CapabilityGuard):
        def event_allowed(self, capability_id: str, **kwargs: Any) -> bool:
            allowed = super().event_allowed(capability_id, **kwargs)
            if capability_id == capability:
                self.states[capability] = False
            return allowed

    guard = RevokingGuard(states={capability: True, mention_capability: True})
    bot, message, _ = _message(text, guard=guard)
    bot.ai_service = SimpleNamespace(__getattr__=lambda *_: (_ for _ in ()).throw(AssertionError("AI must not run")))

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "待機中に権限または機能設定が変更されたため" in reply.text
    assert [call["capability_id"] for call in guard.current_calls] == [capability]


POLL_ID = "0123456789abcdef0123456789abcdef"


class PollResultsRepository:
    def __init__(
        self,
        *,
        guild_id: int = GUILD_ID,
        channel_id: int | None = CHANNEL_ID,
        status: str = "open",
        after_get: Any = None,
        fail: bool = False,
    ) -> None:
        self.poll = SimpleNamespace(
            id=POLL_ID,
            guild_id=guild_id,
            channel_id=channel_id,
            question="**好き** @everyone",
            options=("赤", "青"),
            status=SimpleNamespace(value=status),
        )
        self.results = (
            SimpleNamespace(option_index=0, option="赤", votes=2),
            SimpleNamespace(option_index=1, option="青", votes=1),
        )
        self.after_get = after_get
        self.fail = fail
        self.get_calls = 0
        self.result_calls = 0

    def get_poll(self, guild_id: int, poll_id: str) -> Any:
        self.get_calls += 1
        if self.fail:
            raise RuntimeError("private repository failure")
        assert guild_id == GUILD_ID and poll_id == POLL_ID
        if self.after_get is not None:
            self.after_get()
            self.after_get = None
        return self.poll

    def poll_results(self, guild_id: int, poll_id: str) -> tuple[Any, ...]:
        self.result_calls += 1
        assert guild_id == GUILD_ID and poll_id == POLL_ID
        return self.results


def _poll_message(
    text: str,
    *,
    repository: PollResultsRepository | None = None,
    guard: CapabilityGuard | None = None,
    view_channel: bool = True,
) -> tuple[Any, Any, CapabilityGuard, PollResultsRepository]:
    capability = COMMAND_CAPABILITIES["poll results"]
    mention_capability = EVENT_CAPABILITIES["ai_mention_message"]
    actual_guard = guard or CapabilityGuard(states={capability: True, mention_capability: True})
    bot, message, _ = _message(text, guard=actual_guard)
    actual_repository = repository or PollResultsRepository()
    plugin = SimpleNamespace(closing=False, bot=bot, repository=actual_repository)
    bot.community_plugin = plugin
    bot.community_repository = actual_repository
    message.channel.permissions_for = lambda _: SimpleNamespace(
        view_channel=view_channel,
        read_message_history=view_channel,
    )
    bot.ai_service = SimpleNamespace(__getattr__=lambda *_: (_ for _ in ()).throw(AssertionError("AI must not run")))
    return bot, message, actual_guard, actual_repository


@pytest.mark.parametrize(
    ("poll_text", "status_label"),
    ((POLL_ID, "受付中"), ("０１２３４５６７８９ａｂｃｄｅｆ０１２３４５６７８９ａｂｃｄｅｆ", "終了")),
)
@pytest.mark.asyncio
async def test_poll_results_use_existing_read_only_repository_with_safe_output(
    poll_text: str,
    status_label: str,
) -> None:
    repository = PollResultsRepository(status="closed" if status_label == "終了" else "open")
    bot, message, guard, _ = _poll_message(f"投票 {poll_text} の結果を見せて", repository=repository)

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and reply.provider == "local-action-router"
    assert status_label in reply.text and "1. 赤: 2票" in reply.text and "2. 青: 1票" in reply.text
    assert "\\*\\*好き\\*\\*" in reply.text
    assert "@everyone" not in reply.text
    assert len(reply.text) <= 1_900
    assert repository.get_calls == 2 and repository.result_calls == 1
    capability = COMMAND_CAPABILITIES["poll results"]
    assert guard.calls[0]["capability_id"] == capability
    assert capability in [call["capability_id"] for call in guard.current_calls]


@pytest.mark.parametrize("poll_id", ("ABCDEF0123456789ABCDEF0123456789", "abc", "abc def"))
@pytest.mark.asyncio
async def test_invalid_explicit_poll_results_are_consumed_without_repository_read(poll_id: str) -> None:
    bot, message, _, repository = _poll_message(f"投票 {poll_id} の結果を見せて")

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "32桁の小文字16進数" in reply.text
    assert repository.get_calls == repository.result_calls == 0


@pytest.mark.parametrize(
    ("guild_id", "channel_id"),
    ((GUILD_ID + 1, CHANNEL_ID), (GUILD_ID, CHANNEL_ID + 1), (GUILD_ID, None)),
)
@pytest.mark.asyncio
async def test_poll_results_do_not_disclose_other_scope_or_unbound_poll(guild_id: int, channel_id: int | None) -> None:
    repository = PollResultsRepository(guild_id=guild_id, channel_id=channel_id)
    bot, message, _, _ = _poll_message(f"投票 {POLL_ID} の結果を見せて", repository=repository)

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "安全に確認できないため" in reply.text
    assert "好き" not in reply.text and "赤" not in reply.text
    assert repository.result_calls == 0


@pytest.mark.asyncio
async def test_poll_results_recheck_after_repository_read_before_disclosure() -> None:
    capability = COMMAND_CAPABILITIES["poll results"]
    mention_capability = EVENT_CAPABILITIES["ai_mention_message"]
    states = {capability: True, mention_capability: True}
    guard = CapabilityGuard(states=states)
    repository = PollResultsRepository(after_get=lambda: states.__setitem__(capability, False))
    bot, message, _, _ = _poll_message(
        f"投票 {POLL_ID} の結果を見せて",
        repository=repository,
        guard=guard,
    )

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "安全に確認できないため" in reply.text
    assert "好き" not in reply.text and repository.result_calls == 0


@pytest.mark.parametrize("invalidate", ("native_permission", "plugin_identity"))
@pytest.mark.asyncio
async def test_poll_results_fail_closed_for_native_permission_or_plugin_replacement(invalidate: str) -> None:
    repository = PollResultsRepository()
    bot, message, _, _ = _poll_message(
        f"投票 {POLL_ID} の結果を見せて",
        repository=repository,
        view_channel=invalidate != "native_permission",
    )
    if invalidate == "plugin_identity":
        repository.after_get = lambda: setattr(bot, "community_plugin", SimpleNamespace())

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "安全に確認できないため" in reply.text
    assert "好き" not in reply.text


@pytest.mark.asyncio
async def test_poll_results_repository_error_returns_fixed_failure() -> None:
    bot, message, _, repository = _poll_message(
        f"投票 {POLL_ID} の結果を見せて",
        repository=PollResultsRepository(fail=True),
    )

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and reply.text == "投票結果を安全に取得できませんでした。"
    assert "private repository failure" not in reply.text
    assert repository.result_calls == 0


_DISCOVERY_COMMANDS = {
    "tools dice": "ダイスを振る",
    "tools random": "整数をランダム選択",
    "tools choose": "候補から選択",
    "tools timestamp": "Discord時刻へ変換",
    "tools snowflake": "Snowflake作成日時",
    "tools sha256": "SHA-256を計算",
    "tools color": "カラーを確認 @everyone",
}


def _discovery_message(
    text: str,
    *,
    readiness: dict[str, bool | None] | None = None,
    required_levels: dict[str, RbacLevel] | None = None,
    guard: CapabilityGuard | None = None,
    plugin_running: bool = True,
    native_allowed: bool = True,
) -> tuple[Any, Any, CapabilityGuard, Registry, InMemoryStateStore]:
    store = InMemoryStateStore()
    registry = Registry(store)
    registry.register_module(ModuleSpec("tools.utility"))
    configured_readiness = readiness or {path: True for path in _DISCOVERY_COMMANDS}
    for path, description in _DISCOVERY_COMMANDS.items():
        capability_id = COMMAND_CAPABILITIES[path]
        registry.register_capability(
            CapabilitySpec(
                capability_id,
                "tools.utility",
                name=description,
                required_level=(required_levels or {}).get(path, RbacLevel.EVERYONE),
            )
        )
        ready = configured_readiness.get(path)
        if ready is not None:
            registry.set_runtime_availability(capability_id, ready)

    help_capability = COMMAND_CAPABILITIES["help"]
    mention_capability = EVENT_CAPABILITIES["ai_mention_message"]
    actual_guard = guard or CapabilityGuard(states={help_capability: True, mention_capability: True})
    bot, message, _ = _message(text, guard=actual_guard)
    bot.require_registry = lambda: registry
    bot.plugins = SimpleNamespace(is_running=lambda _: plugin_running)
    children = tuple(SimpleNamespace(name=path.split(" ", 1)[1], commands=()) for path in _DISCOVERY_COMMANDS)
    bot.tree = SimpleNamespace(get_commands=lambda: (SimpleNamespace(name="tools", commands=children),))
    message.channel.permissions_for = lambda _: SimpleNamespace(
        view_channel=native_allowed,
        read_message_history=native_allowed,
    )
    bot.ai_service = SimpleNamespace(__getattr__=lambda *_: (_ for _ in ()).throw(AssertionError("AI must not run")))
    return bot, message, actual_guard, registry, store


@pytest.mark.asyncio
async def test_feature_list_reuses_canonical_discovery_with_bounded_output() -> None:
    bot, message, guard, _, _ = _discovery_message("使える機能を見せて")

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and reply.provider == "local-action-router"
    assert "現在の利用候補: 7件（先頭6件）" in reply.text
    assert "続きは検索語を絞るか" in reply.text
    assert "runtime readinessがtrue" in reply.text
    assert len(reply.text) <= 1_900
    assert "@everyone" not in reply.text
    assert guard.calls[0]["capability_id"] == COMMAND_CAPABILITIES["help"]


@pytest.mark.asyncio
async def test_feature_list_preserves_readiness_footer_when_canonical_entries_are_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, message, _, _, _ = _discovery_message("使える機能を見せて")
    entries = tuple(
        SimpleNamespace(path="long-command", module_id="module_" * 10, description="*" * 120) for _ in range(6)
    )
    monkeypatch.setattr(
        "yonerai_discord.modules.ai.action_router.DiscoveryService.search",
        lambda *_args, **_kwargs: SimpleNamespace(entries=entries, total_entries=6, total_pages=1),
    )

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and len(reply.text) <= 1_900
    assert reply.text.endswith("※runtime readinessがtrueの候補だけです。対象・Bot権限は実行時に再確認されます。")
    assert "続きは検索語を絞るか" in reply.text
    assert "先頭6件" not in reply.text


@pytest.mark.asyncio
async def test_feature_search_is_nfkc_exact_and_filters_by_canonical_description() -> None:
    bot, message, _, _, _ = _discovery_message("機能を　ダイス　で検索して")

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None
    assert "現在の利用候補: 1件" in reply.text
    assert "/tools dice" in reply.text
    assert "/tools random" not in reply.text


@pytest.mark.asyncio
async def test_feature_list_hides_false_and_unknown_runtime_readiness() -> None:
    readiness = {path: None for path in _DISCOVERY_COMMANDS}
    readiness["tools dice"] = True
    readiness["tools random"] = False
    bot, message, _, _, _ = _discovery_message("使える機能を見せて", readiness=readiness)

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "現在の利用候補: 1件" in reply.text
    assert "/tools dice" in reply.text
    assert "/tools random" not in reply.text
    assert "/tools choose" not in reply.text


@pytest.mark.parametrize("invalidate", ("module", "plugin"))
@pytest.mark.asyncio
async def test_feature_list_hides_disabled_module_or_stopped_plugin(invalidate: str) -> None:
    bot, message, _, _, store = _discovery_message(
        "使える機能を見せて",
        plugin_running=invalidate != "plugin",
    )
    if invalidate == "module":
        store.set_module_override("tools.utility", False, GUILD_ID)

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and reply.text == "現在の条件で一致する利用候補はありません。"


@pytest.mark.asyncio
async def test_feature_list_requires_current_channel_native_permission() -> None:
    bot, message, _, _, _ = _discovery_message("使える機能を見せて", native_allowed=False)

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "機能一覧を表示する現在の権限" in reply.text
    assert "/tools" not in reply.text


@pytest.mark.asyncio
async def test_feature_list_uses_fresh_member_level_for_admin_candidate() -> None:
    bot, message, _, _, _ = _discovery_message(
        "機能を カラー で検索して",
        required_levels={"tools color": RbacLevel.GUILD_ADMIN},
    )

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "/tools color" in reply.text


@pytest.mark.asyncio
async def test_feature_list_reprojects_after_fresh_admin_is_downgraded() -> None:
    help_capability = COMMAND_CAPABILITIES["help"]
    mention_capability = EVENT_CAPABILITIES["ai_mention_message"]

    class DowngradingGuard(CapabilityGuard):
        def __init__(self) -> None:
            super().__init__(states={help_capability: True, mention_capability: True})
            self.fresh_calls = 0

        async def evaluate_fresh_member(self, capability_id: str, *, guild: Any, member: Any) -> Any:
            assert guild.id == GUILD_ID and member.id == USER_ID
            level = RbacLevel.GUILD_ADMIN if self.fresh_calls < 2 else RbacLevel.EVERYONE
            self.fresh_calls += 1
            return SimpleNamespace(allowed=True, actor_level=level)

    guard = DowngradingGuard()
    bot, message, _, _, _ = _discovery_message(
        "使える機能を見せて",
        required_levels={"tools color": RbacLevel.GUILD_ADMIN},
        guard=guard,
    )

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "/tools dice" in reply.text
    assert "/tools color" not in reply.text
    assert guard.fresh_calls == 4


@pytest.mark.parametrize("failure", ("fetch", "identity"))
@pytest.mark.asyncio
async def test_feature_list_fails_closed_when_fresh_member_cannot_be_verified(failure: str) -> None:
    bot, message, _, _, _ = _discovery_message("使える機能を見せて")

    async def fetch_member(_: int) -> Any:
        if failure == "fetch":
            raise LookupError("member unavailable")
        return SimpleNamespace(id=USER_ID + 1)

    message.guild.fetch_member = fetch_member

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "機能一覧を表示する現在の権限" in reply.text
    assert "/tools" not in reply.text


@pytest.mark.asyncio
async def test_feature_list_hides_results_when_capability_is_revoked_after_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    help_capability = COMMAND_CAPABILITIES["help"]
    mention_capability = EVENT_CAPABILITIES["ai_mention_message"]
    states = {help_capability: True, mention_capability: True}
    guard = CapabilityGuard(states=states)
    bot, message, _, _, _ = _discovery_message("使える機能を見せて", guard=guard)
    original_search = DiscoveryService.search
    calls = 0

    def search(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        result = original_search(*args, **kwargs)
        calls += 1
        if calls == 1:
            states[help_capability] = False
        return result

    monkeypatch.setattr("yonerai_discord.modules.ai.action_router.DiscoveryService.search", search)

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "機能一覧を表示する現在の権限" in reply.text
    assert "/tools" not in reply.text
    assert calls == 1


@pytest.mark.asyncio
async def test_feature_list_fails_closed_when_help_capability_is_revoked_after_entry() -> None:
    help_capability = COMMAND_CAPABILITIES["help"]
    mention_capability = EVENT_CAPABILITIES["ai_mention_message"]

    class RevokingGuard(CapabilityGuard):
        def event_allowed(self, capability_id: str, **kwargs: Any) -> bool:
            allowed = super().event_allowed(capability_id, **kwargs)
            if capability_id == help_capability:
                self.states[help_capability] = False
            return allowed

    guard = RevokingGuard(states={help_capability: True, mention_capability: True})
    bot, message, _, _, _ = _discovery_message("使える機能を見せて", guard=guard)

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "待機中に権限または機能設定が変更されたため" in reply.text
    assert "/tools" not in reply.text


@pytest.mark.asyncio
async def test_invalid_explicit_feature_search_is_consumed_without_discovery_or_ai() -> None:
    bot, message, _, _, _ = _discovery_message(f"機能を {'x' * 81} で検索して")
    calls = 0

    def require_registry() -> Registry:
        nonlocal calls
        calls += 1
        raise AssertionError("discovery must not run")

    bot.require_registry = require_registry

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "検索語は1〜80文字" in reply.text
    assert calls == 0


@pytest.mark.asyncio
async def test_feature_list_fails_closed_without_canonical_registry() -> None:
    bot, message, _, _, _ = _discovery_message("使える機能を見せて")
    bot.require_registry = lambda: None

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and reply.text == "機能一覧を安全に確認できないため、表示しませんでした。"


@pytest.mark.asyncio
async def test_community_plugin_publishes_only_after_start_and_unpublishes_before_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Repository:
        def __init__(self, _: Path) -> None:
            self.opened = False
            self.closed = False

        def open(self) -> None:
            self.opened = True

        def close(self) -> None:
            self.closed = True

        def open_polls(self) -> tuple[Any, ...]:
            return ()

        def all_selfrole_sets(self) -> tuple[Any, ...]:
            return ()

    repository: Repository | None = None

    def repository_factory(path: Path) -> Repository:
        nonlocal repository
        repository = Repository(path)
        return repository

    monkeypatch.setattr("yonerai_discord.modules.community.plugin.CommunityRepository", repository_factory)
    for name in ("TicketGroup", "PollGroup", "SuggestGroup", "SelfRoleGroup"):
        monkeypatch.setattr(f"yonerai_discord.modules.community.plugin.{name}", lambda *_: SimpleNamespace())
    removed: list[str] = []
    bot = SimpleNamespace(
        settings=SimpleNamespace(database_path="unused.sqlite3"),
        tree=SimpleNamespace(add_command=lambda _: None, remove_command=lambda name, **_: removed.append(name)),
        add_view=lambda *_args, **_kwargs: None,
    )
    plugin = CommunityPlugin()

    await plugin.start(bot)

    assert repository is not None and repository.opened
    assert bot.community_plugin is plugin and bot.community_repository is repository
    assert plugin.closing is False

    await plugin.begin_close()

    assert not hasattr(bot, "community_plugin") and not hasattr(bot, "community_repository")
    assert plugin.closing is True and repository.closed is False

    await plugin.stop()

    assert repository.closed is True
    assert removed == list(plugin.command_names)


@pytest.mark.parametrize(
    "text",
    (
        "2d1を振って",
        "100から1でランダムに選んで",
        "1_000から1001でランダムに選んで",
        "١から100でランダムに選んで",
        "候補から選んで: カレー",
        "2026-07-25T20:00をDiscord時刻にして",
    ),
)
@pytest.mark.asyncio
async def test_invalid_explicit_utility_actions_are_consumed_locally(text: str) -> None:
    bot, message, _ = _message(text)
    bot.ai_service = SimpleNamespace(__getattr__=lambda *_: (_ for _ in ()).throw(AssertionError("AI must not run")))

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and reply.provider == "local-action-router"
    assert reply.text


@pytest.mark.parametrize(
    "text",
    (
        "Discord ID 1759288472991170 の作成日時を教えて",
        "Snowflake 175928847299117063000 を時刻にして",
        "Discord ID 99999999999999999999 の作成日時を教えて",
    ),
)
@pytest.mark.asyncio
async def test_invalid_explicit_snowflake_actions_are_consumed_locally(text: str) -> None:
    bot, message, _ = _message(text)
    bot.ai_service = SimpleNamespace(__getattr__=lambda *_: (_ for _ in ()).throw(AssertionError("AI must not run")))

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and reply.provider == "local-action-router"
    assert "17〜20桁" in reply.text


@pytest.mark.parametrize(
    "text",
    (
        "Snowflakeについて教えて",
        "Snowflake " + "x" * 65 + " を時刻にして",
    ),
)
@pytest.mark.asyncio
async def test_unrelated_or_long_snowflake_text_falls_through_to_normal_conversation(text: str) -> None:
    bot, message, _ = _message(text)

    assert await NaturalActionRouter(bot)(message, _request()) is None


class ForbiddenSiteService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_sites(self, *_: Any, **__: Any) -> tuple[Any, ...]:
        self.calls.append("list")
        raise AssertionError("public mention must not read site metadata")

    def get_site(self, *_: Any, **__: Any) -> Any:
        self.calls.append("show")
        raise AssertionError("public mention must not read site metadata")

    def list_releases(self, *_: Any, **__: Any) -> tuple[Any, ...]:
        self.calls.append("releases")
        raise AssertionError("public mention must not read site metadata")


@pytest.mark.asyncio
async def test_site_list_is_locally_consumed_without_reading_private_metadata() -> None:
    guard = Guard()
    bot, message, _ = _message("私のサイト一覧を見せて", guard=guard)
    service = ForbiddenSiteService()
    bot.site_publish_service = service
    bot.ai_service = SimpleNamespace(__getattr__=lambda *_: (_ for _ in ()).throw(AssertionError("AI must not run")))

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and reply.provider == "local-action-router"
    assert "/site list" in reply.text
    assert "ephemeral" in reply.text
    assert service.calls == []
    assert "https://publish.example.test" not in reply.text
    assert "private" not in reply.text.casefold()
    assert guard.calls[0]["capability_id"] == COMMAND_CAPABILITIES["site list"]


@pytest.mark.asyncio
async def test_site_list_alias_also_uses_ephemeral_slash_guidance() -> None:
    guard = Guard()
    bot, message, _ = _message("サイト一覧を見せて", guard=guard, owner=True)
    service = ForbiddenSiteService()
    bot.site_publish_service = service

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "/site list" in reply.text
    assert service.calls == []
    assert guard.calls[0]["capability_id"] == COMMAND_CAPABILITIES["site list"]


@pytest.mark.asyncio
async def test_site_show_is_locally_consumed_without_reading_private_metadata() -> None:
    guard = Guard()
    bot, message, _ = _message("サイト詳細: PRIVATE-SITE", guard=guard, owner=True)
    service = ForbiddenSiteService()
    bot.site_publish_service = service

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "/site show" in reply.text
    assert "ephemeral" in reply.text
    assert service.calls == []
    for forbidden in ("private-site", "release-secret", "aaaaaaaaaaaa", "https://"):
        assert forbidden not in reply.text.casefold()
    assert guard.calls[0]["capability_id"] == COMMAND_CAPABILITIES["site show"]


@pytest.mark.asyncio
async def test_site_status_reads_local_snapshot_without_refresh_or_live_claim() -> None:
    guard = Guard()
    bot, message, _ = _message("サイト公開基盤の状態を教えて", guard=guard)
    bot.site_publish_status = SimpleNamespace(configured=True, ready=False, detail="構成済み @everyone")
    bot.site_publish_refresh = lambda: (_ for _ in ()).throw(AssertionError("refresh must not run"))

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "ローカルreadiness" in reply.text
    assert "not ready" in reply.text
    assert "live接続の成功を意味しません" in reply.text
    assert "@everyone" not in reply.text
    assert guard.calls[0]["capability_id"] == COMMAND_CAPABILITIES["site status"]


@pytest.mark.asyncio
async def test_bot_owner_can_grant_and_revoke_site_publish_for_one_mentioned_member(tmp_path: Path) -> None:
    target_id = USER_ID + 1
    bot, message, _ = _message(f"<@{target_id}> にもサイト公開を許可", owner=True)
    target = SimpleNamespace(id=target_id, bot=False, guild_permissions=SimpleNamespace())
    owner = message.author

    async def fetch_member(user_id: int) -> Any:
        if user_id == owner.id:
            return owner
        if user_id == target.id:
            return target
        raise LookupError("member not found")

    message.guild.fetch_member = fetch_member
    message.channel.permissions_for = lambda _member: SimpleNamespace(
        view_channel=True,
        read_message_history=True,
    )
    message.mentions = (target,)
    database = Database(tmp_path / "site-delegation.sqlite3")
    database.open()
    database.migrate()
    database.set_owner_managed_capability_enabled(
        "cap-run-site-auto-publish",
        True,
        GUILD_ID,
        updated_by=USER_ID,
    )
    bot.database = database
    try:
        granted = await NaturalActionRouter(bot)(message, _request())

        assert granted is not None
        assert "委任許可リストで追加" in granted.text
        assert "BOT所有者権限や他の管理権限は付与していません" in granted.text
        record = database.get_capability_actor_grant("cap-run-site-auto-publish", target_id, GUILD_ID)
        assert record is not None and record.grant_kind == "owner_delegated"

        message.id += 1
        message.content = f"<@{BOT_ID}> <@{target_id}> のサイト公開許可を解除"
        revoked = await NaturalActionRouter(bot)(message, _request())

        assert revoked is not None and "委任許可リストで解除" in revoked.text
        assert database.get_capability_actor_grant("cap-run-site-auto-publish", target_id, GUILD_ID) is None

        message.id += 1
        message.content = f"<@{BOT_ID}> <@{target_id}> にもサイト公開を許可して、その後サイトを作って"
        assert await NaturalActionRouter(bot)(message, _request()) is None
    finally:
        database.close()


@pytest.mark.asyncio
async def test_guild_owner_without_bot_owner_identity_cannot_delegate_site_publish(tmp_path: Path) -> None:
    target_id = USER_ID + 1
    bot, message, _ = _message(f"<@{target_id}> にもサイト公開を許可", owner=True)
    bot.settings.bot_owner_ids = frozenset()
    bot.database = Database(tmp_path / "site-delegation-denied.sqlite3")
    bot.database.open()
    bot.database.migrate()
    message.mentions = (SimpleNamespace(id=target_id, bot=False),)
    try:
        reply = await NaturalActionRouter(bot)(message, _request())

        assert reply is not None and "利用できません" in reply.text
        assert bot.database.get_capability_actor_grant("cap-run-site-auto-publish", target_id, GUILD_ID) is None
    finally:
        bot.database.close()


@pytest.mark.parametrize("text", ("サイト詳細:", "サイト詳細: ../secret"))
@pytest.mark.asyncio
async def test_invalid_explicit_site_show_is_consumed_locally(text: str) -> None:
    bot, message, _ = _message(text)
    bot.ai_service = SimpleNamespace(__getattr__=lambda *_: (_ for _ in ()).throw(AssertionError("AI must not run")))

    reply = await NaturalActionRouter(bot)(message, _request())

    assert reply is not None and "site_idの形式が不正" in reply.text


@pytest.mark.asyncio
async def test_timeout_includes_waiting_for_the_concurrency_slot() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(*_: Any) -> ActionResult:
        started.set()
        await release.wait()
        return ActionResult(ActionStatus.COMPLETED, "done")

    def parser(text: str) -> dict[str, str] | None:
        return {} if text == "実行" else None

    spec = ActionSpec(
        "test.execute",
        "earthquake latest",
        COMMAND_CAPABILITIES["earthquake latest"],
        RbacLevel.EVERYONE,
        parser,
        execute,
    )
    bot, first_message, _ = _message("実行")
    second_message = SimpleNamespace(**vars(first_message))
    second_message.id = 401
    router = NaturalActionRouter(bot, registry=ActionRegistry((spec,)), timeout_seconds=0.1, max_concurrency=1)

    first = asyncio.create_task(router(first_message, _request()))
    await started.wait()
    second = await router(second_message, _request())
    release.set()
    first_result = await first

    assert second is not None and "時間内" in second.text
    assert first_result is not None and "時間内" in first_result.text


@pytest.mark.asyncio
async def test_action_specific_timeout_can_exceed_the_router_default() -> None:
    async def execute(*_: Any) -> ActionResult:
        await asyncio.sleep(0.15)
        return ActionResult(ActionStatus.COMPLETED, "done")

    spec = ActionSpec(
        "test.slow-remote",
        "earthquake latest",
        COMMAND_CAPABILITIES["earthquake latest"],
        RbacLevel.EVERYONE,
        lambda text: {} if text == "実行" else None,
        execute,
        timeout_seconds=0.3,
    )
    bot, message, _ = _message("実行")

    reply = await NaturalActionRouter(
        bot,
        registry=ActionRegistry((spec,)),
        timeout_seconds=0.1,
    )(message, _request())

    assert reply is not None
    assert reply.text == "done"
