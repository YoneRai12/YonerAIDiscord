from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from yonerai_discord.capability_forge.sandbox_contract import (
    SandboxCandidate,
    SandboxResult,
    SandboxScope,
)
from yonerai_discord.capability_forge.sandbox_runtime import DiscordSandboxRuntime
from yonerai_discord.capability_forge.sandbox_service import SandboxRunOutcome, SandboxRunStatus
from yonerai_discord.sandbox_operator_cli import SandboxCliDependencies, SandboxCode, SandboxTemplate


class _Lifecycle:
    def __init__(self) -> None:
        self.calls: list[tuple[int, SandboxCandidate, SandboxScope, str]] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False
        self.on_cancel = None
        self.error: Exception | None = None

    async def run(
        self,
        *,
        owner_user_id: int,
        candidate: SandboxCandidate,
        scope: SandboxScope,
        backend_identity: str,
    ) -> SandboxRunOutcome:
        self.calls.append((owner_user_id, candidate, scope, backend_identity))
        self.started.set()
        if self.block:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                if self.on_cancel is not None:
                    self.on_cancel()
                raise
        if self.error is not None:
            raise self.error
        return SandboxRunOutcome(
            SandboxRunStatus.SUCCEEDED,
            result=SandboxResult(
                scope=scope,
                backend_identity=backend_identity,
                backend_generation=1,
                policy_digest="a" * 64,
                request_digest="b" * 64,
                session_nonce="c" * 32,
                output={"answer": {"ok": True, "value": 42}},
            ),
        )


class _Audit:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, object]]] = []

    def __call__(self, event: str, **values: object) -> int:
        self.rows.append((event, values))
        return len(self.rows)


def _interaction(interaction_id: int, guild_id: int, *, channel_id: int = 77) -> object:
    return SimpleNamespace(
        id=interaction_id,
        guild_id=guild_id,
        channel_id=channel_id,
        user=SimpleNamespace(id=42),
    )


def _runtime(
    tmp_path: Path,
    lifecycle: _Lifecycle,
    audit: _Audit,
    *,
    ready: list[bool] | None = None,
) -> DiscordSandboxRuntime:
    current = [True]
    backend = ready or [True]
    runtime = DiscordSandboxRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        lifecycle=lifecycle,
        backend_ready=lambda: backend[0],
        current=lambda: current[0],
        append_audit=audit,
    )
    runtime.open()
    return runtime


@pytest.mark.asyncio
async def test_fixed_template_runs_with_exact_discord_scope_and_persists_redacted_receipt(tmp_path: Path) -> None:
    lifecycle = _Lifecycle()
    audit = _Audit()
    runtime = _runtime(tmp_path, lifecycle, audit)
    access = runtime.bind_dependencies(_interaction(1234, 9001), SandboxCliDependencies())
    assert access.read_projection is not None and access.mutations is not None

    outcome = await access.mutations.run_template(SandboxTemplate.PYTHON_SMOKE)

    assert outcome.code is SandboxCode.OK
    assert outcome.changed is True and outcome.audit_recorded is True
    assert outcome.job_id == "job_d1234" and outcome.state == "succeeded"
    owner_id, candidate, scope, backend_identity = lifecycle.calls[0]
    assert owner_id == 42
    assert candidate.source == "answer = {'ok': True, 'value': data['value']}"
    assert dict(candidate.input_data) == {"value": 42}
    assert scope == SandboxScope(request_id="discord_1234", guild_id=9001, channel_id=77, user_id=42)
    assert backend_identity == "hyperv-disposable"
    receipt = await access.read_projection.read_receipt("job_d1234")
    assert receipt is not None
    assert (receipt.state, receipt.signed, receipt.cleanup_confirmed, receipt.failure_code) == (
        "succeeded",
        True,
        True,
        None,
    )
    assert {event for event, _ in audit.rows} == {"sandbox.template.requested", "sandbox.template.completed"}
    assert "answer" not in repr(audit.rows)
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as connection:
        row = connection.execute(
            """SELECT broker_job_id,broker_generation,broker_policy_digest,
                      broker_request_digest,broker_scope_digest,broker_nonce_digest,
                      broker_output_sha256,broker_evidence_digest
               FROM execution_sandbox_discord_job_v1 WHERE job_id='job_d1234'"""
        ).fetchone()
    assert row is not None
    assert row[0].startswith("job_") and row[1] == 1
    assert row[2] == "sha256:" + "a" * 64
    assert row[3] == "sha256:" + "b" * 64
    assert all(value.startswith("sha256:") for value in row[4:])
    terminal = next(values for event, values in audit.rows if event == "sandbox.template.completed")
    details = terminal["details"]
    assert isinstance(details, dict)
    assert details["broker_job_id"] == row[0]
    assert details["broker_evidence_digest"] == row[7]


