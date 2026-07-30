"""SQLite-backed durable checkpoints for the bounded orchestration kernel."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import islice
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from yonerai_discord.modules.media_pipeline.domain import ArtifactKind, ArtifactRef

from .discord_inputs import contains_secret_like_text


_MAX_STEPS = 20
_MAX_PUBLIC_OUTPUT_CHARS = 1_900
_MAX_ARTIFACT_JSON_BYTES = 2_048
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DIGEST_RE = re.compile(r"[a-f0-9]{64}\Z")
_MAX_DISCORD_ID = (1 << 64) - 1
_SCHEMA_VERSION = 2
_RUN_STATES = frozenset({"running", "completed", "failed"})
_STEP_STATES = frozenset({"pending", "started", "completed", "failed", "not_run"})
_ACTION_STATES = frozenset({"completed", "denied", "deferred", "unavailable", "failed"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_orchestration_runs (
    idempotency_key TEXT PRIMARY KEY,
    plan_digest TEXT NOT NULL,
    request_id TEXT NOT NULL,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('running', 'completed', 'failed')),
    plan_status TEXT,
    owner_token TEXT,
    lease_until REAL,
    cancel_requested_at REAL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_orchestration_steps (
    idempotency_key TEXT NOT NULL,
    step_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    action_id TEXT NOT NULL,
    effect TEXT NOT NULL CHECK (effect IN ('read_only', 'side_effect')),
    state TEXT NOT NULL CHECK (state IN ('pending', 'started', 'completed', 'failed', 'not_run')),
    action_status TEXT,
    failure_code TEXT,
    public_text TEXT,
    artifact_json TEXT,
    PRIMARY KEY (idempotency_key, step_id),
    FOREIGN KEY (idempotency_key)
        REFERENCES ai_orchestration_runs(idempotency_key)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ai_orchestration_updated
    ON ai_orchestration_runs(updated_at);
"""


class OrchestrationRepositoryError(RuntimeError):
    """Durable orchestration state could not be claimed or committed safely."""


class DurableClaimKind(StrEnum):
    NEW = "new"
    RESUME = "resume"
    REPLAY = "replay"
    BUSY = "busy"


class StartupReconciliationStatus(StrEnum):
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class StartupReconciliationReceipt:
    """Bounded startup state counts without run identities or persisted inputs."""

    status: StartupReconciliationStatus
    running_runs: int
    active_leases: int
    expired_leases: int
    released_read_only_runs: int
    quarantined_side_effect_runs: int
    terminalized_failed_runs: int
    cancellation_requested_runs: int

    def __post_init__(self) -> None:
        if self.status is not StartupReconciliationStatus.COMPLETED:
            raise ValueError("startup reconciliation status is invalid")
        counts = (
            self.running_runs,
            self.active_leases,
            self.expired_leases,
            self.released_read_only_runs,
            self.quarantined_side_effect_runs,
            self.terminalized_failed_runs,
            self.cancellation_requested_runs,
        )
        if any(isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 10_000 for count in counts):
            raise ValueError("startup reconciliation count is outside the bounded range")
        if self.active_leases + self.expired_leases > self.running_runs:
            raise ValueError("startup reconciliation lease counts are inconsistent")
        if (
            self.released_read_only_runs + self.quarantined_side_effect_runs + self.terminalized_failed_runs
            != self.expired_leases
        ):
            raise ValueError("startup reconciliation result counts are inconsistent")
        if self.cancellation_requested_runs > self.expired_leases:
            raise ValueError("startup reconciliation cancellation count is inconsistent")


@dataclass(frozen=True, slots=True)
class DurableStepDefinition:
    step_id: str
    action_id: str
    effect: str


@dataclass(frozen=True, slots=True)
class DurableStepRecord:
    step_id: str
    action_id: str
    effect: str
    state: str
    action_status: str | None = None
    failure_code: str | None = None
    public_text: str | None = field(default=None, repr=False)
    artifact: ArtifactRef | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class DurableRunRecord:
    idempotency_key: str
    plan_digest: str
    request_id: str
    guild_id: int
    channel_id: int
    user_id: int
    state: str
    plan_status: str | None
    steps: tuple[DurableStepRecord, ...]


@dataclass(frozen=True, slots=True)
class DurableClaim:
    kind: DurableClaimKind
    owner_token: str | None
    record: DurableRunRecord


