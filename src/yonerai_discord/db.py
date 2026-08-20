from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


MAX_SQLITE_ID = 9_223_372_036_854_775_807
GLOBAL_GUILD_ID = 0
_AGENT_AUDIT_STORE_BINDING_VERSION = b"yonerai.agent-audit-store.v1"


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    sql: str


@dataclass(frozen=True, slots=True)
class OverrideRecord:
    kind: Literal["module", "capability", "permission"]
    subject_id: str
    guild_id: int
    enabled: bool | None
    required_level: int | None
    updated_by: int
    reason: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class CapabilityActorGrantRecord:
    capability_id: str
    guild_id: int
    subject_user_id: int
    grant_kind: Literal["owner_delegated"]
    granted_by: int
    reason: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class AuditRecord:
    id: int
    event: str
    plugin: str | None
    guild_id: int | None
    actor_id: int | None
    details: Mapping[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True, repr=False)
class AgentAuditProjectionRecord:
    """Scope-filtered audit columns that cannot carry details metadata."""

    id: int
    event: str
    plugin: str | None
    guild_id: int
    actor_id: int
    created_at: str

    def __repr__(self) -> str:
        return "AgentAuditProjectionRecord()"


@dataclass(frozen=True, slots=True)
class GuildAuditSummaryRecord:
    """Admin UIへ本文・detailsを渡さないguild限定監査要約。"""

    id: int
    event: str
    plugin: str | None
    actor_id: int | None
    created_at: str


@dataclass(frozen=True, slots=True)
class EvolutionProposalRecord:
    proposal_id: str
    proposal_hash: str
    status: str
    proposer_id: int
    parent_proposal_id: str | None
    updated_by: int
    reason: str
    created_at: str
    updated_at: str


