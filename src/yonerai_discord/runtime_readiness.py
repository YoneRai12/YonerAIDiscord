from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


RuntimeReadinessProbe = Callable[[], bool]
_PROBES_ATTRIBUTE = "runtime_capability_readiness_probes"


def publish_runtime_readiness(bot: Any, values: Mapping[str, bool]) -> None:
    """pluginのcommand登録と外部service readinessを分けて公開する。"""

    readiness = getattr(bot, "runtime_capability_readiness", None)
    if readiness is None:
        readiness = {}
        setattr(bot, "runtime_capability_readiness", readiness)
    if not isinstance(readiness, dict):
        raise TypeError("runtime_capability_readiness must be a dict")
    for capability_id, ready in values.items():
        if not isinstance(capability_id, str) or not capability_id:
            raise ValueError("capability_id must be a non-empty string")
        if not isinstance(ready, bool):
            raise TypeError("runtime readiness must be bool")
        readiness[capability_id] = ready
        registry = getattr(bot, "capability_registry", None)
        setter = getattr(registry, "set_runtime_availability", None)
        if callable(setter):
            setter(capability_id, ready)


def publish_runtime_readiness_probe(
    bot: Any,
    capability_id: str,
    probe: RuntimeReadinessProbe,
) -> bool:
    """同期probeを登録し、現在値をcontrol planeへ即時反映する。"""

    if not isinstance(capability_id, str) or not capability_id:
        raise ValueError("capability_id must be a non-empty string")
    if not callable(probe):
        raise TypeError("runtime readiness probe must be callable")
    probes = getattr(bot, _PROBES_ATTRIBUTE, None)
    if probes is None:
        probes = {}
        setattr(bot, _PROBES_ATTRIBUTE, probes)
    if not isinstance(probes, dict):
        raise TypeError("runtime_capability_readiness_probes must be a dict")
    probes[capability_id] = probe
    return refresh_runtime_readiness(bot, capability_id) is True


def refresh_runtime_readiness(bot: Any, capability_id: str) -> bool | None:
    """登録済みprobeだけを再評価する。未登録capabilityの既存値は変更しない。"""

    probes = getattr(bot, _PROBES_ATTRIBUTE, None)
    if not isinstance(probes, dict):
        return None
    probe = probes.get(capability_id)
    if not callable(probe):
        return None
    try:
        ready = probe() is True
    except Exception:
        ready = False
    publish_runtime_readiness(bot, {capability_id: ready})
    return ready


def withdraw_runtime_readiness(bot: Any, capability_ids: tuple[str, ...]) -> None:
    probes = getattr(bot, _PROBES_ATTRIBUTE, None)
    if isinstance(probes, dict):
        for capability_id in capability_ids:
            probes.pop(capability_id, None)
        if not probes:
            delattr(bot, _PROBES_ATTRIBUTE)
    readiness = getattr(bot, "runtime_capability_readiness", None)
    if not isinstance(readiness, dict):
        return
    for capability_id in capability_ids:
        readiness.pop(capability_id, None)
        registry = getattr(bot, "capability_registry", None)
        setter = getattr(registry, "set_runtime_availability", None)
        if callable(setter):
            setter(capability_id, False)


__all__ = [
    "RuntimeReadinessProbe",
    "publish_runtime_readiness",
    "publish_runtime_readiness_probe",
    "refresh_runtime_readiness",
    "withdraw_runtime_readiness",
]
