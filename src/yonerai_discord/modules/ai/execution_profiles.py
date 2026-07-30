from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any
from uuid import uuid4

from yonerai_discord.execution_gateway import (
    CapabilityResult,
    ExecutionGateway,
    ExecutionGatewayError,
    IdempotencyConflictError,
    RunEvent,
    RunInput,
    RunReference,
)


class ExecutionTopology(StrEnum):
    """Discord Surfaceから見た実行先。hosting profileとは独立に扱う。"""

    LOCAL_STANDALONE = "local_standalone"
    DIRECT_CORE = "direct_core"
    DISCORD_PROCESSING = "discord_processing"
    HYBRID = "hybrid"


class HostingProfile(StrEnum):
    """運用主体を表す。実行topologyやlive readinessとは独立である。"""

    OFFICIAL_MANAGED = "official_managed"
    OFFICIAL_HYBRID_PRIVATE = "official_hybrid_private"
    FULL_PRIVATE_SELF_HOST = "full_private_self_host"


class PackagingCandidate(StrEnum):
    """公開範囲候補。ここで実際のpackageや公開操作は行わない。"""

    PUBLIC_SAFE_SHARED = "public_safe_shared"
    OFFICIAL_PRIVATE = "official_private"
    LOCAL_ONLY = "local_only"
    UNDECIDED = "undecided"


class ExecutionPort(StrEnum):
    """実行topologyが使うcode-owned注入port。"""

    LOCAL = "local"
    DIRECT_CORE = "direct_core"
    DISCORD_PROCESSING = "discord_processing"
    HYBRID_CORE = "hybrid_core"
    HYBRID_SELECTOR = "hybrid_selector"


class PackagingDependencyClass(StrEnum):
    """package候補policyが検査する有界な依存class。"""

    PUBLIC_CODE = "public_code"
    PURE_RUNTIME = "pure_runtime"
    OFFICIAL_SECRET = "official_secret"
    PRIVATE_ENDPOINT = "private_endpoint"
    LIVE_OPERATION = "live_operation"
    HOST_LOCAL_RESOURCE = "host_local_resource"


class ExecutionProfileError(ExecutionGatewayError):
    """明示profileを安全に構成できない場合の固定エラー。"""


_ALL_EXECUTION_PORTS = tuple(ExecutionPort)
_REQUIRED_PORTS = {
    ExecutionTopology.LOCAL_STANDALONE: (ExecutionPort.LOCAL,),
    ExecutionTopology.DIRECT_CORE: (ExecutionPort.DIRECT_CORE,),
    ExecutionTopology.DISCORD_PROCESSING: (ExecutionPort.DISCORD_PROCESSING,),
    ExecutionTopology.HYBRID: (
        ExecutionPort.HYBRID_CORE,
        ExecutionPort.HYBRID_SELECTOR,
        ExecutionPort.LOCAL,
    ),
}
_ALLOWED_DEPENDENCIES = {
    PackagingCandidate.PUBLIC_SAFE_SHARED: (
        PackagingDependencyClass.PUBLIC_CODE,
        PackagingDependencyClass.PURE_RUNTIME,
    ),
    PackagingCandidate.OFFICIAL_PRIVATE: (
        PackagingDependencyClass.OFFICIAL_SECRET,
        PackagingDependencyClass.PRIVATE_ENDPOINT,
        PackagingDependencyClass.PUBLIC_CODE,
        PackagingDependencyClass.PURE_RUNTIME,
    ),
    PackagingCandidate.LOCAL_ONLY: (
        PackagingDependencyClass.HOST_LOCAL_RESOURCE,
        PackagingDependencyClass.OFFICIAL_SECRET,
        PackagingDependencyClass.PRIVATE_ENDPOINT,
        PackagingDependencyClass.PUBLIC_CODE,
        PackagingDependencyClass.PURE_RUNTIME,
    ),
    PackagingCandidate.UNDECIDED: (),
}


