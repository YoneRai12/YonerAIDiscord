from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping

from .domain import ModAction, ModerationCase, Warning


SCHEMA = """
CREATE TABLE IF NOT EXISTS modtools_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    target_id INTEGER,
    moderator_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS modtools_cases_guild_idx
ON modtools_cases(guild_id, id DESC);
CREATE TABLE IF NOT EXISTS modtools_warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL UNIQUE REFERENCES modtools_cases(id),
    guild_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS modtools_warnings_target_idx
ON modtools_warnings(guild_id, target_id, active, id DESC);
"""


class ModtoolsRepository:
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
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            self._connection = connection

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def record_case(
        self,
        *,
        guild_id: int,
        action: ModAction,
        target_id: int | None,
        moderator_id: int,
        reason: str,
        status: str = "completed",
        metadata: Mapping[str, object] | None = None,
        created_at: datetime | None = None,
    ) -> ModerationCase:
        connection = self._required()
        now = (created_at or datetime.now(UTC)).astimezone(UTC)
        metadata_value = dict(metadata or {})
        with self._lock:
            cursor = connection.execute(
                """INSERT INTO modtools_cases
                (guild_id, action, target_id, moderator_id, reason, status, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    guild_id,
                    action.value,
                    target_id,
                    moderator_id,
                    reason,
                    status,
                    json.dumps(metadata_value, ensure_ascii=False, sort_keys=True, default=str),
                    now.isoformat(),
                ),
            )
        return ModerationCase(
            id=int(cursor.lastrowid),
            guild_id=guild_id,
            action=action,
            target_id=target_id,
            moderator_id=moderator_id,
            reason=reason,
            status=status,
            metadata=metadata_value,
            created_at=now,
        )

    def add_warning(
        self,
        *,
        guild_id: int,
        target_id: int,
        moderator_id: int,
        reason: str,
        created_at: datetime | None = None,
    ) -> Warning:
        connection = self._required()
        now = (created_at or datetime.now(UTC)).astimezone(UTC)
        with self._lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                case_cursor = connection.execute(
                    """INSERT INTO modtools_cases
                    (guild_id, action, target_id, moderator_id, reason, status, metadata_json, created_at)
                    VALUES (?, 'warn', ?, ?, ?, 'completed', '{}', ?)""",
                    (guild_id, target_id, moderator_id, reason, now.isoformat()),
                )
                case_id = int(case_cursor.lastrowid)
                warning_cursor = connection.execute(
                    """INSERT INTO modtools_warnings
                    (case_id, guild_id, target_id, moderator_id, reason, active, created_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?)""",
                    (case_id, guild_id, target_id, moderator_id, reason, now.isoformat()),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return Warning(
            id=int(warning_cursor.lastrowid),
            case_id=case_id,
            guild_id=guild_id,
            target_id=target_id,
            moderator_id=moderator_id,
            reason=reason,
            active=True,
            created_at=now,
        )

    def warnings_for(self, guild_id: int, target_id: int, *, active_only: bool = True) -> tuple[Warning, ...]:
        sql = """SELECT * FROM modtools_warnings WHERE guild_id=? AND target_id=?"""
        params: list[object] = [guild_id, target_id]
        if active_only:
            sql += " AND active=1"
        sql += " ORDER BY id DESC"
        rows = self._required().execute(sql, params).fetchall()
        return tuple(self._warning(row) for row in rows)

    def get_case(self, guild_id: int, case_id: int) -> ModerationCase | None:
        row = (
            self._required()
            .execute(
                "SELECT * FROM modtools_cases WHERE guild_id=? AND id=?",
                (guild_id, case_id),
            )
            .fetchone()
        )
        if row is None:
            return None
        return ModerationCase(
            id=int(row["id"]),
            guild_id=int(row["guild_id"]),
            action=ModAction(str(row["action"])),
            target_id=int(row["target_id"]) if row["target_id"] is not None else None,
            moderator_id=int(row["moderator_id"]),
            reason=str(row["reason"]),
            status=str(row["status"]),
            metadata=json.loads(str(row["metadata_json"])),
            created_at=datetime.fromisoformat(str(row["created_at"])).astimezone(UTC),
        )

    def update_case(
        self,
        guild_id: int,
        case_id: int,
        *,
        status: str,
        metadata: Mapping[str, object] | None = None,
    ) -> bool:
        connection = self._required()
        with self._lock:
            if metadata is None:
                cursor = connection.execute(
                    "UPDATE modtools_cases SET status=? WHERE guild_id=? AND id=?",
                    (status, guild_id, case_id),
                )
            else:
                cursor = connection.execute(
                    "UPDATE modtools_cases SET status=?, metadata_json=? WHERE guild_id=? AND id=?",
                    (
                        status,
                        json.dumps(dict(metadata), ensure_ascii=False, sort_keys=True, default=str),
                        guild_id,
                        case_id,
                    ),
                )
            return cursor.rowcount == 1

    @staticmethod
    def _warning(row: sqlite3.Row) -> Warning:
        return Warning(
            id=int(row["id"]),
            case_id=int(row["case_id"]),
            guild_id=int(row["guild_id"]),
            target_id=int(row["target_id"]),
            moderator_id=int(row["moderator_id"]),
            reason=str(row["reason"]),
            active=bool(row["active"]),
            created_at=datetime.fromisoformat(str(row["created_at"])).astimezone(UTC),
        )

    def _required(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("modtools repository is not open")
        return self._connection