@pytest.mark.asyncio
async def test_jobs_and_receipts_are_strictly_isolated_by_guild(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, _Lifecycle(), _Audit())
    guild_one = runtime.bind_dependencies(_interaction(1001, 9001), SandboxCliDependencies())
    guild_two = runtime.bind_dependencies(_interaction(2001, 9002), SandboxCliDependencies())
    assert guild_one.mutations is not None and guild_two.mutations is not None
    assert guild_one.read_projection is not None and guild_two.read_projection is not None

    first = await guild_one.mutations.run_template(SandboxTemplate.PYTHON_SMOKE)
    second = await guild_two.mutations.run_template(SandboxTemplate.PYTHON_SMOKE)

    assert tuple(job.job_id for job in await guild_one.read_projection.list_jobs(limit=25)) == (first.job_id,)
    assert tuple(job.job_id for job in await guild_two.read_projection.list_jobs(limit=25)) == (second.job_id,)
    assert await guild_one.read_projection.read_receipt(second.job_id or "") is None
    assert await guild_two.read_projection.read_receipt(first.job_id or "") is None
    assert (await guild_one.read_projection.read_status()).execution_count == 1
    assert (await guild_two.read_projection.read_status()).execution_count == 1


@pytest.mark.asyncio
async def test_backend_readiness_and_audit_fail_closed_before_vm_execution(tmp_path: Path) -> None:
    lifecycle = _Lifecycle()
    backend_ready = [False]
    runtime = _runtime(tmp_path, lifecycle, _Audit(), ready=backend_ready)
    access = runtime.bind_dependencies(_interaction(1234, 9001), SandboxCliDependencies())
    assert access.read_projection is not None and access.mutations is not None

    snapshot = await access.read_projection.read_status()
    outcome = await access.mutations.run_template(SandboxTemplate.PYTHON_SMOKE)

    assert snapshot.ready is False
    assert snapshot.blockers == ("trusted_broker_unavailable",)
    assert outcome.code is SandboxCode.SANDBOX_NOT_READY and outcome.changed is False
    assert lifecycle.calls == []

    def broken_audit(_event: str, **_values: object) -> int:
        raise RuntimeError("private audit failure")

    blocked = DiscordSandboxRuntime(
        database_path=tmp_path / "blocked.sqlite3",
        lifecycle=lifecycle,
        backend_ready=lambda: True,
        current=lambda: True,
        append_audit=broken_audit,
    )
    blocked.open()
    bound = blocked.bind_dependencies(_interaction(1235, 9001), SandboxCliDependencies())
    assert bound.mutations is not None
    failed = await bound.mutations.run_template(SandboxTemplate.PYTHON_SMOKE)
    assert failed.code is SandboxCode.AUDIT_UNAVAILABLE and failed.changed is False
    assert lifecycle.calls == []


@pytest.mark.asyncio
async def test_second_guild_cannot_start_while_first_guild_owns_single_vm_capacity(tmp_path: Path) -> None:
    lifecycle = _Lifecycle()
    lifecycle.block = True
    runtime = _runtime(tmp_path, lifecycle, _Audit())
    first = runtime.bind_dependencies(_interaction(1001, 9001), SandboxCliDependencies())
    second = runtime.bind_dependencies(_interaction(2001, 9002), SandboxCliDependencies())
    assert first.mutations is not None and second.mutations is not None
    running = asyncio.create_task(first.mutations.run_template(SandboxTemplate.PYTHON_SMOKE))
    await asyncio.wait_for(lifecycle.started.wait(), timeout=1)

    blocked = await second.mutations.run_template(SandboxTemplate.PYTHON_SMOKE)
    lifecycle.release.set()
    completed = await asyncio.wait_for(running, timeout=1)

    assert blocked.code is SandboxCode.SANDBOX_NOT_READY and blocked.changed is False
    assert completed.code is SandboxCode.OK
    assert len(lifecycle.calls) == 1 and lifecycle.calls[0][2].guild_id == 9001


@pytest.mark.asyncio
async def test_cancel_is_scope_bound_and_never_infers_cleanup_from_readiness(tmp_path: Path) -> None:
    lifecycle = _Lifecycle()
    lifecycle.block = True
    audit = _Audit()
    runtime = _runtime(tmp_path, lifecycle, audit)
    access = runtime.bind_dependencies(_interaction(1234, 9001), SandboxCliDependencies())
    other = runtime.bind_dependencies(_interaction(9999, 9002), SandboxCliDependencies())
    assert access.mutations is not None and access.read_projection is not None and other.mutations is not None

    running = asyncio.create_task(access.mutations.run_template(SandboxTemplate.PYTHON_SMOKE))
    await asyncio.wait_for(lifecycle.started.wait(), timeout=1)
    hidden = await other.mutations.cancel("job_d1234")
    cancelled = await access.mutations.cancel("job_d1234")
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, timeout=1)

    assert hidden.code is SandboxCode.JOB_NOT_FOUND
    assert cancelled.code is SandboxCode.MUTATION_FAILED and cancelled.changed is False
    receipt = await access.read_projection.read_receipt("job_d1234")
    assert receipt is not None and receipt.state == "cleanup_unconfirmed" and receipt.cleanup_confirmed is False


