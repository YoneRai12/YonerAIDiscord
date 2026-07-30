from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from yonerai_discord.execution_gateway import (
    CapabilityResult,
    IdempotencyConflictError,
    RunEvent,
    RunInput,
    RunReference,
)
from yonerai_discord.modules.ai.execution_profiles import (
    ExecutionPort,
    ExecutionProfileConformance,
    ExecutionProfileError,
    ExecutionTopology,
    HostingProfile,
    HybridExecutionGateway,
    PackagingDependencyClass,
    PackagingCandidate,
    compose_execution_gateway,
    compose_profiled_execution_gateway,
    profile_contract,
    validate_profile_dependencies,
)


class _Gateway:
    def __init__(self, prefix: str, *, terminal_kind: str = "final") -> None:
        self.prefix = prefix
        self.terminal_kind = terminal_kind
        self.started: list[RunInput] = []
        self.results: list[tuple[str, CapabilityResult]] = []
        self.cancelled: list[str] = []
        self._runs: dict[str, str] = {}

    async def start(self, request: RunInput) -> RunReference:
        self.started.append(request)
        run_id = self._runs.setdefault(request.idempotency_key, f"{self.prefix}-{len(self._runs) + 1}")
        return RunReference(
            run_id,
            request.idempotency_key,
            reused=sum(item.idempotency_key == request.idempotency_key for item in self.started) > 1,
        )

    async def events(self, run_id: str) -> AsyncIterator[RunEvent]:
        yield RunEvent(kind="status", payload={"backend": self.prefix}, run_id=run_id, sequence=1)
        yield RunEvent(
            kind=self.terminal_kind,
            text=f"{self.prefix} result",
            run_id=run_id,
            sequence=2,
        )

    async def submit_result(self, run_id: str, result: CapabilityResult) -> None:
        self.results.append((run_id, result))

    async def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)


def _compatible_dependencies(
    packaging: PackagingCandidate,
) -> tuple[PackagingDependencyClass, ...]:
    if packaging is PackagingCandidate.UNDECIDED:
        return ()
    return (PackagingDependencyClass.PUBLIC_CODE,)


def _request(key: str, *, remote: bool) -> RunInput:
    return RunInput(
        input_text="bounded request",
        idempotency_key=key,
        extensions={"code_owned_remote": remote},
    )


@pytest.mark.parametrize(
    ("topology", "expected"),
    [
        (ExecutionTopology.LOCAL_STANDALONE, "local"),
        (ExecutionTopology.DIRECT_CORE, "remote"),
        (ExecutionTopology.DISCORD_PROCESSING, "remote"),
    ],
)
def test_profiles_reuse_the_same_gateway_contract(
    topology: ExecutionTopology,
    expected: str,
) -> None:
    local = _Gateway("local")
    remote = _Gateway("remote")

    selected = compose_execution_gateway(topology, local=local, remote=remote)

    assert selected is (local if expected == "local" else remote)


def test_remote_and_hybrid_profiles_fail_closed_when_dependencies_are_missing() -> None:
    local = _Gateway("local")

    with pytest.raises(ExecutionProfileError, match="remote gateway"):
        compose_execution_gateway(ExecutionTopology.DIRECT_CORE, local=local)
    with pytest.raises(ExecutionProfileError, match="code-owned selector"):
        compose_execution_gateway(
            ExecutionTopology.HYBRID,
            local=local,
            remote=_Gateway("remote"),
        )


