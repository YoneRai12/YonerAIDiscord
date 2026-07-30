from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Iterator


PRESET_SCHEMA_VERSION = 1
DEFAULT_SPEED_MILLI = 1_000
DEFAULT_VOLUME_MILLI = 1_000
MIN_SPEED_MILLI = 500
MAX_SPEED_MILLI = 2_000
MIN_VOLUME_MILLI = 0
MAX_VOLUME_MILLI = 2_000
_MAX_DISCORD_ID = 9_223_372_036_854_775_807

_SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS voice_read_aloud_preset_schema (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    schema_version INTEGER NOT NULL CHECK(schema_version > 0)
)
"""

_DATA_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS voice_read_aloud_server_presets (
        guild_id INTEGER PRIMARY KEY
            CHECK(guild_id BETWEEN 1 AND 9223372036854775807),
        speed_milli INTEGER NOT NULL CHECK(speed_milli BETWEEN 500 AND 2000),
        volume_milli INTEGER NOT NULL CHECK(volume_milli BETWEEN 0 AND 2000),
        revision INTEGER NOT NULL CHECK(revision > 0),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS voice_read_aloud_user_presets (
        guild_id INTEGER NOT NULL
            CHECK(guild_id BETWEEN 1 AND 9223372036854775807),
        user_id INTEGER NOT NULL
            CHECK(user_id BETWEEN 1 AND 9223372036854775807),
        speed_milli INTEGER NOT NULL CHECK(speed_milli BETWEEN 500 AND 2000),
        volume_milli INTEGER NOT NULL CHECK(volume_milli BETWEEN 0 AND 2000),
        revision INTEGER NOT NULL CHECK(revision > 0),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        PRIMARY KEY(guild_id, user_id)
    )
    """,
)


class VoicePresetRepositoryError(RuntimeError):
    """Persistent preset state could not be interpreted safely."""


class VoicePresetScope(StrEnum):
    DEFAULT = "default"
    SERVER = "server"
    USER = "user"


@dataclass(frozen=True, slots=True)
class VoicePresetValues:
    speed_milli: int = DEFAULT_SPEED_MILLI
    volume_milli: int = DEFAULT_VOLUME_MILLI

    def __post_init__(self) -> None:
        _bounded_milli(
            self.speed_milli,
            "speed_milli",
            minimum=MIN_SPEED_MILLI,
            maximum=MAX_SPEED_MILLI,
        )
        _bounded_milli(
            self.volume_milli,
            "volume_milli",
            minimum=MIN_VOLUME_MILLI,
            maximum=MAX_VOLUME_MILLI,
        )

    @property
    def speed_scale(self) -> float:
        return self.speed_milli / 1_000

    @property
    def volume_scale(self) -> float:
        return self.volume_milli / 1_000


@dataclass(frozen=True, slots=True)
class VoicePresetRecord:
    guild_id: int = field(repr=False)
    user_id: int | None = field(default=None, repr=False)
    values: VoicePresetValues = field(default_factory=VoicePresetValues)
    revision: int = 1

    def __post_init__(self) -> None:
        _discord_id(self.guild_id, "guild_id")
        if self.user_id is not None:
            _discord_id(self.user_id, "user_id")
        if not isinstance(self.values, VoicePresetValues):
            raise TypeError("values must be VoicePresetValues")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("revision must be positive")


@dataclass(frozen=True, slots=True)
class ResolvedVoicePreset:
    guild_id: int = field(repr=False)
    user_id: int = field(repr=False)
    values: VoicePresetValues = field(default_factory=VoicePresetValues)
    source: VoicePresetScope = VoicePresetScope.DEFAULT
    revision: int = 0

    def __post_init__(self) -> None:
        _discord_id(self.guild_id, "guild_id")
        _discord_id(self.user_id, "user_id")
        if not isinstance(self.values, VoicePresetValues):
            raise TypeError("values must be VoicePresetValues")
        if not isinstance(self.source, VoicePresetScope):
            raise TypeError("source must be VoicePresetScope")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revision must be non-negative")
        if (self.source is VoicePresetScope.DEFAULT) != (self.revision == 0):
            raise ValueError("preset source and revision do not match")

    @property
    def batch_key(self) -> tuple[VoicePresetScope, int, int | None, int, int, int]:
        return (
            self.source,
            self.guild_id,
            self.user_id if self.source is VoicePresetScope.USER else None,
            self.revision,
            self.values.speed_milli,
            self.values.volume_milli,
        )


