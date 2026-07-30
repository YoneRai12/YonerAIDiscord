from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from .config import Settings
from .control_plane import RbacLevel, Registry, StateStore, load_capability_catalog
from .runtime_manifest import (
    RUNTIME_COMMAND_CAPABILITIES,
    RUNTIME_EVENT_CAPABILITIES,
    register_runtime_capabilities,
    register_runtime_modules,
)


AI_WEB_SEARCH_CAPABILITY_ID = "cap-can-0153"
OPENAI_PAID_WEB_SEARCH_CAPABILITY_ID = "cap-run-web-search-openai-paid"
MODEL_TOOL_CAPABILITY_BINDINGS: Mapping[str, str] = MappingProxyType(
    {"web_search": OPENAI_PAID_WEB_SEARCH_CAPABILITY_ID}
)
SITE_AUTO_PUBLISH_CAPABILITY_ID = "cap-run-site-auto-publish"
DELEGATABLE_ACTOR_CAPABILITY_IDS = frozenset({SITE_AUTO_PUBLISH_CAPABILITY_ID})


# Discordの入口とcanonical capabilityを一対一で結ぶ。ここにないcommandは
# pluginが存在してもglobal guardでfail-closedになる。
CANONICAL_COMMAND_CAPABILITIES: Mapping[str, str] = MappingProxyType(
    {
        "system ping": "cap-can-0265",
        "system health": "cap-can-0519",
        "system plugins": "cap-can-0588",
        "system doctor": "cap-can-0278",
        "system modules": "cap-can-0589",
        "system capabilities": "cap-can-0589",
        "system module-set": "cap-can-0613",
        "system capability-set": "cap-can-0613",
        "system permission-set": "cap-can-0613",
        "ai status": "cap-can-0162",
        "ai ask": "cap-can-0161",
        "ai search": AI_WEB_SEARCH_CAPABILITY_ID,
        "web search": AI_WEB_SEARCH_CAPABILITY_ID,
        "web fetch": AI_WEB_SEARCH_CAPABILITY_ID,
        "web find": AI_WEB_SEARCH_CAPABILITY_ID,
        "schedule create": "cap-can-0002",
        "schedule list": "cap-can-0007",
        "schedule show": "cap-can-0272",
        "schedule cancel": "cap-can-0001",
        "schedule rsvp": "cap-can-0030",
        "schedule remind": "cap-can-0538",
        "voice status": "cap-can-0415",
        "voice synthesize": "cap-can-0416",
    }
)


COMMAND_CAPABILITIES: Mapping[str, str] = MappingProxyType(
    {**CANONICAL_COMMAND_CAPABILITIES, **RUNTIME_COMMAND_CAPABILITIES}
)


# Discord slash commandを偽装せず、planner-only ActionSpecだけへ接続するcode-owned binding。
# tupleを正本にして、同じaction pathの重複をimport時にfail-closedで拒否する。
_ACTION_CAPABILITY_ROWS: tuple[tuple[str, str], ...] = (
    ("browser interact", "cap-run-browser-remote-interactive"),
    ("browser screenshot", "cap-run-browser-remote-screenshot"),
    ("media url-inspect", "cap-run-media-url-inspection"),
    ("image edit", "cap-run-image-edit"),
    ("media qr-encode", "cap-run-media-qr-encode"),
    ("media place-on-canvas", "cap-run-media-place-on-canvas"),
    ("media compose-grid", "cap-run-media-compose-grid"),
    ("media quote-card", "cap-run-media-quote-card"),
    ("media discord-asset-inspect", "cap-run-media-discord-asset-inspect"),
)
if len({path for path, _ in _ACTION_CAPABILITY_ROWS}) != len(_ACTION_CAPABILITY_ROWS):
    raise RuntimeError("duplicate planner action capability path")
ACTION_CAPABILITIES: Mapping[str, str] = MappingProxyType(dict(_ACTION_CAPABILITY_ROWS))
if set(ACTION_CAPABILITIES).intersection(COMMAND_CAPABILITIES):
    raise RuntimeError("planner action capability path collides with a Discord command path")


EVENT_CAPABILITIES: Mapping[str, str] = MappingProxyType(dict(RUNTIME_EVENT_CAPABILITIES))