@pytest.mark.parametrize(
    "hosting_profile",
    tuple(HostingProfile),
)
@pytest.mark.parametrize("packaging", tuple(PackagingCandidate))
@pytest.mark.parametrize("topology", tuple(ExecutionTopology))
def test_hosting_profiles_compose_every_topology_through_the_same_gateway_contract(
    hosting_profile: HostingProfile,
    packaging: PackagingCandidate,
    topology: ExecutionTopology,
) -> None:
    local = _Gateway("local")
    direct = _Gateway("direct")
    processing = _Gateway("processing")
    hybrid_core = _Gateway("hybrid-core")

    if topology is ExecutionTopology.LOCAL_STANDALONE:
        selected = compose_profiled_execution_gateway(
            topology,
            hosting_profile,
            packaging,
            dependencies=_compatible_dependencies(packaging),
            local=local,
        )
    elif topology is ExecutionTopology.DIRECT_CORE:
        selected = compose_profiled_execution_gateway(
            topology,
            hosting_profile,
            packaging,
            dependencies=_compatible_dependencies(packaging),
            direct_core=direct,
        )
    elif topology is ExecutionTopology.DISCORD_PROCESSING:
        selected = compose_profiled_execution_gateway(
            topology,
            hosting_profile,
            packaging,
            dependencies=_compatible_dependencies(packaging),
            discord_processing=processing,
        )
    else:
        selected = compose_profiled_execution_gateway(
            topology,
            hosting_profile,
            packaging,
            dependencies=_compatible_dependencies(packaging),
            local=local,
            hybrid_core=hybrid_core,
            hybrid_selector=lambda request: request.extensions["code_owned_remote"] is True,
        )

    if topology is ExecutionTopology.LOCAL_STANDALONE:
        assert selected is local
    elif topology is ExecutionTopology.DIRECT_CORE:
        assert selected is direct
    elif topology is ExecutionTopology.DISCORD_PROCESSING:
        assert selected is processing
    else:
        assert isinstance(selected, HybridExecutionGateway)
    contract = profile_contract(topology, hosting_profile, packaging)
    assert set(contract.required_ports).isdisjoint(contract.forbidden_ports)
    assert set(contract.required_ports) | set(contract.forbidden_ports) == set(ExecutionPort)
    assert contract.external_activation_default is False
    assert contract.live_readiness_claim is False
    assert contract.publication_ready_claim is False
    assert contract.to_mapping()["live_readiness_claim"] is False


def test_packaging_candidate_is_validated_but_does_not_select_a_runtime_or_claim_publication() -> None:
    gateway = _Gateway("local")

    for packaging in PackagingCandidate:
        assert (
            compose_profiled_execution_gateway(
                ExecutionTopology.LOCAL_STANDALONE,
                HostingProfile.OFFICIAL_MANAGED,
                packaging,
                dependencies=_compatible_dependencies(packaging),
                local=gateway,
            )
            is gateway
        )


def test_profiled_composition_requires_declared_dependency_evidence() -> None:
    with pytest.raises(TypeError, match="dependencies"):
        compose_profiled_execution_gateway(  # type: ignore[call-arg]
            ExecutionTopology.LOCAL_STANDALONE,
            HostingProfile.FULL_PRIVATE_SELF_HOST,
            PackagingCandidate.LOCAL_ONLY,
            local=_Gateway("local"),
        )


@pytest.mark.parametrize(
    "packaging",
    (
        PackagingCandidate.PUBLIC_SAFE_SHARED,
        PackagingCandidate.UNDECIDED,
    ),
)
@pytest.mark.parametrize(
    "dependency",
    (
        PackagingDependencyClass.OFFICIAL_SECRET,
        PackagingDependencyClass.PRIVATE_ENDPOINT,
    ),
)
def test_profiled_composition_rejects_private_dependencies_for_non_private_packages(
    packaging: PackagingCandidate,
    dependency: PackagingDependencyClass,
) -> None:
    with pytest.raises(ExecutionProfileError, match="forbids"):
        compose_profiled_execution_gateway(
            ExecutionTopology.LOCAL_STANDALONE,
            HostingProfile.OFFICIAL_MANAGED,
            packaging,
            dependencies=(dependency,),
            local=_Gateway("local"),
        )


def test_public_safe_dependencies_are_typed_and_exclude_private_or_live_resources() -> None:
    contract = profile_contract(
        ExecutionTopology.LOCAL_STANDALONE,
        HostingProfile.OFFICIAL_MANAGED,
        PackagingCandidate.PUBLIC_SAFE_SHARED,
    )

    validate_profile_dependencies(
        contract,
        (
            PackagingDependencyClass.PUBLIC_CODE,
            PackagingDependencyClass.PURE_RUNTIME,
        ),
    )
    for dependency in (
        PackagingDependencyClass.OFFICIAL_SECRET,
        PackagingDependencyClass.PRIVATE_ENDPOINT,
        PackagingDependencyClass.LIVE_OPERATION,
        PackagingDependencyClass.HOST_LOCAL_RESOURCE,
    ):
        with pytest.raises(ExecutionProfileError, match="forbids"):
            validate_profile_dependencies(contract, (dependency,))

    with pytest.raises(TypeError, match="PackagingDependencyClass"):
        validate_profile_dependencies(contract, ("official_secret",))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="tuple"):
        validate_profile_dependencies(contract, [])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unique"):
        validate_profile_dependencies(
            contract,
            (
                PackagingDependencyClass.PUBLIC_CODE,
                PackagingDependencyClass.PUBLIC_CODE,
            ),
        )