class SqliteVoicePresetRepository:
    """Guild/user scoped speed and volume presets without message content."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
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
            connection = sqlite3.connect(
                self.path,
                timeout=5,
                check_same_thread=False,
                isolation_level=None,
            )
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout = 5000")
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(_SCHEMA_TABLE)
                row = connection.execute(
                    """
                    SELECT schema_version
                    FROM voice_read_aloud_preset_schema
                    WHERE singleton = 1
                    """
                ).fetchone()
                if row is None:
                    for statement in _DATA_SCHEMA:
                        connection.execute(statement)
                    connection.execute(
                        """
                        INSERT INTO voice_read_aloud_preset_schema(singleton, schema_version)
                        VALUES (1, ?)
                        """,
                        (PRESET_SCHEMA_VERSION,),
                    )
                else:
                    try:
                        version = _strict_stored_int(row["schema_version"])
                    except ValueError as exc:
                        raise VoicePresetRepositoryError("preset_schema_version_corrupt") from exc
                    if version != PRESET_SCHEMA_VERSION:
                        raise VoicePresetRepositoryError("preset_schema_version_unsupported")
                    for statement in _DATA_SCHEMA:
                        connection.execute(statement)
                connection.commit()
            except BaseException:
                connection.rollback()
                connection.close()
                raise
            self._connection = connection

    def close(self) -> None:
        with self._lock:
            connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

    def get_server(self, guild_id: int) -> VoicePresetRecord | None:
        guild_id = _discord_id(guild_id, "guild_id")
        with self._lock:
            row = (
                self._required()
                .execute(
                    """
                SELECT guild_id, speed_milli, volume_milli, revision
                FROM voice_read_aloud_server_presets
                WHERE guild_id = ?
                """,
                    (guild_id,),
                )
                .fetchone()
            )
            return None if row is None else _decode_record(row, user_id=None)

    def get_user(self, guild_id: int, user_id: int) -> VoicePresetRecord | None:
        guild_id = _discord_id(guild_id, "guild_id")
        user_id = _discord_id(user_id, "user_id")
        with self._lock:
            row = (
                self._required()
                .execute(
                    """
                SELECT guild_id, user_id, speed_milli, volume_milli, revision
                FROM voice_read_aloud_user_presets
                WHERE guild_id = ? AND user_id = ?
                """,
                    (guild_id, user_id),
                )
                .fetchone()
            )
            return None if row is None else _decode_record(row, user_id=row["user_id"])

    def resolve(self, guild_id: int, user_id: int) -> ResolvedVoicePreset:
        guild_id = _discord_id(guild_id, "guild_id")
        user_id = _discord_id(user_id, "user_id")
        with self._lock:
            connection = self._required()
            user_row = connection.execute(
                """
                SELECT guild_id, user_id, speed_milli, volume_milli, revision
                FROM voice_read_aloud_user_presets
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()
            if user_row is not None:
                record = _decode_record(user_row, user_id=user_row["user_id"])
                return ResolvedVoicePreset(
                    guild_id=guild_id,
                    user_id=user_id,
                    values=record.values,
                    source=VoicePresetScope.USER,
                    revision=record.revision,
                )
            server_row = connection.execute(
                """
                SELECT guild_id, speed_milli, volume_milli, revision
                FROM voice_read_aloud_server_presets
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
            if server_row is not None:
                record = _decode_record(server_row, user_id=None)
                return ResolvedVoicePreset(
                    guild_id=guild_id,
                    user_id=user_id,
                    values=record.values,
                    source=VoicePresetScope.SERVER,
                    revision=record.revision,
                )
            return ResolvedVoicePreset(guild_id=guild_id, user_id=user_id)

    def set_server(
        self,
        guild_id: int,
        values: VoicePresetValues,
        *,
        expected_revision: int,
    ) -> VoicePresetRecord:
        guild_id = _discord_id(guild_id, "guild_id")
        values = _preset_values(values)
        expected_revision = _expected_revision(expected_revision)
        with self._lock, self._transaction() as connection:
            row = connection.execute(
                """
                SELECT guild_id, speed_milli, volume_milli, revision
                FROM voice_read_aloud_server_presets
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
            current = None if row is None else _decode_record(row, user_id=None)
            revision = _next_revision(current, expected_revision)
            connection.execute(
                """
                INSERT INTO voice_read_aloud_server_presets(
                    guild_id, speed_milli, volume_milli, revision
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    speed_milli=excluded.speed_milli,
                    volume_milli=excluded.volume_milli,
                    revision=excluded.revision,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (guild_id, values.speed_milli, values.volume_milli, revision),
            )
        return VoicePresetRecord(guild_id=guild_id, values=values, revision=revision)

    def set_user(
        self,
        guild_id: int,
        user_id: int,
        values: VoicePresetValues,
        *,
        expected_revision: int,
    ) -> VoicePresetRecord:
        guild_id = _discord_id(guild_id, "guild_id")
        user_id = _discord_id(user_id, "user_id")
        values = _preset_values(values)
        expected_revision = _expected_revision(expected_revision)
        with self._lock, self._transaction() as connection:
            row = connection.execute(
                """
                SELECT guild_id, user_id, speed_milli, volume_milli, revision
                FROM voice_read_aloud_user_presets
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()
            current = None if row is None else _decode_record(row, user_id=row["user_id"])
            revision = _next_revision(current, expected_revision)
            connection.execute(
                """
                INSERT INTO voice_read_aloud_user_presets(
                    guild_id, user_id, speed_milli, volume_milli, revision
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    speed_milli=excluded.speed_milli,
                    volume_milli=excluded.volume_milli,
                    revision=excluded.revision,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (guild_id, user_id, values.speed_milli, values.volume_milli, revision),
            )
        return VoicePresetRecord(
            guild_id=guild_id,
            user_id=user_id,
            values=values,
            revision=revision,
        )

    def clear_server(self, guild_id: int, *, expected_revision: int) -> bool:
        guild_id = _discord_id(guild_id, "guild_id")
        expected_revision = _expected_revision(expected_revision)
        with self._lock, self._transaction() as connection:
            row = connection.execute(
                """
                SELECT guild_id, speed_milli, volume_milli, revision
                FROM voice_read_aloud_server_presets
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
            current = None if row is None else _decode_record(row, user_id=None)
            _require_expected_revision(current, expected_revision)
            if current is None:
                return False
            cursor = connection.execute(
                """
                DELETE FROM voice_read_aloud_server_presets
                WHERE guild_id = ? AND revision = ?
                """,
                (guild_id, current.revision),
            )
            if cursor.rowcount != 1:
                raise VoicePresetRepositoryError("preset_revision_conflict")
            return True

    def clear_user(self, guild_id: int, user_id: int, *, expected_revision: int) -> bool:
        guild_id = _discord_id(guild_id, "guild_id")
        user_id = _discord_id(user_id, "user_id")
        expected_revision = _expected_revision(expected_revision)
        with self._lock, self._transaction() as connection:
            row = connection.execute(
                """
                SELECT guild_id, user_id, speed_milli, volume_milli, revision
                FROM voice_read_aloud_user_presets
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()
            current = None if row is None else _decode_record(row, user_id=row["user_id"])
            _require_expected_revision(current, expected_revision)
            if current is None:
                return False
            cursor = connection.execute(
                """
                DELETE FROM voice_read_aloud_user_presets
                WHERE guild_id = ? AND user_id = ? AND revision = ?
                """,
                (guild_id, user_id, current.revision),
            )
            if cursor.rowcount != 1:
                raise VoicePresetRepositoryError("preset_revision_conflict")
            return True

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._required()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    def _required(self) -> sqlite3.Connection:
        if self._connection is None:
            raise VoicePresetRepositoryError("preset_repository_closed")
        return self._connection


def _decode_record(row: sqlite3.Row, *, user_id: object | None) -> VoicePresetRecord:
    try:
        decoded_user_id = None if user_id is None else _strict_stored_int(user_id)
        return VoicePresetRecord(
            guild_id=_strict_stored_int(row["guild_id"]),
            user_id=decoded_user_id,
            values=VoicePresetValues(
                speed_milli=_strict_stored_int(row["speed_milli"]),
                volume_milli=_strict_stored_int(row["volume_milli"]),
            ),
            revision=_strict_stored_int(row["revision"]),
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise VoicePresetRepositoryError("preset_corrupt") from exc


def _next_revision(current: VoicePresetRecord | None, expected_revision: int) -> int:
    _require_expected_revision(current, expected_revision)
    return 1 if current is None else current.revision + 1


def _require_expected_revision(
    current: VoicePresetRecord | None,
    expected_revision: int,
) -> None:
    current_revision = 0 if current is None else current.revision
    if current_revision != expected_revision:
        raise VoicePresetRepositoryError("preset_revision_conflict")


def _preset_values(value: VoicePresetValues) -> VoicePresetValues:
    if not isinstance(value, VoicePresetValues):
        raise TypeError("values must be VoicePresetValues")
    return value


def _expected_revision(value: int) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("expected_revision must be non-negative")
    return value


def _bounded_milli(value: int, name: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} is out of bounds")
    return value


def _strict_stored_int(value: object) -> int:
    if type(value) is not int:
        raise ValueError("stored integer is invalid")
    return value


def _discord_id(value: int, name: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_DISCORD_ID:
        raise ValueError(f"{name} must be a valid Discord ID")
    return value


DEFAULT_VOICE_PRESET = VoicePresetValues()


__all__ = [
    "DEFAULT_SPEED_MILLI",
    "DEFAULT_VOICE_PRESET",
    "DEFAULT_VOLUME_MILLI",
    "MAX_SPEED_MILLI",
    "MAX_VOLUME_MILLI",
    "MIN_SPEED_MILLI",
    "MIN_VOLUME_MILLI",
    "PRESET_SCHEMA_VERSION",
    "ResolvedVoicePreset",
    "SqliteVoicePresetRepository",
    "VoicePresetRecord",
    "VoicePresetRepositoryError",
    "VoicePresetScope",
    "VoicePresetValues",
]
