"""Recipe Forge Stage 2aの最小SQLite lifecycle CAS。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from .domain import (
    ForgePrimitiveManifest,
    RecipeCandidate,
    RecipeReceipt,
    RecipeRunStatus,
)
from .recipe import ForgePrimitiveRegistry, RecipeRunner
from .sandbox_contract import SANDBOX_POLICY_REVISION, SandboxEntrypoint


LIFECYCLE_SCHEMA_REVISION = 2
MAX_DESCRIPTION_CHARS = 160
MAX_DESCRIPTION_BYTES = 512
MAX_NOTIFICATION_LEASE_SECONDS = 300
MAX_SQLITE_USER_ID = 9_223_372_036_854_775_807

_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_REVISION = re.compile(r"[1-9][0-9]{0,15}\Z")
_FAILURE_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_WINDOWS_ABSOLUTE = re.compile(r"[A-Za-z]:[\\/]")
_SECRET_TEXT = re.compile(
    r"(?:\bsk-(?:proj-)?[A-Za-z0-9_-]{12,}\b|-----BEGIN .*PRIVATE KEY-----|"
    r"\b(?:api[_-]?key|authorization|password|secret|token)\s*[:=])",
    re.IGNORECASE,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS forge_recipe_candidate (
    recipe_digest TEXT PRIMARY KEY
        CHECK(length(recipe_digest) = 64)
        CHECK(recipe_digest = lower(recipe_digest))
        CHECK(recipe_digest NOT GLOB '*[^0-9a-f]*'),
    candidate_kind TEXT NOT NULL DEFAULT 'sealed_recipe'
        CHECK(candidate_kind IN ('sealed_recipe', 'sandbox_python_pure', 'sandbox_browser_readonly')),
    templates_json TEXT NOT NULL CHECK(length(templates_json) BETWEEN 2 AND 4096),
    code_owned_description TEXT NOT NULL CHECK(length(code_owned_description) BETWEEN 1 AND 160),
    lifecycle_state TEXT NOT NULL
        CHECK(lifecycle_state IN ('discovered', 'kept', 'rejected', 'promote_requested')),
    notification_state TEXT NOT NULL
        CHECK(notification_state IN ('pending', 'claimed', 'sent')),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    success_count INTEGER NOT NULL CHECK(success_count >= 1),
    notification_attempts INTEGER NOT NULL DEFAULT 0 CHECK(notification_attempts >= 0),
    first_success_at TEXT NOT NULL,
    last_success_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    claim_token TEXT,
    claim_expires_at TEXT,
    notification_sent_at TEXT,
    retry_not_before TEXT,
    last_failure_code TEXT,
    CHECK(
        (notification_state = 'pending' AND claim_token IS NULL AND claim_expires_at IS NULL)
        OR (notification_state = 'claimed' AND claim_token IS NOT NULL AND claim_expires_at IS NOT NULL)
        OR (
            notification_state = 'sent'
            AND claim_token IS NULL
            AND claim_expires_at IS NULL
            AND notification_sent_at IS NOT NULL
        )
    )
);
CREATE TABLE IF NOT EXISTS forge_recipe_user_success (
    recipe_digest TEXT NOT NULL
        REFERENCES forge_recipe_candidate(recipe_digest) ON UPDATE RESTRICT ON DELETE RESTRICT,
    user_id INTEGER NOT NULL CHECK(user_id BETWEEN 1 AND 9223372036854775807),
    success_count INTEGER NOT NULL CHECK(success_count >= 1),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    first_success_at TEXT NOT NULL,
    last_success_at TEXT NOT NULL,
    PRIMARY KEY(recipe_digest, user_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS forge_recipe_notification_pending_idx
ON forge_recipe_candidate(notification_state, retry_not_before, claim_expires_at, created_at);
"""


class ForgeLifecycleError(RuntimeError):
    pass


class LifecycleValidationError(ForgeLifecycleError, ValueError):
    pass


class LifecycleIntegrityError(ForgeLifecycleError):
    pass


class CandidateKind(StrEnum):
    SEALED_RECIPE = "sealed_recipe"
    SANDBOX_PYTHON_PURE = "sandbox_python_pure"
    SANDBOX_BROWSER_READONLY = "sandbox_browser_readonly"


