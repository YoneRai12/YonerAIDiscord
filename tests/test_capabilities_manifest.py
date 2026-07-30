from __future__ import annotations

from pathlib import Path

from yonerai_discord.capabilities import (
    COMMAND_CAPABILITIES,
    COMMAND_PLUGIN_BY_ROOT,
    COMMAND_RBAC_FLOORS,
    CONNECTED_INTERNAL_CAPABILITY_IDS,
    EVENT_CAPABILITIES,
    PLUGIN_MODULES,
    SURFACE_RATE_LIMITS,
    build_capability_registry,
    startup_plugins_for_registry,
)
from yonerai_discord.config import SAFE_DEFAULT_PLUGINS, Settings
from yonerai_discord.control_plane import InMemoryStateStore, RbacLevel, RiskLevel
from yonerai_discord.runtime_manifest import RUNTIME_CAPABILITIES
from yonerai_discord.runtime_manifests.ai_memory import AI_ATTACHMENT_UNDERSTANDING_CAPABILITY_ID
from yonerai_discord.runtime_manifests.capability_forge import FORGE_OWNER_NOTIFICATION_CAPABILITY_ID
from yonerai_discord.runtime_manifests.image_editing import IMAGE_EDITING_CAPABILITY_ID
from yonerai_discord.runtime_manifests.image_generation import IMAGE_GENERATION_CAPABILITY_ID
from yonerai_discord.runtime_manifests.music_generation import MUSIC_GENERATION_CAPABILITY_ID
from yonerai_discord.runtime_manifests.speech_synthesis import (
    SPEECH_SYNTHESIS_CAPABILITY_ID,
)
from yonerai_discord.runtime_manifests.speech_transcription import (
    SPEECH_TRANSCRIPTION_CAPABILITY_ID,
)
from yonerai_discord.runtime_manifests.video_generation import VIDEO_GENERATION_CAPABILITY_ID


