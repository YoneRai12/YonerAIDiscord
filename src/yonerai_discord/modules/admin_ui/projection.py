from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from yonerai_discord.control_plane import Registry
from yonerai_discord.db import GuildAuditSummaryRecord
from yonerai_discord.deployment_current_truth import M10CurrentTruthV1, build_m10_current_truth


MAX_ADMIN_MODULES = 64
MAX_ADMIN_CAPABILITIES = 1_024
MAX_ADMIN_AUDIT_ROWS = 50


class AdminUiProjectionError(RuntimeError):
    pass


class GuildAuditSummarySource(Protocol):
    def list_guild_audit_summary(
        self,
        guild_id: int | str,
        *,
        limit: int = 50,
    ) -> tuple[GuildAuditSummaryRecord, ...]: ...


@dataclass(frozen=True, slots=True)
class AdminModuleProjection:
    module_id: str
    configured_enabled: bool
    executable: bool
    status_code: str


@dataclass(frozen=True, slots=True)
class AdminCapabilityProjection:
    capability_id: str
    module_id: str
    name: str
    configured_enabled: bool
    executable: bool
    status_code: str
    required_level: str
    runtime_readiness: str


@dataclass(frozen=True, slots=True)
class AdminAuditProjection:
    id: int
    event: str
    plugin: str | None
    actor_id: int | None
    created_at: str


@dataclass(frozen=True, slots=True)
class AdminDeploymentSourceProjection:
    name: str
    configured: bool
    ready: bool
    live_success: str
    blocker: str | None


@dataclass(frozen=True, slots=True)
class AdminDeploymentProjection:
    schema_version: str
    selection_configured: bool
    selected_topology: str | None
    effective_topology: str
    hosting_profile: str | None
    packaging: str | None
    available_ports: tuple[str, ...]
    missing_ports: tuple[str, ...]
    blockers: tuple[str, ...]
    sources: tuple[AdminDeploymentSourceProjection, ...]


@dataclass(frozen=True, slots=True)
class AdminUiProjection:
    guild_id: int
    modules: tuple[AdminModuleProjection, ...]
    capabilities: tuple[AdminCapabilityProjection, ...]
    audit: tuple[AdminAuditProjection, ...]
    deployment: AdminDeploymentProjection


def build_admin_ui_projection(
    *,
    guild_id: int,
    registry: Registry,
    audit_source: GuildAuditSummarySource,
    deployment_truth: M10CurrentTruthV1 | None = None,
) -> AdminUiProjection:
    if isinstance(guild_id, bool) or not isinstance(guild_id, int) or guild_id <= 0:
        raise AdminUiProjectionError("guild_id is invalid")
    if not isinstance(registry, Registry):
        raise TypeError("registry must be a Registry")
    if deployment_truth is not None and type(deployment_truth) is not M10CurrentTruthV1:
        raise TypeError("deployment_truth must be M10CurrentTruthV1")

    module_specs = registry.modules
    capability_specs = registry.capabilities
    if len(module_specs) > MAX_ADMIN_MODULES or len(capability_specs) > MAX_ADMIN_CAPABILITIES:
        raise AdminUiProjectionError("admin UI projection exceeds its fixed bounds")

    modules = tuple(
        AdminModuleProjection(
            module_id=spec.module_id,
            configured_enabled=registry.configured_module_enabled(spec.module_id, guild_id),
            executable=(status := registry.module_status(spec.module_id, guild_id)).executable,
            status_code=status.code.value,
        )
        for spec in module_specs
    )
    capabilities = tuple(
        AdminCapabilityProjection(
            capability_id=spec.capability_id,
            module_id=spec.module_id,
            name=spec.name,
            configured_enabled=registry.configured_capability_enabled(spec.capability_id, guild_id),
            executable=(status := registry.capability_status(spec.capability_id, guild_id)).executable,
            status_code=status.code.value,
            required_level=registry.required_level(spec.capability_id, guild_id).name.lower(),
            runtime_readiness=_runtime_readiness(registry.runtime_available(spec.capability_id)),
        )
        for spec in capability_specs
    )
    audit_rows = audit_source.list_guild_audit_summary(guild_id, limit=MAX_ADMIN_AUDIT_ROWS)
    if len(audit_rows) > MAX_ADMIN_AUDIT_ROWS:
        raise AdminUiProjectionError("admin UI audit projection exceeds its fixed bound")
    audit = tuple(
        AdminAuditProjection(
            id=row.id,
            event=row.event,
            plugin=row.plugin,
            actor_id=row.actor_id,
            created_at=row.created_at,
        )
        for row in audit_rows
    )
    return AdminUiProjection(
        guild_id=guild_id,
        modules=modules,
        capabilities=capabilities,
        audit=audit,
        deployment=_deployment_projection(deployment_truth or build_m10_current_truth()),
    )


def _runtime_readiness(value: bool | None) -> str:
    if value is True:
        return "ready"
    if value is False:
        return "unavailable"
    return "unknown"


def _deployment_projection(truth: M10CurrentTruthV1) -> AdminDeploymentProjection:
    return AdminDeploymentProjection(
        schema_version=truth.schema_version,
        selection_configured=truth.selection_configured,
        selected_topology=None if truth.selected_topology is None else truth.selected_topology.value,
        effective_topology=truth.effective_topology.value,
        hosting_profile=None if truth.selected_hosting_profile is None else truth.selected_hosting_profile.value,
        packaging=None if truth.selected_packaging is None else truth.selected_packaging.value,
        available_ports=truth.available_ports,
        missing_ports=truth.missing_ports,
        blockers=truth.blockers,
        sources=tuple(
            AdminDeploymentSourceProjection(
                name=name,
                configured=source.configured,
                ready=source.ready,
                live_success=_live_success(source.live_success),
                blocker=source.blocker,
            )
            for name, source in (
                ("provider", truth.provider_source),
                ("sandbox", truth.sandbox_source),
                ("jobs", truth.jobs_source),
                ("audit", truth.audit_source),
            )
        ),
    )


def _live_success(value: bool | None) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


__all__ = [
    "MAX_ADMIN_AUDIT_ROWS",
    "MAX_ADMIN_CAPABILITIES",
    "MAX_ADMIN_MODULES",
    "AdminAuditProjection",
    "AdminCapabilityProjection",
    "AdminDeploymentProjection",
    "AdminDeploymentSourceProjection",
    "AdminModuleProjection",
    "AdminUiProjection",
    "AdminUiProjectionError",
    "GuildAuditSummarySource",
    "build_admin_ui_projection",
]