@dataclass(frozen=True, slots=True)
class ExecutionProfileConformance:
    """秘密を含まないpolicy投影。packageやlive readinessの証拠ではない。"""

    topology: ExecutionTopology
    hosting_profile: HostingProfile
    packaging: PackagingCandidate
    required_ports: tuple[ExecutionPort, ...]
    forbidden_ports: tuple[ExecutionPort, ...]
    allowed_dependency_classes: tuple[PackagingDependencyClass, ...]
    external_activation_default: bool = False
    live_readiness_claim: bool = False
    publication_ready_claim: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.topology, ExecutionTopology):
            raise TypeError("topology must be an ExecutionTopology")
        if not isinstance(self.hosting_profile, HostingProfile):
            raise TypeError("hosting_profile must be a HostingProfile")
        if not isinstance(self.packaging, PackagingCandidate):
            raise TypeError("packaging must be a PackagingCandidate")
        expected_required = _REQUIRED_PORTS[self.topology]
        expected_forbidden = tuple(port for port in _ALL_EXECUTION_PORTS if port not in expected_required)
        if self.required_ports != expected_required:
            raise ValueError("required_ports must exactly match topology")
        if self.forbidden_ports != expected_forbidden:
            raise ValueError("forbidden_ports must exactly complement required_ports")
        if self.allowed_dependency_classes != _ALLOWED_DEPENDENCIES[self.packaging]:
            raise ValueError("allowed_dependency_classes must exactly match packaging policy")
        for label, value in (
            ("external_activation_default", self.external_activation_default),
            ("live_readiness_claim", self.live_readiness_claim),
            ("publication_ready_claim", self.publication_ready_claim),
        ):
            if type(value) is not bool:
                raise TypeError(f"{label} must be a boolean")
            if value:
                raise ValueError(f"{label} is fixed false")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema": "yonerai.discord.execution-profile-conformance.v1",
            "topology": self.topology.value,
            "hosting_profile": self.hosting_profile.value,
            "packaging": self.packaging.value,
            "required_ports": [port.value for port in self.required_ports],
            "forbidden_ports": [port.value for port in self.forbidden_ports],
            "allowed_dependency_classes": [dependency.value for dependency in self.allowed_dependency_classes],
            "external_activation_default": self.external_activation_default,
            "live_readiness_claim": self.live_readiness_claim,
            "publication_ready_claim": self.publication_ready_claim,
        }


def profile_contract(
    topology: ExecutionTopology,
    hosting_profile: HostingProfile,
    packaging: PackagingCandidate,
) -> ExecutionProfileConformance:
    """既存profile選択から決定論的conformance契約を返す。"""

    if not isinstance(topology, ExecutionTopology):
        raise TypeError("topology must be an ExecutionTopology")
    if not isinstance(hosting_profile, HostingProfile):
        raise TypeError("hosting_profile must be a HostingProfile")
    if not isinstance(packaging, PackagingCandidate):
        raise TypeError("packaging must be a PackagingCandidate")
    required_ports = _REQUIRED_PORTS[topology]
    return ExecutionProfileConformance(
        topology=topology,
        hosting_profile=hosting_profile,
        packaging=packaging,
        required_ports=required_ports,
        forbidden_ports=tuple(port for port in _ALL_EXECUTION_PORTS if port not in required_ports),
        allowed_dependency_classes=_ALLOWED_DEPENDENCIES[packaging],
    )


def validate_profile_dependencies(
    contract: ExecutionProfileConformance,
    dependencies: tuple[PackagingDependencyClass, ...],
) -> None:
    """全依存が候補の許可するtyped classでなければfail closedにする。"""

    if not isinstance(contract, ExecutionProfileConformance):
        raise TypeError("contract must be an ExecutionProfileConformance")
    if not isinstance(dependencies, tuple):
        raise TypeError("dependencies must be a tuple")
    if len(dependencies) > len(PackagingDependencyClass):
        raise ValueError("dependencies exceed the bounded dependency class set")
    if any(type(dependency) is not PackagingDependencyClass for dependency in dependencies):
        raise TypeError("dependencies must contain PackagingDependencyClass values")
    if len(set(dependencies)) != len(dependencies):
        raise ValueError("dependencies must be unique")
    allowed = set(contract.allowed_dependency_classes)
    if any(dependency not in allowed for dependency in dependencies):
        raise ExecutionProfileError("packaging candidate forbids a dependency class")


