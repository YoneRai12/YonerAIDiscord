from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .domain import MemoryItem, MemoryKind


SCHEMA = """
CREATE TABLE IF NOT EXISTS personal_memory_consent (
    guild_id INTEGER NOT NULL CHECK(guild_id > 0),
    user_id INTEGER NOT NULL CHECK(user_id > 0),
    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
    updated_at INTEGER NOT NULL CHECK(updated_at > 0),
    PRIMARY KEY(guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS personal_memory_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL CHECK(guild_id > 0),
    user_id INTEGER NOT NULL CHECK(user_id > 0),
    kind TEXT NOT NULL CHECK(kind IN ('fact', 'conversation')),
    content TEXT NOT NULL CHECK(length(content) BETWEEN 1 AND 4000),
    created_at INTEGER NOT NULL CHECK(created_at > 0),
    expires_at INTEGER NOT NULL CHECK(expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS personal_memory_items_owner_idx
ON personal_memory_items(guild_id, user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS personal_memory_items_expiry_idx
ON personal_memory_items(expires_at);
"""


class MemoryWriteRejectedError(PermissionError):
    pass


CommitAllowed = Callable[[], bool]


class SqlitePersonalMemoryRepository:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def open(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            self._connection = connection

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def set_enabled(
        self,
        guild_id: int,
        user_id: int,
        enabled: bool,
        *,
        now: int | None = None,
        commit_allowed: CommitAllowed | None = None,
    ) -> None:
        self._validate_owner(guild_id, user_id)
        timestamp = int(time.time()) if now is None else now
        if timestamp <= 0:
            raise ValueError("now must be positive")
        connection = self._required()
        with self._lock, connection:
            self._require_commit_allowed(commit_allowed)
            connection.execute(
                """INSERT INTO personal_memory_consent(guild_id,user_id,enabled,updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id,user_id) DO UPDATE SET
                    enabled=excluded.enabled, updated_at=excluded.updated_at""",
                (guild_id, user_id, int(enabled), timestamp),
            )

    def is_enabled(self, guild_id: int, user_id: int) -> bool:
        self._validate_owner(guild_id, user_id)
        with self._lock:
            row = (
                self._required()
                .execute(
                    "SELECT enabled FROM personal_memory_consent WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                )
                .fetchone()
            )
        return bool(row is not None and row["enabled"])

    def add(
        self,
        guild_id: int,
        user_id: int,
        kind: MemoryKind,
        content: str,
        *,
        created_at: int,
        expires_at: int,
        commit_allowed: CommitAllowed | None = None,
    ) -> MemoryItem:
        self._validate_owner(guild_id, user_id)
        normalized = content.strip()
        if not normalized or len(normalized) > 4_000:
            raise ValueError("memory content is invalid")
        if expires_at <= created_at or created_at <= 0:
            raise ValueError("memory timestamps are invalid")
        connection = self._required()
        with self._lock, connection:
            self._require_commit_allowed(commit_allowed)
            cursor = connection.execute(
                """INSERT INTO personal_memory_items(
                    guild_id,user_id,kind,content,created_at,expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (guild_id, user_id, kind.value, normalized, created_at, expires_at),
            )
            memory_id = int(cursor.lastrowid)
        return MemoryItem(memory_id, guild_id, user_id, kind, normalized, created_at, expires_at)

    def add_bounded(
        self,
        guild_id: int,
        user_id: int,
        kind: MemoryKind,
        content: str,
        *,
        created_at: int,
        expires_at: int,
        maximum: int,
        prune_expired: bool,
        commit_allowed: CommitAllowed,
    ) -> MemoryItem:
        """追加・期限切れ整理・上限整理を一つの再認可付きtransactionで行う。"""

        self._validate_owner(guild_id, user_id)
        normalized = content.strip()
        if not normalized or len(normalized) > 4_000:
            raise ValueError("memory content is invalid")
        if expires_at <= created_at or created_at <= 0:
            raise ValueError("memory timestamps are invalid")
        if maximum < 1:
            raise ValueError("maximum must be positive")
        connection = self._required()
        with self._lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_commit_allowed(commit_allowed)
                cursor = connection.execute(
                    """INSERT INTO personal_memory_items(
                        guild_id,user_id,kind,content,created_at,expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (guild_id, user_id, kind.value, normalized, created_at, expires_at),
                )
                memory_id = int(cursor.lastrowid)
                if prune_expired:
                    connection.execute("DELETE FROM personal_memory_items WHERE expires_at<=?", (created_at,))
                connection.execute(
                    """DELETE FROM personal_memory_items
                    WHERE guild_id=? AND user_id=? AND id NOT IN (
                        SELECT id FROM personal_memory_items
                        WHERE guild_id=? AND user_id=?
                        ORDER BY created_at DESC,id DESC LIMIT ?
                    )""",
                    (guild_id, user_id, guild_id, user_id, maximum),
                )
                # Policy or shutdown state can change while pruning/trimming.
                # Reauthorize after all staged mutations and immediately before
                # the durable commit. Rejection is rolled back below.
                self._require_commit_allowed(commit_allowed)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return MemoryItem(memory_id, guild_id, user_id, kind, normalized, created_at, expires_at)

    def list_items(
        self,
        guild_id: int,
        user_id: int,
        *,
        limit: int = 20,
        kind: MemoryKind | None = None,
        now: int | None = None,
    ) -> tuple[MemoryItem, ...]:
        self._validate_owner(guild_id, user_id)
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        timestamp = int(time.time()) if now is None else now
        parameters: list[object] = [guild_id, user_id, timestamp]
        kind_clause = ""
        if kind is not None:
            kind_clause = " AND kind=?"
            parameters.append(kind.value)
        parameters.append(limit)
        with self._lock:
            rows = (
                self._required()
                .execute(
                    """SELECT id,guild_id,user_id,kind,content,created_at,expires_at
                FROM personal_memory_items
                WHERE guild_id=? AND user_id=? AND expires_at>?"""
                    + kind_clause
                    + " ORDER BY created_at DESC,id DESC LIMIT ?",
                    tuple(parameters),
                )
                .fetchall()
            )
        return tuple(self._from_row(row) for row in rows)

    def delete(
        self,
        guild_id: int,
        user_id: int,
        memory_id: int,
        *,
        commit_allowed: CommitAllowed | None = None,
    ) -> bool:
        self._validate_owner(guild_id, user_id)
        if memory_id <= 0:
            raise ValueError("memory_id must be positive")
        connection = self._required()
        with self._lock, connection:
            self._require_commit_allowed(commit_allowed)
            cursor = connection.execute(
                "DELETE FROM personal_memory_items WHERE id=? AND guild_id=? AND user_id=?",
                (memory_id, guild_id, user_id),
            )
        return cursor.rowcount == 1

    def clear(
        self,
        guild_id: int,
        user_id: int,
        *,
        commit_allowed: CommitAllowed | None = None,
    ) -> int:
        self._validate_owner(guild_id, user_id)
        connection = self._required()
        with self._lock, connection:
            self._require_commit_allowed(commit_allowed)
            cursor = connection.execute(
                "DELETE FROM personal_memory_items WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            )
        return max(0, cursor.rowcount)

    def prune(
        self,
        *,
        now: int | None = None,
        commit_allowed: CommitAllowed | None = None,
    ) -> int:
        timestamp = int(time.time()) if now is None else now
        connection = self._required()
        with self._lock, connection:
            self._require_commit_allowed(commit_allowed)
            cursor = connection.execute("DELETE FROM personal_memory_items WHERE expires_at<=?", (timestamp,))
        return max(0, cursor.rowcount)

    def trim_owner(
        self,
        guild_id: int,
        user_id: int,
        *,
        maximum: int,
        commit_allowed: CommitAllowed | None = None,
    ) -> int:
        self._validate_owner(guild_id, user_id)
        if maximum < 1:
            raise ValueError("maximum must be positive")
        connection = self._required()
        with self._lock, connection:
            self._require_commit_allowed(commit_allowed)
            cursor = connection.execute(
                """DELETE FROM personal_memory_items
                WHERE guild_id=? AND user_id=? AND id NOT IN (
                    SELECT id FROM personal_memory_items
                    WHERE guild_id=? AND user_id=?
                    ORDER BY created_at DESC,id DESC LIMIT ?
                )""",
                (guild_id, user_id, guild_id, user_id, maximum),
            )
        return max(0, cursor.rowcount)

    def _required(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("personal memory repository is not open")
        return self._connection

    @staticmethod
    def _require_commit_allowed(commit_allowed: CommitAllowed | None) -> None:
        if commit_allowed is None:
            return
        try:
            allowed = commit_allowed()
        except Exception:
            allowed = False
        if allowed is not True:
            raise MemoryWriteRejectedError("personal memory write is no longer allowed")

    @staticmethod
    def _validate_owner(guild_id: int, user_id: int) -> None:
        if guild_id <= 0 or user_id <= 0:
            raise ValueError("guild_id and user_id must be positive")

    @staticmethod
    def _from_row(row: sqlite3.Row) -> MemoryItem:
        return MemoryItem(
            id=int(row["id"]),
            guild_id=int(row["guild_id"]),
            user_id=int(row["user_id"]),
            kind=MemoryKind(str(row["kind"])),
            content=str(row["content"]),
            created_at=int(row["created_at"]),
            expires_at=int(row["expires_at"]),
        )