def test_manifest_keeps_656_canonical_rows_and_registers_only_real_runtime_entries() -> None:
    root = Path(__file__).parents[1]
    settings = Settings.from_env(
        {
            "DISCORD_TOKEN": "unused-test-token",
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
        }
    )
    registry = build_capability_registry(settings, InMemoryStateStore())
    canonical = {item.capability_id for item in registry.capabilities if item.source_state != "runtime"}
    runtime = {item.capability_id for item in registry.capabilities if item.source_state == "runtime"}
    assert len(canonical) == 656
    connected = {item.capability_id for item in registry.capabilities if item.implemented}
    assert runtime == {definition.capability_id for definition in RUNTIME_CAPABILITIES}
    assert connected == (
        set(COMMAND_CAPABILITIES.values())
        | set(EVENT_CAPABILITIES.values())
        | set(CONNECTED_INTERNAL_CAPABILITY_IDS)
        | runtime
    )
    assert registry.capability("cap-can-0153").implemented is True
    startup = startup_plugins_for_registry(settings, registry)
    assert {"ai", "community", "discovery", "modtools", "scheduling", "servertools", "utility", "voice"} <= startup
    assert {"earthquake", "jp_information", "personal_memory"} <= startup
    assert "identity" not in startup
    assert COMMAND_CAPABILITIES["verify status"] == "cap-run-verify-status"
    assert COMMAND_CAPABILITIES["verify configure"] == "cap-run-verify-configure"
    assert COMMAND_CAPABILITIES["yonerai status"] == "cap-run-yonerai-status"
    assert COMMAND_CAPABILITIES["yonerai health"] == "cap-run-yonerai-health"
    assert COMMAND_CAPABILITIES["earthquake latest"] == "cap-run-earthquake-latest"
    assert COMMAND_CAPABILITIES["earthquake subscribe"] == "cap-run-earthquake-subscribe"
    assert COMMAND_CAPABILITIES["help"] == "cap-run-discovery-help"
    assert COMMAND_CAPABILITIES["weather"] == "cap-run-weather"
    assert COMMAND_CAPABILITIES["warning"] == "cap-run-warning"
    assert COMMAND_CAPABILITIES["holiday next"] == "cap-run-holiday-next"
    assert COMMAND_CAPABILITIES["holiday year"] == "cap-run-holiday-year"
    assert COMMAND_CAPABILITIES["nasa apod"] == "cap-run-nasa-apod-read"
    assert COMMAND_CAPABILITIES["web search"] == "cap-can-0153"
    assert SURFACE_RATE_LIMITS["web search"] == 5
    assert COMMAND_CAPABILITIES["web fetch"] == "cap-can-0153"
    assert COMMAND_CAPABILITIES["web find"] == "cap-can-0153"
    assert SURFACE_RATE_LIMITS["web fetch"] == 5
    assert SURFACE_RATE_LIMITS["web find"] == 10
    assert COMMAND_PLUGIN_BY_ROOT["nasa"] == "nasa_apod"
    assert PLUGIN_MODULES["nasa_apod"] == ("operations.nasa-apod",)
    assert SURFACE_RATE_LIMITS["nasa apod"] == 10
    assert COMMAND_RBAC_FLOORS.get("nasa apod", RbacLevel.EVERYONE) is RbacLevel.EVERYONE
    assert COMMAND_CAPABILITIES["image generate"] == IMAGE_GENERATION_CAPABILITY_ID
    assert COMMAND_PLUGIN_BY_ROOT["image"] == "image_generation"
    assert PLUGIN_MODULES["image_generation"] == ("media.image-generation",)
    assert SURFACE_RATE_LIMITS["image generate"] == 3
    assert COMMAND_RBAC_FLOORS.get("image generate", RbacLevel.EVERYONE) is RbacLevel.TRUSTED
    assert COMMAND_CAPABILITIES["musicgen generate"] == MUSIC_GENERATION_CAPABILITY_ID
    assert COMMAND_PLUGIN_BY_ROOT["musicgen"] == "music_generation"
    assert PLUGIN_MODULES["music_generation"] == ("media.music-generation",)
    assert SURFACE_RATE_LIMITS["musicgen generate"] == 3
    assert COMMAND_RBAC_FLOORS.get("musicgen generate", RbacLevel.EVERYONE) is RbacLevel.TRUSTED
    assert COMMAND_CAPABILITIES["video generate"] == VIDEO_GENERATION_CAPABILITY_ID
    assert COMMAND_PLUGIN_BY_ROOT["video"] == "video_generation"
    assert PLUGIN_MODULES["video_generation"] == ("media.video-generation",)
    assert SURFACE_RATE_LIMITS["video generate"] == 3
    assert COMMAND_RBAC_FLOORS.get("video generate", RbacLevel.EVERYONE) is RbacLevel.TRUSTED
    assert COMMAND_CAPABILITIES["music move"] == "cap-run-music-move"
    assert COMMAND_CAPABILITIES["music clear-mine"] == "cap-run-music-clear-mine"
    assert COMMAND_CAPABILITIES["music seek"] == "cap-run-music-seek"
    assert SURFACE_RATE_LIMITS["music seek"] == 15
    assert COMMAND_RBAC_FLOORS.get("music seek", RbacLevel.EVERYONE) is RbacLevel.EVERYONE
    assert COMMAND_CAPABILITIES["music radio"] == "cap-run-music-radio"
    assert SURFACE_RATE_LIMITS["music radio"] == 5
    assert EVENT_CAPABILITIES["music_read_aloud_message"] == "cap-run-music-read-aloud-message"
    assert SURFACE_RATE_LIMITS["music_read_aloud_message"] == 20
    for command_path, rate_limit in (
        ("music read-aloud enable", 5),
        ("music read-aloud disable", 5),
        ("music read-aloud list", 10),
        ("music read-aloud dictionary-set", 5),
        ("music read-aloud dictionary-delete", 5),
        ("music read-aloud exclude-add", 5),
        ("music read-aloud exclude-delete", 5),
        ("music read-aloud policy", 10),
        ("music read-aloud server-preset", 5),
    ):
        assert COMMAND_CAPABILITIES[command_path] == "cap-run-music-read-aloud-message"
        assert COMMAND_RBAC_FLOORS[command_path] is RbacLevel.GUILD_ADMIN
        assert SURFACE_RATE_LIMITS[command_path] == rate_limit
    for command_path, rate_limit in (
        ("music read-aloud my-preset", 5),
        ("music read-aloud preset", 10),
    ):
        assert COMMAND_CAPABILITIES[command_path] == "cap-run-music-read-aloud-message"
        assert COMMAND_RBAC_FLOORS[command_path] is RbacLevel.EVERYONE
        assert SURFACE_RATE_LIMITS[command_path] == rate_limit
    read_aloud = registry.capability("cap-run-music-read-aloud-message")
    assert read_aloud.default_enabled is False
    assert read_aloud.required_level is RbacLevel.EVERYONE
    assert read_aloud.risk is RiskLevel.MEDIUM
    assert COMMAND_RBAC_FLOORS.get("music radio", RbacLevel.EVERYONE) is RbacLevel.EVERYONE
    seek = registry.capability("cap-run-music-seek")
    assert seek.module_id == "media.music"
    assert seek.required_level is RbacLevel.EVERYONE
    assert seek.minimum_level is RbacLevel.EVERYONE
    assert seek.risk is RiskLevel.MEDIUM
    assert seek.default_enabled is True
    assert seek.dependencies == ("cap-run-audio-ducking-core",)
    personal_memory = registry.module("intelligence.personal-memory")
    assert personal_memory.dependencies == ("intelligence.memory",)
    assert registry.capability("cap-run-memory-remember").module_id == personal_memory.module_id
    context_recall = registry.capability("cap-run-memory-context-recall")
    assert context_recall.module_id == personal_memory.module_id
    context_definition = next(
        definition for definition in RUNTIME_CAPABILITIES if definition.capability_id == "cap-run-memory-context-recall"
    )
    assert context_definition.command_paths == ()
    assert context_definition.event_names == ()
    attachment_understanding = registry.capability(AI_ATTACHMENT_UNDERSTANDING_CAPABILITY_ID)
    assert attachment_understanding.module_id == "intelligence.ai-runtime"
    assert attachment_understanding.default_enabled is False
    attachment_definition = next(
        definition
        for definition in RUNTIME_CAPABILITIES
        if definition.capability_id == AI_ATTACHMENT_UNDERSTANDING_CAPABILITY_ID
    )
    assert attachment_definition.command_paths == ()
    assert attachment_definition.event_names == ()
    assert registry.module("operations.earthquake").dependencies == ("operations.scheduling-notification",)
    assert registry.capability("cap-run-weather").module_id == "operations.public-information"
    assert registry.module("operations.nasa-apod").default_enabled is False
    assert registry.capability("cap-run-nasa-apod-read").default_enabled is False
    assert registry.capability("cap-run-nasa-apod-read").module_id == "operations.nasa-apod"
    assert registry.module("media.image-generation").default_enabled is False
    assert registry.capability(IMAGE_GENERATION_CAPABILITY_ID).default_enabled is False
    assert registry.capability(IMAGE_GENERATION_CAPABILITY_ID).module_id == "media.image-generation"
    assert PLUGIN_MODULES["image_editing"] == ("media.image-editing",)
    assert registry.module("media.image-editing").default_enabled is False
    assert registry.capability(IMAGE_EDITING_CAPABILITY_ID).default_enabled is False
    assert registry.capability(IMAGE_EDITING_CAPABILITY_ID).module_id == "media.image-editing"
    editing_definition = next(
        definition for definition in RUNTIME_CAPABILITIES if definition.capability_id == IMAGE_EDITING_CAPABILITY_ID
    )
    assert editing_definition.command_paths == ()
    assert editing_definition.event_names == ()
    assert registry.module("media.music-generation").default_enabled is False
    assert registry.capability(MUSIC_GENERATION_CAPABILITY_ID).default_enabled is False
    assert registry.capability(MUSIC_GENERATION_CAPABILITY_ID).module_id == "media.music-generation"
    assert PLUGIN_MODULES["speech_synthesis"] == ("media.speech-synthesis",)
    assert registry.module("media.speech-synthesis").default_enabled is False
    assert registry.capability(SPEECH_SYNTHESIS_CAPABILITY_ID).default_enabled is False
    assert registry.capability(SPEECH_SYNTHESIS_CAPABILITY_ID).module_id == "media.speech-synthesis"
    synthesis_definition = next(
        definition for definition in RUNTIME_CAPABILITIES if definition.capability_id == SPEECH_SYNTHESIS_CAPABILITY_ID
    )
    assert synthesis_definition.command_paths == ()
    assert synthesis_definition.event_names == ()
    assert PLUGIN_MODULES["speech_transcription"] == ("media.speech-transcription",)
    assert registry.module("media.speech-transcription").default_enabled is False
    assert registry.capability(SPEECH_TRANSCRIPTION_CAPABILITY_ID).default_enabled is False
    assert registry.capability(SPEECH_TRANSCRIPTION_CAPABILITY_ID).module_id == "media.speech-transcription"
    speech_definition = next(
        definition
        for definition in RUNTIME_CAPABILITIES
        if definition.capability_id == SPEECH_TRANSCRIPTION_CAPABILITY_ID
    )
    assert speech_definition.command_paths == ()
    assert speech_definition.event_names == ()
    assert registry.module("media.video-generation").default_enabled is False
    assert registry.capability(VIDEO_GENERATION_CAPABILITY_ID).default_enabled is False
    assert registry.capability(VIDEO_GENERATION_CAPABILITY_ID).module_id == "media.video-generation"
    assert "nasa_apod" not in startup
    assert "image_generation" not in startup
    assert "image_generation" not in SAFE_DEFAULT_PLUGINS
    assert "image_editing" not in startup
    assert "image_editing" not in SAFE_DEFAULT_PLUGINS
    assert "music_generation" not in startup
    assert "music_generation" not in SAFE_DEFAULT_PLUGINS
    assert "speech_synthesis" not in startup
    assert "speech_synthesis" not in SAFE_DEFAULT_PLUGINS
    assert "speech_transcription" not in startup
    assert "speech_transcription" not in SAFE_DEFAULT_PLUGINS
    assert "video_generation" not in startup
    assert "video_generation" not in SAFE_DEFAULT_PLUGINS
    assert "capability_forge" not in startup
    assert "capability_forge" not in SAFE_DEFAULT_PLUGINS
    assert registry.module("intelligence.capability-forge").default_enabled is False
    forge = registry.capability(FORGE_OWNER_NOTIFICATION_CAPABILITY_ID)
    assert forge.default_enabled is False
    assert forge.owner_only is True
    assert forge.module_id == "intelligence.capability-forge"


