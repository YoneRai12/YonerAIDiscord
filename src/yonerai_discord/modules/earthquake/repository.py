from __future__ import annotations

import math
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable


MAX_DISCORD_ID = 9_223_372_036_854_775_807
MINIMUM_SCALES = frozenset({10, 20, 30, 40, 45, 50, 55, 60, 70})

SCHEMA = """
CREATE TABLE IF NOT EXISTS earthquake_guild_subscription (
    guild_id INTEGER PRIMARY KEY CHECK(guild_id BETWEEN 1 AND 9223372036854775807),
    channel_id INTEGER CHECK(channel_id BETWEEN 1 AND 9223372036854775807),
    min_scale INTEGER NOT NULL DEFAULT 40
        CHECK(min_scale IN (10, 20, 30, 40, 45, 50, 55, 60, 70)),
    notify_551 INTEGER NOT NULL DEFAULT 1 CHECK(notify_551 IN (0, 1)),
    notify_556 INTEGER NOT NULL DEFAULT 1 CHECK(notify_556 IN (0, 1)),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK(enabled = 0 OR channel_id IS NOT NULL),
    CHECK(enabled = 0 OR notify_551 = 1 OR notify_556 = 1)
);
CREATE INDEX IF NOT EXISTS earthquake_subscription_enabled_idx
ON earthquake_guild_subscription(enabled, guild_id);
CREATE TABLE IF NOT EXISTS earthquake_event_dedupe (
    event_id TEXT NOT NULL CHECK(length(event_id) BETWEEN 1 AND 256),
    payload_hash TEXT NOT NULL
        CHECK(length(payload_hash) = 64)
        CHECK(payload_hash = lower(payload_hash))
        CHECK(payload_hash NOT GLOB '*[^0-9a-f]*'),
    seen_at TEXT NOT NULL,
    PRIMARY KEY(event_id, payload_hash)
);
CREATE INDEX IF NOT EXISTS earthquake_event_dedupe_seen_idx
ON earthquake_event_dedupe(seen_at);
"""


@dataclass(frozen=True, slots=True)
class GuildSubscription:
    guild_id: int
    channel_id: int | None = None
    min_scale: int = 40
    notify_551: bool = True
    notify_556: bool = True
    enabled: bool = False

    def __post_init__(self) -> None:
        _discord_id(self.guild_id, "guild_id")
        if self.channel_id is not None:
            _discord_id(self.channel_id, "channel_id")
        if isinstance(self.min_scale, bool) or self.min_scale not in MINIMUM_SCALES:
            raise ValueError("min_scale is unsupported")
        if not isinstance(self.notify_551, bool) or not isinstance(self.notify_556, bool):
            raise TypeError("notification flags must be bool")
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be bool")
        if self.enabled and self.channel_id is None:
            raise ValueError("an enabled subscription requires channel_id")
        if self.enabled and not (self.notify_551 or self.notify_556):
            raise ValueError("an enabled subscription requires at least one event code")

    def accepts(self, code: int, max_scale: int | None) -> bool:
        if not self.enabled or max_scale is None or max_scale < self.min_scale:
            return False
        return (code == 551 and self.notify_551) or (code == 556 and self.notify_556)


