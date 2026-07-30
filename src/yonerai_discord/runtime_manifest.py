from __future__ import annotations

from .control_plane import Registry
from .runtime_manifests import (
    RUNTIME_CAPABILITIES as _RUNTIME_CAPABILITIES,
    RUNTIME_MODULES as _RUNTIME_MODULES,
)
from .runtime_manifests.types import (
    RuntimeCapabilityDefinition,
    RuntimeModuleDefinition,
    _cap as _cap,
)


RUNTIME_MODULES: tuple[RuntimeModuleDefinition, ...] = _RUNTIME_MODULES
RUNTIME_CAPABILITIES: tuple[RuntimeCapabilityDefinition, ...] = _RUNTIME_CAPABILITIES


def _unique_mapping(attribute: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for definition in RUNTIME_CAPABILITIES:
        for key in getattr(definition, attribute):
            normalized = key.strip().lower()
            if normalized in result:
                raise RuntimeError(f"duplicate runtime manifest binding: {normalized}")
            result[normalized] = definition.capability_id
    return result


RUNTIME_COMMAND_CAPABILITIES = _unique_mapping("command_paths")
RUNTIME_EVENT_CAPABILITIES = _unique_mapping("event_names")


def register_runtime_capabilities(registry: Registry) -> None:
    for definition in RUNTIME_CAPABILITIES:
        registry.register_capability(definition.to_spec())


def register_runtime_modules(registry: Registry) -> None:
    for definition in RUNTIME_MODULES:
        registry.register_module(definition.to_spec())


__all__ = [
    "RUNTIME_CAPABILITIES",
    "RUNTIME_COMMAND_CAPABILITIES",
    "RUNTIME_EVENT_CAPABILITIES",
    "RUNTIME_MODULES",
    "RuntimeCapabilityDefinition",
    "RuntimeModuleDefinition",
    "register_runtime_capabilities",
    "register_runtime_modules",
]