MIGRATIONS = (
    Migration(
        1,
        """
        CREATE TABLE IF NOT EXISTS plugin_state (
            name TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL,
            plugin TEXT,
            guild_id INTEGER,
            actor_id INTEGER,
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """,
    ),
    Migration(
        2,
        """
        CREATE TABLE IF NOT EXISTS module_override (
            guild_id INTEGER NOT NULL DEFAULT 0
                CHECK(guild_id BETWEEN 0 AND 9223372036854775807),
            module_id TEXT NOT NULL
                CHECK(length(module_id) BETWEEN 1 AND 128)
                CHECK(module_id = lower(trim(module_id)))
                CHECK(module_id NOT GLOB '*[^a-z0-9._-]*'),
            enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
            updated_by INTEGER NOT NULL
                CHECK(updated_by BETWEEN 0 AND 9223372036854775807),
            reason TEXT NOT NULL DEFAULT '' CHECK(length(reason) <= 1000),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            PRIMARY KEY (guild_id, module_id)
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS capability_override (
            guild_id INTEGER NOT NULL DEFAULT 0
                CHECK(guild_id BETWEEN 0 AND 9223372036854775807),
            capability_id TEXT NOT NULL
                CHECK(length(capability_id) BETWEEN 1 AND 128)
                CHECK(capability_id = lower(trim(capability_id)))
                CHECK(capability_id NOT GLOB '*[^a-z0-9._-]*'),
            enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
            updated_by INTEGER NOT NULL
                CHECK(updated_by BETWEEN 0 AND 9223372036854775807),
            reason TEXT NOT NULL DEFAULT '' CHECK(length(reason) <= 1000),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            PRIMARY KEY (guild_id, capability_id)
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS permission_override (
            guild_id INTEGER NOT NULL DEFAULT 0
                CHECK(guild_id BETWEEN 0 AND 9223372036854775807),
            capability_id TEXT NOT NULL
                CHECK(length(capability_id) BETWEEN 1 AND 128)
                CHECK(capability_id = lower(trim(capability_id)))
                CHECK(capability_id NOT GLOB '*[^a-z0-9._-]*'),
            required_level INTEGER NOT NULL CHECK(required_level IN (0, 10, 20, 30, 40, 50)),
            updated_by INTEGER NOT NULL
                CHECK(updated_by BETWEEN 0 AND 9223372036854775807),
            reason TEXT NOT NULL DEFAULT '' CHECK(length(reason) <= 1000),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            PRIMARY KEY (guild_id, capability_id)
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS evolution_proposal (
            proposal_id TEXT PRIMARY KEY
                CHECK(length(proposal_id) BETWEEN 1 AND 128)
                CHECK(proposal_id = lower(trim(proposal_id)))
                CHECK(proposal_id NOT GLOB '*[^a-z0-9._-]*'),
            proposal_hash TEXT NOT NULL UNIQUE
                CHECK(length(proposal_hash) = 64)
                CHECK(proposal_hash = lower(proposal_hash))
                CHECK(proposal_hash NOT GLOB '*[^0-9a-f]*'),
            status TEXT NOT NULL
                CHECK(status IN ('proposed', 'in_review', 'approved', 'rejected')),
            proposer_id INTEGER NOT NULL
                CHECK(proposer_id BETWEEN 1 AND 9223372036854775807),
            parent_proposal_id TEXT REFERENCES evolution_proposal(proposal_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            updated_by INTEGER NOT NULL
                CHECK(updated_by BETWEEN 1 AND 9223372036854775807),
            reason TEXT NOT NULL DEFAULT '' CHECK(length(reason) <= 1000),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            CHECK(parent_proposal_id IS NULL OR parent_proposal_id <> proposal_id)
        );

        CREATE INDEX IF NOT EXISTS audit_log_event_created_idx
            ON audit_log(event, created_at, id);
        CREATE INDEX IF NOT EXISTS audit_log_guild_created_idx
            ON audit_log(guild_id, created_at, id);

        CREATE TRIGGER IF NOT EXISTS audit_log_no_update
        BEFORE UPDATE ON audit_log
        BEGIN
            SELECT RAISE(ABORT, 'audit_log is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
        BEFORE DELETE ON audit_log
        BEGIN
            SELECT RAISE(ABORT, 'audit_log is append-only');
        END;
        """,
    ),
    Migration(
        3,
        """
        CREATE TABLE IF NOT EXISTS capability_actor_grant (
            guild_id INTEGER NOT NULL
                CHECK(guild_id BETWEEN 1 AND 9223372036854775807),
            capability_id TEXT NOT NULL
                CHECK(length(capability_id) BETWEEN 1 AND 128)
                CHECK(capability_id = lower(trim(capability_id)))
                CHECK(capability_id NOT GLOB '*[^a-z0-9._-]*'),
            subject_user_id INTEGER NOT NULL
                CHECK(subject_user_id BETWEEN 1 AND 9223372036854775807),
            grant_kind TEXT NOT NULL
                CHECK(grant_kind = 'owner_delegated'),
            granted_by INTEGER NOT NULL
                CHECK(granted_by BETWEEN 1 AND 9223372036854775807),
            reason TEXT NOT NULL DEFAULT '' CHECK(length(reason) <= 1000),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            PRIMARY KEY (guild_id, capability_id, subject_user_id)
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS capability_actor_grant_subject_idx
            ON capability_actor_grant(subject_user_id, guild_id, capability_id);
        """,
    ),
    Migration(
        4,
        """
        CREATE TABLE IF NOT EXISTS media_url_inspection_daily_usage (
            usage_date TEXT PRIMARY KEY
                CHECK(length(usage_date) = 10)
                CHECK(usage_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
            calls INTEGER NOT NULL CHECK(calls BETWEEN 0 AND 1000000),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        ) WITHOUT ROWID;
        """,
    ),
    Migration(
        5,
        """
        CREATE TABLE IF NOT EXISTS autonomy_checkpoint_journal (
            task_id TEXT PRIMARY KEY
                CHECK(length(task_id) BETWEEN 1 AND 128)
                CHECK(task_id NOT GLOB '*[^A-Za-z0-9._:-]*'),
            template_id TEXT NOT NULL
                CHECK(length(template_id) BETWEEN 1 AND 128)
                CHECK(template_id NOT GLOB '*[^a-z0-9._:-]*'),
            template_version INTEGER NOT NULL CHECK(template_version BETWEEN 1 AND 1000000),
            plan_digest TEXT NOT NULL
                CHECK(length(plan_digest) = 64)
                CHECK(plan_digest NOT GLOB '*[^0-9a-f]*'),
            binding_digest TEXT NOT NULL
                CHECK(length(binding_digest) = 64)
                CHECK(binding_digest NOT GLOB '*[^0-9a-f]*'),
            store_binding_digest TEXT NOT NULL
                CHECK(length(store_binding_digest) = 64)
                CHECK(store_binding_digest NOT GLOB '*[^0-9a-f]*'),
            status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed')),
            completed_prefix INTEGER NOT NULL CHECK(completed_prefix BETWEEN 0 AND 3),
            attempts_json TEXT NOT NULL CHECK(length(attempts_json) BETWEEN 2 AND 512),
            artifact_id TEXT,
            artifact_kind TEXT,
            artifact_media_type TEXT,
            artifact_size_bytes INTEGER,
            artifact_sha256 TEXT,
            failure_code TEXT,
            terminal_emitted INTEGER NOT NULL CHECK(terminal_emitted IN (0, 1)),
            lease_owner TEXT
                CHECK(lease_owner IS NULL OR (
                    length(lease_owner) = 64
                    AND lease_owner NOT GLOB '*[^0-9a-f]*'
                )),
            lease_expires_at INTEGER
                CHECK(lease_expires_at IS NULL OR lease_expires_at > 0),
            revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 2147483647),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            CHECK(
                (artifact_id IS NULL AND artifact_kind IS NULL
                 AND artifact_media_type IS NULL AND artifact_size_bytes IS NULL
                 AND artifact_sha256 IS NULL)
                OR
                (artifact_id IS NOT NULL AND artifact_kind IS NOT NULL
                 AND artifact_media_type IS NOT NULL AND artifact_size_bytes > 0
                 AND length(artifact_sha256) = 64
                 AND artifact_sha256 NOT GLOB '*[^0-9a-f]*')
            ),
            CHECK(
                (status = 'failed' AND failure_code IS NOT NULL)
                OR (status <> 'failed' AND failure_code IS NULL)
            ),
            CHECK(status <> 'running' OR terminal_emitted = 0),
            CHECK(status <> 'completed' OR completed_prefix = 3),
            CHECK(
                (lease_owner IS NULL AND lease_expires_at IS NULL)
                OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
            )
        ) WITHOUT ROWID;
        """,
    ),
)


_OVERRIDE_TABLES = {
    "module": ("module_override", "module_id"),
    "capability": ("capability_override", "capability_id"),
    "permission": ("permission_override", "capability_id"),
}
_PROPOSAL_TRANSITIONS = {
    "proposed": frozenset({"in_review"}),
    "in_review": frozenset({"approved", "rejected"}),
    "approved": frozenset(),
    "rejected": frozenset(),
}
_FORBIDDEN_DETAIL_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "body",
        "content",
        "cookie",
        "message",
        "message_content",
        "password",
        "patch",
        "prompt",
        "raw_prompt",
        "secret",
        "token",
        "transcript",
    }
)
_FORBIDDEN_DETAIL_SUFFIXES = (
    "_api_key",
    "_body",
    "_content",
    "_cookie",
    "_password",
    "_patch",
    "_prompt",
    "_secret",
    "_token",
    "_transcript",
)


