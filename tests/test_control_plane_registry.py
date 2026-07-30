from __future__ import annotations

import pytest

from yonerai_discord.control_plane import (
    CapabilitySpec,
    DecisionCode,
    InMemoryStateStore,
    ModuleSpec,
    Registry,
    RegistryValidationError,
    RbacLevel,
)


def registry_with_core() -> tuple[Registry, InMemoryStateStore]:
    state = InMemoryStateStore()
    registry = Registry(state)
    registry.register_module(ModuleSpec("core"))
    registry.register_capability(CapabilitySpec("system.health", "core", required_level=RbacLevel.EVERYONE))
    return registry, state


def test_minecraft_is_default_off_including_catalog_style_module_id() -> None:
    for module_id in ("minecraft", "gaming.minecraft"):
        registry = Registry()
        registry.register_module(ModuleSpec(module_id))
        registry.register_capability(
            CapabilitySpec(f"{module_id}.status", module_id, required_level=RbacLevel.EVERYONE)
        )
        decision = registry.capability_status(f"{module_id}.status")
        assert not decision.executable
        assert decision.code == DecisionCode.MODULE_DISABLED


def test_guild_override_wins_over_global_and_can_be_removed() -> None:
    registry, state = registry_with_core()
    state.set_capability_override("system.health", False)
    assert not registry.is_capability_enabled("system.health", 100)

    state.set_capability_override("system.health", True, guild_id=100)
    assert registry.is_capability_enabled("system.health", 100)
    assert not registry.is_capability_enabled("system.health", 200)

    state.set_capability_override("system.health", None, guild_id=100)
    assert not registry.is_capability_enabled("system.health", 100)


def test_module_off_always_suppresses_enabled_capability() -> None:
    registry, state = registry_with_core()
    state.set_module_override("core", False, guild_id=100)
    state.set_capability_override("system.health", True, guild_id=100)

    decision = registry.capability_status("system.health", 100)
    assert not decision.executable
    assert decision.code == DecisionCode.MODULE_DISABLED
    assert decision.detail == "core"


def test_unimplemented_capability_cannot_be_enabled_by_override() -> None:
    registry = Registry()
    registry.register_module(ModuleSpec("future"))
    registry.register_capability(CapabilitySpec("future.action", "future", implemented=False))
    registry.state_store.set_capability_override("future.action", True)

    decision = registry.capability_status("future.action")
    assert not decision.executable
    assert decision.code == DecisionCode.CAPABILITY_UNIMPLEMENTED


def test_module_and_capability_dependencies_are_fail_closed() -> None:
    state = InMemoryStateStore()
    registry = Registry(state)
    registry.register_module(ModuleSpec("base"))
    registry.register_module(ModuleSpec("feature", dependencies=("base",)))
    registry.register_capability(CapabilitySpec("base.ready", "base", required_level=RbacLevel.EVERYONE))
    registry.register_capability(
        CapabilitySpec(
            "feature.run",
            "feature",
            dependencies=("base.ready",),
            required_level=RbacLevel.EVERYONE,
        )
    )
    assert registry.is_capability_enabled("feature.run")

    state.set_module_override("base", False)
    decision = registry.capability_status("feature.run")
    assert not decision.executable
    assert decision.code == DecisionCode.DEPENDENCY_UNAVAILABLE


def test_unknown_dependency_and_cycle_are_reported_and_validation_can_raise() -> None:
    registry = Registry()
    registry.register_module(ModuleSpec("broken", dependencies=("absent",)))
    registry.register_capability(CapabilitySpec("broken.run", "broken", required_level=RbacLevel.EVERYONE))
    status = registry.capability_status("broken.run")
    assert not status.executable
    assert status.code == DecisionCode.MISSING_DEPENDENCY
    assert any(issue.code == DecisionCode.MISSING_DEPENDENCY for issue in registry.validate())
    with pytest.raises(RegistryValidationError):
        registry.validate(raise_on_error=True)

    cyclic = Registry()
    cyclic.register_module(ModuleSpec("cycle"))
    cyclic.register_capability(CapabilitySpec("cycle.a", "cycle", dependencies=("cycle.b",)))
    cyclic.register_capability(CapabilitySpec("cycle.b", "cycle", dependencies=("cycle.a",)))
    decision = cyclic.capability_status("cycle.a")
    assert decision.code == DecisionCode.DEPENDENCY_CYCLE


def test_duplicate_registration_and_unknown_parent_are_rejected() -> None:
    registry = Registry()
    registry.register_module(ModuleSpec("core"))
    with pytest.raises(ValueError, match="already registered"):
        registry.register_module(ModuleSpec("CORE"))
    with pytest.raises(ValueError, match="unknown module"):
        registry.register_capability(CapabilitySpec("missing.action", "missing"))