def test_local_only_dependencies_allow_private_and_host_resources_but_not_live_operations() -> None:
    contract = profile_contract(
        ExecutionTopology.LOCAL_STANDALONE,
        HostingProfile.FULL_PRIVATE_SELF_HOST,
        PackagingCandidate.LOCAL_ONLY,
    )

    assert contract.allowed_dependency_classes == (
        PackagingDependencyClass.HOST_LOCAL_RESOURCE,
        PackagingDependencyClass.OFFICIAL_SECRET,
        PackagingDependencyClass.PRIVATE_ENDPOINT,
        PackagingDependencyClass.PUBLIC_CODE,
        PackagingDependencyClass.PURE_RUNTIME,
    )
    validate_profile_dependencies(contract, contract.allowed_dependency_classes)
    with pytest.raises(ExecutionProfileError, match="forbids"):
        validate_profile_dependencies(contract, (PackagingDependencyClass.LIVE_OPERATION,))


@pytest.mark.parametrize("packaging", tuple(PackagingCandidate))
def test_no_packaging_candidate_authorizes_live_operations(packaging: PackagingCandidate) -> None:
    contract = profile_contract(
        ExecutionTopology.LOCAL_STANDALONE,
        HostingProfile.OFFICIAL_MANAGED,
        packaging,
    )

    with pytest.raises(ExecutionProfileError, match="forbids"):
        validate_profile_dependencies(contract, (PackagingDependencyClass.LIVE_OPERATION,))


def test_profile_contract_cannot_be_constructed_with_live_or_publication_claims() -> None:
    contract = profile_contract(
        ExecutionTopology.DIRECT_CORE,
        HostingProfile.OFFICIAL_MANAGED,
        PackagingCandidate.OFFICIAL_PRIVATE,
    )
    values = {
        "topology": contract.topology,
        "hosting_profile": contract.hosting_profile,
        "packaging": contract.packaging,
        "required_ports": contract.required_ports,
        "forbidden_ports": contract.forbidden_ports,
        "allowed_dependency_classes": contract.allowed_dependency_classes,
    }

    with pytest.raises(ValueError, match="live_readiness_claim"):
        ExecutionProfileConformance(**values, live_readiness_claim=True)
    with pytest.raises(ValueError, match="publication_ready_claim"):
        ExecutionProfileConformance(**values, publication_ready_claim=True)
    with pytest.raises(ValueError, match="external_activation_default"):
        ExecutionProfileConformance(**values, external_activation_default=True)


def test_profiled_composition_rejects_forbidden_ports_before_gateway_use() -> None:
    with pytest.raises(ExecutionProfileError, match="forbidden execution port"):
        compose_profiled_execution_gateway(
            ExecutionTopology.DIRECT_CORE,
            HostingProfile.OFFICIAL_MANAGED,
            PackagingCandidate.OFFICIAL_PRIVATE,
            dependencies=(PackagingDependencyClass.PUBLIC_CODE,),
            local=_Gateway("unexpected-local"),
            direct_core=_Gateway("direct"),
        )


@pytest.mark.parametrize(
    ("topology", "message"),
    [
        (ExecutionTopology.LOCAL_STANDALONE, "local gateway"),
        (ExecutionTopology.DIRECT_CORE, "direct Core gateway"),
        (ExecutionTopology.DISCORD_PROCESSING, "processing gateway"),
        (ExecutionTopology.HYBRID, "local and Core gateways"),
    ],
)
def test_profiled_topologies_fail_closed_when_the_exact_port_is_missing(
    topology: ExecutionTopology,
    message: str,
) -> None:
    with pytest.raises(ExecutionProfileError, match=message):
        compose_profiled_execution_gateway(
            topology,
            HostingProfile.OFFICIAL_MANAGED,
            PackagingCandidate.OFFICIAL_PRIVATE,
            dependencies=(PackagingDependencyClass.PUBLIC_CODE,),
        )