@pytest.mark.asyncio
async def test_begin_close_cancels_active_job_before_runtime_becomes_disposable(tmp_path: Path) -> None:
    lifecycle = _Lifecycle()
    lifecycle.block = True
    runtime = _runtime(tmp_path, lifecycle, _Audit())
    access = runtime.bind_dependencies(_interaction(1234, 9001), SandboxCliDependencies())
    assert access.mutations is not None
    running = asyncio.create_task(access.mutations.run_template(SandboxTemplate.PYTHON_SMOKE))
    await asyncio.wait_for(lifecycle.started.wait(), timeout=1)

    await runtime.begin_close()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, timeout=1)

    assert runtime.ready is False
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as connection:
        assert connection.execute(
            "SELECT state, cleanup_confirmed FROM execution_sandbox_discord_job_v1 WHERE job_id='job_d1234'"
        ).fetchone() == ("cleanup_unconfirmed", 0)
    with pytest.raises(RuntimeError, match="closed"):
        runtime.bind_dependencies(_interaction(1235, 9001), SandboxCliDependencies())


@pytest.mark.asyncio
async def test_cancel_never_claims_cleanup_when_backend_generation_was_quarantined(tmp_path: Path) -> None:
    lifecycle = _Lifecycle()
    lifecycle.block = True
    backend_ready = [True]
    lifecycle.on_cancel = lambda: backend_ready.__setitem__(0, False)
    runtime = _runtime(tmp_path, lifecycle, _Audit(), ready=backend_ready)
    access = runtime.bind_dependencies(_interaction(1234, 9001), SandboxCliDependencies())
    assert access.mutations is not None and access.read_projection is not None
    running = asyncio.create_task(access.mutations.run_template(SandboxTemplate.PYTHON_SMOKE))
    await asyncio.wait_for(lifecycle.started.wait(), timeout=1)

    cancelled = await access.mutations.cancel("job_d1234")
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, timeout=1)

    assert cancelled.code is SandboxCode.MUTATION_FAILED and cancelled.changed is False
    receipt = await access.read_projection.read_receipt("job_d1234")
    assert receipt is not None
    assert receipt.state == "cleanup_unconfirmed" and receipt.cleanup_confirmed is False
    assert runtime.ready is False


@pytest.mark.asyncio
async def test_untyped_lifecycle_exception_is_durably_quarantined_even_if_backend_probe_is_ready(
    tmp_path: Path,
) -> None:
    lifecycle = _Lifecycle()
    lifecycle.error = RuntimeError("private backend detail")
    runtime = _runtime(tmp_path, lifecycle, _Audit(), ready=[True])
    access = runtime.bind_dependencies(_interaction(1234, 9001), SandboxCliDependencies())
    assert access.mutations is not None and access.read_projection is not None

    outcome = await access.mutations.run_template(SandboxTemplate.PYTHON_SMOKE)

    assert outcome.code is SandboxCode.MUTATION_FAILED and outcome.state == "cleanup_unconfirmed"
    receipt = await access.read_projection.read_receipt("job_d1234")
    assert receipt is not None
    assert (receipt.state, receipt.cleanup_confirmed, receipt.failure_code) == (
        "cleanup_unconfirmed",
        False,
        "lifecycle_exception_cleanup_unconfirmed",
    )
    assert runtime.ready is False
    runtime._connection.close()  # type: ignore[union-attr]
    runtime._connection = None

    reopened = _runtime(tmp_path, _Lifecycle(), _Audit(), ready=[True])
    assert reopened.ready is False


@pytest.mark.asyncio
async def test_cancelled_lifecycle_persists_quarantine_before_reraising(tmp_path: Path) -> None:
    lifecycle = _Lifecycle()
    lifecycle.block = True
    runtime = _runtime(tmp_path, lifecycle, _Audit(), ready=[True])
    access = runtime.bind_dependencies(_interaction(1234, 9001), SandboxCliDependencies())
    assert access.mutations is not None and access.read_projection is not None
    running = asyncio.create_task(access.mutations.run_template(SandboxTemplate.PYTHON_SMOKE))
    await asyncio.wait_for(lifecycle.started.wait(), timeout=1)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    receipt = await access.read_projection.read_receipt("job_d1234")
    assert receipt is not None
    assert (receipt.state, receipt.cleanup_confirmed, receipt.failure_code) == (
        "cleanup_unconfirmed",
        False,
        "lifecycle_cancelled_cleanup_unconfirmed",
    )
    assert runtime.ready is False