class SqliteEarthquakeRepository:
    def __init__(
        self,
        path: Path,
        *,
        connect: Callable[..., sqlite3.Connection] = sqlite3.connect,
    ) -> None:
        self.path = Path(path)
        self._connect = connect
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    @property
    def is_open(self) -> bool:
        return self._connection is not None

    def open(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = self._connect(self.path, timeout=5, check_same_thread=False, isolation_level=None)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout = 5000")
                connection.execute("PRAGMA journal_mode = WAL")
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

    def get(self, guild_id: int) -> GuildSubscription:
        normalized_guild_id = _discord_id(guild_id, "guild_id")
        with self._lock:
            row = (
                self._required()
                .execute(
                    "SELECT * FROM earthquake_guild_subscription WHERE guild_id = ?",
                    (normalized_guild_id,),
                )
                .fetchone()
            )
        return GuildSubscription(normalized_guild_id) if row is None else self._from_row(row)

    def subscribe(
        self,
        guild_id: int,
        channel_id: int,
        *,
        min_scale: int = 40,
        notify_551: bool = True,
        notify_556: bool = True,
    ) -> GuildSubscription:
        subscription = GuildSubscription(
            guild_id=guild_id,
            channel_id=channel_id,
            min_scale=min_scale,
            notify_551=notify_551,
            notify_556=notify_556,
            enabled=True,
        )
        with self._lock:
            self._required().execute(
                """
                INSERT INTO earthquake_guild_subscription(
                    guild_id, channel_id, min_scale, notify_551, notify_556, enabled
                ) VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(guild_id) DO UPDATE SET
                    channel_id=excluded.channel_id,
                    min_scale=excluded.min_scale,
                    notify_551=excluded.notify_551,
                    notify_556=excluded.notify_556,
                    enabled=1,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (
                    subscription.guild_id,
                    subscription.channel_id,
                    subscription.min_scale,
                    int(subscription.notify_551),
                    int(subscription.notify_556),
                ),
            )
        return subscription

    def unsubscribe(self, guild_id: int) -> GuildSubscription:
        normalized_guild_id = _discord_id(guild_id, "guild_id")
        with self._lock:
            self._required().execute(
                """
                INSERT INTO earthquake_guild_subscription(guild_id, enabled)
                VALUES (?, 0)
                ON CONFLICT(guild_id) DO UPDATE SET
                    enabled=0,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (normalized_guild_id,),
            )
        return self.get(normalized_guild_id)

    def list_enabled(self) -> tuple[GuildSubscription, ...]:
        with self._lock:
            rows = (
                self._required()
                .execute("SELECT * FROM earthquake_guild_subscription WHERE enabled = 1 ORDER BY guild_id")
                .fetchall()
            )
        return tuple(self._from_row(row) for row in rows)

    def claim_event(
        self,
        event_id: str,
        payload_hash: str,
        *,
        seen_at: datetime,
        retention_seconds: float,
        capacity: int,
    ) -> bool:
        """同一id+hashを原子的に確保し、期限切れと古い超過行を同じtransactionで除去する。"""

        normalized_id = _event_id(event_id)
        normalized_hash = _payload_hash(payload_hash)
        normalized_seen_at = _aware_utc(seen_at)
        if isinstance(retention_seconds, bool) or not isinstance(retention_seconds, (int, float)):
            raise TypeError("retention_seconds must be a number")
        if not math.isfinite(float(retention_seconds)) or retention_seconds <= 0:
            raise ValueError("retention_seconds must be positive and finite")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or not 1 <= capacity <= 100_000:
            raise ValueError("capacity must be between 1 and 100000")

        connection = self._required()
        seen_text = normalized_seen_at.isoformat(timespec="microseconds")
        cutoff_text = (normalized_seen_at - timedelta(seconds=float(retention_seconds))).isoformat(
            timespec="microseconds"
        )
        with self._lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM earthquake_event_dedupe WHERE seen_at < ?", (cutoff_text,))
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO earthquake_event_dedupe(event_id, payload_hash, seen_at) VALUES (?, ?, ?)",
                    (normalized_id, normalized_hash, seen_text),
                )
                count = int(connection.execute("SELECT COUNT(*) FROM earthquake_event_dedupe").fetchone()[0])
                excess = max(0, count - capacity)
                if excess:
                    connection.execute(
                        """
                        DELETE FROM earthquake_event_dedupe WHERE rowid IN (
                            SELECT rowid FROM earthquake_event_dedupe
                            ORDER BY seen_at ASC, rowid ASC LIMIT ?
                        )
                        """,
                        (excess,),
                    )
                connection.commit()
                return cursor.rowcount == 1
            except BaseException:
                connection.rollback()
                raise

    def dedupe_count(self) -> int:
        with self._lock:
            return int(self._required().execute("SELECT COUNT(*) FROM earthquake_event_dedupe").fetchone()[0])

    def _required(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("earthquake repository is not open")
        return self._connection

    @staticmethod
    def _from_row(row: sqlite3.Row) -> GuildSubscription:
        return GuildSubscription(
            guild_id=int(row["guild_id"]),
            channel_id=int(row["channel_id"]) if row["channel_id"] is not None else None,
            min_scale=int(row["min_scale"]),
            notify_551=bool(row["notify_551"]),
            notify_556=bool(row["notify_556"]),
            enabled=bool(row["enabled"]),
        )


def _discord_id(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_DISCORD_ID:
        raise ValueError(f"{label} must be a positive Discord snowflake")
    return value


def _event_id(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("event_id must be a string")
    normalized = value.strip()
    if not 1 <= len(normalized) <= 256:
        raise ValueError("event_id must contain 1 to 256 characters")
    return normalized


def _payload_hash(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("payload_hash must be a string")
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError("payload_hash must be a SHA-256 hexadecimal digest")
    return normalized


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("seen_at must be timezone-aware")
    return value.astimezone(UTC)
