from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from yonerai_discord.capabilities import build_capability_registry
from yonerai_discord.config import Settings
from yonerai_discord.control_plane import InMemoryStateStore
from yonerai_discord.runtime_manifest import (
    RUNTIME_CAPABILITIES,
    RUNTIME_COMMAND_CAPABILITIES,
    RUNTIME_EVENT_CAPABILITIES,
    RUNTIME_MODULES,
)
from yonerai_discord.runtime_manifests import (
    CAPABILITY_FRAGMENTS,
    RUNTIME_CAPABILITIES as FRAGMENT_CAPABILITIES,
    RUNTIME_MODULES as FRAGMENT_MODULES,
)


EXPECTED_FRAGMENT_ORDER = (
    "admin_ui",
    "ai_memory",
    "browser_rendering",
    "earthquake",
    "music_audio",
    "media_pipeline",
    "media_inspection",
    "site_publish",
    "system_ops",
    "community",
    "capability_forge",
    "utility",
    "moderation_server",
    "discovery",
    "jp_information",
    "nasa_apod",
    "image_editing",
    "image_generation",
    "music_generation",
    "speech_synthesis",
    "speech_transcription",
    "video_generation",
    "web_search",
)
EXPECTED_MANIFEST_SHA256 = "3c12e5fe6bdbd5685a9b9f05ad89265dfdc1d83053f92c5e845b82f490eb245f"


def test_explicit_fragments_reconstruct_public_aggregate_in_original_order() -> None:
    assert tuple(name for name, _ in CAPABILITY_FRAGMENTS) == EXPECTED_FRAGMENT_ORDER
    assert all(fragment for _, fragment in CAPABILITY_FRAGMENTS)
    flattened = tuple(definition for _, fragment in CAPABILITY_FRAGMENTS for definition in fragment)

    assert flattened == RUNTIME_CAPABILITIES
    assert FRAGMENT_CAPABILITIES is RUNTIME_CAPABILITIES
    assert FRAGMENT_MODULES is RUNTIME_MODULES


def test_ordered_manifest_fingerprint_preserves_every_public_definition_field() -> None:
    payload = {
        "modules": [asdict(definition) for definition in RUNTIME_MODULES],
        "capabilities": [asdict(definition) for definition in RUNTIME_CAPABILITIES],
    }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    assert len(RUNTIME_MODULES) == 17
    assert len(RUNTIME_CAPABILITIES) == 170
    assert hashlib.sha256(serialized.encode("utf-8")).hexdigest() == EXPECTED_MANIFEST_SHA256


def test_runtime_ids_and_normalized_command_event_paths_are_unique() -> None:
    module_ids = [definition.module_id for definition in RUNTIME_MODULES]
    capability_ids = [definition.capability_id for definition in RUNTIME_CAPABILITIES]
    command_paths = [path.strip().lower() for definition in RUNTIME_CAPABILITIES for path in definition.command_paths]
    event_names = [name.strip().lower() for definition in RUNTIME_CAPABILITIES for name in definition.event_names]

    assert len(module_ids) == len(set(module_ids))
    assert len(capability_ids) == len(set(capability_ids))
    assert len(command_paths) == len(set(command_paths))
    assert len(event_names) == len(set(event_names))
    assert RUNTIME_COMMAND_CAPABILITIES == {
        path.strip().lower(): definition.capability_id
        for definition in RUNTIME_CAPABILITIES
        for path in definition.command_paths
    }
    assert RUNTIME_EVENT_CAPABILITIES == {
        name.strip().lower(): definition.capability_id
        for definition in RUNTIME_CAPABILITIES
        for name in definition.event_names
    }


def test_runtime_module_and_capability_dependencies_resolve_without_duplicates() -> None:
    root = Path(__file__).parents[1]
    settings = Settings.from_env(
        {
            "DISCORD_TOKEN": "unused-test-token",
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
        }
    )
    registry = build_capability_registry(settings, InMemoryStateStore())
    registered_module_ids = {module.module_id for module in registry.modules}
    registered_capability_ids = {capability.capability_id for capability in registry.capabilities}

    for definition in RUNTIME_MODULES:
        assert len(definition.dependencies) == len(set(definition.dependencies))
        assert definition.module_id not in definition.dependencies
        assert set(definition.dependencies) <= registered_module_ids
    for definition in RUNTIME_CAPABILITIES:
        assert len(definition.dependencies) == len(set(definition.dependencies))
        assert definition.capability_id not in definition.dependencies
        assert set(definition.dependencies) <= registered_capability_ids
    assert registry.validate() == ()


def test_aggregate_uses_static_imports_instead_of_dynamic_discovery() -> None:
    aggregate = Path(__file__).parents[1] / "src" / "yonerai_discord" / "runtime_manifests" / "__init__.py"
    source = aggregate.read_text(encoding="utf-8")

    assert "CAPABILITY_FRAGMENTS" in source
    assert "importlib" not in source
    assert "pkgutil" not in source
    assert "iter_modules" not in source
