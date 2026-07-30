from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
import threading

from .domain import GuildServerConfig


SCHEMA = """
CREATE TABLE IF NOT EXISTS servertools_guild_config (
    guild_id INTEGER PRIMARY KEY,
    welcome_channel_id INTEGER,
    welcome_message TEXT,
    goodbye_channel_id INTEGER,
    goodbye_message TEXT,
    log_channel_id INTEGER,
    audit_include_content INTEGER NOT NULL DEFAULT 0 CHECK(audit_include_content IN (0, 1)),
    audit_content_limit INTEGER NOT NULL DEFAULT 200 CHECK(audit_content_limit BETWEEN 1 AND 500),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class SqliteServerToolsRepository:
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

    def get(self, guild_id: int) -> GuildServerConfig:
        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        with self._lock:
            row = (
                self._required()
                .execute("SELECT * FROM servertools_guild_config WHERE guild_id = ?", (guild_id,))
                .fetchone()
            )
        return self._from_row(row) if row is not None else GuildServerConfig(guild_id)

    def save(self, config: GuildServerConfig) -> None:
        connection = self._required()
        with self._lock, connection:
            connection.execute(
                """INSERT INTO servertools_guild_config (
                    guild_id, welcome_channel_id, welcome_message, goodbye_channel_id,
                    goodbye_message, log_channel_id, audit_include_content, audit_content_limit
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    welcome_channel_id=excluded.welcome_channel_id,
                    welcome_message=excluded.welcome_message,
                    goodbye_channel_id=excluded.goodbye_channel_id,
                    goodbye_message=excluded.goodbye_message,
                    log_channel_id=excluded.log_channel_id,
                    audit_include_content=excluded.audit_include_content,
                    audit_content_limit=excluded.audit_content_limit,
                    updated_at=CURRENT_TIMESTAMP""",
                (
                    config.guild_id,
                    config.welcome_channel_id,
                    config.welcome_message,
                    config.goodbye_channel_id,
                    config.goodbye_message,
                    config.log_channel_id,
                    int(config.audit_include_content),
                    config.audit_content_limit,
                ),
            )

    def set_welcome(self, guild_id: int, channel_id: int, message: str) -> GuildServerConfig:
        return self._update(guild_id, welcome_channel_id=channel_id, welcome_message=message)

    def set_goodbye(self, guild_id: int, channel_id: int, message: str) -> GuildServerConfig:
        return self._update(guild_id, goodbye_channel_id=channel_id, goodbye_message=message)

    def set_log_channel(self, guild_id: int, channel_id: int) -> GuildServerConfig:
        return self._update(guild_id, log_channel_id=channel_id)

    def set_audit_content(self, guild_id: int, *, include: bool, limit: int = 200) -> GuildServerConfig:
        return self._update(guild_id, audit_include_content=include, audit_content_limit=limit)

    def _update(self, guild_id: int, **changes: object) -> GuildServerConfig:
        config = replace(self.get(guild_id), **changes)
        self.save(config)
        return config

    def _required(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("servertools repository is not open")
        return self._connection

    @staticmethod
    def _from_row(row: sqlite3.Row) -> GuildServerConfig:
        return GuildServerConfig(
            guild_id=int(row["guild_id"]),
            welcome_channel_id=row["welcome_channel_id"],
            welcome_message=row["welcome_message"],
            goodbye_channel_id=row["goodbye_channel_id"],
            goodbye_message=row["goodbye_message"],
            log_channel_id=row["log_channel_id"],
            audit_include_content=bool(row["audit_include_content"]),
            audit_content_limit=int(row["audit_content_limit"]),
        )