@pytest.mark.asyncio
async def test_hybrid_routes_and_replays_each_request_on_one_pinned_backend() -> None:
    local = _Gateway("local")
    remote = _Gateway("remote")
    gateway = HybridExecutionGateway(
        local=local,
        remote=remote,
        select_remote=lambda request: request.extensions["code_owned_remote"] is True,
    )

    local_reference = await gateway.start(_request("local-key", remote=False))
    remote_reference = await gateway.start(_request("remote-key", remote=True))
    replay = await gateway.start(_request("remote-key", remote=True))

    assert replay == RunReference(remote_reference.run_id, "remote-key", reused=True)
    assert len(local.started) == 1
    assert len(remote.started) == 1
    assert local_reference.run_id != remote_reference.run_id

    events = [event async for event in gateway.events(remote_reference.run_id)]
    assert [event.run_id for event in events] == [remote_reference.run_id, remote_reference.run_id]
    assert events[-1].text == "remote result"

    result = CapabilityResult("result-1", "tools.example", output={"ok": True})
    await gateway.submit_result(remote_reference.run_id, result)
    await gateway.cancel(local_reference.run_id)

    assert remote.results == [("remote-1", result)]
    assert local.cancelled == ["local-1"]


@pytest.mark.asyncio
async def test_hybrid_rejects_topology_change_for_the_same_idempotency_key() -> None:
    route = {"remote": False}
    gateway = HybridExecutionGateway(
        local=_Gateway("local"),
        remote=_Gateway("remote"),
        select_remote=lambda _request: route["remote"],
    )

    await gateway.start(_request("fixed-key", remote=False))
    route["remote"] = True

    with pytest.raises(IdempotencyConflictError, match="cannot change"):
        await gateway.start(_request("fixed-key", remote=True))


@pytest.mark.asyncio
async def test_hybrid_rejects_changed_request_and_capacity_before_backend_start() -> None:
    local = _Gateway("local")
    gateway = HybridExecutionGateway(
        local=local,
        remote=_Gateway("remote"),
        select_remote=lambda _request: False,
        max_runs=1,
    )

    await gateway.start(_request("fixed-key", remote=False))
    changed = RunInput(
        input_text="different request",
        idempotency_key="fixed-key",
        extensions={"code_owned_remote": False},
    )

    with pytest.raises(IdempotencyConflictError, match="cannot change"):
        await gateway.start(changed)
    with pytest.raises(ExecutionProfileError, match="capacity"):
        await gateway.start(_request("second-key", remote=False))

    assert [request.idempotency_key for request in local.started] == ["fixed-key"]


@pytest.mark.asyncio
async def test_hybrid_rejects_non_boolean_selector_and_bounded_capacity() -> None:
    invalid = HybridExecutionGateway(
        local=_Gateway("local"),
        remote=_Gateway("remote"),
        select_remote=lambda _request: 1,  # type: ignore[return-value]
    )
    with pytest.raises(ExecutionProfileError, match="boolean"):
        await invalid.start(_request("invalid", remote=False))

    bounded = HybridExecutionGateway(
        local=_Gateway("local"),
        remote=_Gateway("remote"),
        select_remote=lambda _request: False,
        max_runs=1,
    )
    await bounded.start(_request("first", remote=False))
    with pytest.raises(ExecutionProfileError, match="capacity"):
        await bounded.start(_request("second", remote=False))


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_kind", ["final", "error"])
async def test_hybrid_prunes_only_terminal_routes_when_capacity_is_needed(
    terminal_kind: str,
) -> None:
    local = _Gateway("local", terminal_kind=terminal_kind)
    gateway = HybridExecutionGateway(
        local=local,
        remote=_Gateway("remote"),
        select_remote=lambda _request: False,
        max_runs=1,
    )

    first = await gateway.start(_request("first", remote=False))
    replay = await gateway.start(_request("first", remote=False))
    assert replay == RunReference(first.run_id, "first", reused=True)
    assert len(local.started) == 1

    with pytest.raises(ExecutionProfileError, match="capacity"):
        await gateway.start(_request("blocked", remote=False))

    events = [event async for event in gateway.events(first.run_id)]
    assert events[-1].kind == terminal_kind

    second = await gateway.start(_request("second", remote=False))
    assert second.run_id != first.run_id
    assert [request.idempotency_key for request in local.started] == ["first", "second"]


@pytest.mark.asyncio
async def test_hybrid_cancelled_route_releases_capacity_after_child_cancel() -> None:
    local = _Gateway("local")
    gateway = HybridExecutionGateway(
        local=local,
        remote=_Gateway("remote"),
        select_remote=lambda _request: False,
        max_runs=1,
    )

    first = await gateway.start(_request("first", remote=False))
    await gateway.cancel(first.run_id)
    second = await gateway.start(_request("second", remote=False))

    assert local.cancelled == ["local-1"]
    assert second.run_id != first.run_id
    assert [request.idempotency_key for request in local.started] == ["first", "second"]
