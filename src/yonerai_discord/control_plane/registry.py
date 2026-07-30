from __future__ import annotations

from dataclasses import dataclass

from .models import (
    AvailabilityDecision,
    CapabilitySpec,
    DecisionCode,
    ModuleSpec,
    RbacLevel,
    normalize_id,
)
from .state import GuildId, InMemoryStateStore, StateStore


@dataclass(frozen=True, slots=True)
class RegistryIssue:
    code: DecisionCode
    subject_id: str
    dependency_id: str


class RegistryValidationError(ValueError):
    def __init__(self, issues: tuple[RegistryIssue, ...]) -> None:
        self.issues = issues
        description = ", ".join(f"{issue.subject_id}:{issue.code.value}:{issue.dependency_id}" for issue in issues)
        super().__init__(f"invalid registry: {description}")


class Registry:
    """module/capabilityの登録と、scopeごとの実行可能性を一元管理する。"""

    def __init__(self, state_store: StateStore | None = None) -> None:
        self.state_store: StateStore = state_store or InMemoryStateStore()
        self._modules: dict[str, ModuleSpec] = {}
        self._capabilities: dict[str, CapabilitySpec] = {}
        self._runtime_availability: dict[str, bool] = {}

    @property
    def modules(self) -> tuple[ModuleSpec, ...]:
        return tuple(self._modules[key] for key in sorted(self._modules))

    @property
    def capabilities(self) -> tuple[CapabilitySpec, ...]:
        return tuple(self._capabilities[key] for key in sorted(self._capabilities))

    def register_module(self, spec: ModuleSpec) -> ModuleSpec:
        if not isinstance(spec, ModuleSpec):
            raise TypeError("spec must be ModuleSpec")
        if spec.module_id in self._modules:
            raise ValueError(f"module already registered: {spec.module_id}")
        self._modules[spec.module_id] = spec
        return spec

    def register_capability(self, spec: CapabilitySpec) -> CapabilitySpec:
        if not isinstance(spec, CapabilitySpec):
            raise TypeError("spec must be CapabilitySpec")
        if spec.capability_id in self._capabilities:
            raise ValueError(f"capability already registered: {spec.capability_id}")
        if spec.module_id not in self._modules:
            raise ValueError(f"unknown module for capability {spec.capability_id}: {spec.module_id}")
        self._capabilities[spec.capability_id] = spec
        return spec

    # 短い別名はmanifest loaderからの利用を想定。
    add_module = register_module
    add_capability = register_capability

    def module(self, module_id: str) -> ModuleSpec:
        normalized = normalize_id(module_id, label="module_id")
        try:
            return self._modules[normalized]
        except KeyError as exc:
            raise KeyError(f"unknown module: {normalized}") from exc

    def capability(self, capability_id: str) -> CapabilitySpec:
        normalized = normalize_id(capability_id, label="capability_id")
        try:
            return self._capabilities[normalized]
        except KeyError as exc:
            raise KeyError(f"unknown capability: {normalized}") from exc

    def configured_module_enabled(self, module_id: str, guild_id: GuildId | None = None) -> bool:
        spec = self.module(module_id)
        override = self._scoped_override(self.state_store.get_module_override, spec.module_id, guild_id)
        return spec.default_enabled if override is None else override

    def configured_capability_enabled(self, capability_id: str, guild_id: GuildId | None = None) -> bool:
        spec = self.capability(capability_id)
        override = self._scoped_override(
            self.state_store.get_capability_override,
            spec.capability_id,
            guild_id,
        )
        return spec.default_enabled if override is None else override

    def set_runtime_availability(self, capability_id: str, available: bool | None) -> None:
        """実際のcommand/listener/worker配線状態を起動後inventoryから反映する。"""

        normalized = normalize_id(capability_id, label="capability_id")
        if normalized not in self._capabilities:
            raise KeyError(f"unknown capability: {normalized}")
        if available is None:
            self._runtime_availability.pop(normalized, None)
            return
        if not isinstance(available, bool):
            raise TypeError("available must be bool or None")
        self._runtime_availability[normalized] = available

    def runtime_available(self, capability_id: str) -> bool | None:
        normalized = normalize_id(capability_id, label="capability_id")
        if normalized not in self._capabilities:
            raise KeyError(f"unknown capability: {normalized}")
        return self._runtime_availability.get(normalized)

    def required_level(self, capability_id: str, guild_id: GuildId | None = None) -> RbacLevel:
        spec = self.capability(capability_id)
        override = self._scoped_override(self.state_store.get_level_override, spec.capability_id, guild_id)
        requested = spec.required_level if override is None else RbacLevel.parse(override)
        return max(requested, spec.safety_floor)

    def module_status(self, module_id: str, guild_id: GuildId | None = None) -> AvailabilityDecision:
        normalized = normalize_id(module_id, label="module_id")
        return self._module_status(normalized, guild_id, path=())

    def capability_status(self, capability_id: str, guild_id: GuildId | None = None) -> AvailabilityDecision:
        normalized = normalize_id(capability_id, label="capability_id")
        return self._capability_status(normalized, guild_id, module_path=(), capability_path=())

    evaluate = capability_status

    def is_module_enabled(self, module_id: str, guild_id: GuildId | None = None) -> bool:
        return self.module_status(module_id, guild_id).executable

    def is_capability_enabled(self, capability_id: str, guild_id: GuildId | None = None) -> bool:
        return self.capability_status(capability_id, guild_id).executable

    def validate(self, *, raise_on_error: bool = False) -> tuple[RegistryIssue, ...]:
        issues: list[RegistryIssue] = []
        for module in self._modules.values():
            for dependency_id in module.dependencies:
                if dependency_id not in self._modules:
                    issues.append(RegistryIssue(DecisionCode.MISSING_DEPENDENCY, module.module_id, dependency_id))
        for capability in self._capabilities.values():
            for dependency_id in capability.dependencies:
                if dependency_id not in self._capabilities:
                    issues.append(
                        RegistryIssue(DecisionCode.MISSING_DEPENDENCY, capability.capability_id, dependency_id)
                    )

        issues.extend(self._cycle_issues(self._modules, lambda spec: spec.dependencies))
        issues.extend(self._cycle_issues(self._capabilities, lambda spec: spec.dependencies))
        result = tuple(issues)
        if result and raise_on_error:
            raise RegistryValidationError(result)
        return result

    def _module_status(
        self,
        module_id: str,
        guild_id: GuildId | None,
        *,
        path: tuple[str, ...],
    ) -> AvailabilityDecision:
        spec = self._modules.get(module_id)
        if spec is None:
            return AvailabilityDecision(False, DecisionCode.UNKNOWN_MODULE, module_id)
        if module_id in path:
            cycle = " -> ".join((*path, module_id))
            return AvailabilityDecision(False, DecisionCode.DEPENDENCY_CYCLE, module_id, cycle)
        if not spec.implemented:
            return AvailabilityDecision(False, DecisionCode.MODULE_UNIMPLEMENTED, module_id)
        if not self.configured_module_enabled(module_id, guild_id):
            return AvailabilityDecision(False, DecisionCode.MODULE_DISABLED, module_id)

        next_path = (*path, module_id)
        for dependency_id in spec.dependencies:
            dependency = self._module_status(dependency_id, guild_id, path=next_path)
            if not dependency.executable:
                code = (
                    DecisionCode.MISSING_DEPENDENCY
                    if dependency.code == DecisionCode.UNKNOWN_MODULE
                    else dependency.code
                    if dependency.code == DecisionCode.DEPENDENCY_CYCLE
                    else DecisionCode.DEPENDENCY_UNAVAILABLE
                )
                return AvailabilityDecision(False, code, module_id, dependency_id)
        return AvailabilityDecision(True, DecisionCode.ALLOWED, module_id)

    def _capability_status(
        self,
        capability_id: str,
        guild_id: GuildId | None,
        *,
        module_path: tuple[str, ...],
        capability_path: tuple[str, ...],
    ) -> AvailabilityDecision:
        spec = self._capabilities.get(capability_id)
        if spec is None:
            return AvailabilityDecision(False, DecisionCode.UNKNOWN_CAPABILITY, capability_id)
        if capability_id in capability_path:
            cycle = " -> ".join((*capability_path, capability_id))
            return AvailabilityDecision(False, DecisionCode.DEPENDENCY_CYCLE, capability_id, cycle)
        if not spec.implemented:
            return AvailabilityDecision(False, DecisionCode.CAPABILITY_UNIMPLEMENTED, capability_id)
        if self._runtime_availability.get(capability_id) is False:
            return AvailabilityDecision(False, DecisionCode.RUNTIME_UNAVAILABLE, capability_id)

        module = self._module_status(spec.module_id, guild_id, path=module_path)
        if not module.executable:
            # 親module OFFは必ずcapabilityの判定理由として保存する。
            return AvailabilityDecision(False, module.code, capability_id, spec.module_id)
        if not self.configured_capability_enabled(capability_id, guild_id):
            return AvailabilityDecision(False, DecisionCode.CAPABILITY_DISABLED, capability_id)

        next_path = (*capability_path, capability_id)
        for dependency_id in spec.dependencies:
            if dependency_id not in self._capabilities:
                return AvailabilityDecision(False, DecisionCode.MISSING_DEPENDENCY, capability_id, dependency_id)
            dependency = self._capability_status(
                dependency_id,
                guild_id,
                module_path=module_path,
                capability_path=next_path,
            )
            if not dependency.executable:
                code = (
                    DecisionCode.DEPENDENCY_CYCLE
                    if dependency.code == DecisionCode.DEPENDENCY_CYCLE
                    else DecisionCode.DEPENDENCY_UNAVAILABLE
                )
                return AvailabilityDecision(False, code, capability_id, dependency_id)
        return AvailabilityDecision(True, DecisionCode.ALLOWED, capability_id)

    @staticmethod
    def _scoped_override(getter: object, subject_id: str, guild_id: GuildId | None) -> object | None:
        if not callable(getter):
            raise TypeError("state store getter must be callable")
        if guild_id is not None:
            guild_value = getter(subject_id, guild_id)
            if guild_value is not None:
                return guild_value
        return getter(subject_id, None)

    @staticmethod
    def _cycle_issues(items: dict[str, object], dependencies: object) -> list[RegistryIssue]:
        if not callable(dependencies):
            raise TypeError("dependencies must be callable")
        issues: list[RegistryIssue] = []
        visited: set[str] = set()
        active: list[str] = []

        def visit(item_id: str) -> None:
            if item_id in active:
                issues.append(RegistryIssue(DecisionCode.DEPENDENCY_CYCLE, active[-1], item_id))
                return
            if item_id in visited or item_id not in items:
                return
            active.append(item_id)
            for dependency_id in dependencies(items[item_id]):
                visit(dependency_id)
            active.pop()
            visited.add(item_id)

        for item_id in items:
            visit(item_id)
        return issues


CapabilityRegistry = Registry
