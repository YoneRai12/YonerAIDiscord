from __future__ import annotations

import math
import sys
import threading
from array import array
from collections import deque
from dataclasses import dataclass
from collections.abc import Callable
from typing import Protocol

import discord


PCM_SAMPLE_RATE = 48_000
PCM_CHANNELS = 2
PCM_SAMPLE_WIDTH = 2
PCM_FRAME_MILLISECONDS = 20
PCM_FRAME_BYTES = PCM_SAMPLE_RATE * PCM_CHANNELS * PCM_SAMPLE_WIDTH * PCM_FRAME_MILLISECONDS // 1_000


class PCMSource(Protocol):
    def read(self) -> bytes: ...

    def is_opus(self) -> bool: ...

    def cleanup(self) -> None: ...


@dataclass(frozen=True, slots=True)
class MixerSnapshot:
    music_active: bool
    music_paused: bool
    speech_active: bool
    queued_speech: int
    effective_music_gain: float


class DuckingMixer(discord.AudioSource):
    """20 ms PCM frame単位でBGMとTTSを重ね、TTS中だけBGMを下げる。"""

    def __init__(
        self,
        music: PCMSource | None = None,
        *,
        music_volume: float = 0.75,
        speech_volume: float = 1.0,
        ducking_ratio: float = 0.30,
        attack_frames: int = 10,
        release_frames: int = 40,
        hold_frames: int = 8,
        on_retire: Callable[[PCMSource], None] | None = None,
    ) -> None:
        # discord.AudioSource.__del__は初期化途中の例外時もcleanup()を呼ぶ。
        self._lock = threading.RLock()
        self._closed = True
        self._music: PCMSource | None = None
        self._speech_queue: deque[PCMSource] = deque()
        self._speech: PCMSource | None = None
        self._music_paused = False
        if not _volume_is_valid(music_volume) or not _volume_is_valid(speech_volume):
            raise ValueError("volume must be between 0 and 2")
        if not 0.0 <= ducking_ratio <= 1.0:
            raise ValueError("ducking_ratio must be between 0 and 1")
        if attack_frames < 1 or release_frames < 1 or hold_frames < 0:
            raise ValueError("fade frames are invalid")
        _require_pcm(music)
        self._music = music
        self._music_volume = music_volume
        self._speech_volume = speech_volume
        self._ducking_ratio = ducking_ratio
        self._attack_step = (1.0 - ducking_ratio) / attack_frames
        self._release_step = (1.0 - ducking_ratio) / release_frames
        self._hold_frames = hold_frames
        self._remaining_hold_frames = 0
        self._music_gain = 1.0
        self._on_retire = on_retire
        self._closed = False

    def is_opus(self) -> bool:
        return False

    def add_speech(self, source: PCMSource) -> None:
        _require_pcm(source)
        if source is None:
            raise TypeError("source must not be None")
        with self._lock:
            if self._closed:
                raise RuntimeError("mixer is closed")
            self._speech_queue.append(source)

    def set_music_volume(self, volume: float) -> None:
        if not _volume_is_valid(volume):
            raise ValueError("volume must be between 0 and 2")
        with self._lock:
            self._music_volume = volume

    def set_speech_volume(self, volume: float) -> None:
        if not _volume_is_valid(volume):
            raise ValueError("volume must be between 0 and 2")
        with self._lock:
            self._speech_volume = volume

    def pause_music(self) -> None:
        with self._lock:
            if self._music is not None:
                self._music_paused = True

    def resume_music(self) -> None:
        with self._lock:
            self._music_paused = False

    def clear_music(self) -> None:
        with self._lock:
            music, self._music = self._music, None
            self._music_paused = False
        self._retire(music)

    def replace_music(self, source: PCMSource) -> PCMSource:
        """Replace only the music source while preserving speech and ducking state."""

        _require_pcm(source)
        if source is None:
            raise TypeError("source must not be None")
        with self._lock:
            if self._closed:
                raise RuntimeError("mixer is closed")
            if self._music is None:
                raise RuntimeError("music is not active")
            previous, self._music = self._music, source
        return previous

    def read(self) -> bytes:
        with self._lock:
            if self._closed:
                return b""
            speech_frame = self._read_speech_locked()
            speech_active = bool(speech_frame)
            if speech_active:
                self._remaining_hold_frames = self._hold_frames
                ducking_active = True
            elif self._remaining_hold_frames > 0:
                self._remaining_hold_frames -= 1
                ducking_active = True
            else:
                ducking_active = False
            self._advance_ducking(ducking_active)

            music_frame = b""
            if self._music is not None and self._music_paused:
                music_frame = bytes(PCM_FRAME_BYTES)
            elif self._music is not None:
                music_frame = self._music.read()
                if not music_frame:
                    music, self._music = self._music, None
                    self._retire(music)

            if not music_frame and not speech_frame:
                return b""
            return mix_pcm16le(
                music_frame,
                speech_frame,
                primary_gain=self._music_volume * self._music_gain,
                overlay_gain=self._speech_volume,
            )

    def snapshot(self) -> MixerSnapshot:
        with self._lock:
            return MixerSnapshot(
                music_active=self._music is not None,
                music_paused=self._music_paused,
                speech_active=self._speech is not None,
                queued_speech=len(self._speech_queue),
                effective_music_gain=self._music_volume * self._music_gain,
            )

    def cleanup(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sources = [self._music, self._speech, *self._speech_queue]
            self._music = None
            self._speech = None
            self._speech_queue.clear()
        for source in sources:
            self._retire(source)

    def _read_speech_locked(self) -> bytes:
        while True:
            if self._speech is None:
                if not self._speech_queue:
                    return b""
                self._speech = self._speech_queue.popleft()
            frame = self._speech.read()
            if frame:
                return frame
            speech, self._speech = self._speech, None
            self._retire(speech)

    def _advance_ducking(self, speech_active: bool) -> None:
        if speech_active:
            self._music_gain = max(self._ducking_ratio, self._music_gain - self._attack_step)
        else:
            self._music_gain = min(1.0, self._music_gain + self._release_step)

    def _retire(self, source: PCMSource | None) -> None:
        if source is None:
            return
        callback = self._on_retire
        if callback is None:
            _cleanup(source)
            return
        callback(source)


def mix_pcm16le(primary: bytes, overlay: bytes, *, primary_gain: float, overlay_gain: float) -> bytes:
    """Discord PCM用のstereo signed 16-bit little-endian frameをsaturating mixする。"""

    if primary_gain < 0.0 or overlay_gain < 0.0:
        raise ValueError("gain must not be negative")
    primary_frame = _frame(primary)
    overlay_frame = _frame(overlay)
    primary_samples = array("h")
    primary_samples.frombytes(primary_frame)
    overlay_samples = array("h")
    overlay_samples.frombytes(overlay_frame)
    if sys.byteorder != "little":
        primary_samples.byteswap()
        overlay_samples.byteswap()
    mixed = array(
        "h",
        (
            max(-32_768, min(32_767, round(left * primary_gain + right * overlay_gain)))
            for left, right in zip(primary_samples, overlay_samples, strict=True)
        ),
    )
    if sys.byteorder != "little":
        mixed.byteswap()
    return mixed.tobytes()


def _frame(value: bytes) -> bytes:
    if not value:
        return bytes(PCM_FRAME_BYTES)
    if len(value) >= PCM_FRAME_BYTES:
        return value[:PCM_FRAME_BYTES]
    return value + bytes(PCM_FRAME_BYTES - len(value))


def _require_pcm(source: PCMSource | None) -> None:
    if source is not None and source.is_opus():
        raise ValueError("ducking mixer requires PCM sources")


def _cleanup(source: PCMSource | None) -> None:
    if source is not None:
        source.cleanup()


def _volume_is_valid(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0.0 <= value <= 2.0