def test_invalid_or_dm_binding_is_rejected_without_persisting_identity(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, _Lifecycle(), _Audit())
    with pytest.raises(ValueError, match="guild"):
        runtime.bind_dependencies(_interaction(1234, 0), SandboxCliDependencies())
    with pytest.raises(ValueError, match="interaction"):
        runtime.bind_dependencies(
            SimpleNamespace(id=0, guild_id=9001, channel_id=77, user=SimpleNamespace(id=42)), SandboxCliDependencies()
        )


def test_open_migrates_legacy_job_table_without_forging_canary_evidence(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE execution_sandbox_discord_job_v1(
                job_id TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                owner_user_id INTEGER NOT NULL,
                interaction_id INTEGER NOT NULL,
                template TEXT NOT NULL,
                state TEXT NOT NULL,
                signed INTEGER NOT NULL,
                cleanup_confirmed INTEGER NOT NULL,
                failure_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO execution_sandbox_discord_job_v1 VALUES(
                'job_d1',1,1,1,'python-smoke','succeeded',1,1,NULL,
                '2026-08-12T00:00:00Z','2026-08-12T00:00:00Z'
            );
            """
        )
    runtime = DiscordSandboxRuntime(
        database_path=database_path,
        lifecycle=_Lifecycle(),
        backend_ready=lambda: True,
        current=lambda: True,
        append_audit=_Audit(),
    )

    runtime.open()

    with sqlite3.connect(database_path) as connection:
        columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(execution_sandbox_discord_job_v1)"))
        evidence = connection.execute(
            "SELECT broker_job_id,broker_evidence_digest FROM execution_sandbox_discord_job_v1 WHERE job_id='job_d1'"
        ).fetchone()
    assert columns[-8:] == (
        "broker_job_id",
        "broker_generation",
        "broker_policy_digest",
        "broker_request_digest",
        "broker_scope_digest",
        "broker_nonce_digest",
        "broker_output_sha256",
        "broker_evidence_digest",
    )
    assert evidence == (None, None)


def test_open_resumes_a_crash_interrupted_evidence_column_migration(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE execution_sandbox_discord_job_v1(
                job_id TEXT PRIMARY KEY,guild_id INTEGER,owner_user_id INTEGER,interaction_id INTEGER,
                template TEXT,state TEXT,signed INTEGER,cleanup_confirmed INTEGER,failure_code TEXT,
                created_at TEXT,updated_at TEXT,broker_job_id TEXT,broker_generation INTEGER
            );
            """
        )
    runtime = DiscordSandboxRuntime(
        database_path=database_path,
        lifecycle=_Lifecycle(),
        backend_ready=lambda: True,
        current=lambda: True,
        append_audit=_Audit(),
    )

    runtime.open()

    with sqlite3.connect(database_path) as connection:
        columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(execution_sandbox_discord_job_v1)"))
    assert columns[-8:] == (
        "broker_job_id",
        "broker_generation",
        "broker_policy_digest",
        "broker_request_digest",
        "broker_scope_digest",
        "broker_nonce_digest",
        "broker_output_sha256",
        "broker_evidence_digest",
    )


@pytest.mark.asyncio
async def test_restart_reconciles_unknown_active_work_to_durable_quarantine(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime.sqlite3"
    first = DiscordSandboxRuntime(
        database_path=database_path,
        lifecycle=_Lifecycle(),
        backend_ready=lambda: True,
        current=lambda: True,
        append_audit=_Audit(),
    )
    first.open()
    assert first._insert_job(job_id="job_d1234", guild_id=9001, owner_user_id=42, interaction_id=1234)
    first._update_job("job_d1234", state="running", signed=False, cleanup_confirmed=False, failure_code=None)
    assert first._connection is not None
    first._connection.close()
    first._connection = None

    reopened = DiscordSandboxRuntime(
        database_path=database_path,
        lifecycle=_Lifecycle(),
        backend_ready=lambda: True,
        current=lambda: True,
        append_audit=_Audit(),
    )
    reopened.open()
    access = reopened.bind_dependencies(_interaction(9999, 9001), SandboxCliDependencies())
    assert access.read_projection is not None

    snapshot = await access.read_projection.read_status()
    receipt = await access.read_projection.read_receipt("job_d1234")

    assert snapshot.ready is False and snapshot.blockers == ("cleanup_unconfirmed",)
    assert receipt is not None
    assert receipt.state == "cleanup_unconfirmed" and receipt.cleanup_confirmed is False
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT failure_code FROM execution_sandbox_discord_job_v1 WHERE job_id='job_d1234'"
        ).fetchone() == ("restart_cleanup_unconfirmed",)