@dataclass(frozen=True, slots=True)
class RuntimeExecutionProfileSelection:
    """Settingsから確定したsecret-freeな実行profile選択。"""

    explicit: bool
    topology: ExecutionTopology
    hosting_profile: HostingProfile
    packaging: PackagingCandidate

    def __post_init__(self) -> None:
        if type(self.explicit) is not bool:
            raise TypeError("explicit must be a boolean")
        if not isinstance(self.topology, ExecutionTopology):
            raise TypeError("topology must be an ExecutionTopology")
        if not isinstance(self.hosting_profile, HostingProfile):
            raise TypeError("hosting_profile must be a HostingProfile")
        if not isinstance(self.packaging, PackagingCandidate):
            raise TypeError("packaging must be a PackagingCandidate")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema": "yonerai.discord.execution-profile-selection.v1",
            "explicit": self.explicit,
            "topology": self.topology.value,
            "hosting_profile": self.hosting_profile.value,
            "packaging": self.packaging.value,
            "live_readiness_claim": False,
        }


def resolve_runtime_execution_profile(settings: Any) -> RuntimeExecutionProfileSelection:
    """3項目をatomicに解釈し、未指定時だけ安全なLocal既定値を返す。"""

    raw_values = (
        getattr(settings, "ai_execution_topology", None),
        getattr(settings, "ai_hosting_profile", None),
        getattr(settings, "ai_packaging_candidate", None),
    )
    configured = tuple(value is not None for value in raw_values)
    if not any(configured):
        return RuntimeExecutionProfileSelection(
            explicit=False,
            topology=ExecutionTopology.LOCAL_STANDALONE,
            hosting_profile=HostingProfile.FULL_PRIVATE_SELF_HOST,
            packaging=PackagingCandidate.LOCAL_ONLY,
        )
    if not all(configured):
        raise ExecutionProfileError("execution profile selection must specify all three fields")
    if any(not isinstance(value, str) or not value for value in raw_values):
        raise ExecutionProfileError("execution profile selection is invalid")
    try:
        topology = ExecutionTopology(raw_values[0])
        hosting_profile = HostingProfile(raw_values[1])
        packaging = PackagingCandidate(raw_values[2])
    except ValueError as exc:
        raise ExecutionProfileError("execution profile selection is invalid") from exc
    return RuntimeExecutionProfileSelection(
        explicit=True,
        topology=topology,
        hosting_profile=hosting_profile,
        packaging=packaging,
    )


@dataclass(frozen=True, slots=True)
class _HybridRoute:
    gateway: ExecutionGateway
    child_run_id: str
    reference: RunReference
    remote: bool
    request: RunInput
    terminal: bool = False


