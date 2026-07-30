"""Media artifact専用の永続indexとhard quota。"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .domain import ArtifactKind, ArtifactRef, ArtifactScope, MediaPipelineError, MediaValidationError


MEDIA_ARTIFACT_TTL_SECONDS = 24 * 60 * 60
MAX_DELIVERY_LEASES_PER_ARTIFACT = 16


@dataclass(frozen=True, slots=True)
class MediaArtifactLimits:
    global_count: int = 512
    global_bytes: int = 512 * 1024 * 1024
    guild_count: int = 256
    guild_bytes: int = 256 * 1024 * 1024
    user_count: int = 64
    user_bytes: int = 64 * 1024 * 1024
    request_count: int = 16
    request_bytes: int = 24 * 1024 * 1024

    def __post_init__(self) -> None:
        for name, value in (
            ("global_count", self.global_count),
            ("global_bytes", self.global_bytes),
            ("guild_count", self.guild_count),
            ("guild_bytes", self.guild_bytes),
            ("user_count", self.user_count),
            ("user_bytes", self.user_bytes),
            ("request_count", self.request_count),
            ("request_bytes", self.request_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise MediaValidationError(f"{name} must be a positive integer")


DEFAULT_MEDIA_ARTIFACT_LIMITS = MediaArtifactLimits()


@dataclass(frozen=True, slots=True, repr=False)
class MediaArtifactRecord:
    artifact_id: str
    request_id: str
    guild_id: int | None
    channel_id: int
    user_id: int
    scope_digest: str
    recipe_digest: str
    content_digest: str
    kind: ArtifactKind
    width: int
    height: int
    byte_size: int
    created_at: int
    expires_at: int

    @property
    def ref(self) -> ArtifactRef:
        return ArtifactRef(
            artifact_id=self.artifact_id,
            scope_digest=self.scope_digest,
            recipe_digest=self.recipe_digest,
            content_digest=self.content_digest,
            kind=self.kind,
            width=self.width,
            height=self.height,
            byte_size=self.byte_size,
        )


class MediaArtifactIndex:
    """AI state repositoryとは接続を共有しないmedia専用SQLite connection。"""

    def __init__(
        self,
        database_path: Path | str,
        *,
        limits: MediaArtifactLimits = DEFAULT_MEDIA_ARTIFACT_LIMITS,
        ttl_seconds: int = MEDIA_ARTIFACT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        path = Path(database_path).expanduser()
        if not path.is_absolute():
            raise MediaValidationError("media artifact index path must be absolute")
        if not isinstance(limits, MediaArtifactLimits):
            raise MediaValidationError("limits must be MediaArtifactLimits")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds < 1:
            raise MediaValidationError("ttl_seconds must be a positive integer")
        if not callable(clock):
            raise MediaValidationError("clock must be callable")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._limits = limits
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                path,
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            self._connection = connection
            self._create_schema()
        except BaseException as exc:
            self.close()
            if isinstance(exc, MediaPipelineError):
                raise
            raise MediaPipelineError("media artifact index is unavailable") from exc

    @property
    def limits(self) -> MediaArtifactLimits:
        return self._limits

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    def now(self) -> int:
        try:
            value = int(self._clock())
        except Exception as exc:
            raise MediaPipelineError("media artifact clock is unavailable") from exc
        if value < 0:
            raise MediaPipelineError("media artifact clock is unavailable")
        return value

    @contextmanager
    def immediate(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._required()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except sqlite3.Error as exc:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
                raise MediaPipelineError("media artifact index operation failed") from exc
            except BaseException:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
                raise

    def close(self) -> None:
        with self._lock:
            connection, self._connection = self._connection, None
            if connection is not None:
                connection.close()

    def get(self, connection: sqlite3.Connection, artifact_id: str) -> MediaArtifactRecord | None:
        row = connection.execute(
            """
            SELECT artifact_id, request_id, guild_id, channel_id, user_id,
                   scope_digest, recipe_digest, content_digest, kind,
                   width, height, byte_size, created_at, expires_at
            FROM media_artifacts
            WHERE artifact_id = ?
            """,
            (artifact_id,),
        ).fetchone()
        return _record(row) if row is not None else None

    def all(self, connection: sqlite3.Connection) -> tuple[MediaArtifactRecord, ...]:
        rows = connection.execute(
            """
            SELECT artifact_id, request_id, guild_id, channel_id, user_id,
                   scope_digest, recipe_digest, content_digest, kind,
                   width, height, byte_size, created_at, expires_at
            FROM media_artifacts
            ORDER BY artifact_id
            """
        ).fetchall()
        return tuple(_record(row) for row in rows)

    def expired(self, connection: sqlite3.Connection, now: int) -> tuple[MediaArtifactRecord, ...]:
        rows = connection.execute(
            """
            SELECT a.artifact_id, a.request_id, a.guild_id, a.channel_id, a.user_id,
                   a.scope_digest, a.recipe_digest, a.content_digest, a.kind,
                   a.width, a.height, a.byte_size, a.created_at, a.expires_at
            FROM media_artifacts AS a
            WHERE a.expires_at <= ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM media_artifact_delivery_leases AS lease
                  WHERE lease.artifact_id = a.artifact_id
                    AND lease.retain_until > ?
                    AND lease.released_at IS NULL
              )
            ORDER BY artifact_id
            """,
            (now, now),
        ).fetchall()
        return tuple(_record(row) for row in rows)

    def delete(self, connection: sqlite3.Connection, artifact_id: str) -> None:
        connection.execute("DELETE FROM media_artifacts WHERE artifact_id = ?", (artifact_id,))

    def expire_delivery_leases(self, connection: sqlite3.Connection, now: int) -> None:
        connection.execute(
            "DELETE FROM media_artifact_delivery_leases WHERE retain_until <= ?",
            (now,),
        )

    def is_retained(self, connection: sqlite3.Connection, artifact_id: str, now: int) -> bool:
        row = connection.execute(
            """
            SELECT 1
            FROM media_artifact_delivery_leases
            WHERE artifact_id = ?
              AND retain_until > ?
              AND released_at IS NULL
            LIMIT 1
            """,
            (artifact_id, now),
        ).fetchone()
        return row is not None

    def retain_for_delivery(
        self,
        connection: sqlite3.Connection,
        *,
        delivery_key_digest: str,
        artifact_id: str,
        scope_digest: str,
        store_identity: str,
        retain_until: int,
        created_at: int,
    ) -> int | None:
        row = connection.execute(
            """
            SELECT scope_digest, store_identity, retain_until, released_at
            FROM media_artifact_delivery_leases
            WHERE delivery_key_digest = ? AND artifact_id = ?
            """,
            (delivery_key_digest, artifact_id),
        ).fetchone()
        if row is not None:
            if str(row["scope_digest"]) != scope_digest or str(row["store_identity"]) != store_identity:
                raise MediaPipelineError("media artifact delivery lease binding is invalid")
            if row["released_at"] is not None:
                return None
            return int(row["retain_until"])
        count_row = connection.execute(
            """
            SELECT COUNT(*) AS lease_count
            FROM media_artifact_delivery_leases
            WHERE artifact_id = ?
            """,
            (artifact_id,),
        ).fetchone()
        if count_row is None or int(count_row["lease_count"]) >= MAX_DELIVERY_LEASES_PER_ARTIFACT:
            raise MediaValidationError("media artifact delivery lease limit exceeded")
        connection.execute(
            """
            INSERT INTO media_artifact_delivery_leases (
                delivery_key_digest, artifact_id, scope_digest, store_identity,
                retain_until, created_at, released_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                delivery_key_digest,
                artifact_id,
                scope_digest,
                store_identity,
                retain_until,
                created_at,
            ),
        )
        return retain_until

    def release_delivery(
        self,
        connection: sqlite3.Connection,
        *,
        delivery_key_digest: str,
        artifact_id: str,
        scope_digest: str,
        store_identity: str,
    ) -> bool | None:
        row = connection.execute(
            """
            SELECT scope_digest, store_identity, released_at
            FROM media_artifact_delivery_leases
            WHERE delivery_key_digest = ? AND artifact_id = ?
            """,
            (delivery_key_digest, artifact_id),
        ).fetchone()
        if row is None:
            return None
        if str(row["scope_digest"]) != scope_digest or str(row["store_identity"]) != store_identity:
            raise MediaPipelineError("media artifact delivery lease binding is invalid")
        if row["released_at"] is not None:
            return False
        connection.execute(
            """
            UPDATE media_artifact_delivery_leases
            SET released_at = ?
            WHERE delivery_key_digest = ? AND artifact_id = ?
            """,
            (self.now(), delivery_key_digest, artifact_id),
        )
        return True

    def insert(
        self,
        connection: sqlite3.Connection,
        ref: ArtifactRef,
        scope: ArtifactScope,
        *,
        created_at: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO media_artifacts (
                artifact_id, request_id, guild_id, channel_id, user_id,
                scope_digest, recipe_digest, content_digest, kind,
                width, height, byte_size, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ref.artifact_id,
                scope.request_id,
                _discord_id(scope.guild_id),
                _discord_id(scope.channel_id),
                _discord_id(scope.user_id),
                ref.scope_digest,
                ref.recipe_digest,
                ref.content_digest,
                ref.kind.value,
                ref.width,
                ref.height,
                ref.byte_size,
                created_at,
                created_at + self._ttl_seconds,
            ),
        )

    def enforce_quota(
        self,
        connection: sqlite3.Connection,
        *,
        scope: ArtifactScope,
        incoming_bytes: int,
    ) -> None:
        limits = self._limits
        checks = [
            (None, (), limits.global_count, limits.global_bytes),
            (
                "scope_digest = ?",
                (scope.digest,),
                limits.request_count,
                limits.request_bytes,
            ),
            (
                "user_id = ?",
                (_discord_id(scope.user_id),),
                limits.user_count,
                limits.user_bytes,
            ),
        ]
        if scope.guild_id is not None:
            checks.append(
                (
                    "guild_id = ?",
                    (_discord_id(scope.guild_id),),
                    limits.guild_count,
                    limits.guild_bytes,
                )
            )
        for where, parameters, count_limit, byte_limit in checks:
            query = "SELECT COUNT(*) AS item_count, COALESCE(SUM(byte_size), 0) AS total_bytes FROM media_artifacts"
            if where is not None:
                query += f" WHERE {where}"
            row = connection.execute(query, parameters).fetchone()
            if row is None:
                raise MediaPipelineError("media artifact quota is unavailable")
            if int(row["item_count"]) + 1 > count_limit or int(row["total_bytes"]) + incoming_bytes > byte_limit:
                raise MediaValidationError("media artifact quota exceeded")

    def _create_schema(self) -> None:
        with self.immediate() as connection:
            statements = (
                """
                CREATE TABLE IF NOT EXISTS media_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    guild_id TEXT,
                    channel_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    scope_digest TEXT NOT NULL,
                    recipe_digest TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    byte_size INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_media_artifacts_expiry
                    ON media_artifacts (expires_at)
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_media_artifacts_request
                    ON media_artifacts (request_id)
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_media_artifacts_guild
                    ON media_artifacts (guild_id)
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_media_artifacts_user
                    ON media_artifacts (user_id)
                """,
                """
                CREATE TABLE IF NOT EXISTS media_artifact_delivery_leases (
                    delivery_key_digest TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    scope_digest TEXT NOT NULL,
                    store_identity TEXT NOT NULL,
                    retain_until INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    released_at INTEGER,
                    PRIMARY KEY (delivery_key_digest, artifact_id)
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_media_artifact_delivery_lease_expiry
                    ON media_artifact_delivery_leases (retain_until)
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_media_artifact_delivery_lease_artifact
                    ON media_artifact_delivery_leases (artifact_id)
                """,
            )
            for statement in statements:
                connection.execute(statement)

    def _required(self) -> sqlite3.Connection:
        if self._connection is None:
            raise MediaPipelineError("media artifact index is closed")
        return self._connection


def _discord_id(value: int | None) -> str | None:
    return None if value is None else str(value)


def _record(row: sqlite3.Row) -> MediaArtifactRecord:
    try:
        return MediaArtifactRecord(
            artifact_id=str(row["artifact_id"]),
            request_id=str(row["request_id"]),
            guild_id=None if row["guild_id"] is None else int(row["guild_id"]),
            channel_id=int(row["channel_id"]),
            user_id=int(row["user_id"]),
            scope_digest=str(row["scope_digest"]),
            recipe_digest=str(row["recipe_digest"]),
            content_digest=str(row["content_digest"]),
            kind=ArtifactKind(str(row["kind"])),
            width=int(row["width"]),
            height=int(row["height"]),
            byte_size=int(row["byte_size"]),
            created_at=int(row["created_at"]),
            expires_at=int(row["expires_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MediaPipelineError("media artifact index record is invalid") from exc


__all__ = [
    "DEFAULT_MEDIA_ARTIFACT_LIMITS",
    "MEDIA_ARTIFACT_TTL_SECONDS",
    "MAX_DELIVERY_LEASES_PER_ARTIFACT",
    "MediaArtifactIndex",
    "MediaArtifactLimits",
    "MediaArtifactRecord",
]
