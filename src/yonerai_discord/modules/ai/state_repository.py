"""AI consent and reply-continuation state stored in the suite SQLite DB."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


def migrate_v0_provider_preferences(connection: sqlite3.Connection) -> None:
    """v0 preference用の唯一のSQLite migration。既存行を変更しない。"""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS v0_provider_preferences (
            preference_key TEXT PRIMARY KEY,
            level TEXT NOT NULL CHECK(level IN ('user', 'conversation')),
            guild_id INTEGER,
            user_id INTEGER NOT NULL,
            conversation_key TEXT,
            scope_json TEXT NOT NULL,
            model_alias TEXT NOT NULL,
            provider_id TEXT
        )
        """
    )


@dataclass(frozen=True, slots=True)
class StoredConsent:
    user_id: int
    policy_version: str
    disclosure_version: str
    granted_at: float
    expires_at: float | None


@dataclass(frozen=True, slots=True)
class StoredConversation:
    session_id: str
    guild_id: int
    channel_id: int
    user_id: int
    created_at: float
    updated_at: float
    access_order: int
    exchanges: tuple[tuple[str, str, int | None], ...]


@dataclass(frozen=True, slots=True)
class StoredDisplayPreference:
    user_id: int
    mode: str
    updated_at: float


@dataclass(frozen=True, slots=True)
class StoredV0Memory:
    memory_id: str
    guild_id: int | None
    user_id: int
    channel_id: int | None
    dm_channel_id: int | None
    visibility: str
    content: str
    created_at: int
    expires_at: int


