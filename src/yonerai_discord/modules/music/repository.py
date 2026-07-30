from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from yonerai_discord.modules.audio_core import LoopMode

from .models import (
    AuthorizedMusicTrackRef,
    GuildAudioProjection,
    ImportedMusicAsset,
    MusicDashboardBinding,
    PERSISTED_MUSIC_SCHEMA_VERSION,
    PersistedMusicTrackRef,
    PlaylistError,
    PlaylistRecord,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS music_playlists (
    guild_id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL,
    name_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, owner_id, name_key)
);

CREATE TABLE IF NOT EXISTS music_playlist_tracks (
    guild_id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL,
    name_key TEXT NOT NULL,
    position INTEGER NOT NULL,
    track_title TEXT NOT NULL,
    PRIMARY KEY (guild_id, owner_id, name_key, position),
    FOREIGN KEY (guild_id, owner_id, name_key)
        REFERENCES music_playlists (guild_id, owner_id, name_key)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_music_playlists_owner
    ON music_playlists (guild_id, owner_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS music_track_rights (
    guild_id INTEGER NOT NULL,
    track_key TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    approved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, track_key)
);

CREATE TABLE IF NOT EXISTS music_imported_assets (
    library_ref TEXT PRIMARY KEY,
    content_sha256 TEXT NOT NULL UNIQUE,
    display_title TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 44 AND 8388608),
    duration_milliseconds INTEGER NOT NULL CHECK(duration_milliseconds BETWEEN 1000 AND 30000),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS music_guild_imported_asset_aliases (
    guild_id INTEGER NOT NULL CHECK(guild_id > 0),
    library_ref TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    display_title TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, library_ref)
);

CREATE INDEX IF NOT EXISTS idx_music_guild_imported_asset_aliases_asset
    ON music_guild_imported_asset_aliases (library_ref, content_sha256);