def test_minecraft_remains_off_even_if_capability_override_is_on() -> None:
    root = Path(__file__).parents[1]
    store = InMemoryStateStore()
    settings = Settings.from_env(
        {
            "DISCORD_TOKEN": "unused-test-token",
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
        }
    )
    registry = build_capability_registry(settings, store)
    minecraft_capability = next(item for item in registry.capabilities if item.module_id == "gaming.minecraft")
    store.set_capability_override(minecraft_capability.capability_id, True)
    assert not registry.is_module_enabled("gaming.minecraft")
    assert not registry.is_capability_enabled(minecraft_capability.capability_id)


def test_nasa_plugin_requires_explicit_plugin_module_and_capability_opt_in() -> None:
    root = Path(__file__).parents[1]
    store = InMemoryStateStore()
    settings = Settings.from_env(
        {
            "DISCORD_TOKEN": "unused-test-token",
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
            "ENABLED_PLUGINS": "nasa_apod",
        }
    )
    registry = build_capability_registry(settings, store)

    assert "nasa_apod" not in startup_plugins_for_registry(settings, registry)
    store.set_module_override("operations.nasa-apod", True)
    assert "nasa_apod" in startup_plugins_for_registry(settings, registry)
    assert registry.is_capability_enabled("cap-run-nasa-apod-read") is False
    store.set_capability_override("cap-run-nasa-apod-read", True)
    assert registry.is_capability_enabled("cap-run-nasa-apod-read") is True


