from __future__ import annotations

from dataclasses import dataclass

from ..control_plane import CapabilitySpec, ModuleSpec, RbacLevel, RiskLevel


@dataclass(frozen=True, slots=True)
class RuntimeModuleDefinition:
    """旧カタログより細かい、実行時に独立してON/OFFできるモジュール。"""

    module_id: str
    default_enabled: bool = True
    dependencies: tuple[str, ...] = ()

    def to_spec(self) -> ModuleSpec:
        return ModuleSpec(
            module_id=self.module_id,
            default_enabled=self.default_enabled,
            implemented=True,
            dependencies=self.dependencies,
        )


@dataclass(frozen=True, slots=True)
class RuntimeCapabilityDefinition:
    """このsuiteで新規実装した、旧canonical台帳を水増ししないruntime能力。"""

    capability_id: str
    module_id: str
    name: str
    command_paths: tuple[str, ...] = ()
    event_names: tuple[str, ...] = ()
    plugin: str | None = None
    level: RbacLevel = RbacLevel.EVERYONE
    risk: RiskLevel = RiskLevel.LOW
    default_enabled: bool = True
    owner_only: bool = False
    dependencies: tuple[str, ...] = ()

    def to_spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            capability_id=self.capability_id,
            module_id=self.module_id,
            name=self.name,
            source_state="runtime",
            default_enabled=self.default_enabled,
            implemented=True,
            required_level=self.level,
            minimum_level=self.level,
            risk=self.risk,
            owner_only=self.owner_only,
            dependencies=self.dependencies,
        )


def _cap(
    capability_id: str,
    module_id: str,
    name: str,
    *,
    command: str | None = None,
    event: str | None = None,
    plugin: str | None = None,
    level: RbacLevel = RbacLevel.EVERYONE,
    risk: RiskLevel = RiskLevel.LOW,
    default_enabled: bool = True,
    owner_only: bool = False,
    dependencies: tuple[str, ...] = (),
) -> RuntimeCapabilityDefinition:
    return RuntimeCapabilityDefinition(
        capability_id=capability_id,
        module_id=module_id,
        name=name,
        command_paths=() if command is None else (command,),
        event_names=() if event is None else (event,),
        plugin=plugin,
        level=level,
        risk=risk,
        default_enabled=default_enabled,
        owner_only=owner_only,
        dependencies=dependencies,
    )
