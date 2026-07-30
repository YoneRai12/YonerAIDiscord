from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from .capabilities import COMMAND_CAPABILITIES, COMMAND_PLUGIN_BY_ROOT, EVENT_CAPABILITIES
from .control_plane import Registry
from .plugin import PluginManager
from .runtime_manifest import RUNTIME_CAPABILITIES


class SurfaceInventoryError(RuntimeError):
    """中央policyへ未登録のDiscord surfaceを同期前に停止する。"""


@dataclass(frozen=True, slots=True)
class SurfaceInventoryReport:
    actual_command_paths: frozenset[str]
    missing_command_paths: frozenset[str]
    unmapped_command_paths: frozenset[str]
    available_capability_ids: frozenset[str]
    unavailable_capability_ids: frozenset[str]


def command_paths_from_tree(tree: Any) -> frozenset[str]:
    paths: set[str] = set()

    def visit(command: Any, parents: tuple[str, ...] = ()) -> None:
        name = str(getattr(command, "name", "")).strip().lower()
        if not name:
            raise SurfaceInventoryError("application command without a name")
        current = (*parents, name)
        children = tuple(getattr(command, "commands", ()) or ())
        if children:
            for child in children:
                visit(child, current)
            return
        paths.add(" ".join(current))

    for command in tree.get_commands():
        visit(command)
    return frozenset(paths)


def reconcile_runtime_surfaces(
    registry: Registry,
    tree: Any,
    plugins: PluginManager,
) -> SurfaceInventoryReport:
    """実tree・listener owner・plugin状態をRegistryへ反映する。"""

    actual = command_paths_from_tree(tree)
    expected = frozenset(COMMAND_CAPABILITIES)
    unmapped = actual - expected
    if unmapped:
        raise SurfaceInventoryError("unmapped Discord command surfaces: " + ", ".join(sorted(unmapped)))

    paths_by_capability: dict[str, set[str]] = defaultdict(set)
    for path, capability_id in COMMAND_CAPABILITIES.items():
        paths_by_capability[capability_id].add(path)

    available: set[str] = set()
    unavailable: set[str] = set()
    for capability_id, paths in paths_by_capability.items():
        command_ready = all(path in actual for path in paths)
        plugin_ready = all(_plugin_ready(path, plugins) for path in paths)
        ready = command_ready and plugin_ready and _capability_ready_by_service(capability_id, tree)
        registry.set_runtime_availability(capability_id, ready)
        (available if ready else unavailable).add(capability_id)

    event_plugins = {
        event_name: definition.plugin for definition in RUNTIME_CAPABILITIES for event_name in definition.event_names
    }
    for event_name, capability_id in EVENT_CAPABILITIES.items():
        plugin_name = event_plugins.get(event_name)
        ready = (
            bool(plugin_name)
            and plugins.is_running(plugin_name)
            and _event_enabled_by_settings(event_name, tree)
            and _capability_ready_by_service(capability_id, tree)
        )
        registry.set_runtime_availability(capability_id, ready)
        (available if ready else unavailable).add(capability_id)

    # command/eventを持たないmixerやworkerも、pluginが明示したreadinessを
    # Registryへ反映する。未知IDはtypoのままfail-openさせず起動時に止める。
    client = getattr(tree, "client", None)
    service_readiness = getattr(client, "runtime_capability_readiness", None)
    if isinstance(service_readiness, dict):
        bound_capability_ids = set(paths_by_capability) | set(EVENT_CAPABILITIES.values())
        plugin_by_capability = {definition.capability_id: definition.plugin for definition in RUNTIME_CAPABILITIES}
        for capability_id, ready in service_readiness.items():
            try:
                registry.capability(capability_id)
            except KeyError as exc:
                raise SurfaceInventoryError(f"unknown runtime readiness capability: {capability_id}") from exc
            if not isinstance(ready, bool):
                raise SurfaceInventoryError(f"invalid runtime readiness value: {capability_id}")
            plugin_name = plugin_by_capability.get(capability_id)
            plugin_ready = plugin_name is None or plugins.is_running(plugin_name)
            if capability_id in bound_capability_ids:
                ready = ready and registry.runtime_available(capability_id) is True
            else:
                ready = ready and plugin_ready
            registry.set_runtime_availability(capability_id, ready)
            (available if ready else unavailable).add(capability_id)

    return SurfaceInventoryReport(
        actual_command_paths=actual,
        missing_command_paths=expected - actual,
        unmapped_command_paths=unmapped,
        available_capability_ids=frozenset(available),
        unavailable_capability_ids=frozenset(unavailable),
    )


def _plugin_ready(command_path: str, plugins: PluginManager) -> bool:
    root = command_path.split(" ", 1)[0]
    plugin_name = COMMAND_PLUGIN_BY_ROOT.get(root)
    return plugin_name is None or plugins.is_running(plugin_name)


def _event_enabled_by_settings(event_name: str, tree: Any) -> bool:
    client = getattr(tree, "client", None)
    settings = getattr(client, "settings", None)
    if event_name in {"member_join", "member_remove"}:
        return bool(getattr(settings, "member_events_enabled", False))
    if event_name in {"message_delete", "message_edit"}:
        return bool(getattr(settings, "message_audit_events_enabled", False))
    if event_name in {"automod_message_create", "automod_message_edit"}:
        return bool(getattr(settings, "automod_enabled", False))
    if event_name == "ai_mention_message":
        return bool(getattr(settings, "ai_mention_enabled", False))
    if event_name == "music_read_aloud_message":
        return bool(getattr(settings, "music_read_aloud_enabled", False))
    return True


def _capability_ready_by_service(capability_id: str, tree: Any) -> bool:
    client = getattr(tree, "client", None)
    readiness = getattr(client, "runtime_capability_readiness", None)
    if not isinstance(readiness, dict):
        return True
    value = readiness.get(capability_id, True)
    return value if isinstance(value, bool) else False