def test_image_generation_requires_explicit_plugin_module_and_capability_opt_in() -> None:
    root = Path(__file__).parents[1]
    store = InMemoryStateStore()
    settings = Settings.from_env(
        {
            "DISCORD_TOKEN": "unused-test-token",
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
            "ENABLED_PLUGINS": "image_generation",
        }
    )
    registry = build_capability_registry(settings, store)

    assert "image_generation" not in startup_plugins_for_registry(settings, registry)
    store.set_module_override("media.image-generation", True)
    assert "image_generation" in startup_plugins_for_registry(settings, registry)
    assert registry.is_capability_enabled(IMAGE_GENERATION_CAPABILITY_ID) is False
    store.set_capability_override(IMAGE_GENERATION_CAPABILITY_ID, True)
    assert registry.is_capability_enabled(IMAGE_GENERATION_CAPABILITY_ID) is True


def test_video_generation_requires_explicit_plugin_module_and_capability_opt_in() -> None:
    root = Path(__file__).parents[1]
    store = InMemoryStateStore()
    settings = Settings.from_env(
        {
            "DISCORD_TOKEN": "unused-test-token",
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
            "ENABLED_PLUGINS": "video_generation",
        }
    )
    registry = build_capability_registry(settings, store)

    assert "video_generation" not in startup_plugins_for_registry(settings, registry)
    store.set_module_override("media.video-generation", True)
    assert "video_generation" in startup_plugins_for_registry(settings, registry)
    assert registry.is_capability_enabled(VIDEO_GENERATION_CAPABILITY_ID) is False
    store.set_capability_override(VIDEO_GENERATION_CAPABILITY_ID, True)
    assert registry.is_capability_enabled(VIDEO_GENERATION_CAPABILITY_ID) is True


def test_music_generation_requires_explicit_plugin_module_and_capability_opt_in() -> None:
    root = Path(__file__).parents[1]
    store = InMemoryStateStore()
    settings = Settings.from_env(
        {
            "DISCORD_TOKEN": "unused-test-token",
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
            "ENABLED_PLUGINS": "music_generation",
        }
    )
    registry = build_capability_registry(settings, store)

    assert "music_generation" not in startup_plugins_for_registry(settings, registry)
    store.set_module_override("media.music-generation", True)
    assert "music_generation" in startup_plugins_for_registry(settings, registry)
    assert registry.is_capability_enabled(MUSIC_GENERATION_CAPABILITY_ID) is False
    store.set_capability_override(MUSIC_GENERATION_CAPABILITY_ID, True)
    assert registry.is_capability_enabled(MUSIC_GENERATION_CAPABILITY_ID) is True