class SqliteOrchestrationRepository:
    """Owns bounded orchestration claims without storing prompts or parameters."""

    def __init__(
        self,
        path: Path,
        *,
        lease_seconds: float = 660.0,
        max_runs: int = 1_024,
        clock: Callable[[], float] = time.time,
        connect: Callable[..., sqlite3.Connection] = sqlite3.connect,
    ) -> None:
        if not isinstance(path, Path):
            raise TypeError("path must be a Path")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not 1.0 <= float(lease_seconds) <= 900.0
        ):
            raise ValueError("lease_seconds is outside the bounded range")
        if isinstance(max_runs, bool) or not isinstance(max_runs, int) or not 1 <= max_runs <= 10_000:
            raise ValueError("max_runs is outside the bounded range")
        self.path = path
        self.lease_seconds = float(lease_seconds)
        self.max_runs = max_runs
        self._clock = clock
        self._connect = connect
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(_SCHEMA)
            self._migrate_schema(connection)

    def reconcile_startup(self) -> StartupReconciliationReceipt:
        """Safely release only expired leases before the runtime is published."""

        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = float(self._clock())
            if not math.isfinite(now):
                connection.rollback()
                raise OrchestrationRepositoryError("durable orchestration clock is invalid")
            rows = connection.execute(
                """
                SELECT idempotency_key, owner_token, lease_until, cancel_requested_at
                FROM ai_orchestration_runs
                WHERE state = 'running'
                ORDER BY updated_at ASC, idempotency_key ASC
                LIMIT ?
                """,
                (self.max_runs + 1,),
            ).fetchall()
            if len(rows) > self.max_runs:
                connection.rollback()
                raise OrchestrationRepositoryError(
                    "durable orchestration startup reconciliation exceeds the bounded range"
                )

            active_leases = 0
            expired: list[tuple[str, bool]] = []
            for raw_key, owner_token, lease_until, cancel_requested_at in rows:
                if (owner_token is None) != (lease_until is None):
                    connection.rollback()
                    raise OrchestrationRepositoryError("durable orchestration lease state is invalid")
                if owner_token is None:
                    continue
                try:
                    lease = float(lease_until)
                except (TypeError, ValueError) as exc:
                    connection.rollback()
                    raise OrchestrationRepositoryError("durable orchestration lease state is invalid") from exc
                if not math.isfinite(lease):
                    connection.rollback()
                    raise OrchestrationRepositoryError("durable orchestration lease state is invalid")
                if lease > now:
                    active_leases += 1
                    continue
                expired.append((str(raw_key), cancel_requested_at is not None))

            released_read_only_runs = 0
            quarantined_side_effect_runs = 0
            terminalized_failed_runs = 0
            cancellation_requested_runs = 0
            for idempotency_key, cancellation_requested in expired:
                if cancellation_requested:
                    cancellation_requested_runs += 1
                step_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM ai_orchestration_steps
                        WHERE idempotency_key = ?
                        """,
                        (idempotency_key,),
                    ).fetchone()[0]
                )
                if not 1 <= step_count <= _MAX_STEPS:
                    connection.rollback()
                    raise OrchestrationRepositoryError("durable orchestration step rows are invalid")
                has_side_effect = connection.execute(
                    """
                    SELECT 1
                    FROM ai_orchestration_steps
                    WHERE idempotency_key = ? AND effect = 'side_effect'
                    LIMIT 1
                    """,
                    (idempotency_key,),
                ).fetchone()
                if has_side_effect is not None:
                    self._fail_incomplete_side_effect_plan(connection, idempotency_key, now)
                    quarantined_side_effect_runs += 1
                    continue
                if self._terminalize_checkpointed_failure(connection, idempotency_key, now):
                    terminalized_failed_runs += 1
                    continue
                connection.execute(
                    """
                    UPDATE ai_orchestration_steps
                    SET state = 'pending', action_status = NULL, failure_code = NULL,
                        public_text = NULL, artifact_json = NULL
                    WHERE idempotency_key = ? AND state = 'started'
                    """,
                    (idempotency_key,),
                )
                cursor = connection.execute(
                    """
                    UPDATE ai_orchestration_runs
                    SET owner_token = NULL, lease_until = NULL, updated_at = ?
                    WHERE idempotency_key = ? AND state = 'running'
                          AND owner_token IS NOT NULL AND lease_until <= ?
                    """,
                    (now, idempotency_key, now),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    raise OrchestrationRepositoryError(
                        "durable orchestration startup reconciliation lost lease ownership"
                    )
                released_read_only_runs += 1

            receipt = StartupReconciliationReceipt(
                StartupReconciliationStatus.COMPLETED,
                running_runs=len(rows),
                active_leases=active_leases,
                expired_leases=len(expired),
                released_read_only_runs=released_read_only_runs,
                quarantined_side_effect_runs=quarantined_side_effect_runs,
                terminalized_failed_runs=terminalized_failed_runs,
                cancellation_requested_runs=cancellation_requested_runs,
            )
            connection.commit()
            return receipt

    def claim(
        self,
        *,
        idempotency_key: str,
        plan_digest: str,
        request_id: str,
        guild_id: int,
        channel_id: int,
        user_id: int,
        steps: Iterable[DurableStepDefinition],
    ) -> DurableClaim:
        idempotency_key = _identifier(idempotency_key, "idempotency_key")
        plan_digest = _digest(plan_digest)
        request_id = _identifier(request_id, "request_id")
        guild_id = _discord_id(guild_id, "guild_id")
        channel_id = _discord_id(channel_id, "channel_id")
        user_id = _discord_id(user_id, "user_id")
        definitions = _bounded_steps(steps)
        if not 1 <= len(definitions) <= _MAX_STEPS:
            raise OrchestrationRepositoryError("durable plan step count is outside the bounded range")
        if any(not isinstance(step, DurableStepDefinition) for step in definitions):
            raise OrchestrationRepositoryError("durable plan step definition is invalid")
        definitions = tuple(
            DurableStepDefinition(
                _identifier(step.step_id, "step_id"),
                _identifier(step.action_id, "action_id"),
                step.effect,
            )
            for step in definitions
        )
        if any(step.effect not in {"read_only", "side_effect"} for step in definitions):
            raise OrchestrationRepositoryError("durable plan step definition is invalid")
        if len({step.step_id for step in definitions}) != len(definitions):
            raise OrchestrationRepositoryError("durable plan step IDs must be unique")
        now = float(self._clock())
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT plan_digest, request_id, guild_id, channel_id, user_id,
                       state, plan_status, owner_token, lease_until
                FROM ai_orchestration_runs
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if row is None:
                self._prune_terminal_runs(connection)
                owner_token = uuid4().hex
                connection.execute(
                    """
                    INSERT INTO ai_orchestration_runs(
                        idempotency_key, plan_digest, request_id, guild_id, channel_id,
                        user_id, state, owner_token, lease_until, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
                    """,
                    (
                        idempotency_key,
                        plan_digest,
                        request_id,
                        guild_id,
                        channel_id,
                        user_id,
                        owner_token,
                        now + self.lease_seconds,
                        now,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO ai_orchestration_steps(
                        idempotency_key, step_id, ordinal, action_id, effect, state
                    ) VALUES (?, ?, ?, ?, ?, 'pending')
                    """,
                    (
                        (idempotency_key, step.step_id, ordinal, step.action_id, step.effect)
                        for ordinal, step in enumerate(definitions)
                    ),
                )
                record = self._load_record(connection, idempotency_key)
                connection.commit()
                return DurableClaim(DurableClaimKind.NEW, owner_token, record)

            expected = (plan_digest, request_id, guild_id, channel_id, user_id)
            if tuple(row[:5]) != expected:
                connection.rollback()
                raise OrchestrationRepositoryError("durable idempotency key belongs to another plan or scope")
            self._verify_definitions(connection, idempotency_key, definitions)
            state = str(row[5])
            if state in {"completed", "failed"}:
                record = self._load_record(connection, idempotency_key)
                connection.commit()
                return DurableClaim(DurableClaimKind.REPLAY, None, record)
            lease_until = row[8]
            if row[7] is not None and lease_until is not None and float(lease_until) > now:
                record = self._load_record(connection, idempotency_key)
                connection.commit()
                return DurableClaim(DurableClaimKind.BUSY, None, record)

            step_rows = connection.execute(
                """
                SELECT step_id, effect, state
                FROM ai_orchestration_steps
                WHERE idempotency_key = ?
                ORDER BY ordinal
                """,
                (idempotency_key,),
            ).fetchall()
            if self._terminalize_checkpointed_failure(connection, idempotency_key, now):
                record = self._load_record(connection, idempotency_key)
                connection.commit()
                return DurableClaim(DurableClaimKind.REPLAY, None, record)
            if any(str(step[1]) == "side_effect" for step in step_rows):
                self._fail_incomplete_side_effect_plan(connection, idempotency_key, now)
                record = self._load_record(connection, idempotency_key)
                connection.commit()
                return DurableClaim(DurableClaimKind.REPLAY, None, record)

            connection.execute(
                """
                UPDATE ai_orchestration_steps
                SET state = 'pending', action_status = NULL, failure_code = NULL,
                    public_text = NULL, artifact_json = NULL
                WHERE idempotency_key = ? AND state = 'started'
                """,
                (idempotency_key,),
            )
            owner_token = uuid4().hex
            connection.execute(
                """
                UPDATE ai_orchestration_runs
                SET owner_token = ?, lease_until = ?, updated_at = ?
                WHERE idempotency_key = ? AND state = 'running'
                """,
                (owner_token, now + self.lease_seconds, now, idempotency_key),
            )
            record = self._load_record(connection, idempotency_key)
            connection.commit()
            return DurableClaim(DurableClaimKind.RESUME, owner_token, record)

    def mark_step_started(
        self,
        *,
        idempotency_key: str,
        plan_digest: str,
        owner_token: str,
        step_id: str,
    ) -> bool:
        now = float(self._clock())
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_owner(connection, idempotency_key, plan_digest, owner_token)
            cancellation = connection.execute(
                """
                SELECT cancel_requested_at
                FROM ai_orchestration_runs
                WHERE idempotency_key = ? AND plan_digest = ? AND owner_token = ?
                      AND state = 'running'
                """,
                (idempotency_key, plan_digest, owner_token),
            ).fetchone()
            if cancellation is None:
                connection.rollback()
                raise OrchestrationRepositoryError("durable orchestration ownership is not current")
            if cancellation[0] is not None:
                connection.rollback()
                return False
            cursor = connection.execute(
                """
                UPDATE ai_orchestration_steps
                SET state = 'started', action_status = NULL, failure_code = NULL,
                    public_text = NULL, artifact_json = NULL
                WHERE idempotency_key = ? AND step_id = ? AND state = 'pending'
                """,
                (idempotency_key, step_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise OrchestrationRepositoryError("durable step cannot be started from its current state")
            self._renew(connection, idempotency_key, owner_token, now)
            connection.commit()
            return True

    def request_cancel(
        self,
        *,
        idempotency_key: str,
        plan_digest: str,
        request_id: str,
        guild_id: int,
        channel_id: int,
        user_id: int,
    ) -> bool:
        """Persist an exact-scope cancellation request for a currently running plan."""

        idempotency_key = _identifier(idempotency_key, "idempotency_key")
        plan_digest = _digest(plan_digest)
        request_id = _identifier(request_id, "request_id")
        guild_id = _discord_id(guild_id, "guild_id")
        channel_id = _discord_id(channel_id, "channel_id")
        user_id = _discord_id(user_id, "user_id")
        now = float(self._clock())
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT plan_digest, request_id, guild_id, channel_id, user_id,
                       state, cancel_requested_at
                FROM ai_orchestration_runs
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            expected = (plan_digest, request_id, guild_id, channel_id, user_id)
            if row is None or tuple(row[:5]) != expected or str(row[5]) != "running":
                connection.rollback()
                return False
            if row[6] is None:
                connection.execute(
                    """
                    UPDATE ai_orchestration_runs
                    SET cancel_requested_at = ?, updated_at = ?
                    WHERE idempotency_key = ? AND state = 'running'
                    """,
                    (now, now, idempotency_key),
                )
            connection.commit()
            return True

    def cancellation_requested(
        self,
        *,
        idempotency_key: str,
        plan_digest: str,
        request_id: str,
        guild_id: int,
        channel_id: int,
        user_id: int,
    ) -> bool:
        """Read a cancellation bit only after exact plan and Discord scope binding."""

        idempotency_key = _identifier(idempotency_key, "idempotency_key")
        plan_digest = _digest(plan_digest)
        request_id = _identifier(request_id, "request_id")
        guild_id = _discord_id(guild_id, "guild_id")
        channel_id = _discord_id(channel_id, "channel_id")
        user_id = _discord_id(user_id, "user_id")
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT plan_digest, request_id, guild_id, channel_id, user_id,
                       state, cancel_requested_at
                FROM ai_orchestration_runs
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
        expected = (plan_digest, request_id, guild_id, channel_id, user_id)
        if row is None or tuple(row[:5]) != expected:
            raise OrchestrationRepositoryError("durable cancellation scope does not match the plan")
        return str(row[5]) == "running" and row[6] is not None

    def checkpoint_step(
        self,
        *,
        idempotency_key: str,
        plan_digest: str,
        owner_token: str,
        step_id: str,
        state: str,
        action_status: str | None,
        failure_code: str | None,
        public_text: str | None = None,
        artifact: ArtifactRef | None = None,
    ) -> None:
        if state not in {"completed", "failed"}:
            raise OrchestrationRepositoryError("durable terminal step state is invalid")
        _validate_step_result(state, action_status, failure_code)
        _validate_public_text(public_text)
        artifact_json = _artifact_to_json(artifact)
        now = float(self._clock())
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_owner(connection, idempotency_key, plan_digest, owner_token)
            cursor = connection.execute(
                """
                UPDATE ai_orchestration_steps
                SET state = ?, action_status = ?, failure_code = ?,
                    public_text = ?, artifact_json = ?
                WHERE idempotency_key = ? AND step_id = ? AND state IN ('pending', 'started')
                """,
                (
                    state,
                    action_status,
                    failure_code,
                    public_text,
                    artifact_json,
                    idempotency_key,
                    step_id,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise OrchestrationRepositoryError("durable step checkpoint conflicts with current state")
            self._renew(connection, idempotency_key, owner_token, now)
            connection.commit()

    def complete(
        self,
        *,
        idempotency_key: str,
        plan_digest: str,
        owner_token: str,
        plan_status: str,
        steps: Iterable[DurableStepRecord],
    ) -> DurableRunRecord:
        if plan_status not in {"completed", "failed"}:
            raise OrchestrationRepositoryError("durable plan status is invalid")
        records = _bounded_steps(steps)
        if not 1 <= len(records) <= _MAX_STEPS:
            raise OrchestrationRepositoryError("durable terminal step count is invalid")
        if any(not isinstance(record, DurableStepRecord) for record in records):
            raise OrchestrationRepositoryError("durable terminal step receipt is invalid")
        if plan_status == "completed" and any(record.state != "completed" for record in records):
            raise OrchestrationRepositoryError("completed durable plan contains a non-completed step")
        now = float(self._clock())
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_owner(connection, idempotency_key, plan_digest, owner_token)
            if plan_status == "failed" and not any(record.state == "failed" for record in records):
                cancellation = connection.execute(
                    """
                    SELECT cancel_requested_at
                    FROM ai_orchestration_runs
                    WHERE idempotency_key = ? AND plan_digest = ? AND owner_token = ?
                          AND state = 'running'
                    """,
                    (idempotency_key, plan_digest, owner_token),
                ).fetchone()
                truthful_in_flight_completion = (
                    cancellation is not None
                    and cancellation[0] is not None
                    and any(record.state == "completed" for record in records)
                    and all(record.state in {"completed", "not_run"} for record in records)
                )
                if not truthful_in_flight_completion:
                    connection.rollback()
                    raise OrchestrationRepositoryError("failed durable plan has no failed step")
            stored = {
                str(row[0]): (str(row[1]), str(row[2]))
                for row in connection.execute(
                    """
                    SELECT step_id, action_id, effect
                    FROM ai_orchestration_steps
                    WHERE idempotency_key = ?
                    """,
                    (idempotency_key,),
                )
            }
            supplied = {record.step_id: (record.action_id, record.effect) for record in records}
            if len(supplied) != len(records) or stored != supplied:
                connection.rollback()
                raise OrchestrationRepositoryError("durable terminal receipt does not match the plan")
            for record in records:
                if record.state not in {"completed", "failed", "not_run"}:
                    connection.rollback()
                    raise OrchestrationRepositoryError("durable terminal step state is invalid")
                _validate_step_result(record.state, record.action_status, record.failure_code)
                _validate_public_text(record.public_text)
                connection.execute(
                    """
                    UPDATE ai_orchestration_steps
                    SET state = ?, action_status = ?, failure_code = ?,
                        public_text = ?, artifact_json = ?
                    WHERE idempotency_key = ? AND step_id = ? AND action_id = ?
                    """,
                    (
                        record.state,
                        record.action_status,
                        record.failure_code,
                        record.public_text,
                        _artifact_to_json(record.artifact),
                        idempotency_key,
                        record.step_id,
                        record.action_id,
                    ),
                )
            connection.execute(
                """
                UPDATE ai_orchestration_runs
                SET state = ?, plan_status = ?, owner_token = NULL,
                    lease_until = NULL, updated_at = ?
                WHERE idempotency_key = ?
                """,
                (plan_status, plan_status, now, idempotency_key),
            )
            record = self._load_record(connection, idempotency_key)
            connection.commit()
            return record

    def abandon(
        self,
        *,
        idempotency_key: str,
        plan_digest: str,
        owner_token: str,
    ) -> None:
        """Release read-only work; quarantine any plan containing side effects."""

        now = float(self._clock())
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_owner(connection, idempotency_key, plan_digest, owner_token)
            has_side_effect = connection.execute(
                """
                SELECT 1 FROM ai_orchestration_steps
                WHERE idempotency_key = ? AND effect = 'side_effect'
                LIMIT 1
                """,
                (idempotency_key,),
            ).fetchone()
            if has_side_effect is not None:
                self._fail_incomplete_side_effect_plan(connection, idempotency_key, now)
            else:
                connection.execute(
                    """
                    UPDATE ai_orchestration_runs
                    SET owner_token = NULL, lease_until = NULL, updated_at = ?
                    WHERE idempotency_key = ? AND state = 'running'
                    """,
                    (now, idempotency_key),
                )
            connection.commit()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect(
            self.path,
            timeout=5.0,
            check_same_thread=False,
            isolation_level=None,
        )
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
        finally:
            connection.close()

    def _migrate_schema(self, connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > _SCHEMA_VERSION:
            raise OrchestrationRepositoryError("durable orchestration schema is newer than supported")
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(ai_orchestration_runs)")}
        connection.execute("BEGIN IMMEDIATE")
        try:
            if "cancel_requested_at" not in columns:
                connection.execute("ALTER TABLE ai_orchestration_runs ADD COLUMN cancel_requested_at REAL")
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def _verify_definitions(
        self,
        connection: sqlite3.Connection,
        idempotency_key: str,
        definitions: tuple[DurableStepDefinition, ...],
    ) -> None:
        rows = connection.execute(
            """
            SELECT step_id, action_id, effect
            FROM ai_orchestration_steps
            WHERE idempotency_key = ?
            ORDER BY ordinal
            """,
            (idempotency_key,),
        ).fetchall()
        expected = tuple((step.step_id, step.action_id, step.effect) for step in definitions)
        if tuple(tuple(str(value) for value in row) for row in rows) != expected:
            connection.rollback()
            raise OrchestrationRepositoryError("durable idempotency key has conflicting step metadata")

    def _require_owner(
        self,
        connection: sqlite3.Connection,
        idempotency_key: str,
        plan_digest: str,
        owner_token: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT plan_digest, state, owner_token, lease_until
            FROM ai_orchestration_runs
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        now = float(self._clock())
        if (
            row is None
            or str(row[0]) != plan_digest
            or str(row[1]) != "running"
            or row[2] != owner_token
            or row[3] is None
            or float(row[3]) <= now
        ):
            raise OrchestrationRepositoryError("durable orchestration ownership is not current")

    def _renew(
        self,
        connection: sqlite3.Connection,
        idempotency_key: str,
        owner_token: str,
        now: float,
    ) -> None:
        connection.execute(
            """
            UPDATE ai_orchestration_runs
            SET lease_until = ?, updated_at = ?
            WHERE idempotency_key = ? AND owner_token = ? AND state = 'running'
            """,
            (now + self.lease_seconds, now, idempotency_key, owner_token),
        )

    def _fail_incomplete_side_effect_plan(
        self,
        connection: sqlite3.Connection,
        idempotency_key: str,
        now: float,
    ) -> None:
        if self._terminalize_checkpointed_failure(connection, idempotency_key, now):
            return
        rows = connection.execute(
            """
            SELECT step_id, effect, state
            FROM ai_orchestration_steps
            WHERE idempotency_key = ?
            ORDER BY ordinal
            """,
            (idempotency_key,),
        ).fetchall()
        first_failure_written = False
        for step_id, effect, state in rows:
            state = str(state)
            if state in {"completed", "failed"}:
                continue
            if not first_failure_written:
                failure = (
                    "side_effect_uncertain" if str(effect) == "side_effect" and state == "started" else "restart_unsafe"
                )
                connection.execute(
                    """
                    UPDATE ai_orchestration_steps
                    SET state = 'failed', failure_code = ?
                    WHERE idempotency_key = ? AND step_id = ?
                    """,
                    (failure, idempotency_key, str(step_id)),
                )
                first_failure_written = True
            else:
                connection.execute(
                    """
                    UPDATE ai_orchestration_steps
                    SET state = 'not_run', failure_code = 'not_started'
                    WHERE idempotency_key = ? AND step_id = ?
                    """,
                    (idempotency_key, str(step_id)),
                )
        if not first_failure_written and rows:
            step_id = str(rows[-1][0])
            connection.execute(
                """
                UPDATE ai_orchestration_steps
                SET state = 'failed', action_status = 'completed',
                    failure_code = 'terminal_commit_uncertain',
                    public_text = NULL, artifact_json = NULL
                WHERE idempotency_key = ? AND step_id = ?
                """,
                (idempotency_key, step_id),
            )
        connection.execute(
            """
            UPDATE ai_orchestration_runs
            SET state = 'failed', plan_status = 'failed', owner_token = NULL,
                lease_until = NULL, updated_at = ?
            WHERE idempotency_key = ?
            """,
            (now, idempotency_key),
        )

    def _terminalize_checkpointed_failure(
        self,
        connection: sqlite3.Connection,
        idempotency_key: str,
        now: float,
    ) -> bool:
        failed = connection.execute(
            """
            SELECT 1
            FROM ai_orchestration_steps
            WHERE idempotency_key = ? AND state = 'failed'
            LIMIT 1
            """,
            (idempotency_key,),
        ).fetchone()
        if failed is None:
            return False
        connection.execute(
            """
            UPDATE ai_orchestration_steps
            SET state = 'not_run', action_status = NULL,
                failure_code = 'not_started', public_text = NULL, artifact_json = NULL
            WHERE idempotency_key = ? AND state IN ('pending', 'started')
            """,
            (idempotency_key,),
        )
        connection.execute(
            """
            UPDATE ai_orchestration_runs
            SET state = 'failed', plan_status = 'failed', owner_token = NULL,
                lease_until = NULL, updated_at = ?
            WHERE idempotency_key = ?
            """,
            (now, idempotency_key),
        )
        return True

    def _prune_terminal_runs(self, connection: sqlite3.Connection) -> None:
        count = int(connection.execute("SELECT COUNT(*) FROM ai_orchestration_runs").fetchone()[0])
        overflow = count - self.max_runs + 1
        if overflow <= 0:
            return
        connection.execute(
            """
            DELETE FROM ai_orchestration_runs
            WHERE idempotency_key IN (
                SELECT idempotency_key
                FROM ai_orchestration_runs
                WHERE state IN ('completed', 'failed')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM ai_orchestration_steps
                      WHERE ai_orchestration_steps.idempotency_key =
                            ai_orchestration_runs.idempotency_key
                        AND ai_orchestration_steps.effect = 'side_effect'
                  )
                ORDER BY updated_at ASC, idempotency_key ASC
                LIMIT ?
            )
            """,
            (overflow,),
        )
        remaining = int(connection.execute("SELECT COUNT(*) FROM ai_orchestration_runs").fetchone()[0])
        if remaining >= self.max_runs:
            raise OrchestrationRepositoryError("durable orchestration capacity is exhausted by active runs")

    def _load_record(self, connection: sqlite3.Connection, idempotency_key: str) -> DurableRunRecord:
        row = connection.execute(
            """
            SELECT idempotency_key, plan_digest, request_id, guild_id, channel_id,
                   user_id, state, plan_status
            FROM ai_orchestration_runs
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        if row is None:
            raise OrchestrationRepositoryError("durable orchestration run is missing")
        step_rows = connection.execute(
            """
            SELECT step_id, action_id, effect, state, action_status,
                   failure_code, public_text, artifact_json
            FROM ai_orchestration_steps
            WHERE idempotency_key = ?
            ORDER BY ordinal
            """,
            (idempotency_key,),
        ).fetchall()
        if not 1 <= len(step_rows) <= _MAX_STEPS:
            raise OrchestrationRepositoryError("durable orchestration step rows are invalid")
        steps = tuple(
            DurableStepRecord(
                step_id=str(step[0]),
                action_id=str(step[1]),
                effect=str(step[2]),
                state=str(step[3]),
                action_status=str(step[4]) if step[4] is not None else None,
                failure_code=str(step[5]) if step[5] is not None else None,
                public_text=str(step[6]) if step[6] is not None else None,
                artifact=_artifact_from_json(step[7]),
            )
            for step in step_rows
        )
        state = str(row[6])
        if state not in _RUN_STATES or any(step.state not in _STEP_STATES for step in steps):
            raise OrchestrationRepositoryError("durable orchestration state is invalid")
        return DurableRunRecord(
            idempotency_key=str(row[0]),
            plan_digest=str(row[1]),
            request_id=str(row[2]),
            guild_id=int(row[3]),
            channel_id=int(row[4]),
            user_id=int(row[5]),
            state=state,
            plan_status=str(row[7]) if row[7] is not None else None,
            steps=steps,
        )


def _artifact_to_json(artifact: ArtifactRef | None) -> str | None:
    if artifact is None:
        return None
    if not isinstance(artifact, ArtifactRef):
        raise OrchestrationRepositoryError("durable artifact metadata must be an ArtifactRef")
    encoded = json.dumps(
        {
            "artifact_id": artifact.artifact_id,
            "scope_digest": artifact.scope_digest,
            "recipe_digest": artifact.recipe_digest,
            "content_digest": artifact.content_digest,
            "kind": artifact.kind.value,
            "width": artifact.width,
            "height": artifact.height,
            "byte_size": artifact.byte_size,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_JSON_BYTES:
        raise OrchestrationRepositoryError("durable artifact metadata exceeds the bounded range")
    return encoded


def _artifact_from_json(value: Any) -> ArtifactRef | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_ARTIFACT_JSON_BYTES:
        raise OrchestrationRepositoryError("durable artifact metadata is invalid")
    try:
        raw = json.loads(value)
        if not isinstance(raw, dict) or set(raw) != {
            "artifact_id",
            "scope_digest",
            "recipe_digest",
            "content_digest",
            "kind",
            "width",
            "height",
            "byte_size",
        }:
            raise ValueError
        return ArtifactRef(
            artifact_id=raw["artifact_id"],
            scope_digest=raw["scope_digest"],
            recipe_digest=raw["recipe_digest"],
            content_digest=raw["content_digest"],
            kind=ArtifactKind(raw["kind"]),
            width=raw["width"],
            height=raw["height"],
            byte_size=raw["byte_size"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OrchestrationRepositoryError("durable artifact metadata is invalid") from exc


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise OrchestrationRepositoryError(f"durable {label} is invalid")
    return value


def _digest(value: Any) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise OrchestrationRepositoryError("durable plan digest is invalid")
    return value


def _discord_id(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_DISCORD_ID:
        raise OrchestrationRepositoryError(f"durable {label} is invalid")
    return value


def _validate_public_text(value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not 1 <= len(value) <= _MAX_PUBLIC_OUTPUT_CHARS:
        raise OrchestrationRepositoryError("durable public output is outside the bounded range")
    if contains_secret_like_text(value):
        raise OrchestrationRepositoryError("durable public output contains secret-like text")


def _validate_step_result(
    state: str,
    action_status: str | None,
    failure_code: str | None,
) -> None:
    if action_status is not None and action_status not in _ACTION_STATES:
        raise OrchestrationRepositoryError("durable action status is invalid")
    if failure_code is not None:
        _identifier(failure_code, "failure_code")
    if state == "completed":
        if action_status != "completed" or failure_code is not None:
            raise OrchestrationRepositoryError("durable completed step result is invalid")
    elif state == "failed":
        if failure_code is None:
            raise OrchestrationRepositoryError("durable failed step needs a failure code")
    elif state == "not_run":
        if action_status is not None or failure_code is None:
            raise OrchestrationRepositoryError("durable not-run step result is invalid")
    else:
        raise OrchestrationRepositoryError("durable terminal step state is invalid")


def _bounded_steps(values: Iterable[Any]) -> tuple[Any, ...]:
    try:
        return tuple(islice(iter(values), _MAX_STEPS + 1))
    except TypeError as exc:
        raise OrchestrationRepositoryError("durable step collection is invalid") from exc
