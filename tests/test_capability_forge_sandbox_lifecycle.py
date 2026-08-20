from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime

import pytest

from yonerai_discord.capability_forge.lifecycle import (
    CandidateKind,
    NotificationState,
    SqliteForgeLifecycleRepository,
)
from yonerai_discord.capability_forge.sandbox_contract import (
    SANDBOX_POLICY_REVISION,
    SandboxArtifactDescriptor,
    SandboxCandidate,
    SandboxHandshake,
    SandboxPolicy,
    SandboxResult,
    SandboxScope,
    SandboxTerminationReceipt,
)
from yonerai_discord.capability_forge.sandbox_lifecycle import SandboxProposalLifecycleBridge
from yonerai_discord.capability_forge.sandbox_service import (
    ExternalSandboxService,
    SandboxRunCancelledError,
    SandboxRunOutcome,
    SandboxRunStatus,
)


NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
SCOPE = SandboxScope(request_id="sandbox-proposal", guild_id=1, channel_id=2, user_id=42)


class _Port:
    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.calls: list[str] = []

    async def handshake(self, request):
        self.calls.append("handshake")
        nonce = "b" * 32 if self.mode == "rejected" else request.session_nonce
        return SandboxHandshake(
            scope=request.scope,
            backend_identity=request.backend_identity,
            backend_generation=request.backend_generation,
            policy_digest=request.policy_digest,
            request_digest=request.request_digest,
            session_nonce=nonce,
            network_connections=0,
            host_mount=False,
            secret_access=False,
            environment_access=False,
            privileged=False,
            docker_socket=False,
            clipboard=False,
            persistent_profile=False,
            child_processes=0,
        )

    async def execute(self, request):
        self.calls.append("execute")
        if self.mode in {"timeout", "cancel"}:
            await asyncio.Event().wait()
        if self.mode == "failed":
            raise RuntimeError("PRIVATE-EXECUTION-EXCEPTION-9217")
        return SandboxResult(
            scope=request.scope,
            backend_identity=request.backend_identity,
            backend_generation=request.backend_generation,
            policy_digest=request.policy_digest,
            request_digest=request.request_digest,
            session_nonce=request.session_nonce,
            output={"value": "PRIVATE-OUTPUT-9217"},
            artifacts=(
                SandboxArtifactDescriptor(
                    opaque_id="artifact_private_9217",
                    media_type="application/json",
                    size_bytes=128,
                ),
            ),
        )

    async def terminate(self, request, reason):
        self.calls.append(f"terminate:{reason.value}")
        if self.mode == "cleanup_unconfirmed":
            return object()
        return SandboxTerminationReceipt(
            scope=request.scope,
            backend_identity=request.backend_identity,
            backend_generation=request.backend_generation,
            policy_digest=request.policy_digest,
            request_digest=request.request_digest,
            session_nonce=request.session_nonce,
            reason=reason,
            worker_terminated=True,
            workspace_destroyed=True,
        )


@pytest.fixture
def repository(tmp_path):
    repository = SqliteForgeLifecycleRepository(tmp_path / "forge.sqlite3")
    repository.open()
    try:
        yield repository
    finally:
        repository.close()


def _candidate(
    *,
    source_marker: str = "PRIVATE-SOURCE-9217",
    input_marker: str = "PRIVATE-INPUT-9217",
) -> SandboxCandidate:
    return SandboxCandidate(
        source=f"result = {{'value': '{source_marker}'}}",
        input_data={"value": input_marker},
    )


def _bridge(
    repository: SqliteForgeLifecycleRepository,
    *,
    port: _Port | None,
    policy: SandboxPolicy | None = None,
    current=lambda: True,
) -> SandboxProposalLifecycleBridge:
    return SandboxProposalLifecycleBridge(
        sandbox=ExternalSandboxService(
            port=port,
            containment_current=(lambda: True) if port is not None else None,
            policy=policy,
        ),
        repository=repository,
        current=current,
        clock=lambda: NOW,
    )


def _row_count(repository: SqliteForgeLifecycleRepository) -> int:
    with sqlite3.connect(repository.path) as connection:
        return connection.execute("SELECT COUNT(*) FROM forge_recipe_candidate").fetchone()[0]