class LifecycleState(str):
    DISCOVERED = "discovered"
    KEPT = "kept"
    REJECTED = "rejected"
    PROMOTE_REQUESTED = "promote_requested"


class NotificationState(str):
    PENDING = "pending"
    CLAIMED = "claimed"
    SENT = "sent"


@dataclass(frozen=True, slots=True)
class TemplateIdentity:
    primitive_id: str
    revision: str

    def __post_init__(self) -> None:
        if not isinstance(self.primitive_id, str) or not _IDENTIFIER.fullmatch(self.primitive_id):
            raise LifecycleValidationError("invalid template identity")
        if not isinstance(self.revision, str) or not _REVISION.fullmatch(self.revision):
            raise LifecycleValidationError("invalid template revision")


@dataclass(frozen=True, slots=True)
class CodeOwnedDescription:
    text: str
    code_owned: bool

    def __post_init__(self) -> None:
        if self.code_owned is not True or not isinstance(self.text, str) or self.text != self.text.strip():
            raise LifecycleValidationError("description must be an explicit code-owned constant")
        try:
            encoded = self.text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise LifecycleValidationError("description must be valid UTF-8") from exc
        if (
            not self.text
            or len(self.text) > MAX_DESCRIPTION_CHARS
            or len(encoded) > MAX_DESCRIPTION_BYTES
            or any(ord(character) < 32 for character in self.text)
            or _looks_like_host_path(self.text)
            or _SECRET_TEXT.search(self.text)
        ):
            raise LifecycleValidationError("description is outside the code-owned metadata contract")


SANDBOX_PYTHON_PURE_IDENTITY = TemplateIdentity(
    SandboxEntrypoint.PYTHON_PURE.value,
    SANDBOX_POLICY_REVISION,
)
SANDBOX_BROWSER_READONLY_IDENTITY = TemplateIdentity("browser_readonly", "1")


@dataclass(frozen=True, slots=True)
class LifecycleCandidate:
    recipe_digest: str
    candidate_kind: CandidateKind
    templates: tuple[TemplateIdentity, ...]
    code_owned_description: str
    lifecycle_state: str
    notification_state: str
    revision: int
    success_count: int
    notification_attempts: int
    first_success_at: datetime
    last_success_at: datetime
    created_at: datetime
    updated_at: datetime
    notification_sent_at: datetime | None
    retry_not_before: datetime | None
    last_failure_code: str | None
    official: bool = field(init=False, default=False)
    runtime_ready: bool = field(init=False, default=False)
    owner_notification_transport_ready: bool = field(init=False, default=False)


@dataclass(frozen=True, slots=True)
class NotificationClaim:
    candidate: LifecycleCandidate
    claim_token: str = field(repr=False)
    claim_expires_at: datetime


@dataclass(frozen=True, slots=True)
class UserSuccessSummary:
    recipe_digest: str
    user_id: int
    success_count: int
    revision: int
    first_success_at: datetime
    last_success_at: datetime


@dataclass(frozen=True, slots=True)
class RecordSuccessResult:
    candidate: LifecycleCandidate
    candidate_created: bool
    user_success: UserSuccessSummary