class HybridExecutionGateway:
    """code-owned selectorだけでLocal/Coreを選ぶ小さなgateway adapter。"""

    def __init__(
        self,
        *,
        local: ExecutionGateway,
        remote: ExecutionGateway,
        select_remote: Callable[[RunInput], bool],
        max_runs: int = 4_096,
    ) -> None:
        _validate_gateway(local, label="local")
        _validate_gateway(remote, label="remote")
        if not callable(select_remote):
            raise TypeError("select_remote must be callable")
        if isinstance(max_runs, bool) or not isinstance(max_runs, int) or not 1 <= max_runs <= 65_536:
            raise ValueError("max_runs must be between 1 and 65536")
        self._local = local
        self._remote = remote
        self._select_remote = select_remote
        self._max_runs = max_runs
        self._lock = asyncio.Lock()
        self._routes: dict[str, _HybridRoute] = {}
        self._idempotency: dict[str, str] = {}

    async def start(self, request: RunInput) -> RunReference:
        if not isinstance(request, RunInput):
            raise TypeError("request must be a RunInput")
        remote = self._select_remote(request)
        if type(remote) is not bool:
            raise ExecutionProfileError("hybrid selector must return a boolean")

        async with self._lock:
            wrapper_run_id = self._idempotency.get(request.idempotency_key)
            existing = self._routes.get(wrapper_run_id) if wrapper_run_id is not None else None
            if existing is not None:
                if existing.remote is not remote or existing.request != request:
                    raise IdempotencyConflictError("idempotency key cannot change execution request")
                return replace(existing.reference, reused=True)
            self._prune_terminal_routes()
            if len(self._routes) >= self._max_runs:
                raise ExecutionProfileError("hybrid execution capacity is exhausted")
            gateway = self._remote if remote else self._local
            child_reference = await gateway.start(request)
            if child_reference.idempotency_key != request.idempotency_key:
                raise IdempotencyConflictError("child gateway returned a conflicting idempotency key")
            reference = RunReference(uuid4().hex, child_reference.idempotency_key)
            self._routes[reference.run_id] = _HybridRoute(
                gateway=gateway,
                child_run_id=child_reference.run_id,
                reference=reference,
                remote=remote,
                request=request,
            )
            self._idempotency[reference.idempotency_key] = reference.run_id
            return reference

    async def events(self, run_id: str) -> AsyncIterator[RunEvent]:
        route = await self._route(run_id)
        async for event in route.gateway.events(route.child_run_id):
            bound = replace(event, run_id=run_id)
            if bound.terminal:
                await self._mark_terminal(run_id, route)
            yield bound

    async def submit_result(self, run_id: str, result: CapabilityResult) -> None:
        route = await self._route(run_id)
        await route.gateway.submit_result(route.child_run_id, result)

    async def cancel(self, run_id: str) -> None:
        route = await self._route(run_id)
        await route.gateway.cancel(route.child_run_id)
        await self._mark_terminal(run_id, route)

    async def _route(self, run_id: str) -> _HybridRoute:
        if not isinstance(run_id, str) or not run_id:
            raise TypeError("run_id must be a non-empty string")
        async with self._lock:
            route = self._routes.get(run_id)
        if route is None:
            raise ExecutionProfileError("hybrid run is unavailable")
        return route

    async def _mark_terminal(self, run_id: str, route: _HybridRoute) -> None:
        async with self._lock:
            if self._routes.get(run_id) is route:
                self._routes[run_id] = replace(route, terminal=True)

    def _prune_terminal_routes(self) -> None:
        while len(self._routes) >= self._max_runs:
            terminal_id = next(
                (run_id for run_id, route in self._routes.items() if route.terminal),
                None,
            )
            if terminal_id is None:
                return
            route = self._routes.pop(terminal_id)
            self._idempotency.pop(route.reference.idempotency_key, None)


def compose_execution_gateway(
    topology: ExecutionTopology,
    *,
    local: ExecutionGateway,
    remote: ExecutionGateway | None = None,
    hybrid_selector: Callable[[RunInput], bool] | None = None,
    max_runs: int = 4_096,
) -> ExecutionGateway:
    """同じSurface契約へLocal/Core/Processing/Hybridを注入する。"""

    if not isinstance(topology, ExecutionTopology):
        raise TypeError("topology must be an ExecutionTopology")
    _validate_gateway(local, label="local")
    if topology is ExecutionTopology.LOCAL_STANDALONE:
        return local
    if remote is None:
        raise ExecutionProfileError("selected topology requires a remote gateway")
    _validate_gateway(remote, label="remote")
    if topology in {ExecutionTopology.DIRECT_CORE, ExecutionTopology.DISCORD_PROCESSING}:
        return remote
    if hybrid_selector is None:
        raise ExecutionProfileError("hybrid topology requires a code-owned selector")
    return HybridExecutionGateway(
        local=local,
        remote=remote,
        select_remote=hybrid_selector,
        max_runs=max_runs,
    )


