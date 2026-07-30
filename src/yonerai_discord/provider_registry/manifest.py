from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from yonerai_discord.control_plane import RbacLevel, RiskLevel

from .domain import (
    CapabilityPolicy,
    LogicalCapability,
    ModelLoadPolicy,
    OffloadPolicy,
    ProviderKind,
    QualityTier,
    ResourceProfile,
    ResourceTarget,
    SecretReference,
    SettingReference,
    TimeoutPolicy,
    normalize_identifier,
)


class ManifestValidationError(ValueError):
    pass


class ModelMaturity(StrEnum):
    STABLE = "stable"
    PREVIEW = "preview"
    EXPERIMENTAL = "experimental"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ModelBinding:
    alias: str
    provider_model: str
    resources: ResourceProfile | None = None
    maturity: ModelMaturity = ModelMaturity.UNKNOWN
    license_id: str = "verify-required"
    probe_required: bool = True

    def __post_init__(self) -> None:
        alias = normalize_identifier(self.alias, label="model alias")
        if not isinstance(self.provider_model, str):
            raise TypeError("provider_model must be a string")
        provider_model = self.provider_model.strip()
        if not provider_model or len(provider_model) > 256 or any(character.isspace() for character in provider_model):
            raise ValueError("provider_model must be a non-empty model identifier")
        object.__setattr__(self, "alias", alias)
        object.__setattr__(self, "provider_model", provider_model)
        if self.resources is not None and not isinstance(self.resources, ResourceProfile):
            raise TypeError("model resources must be a ResourceProfile")
        object.__setattr__(self, "maturity", ModelMaturity(self.maturity))
        if (
            not isinstance(self.license_id, str)
            or not self.license_id.strip()
            or len(self.license_id) > 128
            or any(character.isspace() for character in self.license_id)
        ):
            raise ValueError("license_id must be a compact license identifier")
        if not isinstance(self.probe_required, bool):
            raise TypeError("probe_required must be a boolean")
        object.__setattr__(self, "license_id", self.license_id.strip())


@dataclass(frozen=True, slots=True)
class AliasBinding:
    source: str
    target: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", normalize_identifier(self.source, label="alias source"))
        object.__setattr__(self, "target", normalize_identifier(self.target, label="alias target"))
        if self.source == self.target:
            raise ValueError("compatibility alias must point to a different identifier")


@dataclass(frozen=True, slots=True)
class ProviderManifest:
    provider_id: str
    kind: ProviderKind
    adapter_id: str
    capabilities: tuple[LogicalCapability, ...]
    resources: ResourceProfile
    enabled: bool = False
    models: tuple[ModelBinding, ...] = ()
    secret_refs: tuple[SecretReference, ...] = ()
    setting_refs: tuple[SettingReference, ...] = ()
    timeouts: TimeoutPolicy = TimeoutPolicy()

    def __post_init__(self) -> None:
        provider_id = normalize_identifier(self.provider_id, label="provider_id")
        adapter_id = normalize_identifier(self.adapter_id, label="adapter_id")
        kind = ProviderKind(self.kind)
        capabilities = tuple(LogicalCapability(value) for value in self.capabilities)
        if not capabilities or len(set(capabilities)) != len(capabilities):
            raise ValueError("provider capabilities must be non-empty and unique")
        models = tuple(self.models)
        if any(not isinstance(model, ModelBinding) for model in models):
            raise TypeError("models must contain ModelBinding values")
        if len({model.alias for model in models}) != len(models):
            raise ValueError("model aliases must be unique within a provider")
        secret_refs = tuple(self.secret_refs)
        setting_refs = tuple(self.setting_refs)
        if len({str(ref) for ref in secret_refs}) != len(secret_refs):
            raise ValueError("secret_refs must be unique")
        if len({str(ref) for ref in setting_refs}) != len(setting_refs):
            raise ValueError("setting_refs must be unique")
        if not isinstance(self.timeouts, TimeoutPolicy):
            raise TypeError("timeouts must be a TimeoutPolicy")
        if not isinstance(self.resources, ResourceProfile):
            raise TypeError("resources must be a ResourceProfile")
        if kind is ProviderKind.API and self.resources.target is not ResourceTarget.REMOTE:
            raise ValueError("API providers must use remote resources")
        if kind is ProviderKind.LOCAL and self.resources.target is ResourceTarget.REMOTE:
            raise ValueError("local providers must use CPU or CUDA resources")
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "adapter_id", adapter_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "models", models)
        object.__setattr__(self, "secret_refs", secret_refs)
        object.__setattr__(self, "setting_refs", setting_refs)

    def model_for(self, alias: str) -> str | None:
        normalized = normalize_identifier(alias, label="model alias")
        for binding in self.models:
            if binding.alias == normalized:
                return binding.provider_model
        return None

    def model_binding(self, alias: str) -> ModelBinding | None:
        normalized = normalize_identifier(alias, label="model alias")
        return next((binding for binding in self.models if binding.alias == normalized), None)

    def resources_for(self, alias: str | None) -> ResourceProfile:
        if alias is not None:
            normalized = normalize_identifier(alias, label="model alias")
            for binding in self.models:
                if binding.alias == normalized and binding.resources is not None:
                    return binding.resources
        return self.resources