# 台帳上のriskに加え、Discord管理面ではこの下限を必ず満たす。
COMMAND_RBAC_FLOORS: Mapping[str, RbacLevel] = MappingProxyType(
    {
        "system health": RbacLevel.GUILD_ADMIN,
        "system plugins": RbacLevel.GUILD_ADMIN,
        "system doctor": RbacLevel.GUILD_ADMIN,
        "system modules": RbacLevel.GUILD_ADMIN,
        "system capabilities": RbacLevel.GUILD_ADMIN,
        "system module-set": RbacLevel.GUILD_ADMIN,
        "system capability-set": RbacLevel.GUILD_ADMIN,
        "system permission-set": RbacLevel.GUILD_ADMIN,
        "image generate": RbacLevel.TRUSTED,
        "musicgen generate": RbacLevel.TRUSTED,
        "video generate": RbacLevel.TRUSTED,
        "music import": RbacLevel.GUILD_ADMIN,
        "music read-aloud enable": RbacLevel.GUILD_ADMIN,
        "music read-aloud disable": RbacLevel.GUILD_ADMIN,
        "music read-aloud focus-start": RbacLevel.GUILD_ADMIN,
        "music read-aloud focus-cancel": RbacLevel.GUILD_ADMIN,
        "music read-aloud list": RbacLevel.GUILD_ADMIN,
        "music read-aloud dictionary-set": RbacLevel.GUILD_ADMIN,
        "music read-aloud dictionary-delete": RbacLevel.GUILD_ADMIN,
        "music read-aloud exclude-add": RbacLevel.GUILD_ADMIN,
        "music read-aloud exclude-delete": RbacLevel.GUILD_ADMIN,
        "music read-aloud policy": RbacLevel.GUILD_ADMIN,
        "music read-aloud server-preset": RbacLevel.GUILD_ADMIN,
        # RbacLevel has no MEMBER sentinel; fresh guild fetch is the membership floor.
        "music read-aloud my-preset": RbacLevel.EVERYONE,
        "music read-aloud preset": RbacLevel.EVERYONE,
    }
)


SURFACE_RATE_LIMITS: Mapping[str, int] = MappingProxyType(
    {
        "ai ask": 5,
        "ai search": 5,
        "web search": 5,
        "web fetch": 5,
        "web find": 10,
        "ai model list": 10,
        "ai model set": 5,
        "ai model auto": 5,
        "ai provider list": 10,
        "ai provider set": 5,
        "ai route": 10,
        "ai reset": 5,
        "ai_mention_message": 6,
        "message_link_expand": 10,
        "site_auto_publish": 3,
        "site status": 10,
        "site list": 10,
        "site show": 10,
        "site publish": 3,
        "site update": 3,
        "site rollback": 2,
        "site visibility": 2,
        "site archive": 2,
        "site domain": 2,
        "memory remember": 10,
        "memory search": 20,
        "memory preview": 10,
        "memory export": 3,
        "memory forget": 10,
        "memory clear": 3,
        "earthquake latest": 10,
        "earthquake status": 10,
        "earthquake subscribe": 3,
        "earthquake unsubscribe": 3,
        "earthquake_feed_delivery": 60,
        "weather": 10,
        "warning": 10,
        "holiday next": 10,
        "holiday year": 10,
        "nasa apod": 10,
        "image generate": 3,
        "musicgen generate": 3,
        "video generate": 3,
        "music status": 10,
        "music join": 5,
        "music leave": 5,
        "music play": 15,
        "music import": 5,
        "music search": 20,
        "music now": 20,
        "music queue": 20,
        "music pause": 15,
        "music resume": 15,
        "music skip": 15,
        "music stop": 10,
        "music radio": 5,
        "music seek": 15,
        "music remove": 15,
        "music shuffle": 10,
        "music loop": 10,
        "music volume": 10,
        "music speak": 6,
        "music read-aloud enable": 5,
        "music read-aloud disable": 5,
        "music read-aloud focus-start": 5,
        "music read-aloud focus-cancel": 5,
        "music read-aloud list": 10,
        "music read-aloud dictionary-set": 5,
        "music read-aloud dictionary-delete": 5,
        "music read-aloud exclude-add": 5,
        "music read-aloud exclude-delete": 5,
        "music read-aloud policy": 10,
        "music read-aloud server-preset": 5,
        "music read-aloud my-preset": 5,
        "music read-aloud preset": 10,
        "music_read_aloud_message": 20,
        "music search-youtube": 20,
        "music playlist save": 5,
        "music playlist list": 10,
        "music playlist load": 5,
        "music playlist delete": 5,
        "voice synthesize": 10,
        "ticket open": 5,
        "poll create": 10,
        "suggest create": 10,
        "mod warn": 10,
        "mod timeout": 5,
        "mod untimeout": 5,
        "mod kick": 3,
        "mod ban": 3,
        "mod unban": 3,
        "mod purge": 3,
        "mod purge-user": 3,
        "mod purge-links": 3,
        "server announce": 5,
        "component.poll-vote": 20,
        "component.selfrole-toggle": 10,
        "minecraft status": 10,
        "automod status": 10,
        "automod channel": 5,
        "automod policy": 5,
        "automod_message_create": 30,
        "automod_message_edit": 20,
        "jobs status": 10,
        "jobs list": 10,
        "jobs retry": 3,
        "jobs cancel": 3,
        "verify status": 5,
        "verify start": 5,
        "verify configure": 3,
        "yonerai status": 10,
        "yonerai health": 5,
        "schedule list": 20,
        "schedule cancel": 5,
        "schedule uncertain": 5,
        "schedule resolve": 3,
        "help": 10,
    }
)