CREATE TABLE IF NOT EXISTS music_guild_audio_projections (
    guild_id INTEGER PRIMARY KEY CHECK(guild_id > 0),
    schema_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS music_guild_audio_projection_quarantine (
    quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL CHECK(guild_id > 0),
    reason_code TEXT NOT NULL,
    row_sha256 TEXT NOT NULL,
    quarantined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS music_dashboard_bindings (
    guild_id INTEGER PRIMARY KEY CHECK(guild_id > 0),
    channel_id INTEGER NOT NULL CHECK(channel_id > 0),
    message_id INTEGER NOT NULL CHECK(message_id > 0),
    owner_id INTEGER NOT NULL CHECK(owner_id > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (channel_id, message_id)
);
"""

_AUDIO_PROJECTION_MAX_BYTES = 131_072
_PROJECTION_KEYS = frozenset(
    {
        "schema_version",
        "guild_id",
        "tracks",
        "loop_mode",
        "paused",
        "music_volume",
        "speech_volume",
    }
)
_TRACK_KEYS = frozenset({"library_ref", "content_sha256", "requester_id", "retry_count"})
_QUARANTINE_REASONS = frozenset({"unknown_schema", "digest_mismatch", "payload_invalid"})
_PRIVATE_IMPORTED_ASSET_TITLE = "private-import"


class _ProjectionDecodeError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class MusicPlaylistRepository:
    """guild/user境界を複合主キーで固定したSQLite playlist store。"""

    def __init__(
        self,
        path: Path,
        *,
        max_playlists_per_user: int = 50,
        max_tracks_per_playlist: int = 100,
    ) -> None:
        if not 1 <= max_playlists_per_user <= 500:
            raise ValueError("max_playlists_per_user is invalid")
        if not 1 <= max_tracks_per_playlist <= 1_000:
            raise ValueError("max_tracks_per_playlist is invalid")
        self.path = Path(path)
        self.max_playlists_per_user = max_playlists_per_user
        self.max_tracks_per_playlist = max_tracks_per_playlist
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
                connection.executescript(SCHEMA)
            except sqlite3.Error:
                connection.close()
                raise
            self._connection = connection

    def close(self) -> None:
        with self._lock:
            connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

    def save(self, guild_id: int, owner_id: int, name: str, track_titles: tuple[str, ...]) -> PlaylistRecord:
        guild_id, owner_id = _ids(guild_id, owner_id)
        display_name, name_key = _playlist_name(name)
        titles = _track_titles(track_titles, maximum=self.max_tracks_per_playlist)
        with self._lock, self._transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM music_playlists WHERE guild_id = ? AND owner_id = ? AND name_key = ?",
                (guild_id, owner_id, name_key),
            ).fetchone()
            if exists is None:
                count = connection.execute(
                    "SELECT COUNT(*) AS value FROM music_playlists WHERE guild_id = ? AND owner_id = ?",
                    (guild_id, owner_id),
                ).fetchone()["value"]
                if int(count) >= self.max_playlists_per_user:
                    raise PlaylistError("playlist limit reached")
            connection.execute(
                """
                INSERT INTO music_playlists (guild_id, owner_id, name_key, display_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, owner_id, name_key) DO UPDATE SET
                    display_name = excluded.display_name,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, owner_id, name_key, display_name),
            )
            connection.execute(
                "DELETE FROM music_playlist_tracks WHERE guild_id = ? AND owner_id = ? AND name_key = ?",
                (guild_id, owner_id, name_key),
            )
            connection.executemany(
                """
                INSERT INTO music_playlist_tracks (guild_id, owner_id, name_key, position, track_title)
                VALUES (?, ?, ?, ?, ?)
                """,
                ((guild_id, owner_id, name_key, position, title) for position, title in enumerate(titles, start=1)),
            )
        return PlaylistRecord(guild_id, owner_id, display_name, titles)

    def list(self, guild_id: int, owner_id: int) -> tuple[PlaylistRecord, ...]:
        guild_id, owner_id = _ids(guild_id, owner_id)
        with self._lock:
            connection = self._required()
            rows = connection.execute(
                """
                SELECT display_name
                FROM music_playlists
                WHERE guild_id = ? AND owner_id = ?
                ORDER BY display_name COLLATE NOCASE, name_key
                """,
                (guild_id, owner_id),
            ).fetchall()
        return tuple(PlaylistRecord(guild_id, owner_id, str(row["display_name"]), ()) for row in rows)

    def load(self, guild_id: int, owner_id: int, name: str) -> PlaylistRecord | None:
        guild_id, owner_id = _ids(guild_id, owner_id)
        _, name_key = _playlist_name(name)
        with self._lock:
            connection = self._required()
            row = connection.execute(
                """
                SELECT display_name
                FROM music_playlists
                WHERE guild_id = ? AND owner_id = ? AND name_key = ?
                """,
                (guild_id, owner_id, name_key),
            ).fetchone()
            if row is None:
                return None
            tracks = connection.execute(
                """
                SELECT track_title
                FROM music_playlist_tracks
                WHERE guild_id = ? AND owner_id = ? AND name_key = ?
                ORDER BY position
                """,
                (guild_id, owner_id, name_key),
            ).fetchall()
        return PlaylistRecord(
            guild_id,
            owner_id,
            str(row["display_name"]),
            tuple(str(track["track_title"]) for track in tracks),
        )

    def delete(self, guild_id: int, owner_id: int, name: str) -> bool:
        guild_id, owner_id = _ids(guild_id, owner_id)
        _, name_key = _playlist_name(name)
        with self._lock, self._transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM music_playlists WHERE guild_id = ? AND owner_id = ? AND name_key = ?",
                (guild_id, owner_id, name_key),
            )
        return cursor.rowcount > 0

    def grant_track_rights(self, guild_id: int, track_key: str, content_sha256: str) -> None:
        guild_id = _guild_id(guild_id)
        track_key, content_sha256 = _track_rights_values(track_key, content_sha256)
        with self._lock, self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO music_track_rights (guild_id, track_key, content_sha256)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, track_key) DO UPDATE SET
                    content_sha256 = excluded.content_sha256,
                    approved_at = CURRENT_TIMESTAMP
                """,
                (guild_id, track_key, content_sha256),
            )

    def revoke_track_rights(self, guild_id: int, track_key: str) -> bool:
        guild_id = _guild_id(guild_id)
        track_key, _ = _track_rights_values(track_key, "0" * 64)
        with self._lock, self._transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM music_track_rights WHERE guild_id = ? AND track_key = ?",
                (guild_id, track_key),
            )
        return cursor.rowcount > 0

    def track_rights_allowed(self, guild_id: int, track_key: str, content_sha256: str) -> bool:
        guild_id = _guild_id(guild_id)
        track_key, content_sha256 = _track_rights_values(track_key, content_sha256)
        with self._lock:
            row = (
                self._required()
                .execute(
                    """
                SELECT 1 FROM music_track_rights
                WHERE guild_id = ? AND track_key = ? AND content_sha256 = ?
                """,
                    (guild_id, track_key, content_sha256),
                )
                .fetchone()
            )
        return row is not None

    def list_track_rights(
        self,
        guild_id: int,
        *,
        limit: int = 25,
    ) -> tuple[AuthorizedMusicTrackRef, ...]:
        guild_id = _guild_id(guild_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("track rights limit must be between 1 and 25")
        with self._lock:
            rows = (
                self._required()
                .execute(
                    """
                    SELECT track_key, content_sha256
                    FROM music_track_rights
                    WHERE guild_id = ?
                    ORDER BY track_key ASC
                    LIMIT ?
                    """,
                    (guild_id, limit),
                )
                .fetchall()
            )
        return tuple(
            AuthorizedMusicTrackRef(
                library_ref=str(row["track_key"]),
                content_sha256=str(row["content_sha256"]),
            )
            for row in rows
        )

    def register_imported_asset_and_grant(
        self,
        guild_id: int,
        asset: ImportedMusicAsset,
        runtime_current: Callable[[], bool] | None = None,
    ) -> ImportedMusicAsset:
        guild_id = _guild_id(guild_id)
        if not isinstance(asset, ImportedMusicAsset):
            raise TypeError("asset must be an ImportedMusicAsset")
        with self._lock, self._transaction(commit_current=runtime_current) as connection:
            row = connection.execute(
                """
                SELECT library_ref, content_sha256, display_title, size_bytes, duration_milliseconds
                FROM music_imported_assets
                WHERE content_sha256 = ?
                """,
                (asset.content_sha256,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO music_imported_assets
                        (library_ref, content_sha256, display_title, size_bytes, duration_milliseconds)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        asset.library_ref,
                        asset.content_sha256,
                        _PRIVATE_IMPORTED_ASSET_TITLE,
                        asset.size_bytes,
                        asset.duration_milliseconds,
                    ),
                )
                stored = asset
            else:
                stored = _imported_asset(row)
                if (
                    stored.library_ref != asset.library_ref
                    or stored.size_bytes != asset.size_bytes
                    or stored.duration_milliseconds != asset.duration_milliseconds
                ):
                    raise PlaylistError("imported audio identity changed")
            connection.execute(
                """
                INSERT INTO music_track_rights (guild_id, track_key, content_sha256)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, track_key) DO UPDATE SET
                    content_sha256 = excluded.content_sha256,
                    approved_at = CURRENT_TIMESTAMP
                """,
                (guild_id, stored.library_ref, stored.content_sha256),
            )
            connection.execute(
                """
                INSERT INTO music_guild_imported_asset_aliases
                    (guild_id, library_ref, content_sha256, display_title)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, library_ref) DO UPDATE SET
                    content_sha256 = excluded.content_sha256,
                    display_title = excluded.display_title,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, stored.library_ref, stored.content_sha256, asset.display_title),
            )
        return ImportedMusicAsset(
            library_ref=stored.library_ref,
            content_sha256=stored.content_sha256,
            display_title=asset.display_title,
            size_bytes=stored.size_bytes,
            duration_milliseconds=stored.duration_milliseconds,
        )

    def list_imported_assets(self) -> tuple[ImportedMusicAsset, ...]:
        with self._lock:
            rows = (
                self._required()
                .execute(
                    """
                SELECT library_ref, content_sha256, display_title, size_bytes, duration_milliseconds
                FROM music_imported_assets
                ORDER BY created_at, library_ref
                """
                )
                .fetchall()
            )
        return tuple(_imported_asset(row) for row in rows)

    def list_imported_assets_for_guild(
        self,
        guild_id: int,
        *,
        limit: int = 25,
    ) -> tuple[ImportedMusicAsset, ...]:
        """Return only this guild's still-authorized private import aliases."""

        guild_id = _guild_id(guild_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit is invalid")
        with self._lock:
            rows = (
                self._required()
                .execute(
                    """
                SELECT asset.library_ref, asset.content_sha256,
                       aliases.display_title, asset.size_bytes, asset.duration_milliseconds
                FROM music_guild_imported_asset_aliases AS aliases
                JOIN music_imported_assets AS asset
                  ON asset.library_ref = aliases.library_ref
                 AND asset.content_sha256 = aliases.content_sha256
                JOIN music_track_rights AS rights
                  ON rights.guild_id = aliases.guild_id
                 AND rights.track_key = asset.library_ref
                 AND rights.content_sha256 = asset.content_sha256
                WHERE aliases.guild_id = ?
                ORDER BY aliases.created_at, asset.library_ref
                LIMIT ?
                """,
                    (guild_id, limit),
                )
                .fetchall()
            )
        return tuple(_imported_asset(row) for row in rows)

    def search_imported_assets_for_guild(
        self,
        guild_id: int,
        query: str,
        *,
        limit: int = 10,
    ) -> tuple[ImportedMusicAsset, ...]:
        """Search only aliases the current guild is authorized to reveal."""

        normalized = " ".join(str(query or "").split()).casefold()
        if not 1 <= len(normalized) <= 200 or any(
            ord(character) < 32 or ord(character) == 127 for character in normalized
        ):
            raise PlaylistError("imported asset query is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit is invalid")
        with self._lock:
            rows = (
                self._required()
                .execute(
                    """
                SELECT asset.library_ref, asset.content_sha256,
                       aliases.display_title, asset.size_bytes, asset.duration_milliseconds
                FROM music_guild_imported_asset_aliases AS aliases
                JOIN music_imported_assets AS asset
                  ON asset.library_ref = aliases.library_ref
                 AND asset.content_sha256 = aliases.content_sha256
                JOIN music_track_rights AS rights
                  ON rights.guild_id = aliases.guild_id
                 AND rights.track_key = asset.library_ref
                 AND rights.content_sha256 = asset.content_sha256
                WHERE aliases.guild_id = ?
                  AND instr(lower(aliases.display_title), ?) > 0
                ORDER BY aliases.created_at, asset.library_ref
                LIMIT ?
                """,
                    (guild_id, normalized, limit),
                )
                .fetchall()
            )
        return tuple(_imported_asset(row) for row in rows)

    def resolve_imported_asset_for_guild(
        self,
        guild_id: int,
        library_ref: str,
        content_sha256: str,
    ) -> ImportedMusicAsset | None:
        """Resolve one exact current-guild alias only while its right is live."""

        guild_id = _guild_id(guild_id)
        reference, digest = _track_rights_values(library_ref, content_sha256)
        with self._lock:
            row = (
                self._required()
                .execute(
                    """
                SELECT asset.library_ref, asset.content_sha256,
                       aliases.display_title, asset.size_bytes, asset.duration_milliseconds
                FROM music_guild_imported_asset_aliases AS aliases
                JOIN music_imported_assets AS asset
                  ON asset.library_ref = aliases.library_ref
                 AND asset.content_sha256 = aliases.content_sha256
                JOIN music_track_rights AS rights
                  ON rights.guild_id = aliases.guild_id
                 AND rights.track_key = asset.library_ref
                 AND rights.content_sha256 = asset.content_sha256
                WHERE aliases.guild_id = ?
                  AND asset.library_ref = ?
                  AND asset.content_sha256 = ?
                LIMIT 1
                """,
                    (guild_id, reference, digest),
                )
                .fetchone()
            )
        return _imported_asset(row) if row is not None else None

    def imported_asset_reference_count(self, asset: ImportedMusicAsset) -> int:
        if not isinstance(asset, ImportedMusicAsset):
            raise TypeError("asset must be an ImportedMusicAsset")
        with self._lock:
            connection = self._required()
            rights = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS value
                    FROM music_track_rights
                    WHERE track_key = ? AND content_sha256 = ?
                    """,
                    (asset.library_ref, asset.content_sha256),
                ).fetchone()["value"]
            )
            projection_rows = connection.execute(
                "SELECT * FROM music_guild_audio_projections ORDER BY guild_id"
            ).fetchall()
            projections = 0
            for row in projection_rows:
                try:
                    projection = _decode_audio_projection(row)
                except _ProjectionDecodeError:
                    return max(1, rights)
                projections += sum(
                    persisted.library_ref == asset.library_ref
                    and hmac.compare_digest(persisted.content_sha256, asset.content_sha256)
                    for persisted in projection.tracks
                )
            return rights + projections

    def imported_digest_reference_count(
        self,
        content_sha256: str,
        *,
        missing_is_zero: bool = False,
    ) -> int:
        """Return a cleanup-safe count without trusting a caller supplied path.

        A missing or malformed repository row is not evidence that a private
        import is unreferenced.  The caller must therefore retain it until a
        later clean sweep can establish the exact asset identity.
        """

        if not isinstance(missing_is_zero, bool):
            raise TypeError("missing_is_zero must be a boolean")
        digest = str(content_sha256 or "").casefold()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            return 1
        with self._lock:
            row = (
                self._required()
                .execute(
                    """
                SELECT library_ref, content_sha256, display_title, size_bytes, duration_milliseconds
                FROM music_imported_assets
                WHERE content_sha256 = ?
                LIMIT 1
                """,
                    (digest,),
                )
                .fetchone()
            )
            if row is None:
                return 0 if missing_is_zero else 1
            try:
                asset = _imported_asset(row)
            except (TypeError, ValueError):
                return 1
            return self.imported_asset_reference_count(asset)

    def delete_imported_asset_if_unreferenced(self, asset: ImportedMusicAsset) -> bool:
        if not isinstance(asset, ImportedMusicAsset):
            raise TypeError("asset must be an ImportedMusicAsset")
        with self._lock, self._transaction() as connection:
            rights = connection.execute(
                """
                SELECT 1 FROM music_track_rights
                WHERE track_key = ? AND content_sha256 = ?
                LIMIT 1
                """,
                (asset.library_ref, asset.content_sha256),
            ).fetchone()
            if rights is not None:
                return False
            rows = connection.execute("SELECT * FROM music_guild_audio_projections").fetchall()
            for row in rows:
                try:
                    projection = _decode_audio_projection(row)
                except _ProjectionDecodeError:
                    return False
                if any(
                    track.library_ref == asset.library_ref
                    and hmac.compare_digest(track.content_sha256, asset.content_sha256)
                    for track in projection.tracks
                ):
                    return False
            cursor = connection.execute(
                """
                DELETE FROM music_imported_assets
                WHERE library_ref = ? AND content_sha256 = ?
                """,
                (asset.library_ref, asset.content_sha256),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    """
                    DELETE FROM music_guild_imported_asset_aliases
                    WHERE library_ref = ? AND content_sha256 = ?
                    """,
                    (asset.library_ref, asset.content_sha256),
                )
        return cursor.rowcount == 1

    def save_audio_projection(self, projection: GuildAudioProjection) -> GuildAudioProjection:
        if not isinstance(projection, GuildAudioProjection):
            raise TypeError("projection must be a GuildAudioProjection")
        payload = _canonical_audio_projection(projection)
        digest = hashlib.sha256(payload.encode("utf-8", errors="strict")).hexdigest()
        with self._lock, self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO music_guild_audio_projections
                    (guild_id, schema_version, payload_json, payload_sha256)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    payload_json = excluded.payload_json,
                    payload_sha256 = excluded.payload_sha256,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    projection.guild_id,
                    projection.schema_version,
                    payload,
                    digest,
                ),
            )
        return projection

    def load_audio_projection(self, guild_id: int) -> GuildAudioProjection | None:
        guild_id = _guild_id(guild_id)
        with self._lock:
            row = (
                self._required()
                .execute(
                    "SELECT * FROM music_guild_audio_projections WHERE guild_id = ?",
                    (guild_id,),
                )
                .fetchone()
            )
            if row is None:
                return None
            try:
                return _decode_audio_projection(row)
            except _ProjectionDecodeError as exc:
                self._quarantine_audio_projection(row, reason_code=exc.reason_code)
                return None

    def list_audio_projections(self) -> tuple[GuildAudioProjection, ...]:
        with self._lock:
            rows = self._required().execute("SELECT * FROM music_guild_audio_projections ORDER BY guild_id").fetchall()
            values: list[GuildAudioProjection] = []
            for row in rows:
                try:
                    values.append(_decode_audio_projection(row))
                except _ProjectionDecodeError as exc:
                    self._quarantine_audio_projection(row, reason_code=exc.reason_code)
            return tuple(values)

    def delete_audio_projection(self, guild_id: int) -> bool:
        guild_id = _guild_id(guild_id)
        with self._lock, self._transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM music_guild_audio_projections WHERE guild_id = ?",
                (guild_id,),
            )
        return cursor.rowcount == 1

    def save_dashboard_binding(
        self,
        binding: MusicDashboardBinding,
        *,
        commit_current: Callable[[], bool] | None = None,
    ) -> MusicDashboardBinding:
        if not isinstance(binding, MusicDashboardBinding):
            raise TypeError("binding must be a MusicDashboardBinding")
        with self._lock, self._transaction(commit_current=commit_current) as connection:
            connection.execute(
                """
                INSERT INTO music_dashboard_bindings
                    (guild_id, channel_id, message_id, owner_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    message_id = excluded.message_id,
                    owner_id = excluded.owner_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    binding.guild_id,
                    binding.channel_id,
                    binding.message_id,
                    binding.owner_id,
                ),
            )
        return binding

    def load_dashboard_binding(self, guild_id: int) -> MusicDashboardBinding | None:
        guild_id = _guild_id(guild_id)
        with self._lock:
            row = (
                self._required()
                .execute(
                    """
                    SELECT guild_id, channel_id, message_id, owner_id
                    FROM music_dashboard_bindings
                    WHERE guild_id = ?
                    """,
                    (guild_id,),
                )
                .fetchone()
            )
        return None if row is None else _dashboard_binding(row)

    def list_dashboard_bindings(self) -> tuple[MusicDashboardBinding, ...]:
        with self._lock:
            rows = (
                self._required()
                .execute(
                    """
                    SELECT guild_id, channel_id, message_id, owner_id
                    FROM music_dashboard_bindings
                    ORDER BY guild_id
                    """
                )
                .fetchall()
            )
        values: list[MusicDashboardBinding] = []
        for row in rows:
            try:
                values.append(_dashboard_binding(row))
            except (TypeError, ValueError):
                continue
        return tuple(values)

    def dashboard_binding_is_current(self, binding: MusicDashboardBinding) -> bool:
        if not isinstance(binding, MusicDashboardBinding):
            return False
        try:
            current = self.load_dashboard_binding(binding.guild_id)
        except (RuntimeError, sqlite3.Error, ValueError):
            return False
        return current == binding

    def delete_dashboard_binding(self, binding: MusicDashboardBinding) -> bool:
        if not isinstance(binding, MusicDashboardBinding):
            raise TypeError("binding must be a MusicDashboardBinding")
        with self._lock, self._transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM music_dashboard_bindings
                WHERE guild_id = ? AND channel_id = ? AND message_id = ? AND owner_id = ?
                """,
                (
                    binding.guild_id,
                    binding.channel_id,
                    binding.message_id,
                    binding.owner_id,
                ),
            )
        return cursor.rowcount == 1

    def _quarantine_audio_projection(self, row: sqlite3.Row, *, reason_code: str) -> bool:
        if reason_code not in _QUARANTINE_REASONS:
            raise ValueError("unsupported audio projection quarantine reason")
        row_sha256 = _content_free_row_sha256(row)
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM music_guild_audio_projections
                WHERE guild_id = ? AND schema_version = ? AND payload_json = ?
                    AND payload_sha256 = ? AND updated_at = ?
                """,
                (
                    row["guild_id"],
                    row["schema_version"],
                    row["payload_json"],
                    row["payload_sha256"],
                    row["updated_at"],
                ),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                """
                INSERT INTO music_guild_audio_projection_quarantine
                    (guild_id, reason_code, row_sha256)
                VALUES (?, ?, ?)
                """,
                (int(row["guild_id"]), reason_code, row_sha256),
            )
        return True

    @contextmanager
    def _transaction(
        self,
        *,
        commit_current: Callable[[], bool] | None = None,
    ) -> Iterator[sqlite3.Connection]:
        connection = self._required()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            if commit_current is not None:
                try:
                    current = commit_current()
                except Exception:
                    current = False
                if current is not True:
                    connection.rollback()
                    raise PlaylistError("music import runtime identity changed")
            connection.commit()

    def _required(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("music repository is closed")
        return self._connection


def _ids(guild_id: int, owner_id: int) -> tuple[int, int]:
    if guild_id <= 0 or owner_id <= 0:
        raise ValueError("guild_id and owner_id must be positive")
    return guild_id, owner_id


def _guild_id(guild_id: int) -> int:
    if isinstance(guild_id, bool) or not isinstance(guild_id, int) or guild_id <= 0:
        raise ValueError("guild_id must be positive")
    return guild_id


def _track_rights_values(track_key: str, content_sha256: str) -> tuple[str, str]:
    key = str(track_key or "")
    digest = str(content_sha256 or "").casefold()
    if (
        not 1 <= len(key) <= 1_024
        or any(ord(char) < 32 for char in key)
        or not key.startswith("root-")
        or ":" not in key
    ):
        raise PlaylistError("track rights key is invalid")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise PlaylistError("track rights digest is invalid")
    return key, digest


def _imported_asset(row: sqlite3.Row) -> ImportedMusicAsset:
    return ImportedMusicAsset(
        library_ref=str(row["library_ref"]),
        content_sha256=str(row["content_sha256"]),
        display_title=str(row["display_title"]),
        size_bytes=int(row["size_bytes"]),
        duration_milliseconds=int(row["duration_milliseconds"]),
    )


def _dashboard_binding(row: sqlite3.Row) -> MusicDashboardBinding:
    return MusicDashboardBinding(
        guild_id=int(row["guild_id"]),
        channel_id=int(row["channel_id"]),
        message_id=int(row["message_id"]),
        owner_id=int(row["owner_id"]),
    )


def _playlist_name(value: str) -> tuple[str, str]:
    name = " ".join(str(value or "").split())
    if not 1 <= len(name) <= 64 or any(ord(char) < 32 for char in name):
        raise PlaylistError("playlist name is invalid")
    return name, name.casefold()


def _track_titles(values: tuple[str, ...], *, maximum: int) -> tuple[str, ...]:
    if not values or len(values) > maximum:
        raise PlaylistError("playlist track count is invalid")
    titles = tuple(str(value or "").strip() for value in values)
    if any(not title or len(title) > 200 for title in titles):
        raise PlaylistError("playlist track title is invalid")
    return titles


def _canonical_audio_projection(projection: GuildAudioProjection) -> str:
    value = {
        "guild_id": projection.guild_id,
        "loop_mode": projection.loop_mode.value,
        "music_volume": projection.music_volume,
        "paused": projection.paused,
        "schema_version": projection.schema_version,
        "speech_volume": projection.speech_volume,
        "tracks": [
            {
                "content_sha256": track.content_sha256,
                "library_ref": track.library_ref,
                "requester_id": track.requester_id,
                "retry_count": track.retry_count,
            }
            for track in projection.tracks
        ],
    }
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(payload.encode("utf-8", errors="strict")) > _AUDIO_PROJECTION_MAX_BYTES:
        raise ValueError("audio projection exceeds the canonical payload limit")
    return payload


def _decode_audio_projection(row: sqlite3.Row) -> GuildAudioProjection:
    schema_version = row["schema_version"]
    if type(schema_version) is not int or schema_version != PERSISTED_MUSIC_SCHEMA_VERSION:
        raise _ProjectionDecodeError("unknown_schema")
    payload = row["payload_json"]
    stored_digest = row["payload_sha256"]
    if not isinstance(payload, str):
        raise _ProjectionDecodeError("payload_invalid")
    try:
        encoded = payload.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise _ProjectionDecodeError("payload_invalid") from exc
    if not 1 <= len(encoded) <= _AUDIO_PROJECTION_MAX_BYTES:
        raise _ProjectionDecodeError("payload_invalid")
    actual_digest = hashlib.sha256(encoded).hexdigest()
    if (
        not isinstance(stored_digest, str)
        or len(stored_digest) != 64
        or any(character not in "0123456789abcdef" for character in stored_digest)
        or not hmac.compare_digest(stored_digest, actual_digest)
    ):
        raise _ProjectionDecodeError("digest_mismatch")
    try:
        decoded = json.loads(
            payload,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
        if not isinstance(decoded, dict) or frozenset(decoded) != _PROJECTION_KEYS:
            raise ValueError("projection keys are invalid")
        if type(decoded["schema_version"]) is not int:
            raise ValueError("schema_version must be an integer")
        if decoded["schema_version"] != PERSISTED_MUSIC_SCHEMA_VERSION:
            raise _ProjectionDecodeError("unknown_schema")
        if type(decoded["guild_id"]) is not int or decoded["guild_id"] != int(row["guild_id"]):
            raise ValueError("guild_id is invalid")
        if not isinstance(decoded["tracks"], list) or len(decoded["tracks"]) > 100:
            raise ValueError("tracks are invalid")
        tracks: list[PersistedMusicTrackRef] = []
        for item in decoded["tracks"]:
            if not isinstance(item, dict) or frozenset(item) != _TRACK_KEYS:
                raise ValueError("track keys are invalid")
            if type(item["requester_id"]) is not int or type(item["retry_count"]) is not int:
                raise ValueError("track numeric values are invalid")
            tracks.append(
                PersistedMusicTrackRef(
                    library_ref=item["library_ref"],
                    content_sha256=item["content_sha256"],
                    requester_id=item["requester_id"],
                    retry_count=item["retry_count"],
                )
            )
        if not isinstance(decoded["loop_mode"], str) or not isinstance(decoded["paused"], bool):
            raise ValueError("projection state is invalid")
        if type(decoded["music_volume"]) not in (int, float) or type(decoded["speech_volume"]) not in (int, float):
            raise ValueError("projection volume is invalid")
        return GuildAudioProjection(
            guild_id=decoded["guild_id"],
            tracks=tuple(tracks),
            loop_mode=LoopMode(decoded["loop_mode"]),
            paused=decoded["paused"],
            music_volume=decoded["music_volume"],
            speech_volume=decoded["speech_volume"],
            schema_version=decoded["schema_version"],
        )
    except _ProjectionDecodeError:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _ProjectionDecodeError("payload_invalid") from exc


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON numbers are forbidden")


def _content_free_row_sha256(row: sqlite3.Row) -> str:
    digest = hashlib.sha256()
    for name in ("guild_id", "schema_version", "payload_json", "payload_sha256", "updated_at"):
        value: Any = row[name]
        if isinstance(value, bytes):
            encoded = value
        elif isinstance(value, str):
            encoded = value.encode("utf-8", errors="surrogatepass")
        else:
            encoded = str(value).encode("ascii", errors="backslashreplace")
        digest.update(name.encode("ascii"))
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()
