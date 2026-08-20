"""Guild-scoped production composition for the fixed Discord sandbox surface."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from yonerai_discord.sandbox_operator_cli import (
    SandboxCliDependencies,
    SandboxCode,
    SandboxJobView,
    SandboxMutationOutcome,
    SandboxReceiptView,
    SandboxStatusSnapshot,
    SandboxTemplate,
)

from .hyperv_disposable import (
    HYPERV_DISPOSABLE_BACKEND_ID,
    HyperVExecutionEvidence,
    hyperv_execution_evidence,
)
from .sandbox_contract import SandboxCandidate, SandboxScope
from .sandbox_service import SandboxRunCancelledError, SandboxRunOutcome, SandboxRunStatus


_PYTHON_SMOKE_SOURCE = "answer = {'ok': True, 'value': data['value']}"
_PYTHON_SMOKE_INPUT = {"value": 42}
_TERMINAL_STATES = frozenset(
    {
        "cancelled",
        "cleanup_unconfirmed",
        "failed",
        "rejected",
        "succeeded",
        "timed_out",
        "unavailable",
    }
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_sandbox_discord_job_v1(
    job_id TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL CHECK(guild_id BETWEEN 1 AND 9223372036854775807),
    owner_user_id INTEGER NOT NULL CHECK(owner_user_id BETWEEN 1 AND 9223372036854775807),
    interaction_id INTEGER NOT NULL CHECK(interaction_id BETWEEN 1 AND 9223372036854775807),
    template TEXT NOT NULL CHECK(template = 'python-smoke'),
    state TEXT NOT NULL,
    signed INTEGER NOT NULL CHECK(signed IN (0, 1)),
    cleanup_confirmed INTEGER NOT NULL CHECK(cleanup_confirmed IN (0, 1)),
    failure_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    broker_job_id TEXT,
    broker_generation INTEGER,
    broker_policy_digest TEXT,
    broker_request_digest TEXT,
    broker_scope_digest TEXT,
    broker_nonce_digest TEXT,
    broker_output_sha256 TEXT,
    broker_evidence_digest TEXT
);
CREATE INDEX IF NOT EXISTS execution_sandbox_discord_job_scope_v1
    ON execution_sandbox_discord_job_v1(guild_id, owner_user_id, updated_at DESC, job_id DESC);
"""
_LEGACY_COLUMNS = (
    "job_id",
    "guild_id",
    "owner_user_id",
    "interaction_id",
    "template",
    "state",
    "signed",
    "cleanup_confirmed",
    "failure_code",
    "created_at",
    "updated_at",
)
_EVIDENCE_COLUMN_DEFINITIONS = (
    ("broker_job_id", "TEXT"),
    ("broker_generation", "INTEGER"),
    ("broker_policy_digest", "TEXT"),
    ("broker_request_digest", "TEXT"),
    ("broker_scope_digest", "TEXT"),
    ("broker_nonce_digest", "TEXT"),
    ("broker_output_sha256", "TEXT"),
    ("broker_evidence_digest", "TEXT"),
)
_COLUMNS = _LEGACY_COLUMNS + tuple(name for name, _definition in _EVIDENCE_COLUMN_DEFINITIONS)


class SandboxLifecyclePort(Protocol):
    async def run(
        self,
        *,
        owner_user_id: int,
        candidate: SandboxCandidate,
        scope: SandboxScope,
        backend_identity: str,
    ) -> SandboxRunOutcome: ...


@dataclass(slots=True)
class _ActiveJob:
    guild_id: int
    owner_user_id: int
    task: asyncio.Task[SandboxMutationOutcome]