class SqliteForgeLifecycleRepository:
    """候補lifecycleと一度だけ通知claimだけを所有するSQLite repository。"""

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise TypeError("path must be a Path")
        self.path = path
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def open(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self.path,
                timeout=5,
                check_same_thread=False,
                isolation_level=None,
            )
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA busy_timeout = 5000")
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(_SCHEMA)
                _migrate_candidate_kind(connection)
            except BaseException:
                connection.close()
                raise
            self._connection = connection

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def record_success(
        self,
        *,
        user_id: int,
        candidate: RecipeCandidate,
        receipt: RecipeReceipt,
        registry: ForgePrimitiveRegistry,
        description: CodeOwnedDescription,
        succeeded_at: datetime,
    ) -> RecordSuccessResult:
        normalized_user_id = _user_id(user_id)
        normalized_at = _aware_utc(succeeded_at)
        if (
            not isinstance(candidate, RecipeCandidate)
            or not isinstance(receipt, RecipeReceipt)
            or receipt.status is not RecipeRunStatus.SUCCEEDED
            or receipt.recipe_digest != candidate.digest
            or receipt.completed_steps != receipt.total_steps
            or receipt.total_steps != len(candidate.steps)
            or not isinstance(registry, ForgePrimitiveRegistry)
            or not isinstance(description, CodeOwnedDescription)
        ):
            raise LifecycleValidationError("record_success requires a matching successful Stage 1 receipt")
        if RecipeRunner(registry).preflight(candidate) != receipt.recipe_digest:
            raise LifecycleValidationError("record_success recipe preflight changed")
        templates = _code_owned_templates(candidate, registry)
        return self._record_metadata_success(
            user_id=normalized_user_id,
            digest=candidate.digest,
            candidate_kind=CandidateKind.SEALED_RECIPE,
            templates=templates,
            description=description,
            succeeded_at=normalized_at,
        )

    def record_sandbox_success(
        self,
        *,
        user_id: int,
        sandbox: object,
        outcome: object,
        candidate: object,
        succeeded_at: datetime,
    ) -> RecordSuccessResult:
        """Persist only fixed metadata for a cleanup-confirmed sandbox success."""
        from .sandbox_contract import SandboxCandidate, SandboxResult
        from .sandbox_service import ExternalSandboxService, SandboxRunOutcome

        normalized_user_id = _user_id(user_id)
        normalized_at = _aware_utc(succeeded_at)
        if (
            type(sandbox) is not ExternalSandboxService
            or type(outcome) is not SandboxRunOutcome
            or type(candidate) is not SandboxCandidate
            or type(outcome.result) is not SandboxResult
            or outcome.result.scope.user_id != normalized_user_id
        ):
            raise LifecycleValidationError("sandbox success evidence is not service-confirmed")
        result = sandbox.consume_cleanup_confirmed_success(outcome=outcome, candidate=candidate)
        if type(result) is not SandboxResult:
            raise LifecycleValidationError("sandbox success evidence is not service-confirmed")
        return self._record_metadata_success(
            user_id=normalized_user_id,
            digest=_sandbox_proposal_digest(candidate, result),
            candidate_kind=CandidateKind.SANDBOX_PYTHON_PURE,
            templates=(SANDBOX_PYTHON_PURE_IDENTITY,),
            description=SANDBOX_PYTHON_PURE_DESCRIPTION,
            succeeded_at=normalized_at,
        )

    def _record_metadata_success(
        self,
        *,
        user_id: int,
        digest: str,
        candidate_kind: CandidateKind,
        templates: tuple[TemplateIdentity, ...],
        description: CodeOwnedDescription,
        succeeded_at: datetime,
    ) -> RecordSuccessResult:
        templates_json = _templates_json(templates)
        timestamp = _timestamp(succeeded_at)
        connection = self._required()
        with self._lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM forge_recipe_candidate WHERE recipe_digest=?",
                    (digest,),
                ).fetchone()
                created = existing is None
                if existing is None:
                    connection.execute(
                        """INSERT INTO forge_recipe_candidate(
                            recipe_digest, candidate_kind, templates_json, code_owned_description,
                            lifecycle_state, notification_state, revision, success_count,
                            notification_attempts, first_success_at, last_success_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'discovered', 'pending', 1, 1, 0, ?, ?, ?, ?)""",
                        (
                            digest,
                            candidate_kind.value,
                            templates_json,
                            description.text,
                            timestamp,
                            timestamp,
                            timestamp,
                            timestamp,
                        ),
                    )
                else:
                    if (
                        existing["candidate_kind"] != candidate_kind.value
                        or existing["templates_json"] != templates_json
                        or existing["code_owned_description"] != description.text
                    ):
                        raise LifecycleIntegrityError("digest metadata binding changed")
                    connection.execute(
                        """UPDATE forge_recipe_candidate
                        SET success_count=success_count+1,
                            first_success_at=MIN(first_success_at, ?),
                            last_success_at=MAX(last_success_at, ?),
                            updated_at=MAX(updated_at, ?)
                        WHERE recipe_digest=?""",
                        (timestamp, timestamp, timestamp, digest),
                    )
                connection.execute(
                    """INSERT INTO forge_recipe_user_success(
                        recipe_digest, user_id, success_count, revision, first_success_at, last_success_at
                    ) VALUES (?, ?, 1, 1, ?, ?)
                    ON CONFLICT(recipe_digest, user_id) DO UPDATE SET
                        success_count=success_count+1,
                        revision=revision+1,
                        first_success_at=MIN(first_success_at, excluded.first_success_at),
                        last_success_at=MAX(last_success_at, excluded.last_success_at)""",
                    (digest, user_id, timestamp, timestamp),
                )
                candidate_row = connection.execute(
                    "SELECT * FROM forge_recipe_candidate WHERE recipe_digest=?",
                    (digest,),
                ).fetchone()
                user_row = connection.execute(
                    """SELECT * FROM forge_recipe_user_success
                    WHERE recipe_digest=? AND user_id=?""",
                    (digest, user_id),
                ).fetchone()
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return RecordSuccessResult(
            candidate=_candidate_from_row(candidate_row),
            candidate_created=created,
            user_success=_user_success_from_row(user_row),
        )

    def get_candidate(self, recipe_digest: str) -> LifecycleCandidate | None:
        digest = _digest(recipe_digest)
        with self._lock:
            row = (
                self._required()
                .execute(
                    "SELECT * FROM forge_recipe_candidate WHERE recipe_digest=?",
                    (digest,),
                )
                .fetchone()
            )
        return None if row is None else _candidate_from_row(row)

    def get_user_success(self, recipe_digest: str, user_id: int) -> UserSuccessSummary | None:
        digest = _digest(recipe_digest)
        normalized_user_id = _user_id(user_id)
        with self._lock:
            row = (
                self._required()
                .execute(
                    """SELECT * FROM forge_recipe_user_success
                WHERE recipe_digest=? AND user_id=?""",
                    (digest, normalized_user_id),
                )
                .fetchone()
            )
        return None if row is None else _user_success_from_row(row)

    def claim_pending_notification(
        self,
        *,
        now: datetime,
        lease: timedelta = timedelta(seconds=60),
    ) -> NotificationClaim | None:
        normalized_now = _aware_utc(now)
        normalized_lease = _lease(lease)
        now_text = _timestamp(normalized_now)
        expires_at = normalized_now + normalized_lease
        expires_text = _timestamp(expires_at)
        token = uuid.uuid4().hex
        connection = self._required()
        with self._lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """SELECT * FROM forge_recipe_candidate
                    WHERE (
                        notification_state='pending'
                        AND (retry_not_before IS NULL OR retry_not_before<=?)
                    ) OR (
                        notification_state='claimed' AND claim_expires_at<=?
                    )
                    ORDER BY created_at, recipe_digest LIMIT 1""",
                    (now_text, now_text),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                cursor = connection.execute(
                    """UPDATE forge_recipe_candidate
                    SET notification_state='claimed', claim_token=?, claim_expires_at=?,
                        notification_attempts=notification_attempts+1,
                        revision=revision+1, updated_at=?
                    WHERE recipe_digest=? AND revision=? AND (
                        (
                            notification_state='pending'
                            AND (retry_not_before IS NULL OR retry_not_before<=?)
                        ) OR (
                            notification_state='claimed' AND claim_expires_at<=?
                        )
                    )""",
                    (
                        token,
                        expires_text,
                        now_text,
                        row["recipe_digest"],
                        row["revision"],
                        now_text,
                        now_text,
                    ),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return None
                claimed = connection.execute(
                    "SELECT * FROM forge_recipe_candidate WHERE recipe_digest=?",
                    (row["recipe_digest"],),
                ).fetchone()
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return NotificationClaim(
            candidate=_candidate_from_row(claimed),
            claim_token=token,
            claim_expires_at=expires_at,
        )

    def mark_notification_sent(
        self,
        *,
        recipe_digest: str,
        claim_token: str,
        expected_revision: int,
        sent_at: datetime,
    ) -> LifecycleCandidate | None:
        timestamp = _timestamp(_aware_utc(sent_at))
        return self._notification_cas(
            recipe_digest=recipe_digest,
            claim_token=claim_token,
            expected_revision=expected_revision,
            timestamp=timestamp,
            sql="""UPDATE forge_recipe_candidate
                SET notification_state='sent', claim_token=NULL, claim_expires_at=NULL,
                    notification_sent_at=?, retry_not_before=NULL, last_failure_code=NULL,
                    revision=revision+1, updated_at=?
                WHERE recipe_digest=? AND revision=? AND notification_state='claimed'
                    AND claim_token=? AND claim_expires_at>?""",
            parameters=(timestamp, timestamp),
        )

    def mark_notification_retryable_failure(
        self,
        *,
        recipe_digest: str,
        claim_token: str,
        expected_revision: int,
        failed_at: datetime,
        retry_not_before: datetime,
        failure_code: str,
    ) -> LifecycleCandidate | None:
        normalized_failed_at = _aware_utc(failed_at)
        normalized_retry = _aware_utc(retry_not_before)
        if normalized_retry < normalized_failed_at:
            raise LifecycleValidationError("retry_not_before must not precede failed_at")
        normalized_failure = _failure_code(failure_code)
        failed_text = _timestamp(normalized_failed_at)
        retry_text = _timestamp(normalized_retry)
        return self._notification_cas(
            recipe_digest=recipe_digest,
            claim_token=claim_token,
            expected_revision=expected_revision,
            timestamp=failed_text,
            sql="""UPDATE forge_recipe_candidate
                SET notification_state='pending', claim_token=NULL, claim_expires_at=NULL,
                    retry_not_before=?, last_failure_code=?,
                    revision=revision+1, updated_at=?
                WHERE recipe_digest=? AND revision=? AND notification_state='claimed'
                    AND claim_token=? AND claim_expires_at>?""",
            parameters=(retry_text, normalized_failure, failed_text),
        )

    def keep(
        self,
        recipe_digest: str,
        *,
        expected_revision: int,
        changed_at: datetime,
    ) -> LifecycleCandidate | None:
        return self._transition(
            recipe_digest,
            expected_revision=expected_revision,
            changed_at=changed_at,
            target=LifecycleState.KEPT,
            allowed=(LifecycleState.DISCOVERED,),
        )

    def reject(
        self,
        recipe_digest: str,
        *,
        expected_revision: int,
        changed_at: datetime,
    ) -> LifecycleCandidate | None:
        return self._transition(
            recipe_digest,
            expected_revision=expected_revision,
            changed_at=changed_at,
            target=LifecycleState.REJECTED,
            allowed=(LifecycleState.DISCOVERED, LifecycleState.KEPT),
        )

    def request_promotion(
        self,
        recipe_digest: str,
        *,
        expected_revision: int,
        changed_at: datetime,
    ) -> LifecycleCandidate | None:
        return self._transition(
            recipe_digest,
            expected_revision=expected_revision,
            changed_at=changed_at,
            target=LifecycleState.PROMOTE_REQUESTED,
            allowed=(LifecycleState.DISCOVERED, LifecycleState.KEPT),
        )

    def _notification_cas(
        self,
        *,
        recipe_digest: str,
        claim_token: str,
        expected_revision: int,
        timestamp: str,
        sql: str,
        parameters: tuple[object, ...],
    ) -> LifecycleCandidate | None:
        digest = _digest(recipe_digest)
        token = _claim_token(claim_token)
        revision = _expected_revision(expected_revision)
        connection = self._required()
        with self._lock, connection:
            cursor = connection.execute(
                sql,
                (*parameters, digest, revision, token, timestamp),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM forge_recipe_candidate WHERE recipe_digest=?",
                (digest,),
            ).fetchone()
        return _candidate_from_row(row)

    def _transition(
        self,
        recipe_digest: str,
        *,
        expected_revision: int,
        changed_at: datetime,
        target: str,
        allowed: tuple[str, ...],
    ) -> LifecycleCandidate | None:
        digest = _digest(recipe_digest)
        revision = _expected_revision(expected_revision)
        timestamp = _timestamp(_aware_utc(changed_at))
        placeholders = ",".join("?" for _ in allowed)
        connection = self._required()
        with self._lock, connection:
            cursor = connection.execute(
                f"""UPDATE forge_recipe_candidate
                SET lifecycle_state=?, revision=revision+1, updated_at=?
                WHERE recipe_digest=? AND revision=? AND notification_state='sent'
                    AND lifecycle_state IN ({placeholders})""",
                (target, timestamp, digest, revision, *allowed),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM forge_recipe_candidate WHERE recipe_digest=?",
                (digest,),
            ).fetchone()
        return _candidate_from_row(row)

    def _required(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("Forge lifecycle repository is not open")
        return self._connection


def _code_owned_templates(
    candidate: RecipeCandidate,
    registry: ForgePrimitiveRegistry,
) -> tuple[TemplateIdentity, ...]:
    templates: list[TemplateIdentity] = []
    for step in candidate.steps:
        manifest: ForgePrimitiveManifest = registry.resolve(step.primitive_id)
        if manifest.revision != step.primitive_revision:
            raise LifecycleValidationError("candidate template revision changed")
        templates.append(TemplateIdentity(manifest.primitive_id, manifest.revision))
    return tuple(templates)


def _templates_json(templates: tuple[TemplateIdentity, ...]) -> str:
    payload = json.dumps(
        [{"id": item.primitive_id, "revision": item.revision} for item in templates],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(payload.encode("ascii")) > 4096:
        raise LifecycleValidationError("template metadata exceeds the SQLite contract")
    return payload


def _migrate_candidate_kind(connection: sqlite3.Connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(forge_recipe_candidate)").fetchall()}
    if "candidate_kind" not in columns:
        connection.execute(
            """ALTER TABLE forge_recipe_candidate
            ADD COLUMN candidate_kind TEXT NOT NULL DEFAULT 'sealed_recipe'
            CHECK(candidate_kind IN ('sealed_recipe', 'sandbox_python_pure', 'sandbox_browser_readonly'))"""
        )


def _sandbox_proposal_digest(candidate: object, result: object) -> str:
    from .sandbox_contract import SandboxCandidate, SandboxResult

    if type(candidate) is not SandboxCandidate or type(result) is not SandboxResult:
        raise LifecycleValidationError("sandbox proposal binding is invalid")
    digest = hashlib.sha256()
    digest.update(b"yonerai.capability_forge.sandbox.lifecycle.v1\0")
    for value in (
        candidate.entrypoint.value.encode("ascii"),
        candidate.source.encode("utf-8"),
        result.policy_digest.encode("ascii"),
        result.backend_identity.encode("ascii"),
    ):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return digest.hexdigest()


def _candidate_from_row(row: sqlite3.Row | None) -> LifecycleCandidate:
    if row is None:
        raise LifecycleIntegrityError("candidate row disappeared")
    try:
        raw_templates = json.loads(row["templates_json"])
        templates = tuple(
            TemplateIdentity(item["id"], item["revision"])
            for item in raw_templates
            if isinstance(item, dict) and set(item) == {"id", "revision"}
        )
        recipe_digest = _digest(row["recipe_digest"])
        description = CodeOwnedDescription(
            row["code_owned_description"],
            code_owned=True,
        ).text
        candidate_kind = CandidateKind(row["candidate_kind"])
        last_failure_code = None if row["last_failure_code"] is None else _failure_code(row["last_failure_code"])
    except (TypeError, KeyError, ValueError) as exc:
        raise LifecycleIntegrityError("stored lifecycle metadata is invalid") from exc
    if not isinstance(raw_templates, list) or len(templates) != len(raw_templates) or not templates:
        raise LifecycleIntegrityError("stored template metadata is invalid")
    lifecycle_state = row["lifecycle_state"]
    notification_state = row["notification_state"]
    if lifecycle_state not in {
        LifecycleState.DISCOVERED,
        LifecycleState.KEPT,
        LifecycleState.REJECTED,
        LifecycleState.PROMOTE_REQUESTED,
    } or notification_state not in {
        NotificationState.PENDING,
        NotificationState.CLAIMED,
        NotificationState.SENT,
    }:
        raise LifecycleIntegrityError("stored lifecycle state is invalid")
    return LifecycleCandidate(
        recipe_digest=recipe_digest,
        candidate_kind=candidate_kind,
        templates=templates,
        code_owned_description=description,
        lifecycle_state=lifecycle_state,
        notification_state=notification_state,
        revision=int(row["revision"]),
        success_count=int(row["success_count"]),
        notification_attempts=int(row["notification_attempts"]),
        first_success_at=_datetime(row["first_success_at"]),
        last_success_at=_datetime(row["last_success_at"]),
        created_at=_datetime(row["created_at"]),
        updated_at=_datetime(row["updated_at"]),
        notification_sent_at=(None if row["notification_sent_at"] is None else _datetime(row["notification_sent_at"])),
        retry_not_before=None if row["retry_not_before"] is None else _datetime(row["retry_not_before"]),
        last_failure_code=last_failure_code,
    )


def _user_success_from_row(row: sqlite3.Row | None) -> UserSuccessSummary:
    if row is None:
        raise LifecycleIntegrityError("user success row disappeared")
    return UserSuccessSummary(
        recipe_digest=_digest(row["recipe_digest"]),
        user_id=_user_id(row["user_id"]),
        success_count=int(row["success_count"]),
        revision=int(row["revision"]),
        first_success_at=_datetime(row["first_success_at"]),
        last_success_at=_datetime(row["last_success_at"]),
    )


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise LifecycleValidationError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _aware_utc(value).isoformat(timespec="microseconds")


def _datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise LifecycleIntegrityError("stored timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LifecycleIntegrityError("stored timestamp is not timezone-aware")
    return parsed.astimezone(UTC)


def _digest(value: object) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise LifecycleValidationError("recipe_digest must be a lowercase SHA-256 digest")
    return value


def _user_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_SQLITE_USER_ID:
        raise LifecycleValidationError("user_id is outside the SQLite identifier range")
    return value


def _expected_revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise LifecycleValidationError("expected_revision must be a positive integer")
    return value


def _claim_token(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise LifecycleValidationError("claim_token is invalid")
    return value


def _failure_code(value: object) -> str:
    if not isinstance(value, str) or not _FAILURE_CODE.fullmatch(value):
        raise LifecycleValidationError("failure_code is outside the stable code contract")
    return value


def _lease(value: object) -> timedelta:
    if not isinstance(value, timedelta):
        raise LifecycleValidationError("lease must be a timedelta")
    seconds = value.total_seconds()
    if not 0 < seconds <= MAX_NOTIFICATION_LEASE_SECONDS:
        raise LifecycleValidationError("lease is outside the short notification bound")
    return value


def _looks_like_host_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return (
        value.startswith(("\\\\", "//"))
        or _WINDOWS_ABSOLUTE.match(value) is not None
        or normalized.startswith("/")
        or ".." in normalized.split("/")
        or any(character in value for character in "*?[")
        or "://" in value
    )


SANDBOX_PYTHON_PURE_DESCRIPTION = CodeOwnedDescription(
    "External sandbox Python-pure success proposal",
    code_owned=True,
)
SANDBOX_BROWSER_READONLY_DESCRIPTION = CodeOwnedDescription(
    "Sandbox browser read-only success proposal",
    code_owned=True,
)


__all__ = [
    "CandidateKind",
    "CodeOwnedDescription",
    "ForgeLifecycleError",
    "LIFECYCLE_SCHEMA_REVISION",
    "LifecycleCandidate",
    "LifecycleIntegrityError",
    "LifecycleState",
    "LifecycleValidationError",
    "MAX_DESCRIPTION_BYTES",
    "MAX_DESCRIPTION_CHARS",
    "MAX_NOTIFICATION_LEASE_SECONDS",
    "NotificationClaim",
    "NotificationState",
    "RecordSuccessResult",
    "SANDBOX_BROWSER_READONLY_DESCRIPTION",
    "SANDBOX_BROWSER_READONLY_IDENTITY",
    "SANDBOX_PYTHON_PURE_DESCRIPTION",
    "SANDBOX_PYTHON_PURE_IDENTITY",
    "SqliteForgeLifecycleRepository",
    "TemplateIdentity",
    "UserSuccessSummary",
]