# 1 pluginが複数moduleを提供できるためtupleで管理する。guild overrideでは
# processを停止せず、各interaction/eventのpolicyでguildごとに抑止する。
PLUGIN_MODULES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "admin_ui": ("security.admin-ui",),
        "ai": ("intelligence.ai-runtime",),
        "automod": ("moderation.automod",),
        "browser_rendering": ("web.browser-rendering",),
        "jobs": ("operations.execution",),
        "identity": ("security.access-control",),
        "yonerai": ("operations.observability",),
        "minecraft": ("gaming.minecraft",),
        "music": ("media.music",),
        "media_pipeline": ("media.pipeline",),
        "moderation": ("moderation.actions",),
        "operations": ("operations.observability",),
        "personal_memory": ("intelligence.personal-memory",),
        "speech_synthesis": ("media.speech-synthesis",),
        "speech_transcription": ("media.speech-transcription",),
        "earthquake": ("operations.earthquake",),
        "jp_information": ("operations.public-information",),
        "nasa_apod": ("operations.nasa-apod",),
        "image_editing": ("media.image-editing",),
        "image_generation": ("media.image-generation",),
        "music_generation": ("media.music-generation",),
        "video_generation": ("media.video-generation",),
        "scheduling": ("collaboration.meeting", "operations.scheduling-notification"),
        "site_publish": ("publishing.site-host",),
        "voice": ("media.voice",),
        # 個別pluginはprocess lifecycleとguildごとのpolicyを分離する。
        "community": ("interaction.discord-surface",),
        "capability_forge": ("intelligence.capability-forge",),
        "evolution": ("intelligence.ai-runtime",),
        "modtools": ("moderation.actions",),
        "servertools": ("security.access-control",),
        "utility": ("utility.general",),
        "discovery": ("interaction.discord-surface",),
    }
)


# 実command treeにpathが残っていてもplugin起動失敗時は利用可能と数えない。
COMMAND_PLUGIN_BY_ROOT: Mapping[str, str] = MappingProxyType(
    {
        "ai": "ai",
        "automod": "automod",
        "jobs": "jobs",
        "verify": "identity",
        "yonerai": "yonerai",
        "schedule": "scheduling",
        "voice": "voice",
        "ticket": "community",
        "poll": "community",
        "suggest": "community",
        "selfrole": "community",
        "info": "utility",
        "tools": "utility",
        "mod": "modtools",
        "server": "servertools",
        "evolution": "evolution",
        "minecraft": "minecraft",
        "memory": "personal_memory",
        "earthquake": "earthquake",
        "weather": "jp_information",
        "warning": "jp_information",
        "holiday": "jp_information",
        "nasa": "nasa_apod",
        "image": "image_generation",
        "musicgen": "music_generation",
        "video": "video_generation",
        "music": "music",
        "site": "site_publish",
        "help": "discovery",
    }
)


# commandを持たない内部toolも、実装されたsinkだけを明示接続する。
CONNECTED_INTERNAL_CAPABILITY_IDS = frozenset({OPENAI_PAID_WEB_SEARCH_CAPABILITY_ID})
CATALOG_CONNECTED_CAPABILITY_IDS = frozenset(CANONICAL_COMMAND_CAPABILITIES.values())
CONNECTED_CAPABILITY_IDS = CATALOG_CONNECTED_CAPABILITY_IDS | CONNECTED_INTERNAL_CAPABILITY_IDS


def build_capability_registry(settings: Settings, state_store: StateStore) -> Registry:
    """監査台帳を読み、実際に接続済みの入口だけを実行可能として登録する。"""

    registry = load_capability_catalog(
        settings.capability_catalog_path,
        connected_capability_ids=CATALOG_CONNECTED_CAPABILITY_IDS,
        state_store=state_store,
    )
    register_runtime_modules(registry)
    register_runtime_capabilities(registry)
    registry.validate(raise_on_error=True)
    return registry


def startup_plugins_for_registry(settings: Settings, registry: Registry) -> frozenset[str]:
    """deploy allowlistとglobal module stateの積集合を返す。"""

    selected: set[str] = set()
    for plugin_name in settings.startup_plugins:
        module_ids = PLUGIN_MODULES.get(plugin_name)
        if module_ids is None:
            # 未知pluginはPluginManager側で明示的にFAILEDへする。
            selected.add(plugin_name)
            continue
        if any(registry.is_module_enabled(module_id) for module_id in module_ids):
            selected.add(plugin_name)
    return frozenset(selected)