def compose_profiled_execution_gateway(
    topology: ExecutionTopology,
    hosting_profile: HostingProfile,
    packaging: PackagingCandidate,
    *,
    dependencies: tuple[PackagingDependencyClass, ...],
    local: ExecutionGateway | None = None,
    direct_core: ExecutionGateway | None = None,
    discord_processing: ExecutionGateway | None = None,
    hybrid_core: ExecutionGateway | None = None,
    hybrid_selector: Callable[[RunInput], bool] | None = None,
    max_runs: int = 4_096,
) -> ExecutionGateway:
    """Profileとtopologyを分離し、必要なgatewayだけを同じSurface契約へ注入する。"""

    if not isinstance(topology, ExecutionTopology):
        raise TypeError("topology must be an ExecutionTopology")
    if not isinstance(hosting_profile, HostingProfile):
        raise TypeError("hosting_profile must be a HostingProfile")
    if not isinstance(packaging, PackagingCandidate):
        raise TypeError("packaging must be a PackagingCandidate")
    contract = profile_contract(topology, hosting_profile, packaging)
    validate_profile_dependencies(contract, dependencies)
    supplied_ports = {
        port
        for port, supplied in (
            (ExecutionPort.LOCAL, local is not None),
            (ExecutionPort.DIRECT_CORE, direct_core is not None),
            (ExecutionPort.DISCORD_PROCESSING, discord_processing is not None),
            (ExecutionPort.HYBRID_CORE, hybrid_core is not None),
            (ExecutionPort.HYBRID_SELECTOR, hybrid_selector is not None),
        )
        if supplied
    }
    if supplied_ports.intersection(contract.forbidden_ports):
        raise ExecutionProfileError("selected topology received a forbidden execution port")
    if topology is ExecutionTopology.LOCAL_STANDALONE:
        if local is None:
            raise ExecutionProfileError("local standalone topology requires a local gateway")
        return compose_execution_gateway(topology, local=local, max_runs=max_runs)
    if topology is ExecutionTopology.DIRECT_CORE:
        if direct_core is None:
            raise ExecutionProfileError("direct Core topology requires a direct Core gateway")
        return compose_execution_gateway(
            topology,
            local=direct_core,
            remote=direct_core,
            max_runs=max_runs,
        )
    if topology is ExecutionTopology.DISCORD_PROCESSING:
        if discord_processing is None:
            raise ExecutionProfileError("Discord processing topology requires a processing gateway")
        return compose_execution_gateway(
            topology,
            local=discord_processing,
            remote=discord_processing,
            max_runs=max_runs,
        )
    if local is None or hybrid_core is None:
        raise ExecutionProfileError("hybrid topology requires local and Core gateways")
    return compose_execution_gateway(
        topology,
        local=local,
        remote=hybrid_core,
        hybrid_selector=hybrid_selector,
        max_runs=max_runs,
    )


def _validate_gateway(gateway: object, *, label: str) -> None:
    for method_name in ("start", "events", "submit_result", "cancel"):
        if not callable(getattr(gateway, method_name, None)):
            raise TypeError(f"{label} gateway must provide {method_name}()")


__all__ = [
    "ExecutionPort",
    "ExecutionProfileConformance",
    "ExecutionProfileError",
    "ExecutionTopology",
    "HostingProfile",
    "HybridExecutionGateway",
    "PackagingDependencyClass",
    "PackagingCandidate",
    "RuntimeExecutionProfileSelection",
    "compose_execution_gateway",
    "compose_profiled_execution_gateway",
    "profile_contract",
    "resolve_runtime_execution_profile",
    "validate_profile_dependencies",
]
