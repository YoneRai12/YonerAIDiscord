from __future__ import annotations

import hashlib
import re
import secrets
import struct
import wave
from dataclasses import dataclass
from io import BytesIO
from threading import RLock

from yonerai_discord.provider_registry import ArtifactKind, ArtifactRef

from .domain import MAX_AUDIO_BYTES


MIN_AUDIO_SECONDS = 1.0
MAX_AUDIO_SECONDS = 30.0
ALLOWED_SAMPLE_RATES = frozenset({44_100, 48_000})
ALLOWED_CHANNELS = frozenset({1, 2})
DEFAULT_MAX_AUDIO_ARTIFACTS = 16
DEFAULT_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_ARTIFACT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,95}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class SpeechAudioArtifactError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ValidatedWav:
    size_bytes: int
    sha256: str
    channels: int
    sample_rate: int
    frame_count: int
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class _StoredWav:
    ref: ArtifactRef
    request_binding: str
    data: bytes


def validate_pcm_wav(data: bytes) -> ValidatedWav:
    if not isinstance(data, bytes) or not 44 <= len(data) <= MAX_AUDIO_BYTES:
        raise SpeechAudioArtifactError("WAV size is outside the allowed range")
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise SpeechAudioArtifactError("audio must be RIFF/WAVE")
    if struct.unpack_from("<I", data, 4)[0] + 8 != len(data):
        raise SpeechAudioArtifactError("RIFF size does not match downloaded bytes")
    try:
        with wave.open(BytesIO(data), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            frame_count = source.getnframes()
            compression = source.getcomptype()
            frames = source.readframes(frame_count)
            trailing = source.readframes(1)
    except (EOFError, wave.Error, struct.error):
        raise SpeechAudioArtifactError("WAV structure is invalid") from None
    if sample_width != 2 or compression != "NONE":
        raise SpeechAudioArtifactError("audio must be uncompressed PCM16")
    if channels not in ALLOWED_CHANNELS or sample_rate not in ALLOWED_SAMPLE_RATES or frame_count <= 0:
        raise SpeechAudioArtifactError("WAV format is outside the allowed range")
    if len(frames) != frame_count * channels * sample_width or trailing:
        raise SpeechAudioArtifactError("WAV frame data does not match its header")
    duration = frame_count / sample_rate
    if not MIN_AUDIO_SECONDS <= duration <= MAX_AUDIO_SECONDS:
        raise SpeechAudioArtifactError("WAV duration is outside the allowed range")
    return ValidatedWav(
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        channels=channels,
        sample_rate=sample_rate,
        frame_count=frame_count,
        duration_seconds=duration,
    )


class BoundedSpeechAudioStore:
    """request-bound WAV bytesをprocess内だけで有界保持する。"""

    def __init__(
        self,
        *,
        max_artifacts: int = DEFAULT_MAX_AUDIO_ARTIFACTS,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    ) -> None:
        if isinstance(max_artifacts, bool) or not isinstance(max_artifacts, int) or not 1 <= max_artifacts <= 256:
            raise ValueError("max_artifacts is outside the allowed range")
        if (
            isinstance(max_total_bytes, bool)
            or not isinstance(max_total_bytes, int)
            or not MAX_AUDIO_BYTES <= max_total_bytes <= 512 * 1024 * 1024
        ):
            raise ValueError("max_total_bytes is outside the allowed range")
        self.max_artifacts = max_artifacts
        self.max_total_bytes = max_total_bytes
        self._items: dict[str, _StoredWav] = {}
        self._total_bytes = 0
        self._closing = False
        self._lock = RLock()

    def describe_wav(self, data: bytes, *, artifact_id: str | None = None) -> ArtifactRef:
        validated = validate_pcm_wav(data)
        safe_id = artifact_id or f"stt-{secrets.token_hex(16)}"
        if not isinstance(safe_id, str) or not _ARTIFACT_ID.fullmatch(safe_id):
            raise SpeechAudioArtifactError("artifact_id is invalid")
        return ArtifactRef(
            safe_id,
            ArtifactKind.AUDIO,
            "audio/wav",
            validated.size_bytes,
            validated.sha256,
        )

    def put_wav(self, ref: ArtifactRef, data: bytes, *, request_binding: str) -> None:
        validated = validate_pcm_wav(data)
        if (
            not isinstance(ref, ArtifactRef)
            or ref.kind is not ArtifactKind.AUDIO
            or ref.media_type != "audio/wav"
            or ref.size_bytes != validated.size_bytes
            or ref.sha256 != validated.sha256
            or not isinstance(request_binding, str)
            or not _SHA256.fullmatch(request_binding)
        ):
            raise SpeechAudioArtifactError("WAV artifact metadata or binding is invalid")
        with self._lock:
            if self._closing or ref.artifact_id in self._items:
                raise SpeechAudioArtifactError("WAV artifact store is unavailable")
            while self._items and (
                len(self._items) >= self.max_artifacts or self._total_bytes + len(data) > self.max_total_bytes
            ):
                oldest_id = next(iter(self._items))
                oldest = self._items.pop(oldest_id)
                self._total_bytes -= len(oldest.data)
            if self._total_bytes + len(data) > self.max_total_bytes:
                raise SpeechAudioArtifactError("WAV artifact exceeds store capacity")
            self._items[ref.artifact_id] = _StoredWav(ref, request_binding, data)
            self._total_bytes += len(data)

    def current(self, ref: ArtifactRef, request_binding: str) -> bool:
        with self._lock:
            item = self._items.get(getattr(ref, "artifact_id", ""))
            return (
                not self._closing and item is not None and item.ref is ref and item.request_binding == request_binding
            )

    def read_wav(self, ref: ArtifactRef, *, request_binding: str) -> bytes:
        with self._lock:
            item = self._items.get(getattr(ref, "artifact_id", ""))
            if self._closing or item is None or item.ref is not ref or item.request_binding != request_binding:
                raise SpeechAudioArtifactError("WAV artifact is not current")
            return item.data

    def discard(self, ref: ArtifactRef) -> bool:
        with self._lock:
            item = self._items.get(getattr(ref, "artifact_id", ""))
            if item is None or item.ref is not ref:
                return False
            self._items.pop(ref.artifact_id)
            self._total_bytes -= len(item.data)
            return True

    def begin_close(self) -> None:
        with self._lock:
            self._closing = True
            self._items.clear()
            self._total_bytes = 0


__all__ = [
    "ALLOWED_CHANNELS",
    "ALLOWED_SAMPLE_RATES",
    "BoundedSpeechAudioStore",
    "MAX_AUDIO_SECONDS",
    "MIN_AUDIO_SECONDS",
    "SpeechAudioArtifactError",
    "ValidatedWav",
    "validate_pcm_wav",
]
