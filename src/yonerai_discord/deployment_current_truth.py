from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from yonerai_discord.modules.ai.execution_profiles import (
    ExecutionPort,
    ExecutionProfileConformance,
    ExecutionTopology,
    HostingProfile,
    PackagingCandidate,
    profile_contract,
)


M10_CURRENT_TRUTH_SCHEMA = "yonerai.discord.m10-current-truth.v1"
MAX_M10_PORTS = 8
MAX_M10_BLOCKERS = 16
MAX_M10_IDENTIFIER_CHARS = 96
MAX_M10_JSON_BYTES = 16_384

_SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.:-]{0,95}$")
_SECRET_LIKE = re.compile(
    r"(?:secret|token|password|passwd|credential|api[_-]?key|private[_-]?key|bearer|sk-[a-z0-9])",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class M10SourceTruthV1:
    """A bounded statement about one injected runtime source."""

    configured: bool
    ready: bool
    live_success: bool | None
    blocker: str | None

    def __post_init__(self) -> None:
        _exact_bool(self.configured, label="configured")
        _exact_bool(self.ready, label="ready")
        if self.live_success is not None:
            _exact_bool(self.live_success, label="live_success")
        if self.ready and not self.configured:
            raise ValueError("ready source must be configured")
        if self.live_success is True and not self.ready:
            raise ValueError("live_success source must be ready")
        if self.ready and self.blocker is not None:
            raise ValueError("ready source must not have a blocker")
        if not self.ready and self.blocker is None:
            raise ValueError("unready source must have an exact blocker")
        if self.blocker is not None:
            _safe_identifier(self.blocker, label="blocker")

    def to_dict(self) -> dict[str, bool | str | None]:
        return {
            "blocker": self.blocker,
            "configured": self.configured,
            "live_success": self.live_success,
            "ready": self.ready,
        }


@dataclass(frozen=True, slots=True)
class M10CurrentTruthV1:
    """Immutable, secret-free deployment truth for the unfinished M10 selection."""

    selection_configured: bool
    selected_topology: ExecutionTopology | None
    selected_hosting_profile: HostingProfile | None
    selected_packaging: PackagingCandidate | None
    effective_topology: ExecutionTopology
    available_ports: tuple[str, ...]
    required_ports: tuple[str, ...]
    missing_ports: tuple[str, ...]
    provider_source: M10SourceTruthV1
    sandbox_source: M10SourceTruthV1
    jobs_source: M10SourceTruthV1
    audit_source: M10SourceTruthV1
    blockers: tuple[str, ...]
    schema_version: str = M10_CURRENT_TRUTH_SCHEMA

    def __post_init__(self) -> None:
        _exact_bool(self.selection_configured, label="selection_configured")
        if self.schema_version != M10_CURRENT_TRUTH_SCHEMA:
            raise ValueError("schema_version is fixed")
        _optional_enum(self.selected_topology, ExecutionTopology, label="selected_topology")
        _optional_enum(self.selected_hosting_profile, HostingProfile, label="selected_hosting_profile")
        _optional_enum(self.selected_packaging, PackagingCandidate, label="selected_packaging")
        if not isinstance(self.effective_topology, ExecutionTopology):
            raise TypeError("effective_topology must be an ExecutionTopology")
        selected_values = (self.selected_topology, self.selected_hosting_profile, self.selected_packaging)
        if self.selection_configured is not any(value is not None for value in selected_values):
            raise ValueError("selection_configured must exactly match selected values")
        expected_topology = self.selected_topology or ExecutionTopology.LOCAL_STANDALONE
        if self.effective_topology is not expected_topology:
            raise ValueError("effective_topology must match selection or the Local fallback")

        for label, values, maximum in (
            ("available_ports", self.available_ports, MAX_M10_PORTS),
            ("required_ports", self.required_ports, MAX_M10_PORTS),
            ("missing_ports", self.missing_ports, MAX_M10_PORTS),
            ("blockers", self.blockers, MAX_M10_BLOCKERS),
        ):
            _bounded_identifiers(values, label=label, maximum=maximum)
        for port in (*self.available_ports, *self.required_ports, *self.missing_ports):
            _known_port(port)
        if self.available_ports != tuple(sorted(set(self.available_ports))):
            raise ValueError("available_ports must be unique and sorted")
        if self.required_ports != tuple(sorted(set(self.required_ports))):
            raise ValueError("required_ports must be unique and sorted")
        contract = _profile_contract_for_truth(
            self.effective_topology,
            self.selected_hosting_profile,
            self.selected_packaging,
        )
        expected_required = tuple(port.value for port in contract.required_ports)
        if self.required_ports != expected_required:
            raise ValueError("required_ports must exactly match effective_topology")
        forbidden_ports = {port.value for port in contract.forbidden_ports}
        if forbidden_ports.intersection(self.available_ports):
            raise ValueError("available_ports must not include a forbidden execution port")
        expected_missing = tuple(port for port in self.required_ports if port not in self.available_ports)
        if self.missing_ports != expected_missing:
            raise ValueError("missing_ports must exactly match required ports that are unavailable")
        for source in (self.provider_source, self.sandbox_source, self.jobs_source, self.audit_source):
            if not isinstance(source, M10SourceTruthV1):
                raise TypeError("runtime sources must be M10SourceTruthV1")
        expected_blockers: list[str] = []
        if not self.selection_configured:
            expected_blockers.append("deployment_selection_not_configured")
        elif not all(value is not None for value in selected_values):
            expected_blockers.append("deployment_selection_incomplete")
        expected_blockers.extend(f"missing_port.{port}" for port in self.missing_ports)
        expected_blockers.extend(
            f"{name}.{source.blocker}"
            for name, source in (
                ("audit", self.audit_source),
                ("jobs", self.jobs_source),
                ("provider", self.provider_source),
                ("sandbox", self.sandbox_source),
            )
            if source.blocker is not None
        )
        if self.blockers != tuple(sorted(set(expected_blockers))):
            raise ValueError("blockers must exactly match selection, ports, and sources")
        if len(self.to_json().encode("utf-8")) > MAX_M10_JSON_BYTES:
            raise ValueError("M10 current truth exceeds its JSON byte limit")

    def to_dict(self) -> dict[str, Any]:
        return {
            "available_ports": list(self.available_ports),
            "blockers": list(self.blockers),
            "effective_topology": self.effective_topology.value,
            "missing_ports": list(self.missing_ports),
            "required_ports": list(self.required_ports),
            "schema_version": self.schema_version,
            "selected_hosting_profile": (
                None if self.selected_hosting_profile is None else self.selected_hosting_profile.value
            ),
            "selected_packaging": None if self.selected_packaging is None else self.selected_packaging.value,
            "selected_topology": None if self.selected_topology is None else self.selected_topology.value,
            "selection_configured": self.selection_configured,
            "sources": {
                "audit": self.audit_source.to_dict(),
                "jobs": self.jobs_source.to_dict(),
                "provider": self.provider_source.to_dict(),
                "sandbox": self.sandbox_source.to_dict(),
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def build_m10_current_truth(
    *,
    selected_topology: ExecutionTopology | None = None,
    selected_hosting_profile: HostingProfile | None = None,
    selected_packaging: PackagingCandidate | None = None,
    available_ports: tuple[str, ...] = ("local",),
    provider_source: M10SourceTruthV1 | None = None,
    sandbox_source: M10SourceTruthV1 | None = None,
    jobs_source: M10SourceTruthV1 | None = None,
    audit_source: M10SourceTruthV1 | None = None,
) -> M10CurrentTruthV1:
    """Project trusted inputs without inventing a deployment/profile decision."""

    _optional_enum(selected_topology, ExecutionTopology, label="selected_topology")
    _optional_enum(selected_hosting_profile, HostingProfile, label="selected_hosting_profile")
    _optional_enum(selected_packaging, PackagingCandidate, label="selected_packaging")
    _bounded_identifiers(available_ports, label="available_ports", maximum=MAX_M10_PORTS)
    for port in available_ports:
        _known_port(port)
    normalized_available = tuple(sorted(set(available_ports)))
    selected_values = (selected_topology, selected_hosting_profile, selected_packaging)
    selection_configured = any(value is not None for value in selected_values)
    selection_complete = all(value is not None for value in selected_values)
    effective_topology = selected_topology or ExecutionTopology.LOCAL_STANDALONE
    contract = _profile_contract_for_truth(
        effective_topology,
        selected_hosting_profile,
        selected_packaging,
    )
    required_ports = tuple(port.value for port in contract.required_ports)
    forbidden_ports = {port.value for port in contract.forbidden_ports}
    if forbidden_ports.intersection(normalized_available):
        raise ValueError("available_ports must not include a forbidden execution port")
    missing_ports = tuple(port for port in required_ports if port not in normalized_available)

    sources = {
        "audit": audit_source or _unconfigured("audit_source_not_declared"),
        "jobs": jobs_source or _unconfigured("jobs_source_not_declared"),
        "provider": provider_source or _unconfigured("provider_source_not_declared"),
        "sandbox": sandbox_source or _unconfigured("sandbox_source_not_declared"),
    }
    blockers: list[str] = []
    if not selection_configured:
        blockers.append("deployment_selection_not_configured")
    elif not selection_complete:
        blockers.append("deployment_selection_incomplete")
    blockers.extend(f"missing_port.{port}" for port in missing_ports)
    blockers.extend(
        f"{source_name}.{source.blocker}" for source_name, source in sources.items() if source.blocker is not None
    )
    return M10CurrentTruthV1(
        selection_configured=selection_configured,
        selected_topology=selected_topology,
        selected_hosting_profile=selected_hosting_profile,
        selected_packaging=selected_packaging,
        effective_topology=effective_topology,
        available_ports=normalized_available,
        required_ports=required_ports,
        missing_ports=missing_ports,
        provider_source=sources["provider"],
        sandbox_source=sources["sandbox"],
        jobs_source=sources["jobs"],
        audit_source=sources["audit"],
        blockers=tuple(sorted(set(blockers))),
    )


def _profile_contract_for_truth(
    topology: ExecutionTopology,
    hosting_profile: HostingProfile | None,
    packaging: PackagingCandidate | None,
) -> ExecutionProfileConformance:
    return profile_contract(
        topology,
        hosting_profile or HostingProfile.FULL_PRIVATE_SELF_HOST,
        packaging or PackagingCandidate.LOCAL_ONLY,
    )


def _unconfigured(blocker: str) -> M10SourceTruthV1:
    return M10SourceTruthV1(configured=False, ready=False, live_success=None, blocker=blocker)


def _known_port(value: str) -> None:
    if value not in {port.value for port in ExecutionPort}:
        raise ValueError("unknown execution port")


def _exact_bool(value: object, *, label: str) -> None:
    if type(value) is not bool:
        raise TypeError(f"{label} must be bool")


def _optional_enum(value: object, enum_type: type[Any], *, label: str) -> None:
    if value is not None and not isinstance(value, enum_type):
        raise TypeError(f"{label} must use the existing {enum_type.__name__}")


def _safe_identifier(value: object, *, label: str) -> None:
    if not isinstance(value, str) or not value or len(value) > MAX_M10_IDENTIFIER_CHARS:
        raise ValueError(f"{label} must be a bounded machine identifier")
    if not _SAFE_IDENTIFIER.fullmatch(value) or _SECRET_LIKE.search(value):
        raise ValueError(f"{label} must not contain unknown, path, or secret-like content")


def _bounded_identifiers(values: object, *, label: str, maximum: int) -> None:
    if not isinstance(values, tuple):
        raise TypeError(f"{label} must be a tuple")
    if len(values) > maximum:
        raise ValueError(f"{label} exceeds its item limit")
    for value in values:
        _safe_identifier(value, label=label)


__all__ = [
    "M10_CURRENT_TRUTH_SCHEMA",
    "M10CurrentTruthV1",
    "M10SourceTruthV1",
    "build_m10_current_truth",
]