@dataclass(frozen=True, slots=True)
class TierRoute:
    tier: QualityTier
    provider_ids: tuple[str, ...]
    model_alias: str | None = None

    def __post_init__(self) -> None:
        tier = QualityTier(self.tier)
        provider_ids = tuple(normalize_identifier(value, label="route provider_id") for value in self.provider_ids)
        if len(set(provider_ids)) != len(provider_ids):
            raise ValueError("route provider_ids must be unique")
        model_alias = None
        if self.model_alias is not None:
            model_alias = normalize_identifier(self.model_alias, label="model_alias")
        object.__setattr__(self, "tier", tier)
        object.__setattr__(self, "provider_ids", provider_ids)
        object.__setattr__(self, "model_alias", model_alias)


@dataclass(frozen=True, slots=True)
class CapabilityRoute:
    capability: LogicalCapability
    tiers: tuple[TierRoute, ...]
    default_tier: QualityTier = QualityTier.BALANCED

    def __post_init__(self) -> None:
        capability = LogicalCapability(self.capability)
        tiers = tuple(self.tiers)
        if any(not isinstance(route, TierRoute) for route in tiers):
            raise TypeError("tiers must contain TierRoute values")
        if {route.tier for route in tiers} != set(QualityTier):
            raise ValueError("every capability route must define fast, balanced, and quality tiers")
        default_tier = QualityTier(self.default_tier)
        object.__setattr__(self, "capability", capability)
        object.__setattr__(self, "tiers", tiers)
        object.__setattr__(self, "default_tier", default_tier)

    def tier_route(self, tier: QualityTier | str) -> TierRoute:
        normalized = QualityTier(tier)
        return next(item for item in self.tiers if item.tier is normalized)