class DiscordSandboxRuntime:
    """Persist redacted receipts and bind each command to one Discord guild."""

    def __init__(
        self,
        *,
        database_path: Path,
        lifecycle: SandboxLifecyclePort,
        backend_ready: Callable[[], bool],
        current: Callable[[], bool],
        append_audit: Callable[..., object] | None,
    ) -> None:
        if not isinstance(database_path, Path):
            raise TypeError("database_path must be a Path")
        if not callable(getattr(lifecycle, "run", None)):
            raise TypeError("lifecycle must provide run()")
        if not callable(backend_ready) or not callable(current):
            raise TypeError("runtime probes must be callable")
        if append_audit is not None and not callable(append_audit):
            raise TypeError("append_audit must be callable")
        self._database_path = database_path
        self._lifecycle = lifecycle
        self._backend_ready = backend_ready
        self._current = current
        self._append_audit_port = append_audit
        self._connection: sqlite3.Connection | None = None
        self._database_lock = threading.RLock()
        self._active: dict[str, _ActiveJob] = {}
        self._active_lock = asyncio.Lock()
        self._execution_lock = asyncio.Lock()
        self._closing = False
        self._quarantined = False

    def open(self) -> None:
        with self._database_lock:
            if self._connection is not None:
                raise RuntimeError("sandbox runtime is already open")
            connection = sqlite3.connect(self._database_path, timeout=5)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.executescript(_SCHEMA)
                columns = tuple(
                    row[1] for row in connection.execute("PRAGMA table_info(execution_sandbox_discord_job_v1)")
                )
                prefix_length = len(columns) - len(_LEGACY_COLUMNS)
                if (
                    columns[: len(_LEGACY_COLUMNS)] == _LEGACY_COLUMNS
                    and 0 <= prefix_length < len(_EVIDENCE_COLUMN_DEFINITIONS)
                    and columns[len(_LEGACY_COLUMNS) :]
                    == tuple(name for name, _definition in _EVIDENCE_COLUMN_DEFINITIONS[:prefix_length])
                ):
                    for name, definition in _EVIDENCE_COLUMN_DEFINITIONS[prefix_length:]:
                        connection.execute(
                            f"ALTER TABLE execution_sandbox_discord_job_v1 ADD COLUMN {name} {definition}"
                        )
                    columns = tuple(
                        row[1] for row in connection.execute("PRAGMA table_info(execution_sandbox_discord_job_v1)")
                    )
                if columns != _COLUMNS:
                    raise RuntimeError("sandbox runtime schema is not current")
                unknown_active = int(
                    connection.execute(
                        """SELECT COUNT(*) FROM execution_sandbox_discord_job_v1
                        WHERE state IN ('queued', 'running')"""
                    ).fetchone()[0]
                )
                if unknown_active:
                    with connection:
                        connection.execute(
                            """UPDATE execution_sandbox_discord_job_v1
                            SET state='cleanup_unconfirmed', signed=0, cleanup_confirmed=0,
                                failure_code='restart_cleanup_unconfirmed', updated_at=?
                            WHERE state IN ('queued', 'running')""",
                            (_timestamp(),),
                        )
                quarantined = int(
                    connection.execute(
                        """SELECT COUNT(*) FROM execution_sandbox_discord_job_v1
                        WHERE state='cleanup_unconfirmed'"""
                    ).fetchone()[0]
                )
            except BaseException:
                connection.close()
                raise
            self._connection = connection
            self._quarantined = quarantined > 0

    @property
    def ready(self) -> bool:
        if self._closing or self._quarantined or self._connection is None:
            return False
        try:
            return self._current() is True and self._backend_ready() is True
        except Exception:
            return False

    def bind_dependencies(
        self,
        interaction: Any,
        dependencies: SandboxCliDependencies,
    ) -> SandboxCliDependencies:
        if self._closing or self._connection is None:
            raise RuntimeError("sandbox runtime is closed")
        if type(dependencies) is not SandboxCliDependencies:
            raise TypeError("dependencies must be SandboxCliDependencies")
        interaction_id = _positive_id(getattr(interaction, "id", None), "interaction")
        guild_id = _positive_id(getattr(interaction, "guild_id", None), "guild")
        channel_id = _positive_id(getattr(interaction, "channel_id", None), "channel")
        owner_user_id = _positive_id(getattr(getattr(interaction, "user", None), "id", None), "user")
        access = _ScopedRuntimeAccess(
            runtime=self,
            interaction_id=interaction_id,
            guild_id=guild_id,
            channel_id=channel_id,
            owner_user_id=owner_user_id,
        )
        return replace(dependencies, read_projection=access, mutations=access)

    async def begin_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        async with self._active_lock:
            tasks = tuple(active.task for active in self._active.values())
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        with self._database_lock:
            connection = self._connection
            self._connection = None
            if connection is not None:
                connection.close()

    async def _run_template(
        self,
        *,
        interaction_id: int,
        guild_id: int,
        channel_id: int,
        owner_user_id: int,
        template: SandboxTemplate,
    ) -> SandboxMutationOutcome:
        job_id = _job_id(interaction_id)
        if template is not SandboxTemplate.PYTHON_SMOKE:
            return SandboxMutationOutcome(SandboxCode.SANDBOX_NOT_READY, False, job_id=job_id)
        if not self.ready:
            return SandboxMutationOutcome(SandboxCode.SANDBOX_NOT_READY, False, job_id=job_id)
        if self._execution_lock.locked():
            return SandboxMutationOutcome(SandboxCode.SANDBOX_NOT_READY, False, job_id=job_id)
        async with self._execution_lock:
            if not self.ready:
                return SandboxMutationOutcome(SandboxCode.SANDBOX_NOT_READY, False, job_id=job_id)
            if not self._insert_job(
                job_id=job_id,
                guild_id=guild_id,
                owner_user_id=owner_user_id,
                interaction_id=interaction_id,
            ):
                return SandboxMutationOutcome(SandboxCode.MUTATION_FAILED, False, job_id=job_id)
            audit_recorded = self._append_audit(
                "sandbox.template.requested",
                guild_id=guild_id,
                owner_user_id=owner_user_id,
                details={"job_id": job_id, "template": template.value},
            )
            if not audit_recorded:
                self._update_job(
                    job_id,
                    state="failed",
                    signed=False,
                    cleanup_confirmed=False,
                    failure_code="audit_unavailable",
                )
                return SandboxMutationOutcome(
                    SandboxCode.AUDIT_UNAVAILABLE,
                    False,
                    job_id=job_id,
                    state="failed",
                )
            task = asyncio.create_task(
                self._execute_job(
                    job_id=job_id,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    owner_user_id=owner_user_id,
                    interaction_id=interaction_id,
                ),
                name=f"discord-sandbox-{job_id}",
            )
            async with self._active_lock:
                if self._closing or job_id in self._active:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    self._update_job(
                        job_id,
                        state="cancelled",
                        signed=False,
                        cleanup_confirmed=False,
                        failure_code="runtime_closing",
                    )
                    return SandboxMutationOutcome(
                        SandboxCode.MUTATION_FAILED,
                        False,
                        job_id=job_id,
                        state="cancelled",
                        audit_recorded=audit_recorded,
                    )
                self._active[job_id] = _ActiveJob(guild_id, owner_user_id, task)
            try:
                return await task
            finally:
                async with self._active_lock:
                    active = self._active.get(job_id)
                    if active is not None and active.task is task:
                        del self._active[job_id]

    async def _execute_job(
        self,
        *,
        job_id: str,
        guild_id: int,
        channel_id: int,
        owner_user_id: int,
        interaction_id: int,
    ) -> SandboxMutationOutcome:
        self._update_job(job_id, state="running", signed=False, cleanup_confirmed=False, failure_code=None)
        candidate = SandboxCandidate(source=_PYTHON_SMOKE_SOURCE, input_data=_PYTHON_SMOKE_INPUT)
        scope = SandboxScope(
            request_id=f"discord_{interaction_id}",
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=owner_user_id,
        )
        try:
            outcome = await self._lifecycle.run(
                owner_user_id=owner_user_id,
                candidate=candidate,
                scope=scope,
                backend_identity=HYPERV_DISPOSABLE_BACKEND_ID,
            )
        except SandboxRunCancelledError as exc:
            if exc.cleanup_confirmed:
                if self._append_terminal_audit(job_id, guild_id, owner_user_id, "cancelled"):
                    self._update_job(
                        job_id,
                        state="cancelled",
                        signed=False,
                        cleanup_confirmed=True,
                        failure_code="cancelled",
                    )
                else:
                    self._persist_terminal_audit_failure(
                        job_id=job_id,
                        state="cancelled",
                        cleanup_confirmed=True,
                    )
            else:
                self._persist_untyped_cleanup_failure(
                    job_id=job_id,
                    guild_id=guild_id,
                    owner_user_id=owner_user_id,
                    failure_code="lifecycle_cancelled_cleanup_unconfirmed",
                )
            raise
        except asyncio.CancelledError:
            self._persist_untyped_cleanup_failure(
                job_id=job_id,
                guild_id=guild_id,
                owner_user_id=owner_user_id,
                failure_code="lifecycle_cancelled_cleanup_unconfirmed",
            )
            raise
        except Exception:
            terminal_audit_recorded = self._persist_untyped_cleanup_failure(
                job_id=job_id,
                guild_id=guild_id,
                owner_user_id=owner_user_id,
                failure_code="lifecycle_exception_cleanup_unconfirmed",
            )
            return SandboxMutationOutcome(
                SandboxCode.MUTATION_FAILED,
                False,
                job_id=job_id,
                state="cleanup_unconfirmed",
                audit_recorded=terminal_audit_recorded,
            )
        evidence: HyperVExecutionEvidence | None = None
        if outcome.status is SandboxRunStatus.SUCCEEDED:
            try:
                evidence = hyperv_execution_evidence(outcome.result)  # type: ignore[arg-type]
            except Exception:
                terminal_audit_recorded = self._append_terminal_audit(job_id, guild_id, owner_user_id, "failed")
                if not terminal_audit_recorded:
                    self._persist_terminal_audit_failure(
                        job_id=job_id,
                        state="failed",
                        cleanup_confirmed=True,
                    )
                    return SandboxMutationOutcome(
                        SandboxCode.AUDIT_UNAVAILABLE,
                        False,
                        job_id=job_id,
                        state="failed",
                    )
                self._update_job(
                    job_id,
                    state="failed",
                    signed=False,
                    cleanup_confirmed=True,
                    failure_code="broker_evidence_invalid",
                )
                return SandboxMutationOutcome(
                    SandboxCode.MUTATION_FAILED,
                    False,
                    job_id=job_id,
                    state="failed",
                    audit_recorded=terminal_audit_recorded,
                )
        state = outcome.status.value
        signed = outcome.status is SandboxRunStatus.SUCCEEDED
        cleanup_confirmed = outcome.status not in {
            SandboxRunStatus.CLEANUP_UNCONFIRMED,
            SandboxRunStatus.UNAVAILABLE,
        }
        failure_code = None if signed else state
        if outcome.status is SandboxRunStatus.CLEANUP_UNCONFIRMED:
            self._quarantined = True
        terminal_audit_recorded = self._append_terminal_audit(
            job_id,
            guild_id,
            owner_user_id,
            state,
            evidence=evidence,
        )
        if not terminal_audit_recorded:
            audit_failure_state = "failed" if signed else state
            audit_failure_code = (
                "cleanup_and_audit_unavailable"
                if outcome.status is SandboxRunStatus.CLEANUP_UNCONFIRMED
                else "terminal_audit_unavailable"
            )
            self._persist_terminal_audit_failure(
                job_id=job_id,
                state=audit_failure_state,
                cleanup_confirmed=cleanup_confirmed,
                evidence=evidence,
                failure_code=audit_failure_code,
            )
            return SandboxMutationOutcome(
                SandboxCode.AUDIT_UNAVAILABLE,
                False,
                job_id=job_id,
                state=audit_failure_state,
            )
        self._update_job(
            job_id,
            state=state,
            signed=signed,
            cleanup_confirmed=cleanup_confirmed,
            failure_code=failure_code,
            evidence=evidence,
        )
        if outcome.status is SandboxRunStatus.SUCCEEDED:
            return SandboxMutationOutcome(
                SandboxCode.OK,
                True,
                job_id=job_id,
                state=state,
                audit_recorded=terminal_audit_recorded,
            )
        code = (
            SandboxCode.SANDBOX_NOT_READY
            if outcome.status is SandboxRunStatus.UNAVAILABLE
            else SandboxCode.MUTATION_FAILED
        )
        return SandboxMutationOutcome(
            code,
            False,
            job_id=job_id,
            state=state,
            audit_recorded=terminal_audit_recorded,
        )

    def _persist_terminal_audit_failure(
        self,
        *,
        job_id: str,
        state: str,
        cleanup_confirmed: bool,
        evidence: HyperVExecutionEvidence | None = None,
        failure_code: str = "terminal_audit_unavailable",
    ) -> None:
        self._update_job(
            job_id,
            state=state,
            signed=False,
            cleanup_confirmed=cleanup_confirmed,
            failure_code=failure_code,
            evidence=evidence,
        )

    def _persist_untyped_cleanup_failure(
        self,
        *,
        job_id: str,
        guild_id: int,
        owner_user_id: int,
        failure_code: str,
    ) -> bool:
        self._quarantined = True
        terminal_audit_recorded = self._append_terminal_audit(
            job_id,
            guild_id,
            owner_user_id,
            "cleanup_unconfirmed",
        )
        self._update_job(
            job_id,
            state="cleanup_unconfirmed",
            signed=False,
            cleanup_confirmed=False,
            failure_code=(failure_code if terminal_audit_recorded else "cleanup_and_audit_unavailable"),
        )
        return terminal_audit_recorded

    async def _cancel(
        self,
        *,
        guild_id: int,
        owner_user_id: int,
        job_id: str,
    ) -> SandboxMutationOutcome:
        row = self._read_job(guild_id=guild_id, owner_user_id=owner_user_id, job_id=job_id)
        if row is None:
            return SandboxMutationOutcome(SandboxCode.JOB_NOT_FOUND, False, job_id=job_id)
        if row["state"] not in {"queued", "running"}:
            return SandboxMutationOutcome(
                SandboxCode.JOB_NOT_CANCELLABLE,
                False,
                job_id=job_id,
                state=str(row["state"]),
            )
        async with self._active_lock:
            active = self._active.get(job_id)
            if active is None or active.guild_id != guild_id or active.owner_user_id != owner_user_id:
                return SandboxMutationOutcome(
                    SandboxCode.JOB_NOT_CANCELLABLE,
                    False,
                    job_id=job_id,
                    state=str(row["state"]),
                )
            if not self._append_audit(
                "sandbox.cancel.requested",
                guild_id=guild_id,
                owner_user_id=owner_user_id,
                details={"job_id": job_id},
            ):
                return SandboxMutationOutcome(
                    SandboxCode.AUDIT_UNAVAILABLE,
                    False,
                    job_id=job_id,
                    state=str(row["state"]),
                )
            task = active.task
            task.cancel()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if (current is not None and current.cancelling()) or not task.done():
                raise
        terminal = self._read_job(guild_id=guild_id, owner_user_id=owner_user_id, job_id=job_id)
        if (
            terminal is not None
            and terminal["cleanup_confirmed"] == 1
            and terminal["failure_code"] == "terminal_audit_unavailable"
        ):
            return SandboxMutationOutcome(
                SandboxCode.AUDIT_UNAVAILABLE,
                False,
                job_id=job_id,
                state=str(terminal["state"]),
            )
        if terminal is None or terminal["state"] != "cancelled" or terminal["cleanup_confirmed"] != 1:
            return SandboxMutationOutcome(SandboxCode.MUTATION_FAILED, False, job_id=job_id)
        if not self._append_audit(
            "sandbox.cancel.completed",
            guild_id=guild_id,
            owner_user_id=owner_user_id,
            details={"job_id": job_id, "state": "cancelled"},
        ):
            self._persist_terminal_audit_failure(
                job_id=job_id,
                state="cancelled",
                cleanup_confirmed=True,
                failure_code="cancel_audit_unavailable",
            )
            return SandboxMutationOutcome(
                SandboxCode.AUDIT_UNAVAILABLE,
                False,
                job_id=job_id,
                state="cancelled",
            )
        return SandboxMutationOutcome(
            SandboxCode.OK,
            True,
            job_id=job_id,
            state="cancelled",
            audit_recorded=True,
        )

    def _status(self, *, guild_id: int, owner_user_id: int) -> SandboxStatusSnapshot:
        execution_count = self._execution_count(guild_id=guild_id, owner_user_id=owner_user_id)
        if self.ready:
            return SandboxStatusSnapshot(
                ready=True,
                contract="live_verified"
                if self._success_count(guild_id=guild_id, owner_user_id=owner_user_id)
                else "protected_ready",
                vm="disposable_ready",
                broker="ready",
                worker="ready",
                execution_count=execution_count,
                blockers=(),
            )
        blocker = "cleanup_unconfirmed" if self._quarantined else "trusted_broker_unavailable"
        return SandboxStatusSnapshot(
            ready=False,
            contract="implemented_offline",
            vm="unverified",
            broker="unavailable",
            worker="unverified",
            execution_count=execution_count,
            blockers=(blocker,),
        )

    def _list_jobs(self, *, guild_id: int, owner_user_id: int, limit: int) -> tuple[SandboxJobView, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        connection = self._required_connection()
        with self._database_lock:
            rows = connection.execute(
                """SELECT job_id, template, state FROM execution_sandbox_discord_job_v1
                WHERE guild_id=? AND owner_user_id=? ORDER BY updated_at DESC, job_id DESC LIMIT ?""",
                (guild_id, owner_user_id, limit),
            ).fetchall()
        return tuple(SandboxJobView(str(row["job_id"]), str(row["template"]), str(row["state"])) for row in rows)

    def _receipt(self, *, guild_id: int, owner_user_id: int, job_id: str) -> SandboxReceiptView | None:
        row = self._read_job(guild_id=guild_id, owner_user_id=owner_user_id, job_id=job_id)
        if row is None or row["state"] not in _TERMINAL_STATES:
            return None
        return SandboxReceiptView(
            job_id=str(row["job_id"]),
            state=str(row["state"]),
            signed=row["signed"] == 1,
            cleanup_confirmed=row["cleanup_confirmed"] == 1,
            failure_code=None if row["failure_code"] is None else str(row["failure_code"]),
        )

    def _insert_job(
        self,
        *,
        job_id: str,
        guild_id: int,
        owner_user_id: int,
        interaction_id: int,
    ) -> bool:
        timestamp = _timestamp()
        connection = self._required_connection()
        try:
            with self._database_lock, connection:
                connection.execute(
                    """INSERT INTO execution_sandbox_discord_job_v1(
                        job_id, guild_id, owner_user_id, interaction_id, template, state,
                        signed, cleanup_confirmed, failure_code, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, 'python-smoke', 'queued', 0, 0, NULL, ?, ?)""",
                    (job_id, guild_id, owner_user_id, interaction_id, timestamp, timestamp),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def _update_job(
        self,
        job_id: str,
        *,
        state: str,
        signed: bool,
        cleanup_confirmed: bool,
        failure_code: str | None,
        evidence: HyperVExecutionEvidence | None = None,
    ) -> None:
        if state not in _TERMINAL_STATES | {"queued", "running"}:
            raise ValueError("invalid sandbox job state")
        connection = self._required_connection()
        with self._database_lock, connection:
            cursor = connection.execute(
                """UPDATE execution_sandbox_discord_job_v1
                SET state=?, signed=?, cleanup_confirmed=?, failure_code=?, updated_at=?,
                    broker_job_id=?,broker_generation=?,broker_policy_digest=?,broker_request_digest=?,
                    broker_scope_digest=?,broker_nonce_digest=?,broker_output_sha256=?,broker_evidence_digest=?
                WHERE job_id=?""",
                (
                    state,
                    int(signed),
                    int(cleanup_confirmed),
                    failure_code,
                    _timestamp(),
                    None if evidence is None else evidence.job_id,
                    None if evidence is None else evidence.generation,
                    None if evidence is None else evidence.policy_digest,
                    None if evidence is None else evidence.request_digest,
                    None if evidence is None else evidence.scope_digest,
                    None if evidence is None else evidence.nonce_digest,
                    None if evidence is None else evidence.output_sha256,
                    None if evidence is None else evidence.evidence_digest,
                    job_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("sandbox job is unavailable")

    def _read_job(self, *, guild_id: int, owner_user_id: int, job_id: str) -> sqlite3.Row | None:
        connection = self._required_connection()
        with self._database_lock:
            return connection.execute(
                """SELECT * FROM execution_sandbox_discord_job_v1
                WHERE guild_id=? AND owner_user_id=? AND job_id=?""",
                (guild_id, owner_user_id, job_id),
            ).fetchone()

    def _execution_count(self, *, guild_id: int, owner_user_id: int) -> int:
        connection = self._required_connection()
        with self._database_lock:
            row = connection.execute(
                """SELECT COUNT(*) FROM execution_sandbox_discord_job_v1
                WHERE guild_id=? AND owner_user_id=? AND state NOT IN ('queued', 'running')""",
                (guild_id, owner_user_id),
            ).fetchone()
        return int(row[0])

    def _success_count(self, *, guild_id: int, owner_user_id: int) -> int:
        connection = self._required_connection()
        with self._database_lock:
            row = connection.execute(
                """SELECT COUNT(*) FROM execution_sandbox_discord_job_v1
                WHERE guild_id=? AND owner_user_id=? AND state='succeeded'""",
                (guild_id, owner_user_id),
            ).fetchone()
        return int(row[0])

    def _append_terminal_audit(
        self,
        job_id: str,
        guild_id: int,
        owner_user_id: int,
        state: str,
        *,
        evidence: HyperVExecutionEvidence | None = None,
    ) -> bool:
        details: dict[str, object] = {"job_id": job_id, "state": state}
        if evidence is not None:
            details.update(
                {
                    "broker_job_id": evidence.job_id,
                    "broker_evidence_digest": evidence.evidence_digest,
                }
            )
        return self._append_audit(
            "sandbox.template.completed",
            guild_id=guild_id,
            owner_user_id=owner_user_id,
            details=details,
        )

    def _append_audit(
        self,
        event: str,
        *,
        guild_id: int,
        owner_user_id: int,
        details: dict[str, object],
    ) -> bool:
        append = self._append_audit_port
        if append is None:
            return False
        try:
            result = append(
                event,
                actor_id=owner_user_id,
                details=details,
                plugin="capability_forge",
                guild_id=guild_id,
            )
            return type(result) is int and result > 0
        except Exception:
            return False

    def _required_connection(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is None:
            raise RuntimeError("sandbox runtime is closed")
        return connection

    def __repr__(self) -> str:
        state = "closing" if self._closing else "ready" if self.ready else "blocked"
        return f"DiscordSandboxRuntime(state={state})"


@dataclass(frozen=True, slots=True)
class _ScopedRuntimeAccess:
    runtime: DiscordSandboxRuntime
    interaction_id: int
    guild_id: int
    channel_id: int
    owner_user_id: int

    async def read_status(self) -> SandboxStatusSnapshot:
        return self.runtime._status(guild_id=self.guild_id, owner_user_id=self.owner_user_id)

    async def list_jobs(self, *, limit: int) -> tuple[SandboxJobView, ...]:
        return self.runtime._list_jobs(guild_id=self.guild_id, owner_user_id=self.owner_user_id, limit=limit)

    async def read_receipt(self, job_id: str) -> SandboxReceiptView | None:
        return self.runtime._receipt(guild_id=self.guild_id, owner_user_id=self.owner_user_id, job_id=job_id)

    async def run_template(self, template: SandboxTemplate) -> SandboxMutationOutcome:
        return await self.runtime._run_template(
            interaction_id=self.interaction_id,
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            owner_user_id=self.owner_user_id,
            template=template,
        )

    async def cancel(self, job_id: str) -> SandboxMutationOutcome:
        return await self.runtime._cancel(
            guild_id=self.guild_id,
            owner_user_id=self.owner_user_id,
            job_id=job_id,
        )


def _positive_id(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 2**63 - 1:
        raise ValueError(f"{field} id must be a positive Discord identifier")
    return value


def _job_id(interaction_id: int) -> str:
    return f"job_d{interaction_id}"


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["DiscordSandboxRuntime", "SandboxLifecyclePort"]