class AIStateRepository:
    """Small synchronous repository used only while the AI store lock is held."""

    def __init__(self, database_path: str | Path) -> None:
        path = Path(database_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._closed = False
        self._migrate()

    def _migrate(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_remote_consent (
                user_id INTEGER PRIMARY KEY,
                policy_version TEXT NOT NULL,
                disclosure_version TEXT NOT NULL,
                granted_at REAL NOT NULL,
                expires_at REAL
            );

            -- Wall clock rollbackで期限切れ同意を再有効化しないdurable floor。
            CREATE TABLE IF NOT EXISTS ai_remote_consent_clock_floor (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                observed_at REAL NOT NULL CHECK(observed_at >= 0)
            );

            INSERT OR IGNORE INTO ai_remote_consent_clock_floor(singleton, observed_at)
            VALUES (1, 0);

            CREATE TABLE IF NOT EXISTS ai_state_migration_marker (
                name TEXT PRIMARY KEY,
                applied_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ai_conversation_session (
                session_id TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                access_order INTEGER NOT NULL,
                UNIQUE (guild_id, channel_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS ai_conversation_exchange (
                session_id TEXT NOT NULL REFERENCES ai_conversation_session(session_id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                user_text TEXT NOT NULL,
                assistant_text TEXT NOT NULL,
                bot_message_id INTEGER UNIQUE,
                PRIMARY KEY (session_id, ordinal)
            );

            CREATE INDEX IF NOT EXISTS idx_ai_conversation_updated
                ON ai_conversation_session(updated_at);

            CREATE TABLE IF NOT EXISTS ai_display_preference (
                user_id INTEGER PRIMARY KEY,
                mode TEXT NOT NULL CHECK(mode IN ('auto','card','plain')),
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS v0_explicit_memory (
                memory_id TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL CHECK(guild_id >= 0),
                user_id INTEGER NOT NULL CHECK(user_id > 0),
                channel_id INTEGER,
                dm_channel_id INTEGER,
                visibility TEXT NOT NULL CHECK(visibility IN (
                    'user_private','channel_shared','guild_public','direct_message'
                )),
                content TEXT NOT NULL CHECK(length(content) BETWEEN 1 AND 4000),
                created_at INTEGER NOT NULL CHECK(created_at > 0),
                expires_at INTEGER NOT NULL CHECK(expires_at > created_at),
                CHECK (
                    (visibility = 'direct_message' AND guild_id = 0 AND dm_channel_id IS NOT NULL AND channel_id IS NULL)
                    OR
                    (visibility != 'direct_message' AND guild_id > 0 AND dm_channel_id IS NULL)
                )
            );

            CREATE INDEX IF NOT EXISTS idx_v0_explicit_memory_scope
                ON v0_explicit_memory(guild_id, user_id, channel_id, dm_channel_id, visibility, created_at DESC);

            CREATE TABLE IF NOT EXISTS v0_memory_privacy (
                guild_id INTEGER NOT NULL CHECK(guild_id > 0),
                user_id INTEGER NOT NULL CHECK(user_id > 0),
                visibility TEXT NOT NULL CHECK(visibility IN ('user_private','channel_shared','guild_public')),
                channel_id INTEGER,
                PRIMARY KEY(guild_id, user_id),
                CHECK (
                    (visibility = 'channel_shared' AND channel_id IS NOT NULL)
                    OR (visibility != 'channel_shared' AND channel_id IS NULL)
                )
            );

            -- 本文の正史を増やさず、待機中の変更だけを検知する認可sidecar。
            CREATE TABLE IF NOT EXISTS v0_memory_record_revision (
                memory_id TEXT PRIMARY KEY,
                revision INTEGER NOT NULL CHECK(revision > 0),
                updated_at INTEGER NOT NULL CHECK(updated_at > 0),
                present INTEGER NOT NULL CHECK(present IN (0, 1))
            );

            INSERT OR IGNORE INTO v0_memory_record_revision(memory_id, revision, updated_at, present)
            SELECT memory_id, 1, created_at, 1 FROM v0_explicit_memory;

            CREATE TABLE IF NOT EXISTS v0_memory_policy_revision (
                guild_id INTEGER NOT NULL CHECK(guild_id > 0),
                user_id INTEGER NOT NULL CHECK(user_id > 0),
                revision INTEGER NOT NULL CHECK(revision > 0),
                updated_at INTEGER NOT NULL CHECK(updated_at > 0),
                present INTEGER NOT NULL CHECK(present IN (0, 1)),
                PRIMARY KEY(guild_id, user_id)
            );

            -- Wall clock rollbackで期限切れmemoryを再有効化しないdurable floor。
            CREATE TABLE IF NOT EXISTS v0_memory_clock_floor (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                observed_at INTEGER NOT NULL CHECK(observed_at >= 0)
            );

            INSERT OR IGNORE INTO v0_memory_clock_floor(singleton, observed_at)
            VALUES (1, 0);

            INSERT OR IGNORE INTO v0_memory_policy_revision(guild_id, user_id, revision, updated_at, present)
            SELECT guild_id, user_id, 1, CAST(strftime('%s', 'now') AS INTEGER), 1
            FROM v0_memory_privacy;

            CREATE TRIGGER IF NOT EXISTS trg_v0_memory_record_revision_insert
            AFTER INSERT ON v0_explicit_memory
            BEGIN
                INSERT INTO v0_memory_record_revision(memory_id, revision, updated_at, present)
                VALUES (NEW.memory_id, 1, NEW.created_at, 1)
                ON CONFLICT(memory_id) DO UPDATE SET
                    revision = v0_memory_record_revision.revision + 1,
                    updated_at = MAX(
                        CAST(strftime('%s', 'now') AS INTEGER),
                        v0_memory_record_revision.updated_at + 1
                    ),
                    present = 1;
            END;

            CREATE TRIGGER IF NOT EXISTS trg_v0_memory_record_revision_update
            AFTER UPDATE ON v0_explicit_memory
            BEGIN
                UPDATE v0_memory_record_revision
                SET revision = revision + 1,
                    updated_at = MAX(CAST(strftime('%s', 'now') AS INTEGER), updated_at + 1),
                    present = 1
                WHERE memory_id = NEW.memory_id;
            END;

            CREATE TRIGGER IF NOT EXISTS trg_v0_memory_record_revision_delete
            AFTER DELETE ON v0_explicit_memory
            BEGIN
                UPDATE v0_memory_record_revision
                SET revision = revision + 1,
                    updated_at = MAX(CAST(strftime('%s', 'now') AS INTEGER), updated_at + 1),
                    present = 0
                WHERE memory_id = OLD.memory_id;
            END;

            CREATE TRIGGER IF NOT EXISTS trg_v0_memory_policy_revision_insert
            AFTER INSERT ON v0_memory_privacy
            BEGIN
                INSERT INTO v0_memory_policy_revision(guild_id, user_id, revision, updated_at, present)
                VALUES (
                    NEW.guild_id,
                    NEW.user_id,
                    1,
                    CAST(strftime('%s', 'now') AS INTEGER),
                    1
                )
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    revision = v0_memory_policy_revision.revision + 1,
                    updated_at = MAX(
                        CAST(strftime('%s', 'now') AS INTEGER),
                        v0_memory_policy_revision.updated_at + 1
                    ),
                    present = 1;
            END;

            CREATE TRIGGER IF NOT EXISTS trg_v0_memory_policy_revision_update
            AFTER UPDATE ON v0_memory_privacy
            BEGIN
                UPDATE v0_memory_policy_revision
                SET revision = revision + 1,
                    updated_at = MAX(CAST(strftime('%s', 'now') AS INTEGER), updated_at + 1),
                    present = 1
                WHERE guild_id = NEW.guild_id AND user_id = NEW.user_id;
            END;

            CREATE TRIGGER IF NOT EXISTS trg_v0_memory_policy_revision_delete
            AFTER DELETE ON v0_memory_privacy
            BEGIN
                UPDATE v0_memory_policy_revision
                SET revision = revision + 1,
                    updated_at = MAX(CAST(strftime('%s', 'now') AS INTEGER), updated_at + 1),
                    present = 0
                WHERE guild_id = OLD.guild_id AND user_id = OLD.user_id;
            END;
            """
        )
        self._migrate_remote_consent_clock_floor()

    def _migrate_remote_consent_clock_floor(self) -> None:
        """Fail closed once for TTL rows created before durable clock tracking."""

        marker = "remote_consent_clock_floor_v1"
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            applied = self._connection.execute(
                "SELECT 1 FROM ai_state_migration_marker WHERE name = ?",
                (marker,),
            ).fetchone()
            if applied is None:
                self._connection.execute("DELETE FROM ai_remote_consent WHERE expires_at IS NOT NULL")
                self._connection.execute(
                    """
                    INSERT INTO ai_state_migration_marker(name, applied_at)
                    VALUES (?, CAST(strftime('%s', 'now') AS REAL))
                    """,
                    (marker,),
                )
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        migrate_v0_provider_preferences(self._connection)

    def v0_connection(self) -> sqlite3.Connection:
        """Return the shared connection only to v0 row adapters in the composition root."""

        if self._closed:
            raise RuntimeError("AI state repository is closed")
        return self._connection

    def get_display_preference(self, user_id: int) -> StoredDisplayPreference | None:
        row = self._connection.execute(
            "SELECT user_id, mode, updated_at FROM ai_display_preference WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return StoredDisplayPreference(*row) if row is not None else None

    def upsert_display_preference(self, preference: StoredDisplayPreference) -> None:
        self._connection.execute(
            """
            INSERT INTO ai_display_preference (user_id, mode, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                mode = excluded.mode,
                updated_at = excluded.updated_at
            """,
            (preference.user_id, preference.mode, preference.updated_at),
        )

    def get_consent(self, user_id: int) -> StoredConsent | None:
        row = self._connection.execute(
            "SELECT user_id, policy_version, disclosure_version, granted_at, expires_at "
            "FROM ai_remote_consent WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return StoredConsent(*row) if row is not None else None

    def grant_consent(
        self,
        *,
        user_id: int,
        policy_version: str,
        disclosure_version: str,
        observed_at: float,
        ttl_seconds: int | None,
    ) -> tuple[StoredConsent | None, float, bool]:
        """Atomically observe time, invalidate rolled-back TTL rows, and grant."""

        normalized = _validated_clock(observed_at)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            previous = self._remote_consent_clock_floor()
            rolled_back = normalized < previous
            effective = max(previous, normalized)
            self._connection.execute(
                """
                UPDATE ai_remote_consent_clock_floor
                SET observed_at = ?
                WHERE singleton = 1
                """,
                (effective,),
            )
            if rolled_back:
                self._connection.execute("DELETE FROM ai_remote_consent WHERE expires_at IS NOT NULL")
            consent = None
            if ttl_seconds is None or not rolled_back:
                consent = StoredConsent(
                    user_id=user_id,
                    policy_version=policy_version,
                    disclosure_version=disclosure_version,
                    granted_at=effective,
                    expires_at=None if ttl_seconds is None else effective + ttl_seconds,
                )
                self._connection.execute(
                    """
                    INSERT INTO ai_remote_consent
                        (user_id, policy_version, disclosure_version, granted_at, expires_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        policy_version = excluded.policy_version,
                        disclosure_version = excluded.disclosure_version,
                        granted_at = excluded.granted_at,
                        expires_at = excluded.expires_at
                    """,
                    (
                        consent.user_id,
                        consent.policy_version,
                        consent.disclosure_version,
                        consent.granted_at,
                        consent.expires_at,
                    ),
                )
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        return consent, effective, rolled_back

    def delete_consent(self, user_id: int) -> bool:
        cursor = self._connection.execute("DELETE FROM ai_remote_consent WHERE user_id = ?", (user_id,))
        return cursor.rowcount > 0

    def delete_expired_consent(self, user_id: int, *, observed_at: float) -> bool:
        """Delete only a still-expiring row that is expired at the observed time."""

        now = _validated_clock(observed_at)
        cursor = self._connection.execute(
            """
            DELETE FROM ai_remote_consent
            WHERE user_id = ?
              AND expires_at IS NOT NULL
              AND expires_at <= ?
            """,
            (user_id, now),
        )
        return cursor.rowcount > 0

    def clear_consents(self) -> None:
        self._connection.execute("DELETE FROM ai_remote_consent")

    def observe_remote_consent_clock(self, now: float) -> tuple[float, bool]:
        """Persist the clock floor and report rollback against the prior observation."""

        normalized = _validated_clock(now)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            previous = self._remote_consent_clock_floor()
            rolled_back = normalized < previous
            effective = max(previous, normalized)
            self._connection.execute(
                """
                UPDATE ai_remote_consent_clock_floor
                SET observed_at = ?
                WHERE singleton = 1
                """,
                (effective,),
            )
            if rolled_back:
                self._connection.execute("DELETE FROM ai_remote_consent WHERE expires_at IS NOT NULL")
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        return effective, rolled_back

    def _remote_consent_clock_floor(self) -> float:
        row = self._connection.execute(
            "SELECT observed_at FROM ai_remote_consent_clock_floor WHERE singleton = 1"
        ).fetchone()
        if row is None or not math.isfinite(float(row[0])):
            raise RuntimeError("remote consent clock floor is unavailable")
        return float(row[0])

    def list_consents(self) -> tuple[StoredConsent, ...]:
        rows = self._connection.execute(
            "SELECT user_id, policy_version, disclosure_version, granted_at, expires_at "
            "FROM ai_remote_consent ORDER BY granted_at, user_id"
        ).fetchall()
        return tuple(StoredConsent(*row) for row in rows)

    def add_v0_memory(self, memory: StoredV0Memory) -> None:
        self._connection.execute(
            """
            INSERT INTO v0_explicit_memory
                (memory_id, guild_id, user_id, channel_id, dm_channel_id, visibility, content, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory.memory_id,
                0 if memory.guild_id is None else memory.guild_id,
                memory.user_id,
                memory.channel_id,
                memory.dm_channel_id,
                memory.visibility,
                memory.content,
                memory.created_at,
                memory.expires_at,
            ),
        )

    def list_v0_memories(
        self,
        *,
        guild_id: int | None,
        user_id: int,
        channel_id: int | None,
        dm_channel_id: int | None,
        visibility: str,
        now: int,
        limit: int = 20,
    ) -> tuple[StoredV0Memory, ...]:
        rows = self._connection.execute(
            """
            SELECT memory_id, guild_id, user_id, channel_id, dm_channel_id,
                   visibility, content, created_at, expires_at
            FROM v0_explicit_memory
            WHERE guild_id = ? AND user_id = ?
              AND channel_id IS ? AND dm_channel_id IS ? AND visibility = ?
              AND expires_at > ?
            ORDER BY created_at DESC, memory_id DESC
            LIMIT ?
            """,
            (0 if guild_id is None else guild_id, user_id, channel_id, dm_channel_id, visibility, now, limit),
        ).fetchall()
        return tuple(StoredV0Memory(row[0], None if row[1] == 0 else row[1], *row[2:]) for row in rows)

    def get_v0_memory(
        self,
        *,
        memory_id: str,
        guild_id: int | None,
        user_id: int,
        channel_id: int | None,
        dm_channel_id: int | None,
        visibility: str,
        now: int,
    ) -> StoredV0Memory | None:
        row = self._connection.execute(
            """
            SELECT memory_id, guild_id, user_id, channel_id, dm_channel_id,
                   visibility, content, created_at, expires_at
            FROM v0_explicit_memory
            WHERE memory_id = ? AND guild_id = ? AND user_id = ?
              AND channel_id IS ? AND dm_channel_id IS ? AND visibility = ?
              AND expires_at > ?
            """,
            (
                memory_id,
                0 if guild_id is None else guild_id,
                user_id,
                channel_id,
                dm_channel_id,
                visibility,
                now,
            ),
        ).fetchone()
        return None if row is None else StoredV0Memory(row[0], None if row[1] == 0 else row[1], *row[2:])

    def get_v0_memory_revision(self, memory_id: str) -> tuple[int, int, bool] | None:
        row = self._connection.execute(
            """
            SELECT revision, updated_at, present
            FROM v0_memory_record_revision
            WHERE memory_id = ?
            """,
            (memory_id,),
        ).fetchone()
        return None if row is None else (int(row[0]), int(row[1]), bool(row[2]))

    def observe_v0_memory_clock(self, now: int) -> int:
        """Persist a nondecreasing wall-clock floor for memory expiry decisions."""

        if isinstance(now, bool) or not isinstance(now, int) or now <= 0:
            raise ValueError("now must be a positive integer")
        self._connection.execute(
            """
            INSERT INTO v0_memory_clock_floor(singleton, observed_at)
            VALUES (1, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                observed_at = MAX(v0_memory_clock_floor.observed_at, excluded.observed_at)
            """,
            (now,),
        )
        row = self._connection.execute("SELECT observed_at FROM v0_memory_clock_floor WHERE singleton = 1").fetchone()
        if row is None:
            raise RuntimeError("memory clock floor is unavailable")
        return int(row[0])

    def get_v0_memory_policy_revision(self, *, guild_id: int, user_id: int) -> tuple[int, int, bool] | None:
        row = self._connection.execute(
            """
            SELECT revision, updated_at, present
            FROM v0_memory_policy_revision
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()
        return None if row is None else (int(row[0]), int(row[1]), bool(row[2]))

    def delete_v0_memory(
        self,
        *,
        memory_id: str,
        guild_id: int | None,
        user_id: int,
        channel_id: int | None,
        dm_channel_id: int | None,
        visibility: str,
    ) -> bool:
        cursor = self._connection.execute(
            """
            DELETE FROM v0_explicit_memory
            WHERE memory_id = ? AND guild_id = ? AND user_id = ?
              AND channel_id IS ? AND dm_channel_id IS ? AND visibility = ?
            """,
            (memory_id, 0 if guild_id is None else guild_id, user_id, channel_id, dm_channel_id, visibility),
        )
        return cursor.rowcount == 1

    def clear_v0_memories(
        self,
        *,
        guild_id: int | None,
        user_id: int,
        channel_id: int | None,
        dm_channel_id: int | None,
        visibility: str,
    ) -> int:
        cursor = self._connection.execute(
            """
            DELETE FROM v0_explicit_memory
            WHERE guild_id = ? AND user_id = ?
              AND channel_id IS ? AND dm_channel_id IS ? AND visibility = ?
            """,
            (0 if guild_id is None else guild_id, user_id, channel_id, dm_channel_id, visibility),
        )
        return max(0, cursor.rowcount)

    def clear_v0_memories_for_actor(
        self,
        *,
        guild_id: int | None,
        user_id: int,
        dm_channel_id: int | None = None,
    ) -> int:
        """本人の現在spaceだけを、過去のvisibility設定をまたいで削除する。"""

        if guild_id is None:
            if dm_channel_id is None:
                raise ValueError("DM clear requires dm_channel_id")
            cursor = self._connection.execute(
                """
                DELETE FROM v0_explicit_memory
                WHERE guild_id = 0 AND user_id = ? AND dm_channel_id = ?
                """,
                (user_id, dm_channel_id),
            )
        else:
            if dm_channel_id is not None:
                raise ValueError("guild clear must not declare dm_channel_id")
            cursor = self._connection.execute(
                "DELETE FROM v0_explicit_memory WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
        return max(0, cursor.rowcount)

    def get_v0_memory_privacy(self, *, guild_id: int, user_id: int) -> tuple[str, int | None]:
        row = self._connection.execute(
            "SELECT visibility, channel_id FROM v0_memory_privacy WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        return ("user_private", None) if row is None else (str(row[0]), row[1])

    def set_v0_memory_privacy(
        self,
        *,
        guild_id: int,
        user_id: int,
        visibility: str,
        channel_id: int | None,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO v0_memory_privacy(guild_id, user_id, visibility, channel_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                visibility = excluded.visibility,
                channel_id = excluded.channel_id
            """,
            (guild_id, user_id, visibility, channel_id),
        )

    def replace_conversations(self, conversations: Iterable[StoredConversation]) -> None:
        rows = tuple(conversations)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute("DELETE FROM ai_conversation_session")
            for session in rows:
                self._connection.execute(
                    """
                    INSERT INTO ai_conversation_session
                        (session_id, guild_id, channel_id, user_id, created_at, updated_at, access_order)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session.session_id,
                        session.guild_id,
                        session.channel_id,
                        session.user_id,
                        session.created_at,
                        session.updated_at,
                        session.access_order,
                    ),
                )
                self._connection.executemany(
                    """
                    INSERT INTO ai_conversation_exchange
                        (session_id, ordinal, user_text, assistant_text, bot_message_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        (session.session_id, ordinal, user_text, assistant_text, bot_message_id)
                        for ordinal, (user_text, assistant_text, bot_message_id) in enumerate(session.exchanges)
                    ),
                )
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def load_conversations(self) -> tuple[StoredConversation, ...]:
        sessions = self._connection.execute(
            """
            SELECT session_id, guild_id, channel_id, user_id, created_at, updated_at, access_order
            FROM ai_conversation_session
            ORDER BY access_order, created_at, session_id
            """
        ).fetchall()
        result: list[StoredConversation] = []
        for row in sessions:
            exchanges = self._connection.execute(
                """
                SELECT user_text, assistant_text, bot_message_id
                FROM ai_conversation_exchange
                WHERE session_id = ?
                ORDER BY ordinal
                """,
                (row[0],),
            ).fetchall()
            result.append(StoredConversation(*row, exchanges=tuple(exchanges)))
        return tuple(result)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connection.close()


__all__ = [
    "AIStateRepository",
    "StoredConsent",
    "StoredConversation",
    "StoredDisplayPreference",
    "StoredV0Memory",
    "migrate_v0_provider_preferences",
]


def _validated_clock(now: float) -> float:
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(float(now)) or float(now) < 0:
        raise ValueError("now must be a finite non-negative number")
    return float(now)
