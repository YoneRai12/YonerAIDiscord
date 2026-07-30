from __future__ import annotations

import math
import re
import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from yonerai_discord.secret_detection import contains_secret_like


_LOCAL_MEDIA_REF = re.compile(r"root-(?:0|[1-9][0-9]{0,3}):(?P<relative>[^\\]{1,1000})\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_RETRY_COUNT = 10


class LoopMode(StrEnum):
    OFF = "off"
    TRACK = "track"
    QUEUE = "queue"


class RecentTrackState(StrEnum):
    COMPLETED = "completed"
    SKIPPED = "skipped"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RecentTrack:
    title: str
    requester_id: int
    state: RecentTrackState

    def __post_init__(self) -> None:
        title = self.title.strip()
        if not title or len(title) > 200:
            raise ValueError("recent track title is invalid")
        if type(self.requester_id) is not int or self.requester_id <= 0:
            raise ValueError("requester_id must be positive")
        try:
            state = RecentTrackState(self.state)
        except (TypeError, ValueError) as exc:
            raise ValueError("recent track state is invalid") from exc
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "state", state)


@dataclass(frozen=True, slots=True)
class Track:
    title: str
    source: Path = field(repr=False)
    requester_id: int
    track_id: str = field(default_factory=lambda: secrets.token_hex(8))
    library_ref: str | None = field(default=None, repr=False)
    content_sha256: str | None = field(default=None, repr=False)
    retry_count: int = 0

    def __post_init__(self) -> None:
        title = self.title.strip()
        source = Path(self.source)
        if not title or len(title) > 200:
            raise ValueError("track title is invalid")
        if self.requester_id <= 0:
            raise ValueError("requester_id must be positive")
        if not self.track_id or len(self.track_id) > 64:
            raise ValueError("track_id is invalid")
        library_ref, content_sha256, retry_count = _normalize_persistence_fields(
            library_ref=self.library_ref,
            content_sha256=self.content_sha256,
            retry_count=self.retry_count,
        )
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "library_ref", library_ref)
        object.__setattr__(self, "content_sha256", content_sha256)
        object.__setattr__(self, "retry_count", retry_count)


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    current: Track | None
    upcoming: tuple[Track, ...]
    loop_mode: LoopMode
    paused: bool
    volume: float
    speech_volume: float = 1.0
    revision: int = 0
    recent: tuple[RecentTrack, ...] = ()

    def __post_init__(self) -> None:
        if (
            isinstance(self.speech_volume, bool)
            or not isinstance(self.speech_volume, (int, float))
            or not math.isfinite(self.speech_volume)
            or not 0.0 <= self.speech_volume <= 2.0
        ):
            raise ValueError("speech_volume must be finite and between 0.0 and 2.0")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revision must be a non-negative integer")
        if (
            not isinstance(self.recent, tuple)
            or len(self.recent) > 20
            or any(not isinstance(item, RecentTrack) for item in self.recent)
        ):
            raise ValueError("recent must contain at most 20 RecentTrack values")
        object.__setattr__(self, "speech_volume", float(self.speech_volume))


def _normalize_persistence_fields(
    *,
    library_ref: str | None,
    content_sha256: str | None,
    retry_count: int,
) -> tuple[str | None, str | None, int]:
    if (library_ref is None) != (content_sha256 is None):
        raise ValueError("library_ref and content_sha256 must be provided together")
    if type(retry_count) is not int or not 0 <= retry_count <= _MAX_RETRY_COUNT:
        raise ValueError("retry_count must be between 0 and 10")
    if library_ref is None:
        return None, None, retry_count
    reference = library_ref if isinstance(library_ref, str) else ""
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
    digest = content_sha256.casefold() if isinstance(content_sha256, str) else ""
    if _SHA256.fullmatch(digest) is None:
        raise ValueError("content_sha256 must be a lowercase SHA-256 digest")
    return reference, digest, retry_count
