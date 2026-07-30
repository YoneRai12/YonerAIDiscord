from __future__ import annotations

import math
from io import BytesIO
from pathlib import Path
from typing import Protocol, runtime_checkable

import discord

from .mixer import PCMSource
from .models import Track


class TrackSourceFactory(Protocol):
    def create(self, track: Track) -> PCMSource: ...

    def create_speech(self, wav: bytes) -> PCMSource: ...


@runtime_checkable
class SeekableTrackSourceFactory(TrackSourceFactory, Protocol):
    def create_at(self, track: Track, seconds: float) -> PCMSource: ...


class FfmpegPCMSourceFactory:
    """trustedなFFmpegとlocal libraryだけをPCM sourceへ変換する。"""

    def __init__(self, executable: Path, allowed_roots: tuple[Path, ...]) -> None:
        resolved_executable = Path(executable).expanduser().resolve(strict=False)
        if not resolved_executable.is_file():
            raise ValueError("FFmpeg executable does not exist")
        roots = tuple(dict.fromkeys(Path(root).expanduser().resolve(strict=False) for root in allowed_roots))
        if not roots:
            raise ValueError("at least one authorized media root is required")
        self.executable = resolved_executable
        self.allowed_roots = roots

    def create(self, track: Track) -> PCMSource:
        source = self._resolve_track(track)
        return discord.FFmpegPCMAudio(
            str(source),
            executable=str(self.executable),
            before_options="-nostdin -hide_banner -loglevel error",
            options="-vn",
        )

    def create_at(self, track: Track, seconds: float) -> PCMSource:
        seek_seconds = _normalize_seek_seconds(seconds)
        source = self._resolve_track(track)
        return discord.FFmpegPCMAudio(
            str(source),
            executable=str(self.executable),
            before_options=(f"-ss {seek_seconds:.3f} -nostdin -hide_banner -loglevel error"),
            options="-vn",
        )

    def create_speech(self, wav: bytes) -> PCMSource:
        if not wav.startswith(b"RIFF") or len(wav) > 50 * 1024 * 1024:
            raise ValueError("speech WAV is invalid")
        return discord.FFmpegPCMAudio(
            BytesIO(wav),
            executable=str(self.executable),
            pipe=True,
            before_options="-nostdin -hide_banner -loglevel error",
            options="-vn",
        )

    def _allowed(self, source: Path) -> bool:
        for root in self.allowed_roots:
            try:
                source.relative_to(root)
            except ValueError:
                continue
            return True
        return False

    def _resolve_track(self, track: Track) -> Path:
        if not isinstance(track, Track):
            raise TypeError("track must be a Track")
        source = track.source.expanduser().resolve(strict=True)
        if not source.is_file() or not self._allowed(source):
            raise ValueError("track is outside the authorized local library")
        return source


def _normalize_seek_seconds(value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0.0 <= value <= 86_400.0:
        raise ValueError("seek position must be between 0 and 86400 seconds")
    return float(value)
