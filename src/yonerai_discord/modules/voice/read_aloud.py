from __future__ import annotations

import asyncio
import hashlib
import inspect
import sqlite3
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Iterator

from yonerai_discord.voice_contract import VOICEVOX_SPEAKER_ID

from .models import SpeechRequest, SynthesizedSpeech, normalize_speech_text
from .presets import ResolvedVoicePreset


_SCHEMA_VERSION = 2
_MAX_DISCORD_ID = 9_223_372_036_854_775_807
_MAX_POLICY_ITEMS = 64
_MAX_POLICY_TEXT_LENGTH = 64
_CONTENT_DEDUPE_TTL_SECONDS = 30.0
_CONTENT_DEDUPE_LIMIT = 512
_MAX_PENDING_ITEMS_PER_AUTHOR = 2
_SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_read_aloud_schema (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    schema_version INTEGER NOT NULL CHECK(schema_version > 0)
);

CREATE TABLE IF NOT EXISTS voice_read_aloud_routes (
    guild_id INTEGER NOT NULL CHECK(guild_id BETWEEN 1 AND 9223372036854775807),
    source_channel_id INTEGER NOT NULL CHECK(source_channel_id BETWEEN 1 AND 9223372036854775807),
    destination_voice_channel_id INTEGER NOT NULL
        CHECK(destination_voice_channel_id BETWEEN 1 AND 9223372036854775807),
    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
    speaker_id INTEGER NOT NULL CHECK(speaker_id = 3),
    revision INTEGER NOT NULL CHECK(revision > 0),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY(guild_id, source_channel_id)
);

CREATE INDEX IF NOT EXISTS voice_read_aloud_routes_guild_idx
ON voice_read_aloud_routes(guild_id, source_channel_id);

