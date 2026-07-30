"""Content-free SQLite journal for the sealed Search Fabric autonomy task."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import math
import re
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from yonerai_discord.db import Database
from yonerai_discord.provider_registry.domain import ArtifactKind, ArtifactRef

from .autonomy import (
    AutonomyBinding,
    AutonomyCheckpoint,
    AutonomyCheckpointError,
    AutonomyPlanStatus,
    AutonomyStep,
    AutonomyTerminalKind,
    AutonomyTerminalNotice,
    AutonomyTaskTemplate,
    DurableAutonomyCheckpoint,
    SEARXNG_OFFICIAL_RESEARCH_V1,
    StepAttempt,
)


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_JOURNAL_COLUMNS = """
task_id, template_id, template_version, plan_digest, binding_digest,
store_binding_digest, status, completed_prefix, attempts_json,
artifact_id, artifact_kind, artifact_media_type, artifact_size_bytes,
artifact_sha256, failure_code, terminal_emitted, lease_owner,
lease_expires_at, revision
"""

IdentityCurrent = Callable[[], object | None]


@dataclass(frozen=True, slots=True)
class SqliteAutonomyJournal:
    """Use the existing control database as a bounded autonomy checkpoint port."""

    database: Database = field(repr=False)
    database_current: IdentityCurrent = field(repr=False)
    store_binding_digest: str
    _lease_token: contextvars.ContextVar[str | None] = field(
        default_factory=lambda: contextvars.ContextVar("autonomy_journal_lease", default=None),
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.database, Database):
            raise TypeError("database must be Database")
        if not callable(self.database_current):
            raise TypeError("database_current must be callable")
        if not isinstance(self.store_binding_digest, str) or _DIGEST.fullmatch(self.store_binding_digest) is None:
            raise ValueError("store_binding_digest must be a SHA-256 digest")

    @property
    def ready(self) -> bool:
        try:
            return self.database.is_open and self.database_current() is self.database
        except Exception:
            return False

    async def acquire_execution(
        self,
        binding: AutonomyBinding,
        template: AutonomyTaskTemplate,
        *,
        lease_seconds: float,
    ) -> bool:
        if not isinstance(binding, AutonomyBinding):
            raise TypeError("binding must be AutonomyBinding")
        if not isinstance(template, AutonomyTaskTemplate):
            raise TypeError("template must be AutonomyTaskTemplate")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not math.isfinite(float(lease_seconds))
            or not 1.0 <= float(lease_seconds) <= 1_800.0
        ):
            raise ValueError("lease_seconds is outside the bounded contract")
        self._require_current()
        token = secrets.token_hex(32)
        expires_at = int(time.time() + math.ceil(float(lease_seconds)))
        try:
            acquired = await asyncio.to_thread(
                self._claim_execution,
                binding,
                template,
                token,
                expires_at,
            )
        except sqlite3.Error:
            raise AutonomyCheckpointError("autonomy execution lease failed") from None
        self._require_current()
        if acquired:
            self._lease_token.set(token)
        return acquired

    async def release_execution(self) -> None:
        token = self._lease_token.get()
        if token is None:
            return
        try:
            await asyncio.to_thread(self._release_execution, token)
        except sqlite3.Error:
            raise AutonomyCheckpointError("autonomy execution lease cleanup failed") from None
        finally:
            self._lease_token.set(None)

    async def load(self, task_id: str) -> DurableAutonomyCheckpoint | None:
        _validate_task_id(task_id)
        self._require_current()
        token = self._require_lease_token()
        try:
            row = await asyncio.to_thread(self._read_row, task_id, token)
        except sqlite3.Error:
            raise AutonomyCheckpointError("autonomy journal read failed") from None
        self._require_current()
        if row is None:
            return None
        return _decode_row(row, expected_store_digest=self.store_binding_digest)

    async def save(self, task_id: str, checkpoint: AutonomyCheckpoint) -> None:
        _validate_task_id(task_id)
        if not isinstance(checkpoint, AutonomyCheckpoint):
            raise TypeError("checkpoint must be AutonomyCheckpoint")
        self._require_current()
        token = self._require_lease_token()
        projection = _project_checkpoint(task_id, checkpoint, self.store_binding_digest)
        try:
            await asyncio.to_thread(self._write_projection, projection, token)
        except sqlite3.Error:
            raise AutonomyCheckpointError("autonomy journal write failed") from None
        self._require_current()

    async def publish_once(self, notice: AutonomyTerminalNotice) -> None:
        if not isinstance(notice, AutonomyTerminalNotice):
            raise TypeError("notice must be AutonomyTerminalNotice")
        self._require_current()
        token = self._require_lease_token()
        try:
            await asyncio.to_thread(self._record_terminal, notice, token)
        except sqlite3.Error:
            raise AutonomyCheckpointError("autonomy terminal journal write failed") from None
        self._require_current()

    def _require_current(self) -> None:
        try:
            current = self.database_current()
        except Exception:
            raise AutonomyCheckpointError("autonomy journal identity is unavailable") from None
        if current is not self.database or not self.database.is_open:
            raise AutonomyCheckpointError("autonomy journal identity is no longer current")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _require_lease_token(self) -> str:
        token = self._lease_token.get()
        if token is None or _DIGEST.fullmatch(token) is None:
            raise AutonomyCheckpointError("autonomy execution lease is unavailable")
        return token

    def _claim_execution(
        self,
        binding: AutonomyBinding,
        template: AutonomyTaskTemplate,
        token: str,
        expires_at: int,
    ) -> bool:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT {_JOURNAL_COLUMNS} FROM autonomy_checkpoint_journal WHERE task_id = ?",
                (binding.task_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO autonomy_checkpoint_journal(
                        task_id, template_id, template_version, plan_digest,
                        binding_digest, store_binding_digest, status,
                        completed_prefix, attempts_json, artifact_id,
                        artifact_kind, artifact_media_type, artifact_size_bytes,
                        artifact_sha256, failure_code, terminal_emitted,
                        lease_owner, lease_expires_at, revision
                    ) VALUES (?, ?, ?, ?, ?, ?, 'running', 0, '[]',
                              NULL, NULL, NULL, NULL, NULL, NULL, 0, ?, ?, 1)
                    """,
                    (
                        binding.task_id,
                        template.template_id,
                        template.version,
                        template.digest,
                        binding.digest,
                        self.store_binding_digest,
                        token,
                        expires_at,
                    ),
                )
                return True
            identity = {
                "template_id": template.template_id,
                "template_version": template.version,
                "plan_digest": template.digest,
                "binding_digest": binding.digest,
                "store_binding_digest": self.store_binding_digest,
            }
            _require_same_identity(row, identity)
            if row["lease_owner"] is not None and int(row["lease_expires_at"]) > now:
                return False
            connection.execute(
                """
                UPDATE autonomy_checkpoint_journal
                SET lease_owner = ?, lease_expires_at = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE task_id = ?
                """,
                (token, expires_at, binding.task_id),
            )
            return True

    def _release_execution(self, token: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE autonomy_checkpoint_journal
                SET lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE lease_owner = ?
                """,
                (token,),
            )

    def _read_row(self, task_id: str, token: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_JOURNAL_COLUMNS} FROM autonomy_checkpoint_journal WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            _require_owned_lease(row, token)
            return row

    def _write_projection(self, value: dict[str, object], token: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                f"SELECT {_JOURNAL_COLUMNS} FROM autonomy_checkpoint_journal WHERE task_id = ?",
                (value["task_id"],),
            ).fetchone()
            if existing is None:
                raise AutonomyCheckpointError("autonomy execution lease row is missing")
            _require_owned_lease(existing, token)
            _require_same_identity(existing, value)
            _require_valid_transition(existing, value)
            if _row_equals_projection(existing, value):
                return
            connection.execute(
                """
                UPDATE autonomy_checkpoint_journal SET
                    status = ?, completed_prefix = ?, attempts_json = ?,
                    artifact_id = ?, artifact_kind = ?, artifact_media_type = ?,
                    artifact_size_bytes = ?, artifact_sha256 = ?,
                    failure_code = ?, terminal_emitted = ?,
                    revision = revision + 1,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE task_id = ?
                """,
                (
                    value["status"],
                    value["completed_prefix"],
                    value["attempts_json"],
                    value["artifact_id"],
                    value["artifact_kind"],
                    value["artifact_media_type"],
                    value["artifact_size_bytes"],
                    value["artifact_sha256"],
                    value["failure_code"],
                    value["terminal_emitted"],
                    value["task_id"],
                ),
            )

    def _record_terminal(self, notice: AutonomyTerminalNotice, token: str) -> None:
        receipt = notice.receipt
        expected_key = _terminal_key(receipt.task_id, receipt.binding_digest, receipt.plan_digest)
        if notice.idempotency_key != expected_key:
            raise AutonomyCheckpointError("terminal idempotency binding is invalid")
        expected_kind = (
            AutonomyTerminalKind.FINAL if receipt.status is AutonomyPlanStatus.COMPLETED else AutonomyTerminalKind.ERROR
        )
        if notice.kind is not expected_kind:
            raise AutonomyCheckpointError("terminal kind does not match its receipt")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT {_JOURNAL_COLUMNS} FROM autonomy_checkpoint_journal WHERE task_id = ?",
                (receipt.task_id,),
            ).fetchone()
            if row is None:
                raise AutonomyCheckpointError("terminal checkpoint is missing")
            _require_owned_lease(row, token)
            durable = _decode_row(row, expected_store_digest=self.store_binding_digest)
            if (
                durable.template_id != receipt.template_id
                or durable.template_version != receipt.template_version
                or durable.plan_digest != receipt.plan_digest
                or durable.binding_digest != receipt.binding_digest
                or durable.status is not receipt.status
                or durable.completed_steps != tuple(item.step for item in receipt.steps if item.completed)
                or tuple(
                    (
                        item.step,
                        item.attempts,
                        item.last_failure_code,
                    )
                    for item in durable.attempts
                )
                != tuple(
                    (item.step, item.attempts, item.failure_code)
                    for item in receipt.steps
                    if item.attempts > 0 or item.failure_code is not None
                )
                or durable.failure_code != receipt.failure_code
                or (
                    durable.artifact.artifact_id
                    if durable.status is AutonomyPlanStatus.COMPLETED and durable.artifact is not None
                    else None
                )
                != receipt.artifact_id
            ):
                raise AutonomyCheckpointError("terminal receipt does not match its checkpoint")
            if durable.terminal_emitted:
                return
            connection.execute(
                """
                UPDATE autonomy_checkpoint_journal
                SET terminal_emitted = 1, revision = revision + 1,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE task_id = ? AND terminal_emitted = 0
                """,
                (receipt.task_id,),
            )


def _project_checkpoint(
    task_id: str,
    checkpoint: AutonomyCheckpoint,
    store_binding_digest: str,
) -> dict[str, object]:
    artifact = checkpoint.artifact.artifact if checkpoint.artifact is not None else None
    execution_attempts = {item.step: item for item in checkpoint.execution_attempts}
    attempts_json = json.dumps(
        [
            {
                "attempts": item.attempts,
                "execution_attempts": execution_attempts.get(
                    item.step,
                    StepAttempt(item.step, 0),
                ).attempts,
                "failure_code": item.last_failure_code,
                "step": item.step.value,
            }
            for item in checkpoint.attempts
        ],
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(attempts_json) > 512:
        raise AutonomyCheckpointError("checkpoint attempts exceed the durable limit")
    return {
        "task_id": task_id,
        "template_id": checkpoint.template_id,
        "template_version": checkpoint.template_version,
        "plan_digest": checkpoint.plan_digest,
        "binding_digest": checkpoint.binding_digest,
        "store_binding_digest": store_binding_digest,
        "status": checkpoint.status.value,
        "completed_prefix": len(checkpoint.completed_steps),
        "attempts_json": attempts_json,
        "artifact_id": None if artifact is None else artifact.artifact_id,
        "artifact_kind": None if artifact is None else artifact.kind.value,
        "artifact_media_type": None if artifact is None else artifact.media_type,
        "artifact_size_bytes": None if artifact is None else artifact.size_bytes,
        "artifact_sha256": None if artifact is None else artifact.sha256,
        "failure_code": checkpoint.failure_code,
        "terminal_emitted": int(checkpoint.terminal_emitted),
    }


def _decode_row(row: sqlite3.Row, *, expected_store_digest: str) -> DurableAutonomyCheckpoint:
    if str(row["store_binding_digest"]) != expected_store_digest:
        raise AutonomyCheckpointError("durable artifact store binding changed")
    try:
        raw_attempts = json.loads(str(row["attempts_json"]))
        if not isinstance(raw_attempts, list):
            raise TypeError
        attempts = tuple(
            StepAttempt(
                step=AutonomyStep(item["step"]),
                attempts=item["attempts"],
                last_failure_code=item["failure_code"],
            )
            for item in raw_attempts
            if isinstance(item, dict) and set(item) == {"attempts", "execution_attempts", "failure_code", "step"}
        )
        execution_attempts = tuple(
            StepAttempt(
                step=AutonomyStep(item["step"]),
                attempts=item["execution_attempts"],
                last_failure_code=item["failure_code"],
            )
            for item in raw_attempts
            if isinstance(item, dict)
            and set(item) == {"attempts", "execution_attempts", "failure_code", "step"}
            and item["execution_attempts"] > 0
        )
        if len(attempts) != len(raw_attempts):
            raise TypeError
        prefix = int(row["completed_prefix"])
        artifact = _decode_artifact(row)
        return DurableAutonomyCheckpoint(
            template_id=str(row["template_id"]),
            template_version=int(row["template_version"]),
            plan_digest=str(row["plan_digest"]),
            binding_digest=str(row["binding_digest"]),
            status=AutonomyPlanStatus(str(row["status"])),
            completed_steps=SEARXNG_OFFICIAL_RESEARCH_V1.steps[:prefix],
            attempts=attempts,
            execution_attempts=execution_attempts,
            artifact=artifact,
            failure_code=None if row["failure_code"] is None else str(row["failure_code"]),
            terminal_emitted=bool(row["terminal_emitted"]),
        )
    except (KeyError, TypeError, ValueError):
        raise AutonomyCheckpointError("durable autonomy checkpoint is invalid") from None


def _decode_artifact(row: sqlite3.Row) -> ArtifactRef | None:
    if row["artifact_id"] is None:
        return None
    return ArtifactRef(
        artifact_id=str(row["artifact_id"]),
        kind=ArtifactKind(str(row["artifact_kind"])),
        media_type=str(row["artifact_media_type"]),
        size_bytes=int(row["artifact_size_bytes"]),
        sha256=str(row["artifact_sha256"]),
    )


def _require_same_identity(row: sqlite3.Row, value: dict[str, object]) -> None:
    for name in (
        "template_id",
        "template_version",
        "plan_digest",
        "binding_digest",
        "store_binding_digest",
    ):
        if row[name] != value[name]:
            raise AutonomyCheckpointError("durable checkpoint identity changed")


def _require_valid_transition(row: sqlite3.Row, value: dict[str, object]) -> None:
    old_status = AutonomyPlanStatus(str(row["status"]))
    new_status = AutonomyPlanStatus(str(value["status"]))
    if old_status is not AutonomyPlanStatus.RUNNING and new_status is not old_status:
        raise AutonomyCheckpointError("terminal checkpoint is immutable")
    if old_status is not AutonomyPlanStatus.RUNNING:
        for name in (
            "completed_prefix",
            "attempts_json",
            "artifact_id",
            "artifact_kind",
            "artifact_media_type",
            "artifact_size_bytes",
            "artifact_sha256",
            "failure_code",
        ):
            if row[name] != value[name]:
                raise AutonomyCheckpointError("terminal checkpoint projection is immutable")
    if bool(row["terminal_emitted"]) and not bool(value["terminal_emitted"]):
        raise AutonomyCheckpointError("terminal acknowledgement cannot be cleared")
    old_attempts = _attempt_map(str(row["attempts_json"]))
    new_attempts = _attempt_map(str(value["attempts_json"]))
    for step, count in old_attempts.items():
        if new_attempts.get(step, -1) < count:
            raise AutonomyCheckpointError("durable attempt budget cannot be reset")


def _require_owned_lease(row: sqlite3.Row | None, token: str) -> None:
    if (
        row is None
        or row["lease_owner"] != token
        or row["lease_expires_at"] is None
        or int(row["lease_expires_at"]) <= int(time.time())
    ):
        raise AutonomyCheckpointError("autonomy execution lease is no longer current")


def _attempt_map(value: str) -> dict[str, int]:
    try:
        decoded = json.loads(value)
        return {str(item["step"]): int(item["attempts"]) for item in decoded}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise AutonomyCheckpointError("durable attempts are invalid") from None


def _row_equals_projection(row: sqlite3.Row, value: dict[str, object]) -> bool:
    return all(
        row[name] == value[name]
        for name in (
            "status",
            "completed_prefix",
            "attempts_json",
            "artifact_id",
            "artifact_kind",
            "artifact_media_type",
            "artifact_size_bytes",
            "artifact_sha256",
            "failure_code",
            "terminal_emitted",
        )
    )


def _terminal_key(task_id: str, binding_digest: str, plan_digest: str) -> str:
    payload = json.dumps(
        {
            "binding_digest": binding_digest,
            "plan_digest": plan_digest,
            "task_id": task_id,
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _validate_task_id(task_id: str) -> None:
    if not isinstance(task_id, str) or _IDENTIFIER.fullmatch(task_id) is None:
        raise ValueError("task_id is invalid")


__all__ = ["SqliteAutonomyJournal"]
