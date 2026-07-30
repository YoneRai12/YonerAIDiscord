from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
import threading

from .domain import MAX_SQLITE_SNOWFLAKE, AutomodMode, GuildAutomodConfig


SCHEMA = """
CREATE TABLE IF NOT EXISTS automod_guild_config (
    guild_id INTEGER PRIMARY KEY CHECK(guild_id > 0),
    report_channel_id INTEGER CHECK(report_channel_id IS NULL OR report_channel_id > 0),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
    mode TEXT NOT NULL DEFAULT 'report_only' CHECK(mode = 'report_only'),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(enabled = 0 OR report_channel_id IS NOT NULL)
);
"""


class SqliteAutomodRepository:
    """本文・検出履歴・メンバーのstrikeを保存しない設定専用repository。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
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
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA busy_timeout = 5000")
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA foreign_keys = ON")
                connection.executescript(SCHEMA)
            except BaseException:
                connection.close()
                raise
            self._connection = connection

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def get(self, guild_id: int) -> GuildAutomodConfig:
        _positive_id(guild_id, "guild_id")
        with self._lock:
            row = (
                self._required()
                .execute(
                    "SELECT guild_id,report_channel_id,enabled,mode FROM automod_guild_config WHERE guild_id=?",
                    (guild_id,),
                )
                .fetchone()
            )
        if row is None:
            return GuildAutomodConfig(guild_id=guild_id)
        return GuildAutomodConfig(
            guild_id=int(row["guild_id"]),
            report_channel_id=(int(row["report_channel_id"]) if row["report_channel_id"] is not None else None),
            enabled=bool(row["enabled"]),
            mode=AutomodMode(str(row["mode"])),
        )

    def set_report_channel(self, guild_id: int, channel_id: int | None) -> GuildAutomodConfig:
        _positive_id(guild_id, "guild_id")
        if channel_id is not None:
            _positive_id(channel_id, "channel_id")
        current = self.get(guild_id)
        # 送信先を解除した状態でenabledだけが残ることは許さない。
        updated = replace(
            current,
            report_channel_id=channel_id,
            enabled=current.enabled and channel_id is not None,
        )
        self.save(updated)
        return updated

    def set_enabled(self, guild_id: int, enabled: bool) -> GuildAutomodConfig:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be bool")
        current = self.get(guild_id)
        updated = replace(current, enabled=enabled)
        self.save(updated)
        return updated

    def save(self, config: GuildAutomodConfig) -> None:
        if not isinstance(config, GuildAutomodConfig):
            raise TypeError("config must be GuildAutomodConfig")
        with self._lock, self._required() as connection:
            connection.execute(
                """INSERT INTO automod_guild_config (
                    guild_id,report_channel_id,enabled,mode
                ) VALUES (?,?,?,?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    report_channel_id=excluded.report_channel_id,
                    enabled=excluded.enabled,
                    mode=excluded.mode,
                    updated_at=CURRENT_TIMESTAMP""",
                (
                    config.guild_id,
                    config.report_channel_id,
                    int(config.enabled),
                    config.mode.value,
                ),
            )

    def _required(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("automod repository is not open")
        return self._connection


def _positive_id(value: int, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= MAX_SQLITE_SNOWFLAKE:
        raise ValueError(f"{label} must be a positive integer")