@dataclass(frozen=True, slots=True)
class ProviderCatalogManifest:
    schema_version: int
    module_id: str
    capabilities: tuple[CapabilityPolicy, ...]
    providers: tuple[ProviderManifest, ...]
    routes: tuple[CapabilityRoute, ...]
    compatibility_aliases: tuple[AliasBinding, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ManifestValidationError("only provider catalog schema_version=1 is supported")
        module_id = normalize_identifier(self.module_id, label="module_id")
        capabilities = tuple(self.capabilities)
        providers = tuple(self.providers)
        routes = tuple(self.routes)
        aliases = tuple(self.compatibility_aliases)
        _ensure_unique((item.capability.value for item in capabilities), "capability policies")
        _ensure_unique((item.provider_id for item in providers), "providers")
        _ensure_unique((item.capability.value for item in routes), "routes")
        _ensure_unique((item.source for item in aliases), "compatibility aliases")
        if any(not isinstance(item, CapabilityPolicy) for item in capabilities):
            raise TypeError("capabilities must contain CapabilityPolicy values")
        if any(not isinstance(item, ProviderManifest) for item in providers):
            raise TypeError("providers must contain ProviderManifest values")
        if any(not isinstance(item, CapabilityRoute) for item in routes):
            raise TypeError("routes must contain CapabilityRoute values")
        if any(not isinstance(item, AliasBinding) for item in aliases):
            raise TypeError("compatibility_aliases must contain AliasBinding values")

        policy_ids = {item.capability for item in capabilities}
        provider_by_id = {item.provider_id: item for item in providers}
        for route in routes:
            if route.capability not in policy_ids:
                raise ManifestValidationError(f"route has no capability policy: {route.capability.value}")
            for tier_route in route.tiers:
                for provider_id in tier_route.provider_ids:
                    provider = provider_by_id.get(provider_id)
                    if provider is None:
                        raise ManifestValidationError(f"route references an unknown provider: {provider_id}")
                    if route.capability not in provider.capabilities:
                        raise ManifestValidationError(
                            f"provider {provider_id} does not declare {route.capability.value}"
                        )
                    if tier_route.model_alias is not None and provider.model_for(tier_route.model_alias) is None:
                        raise ManifestValidationError(
                            f"provider {provider_id} does not bind model alias {tier_route.model_alias}"
                        )

        alias_map = {item.source: item.target for item in aliases}
        for source in alias_map:
            current = source
            visited: set[str] = set()
            while current in alias_map:
                if current in visited:
                    raise ManifestValidationError("compatibility model aliases contain a cycle")
                visited.add(current)
                current = alias_map[current]

        object.__setattr__(self, "module_id", module_id)
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "providers", providers)
        object.__setattr__(self, "routes", routes)
        object.__setattr__(self, "compatibility_aliases", aliases)

    def capability_policy(self, capability: LogicalCapability | str) -> CapabilityPolicy | None:
        normalized = LogicalCapability(capability)
        return next((item for item in self.capabilities if item.capability is normalized), None)

    def provider(self, provider_id: str) -> ProviderManifest | None:
        normalized = normalize_identifier(provider_id, label="provider_id")
        return next((item for item in self.providers if item.provider_id == normalized), None)

    def route(self, capability: LogicalCapability | str) -> CapabilityRoute | None:
        normalized = LogicalCapability(capability)
        return next((item for item in self.routes if item.capability is normalized), None)

    def canonical_model_alias(self, alias: str) -> str:
        normalized = normalize_identifier(alias, label="model alias")
        bindings = {item.source: item.target for item in self.compatibility_aliases}
        visited: set[str] = set()
        while normalized in bindings:
            if normalized in visited:
                raise ManifestValidationError("compatibility model aliases contain a cycle")
            visited.add(normalized)
            normalized = bindings[normalized]
        return normalized

    def to_public_mapping(self) -> dict[str, Any]:
        """Secret値を含めず、参照名だけをdoctor/WebUIへ渡せる表現。"""

        return {
            "schema_version": self.schema_version,
            "module_id": self.module_id,
            "capabilities": [
                {
                    "id": item.capability.value,
                    "default_enabled": item.default_enabled,
                    "required_rbac": item.required_rbac.name.lower(),
                    "risk": item.risk.name.lower(),
                    "requires_consent": item.requires_consent,
                    "requires_confirmation": item.requires_confirmation,
                    "audit_required": item.audit_required,
                }
                for item in self.capabilities
            ],
            "providers": [
                {
                    "id": item.provider_id,
                    "kind": item.kind.value,
                    "adapter_id": item.adapter_id,
                    "enabled": item.enabled,
                    "capabilities": [capability.value for capability in item.capabilities],
                    "models": [
                        {
                            "alias": model.alias,
                            "provider_model": model.provider_model,
                            "maturity": model.maturity.value,
                            "license_id": model.license_id,
                            "probe_required": model.probe_required,
                            **(
                                {"resources": _resource_mapping(model.resources)} if model.resources is not None else {}
                            ),
                        }
                        for model in item.models
                    ],
                    "secret_refs": [str(ref) for ref in item.secret_refs],
                    "setting_refs": [str(ref) for ref in item.setting_refs],
                    "timeouts": {
                        "request_seconds": item.timeouts.request_seconds,
                        "health_seconds": item.timeouts.health_seconds,
                    },
                    "resources": _resource_mapping(item.resources),
                }
                for item in self.providers
            ],
            "routes": [
                {
                    "capability": item.capability.value,
                    "default_tier": item.default_tier.value,
                    "tiers": [
                        {
                            "tier": tier.tier.value,
                            "provider_ids": list(tier.provider_ids),
                            "model_alias": tier.model_alias,
                        }
                        for tier in item.tiers
                    ],
                }
                for item in self.routes
            ],
            "compatibility_aliases": [
                {"source": item.source, "target": item.target} for item in self.compatibility_aliases
            ],
        }

    @property
    def content_revision(self) -> str:
        """Return a deterministic SHA-256 of the complete non-secret manifest."""

        encoded = json.dumps(
            self.to_public_mapping(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> ProviderCatalogManifest:
        _require_keys(
            raw,
            required={"schema_version", "module_id", "capabilities", "providers", "routes"},
            optional={"compatibility_aliases"},
            label="catalog",
        )
        return cls(
            schema_version=_required_int(raw, "schema_version"),
            module_id=_required_str(raw, "module_id"),
            capabilities=tuple(_parse_capability(_mapping(item, "capability")) for item in _list(raw, "capabilities")),
            providers=tuple(_parse_provider(_mapping(item, "provider")) for item in _list(raw, "providers")),
            routes=tuple(_parse_route(_mapping(item, "route")) for item in _list(raw, "routes")),
            compatibility_aliases=tuple(
                _parse_alias(_mapping(item, "compatibility alias"))
                for item in _list(raw, "compatibility_aliases", default=[])
            ),
        )


def load_catalog_manifest(path: str | Path) -> ProviderCatalogManifest:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ManifestValidationError("provider catalog root must be an object")
    return ProviderCatalogManifest.from_mapping(raw)


def _parse_capability(raw: Mapping[str, Any]) -> CapabilityPolicy:
    _require_keys(
        raw,
        required={"id", "default_enabled", "required_rbac", "risk"},
        optional={"requires_consent", "requires_confirmation", "audit_required"},
        label="capability",
    )
    return CapabilityPolicy(
        capability=LogicalCapability(_required_str(raw, "id")),
        default_enabled=_required_bool(raw, "default_enabled"),
        required_rbac=RbacLevel.parse(_required_str(raw, "required_rbac")),
        risk=RiskLevel.parse(_required_str(raw, "risk")),
        requires_consent=_optional_bool(raw, "requires_consent", False),
        requires_confirmation=_optional_bool(raw, "requires_confirmation", False),
        audit_required=_optional_bool(raw, "audit_required", True),
    )


def _parse_provider(raw: Mapping[str, Any]) -> ProviderManifest:
    _require_keys(
        raw,
        required={"id", "kind", "adapter_id", "capabilities", "enabled", "resources"},
        optional={"models", "secret_refs", "setting_refs", "timeouts"},
        label="provider",
    )
    timeout_raw = _mapping(raw.get("timeouts", {}), "timeouts")
    _require_keys(timeout_raw, required=set(), optional={"request_seconds", "health_seconds"}, label="timeouts")
    return ProviderManifest(
        provider_id=_required_str(raw, "id"),
        kind=ProviderKind(_required_str(raw, "kind")),
        adapter_id=_required_str(raw, "adapter_id"),
        capabilities=tuple(LogicalCapability(value) for value in _string_list(raw, "capabilities")),
        resources=_parse_resources(_mapping(raw["resources"], "resources")),
        enabled=_required_bool(raw, "enabled"),
        models=tuple(_parse_model(_mapping(item, "model")) for item in _list(raw, "models", default=[])),
        secret_refs=tuple(SecretReference.parse(value) for value in _string_list(raw, "secret_refs", default=[])),
        setting_refs=tuple(SettingReference.parse(value) for value in _string_list(raw, "setting_refs", default=[])),
        timeouts=TimeoutPolicy(
            request_seconds=_number(timeout_raw, "request_seconds", 60.0),
            health_seconds=_number(timeout_raw, "health_seconds", 5.0),
        ),
    )


def _parse_model(raw: Mapping[str, Any]) -> ModelBinding:
    _require_keys(
        raw,
        required={"alias", "provider_model"},
        optional={"resources", "maturity", "license_id", "probe_required"},
        label="model",
    )
    resources = raw.get("resources")
    return ModelBinding(
        _required_str(raw, "alias"),
        _required_str(raw, "provider_model"),
        None if resources is None else _parse_resources(_mapping(resources, "model resources")),
        ModelMaturity(raw.get("maturity", ModelMaturity.UNKNOWN.value)),
        raw.get("license_id", "verify-required"),
        _optional_bool(raw, "probe_required", True),
    )


def _parse_route(raw: Mapping[str, Any]) -> CapabilityRoute:
    _require_keys(
        raw,
        required={"capability", "tiers"},
        optional={"default_tier"},
        label="route",
    )
    return CapabilityRoute(
        capability=LogicalCapability(_required_str(raw, "capability")),
        tiers=tuple(_parse_tier_route(_mapping(item, "tier route")) for item in _list(raw, "tiers")),
        default_tier=QualityTier(raw.get("default_tier", QualityTier.BALANCED.value)),
    )


def _parse_tier_route(raw: Mapping[str, Any]) -> TierRoute:
    _require_keys(
        raw,
        required={"tier", "provider_ids"},
        optional={"model_alias"},
        label="tier route",
    )
    alias = raw.get("model_alias")
    if alias is not None and not isinstance(alias, str):
        raise ManifestValidationError("model_alias must be a string or null")
    return TierRoute(
        tier=QualityTier(_required_str(raw, "tier")),
        provider_ids=tuple(_string_list(raw, "provider_ids")),
        model_alias=alias,
    )


def _parse_alias(raw: Mapping[str, Any]) -> AliasBinding:
    _require_keys(raw, required={"source", "target"}, optional=set(), label="compatibility alias")
    return AliasBinding(_required_str(raw, "source"), _required_str(raw, "target"))


def _parse_resources(raw: Mapping[str, Any]) -> ResourceProfile:
    _require_keys(
        raw,
        required={"target", "max_concurrency", "load_policy", "offload_policy", "exclusive_gpu_lease"},
        optional={"vram_budget_mb", "system_ram_budget_mb", "idle_unload_seconds", "device_ref"},
        label="resources",
    )
    device_ref = raw.get("device_ref")
    if device_ref is not None and not isinstance(device_ref, str):
        raise ManifestValidationError("device_ref must be a setting reference string or null")
    return ResourceProfile(
        target=ResourceTarget(_required_str(raw, "target")),
        max_concurrency=_required_int(raw, "max_concurrency"),
        vram_budget_mb=_optional_int(raw, "vram_budget_mb"),
        system_ram_budget_mb=_optional_int(raw, "system_ram_budget_mb"),
        load_policy=ModelLoadPolicy(_required_str(raw, "load_policy")),
        idle_unload_seconds=_optional_int(raw, "idle_unload_seconds"),
        offload_policy=OffloadPolicy(_required_str(raw, "offload_policy")),
        exclusive_gpu_lease=_required_bool(raw, "exclusive_gpu_lease"),
        device_ref=None if device_ref is None else SettingReference.parse(device_ref),
    )


def _resource_mapping(resources: ResourceProfile) -> dict[str, Any]:
    return {
        "target": resources.target.value,
        "max_concurrency": resources.max_concurrency,
        "vram_budget_mb": resources.vram_budget_mb,
        "system_ram_budget_mb": resources.system_ram_budget_mb,
        "load_policy": resources.load_policy.value,
        "idle_unload_seconds": resources.idle_unload_seconds,
        "offload_policy": resources.offload_policy.value,
        "exclusive_gpu_lease": resources.exclusive_gpu_lease,
        "device_ref": None if resources.device_ref is None else str(resources.device_ref),
    }


def _ensure_unique(values: Any, label: str) -> None:
    materialized = tuple(values)
    if len(set(materialized)) != len(materialized):
        raise ManifestValidationError(f"{label} must be unique")


def _require_keys(raw: Mapping[str, Any], *, required: set[str], optional: set[str], label: str) -> None:
    missing = required - set(raw)
    unknown = set(raw) - required - optional
    if missing:
        raise ManifestValidationError(f"{label} is missing keys: {', '.join(sorted(missing))}")
    if unknown:
        raise ManifestValidationError(f"{label} contains unknown keys: {', '.join(sorted(unknown))}")


def _required_str(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ManifestValidationError(f"{key} must be a string")
    return value


def _required_int(raw: Mapping[str, Any], key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestValidationError(f"{key} must be an integer")
    return value


def _optional_int(raw: Mapping[str, Any], key: str) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestValidationError(f"{key} must be an integer or null")
    return value


def _required_bool(raw: Mapping[str, Any], key: str) -> bool:
    value = raw.get(key)
    if not isinstance(value, bool):
        raise ManifestValidationError(f"{key} must be a boolean")
    return value


def _optional_bool(raw: Mapping[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ManifestValidationError(f"{key} must be a boolean")
    return value


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestValidationError(f"{label} must be an object")
    return value


def _list(raw: Mapping[str, Any], key: str, *, default: list[Any] | None = None) -> list[Any]:
    value = raw.get(key, default)
    if not isinstance(value, list):
        raise ManifestValidationError(f"{key} must be an array")
    return value


def _string_list(raw: Mapping[str, Any], key: str, *, default: list[str] | None = None) -> list[str]:
    values = _list(raw, key, default=default)
    if any(not isinstance(value, str) for value in values):
        raise ManifestValidationError(f"{key} must contain strings")
    return values


def _number(raw: Mapping[str, Any], key: str, default: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManifestValidationError(f"{key} must be numeric")
    return float(value)