CREATE TABLE IF NOT EXISTS voice_read_aloud_route_quarantine (
    quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER,
    source_channel_id INTEGER,
    row_sha256 TEXT NOT NULL
        CHECK(length(row_sha256) = 64)
        CHECK(row_sha256 = lower(row_sha256)),
    reason_code TEXT NOT NULL,
    quarantined_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS voice_read_aloud_policy_revisions (
    guild_id INTEGER PRIMARY KEY CHECK(guild_id BETWEEN 1 AND 9223372036854775807),
    revision INTEGER NOT NULL CHECK(revision > 0),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS voice_read_aloud_dictionary (
    guild_id INTEGER NOT NULL CHECK(guild_id BETWEEN 1 AND 9223372036854775807),
    term TEXT NOT NULL CHECK(length(term) BETWEEN 1 AND 64),
    pronunciation TEXT NOT NULL CHECK(length(pronunciation) BETWEEN 1 AND 64),
    PRIMARY KEY(guild_id, term),
    FOREIGN KEY(guild_id) REFERENCES voice_read_aloud_policy_revisions(guild_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS voice_read_aloud_exclusions (
    guild_id INTEGER NOT NULL CHECK(guild_id BETWEEN 1 AND 9223372036854775807),
    phrase TEXT NOT NULL CHECK(length(phrase) BETWEEN 1 AND 64),
    PRIMARY KEY(guild_id, phrase),
    FOREIGN KEY(guild_id) REFERENCES voice_read_aloud_policy_revisions(guild_id)
        ON DELETE CASCADE
);
"""


class ReadAloudRepositoryError(RuntimeError):
    """永続routeを安全に解釈できない場合のcontent-free error。"""


class ReadAloudRejectedError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ReadAloudRoute:
    guild_id: int
    source_channel_id: int
    destination_voice_channel_id: int
    enabled: bool
    speaker_id: int = VOICEVOX_SPEAKER_ID
    revision: int = 1

    def __post_init__(self) -> None:
        _discord_id(self.guild_id, "guild_id")
        _discord_id(self.source_channel_id, "source_channel_id")
        _discord_id(self.destination_voice_channel_id, "destination_voice_channel_id")
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be bool")
        if self.speaker_id != VOICEVOX_SPEAKER_ID:
            raise ValueError("speaker_id is fixed")
        if isinstance(self.revision, bool) or self.revision < 1:
            raise ValueError("revision must be positive")


@dataclass(frozen=True, slots=True)
class ReadAloudDictionaryEntry:
    term: str = field(repr=False)
    pronunciation: str = field(repr=False)

    def __post_init__(self) -> None:
        if _normalize_policy_text(self.term, "term") != self.term:
            raise ValueError("term must be normalized")
        if _normalize_policy_text(self.pronunciation, "pronunciation") != self.pronunciation:
            raise ValueError("pronunciation must be normalized")


@dataclass(frozen=True, slots=True)
class ReadAloudPolicySnapshot:
    guild_id: int
    revision: int
    dictionary: tuple[ReadAloudDictionaryEntry, ...] = field(default=(), repr=False)
    exclusions: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        _discord_id(self.guild_id, "guild_id")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("policy revision must be non-negative")
        if not isinstance(self.dictionary, tuple) or not isinstance(self.exclusions, tuple):
            raise TypeError("policy collections must be tuples")
        if len(self.dictionary) > _MAX_POLICY_ITEMS or len(self.exclusions) > _MAX_POLICY_ITEMS:
            raise ValueError("policy item limit exceeded")
        if any(not isinstance(item, ReadAloudDictionaryEntry) for item in self.dictionary):
            raise TypeError("dictionary entries are invalid")
        if len({item.term for item in self.dictionary}) != len(self.dictionary):
            raise ValueError("dictionary terms must be unique")
        if any(_normalize_policy_text(value, "phrase") != value for value in self.exclusions):
            raise ValueError("exclusion phrases must be normalized")
        if len(set(self.exclusions)) != len(self.exclusions):
            raise ValueError("exclusion phrases must be unique")


class SqliteReadAloudRouteRepository:
    """本文を一切保持しないguild/source-channel scoped route台帳。"""

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
                connection.execute("PRAGMA foreign_keys = ON")
                connection.executescript(_SCHEMA)
                connection.execute("BEGIN IMMEDIATE")
                version_row = connection.execute(
                    "SELECT schema_version FROM voice_read_aloud_schema WHERE singleton = 1"
                ).fetchone()
                if version_row is None:
                    connection.execute(
                        "INSERT INTO voice_read_aloud_schema(singleton, schema_version) VALUES (1, ?)",
                        (_SCHEMA_VERSION,),
                    )
                else:
                    try:
                        schema_version = _strict_stored_int(version_row["schema_version"])
                    except ValueError as exc:
                        raise ReadAloudRepositoryError("schema_version_corrupt") from exc
                    if schema_version == 1:
                        connection.execute(
                            "UPDATE voice_read_aloud_schema SET schema_version = ? WHERE singleton = 1",
                            (_SCHEMA_VERSION,),
                        )
                    elif schema_version != _SCHEMA_VERSION:
                        raise ReadAloudRepositoryError("schema_version_unsupported")
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

    def put(
        self,
        *,
        guild_id: int,
        source_channel_id: int,
        destination_voice_channel_id: int,
        enabled: bool,
        expected_revision: int | None = None,
    ) -> ReadAloudRoute:
        guild_id = _discord_id(guild_id, "guild_id")
        source_channel_id = _discord_id(source_channel_id, "source_channel_id")
        destination_voice_channel_id = _discord_id(
            destination_voice_channel_id,
            "destination_voice_channel_id",
        )
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be bool")
        if expected_revision is not None and (isinstance(expected_revision, bool) or expected_revision < 1):
            raise ValueError("expected_revision must be positive")

        with self._lock, self._transaction() as connection:
            row = connection.execute(
                """
                SELECT revision FROM voice_read_aloud_routes
                WHERE guild_id = ? AND source_channel_id = ?
                """,
                (guild_id, source_channel_id),
            ).fetchone()
            if row is None:
                current_revision = None
            else:
                try:
                    current_revision = _strict_stored_int(row["revision"])
                except ValueError as exc:
                    raise ReadAloudRepositoryError("route_revision_corrupt") from exc
            if expected_revision != current_revision and (
                expected_revision is not None or current_revision is not None
            ):
                raise ReadAloudRepositoryError("route_revision_conflict")
            revision = 1 if current_revision is None else current_revision + 1
            connection.execute(
                """
                INSERT INTO voice_read_aloud_routes(
                    guild_id, source_channel_id, destination_voice_channel_id,
                    enabled, speaker_id, revision
                ) VALUES (?, ?, ?, ?, 3, ?)
                ON CONFLICT(guild_id, source_channel_id) DO UPDATE SET
                    destination_voice_channel_id=excluded.destination_voice_channel_id,
                    enabled=excluded.enabled,
                    speaker_id=3,
                    revision=excluded.revision,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (
                    guild_id,
                    source_channel_id,
                    destination_voice_channel_id,
                    int(enabled),
                    revision,
                ),
            )
        return ReadAloudRoute(
            guild_id=guild_id,
            source_channel_id=source_channel_id,
            destination_voice_channel_id=destination_voice_channel_id,
            enabled=enabled,
            revision=revision,
        )

    def get(self, guild_id: int, source_channel_id: int) -> ReadAloudRoute | None:
        guild_id = _discord_id(guild_id, "guild_id")
        source_channel_id = _discord_id(source_channel_id, "source_channel_id")
        with self._lock:
            row = (
                self._required()
                .execute(
                    """
                    SELECT guild_id, source_channel_id, destination_voice_channel_id,
                           enabled, speaker_id, revision
                    FROM voice_read_aloud_routes
                    WHERE guild_id = ? AND source_channel_id = ?
                    """,
                    (guild_id, source_channel_id),
                )
                .fetchone()
            )
            return None if row is None else self._decode_or_quarantine(row)

    def list_for_guild(self, guild_id: int) -> tuple[ReadAloudRoute, ...]:
        guild_id = _discord_id(guild_id, "guild_id")
        with self._lock:
            rows = (
                self._required()
                .execute(
                    """
                    SELECT guild_id, source_channel_id, destination_voice_channel_id,
                           enabled, speaker_id, revision
                    FROM voice_read_aloud_routes
                    WHERE guild_id = ?
                    ORDER BY source_channel_id
                    """,
                    (guild_id,),
                )
                .fetchall()
            )
            values: list[ReadAloudRoute] = []
            for row in rows:
                route = self._decode_or_quarantine(row)
                if route is not None:
                    values.append(route)
            return tuple(values)

    def delete(
        self,
        guild_id: int,
        source_channel_id: int,
        *,
        expected_revision: int | None = None,
    ) -> bool:
        guild_id = _discord_id(guild_id, "guild_id")
        source_channel_id = _discord_id(source_channel_id, "source_channel_id")
        if expected_revision is not None and (isinstance(expected_revision, bool) or expected_revision < 1):
            raise ValueError("expected_revision must be positive")
        with self._lock, self._transaction() as connection:
            if expected_revision is None:
                cursor = connection.execute(
                    "DELETE FROM voice_read_aloud_routes WHERE guild_id = ? AND source_channel_id = ?",
                    (guild_id, source_channel_id),
                )
            else:
                cursor = connection.execute(
                    """
                    DELETE FROM voice_read_aloud_routes
                    WHERE guild_id = ? AND source_channel_id = ? AND revision = ?
                    """,
                    (guild_id, source_channel_id, expected_revision),
                )
        return cursor.rowcount == 1

    def get_policy(self, guild_id: int) -> ReadAloudPolicySnapshot:
        guild_id = _discord_id(guild_id, "guild_id")
        with self._lock:
            return self._decode_policy_locked(self._required(), guild_id)

    def set_dictionary(
        self,
        guild_id: int,
        term: str,
        pronunciation: str,
        *,
        expected_revision: int,
    ) -> ReadAloudPolicySnapshot:
        guild_id = _discord_id(guild_id, "guild_id")
        term = _normalize_policy_text(term, "term")
        pronunciation = _normalize_policy_text(pronunciation, "pronunciation")

        def mutate(connection: sqlite3.Connection) -> None:
            exists = connection.execute(
                """
                SELECT 1 FROM voice_read_aloud_dictionary
                WHERE guild_id = ? AND term = ?
                """,
                (guild_id, term),
            ).fetchone()
            if exists is None:
                count = connection.execute(
                    "SELECT COUNT(*) FROM voice_read_aloud_dictionary WHERE guild_id = ?",
                    (guild_id,),
                ).fetchone()
                if count is None or _strict_stored_int(count[0]) >= _MAX_POLICY_ITEMS:
                    raise ReadAloudRepositoryError("dictionary_limit_reached")
            connection.execute(
                """
                INSERT INTO voice_read_aloud_dictionary(guild_id, term, pronunciation)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, term) DO UPDATE SET pronunciation=excluded.pronunciation
                """,
                (guild_id, term, pronunciation),
            )

        return self._mutate_policy(guild_id, expected_revision, mutate)

    def delete_dictionary(
        self,
        guild_id: int,
        term: str,
        *,
        expected_revision: int,
    ) -> ReadAloudPolicySnapshot:
        guild_id = _discord_id(guild_id, "guild_id")
        term = _normalize_policy_text(term, "term")

        def mutate(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                "DELETE FROM voice_read_aloud_dictionary WHERE guild_id = ? AND term = ?",
                (guild_id, term),
            )
            if cursor.rowcount != 1:
                raise ReadAloudRepositoryError("dictionary_term_missing")

        return self._mutate_policy(guild_id, expected_revision, mutate)

    def add_exclusion(
        self,
        guild_id: int,
        phrase: str,
        *,
        expected_revision: int,
    ) -> ReadAloudPolicySnapshot:
        guild_id = _discord_id(guild_id, "guild_id")
        phrase = _normalize_policy_text(phrase, "phrase")

        def mutate(connection: sqlite3.Connection) -> None:
            exists = connection.execute(
                """
                SELECT 1 FROM voice_read_aloud_exclusions
                WHERE guild_id = ? AND phrase = ?
                """,
                (guild_id, phrase),
            ).fetchone()
            if exists is None:
                count = connection.execute(
                    "SELECT COUNT(*) FROM voice_read_aloud_exclusions WHERE guild_id = ?",
                    (guild_id,),
                ).fetchone()
                if count is None or _strict_stored_int(count[0]) >= _MAX_POLICY_ITEMS:
                    raise ReadAloudRepositoryError("exclusion_limit_reached")
                connection.execute(
                    "INSERT INTO voice_read_aloud_exclusions(guild_id, phrase) VALUES (?, ?)",
                    (guild_id, phrase),
                )

        return self._mutate_policy(guild_id, expected_revision, mutate)

    def delete_exclusion(
        self,
        guild_id: int,
        phrase: str,
        *,
        expected_revision: int,
    ) -> ReadAloudPolicySnapshot:
        guild_id = _discord_id(guild_id, "guild_id")
        phrase = _normalize_policy_text(phrase, "phrase")

        def mutate(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                "DELETE FROM voice_read_aloud_exclusions WHERE guild_id = ? AND phrase = ?",
                (guild_id, phrase),
            )
            if cursor.rowcount != 1:
                raise ReadAloudRepositoryError("exclusion_phrase_missing")

        return self._mutate_policy(guild_id, expected_revision, mutate)

    def _mutate_policy(
        self,
        guild_id: int,
        expected_revision: int,
        mutate: Callable[[sqlite3.Connection], None],
    ) -> ReadAloudPolicySnapshot:
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        with self._lock, self._transaction() as connection:
            before = self._decode_policy_locked(connection, guild_id)
            if before.revision != expected_revision:
                raise ReadAloudRepositoryError("policy_revision_conflict")
            next_revision = before.revision + 1
            connection.execute(
                """
                INSERT INTO voice_read_aloud_policy_revisions(guild_id, revision)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    revision=excluded.revision,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (guild_id, next_revision),
            )
            mutate(connection)
            return self._decode_policy_locked(connection, guild_id)

    @staticmethod
    def _decode_policy_locked(
        connection: sqlite3.Connection,
        guild_id: int,
    ) -> ReadAloudPolicySnapshot:
        revision_row = connection.execute(
            "SELECT revision FROM voice_read_aloud_policy_revisions WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        dictionary_rows = connection.execute(
            """
            SELECT term, pronunciation FROM voice_read_aloud_dictionary
            WHERE guild_id = ? ORDER BY term
            """,
            (guild_id,),
        ).fetchall()
        exclusion_rows = connection.execute(
            """
            SELECT phrase FROM voice_read_aloud_exclusions
            WHERE guild_id = ? ORDER BY phrase
            """,
            (guild_id,),
        ).fetchall()
        if revision_row is None:
            if dictionary_rows or exclusion_rows:
                raise ReadAloudRepositoryError("policy_revision_missing")
            return ReadAloudPolicySnapshot(guild_id=guild_id, revision=0)
        try:
            revision = _strict_stored_int(revision_row["revision"])
            if revision < 1:
                raise ValueError("stored policy revision is invalid")
            if len(dictionary_rows) > _MAX_POLICY_ITEMS or len(exclusion_rows) > _MAX_POLICY_ITEMS:
                raise ValueError("stored policy exceeds bounds")
            dictionary = tuple(
                ReadAloudDictionaryEntry(
                    term=_decode_policy_text(row["term"], "term"),
                    pronunciation=_decode_policy_text(row["pronunciation"], "pronunciation"),
                )
                for row in dictionary_rows
            )
            exclusions = tuple(_decode_policy_text(row["phrase"], "phrase") for row in exclusion_rows)
            return ReadAloudPolicySnapshot(
                guild_id=guild_id,
                revision=revision,
                dictionary=dictionary,
                exclusions=exclusions,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ReadAloudRepositoryError("policy_corrupt") from exc

    def _decode_or_quarantine(self, row: sqlite3.Row) -> ReadAloudRoute | None:
        try:
            return ReadAloudRoute(
                guild_id=_strict_stored_int(row["guild_id"]),
                source_channel_id=_strict_stored_int(row["source_channel_id"]),
                destination_voice_channel_id=_strict_stored_int(row["destination_voice_channel_id"]),
                enabled=_strict_bool(row["enabled"]),
                speaker_id=_strict_stored_int(row["speaker_id"]),
                revision=_strict_stored_int(row["revision"]),
            )
        except (TypeError, ValueError, OverflowError):
            self._quarantine(row, "route_row_invalid")
            return None

    def _quarantine(self, row: sqlite3.Row, reason_code: str) -> None:
        material = "|".join(
            str(row[key])
            for key in (
                "guild_id",
                "source_channel_id",
                "destination_voice_channel_id",
                "enabled",
                "speaker_id",
                "revision",
            )
        )
        row_sha256 = hashlib.sha256(material.encode("utf-8", errors="strict")).hexdigest()
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM voice_read_aloud_routes
                WHERE guild_id = ? AND source_channel_id = ?
                  AND destination_voice_channel_id = ? AND enabled = ?
                  AND speaker_id = ? AND revision = ?
                """,
                tuple(row[key] for key in row.keys()),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    """
                    INSERT INTO voice_read_aloud_route_quarantine(
                        guild_id, source_channel_id, row_sha256, reason_code
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        _safe_optional_id(row["guild_id"]),
                        _safe_optional_id(row["source_channel_id"]),
                        row_sha256,
                        reason_code,
                    ),
                )

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
            raise ReadAloudRepositoryError("repository_closed")
        return self._connection


class ReadAloudBatchStatus(StrEnum):
    DELIVERED = "delivered"
    REVOKED = "revoked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class _CurrentState(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    TIMED_OUT = "timed_out"


class _HardDeadlineExceeded(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReadAloudEnqueueReceipt:
    guild_id: int
    source_channel_id: int
    route_revision: int
    pending_items: int
    pending_characters: int


@dataclass(frozen=True, slots=True)
class ReadAloudBatchReceipt:
    guild_id: int
    source_channel_id: int
    route_revision: int
    item_count: int
    character_count: int
    status: ReadAloudBatchStatus
    error_code: str | None = None


@dataclass(slots=True)
class _PendingText:
    author_id: int
    text: str = field(repr=False)
    preset: ResolvedVoicePreset = field(repr=False)


@dataclass(frozen=True, slots=True)
class _ReadAloudBatch:
    items: tuple[_PendingText, ...] = field(repr=False)
    author_ids: tuple[int, ...]
    presets: tuple[ResolvedVoicePreset, ...] = field(repr=False)
    character_count: int

    def __post_init__(self) -> None:
        if not self.items or not self.author_ids or not self.presets or self.character_count < 1:
            raise ValueError("read-aloud batch is empty")
        if any(item.preset.batch_key != self.items[0].preset.batch_key for item in self.items):
            raise ValueError("read-aloud batch mixes presets")
        if any(item.author_id != item.preset.user_id for item in self.items):
            raise ValueError("read-aloud preset author scope is invalid")


@dataclass(slots=True)
class _RouteState:
    route: ReadAloudRoute
    policy: ReadAloudPolicySnapshot
    pending: list[_PendingText] = field(default_factory=list, repr=False)
    pending_characters: int = 0
    inflight_items: int = 0
    inflight_characters: int = 0
    inflight_author_ids: tuple[int, ...] = ()
    inflight_receipted: bool = False
    task: asyncio.Task[None] | None = field(default=None, repr=False)


SynthesizeCurrent = Callable[
    [SpeechRequest, ReadAloudRoute, tuple[int, ...]],
    Awaitable[SynthesizedSpeech],
]
DeliverCurrent = Callable[[ReadAloudRoute, tuple[int, ...], bytes], Awaitable[None]]
SynthesizePolicyCurrent = Callable[
    [SpeechRequest, ReadAloudRoute, tuple[int, ...], ReadAloudPolicySnapshot],
    Awaitable[SynthesizedSpeech],
]
DeliverPolicyCurrent = Callable[
    [ReadAloudRoute, tuple[int, ...], bytes, ReadAloudPolicySnapshot],
    Awaitable[None],
]
SynthesizePresetCurrent = Callable[
    [
        SpeechRequest,
        ReadAloudRoute,
        tuple[int, ...],
        ReadAloudPolicySnapshot,
        tuple[ResolvedVoicePreset, ...],
    ],
    Awaitable[SynthesizedSpeech],
]
DeliverPresetCurrent = Callable[
    [
        ReadAloudRoute,
        tuple[int, ...],
        bytes,
        ReadAloudPolicySnapshot,
        tuple[ResolvedVoicePreset, ...],
    ],
    Awaitable[None],
]
RouteCurrent = Callable[[ReadAloudRoute, tuple[int, ...]], Awaitable[bool]]
PolicyCurrent = Callable[
    [ReadAloudRoute, tuple[int, ...], ReadAloudPolicySnapshot],
    Awaitable[bool],
]
PresetCurrent = Callable[
    [ReadAloudRoute, tuple[int, ...], tuple[ResolvedVoicePreset, ...]],
    Awaitable[bool],
]
BatchFinished = Callable[[ReadAloudRoute, tuple[int, ...]], None]


class ReadAloudBurstCoordinator:
    """短い読み上げ本文をprocess memory内だけでまとめるbounded coordinator。"""

    def __init__(
        self,
        *,
        synthesize: SynthesizeCurrent,
        deliver: DeliverCurrent,
        route_current: RouteCurrent,
        synthesize_policy_current: SynthesizePolicyCurrent | None = None,
        deliver_policy_current: DeliverPolicyCurrent | None = None,
        policy_current: PolicyCurrent | None = None,
        synthesize_preset_current: SynthesizePresetCurrent | None = None,
        deliver_preset_current: DeliverPresetCurrent | None = None,
        preset_current: PresetCurrent | None = None,
        batch_finished: BatchFinished | None = None,
        merge_window_seconds: float = 0.25,
        max_pending_items_per_route: int = 8,
        max_pending_characters_per_route: int = 500,
        max_pending_routes: int = 64,
        receipt_limit: int = 128,
        current_timeout_seconds: float = 1.0,
        synthesis_timeout_seconds: float = 15.0,
        delivery_timeout_seconds: float = 5.0,
    ) -> None:
        if not callable(synthesize) or not callable(deliver) or not callable(route_current):
            raise TypeError("coordinator callbacks must be callable")
        if batch_finished is not None and not callable(batch_finished):
            raise TypeError("batch_finished must be callable")
        if policy_current is not None and not callable(policy_current):
            raise TypeError("policy_current must be callable")
        if synthesize_policy_current is not None and not callable(synthesize_policy_current):
            raise TypeError("synthesize_policy_current must be callable")
        if deliver_policy_current is not None and not callable(deliver_policy_current):
            raise TypeError("deliver_policy_current must be callable")
        if preset_current is not None and not callable(preset_current):
            raise TypeError("preset_current must be callable")
        if synthesize_preset_current is not None and not callable(synthesize_preset_current):
            raise TypeError("synthesize_preset_current must be callable")
        if deliver_preset_current is not None and not callable(deliver_preset_current):
            raise TypeError("deliver_preset_current must be callable")
        if not all(_is_async_callable(item) for item in (synthesize, deliver, route_current)):
            raise TypeError("coordinator callbacks must be async")
        if policy_current is not None and not _is_async_callable(policy_current):
            raise TypeError("policy_current must be async")
        if synthesize_policy_current is not None and not _is_async_callable(synthesize_policy_current):
            raise TypeError("synthesize_policy_current must be async")
        if deliver_policy_current is not None and not _is_async_callable(deliver_policy_current):
            raise TypeError("deliver_policy_current must be async")
        if preset_current is not None and not _is_async_callable(preset_current):
            raise TypeError("preset_current must be async")
        if synthesize_preset_current is not None and not _is_async_callable(synthesize_preset_current):
            raise TypeError("synthesize_preset_current must be async")
        if deliver_preset_current is not None and not _is_async_callable(deliver_preset_current):
            raise TypeError("deliver_preset_current must be async")
        if not 0.01 <= merge_window_seconds <= 2.0:
            raise ValueError("merge_window_seconds is out of bounds")
        if not 1 <= max_pending_items_per_route <= 32:
            raise ValueError("max_pending_items_per_route is out of bounds")
        if not 1 <= max_pending_characters_per_route <= 500:
            raise ValueError("max_pending_characters_per_route is out of bounds")
        if not 1 <= max_pending_routes <= 256:
            raise ValueError("max_pending_routes is out of bounds")
        if not 1 <= receipt_limit <= 1_024:
            raise ValueError("receipt_limit is out of bounds")
        if not 0.01 <= current_timeout_seconds <= 5.0:
            raise ValueError("current_timeout_seconds is out of bounds")
        if not 0.01 <= synthesis_timeout_seconds <= 30.0:
            raise ValueError("synthesis_timeout_seconds is out of bounds")
        if not 0.01 <= delivery_timeout_seconds <= 10.0:
            raise ValueError("delivery_timeout_seconds is out of bounds")
        self._synthesize = synthesize
        self._deliver = deliver
        self._synthesize_policy_current = synthesize_policy_current
        self._deliver_policy_current = deliver_policy_current
        self._synthesize_preset_current = synthesize_preset_current
        self._deliver_preset_current = deliver_preset_current
        self._route_current = route_current
        self._policy_current = policy_current
        self._preset_current = preset_current
        self._batch_finished = batch_finished
        self._merge_window_seconds = merge_window_seconds
        self._max_pending_items = max_pending_items_per_route
        self._max_pending_characters = max_pending_characters_per_route
        self._max_pending_routes = max_pending_routes
        self._current_timeout_seconds = current_timeout_seconds
        self._synthesis_timeout_seconds = synthesis_timeout_seconds
        self._delivery_timeout_seconds = delivery_timeout_seconds
        self._states: dict[tuple[int, int], _RouteState] = {}
        self._recent_content: dict[tuple[int, int, int, bytes], float] = {}
        self._receipts: deque[ReadAloudBatchReceipt] = deque(maxlen=receipt_limit)
        self._lock = asyncio.Lock()
        self._closed = False

    async def submit(
        self,
        route: ReadAloudRoute,
        *,
        author_id: int,
        text: str,
        author_is_current_voice_member: bool,
        author_is_bot: bool = False,
        is_webhook: bool = False,
        policy: ReadAloudPolicySnapshot | None = None,
        preset: ResolvedVoicePreset | None = None,
    ) -> ReadAloudEnqueueReceipt:
        if not isinstance(route, ReadAloudRoute):
            raise TypeError("route must be a ReadAloudRoute")
        author_id = _discord_id(author_id, "author_id")
        if not isinstance(author_is_current_voice_member, bool):
            raise TypeError("author_is_current_voice_member must be bool")
        if not isinstance(author_is_bot, bool) or not isinstance(is_webhook, bool):
            raise TypeError("message source flags must be bool")
        if self._closed:
            raise ReadAloudRejectedError("coordinator_closed")
        if not route.enabled:
            raise ReadAloudRejectedError("route_disabled")
        if author_is_bot or is_webhook:
            raise ReadAloudRejectedError("automated_author_rejected")
        if not author_is_current_voice_member:
            raise ReadAloudRejectedError("author_not_in_destination_voice")
        if policy is None:
            policy = ReadAloudPolicySnapshot(guild_id=route.guild_id, revision=0)
        if not isinstance(policy, ReadAloudPolicySnapshot) or policy.guild_id != route.guild_id:
            raise ReadAloudRejectedError("policy_scope_invalid")
        if preset is None:
            preset = ResolvedVoicePreset(guild_id=route.guild_id, user_id=author_id)
        if (
            not isinstance(preset, ResolvedVoicePreset)
            or preset.guild_id != route.guild_id
            or preset.user_id != author_id
        ):
            raise ReadAloudRejectedError("preset_scope_invalid")
        normalized = apply_read_aloud_policy(text, policy)
        key = (route.guild_id, route.source_channel_id)

        async with self._lock:
            if self._closed:
                raise ReadAloudRejectedError("coordinator_closed")
            state = self._states.get(key)
            if state is None:
                if len(self._states) >= self._max_pending_routes:
                    raise ReadAloudRejectedError("route_limit_reached")
            elif state.route != route:
                raise ReadAloudRejectedError("route_identity_changed")
            elif state.policy != policy:
                raise ReadAloudRejectedError("policy_identity_changed")
            pending = () if state is None else state.pending
            pending_characters = 0 if state is None else state.pending_characters
            if sum(item.author_id == author_id for item in pending) >= _MAX_PENDING_ITEMS_PER_AUTHOR:
                raise ReadAloudRejectedError("pending_author_item_limit_reached")
            if len(pending) >= self._max_pending_items:
                raise ReadAloudRejectedError("pending_item_limit_reached")
            added = len(normalized) + (1 if pending else 0)
            if pending_characters + added > self._max_pending_characters:
                raise ReadAloudRejectedError("pending_text_limit_reached")
            self._reject_recent_content_duplicate(key, author_id, normalized)
            if state is None:
                state = _RouteState(route=route, policy=policy)
                self._states[key] = state
            state.pending.append(
                _PendingText(
                    author_id=author_id,
                    text=normalized,
                    preset=preset,
                )
            )
            state.pending_characters += added
            if state.task is None:
                state.task = asyncio.create_task(self._run_route(key, state))
            return ReadAloudEnqueueReceipt(
                guild_id=route.guild_id,
                source_channel_id=route.source_channel_id,
                route_revision=route.revision,
                pending_items=len(state.pending),
                pending_characters=state.pending_characters,
            )

    def _reject_recent_content_duplicate(
        self,
        route_key: tuple[int, int],
        author_id: int,
        normalized: str,
    ) -> None:
        now = time.monotonic()
        cutoff = now - _CONTENT_DEDUPE_TTL_SECONDS
        for key, observed_at in tuple(self._recent_content.items()):
            if observed_at <= cutoff:
                self._recent_content.pop(key, None)
        digest = hashlib.sha256(normalized.encode("utf-8", errors="strict")).digest()
        key = (*route_key, author_id, digest)
        if key in self._recent_content:
            raise ReadAloudRejectedError("duplicate_content")
        self._recent_content[key] = now
        while len(self._recent_content) > _CONTENT_DEDUPE_LIMIT:
            self._recent_content.pop(next(iter(self._recent_content)))

    def receipts(self) -> tuple[ReadAloudBatchReceipt, ...]:
        return tuple(self._receipts)

    async def wait_idle(self) -> None:
        while True:
            async with self._lock:
                tasks = tuple(state.task for state in self._states.values() if state.task is not None)
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            states = tuple(self._states.values())
            self._states.clear()
            self._recent_content.clear()
            tasks = tuple(state.task for state in states if state.task is not None)
            for state in states:
                if state.pending:
                    self._record(
                        state.route,
                        tuple(state.pending),
                        state.pending_characters,
                        ReadAloudBatchStatus.CANCELLED,
                        "coordinator_closed",
                    )
                if state.inflight_items and not state.inflight_receipted:
                    self._record_inflight(
                        state,
                        ReadAloudBatchStatus.CANCELLED,
                        "coordinator_closed",
                    )
                state.pending.clear()
                state.pending_characters = 0
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_route(self, key: tuple[int, int], state: _RouteState) -> None:
        try:
            while True:
                await asyncio.sleep(self._merge_window_seconds)
                async with self._lock:
                    if self._closed or self._states.get(key) is not state:
                        return
                    batch = _take_next_preset_batch(state.pending)
                    character_count = _pending_character_count(batch)
                    del state.pending[: len(batch)]
                    state.pending_characters = _pending_character_count(state.pending)
                    state.inflight_items = len(batch)
                    state.inflight_characters = character_count
                    state.inflight_author_ids = tuple(dict.fromkeys(item.author_id for item in batch))
                    state.inflight_receipted = False
                if not batch:
                    async with self._lock:
                        if self._states.get(key) is state:
                            self._states.pop(key, None)
                            state.task = None
                    return
                immutable_batch = _ReadAloudBatch(
                    items=batch,
                    author_ids=state.inflight_author_ids,
                    presets=tuple(dict.fromkeys(item.preset for item in batch)),
                    character_count=character_count,
                )
                try:
                    continue_route = await self._process_batch(state, immutable_batch)
                finally:
                    self._notify_batch_finished(
                        state.route,
                        immutable_batch.author_ids,
                    )
                async with self._lock:
                    if self._closed or self._states.get(key) is not state:
                        return
                    if not continue_route:
                        if state.pending:
                            self._record(
                                state.route,
                                tuple(state.pending),
                                state.pending_characters,
                                ReadAloudBatchStatus.CANCELLED,
                                "route_task_timed_out",
                            )
                            state.pending.clear()
                            state.pending_characters = 0
                        state.inflight_items = 0
                        state.inflight_characters = 0
                        state.inflight_author_ids = ()
                        state.inflight_receipted = False
                        self._states.pop(key, None)
                        state.task = None
                        return
                    state.inflight_items = 0
                    state.inflight_characters = 0
                    state.inflight_author_ids = ()
                    state.inflight_receipted = False
                    if not state.pending:
                        self._states.pop(key, None)
                        state.task = None
                        return
        except asyncio.CancelledError:
            if state.inflight_items and not state.inflight_receipted:
                self._record_inflight(
                    state,
                    ReadAloudBatchStatus.CANCELLED,
                    "coordinator_closed" if self._closed else "external_cancelled",
                )
            if batch := tuple(state.pending):
                self._record(
                    state.route,
                    batch,
                    state.pending_characters,
                    ReadAloudBatchStatus.CANCELLED,
                    "coordinator_closed" if self._closed else "external_cancelled",
                )
            raise
        finally:
            async with self._lock:
                if self._states.get(key) is state:
                    self._states.pop(key, None)
                    state.pending.clear()
                    state.pending_characters = 0
                    state.inflight_items = 0
                    state.inflight_characters = 0
                    state.inflight_author_ids = ()
                    state.inflight_receipted = False
                state.task = None

    async def _process_batch(
        self,
        state: _RouteState,
        batch: _ReadAloudBatch,
    ) -> bool:
        route = state.route
        current_state = await self._current_state(state, batch.author_ids, batch.presets)
        if current_state is _CurrentState.TIMED_OUT:
            self._record_inflight(
                state,
                ReadAloudBatchStatus.FAILED,
                "route_current_timeout_before_synthesis",
            )
            return False
        if current_state is not _CurrentState.ALLOWED:
            self._record_inflight(state, ReadAloudBatchStatus.REVOKED, "route_revoked_before_synthesis")
            return True
        text = " ".join(item.text for item in batch.items)
        request = SpeechRequest(
            text=text,
            guild_id=route.guild_id,
            channel_id=route.source_channel_id,
            speaker_id=VOICEVOX_SPEAKER_ID,
            speed_scale=batch.presets[0].values.speed_scale,
            volume_scale=batch.presets[0].values.volume_scale,
        )
        try:
            if self._synthesize_preset_current is not None:
                synthesis = self._synthesize_preset_current(
                    request,
                    route,
                    batch.author_ids,
                    state.policy,
                    batch.presets,
                )
            elif self._synthesize_policy_current is not None:
                synthesis = self._synthesize_policy_current(
                    request,
                    route,
                    batch.author_ids,
                    state.policy,
                )
            else:
                synthesis = self._synthesize(request, route, batch.author_ids)
            speech = await _hard_deadline(
                synthesis,
                self._synthesis_timeout_seconds,
            )
        except _HardDeadlineExceeded:
            self._record_inflight(state, ReadAloudBatchStatus.FAILED, "synthesis_timeout")
            return False
        except asyncio.CancelledError:
            raise
        except Exception:
            self._record_inflight(state, ReadAloudBatchStatus.FAILED, "synthesis_failed")
            return True
        if not isinstance(speech, SynthesizedSpeech):
            self._record_inflight(state, ReadAloudBatchStatus.FAILED, "synthesis_result_invalid")
            return True
        current_state = await self._current_state(state, batch.author_ids, batch.presets)
        if current_state is _CurrentState.TIMED_OUT:
            self._record_inflight(
                state,
                ReadAloudBatchStatus.FAILED,
                "route_current_timeout_after_synthesis",
            )
            return False
        if current_state is not _CurrentState.ALLOWED:
            self._record_inflight(state, ReadAloudBatchStatus.REVOKED, "route_revoked_after_synthesis")
            return True
        current_state = await self._current_state(state, batch.author_ids, batch.presets)
        if current_state is _CurrentState.TIMED_OUT:
            self._record_inflight(
                state,
                ReadAloudBatchStatus.FAILED,
                "route_current_timeout_before_delivery",
            )
            return False
        if current_state is not _CurrentState.ALLOWED:
            self._record_inflight(state, ReadAloudBatchStatus.REVOKED, "route_revoked_before_delivery")
            return True
        try:
            if self._deliver_preset_current is not None:
                delivery = self._deliver_preset_current(
                    route,
                    batch.author_ids,
                    speech.wav,
                    state.policy,
                    batch.presets,
                )
            elif self._deliver_policy_current is not None:
                delivery = self._deliver_policy_current(
                    route,
                    batch.author_ids,
                    speech.wav,
                    state.policy,
                )
            else:
                delivery = self._deliver(route, batch.author_ids, speech.wav)
            await _hard_deadline(
                delivery,
                self._delivery_timeout_seconds,
            )
        except _HardDeadlineExceeded:
            self._record_inflight(state, ReadAloudBatchStatus.FAILED, "delivery_timeout")
            return False
        except asyncio.CancelledError:
            raise
        except Exception:
            self._record_inflight(state, ReadAloudBatchStatus.FAILED, "delivery_failed")
            return True
        self._record_inflight(state, ReadAloudBatchStatus.DELIVERED, None)
        return True

    async def _current_state(
        self,
        state: _RouteState,
        author_ids: tuple[int, ...],
        presets: tuple[ResolvedVoicePreset, ...],
    ) -> _CurrentState:
        try:
            value = await _hard_deadline(
                self._route_current(state.route, author_ids),
                self._current_timeout_seconds,
            )
            if value is not True or self._closed:
                return _CurrentState.DENIED
            if self._policy_current is not None:
                value = await _hard_deadline(
                    self._policy_current(state.route, author_ids, state.policy),
                    self._current_timeout_seconds,
                )
                if value is not True or self._closed:
                    return _CurrentState.DENIED
            if self._preset_current is not None:
                value = await _hard_deadline(
                    self._preset_current(state.route, author_ids, presets),
                    self._current_timeout_seconds,
                )
            return _CurrentState.ALLOWED if value is True and not self._closed else _CurrentState.DENIED
        except _HardDeadlineExceeded:
            return _CurrentState.TIMED_OUT
        except asyncio.CancelledError:
            raise
        except Exception:
            return _CurrentState.DENIED

    def _record_inflight(
        self,
        state: _RouteState,
        status: ReadAloudBatchStatus,
        error_code: str | None,
    ) -> None:
        if state.inflight_receipted or state.inflight_items < 1:
            return
        self._receipts.append(
            ReadAloudBatchReceipt(
                guild_id=state.route.guild_id,
                source_channel_id=state.route.source_channel_id,
                route_revision=state.route.revision,
                item_count=state.inflight_items,
                character_count=state.inflight_characters,
                status=status,
                error_code=error_code,
            )
        )
        state.inflight_receipted = True

    def _record(
        self,
        route: ReadAloudRoute,
        batch: tuple[_PendingText, ...],
        character_count: int,
        status: ReadAloudBatchStatus,
        error_code: str | None,
    ) -> None:
        self._receipts.append(
            ReadAloudBatchReceipt(
                guild_id=route.guild_id,
                source_channel_id=route.source_channel_id,
                route_revision=route.revision,
                item_count=len(batch),
                character_count=character_count,
                status=status,
                error_code=error_code,
            )
        )

    def _notify_batch_finished(
        self,
        route: ReadAloudRoute,
        author_ids: tuple[int, ...],
    ) -> None:
        callback = self._batch_finished
        if callback is None:
            return
        try:
            callback(route, author_ids)
        except Exception:
            return


def _take_next_preset_batch(pending: list[_PendingText]) -> tuple[_PendingText, ...]:
    if not pending:
        return ()
    batch_key = pending[0].preset.batch_key
    length = 1
    while length < len(pending) and pending[length].preset.batch_key == batch_key:
        length += 1
    return tuple(pending[:length])


def _pending_character_count(items: list[_PendingText] | tuple[_PendingText, ...]) -> int:
    return sum(len(item.text) for item in items) + max(0, len(items) - 1)


def _normalize_read_aloud_text(text: str) -> str:
    try:
        return normalize_speech_text(text)
    except ValueError as exc:
        raise ReadAloudRejectedError(str(exc)) from exc


def apply_read_aloud_policy(text: str, policy: ReadAloudPolicySnapshot) -> str:
    if not isinstance(policy, ReadAloudPolicySnapshot):
        raise TypeError("policy must be a ReadAloudPolicySnapshot")
    normalized = _normalize_read_aloud_text(text)
    if any(phrase in normalized for phrase in policy.exclusions):
        raise ReadAloudRejectedError("text_excluded")
    ordered = tuple(
        sorted(
            policy.dictionary,
            key=lambda item: (-len(item.term), item.term),
        )
    )
    if not ordered:
        return normalized
    output: list[str] = []
    position = 0
    while position < len(normalized):
        matched = next(
            (item for item in ordered if normalized.startswith(item.term, position)),
            None,
        )
        if matched is None:
            output.append(normalized[position])
            position += 1
        else:
            output.append(matched.pronunciation)
            position += len(matched.term)
        if sum(len(value) for value in output) > 500:
            raise ReadAloudRejectedError("text_too_long")
    replaced = "".join(output)
    if len(replaced) > 500:
        raise ReadAloudRejectedError("text_too_long")
    return replaced


def _normalize_policy_text(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    normalized = unicodedata.normalize("NFKC", value)
    if any(unicodedata.category(character) == "Cc" for character in normalized):
        raise ValueError(f"{name} contains a control character")
    normalized = normalized.strip()
    if not 1 <= len(normalized) <= _MAX_POLICY_TEXT_LENGTH:
        raise ValueError(f"{name} length is out of bounds")
    return normalized


def _decode_policy_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError("stored policy text is invalid")
    normalized = _normalize_policy_text(value, name)
    if normalized != value:
        raise ValueError("stored policy text is not normalized")
    return normalized


async def _hard_deadline(awaitable: Awaitable[object], timeout_seconds: float) -> object:
    task = asyncio.ensure_future(awaitable)
    try:
        done, _pending = await asyncio.wait((task,), timeout=timeout_seconds)
    except asyncio.CancelledError:
        task.cancel()
        task.add_done_callback(_consume_future)
        raise
    if task not in done:
        task.cancel()
        task.add_done_callback(_consume_future)
        raise _HardDeadlineExceeded("callback_deadline_exceeded")
    return task.result()


def _consume_future(future: asyncio.Future[object]) -> None:
    try:
        future.exception()
    except asyncio.CancelledError:
        pass


def _is_async_callable(callback: object) -> bool:
    return inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(getattr(callback, "__call__", None))


def _discord_id(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_DISCORD_ID:
        raise ValueError(f"{name} must be a positive Discord ID")
    return value


def _strict_bool(value: object) -> bool:
    if type(value) is not int or value not in (0, 1):
        raise ValueError("stored bool is invalid")
    return bool(value)


def _strict_stored_int(value: object) -> int:
    if type(value) is not int:
        raise ValueError("stored integer is invalid")
    return value


def _safe_optional_id(value: object) -> int | None:
    return value if type(value) is int and 1 <= value <= _MAX_DISCORD_ID else None
