from __future__ import annotations

from dataclasses import dataclass, field
import math
import re

from yonerai_discord.modules.audio_core import LoopMode, QueueSnapshot, Track
from yonerai_discord.secret_detection import contains_secret_like


PERSISTED_MUSIC_SCHEMA_VERSION = 1
MAX_PERSISTED_MUSIC_TRACKS = 100
_LOCAL_MEDIA_REF = re.compile(r"root-(?:0|[1-9][0-9]{0,3}):(?P<relative>[^\\]{1,1000})\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class MusicError(RuntimeError):
    """利用者へ安全な固定文言で返せるmusic domain error。"""


class MusicUnavailableError(MusicError):
    pass


class MusicAuthorizationError(MusicError):
    pass


class MusicSessionError(MusicError):
    pass


class MusicSeekUnsupportedError(MusicSessionError):
    pass


class PlaylistError(MusicError):
    pass


def normalize_imported_music_title(value: object) -> str:
    title = " ".join(str(value or "").split())
    if (
        not 1 <= len(title) <= 100
        or any(ord(character) < 32 or ord(character) == 127 for character in title)
        or contains_secret_like(title)
    ):
        raise ValueError("display_title is invalid")
    return title


@dataclass(frozen=True, slots=True)
class PersistedMusicTrackRef:
    library_ref: str = field(repr=False)
    content_sha256: str = field(repr=False)
    requester_id: int
    retry_count: int = 0

    def __post_init__(self) -> None:
        reference = self.library_ref if isinstance(self.library_ref, str) else ""
        match = _LOCAL_MEDIA_REF.fullmatch(reference)
        relative = "" if match is None else match.group("relative")
        parts = relative.split("/")
        if (
            match is None
            or relative.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
            or any(character in relative for character in (":", "*", "?", "[", "]", "#"))
            or any(ord(character) < 32 or ord(character) == 127 for character in reference)
            or contains_secret_like(reference)
        ):
            raise ValueError("library_ref must be a safe root-relative media reference")
        digest = self.content_sha256.casefold() if isinstance(self.content_sha256, str) else ""
        if _SHA256.fullmatch(digest) is None:
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest")
        if isinstance(self.requester_id, bool) or not isinstance(self.requester_id, int) or self.requester_id <= 0:
            raise ValueError("requester_id must be a positive integer")
        if (
            isinstance(self.retry_count, bool)
            or not isinstance(self.retry_count, int)
            or not 0 <= self.retry_count <= 10
        ):
            raise ValueError("retry_count must be between 0 and 10")
        object.__setattr__(self, "library_ref", reference)
        object.__setattr__(self, "content_sha256", digest)


@dataclass(frozen=True, slots=True)
class AuthorizedMusicTrackRef:
    """Guild rights-ledger identity without a requester binding."""

    library_ref: str = field(repr=False)
    content_sha256: str = field(repr=False)

    def __post_init__(self) -> None:
        validated = PersistedMusicTrackRef(
            library_ref=self.library_ref,
            content_sha256=self.content_sha256,
            requester_id=1,
        )
        object.__setattr__(self, "library_ref", validated.library_ref)
        object.__setattr__(self, "content_sha256", validated.content_sha256)


@dataclass(frozen=True, slots=True)
class ImportedMusicAsset:
    """Persistent metadata for one content-addressed private WAV."""

    library_ref: str = field(repr=False)
    content_sha256: str = field(repr=False)
    display_title: str
    size_bytes: int
    duration_milliseconds: int

    def __post_init__(self) -> None:
        validated = PersistedMusicTrackRef(
            library_ref=self.library_ref,
            content_sha256=self.content_sha256,
            requester_id=1,
        )
        title = normalize_imported_music_title(self.display_title)
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or not 44 <= self.size_bytes <= 8 * 1024 * 1024
        ):
            raise ValueError("size_bytes is invalid")
        if (
            isinstance(self.duration_milliseconds, bool)
            or not isinstance(self.duration_milliseconds, int)
            or not 1_000 <= self.duration_milliseconds <= 30_000
        ):
            raise ValueError("duration_milliseconds is invalid")
        object.__setattr__(self, "library_ref", validated.library_ref)
        object.__setattr__(self, "content_sha256", validated.content_sha256)
        object.__setattr__(self, "display_title", title)


@dataclass(frozen=True, slots=True)
class GuildAudioProjection:
    guild_id: int
    tracks: tuple[PersistedMusicTrackRef, ...]
    loop_mode: LoopMode = LoopMode.OFF
    paused: bool = False
    music_volume: float = 0.75
    speech_volume: float = 1.0
    schema_version: int = PERSISTED_MUSIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.guild_id, bool) or not isinstance(self.guild_id, int) or self.guild_id <= 0:
            raise ValueError("guild_id must be a positive integer")
        if not isinstance(self.tracks, tuple) or len(self.tracks) > MAX_PERSISTED_MUSIC_TRACKS:
            raise ValueError("tracks must be an ordered tuple with at most 100 entries")
        if any(not isinstance(track, PersistedMusicTrackRef) for track in self.tracks):
            raise TypeError("tracks must contain PersistedMusicTrackRef values")
        if not isinstance(self.paused, bool):
            raise TypeError("paused must be a boolean")
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != PERSISTED_MUSIC_SCHEMA_VERSION
        ):
            raise ValueError("unsupported guild audio projection schema version")
        try:
            loop_mode = LoopMode(self.loop_mode)
        except (TypeError, ValueError) as exc:
            raise ValueError("loop_mode is invalid") from exc
        for label, volume in (("music_volume", self.music_volume), ("speech_volume", self.speech_volume)):
            if (
                isinstance(volume, bool)
                or not isinstance(volume, (int, float))
                or not math.isfinite(float(volume))
                or not 0.0 <= float(volume) <= 2.0
            ):
                raise ValueError(f"{label} must be finite and between 0.0 and 2.0")
        object.__setattr__(self, "loop_mode", loop_mode)
        object.__setattr__(self, "music_volume", float(self.music_volume))
        object.__setattr__(self, "speech_volume", float(self.speech_volume))


@dataclass(frozen=True, slots=True)
class MusicDashboardBinding:
    guild_id: int
    channel_id: int
    message_id: int
    owner_id: int

    def __post_init__(self) -> None:
        for label, value in (
            ("guild_id", self.guild_id),
            ("channel_id", self.channel_id),
            ("message_id", self.message_id),
            ("owner_id", self.owner_id),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{label} must be a positive integer")


@dataclass(frozen=True, slots=True)
class MusicActor:
    user_id: int
    voice_channel_id: int | None
    manage_guild: bool = False

    def __post_init__(self) -> None:
        if self.user_id <= 0:
            raise ValueError("user_id must be positive")
        if self.voice_channel_id is not None and self.voice_channel_id <= 0:
            raise ValueError("voice_channel_id must be positive")


@dataclass(frozen=True, slots=True)
class MusicRuntimeStatus:
    available: bool
    reason: str
    indexed_tracks: int
    active_sessions: int
    speech_available: bool


@dataclass(frozen=True, slots=True)
class PlaylistRecord:
    guild_id: int
    owner_id: int
    name: str
    track_titles: tuple[str, ...]


@dataclass(slots=True)
class SessionBinding:
    session: object
    voice_channel_id: int
    joined_by_id: int


def control_track(snapshot: QueueSnapshot, binding: SessionBinding) -> Track | None:
    if snapshot.current is not None:
        return snapshot.current
    if snapshot.upcoming:
        return snapshot.upcoming[0]
    return None
