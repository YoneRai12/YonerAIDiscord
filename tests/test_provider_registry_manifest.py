from __future__ import annotations

import json

import pytest

from yonerai_discord.control_plane import RbacLevel, RiskLevel
from yonerai_discord.provider_registry import (
    DEFAULT_CATALOG,
    LEGACY_MODEL_ALIASES,
    LogicalCapability,
    ManifestValidationError,
    ProviderCatalogManifest,
    ReadinessCode,
    load_default_catalog,
)
from yonerai_discord.provider_registry.registry import ProviderRegistry


def test_default_manifest_has_every_requested_logical_capability() -> None:
    assert {policy.capability for policy in DEFAULT_CATALOG.capabilities} == set(LogicalCapability)
    assert DEFAULT_CATALOG.module_id == "integration.provider-registry"


def test_default_manifest_is_fail_closed_until_provider_is_configured() -> None:
    registry = ProviderRegistry(DEFAULT_CATALOG)
    resolution = registry.resolve(LogicalCapability.AI_TEXT)
    assert not resolution.ready
    assert resolution.code is ReadinessCode.ROUTE_UNCONFIGURED


def test_current_gpt_names_have_compatibility_aliases() -> None:
    assert LEGACY_MODEL_ALIASES == {
        "gpt-5.6-terra": "ai.balanced",
        "gpt-5.6-sol": "ai.quality",
        "gpt-5.6-luna": "ai.fast",
    }
    assert DEFAULT_CATALOG.canonical_model_alias("gpt-5.6-sol") == "ai.quality"


def test_default_non_text_provider_capabilities_are_off() -> None:
    registry = ProviderRegistry(DEFAULT_CATALOG)
    for capability in set(LogicalCapability) - {LogicalCapability.AI_TEXT}:
        assert not registry.capability_enabled(capability)


def test_speech_transcription_policy_is_trusted_high_and_route_is_unbound() -> None:
    policy = DEFAULT_CATALOG.capability_policy(LogicalCapability.SPEECH_STT)
    route = DEFAULT_CATALOG.route(LogicalCapability.SPEECH_STT)

    assert policy is not None
    assert policy.default_enabled is False
    assert policy.required_rbac is RbacLevel.TRUSTED
    assert policy.risk is RiskLevel.HIGH
    assert policy.requires_consent is True
    assert route is not None
    assert all(not tier.provider_ids for tier in route.tiers)


def test_manifest_parser_rejects_secret_values_and_unknown_keys() -> None:
    raw = DEFAULT_CATALOG.to_public_mapping()
    raw["providers"] = [
        {
            "id": "bad-provider",
            "kind": "api",
            "adapter_id": "bad.adapter",
            "enabled": False,
            "capabilities": ["ai.text.generate"],
            "resources": {
                "target": "remote",
                "max_concurrency": 8,
                "load_policy": "provider_managed",
                "offload_policy": "disabled",
                "exclusive_gpu_lease": False,
            },
            "api_key": "sk-secret-value",
        }
    ]
    raw["routes"][0]["tiers"][1]["provider_ids"] = ["bad-provider"]

    with pytest.raises(ManifestValidationError, match="unknown keys"):
        ProviderCatalogManifest.from_mapping(raw)


def test_public_manifest_roundtrip_contains_refs_not_secret_values() -> None:
    raw = load_default_catalog().to_public_mapping()
    raw["providers"].append(
        {
            "id": "api-provider",
            "kind": "api",
            "adapter_id": "test.adapter",
            "enabled": False,
            "capabilities": ["ai.text.generate"],
            "models": [{"alias": "ai.balanced", "provider_model": "remote-model-v1"}],
            "secret_refs": ["env:PROVIDER_API_KEY"],
            "setting_refs": ["env:PROVIDER_BASE_URL"],
            "timeouts": {"request_seconds": 60, "health_seconds": 5},
            "resources": {
                "target": "remote",
                "max_concurrency": 8,
                "vram_budget_mb": None,
                "system_ram_budget_mb": None,
                "load_policy": "provider_managed",
                "idle_unload_seconds": None,
                "offload_policy": "disabled",
                "exclusive_gpu_lease": False,
                "device_ref": None,
            },
        }
    )
    raw["routes"][0]["tiers"][1]["provider_ids"] = ["api-provider"]
    loaded = ProviderCatalogManifest.from_mapping(raw)
    serialized = json.dumps(loaded.to_public_mapping(), ensure_ascii=False)
    assert "env:PROVIDER_API_KEY" in serialized
    assert "secret_value" not in serialized
    assert 'api_key"' not in serialized