class Database:
    """SQLite persistence for control metadata.

    This API deliberately stores state, IDs, reasons and bounded audit metadata;
    it does not accept message bodies, prompts, patches, credentials or secrets.
    """

    def __init__(self, path: Path) -> None:
        canonical_path = Path(path).expanduser().resolve(strict=False)
        self.path = canonical_path
        self._connection: sqlite3.Connection | None = None
        self._connection_generation = 0
        self._agent_audit_store_binding_digest = _agent_audit_store_binding_digest(canonical_path)
        self._lock = threading.RLock()

    @property
    def is_open(self) -> bool:
        return self._connection is not None

    @property
    def connection_generation(self) -> int:
        """Return the successful closed-to-open transition generation."""

        with self._lock:
            return self._connection_generation

    @property
    def agent_audit_store_binding_digest(self) -> str:
        """Return the content-free durable identity for agent-audit cursors."""

        return self._agent_audit_store_binding_digest

    def open(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA busy_timeout = 5000")
                connection.execute("PRAGMA journal_mode = WAL")
            except BaseException:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
                raise
            self._connection = connection
            self._connection_generation += 1

    def migrate(self, migrations: Iterable[Migration] = MIGRATIONS) -> int:
        with self._lock:
            connection = self._require_connection()
            with connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "version INTEGER PRIMARY KEY CHECK(version > 0), "
                    "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
            applied = {int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")}
            latest = max(applied, default=0)
            for migration in sorted(migrations, key=lambda item: item.version):
                if (
                    not isinstance(migration.version, int)
                    or isinstance(migration.version, bool)
                    or migration.version <= 0
                ):
                    raise ValueError("migration version must be a positive integer")
                if migration.version in applied:
                    continue
                # executescript otherwise commits before running its SQL. Wrapping the
                # entire script explicitly makes schema changes and the version row atomic.
                script = (
                    "BEGIN IMMEDIATE;\n"
                    f"{migration.sql}\n"
                    f"INSERT INTO schema_migrations(version) VALUES ({migration.version});\n"
                    "COMMIT;"
                )
                try:
                    connection.executescript(script)
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
                applied.add(migration.version)
                latest = max(latest, migration.version)
            return latest

    def ping(self) -> bool:
        try:
            with self._lock:
                self._require_connection().execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return False
        return True

    def quick_check(self) -> tuple[str, ...]:
        with self._lock:
            rows = self._require_connection().execute("PRAGMA quick_check").fetchall()
        return tuple(str(row[0]) for row in rows)

    def online_backup(self, destination: Path) -> Path:
        target = Path(destination)
        source_path = self.path.resolve()
        target_path = target.resolve(strict=False)
        if source_path == target_path:
            raise ValueError("backup destination must differ from the live database")
        if target.exists() or target.is_symlink():
            raise FileExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.parent.is_symlink():
            raise ValueError("backup directory must not be a symlink")

        backup_connection: sqlite3.Connection | None = None
        try:
            backup_connection = sqlite3.connect(target_path)
            with self._lock:
                self._require_connection().backup(backup_connection)
            rows = backup_connection.execute("PRAGMA quick_check").fetchall()
            if tuple(str(row[0]) for row in rows) != ("ok",):
                raise RuntimeError("backup quick_check failed")
        except Exception:
            if backup_connection is not None:
                backup_connection.close()
                backup_connection = None
            target_path.unlink(missing_ok=True)
            raise
        finally:
            if backup_connection is not None:
                backup_connection.close()
        return target_path

    def get_module_override(self, module_id: str, guild_id: int | str | None = None) -> bool | None:
        return self._get_bool_override("module", module_id, guild_id)

    def set_module_override(
        self,
        module_id: str,
        enabled: bool | None,
        guild_id: int | str | None = None,
        *,
        updated_by: int = 0,
        reason: str | None = None,
    ) -> None:
        self._set_bool_override("module", module_id, enabled, guild_id, updated_by, reason)

    def resolve_module_override(self, module_id: str, guild_id: int | str | None = None) -> bool | None:
        return self._resolve_bool_override("module", module_id, guild_id)

    def get_capability_override(self, capability_id: str, guild_id: int | str | None = None) -> bool | None:
        return self._get_bool_override("capability", capability_id, guild_id)

    def set_capability_override(
        self,
        capability_id: str,
        enabled: bool | None,
        guild_id: int | str | None = None,
        *,
        updated_by: int = 0,
        reason: str | None = None,
    ) -> None:
        self._set_bool_override("capability", capability_id, enabled, guild_id, updated_by, reason)

    def resolve_capability_override(self, capability_id: str, guild_id: int | str | None = None) -> bool | None:
        return self._resolve_bool_override("capability", capability_id, guild_id)

    def get_level_override(self, capability_id: str, guild_id: int | str | None = None) -> int | None:
        subject_id = _normalize_subject_id(capability_id, "capability_id")
        scope = _normalize_guild_id(guild_id)
        with self._lock:
            row = (
                self._require_connection()
                .execute(
                    "SELECT required_level FROM permission_override WHERE guild_id = ? AND capability_id = ?",
                    (scope, subject_id),
                )
                .fetchone()
            )
        return None if row is None else int(row[0])

    def set_level_override(
        self,
        capability_id: str,
        level: int | str | None,
        guild_id: int | str | None = None,
        *,
        updated_by: int = 0,
        reason: str | None = None,
    ) -> None:
        subject_id = _normalize_subject_id(capability_id, "capability_id")
        scope = _normalize_guild_id(guild_id)
        actor_id = _normalize_actor_id(updated_by, allow_system=True)
        normalized_reason = _normalize_reason(reason)
        normalized_level = None if level is None else _normalize_level(level)
        with self._lock:
            connection = self._require_connection()
            with connection:
                if normalized_level is None:
                    connection.execute(
                        "DELETE FROM permission_override WHERE guild_id = ? AND capability_id = ?",
                        (scope, subject_id),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO permission_override(
                            guild_id, capability_id, required_level, updated_by, reason
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(guild_id, capability_id) DO UPDATE SET
                            required_level = excluded.required_level,
                            updated_by = excluded.updated_by,
                            reason = excluded.reason,
                            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        """,
                        (scope, subject_id, normalized_level, actor_id, normalized_reason),
                    )
                self._insert_audit(
                    connection,
                    event="permission_override.cleared" if normalized_level is None else "permission_override.set",
                    plugin=None,
                    guild_id=scope,
                    actor_id=actor_id,
                    details={"capability_id": subject_id, "required_level": normalized_level},
                )

    def resolve_level_override(self, capability_id: str, guild_id: int | str | None = None) -> int | None:
        if guild_id is not None:
            local = self.get_level_override(capability_id, guild_id)
            if local is not None:
                return local
        return self.get_level_override(capability_id, None)

    def set_owner_managed_capability_enabled(
        self,
        capability_id: str,
        enabled: bool,
        guild_id: int | str,
        *,
        updated_by: int,
        reason: str | None = None,
    ) -> None:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be bool")
        normalized_id = _normalize_subject_id(capability_id, "capability_id")
        scope = _normalize_required_guild_id(guild_id)
        actor_id = _normalize_actor_id(updated_by, allow_system=False)
        normalized_reason = _normalize_reason(reason)
        with self._lock:
            connection = self._require_connection()
            with connection:
                connection.execute(
                    """
                    INSERT INTO permission_override(
                        guild_id, capability_id, required_level, updated_by, reason
                    ) VALUES (?, ?, 50, ?, ?)
                    ON CONFLICT(guild_id, capability_id) DO UPDATE SET
                        required_level = 50,
                        updated_by = excluded.updated_by,
                        reason = excluded.reason,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    """,
                    (scope, normalized_id, actor_id, normalized_reason),
                )
                connection.execute(
                    """
                    INSERT INTO capability_override(
                        guild_id, capability_id, enabled, updated_by, reason
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, capability_id) DO UPDATE SET
                        enabled = excluded.enabled,
                        updated_by = excluded.updated_by,
                        reason = excluded.reason,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    """,
                    (scope, normalized_id, int(enabled), actor_id, normalized_reason),
                )
                self._insert_audit(
                    connection,
                    event=("owner_managed_capability.activated" if enabled else "owner_managed_capability.deactivated"),
                    plugin=None,
                    guild_id=scope,
                    actor_id=actor_id,
                    details={
                        "capability_id": normalized_id,
                        "enabled": enabled,
                        "required_level": 50,
                    },
                )

    def get_capability_actor_grant(
        self,
        capability_id: str,
        subject_user_id: int,
        guild_id: int | str,
    ) -> CapabilityActorGrantRecord | None:
        normalized_id = _normalize_subject_id(capability_id, "capability_id")
        scope = _normalize_required_guild_id(guild_id)
        subject_id = _normalize_actor_id(subject_user_id, allow_system=False)
        with self._lock:
            row = (
                self._require_connection()
                .execute(
                    """
                    SELECT capability_id, guild_id, subject_user_id, grant_kind,
                           granted_by, reason, created_at, updated_at
                    FROM capability_actor_grant
                    WHERE guild_id = ? AND capability_id = ? AND subject_user_id = ?
                    """,
                    (scope, normalized_id, subject_id),
                )
                .fetchone()
            )
        return None if row is None else self._capability_actor_grant_record(row)

    def set_capability_actor_grant(
        self,
        capability_id: str,
        subject_user_id: int,
        enabled: bool,
        guild_id: int | str,
        *,
        granted_by: int,
        reason: str | None = None,
    ) -> None:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be bool")
        normalized_id = _normalize_subject_id(capability_id, "capability_id")
        scope = _normalize_required_guild_id(guild_id)
        subject_id = _normalize_actor_id(subject_user_id, allow_system=False)
        grantor_id = _normalize_actor_id(granted_by, allow_system=False)
        if subject_id == grantor_id:
            raise ValueError("owner self access must not use a delegated actor grant")
        normalized_reason = _normalize_reason(reason)
        with self._lock:
            connection = self._require_connection()
            with connection:
                if enabled:
                    connection.execute(
                        """
                        INSERT INTO capability_actor_grant(
                            guild_id, capability_id, subject_user_id, grant_kind,
                            granted_by, reason
                        ) VALUES (?, ?, ?, 'owner_delegated', ?, ?)
                        ON CONFLICT(guild_id, capability_id, subject_user_id) DO UPDATE SET
                            grant_kind = excluded.grant_kind,
                            granted_by = excluded.granted_by,
                            reason = excluded.reason,
                            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        """,
                        (scope, normalized_id, subject_id, grantor_id, normalized_reason),
                    )
                else:
                    connection.execute(
                        """
                        DELETE FROM capability_actor_grant
                        WHERE guild_id = ? AND capability_id = ? AND subject_user_id = ?
                        """,
                        (scope, normalized_id, subject_id),
                    )
                self._insert_audit(
                    connection,
                    event="capability_actor_grant.set" if enabled else "capability_actor_grant.cleared",
                    plugin=None,
                    guild_id=scope,
                    actor_id=grantor_id,
                    details={
                        "capability_id": normalized_id,
                        "enabled": enabled,
                        "grant_kind": "owner_delegated",
                        "subject_user_id": subject_id,
                    },
                )

    def list_capability_actor_grants(
        self,
        guild_id: int | str,
        *,
        capability_id: str | None = None,
    ) -> tuple[CapabilityActorGrantRecord, ...]:
        scope = _normalize_required_guild_id(guild_id)
        normalized_id = None if capability_id is None else _normalize_subject_id(capability_id, "capability_id")
        with self._lock:
            connection = self._require_connection()
            if normalized_id is None:
                rows = connection.execute(
                    """
                    SELECT capability_id, guild_id, subject_user_id, grant_kind,
                           granted_by, reason, created_at, updated_at
                    FROM capability_actor_grant
                    WHERE guild_id = ?
                    ORDER BY capability_id, subject_user_id
                    """,
                    (scope,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT capability_id, guild_id, subject_user_id, grant_kind,
                           granted_by, reason, created_at, updated_at
                    FROM capability_actor_grant
                    WHERE guild_id = ? AND capability_id = ?
                    ORDER BY subject_user_id
                    """,
                    (scope, normalized_id),
                ).fetchall()
        return tuple(self._capability_actor_grant_record(row) for row in rows)

    def list_effective_overrides(self, guild_id: int | str | None = None) -> tuple[OverrideRecord, ...]:
        scope = _normalize_guild_id(guild_id)
        records: list[OverrideRecord] = []
        with self._lock:
            connection = self._require_connection()
            for kind in ("module", "capability", "permission"):
                table, subject_column = _OVERRIDE_TABLES[kind]
                value_column = "required_level" if kind == "permission" else "enabled"
                if scope == GLOBAL_GUILD_ID:
                    rows = connection.execute(
                        f"SELECT guild_id, {subject_column}, {value_column}, updated_by, reason, created_at, updated_at "
                        f"FROM {table} WHERE guild_id = ? ORDER BY {subject_column}",
                        (GLOBAL_GUILD_ID,),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        f"SELECT guild_id, {subject_column}, {value_column}, updated_by, reason, created_at, updated_at "
                        f"FROM {table} WHERE guild_id IN (?, ?) "
                        f"ORDER BY {subject_column}, guild_id",
                        (GLOBAL_GUILD_ID, scope),
                    ).fetchall()
                effective = {str(row[1]): row for row in rows}
                records.extend(self._override_record(kind, row) for row in effective.values())
        return tuple(sorted(records, key=lambda item: (item.kind, item.subject_id)))

    def list_enabled_overrides(self, guild_id: int | str | None = None) -> tuple[OverrideRecord, ...]:
        return tuple(record for record in self.list_effective_overrides(guild_id) if record.enabled is True)

    def reserve_media_url_inspection_call(self, daily_limit: int) -> bool:
        """UTC日次上限を越えない1 callを、外部POST前に原子的に予約する。"""

        if isinstance(daily_limit, bool) or not isinstance(daily_limit, int) or not 0 <= daily_limit <= 1_000:
            raise ValueError("daily_limit must be between 0 and 1000")
        if daily_limit == 0:
            return False
        with self._lock:
            connection = self._require_connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO media_url_inspection_daily_usage(usage_date, calls)
                    VALUES (strftime('%Y-%m-%d', 'now'), 0)
                    """
                )
                cursor = connection.execute(
                    """
                    UPDATE media_url_inspection_daily_usage
                    SET calls = calls + 1,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE usage_date = strftime('%Y-%m-%d', 'now')
                      AND calls < ?
                    """,
                    (daily_limit,),
                )
                reserved = cursor.rowcount == 1
                connection.commit()
                return reserved
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

    def append_audit(
        self,
        event: str,
        *,
        actor_id: int,
        details: Mapping[str, Any] | None = None,
        plugin: str | None = None,
        guild_id: int | str | None = None,
    ) -> int:
        normalized_event = _normalize_event(event)
        normalized_actor = _normalize_actor_id(actor_id, allow_system=True)
        normalized_plugin = None if plugin is None else _normalize_subject_id(plugin, "plugin")
        normalized_guild = None if guild_id is None else _normalize_guild_id(guild_id)
        safe_details = {} if details is None else _safe_audit_details(details)
        with self._lock:
            connection = self._require_connection()
            with connection:
                return self._insert_audit(
                    connection,
                    event=normalized_event,
                    plugin=normalized_plugin,
                    guild_id=normalized_guild,
                    actor_id=normalized_actor,
                    details=safe_details,
                )

    def list_audit(self, *, limit: int = 100, after_id: int = 0) -> tuple[AuditRecord, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if isinstance(after_id, bool) or not isinstance(after_id, int) or after_id < 0:
            raise ValueError("after_id must be a non-negative integer")
        with self._lock:
            rows = (
                self._require_connection()
                .execute(
                    "SELECT id, event, plugin, guild_id, actor_id, details_json, created_at "
                    "FROM audit_log WHERE id > ? ORDER BY id LIMIT ?",
                    (after_id, limit),
                )
                .fetchall()
            )
        return tuple(
            AuditRecord(
                id=int(row["id"]),
                event=str(row["event"]),
                plugin=row["plugin"],
                guild_id=row["guild_id"],
                actor_id=row["actor_id"],
                details=json.loads(row["details_json"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        )

    def list_agent_audit_projection(
        self,
        *,
        guild_id: int,
        actor_id: int,
        limit: int = 100,
        after_id: int = 0,
    ) -> tuple[AgentAuditProjectionRecord, ...]:
        """Return only redacted rows for one exact guild/actor scope."""

        normalized_guild = _normalize_guild_id(guild_id)
        if normalized_guild == GLOBAL_GUILD_ID:
            raise ValueError("agent audit projection requires a Discord guild")
        normalized_actor = _normalize_actor_id(actor_id, allow_system=False)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if isinstance(after_id, bool) or not isinstance(after_id, int) or not 0 <= after_id <= MAX_SQLITE_ID:
            raise ValueError("after_id must be a non-negative 64-bit integer")
        with self._lock:
            rows = (
                self._require_connection()
                .execute(
                    "SELECT id, event, plugin, guild_id, actor_id, created_at "
                    "FROM audit_log WHERE guild_id = ? AND actor_id = ? AND id > ? ORDER BY id LIMIT ?",
                    (normalized_guild, normalized_actor, after_id, limit),
                )
                .fetchall()
            )
        return tuple(
            AgentAuditProjectionRecord(
                id=int(row["id"]),
                event=str(row["event"]),
                plugin=row["plugin"],
                guild_id=int(row["guild_id"]),
                actor_id=int(row["actor_id"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        )

    def list_recent_audit(self, *, limit: int = 100) -> tuple[AuditRecord, ...]:
        """管理画面向けに、監査イベントを新しい順で取得する。"""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._lock:
            rows = (
                self._require_connection()
                .execute(
                    "SELECT id, event, plugin, guild_id, actor_id, details_json, created_at "
                    "FROM audit_log ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
                .fetchall()
            )
        return tuple(
            AuditRecord(
                id=int(row["id"]),
                event=str(row["event"]),
                plugin=row["plugin"],
                guild_id=row["guild_id"],
                actor_id=row["actor_id"],
                details=json.loads(row["details_json"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        )

    def list_guild_audit_summary(
        self,
        guild_id: int | str,
        *,
        limit: int = 50,
    ) -> tuple[GuildAuditSummaryRecord, ...]:
        """選択guildだけのredacted監査列を新しい順で返す。"""

        scope = _normalize_guild_id(guild_id)
        if scope == GLOBAL_GUILD_ID:
            raise ValueError("guild_id must identify a Discord guild")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        with self._lock:
            rows = (
                self._require_connection()
                .execute(
                    "SELECT id, event, plugin, actor_id, created_at "
                    "FROM audit_log WHERE guild_id = ? ORDER BY id DESC LIMIT ?",
                    (scope, limit),
                )
                .fetchall()
            )
        return tuple(
            GuildAuditSummaryRecord(
                id=int(row["id"]),
                event=str(row["event"]),
                plugin=row["plugin"],
                actor_id=row["actor_id"],
                created_at=str(row["created_at"]),
            )
            for row in rows
        )

    def create_evolution_proposal(
        self,
        proposal_id: str,
        proposal_hash: str,
        *,
        proposer_id: int,
        reason: str | None = None,
        parent_proposal_id: str | None = None,
    ) -> EvolutionProposalRecord:
        normalized_id = _normalize_subject_id(proposal_id, "proposal_id")
        normalized_hash = _normalize_hash(proposal_hash)
        actor_id = _normalize_actor_id(proposer_id, allow_system=False)
        normalized_reason = _normalize_reason(reason)
        normalized_parent = (
            None if parent_proposal_id is None else _normalize_subject_id(parent_proposal_id, "parent_proposal_id")
        )
        if normalized_parent == normalized_id:
            raise ValueError("a proposal cannot be its own parent")
        with self._lock:
            connection = self._require_connection()
            with connection:
                connection.execute(
                    """
                    INSERT INTO evolution_proposal(
                        proposal_id, proposal_hash, status, proposer_id,
                        parent_proposal_id, updated_by, reason
                    ) VALUES (?, ?, 'proposed', ?, ?, ?, ?)
                    """,
                    (normalized_id, normalized_hash, actor_id, normalized_parent, actor_id, normalized_reason),
                )
                self._insert_audit(
                    connection,
                    event="evolution_proposal.created",
                    plugin=None,
                    guild_id=None,
                    actor_id=actor_id,
                    details={"proposal_hash": normalized_hash, "proposal_id": normalized_id, "status": "proposed"},
                )
            return self._get_evolution_proposal_locked(connection, normalized_id)

    def update_evolution_proposal_status(
        self,
        proposal_id: str,
        status: str,
        *,
        updated_by: int,
        reason: str | None = None,
    ) -> EvolutionProposalRecord:
        normalized_id = _normalize_subject_id(proposal_id, "proposal_id")
        normalized_status = _normalize_proposal_status(status)
        actor_id = _normalize_actor_id(updated_by, allow_system=False)
        normalized_reason = _normalize_reason(reason)
        with self._lock:
            connection = self._require_connection()
            with connection:
                current = self._get_evolution_proposal_locked(connection, normalized_id)
                if normalized_status not in _PROPOSAL_TRANSITIONS[current.status]:
                    raise ValueError(f"invalid proposal transition: {current.status} -> {normalized_status}")
                connection.execute(
                    """
                    UPDATE evolution_proposal SET
                        status = ?, updated_by = ?, reason = ?,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE proposal_id = ?
                    """,
                    (normalized_status, actor_id, normalized_reason, normalized_id),
                )
                self._insert_audit(
                    connection,
                    event="evolution_proposal.status_changed",
                    plugin=None,
                    guild_id=None,
                    actor_id=actor_id,
                    details={
                        "from_status": current.status,
                        "proposal_id": normalized_id,
                        "to_status": normalized_status,
                    },
                )
            return self._get_evolution_proposal_locked(connection, normalized_id)

    def get_evolution_proposal(self, proposal_id: str) -> EvolutionProposalRecord | None:
        normalized_id = _normalize_subject_id(proposal_id, "proposal_id")
        with self._lock:
            connection = self._require_connection()
            row = connection.execute(
                "SELECT * FROM evolution_proposal WHERE proposal_id = ?", (normalized_id,)
            ).fetchone()
            return None if row is None else self._proposal_record(row)

    def list_evolution_proposals(
        self,
        *,
        status: str | None = None,
        limit: int = 20,
    ) -> tuple[EvolutionProposalRecord, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        normalized_status = None if status is None else _normalize_proposal_status(status)
        with self._lock:
            connection = self._require_connection()
            if normalized_status is None:
                rows = connection.execute(
                    "SELECT * FROM evolution_proposal ORDER BY created_at DESC, proposal_id LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM evolution_proposal WHERE status = ? ORDER BY created_at DESC, proposal_id LIMIT ?",
                    (normalized_status, limit),
                ).fetchall()
        return tuple(self._proposal_record(row) for row in rows)

    def close(self) -> None:
        with self._lock:
            if self._connection is None:
                return
            self._connection.close()
            self._connection = None

    def _get_bool_override(self, kind: str, subject_id: str, guild_id: int | str | None) -> bool | None:
        table, subject_column = _OVERRIDE_TABLES[kind]
        normalized_id = _normalize_subject_id(subject_id, f"{kind}_id")
        scope = _normalize_guild_id(guild_id)
        with self._lock:
            row = (
                self._require_connection()
                .execute(
                    f"SELECT enabled FROM {table} WHERE guild_id = ? AND {subject_column} = ?",
                    (scope, normalized_id),
                )
                .fetchone()
            )
        return None if row is None else bool(row[0])

    def _set_bool_override(
        self,
        kind: str,
        subject_id: str,
        enabled: bool | None,
        guild_id: int | str | None,
        updated_by: int,
        reason: str | None,
    ) -> None:
        if enabled is not None and not isinstance(enabled, bool):
            raise TypeError("enabled must be bool or None")
        table, subject_column = _OVERRIDE_TABLES[kind]
        normalized_id = _normalize_subject_id(subject_id, f"{kind}_id")
        scope = _normalize_guild_id(guild_id)
        actor_id = _normalize_actor_id(updated_by, allow_system=True)
        normalized_reason = _normalize_reason(reason)
        with self._lock:
            connection = self._require_connection()
            with connection:
                if enabled is None:
                    connection.execute(
                        f"DELETE FROM {table} WHERE guild_id = ? AND {subject_column} = ?",
                        (scope, normalized_id),
                    )
                else:
                    connection.execute(
                        f"""
                        INSERT INTO {table}(guild_id, {subject_column}, enabled, updated_by, reason)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(guild_id, {subject_column}) DO UPDATE SET
                            enabled = excluded.enabled,
                            updated_by = excluded.updated_by,
                            reason = excluded.reason,
                            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        """,
                        (scope, normalized_id, int(enabled), actor_id, normalized_reason),
                    )
                self._insert_audit(
                    connection,
                    event=f"{kind}_override.cleared" if enabled is None else f"{kind}_override.set",
                    plugin=None,
                    guild_id=scope,
                    actor_id=actor_id,
                    details={f"{kind}_id": normalized_id, "enabled": enabled},
                )

    def _resolve_bool_override(self, kind: str, subject_id: str, guild_id: int | str | None) -> bool | None:
        if guild_id is not None:
            local = self._get_bool_override(kind, subject_id, guild_id)
            if local is not None:
                return local
        return self._get_bool_override(kind, subject_id, None)

    @staticmethod
    def _insert_audit(
        connection: sqlite3.Connection,
        *,
        event: str,
        plugin: str | None,
        guild_id: int | None,
        actor_id: int | None,
        details: Mapping[str, Any],
    ) -> int:
        details_json = _encode_audit_details(details)
        cursor = connection.execute(
            "INSERT INTO audit_log(event, plugin, guild_id, actor_id, details_json) VALUES (?, ?, ?, ?, ?)",
            (event, plugin, guild_id, actor_id, details_json),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("audit insert did not produce an id")
        return int(cursor.lastrowid)

    @staticmethod
    def _override_record(kind: str, row: sqlite3.Row) -> OverrideRecord:
        value = int(row[2])
        return OverrideRecord(
            kind=kind,  # type: ignore[arg-type]
            subject_id=str(row[1]),
            guild_id=int(row[0]),
            enabled=None if kind == "permission" else bool(value),
            required_level=value if kind == "permission" else None,
            updated_by=int(row[3]),
            reason=str(row[4]),
            created_at=str(row[5]),
            updated_at=str(row[6]),
        )

    @staticmethod
    def _capability_actor_grant_record(row: sqlite3.Row) -> CapabilityActorGrantRecord:
        return CapabilityActorGrantRecord(
            capability_id=str(row["capability_id"]),
            guild_id=int(row["guild_id"]),
            subject_user_id=int(row["subject_user_id"]),
            grant_kind="owner_delegated",
            granted_by=int(row["granted_by"]),
            reason=str(row["reason"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _proposal_record(row: sqlite3.Row) -> EvolutionProposalRecord:
        return EvolutionProposalRecord(
            proposal_id=str(row["proposal_id"]),
            proposal_hash=str(row["proposal_hash"]),
            status=str(row["status"]),
            proposer_id=int(row["proposer_id"]),
            parent_proposal_id=row["parent_proposal_id"],
            updated_by=int(row["updated_by"]),
            reason=str(row["reason"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _get_evolution_proposal_locked(
        self, connection: sqlite3.Connection, proposal_id: str
    ) -> EvolutionProposalRecord:
        row = connection.execute("SELECT * FROM evolution_proposal WHERE proposal_id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown evolution proposal: {proposal_id}")
        return self._proposal_record(row)

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("database is not open")
        return self._connection


def _normalize_subject_id(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = value.strip().lower()
    if not 1 <= len(normalized) <= 128:
        raise ValueError(f"{label} must contain 1 to 128 characters")
    if any(not (character.isascii() and (character.isalnum() or character in "._-")) for character in normalized):
        raise ValueError(f"{label} contains an invalid character")
    return normalized


def _agent_audit_store_binding_digest(path: Path) -> str:
    canonical_path = os.path.normcase(str(Path(path).expanduser().resolve(strict=False)))
    payload = _AGENT_AUDIT_STORE_BINDING_VERSION + b"\0" + canonical_path.encode("utf-8", errors="strict")
    return hashlib.sha256(payload).hexdigest()


def _normalize_guild_id(value: int | str | None) -> int:
    if value is None:
        return GLOBAL_GUILD_ID
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError("guild_id must be an integer, decimal string, or None")
    if isinstance(value, str):
        if not value.strip().isascii() or not value.strip().isdigit():
            raise ValueError("guild_id must be a decimal string")
        normalized = int(value.strip())
    else:
        normalized = value
    if not 0 <= normalized <= MAX_SQLITE_ID:
        raise ValueError("guild_id must be a non-negative 64-bit integer")
    return normalized


def _normalize_required_guild_id(value: int | str) -> int:
    normalized = _normalize_guild_id(value)
    if normalized == GLOBAL_GUILD_ID:
        raise ValueError("delegated actor grants require a Discord guild")
    return normalized


def _normalize_actor_id(value: int, *, allow_system: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("actor ID must be an integer")
    minimum = 0 if allow_system else 1
    if not minimum <= value <= MAX_SQLITE_ID:
        raise ValueError(f"actor ID must be between {minimum} and {MAX_SQLITE_ID}")
    return value


def _normalize_reason(value: str | None) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError("reason must be a string or None")
    normalized = value.strip()
    if len(normalized) > 1000:
        raise ValueError("reason must contain at most 1000 characters")
    return normalized


def _normalize_level(value: int | str) -> int:
    if isinstance(value, bool):
        raise TypeError("level must be an integer or RBAC level name")
    if isinstance(value, str):
        names = {"everyone": 0, "trusted": 10, "moderator": 20, "guild_admin": 30, "guild_owner": 40, "bot_owner": 50}
        normalized: int | None = names.get(value.strip().lower())
        if normalized is None and value.strip().isdigit():
            normalized = int(value.strip())
    elif isinstance(value, int):
        normalized = value
    else:
        raise TypeError("level must be an integer or RBAC level name")
    if normalized not in {0, 10, 20, 30, 40, 50}:
        raise ValueError("unknown RBAC level")
    return normalized


def _normalize_hash(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("proposal_hash must be a string")
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError("proposal_hash must be a 64-character hexadecimal SHA-256 digest")
    return normalized


def _normalize_proposal_status(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("status must be a string")
    normalized = value.strip().lower()
    if normalized not in _PROPOSAL_TRANSITIONS:
        raise ValueError("unknown proposal status")
    return normalized


def _normalize_event(value: str) -> str:
    normalized = _normalize_subject_id(value, "event")
    if len(normalized) > 100:
        raise ValueError("event must contain at most 100 characters")
    return normalized


def _safe_audit_details(details: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(details, Mapping):
        raise TypeError("audit details must be a mapping")

    def convert(value: Any, *, key: str | None = None, depth: int = 0) -> Any:
        if depth > 8:
            raise ValueError("audit details nesting is too deep")
        if key is not None:
            normalized_key = key.strip().lower().replace("-", "_")
            if not normalized_key or len(normalized_key) > 100:
                raise ValueError("audit detail keys must contain 1 to 100 characters")
            if normalized_key in _FORBIDDEN_DETAIL_KEYS or normalized_key.endswith(_FORBIDDEN_DETAIL_SUFFIXES):
                raise ValueError(f"audit details must not contain secrets or body text: {key}")
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("audit details must not contain non-finite numbers")
            return value
        if isinstance(value, str):
            if len(value) > 2000:
                raise ValueError("audit detail strings must contain at most 2000 characters")
            return value
        if isinstance(value, Mapping):
            converted: dict[str, Any] = {}
            for nested_key, nested_value in value.items():
                if not isinstance(nested_key, str):
                    raise TypeError("audit detail keys must be strings")
                converted[nested_key] = convert(nested_value, key=nested_key, depth=depth + 1)
            return converted
        if isinstance(value, (list, tuple)):
            return [convert(item, depth=depth + 1) for item in value]
        raise TypeError("audit details must contain only JSON-compatible metadata")

    return convert(details)


def _encode_audit_details(details: Mapping[str, Any]) -> str:
    safe_details = _safe_audit_details(details)
    encoded = json.dumps(safe_details, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > 16_384:
        raise ValueError("audit details must encode to at most 16384 bytes")
    return encoded