@pytest.mark.asyncio
async def test_cleanup_confirmed_success_records_only_fixed_sandbox_metadata(repository) -> None:
    candidate = _candidate()
    port = _Port()
    bridge = _bridge(repository, port=port)

    outcome = await bridge.run(
        owner_user_id=42,
        candidate=candidate,
        scope=SCOPE,
        backend_identity="sandbox-a",
    )

    assert outcome.status is SandboxRunStatus.SUCCEEDED
    assert port.calls == ["handshake", "execute", "terminate:completed"]
    with sqlite3.connect(repository.path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM forge_recipe_candidate").fetchone()
        digest = row["recipe_digest"]
        serialized = json.dumps(dict(row), ensure_ascii=False, default=str)
    stored = repository.get_candidate(digest)
    summary = repository.get_user_success(digest, 42)
    assert stored is not None
    assert stored.candidate_kind is CandidateKind.SANDBOX_PYTHON_PURE
    assert [(item.primitive_id, item.revision) for item in stored.templates] == [
        ("python_pure", SANDBOX_POLICY_REVISION)
    ]
    assert stored.code_owned_description == "External sandbox Python-pure success proposal"
    assert stored.notification_state == NotificationState.PENDING
    assert stored.official is False
    assert stored.runtime_ready is False
    assert summary is not None and summary.first_success_at == NOW
    for forbidden in (
        "PRIVATE-SOURCE-9217",
        "PRIVATE-INPUT-9217",
        "PRIVATE-OUTPUT-9217",
        "artifact_private_9217",
        "sandbox-a",
    ):
        assert forbidden not in serialized
        assert forbidden not in repr(bridge)
    assert "PRIVATE-SOURCE-9217" not in repr(candidate)
    assert "PRIVATE-INPUT-9217" not in repr(candidate)
    assert "PRIVATE-OUTPUT-9217" not in repr(outcome)


@pytest.mark.asyncio
async def test_same_tool_aggregates_across_inputs_but_source_policy_and_backend_change_digest(repository) -> None:
    candidate = _candidate()
    bridge = _bridge(repository, port=_Port())
    await bridge.run(
        owner_user_id=42,
        candidate=candidate,
        scope=SCOPE,
        backend_identity="sandbox-a",
    )
    await bridge.run(
        owner_user_id=42,
        candidate=candidate,
        scope=SCOPE,
        backend_identity="sandbox-a",
    )
    assert _row_count(repository) == 1

    await bridge.run(
        owner_user_id=42,
        candidate=_candidate(input_marker="PRIVATE-INPUT-CHANGED"),
        scope=SCOPE,
        backend_identity="sandbox-a",
    )
    assert _row_count(repository) == 1
    await bridge.run(
        owner_user_id=42,
        candidate=_candidate(source_marker="PRIVATE-SOURCE-CHANGED"),
        scope=SCOPE,
        backend_identity="sandbox-a",
    )
    await bridge.run(
        owner_user_id=42,
        candidate=candidate,
        scope=SCOPE,
        backend_identity="sandbox-b",
    )
    policy_bridge = _bridge(
        repository,
        port=_Port(),
        policy=SandboxPolicy(max_wall_time_ms=59_999),
    )
    await policy_bridge.run(
        owner_user_id=42,
        candidate=candidate,
        scope=SCOPE,
        backend_identity="sandbox-a",
    )
    assert _row_count(repository) == 4


@pytest.mark.parametrize(
    ("mode", "expected"),
    (
        ("rejected", SandboxRunStatus.REJECTED),
        ("failed", SandboxRunStatus.FAILED),
        ("timeout", SandboxRunStatus.TIMED_OUT),
        ("cleanup_unconfirmed", SandboxRunStatus.CLEANUP_UNCONFIRMED),
    ),
)
@pytest.mark.asyncio
async def test_non_success_or_unconfirmed_cleanup_records_nothing(repository, mode, expected) -> None:
    policy = SandboxPolicy(max_wall_time_ms=1) if mode == "timeout" else None
    outcome = await _bridge(repository, port=_Port(mode), policy=policy).run(
        owner_user_id=42,
        candidate=_candidate(),
        scope=SCOPE,
        backend_identity="sandbox-a",
    )
    assert outcome.status is expected
    assert _row_count(repository) == 0


@pytest.mark.asyncio
async def test_unavailable_cancelled_stale_or_closed_bridge_records_nothing(repository) -> None:
    unavailable = await _bridge(repository, port=None).run(
        owner_user_id=42,
        candidate=_candidate(),
        scope=SCOPE,
        backend_identity="sandbox-a",
    )
    assert unavailable.status is SandboxRunStatus.UNAVAILABLE

    cancel_port = _Port("cancel")
    cancel_bridge = _bridge(repository, port=cancel_port)
    task = asyncio.create_task(
        cancel_bridge.run(
            owner_user_id=42,
            candidate=_candidate(),
            scope=SCOPE,
            backend_identity="sandbox-a",
        )
    )
    while "execute" not in cancel_port.calls:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    completed_port = _Port()
    completed_bridge = _bridge(repository, port=completed_port)
    await completed_bridge._record_lock.acquire()
    after_cleanup = asyncio.create_task(
        completed_bridge.run(
            owner_user_id=42,
            candidate=_candidate(),
            scope=SCOPE,
            backend_identity="sandbox-a",
        )
    )
    while "terminate:completed" not in completed_port.calls:
        await asyncio.sleep(0)
    while completed_bridge._sandbox._active_runs != 0:
        await asyncio.sleep(0)
    after_cleanup.cancel("bridge private cancellation detail")
    completed_bridge._record_lock.release()
    with pytest.raises(SandboxRunCancelledError) as raised:
        await after_cleanup
    assert raised.value.cleanup_confirmed is True
    assert raised.value.args == ()
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "bridge private cancellation detail" not in repr(raised.value)

    stale = _bridge(repository, port=_Port(), current=lambda: False)
    assert (
        await stale.run(
            owner_user_id=42,
            candidate=_candidate(),
            scope=SCOPE,
            backend_identity="sandbox-a",
        )
    ).status is SandboxRunStatus.SUCCEEDED

    closed = _bridge(repository, port=_Port())
    await closed.begin_close()
    assert (
        await closed.run(
            owner_user_id=42,
            candidate=_candidate(),
            scope=SCOPE,
            backend_identity="sandbox-a",
        )
    ).status is SandboxRunStatus.SUCCEEDED
    assert _row_count(repository) == 0


@pytest.mark.asyncio
async def test_record_failure_is_best_effort_and_never_exposes_exception_text(
    repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _bridge(repository, port=_Port())

    def fail_record(**_kwargs):
        raise RuntimeError("PRIVATE-REPOSITORY-EXCEPTION-9217")

    monkeypatch.setattr(repository, "record_sandbox_success", fail_record)
    outcome = await bridge.run(
        owner_user_id=42,
        candidate=_candidate(),
        scope=SCOPE,
        backend_identity="sandbox-a",
    )
    assert outcome.status is SandboxRunStatus.SUCCEEDED
    assert "PRIVATE-REPOSITORY-EXCEPTION-9217" not in repr(outcome)
    assert "PRIVATE-REPOSITORY-EXCEPTION-9217" not in repr(bridge)
    assert _row_count(repository) == 0


def test_forged_success_outcome_cannot_be_recorded_without_service_cleanup_proof(repository) -> None:
    candidate = _candidate()
    result = SandboxResult(
        scope=SCOPE,
        backend_identity="sandbox-a",
        backend_generation=1,
        policy_digest=SandboxPolicy().digest,
        request_digest="a" * 64,
        session_nonce="b" * 32,
        output={"value": "ok"},
    )
    outcome = SandboxRunOutcome(SandboxRunStatus.SUCCEEDED, result=result)
    sandbox = ExternalSandboxService(
        port=_Port(),
        containment_current=lambda: True,
    )

    with pytest.raises(ValueError, match="sandbox success evidence is not service-confirmed"):
        repository.record_sandbox_success(
            user_id=42,
            sandbox=sandbox,
            outcome=outcome,
            candidate=candidate,
            succeeded_at=NOW,
        )
    assert _row_count(repository) == 0


@pytest.mark.asyncio
async def test_real_success_cannot_be_rebound_to_a_different_candidate(repository) -> None:
    candidate = _candidate()
    sandbox = ExternalSandboxService(
        port=_Port(),
        containment_current=lambda: True,
    )
    outcome = await sandbox.run(
        candidate=candidate,
        scope=SCOPE,
        backend_identity="sandbox-a",
    )
    assert outcome.status is SandboxRunStatus.SUCCEEDED

    with pytest.raises(ValueError, match="sandbox success evidence is not service-confirmed"):
        repository.record_sandbox_success(
            user_id=42,
            sandbox=sandbox,
            outcome=outcome,
            candidate=_candidate(source_marker="DIFFERENT-SOURCE"),
            succeeded_at=NOW,
        )
    assert _row_count(repository) == 0

    repository.record_sandbox_success(
        user_id=42,
        sandbox=sandbox,
        outcome=outcome,
        candidate=candidate,
        succeeded_at=NOW,
    )
    assert _row_count(repository) == 1

    with pytest.raises(ValueError, match="sandbox success evidence is not service-confirmed"):
        repository.record_sandbox_success(
            user_id=42,
            sandbox=sandbox,
            outcome=outcome,
            candidate=candidate,
            succeeded_at=NOW,
        )
    assert _row_count(repository) == 1


@pytest.mark.asyncio
async def test_owner_and_scope_must_match_before_sandbox_execution(repository) -> None:
    port = _Port()
    bridge = _bridge(repository, port=port)
    with pytest.raises(ValueError, match="match sandbox scope"):
        await bridge.run(
            owner_user_id=99,
            candidate=_candidate(),
            scope=SCOPE,
            backend_identity="sandbox-a",
        )
    assert port.calls == []
    assert _row_count(repository) == 0
